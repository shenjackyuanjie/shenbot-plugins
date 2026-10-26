"""QQ 生图任务的编排：审核、润色、预览确认、生成与原群回图。

只在外层 worker 线程运行；所有外部请求先进入 PG，再携带同一请求 id 发送。
取消、断线和异常都不会自动重放收费生成；结果不确定时保留原记录并明确告知。
登录态由调用方注入，避免本模块反向导入插件入口造成循环依赖。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import requests

from .archive import Archive, ArchiveError
from .providers import ProviderError, codex_call, deepseek_call
from .transport import TransportError

MAX_PROMPT = 2000
MAX_REPLY_IMAGE_BYTES = 8 * 1024 * 1024


class ImageError(RuntimeError):
    """可直接展示给群成员的固定提示，不包含上游正文或认证。"""


@dataclass(frozen=True)
class ImageSettings:
    pg_connection: str = field(repr=False, default="")
    deepseek_api_key: str = field(repr=False, default="")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_timeout: float = 90
    image_timeout: float = 300


def _download(url: str) -> bytes:
    """流式下载并提前拒绝超大响应，不把任意 CDN 内容整块读进内存。"""
    try:
        with requests.get(url, timeout=(5, 20), stream=True) as response:
            if response.status_code != 200:
                raise ImageError("下载引用图片失败，请重新发送。")
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_REPLY_IMAGE_BYTES:
                raise ImageError("引用图片过大，第一版仅支持 8MB 以内。")
            chunks, size = [], 0
            for block in response.iter_content(65536):
                size += len(block)
                if size > MAX_REPLY_IMAGE_BYTES:
                    raise ImageError("引用图片过大，第一版仅支持 8MB 以内。")
                chunks.append(block)
    except requests.RequestException:
        raise ImageError("下载引用图片失败，请稍后重试。") from None
    if not chunks:
        raise ImageError("引用的图片为空，请重新发送。")
    return b"".join(chunks)


def reference_image(client, msg) -> bytes | None:
    """只接受一张引用图片；多图歧义直接拒绝，不擅自挑选。"""
    reply = getattr(msg, "reply", None) or {}
    files = list(reply.get("files") or [])
    if not files and reply.get("file"):
        files = [reply["file"]]
    images = [item for item in files if str(item.get("type", "")).startswith("image/")]
    if not images:
        if files:
            raise ImageError("引用的消息里没有可编辑的图片。")
        return None
    if len(images) > 1:
        raise ImageError("一次只能引用一张图片，请重新引用。")
    item = images[0]
    url = item.get("url")
    if not url:
        fid = item.get("fid")
        if not fid:
            raise ImageError("无法读取引用图片，请重新发送。")
        try:
            url = client.get_nt_pic_url_by_file_id(fid)
        except (RuntimeError, ValueError):
            raise ImageError("无法从 QQ 取回引用图片，请重新发送。") from None
    return _download(url)


class ImageService:
    """短生命周期实例；认证从注入的 login_provider 读取，不在实例中保存明文副本。"""

    def __init__(
        self,
        settings: ImageSettings,
        login_provider,
        account: int,
        archive: Archive | None = None,
    ):
        self.settings = settings
        self.login_provider = login_provider
        self.account = account
        self._archive = archive

    @property
    def archive(self) -> Archive:
        # 懒初始化让额度查询等既有功能在未配置 PG 时继续工作。
        if self._archive is None:
            if not self.settings.pg_connection:
                raise ImageError("尚未配置 PostgreSQL 连接，生图功能不可用。")
            self._archive = Archive(self.settings.pg_connection)
        return self._archive

    def owner(self, msg) -> tuple[int, int, int]:
        """消息本身不携带机器人账号，必须用连接状态的 self_id 绑定归属。"""
        if not isinstance(self.account, int) or self.account <= 0:
            raise ImageError("当前连接缺少可用机器人账号。")
        return self.account, msg.room_id, msg.sender_id

    def _call(self, call, job_id, stopped: threading.Event):
        """登记 started 后只发送一次；失败与不确定分别归档，绝不重试。"""
        archive = self.archive
        request_id = archive.begin_request(
            job_id, call.provider, call.stage, call.model, call.archive_payload
        )
        try:
            result = call.send(request_id, stopped)
        except TransportError as error:
            state = "unknown" if error.code.endswith("UNCERTAIN") else "failed"
            archive.finish_request(request_id, state, error_code=error.code)
            raise
        try:
            parsed = call.parse(result)
        except ProviderError as error:
            archive.finish_request(
                request_id, "failed", http_status=result.status, error_code=str(error)
            )
            raise
        archive.finish_request(
            request_id,
            "succeeded",
            parsed.value,
            http_status=result.status,
            upstream_id=result.request_id,
            usage=result.body.get("usage") if isinstance(result.body, dict) else None,
        )
        for role, data in parsed.images:
            archive.put_image(job_id, role, data, request_id)
        return request_id, parsed

    def create(self, msg, client, prompt: str) -> str:
        """依次执行前审、润色、后审，冻结草稿并返回预览文本。"""
        prompt = prompt.strip()
        if not 0 < len(prompt) <= MAX_PROMPT:
            raise ImageError(f"图片描述需为 1–{MAX_PROMPT} 字。")
        if not self.settings.deepseek_api_key:
            raise ImageError("尚未配置 DeepSeek API Key，生图功能不可用。")
        image = reference_image(client, msg)
        archive = self.archive
        job = archive.create_job(self.owner(msg), msg.id, prompt)
        if image is not None:
            archive.put_image(job, "reference", image)
        stopped = threading.Event()

        def review(stage: str, content: str, original: str | None = None):
            call = deepseek_call(
                self.settings.deepseek_base_url,
                self.settings.deepseek_api_key,
                self.settings.deepseek_model,
                stage,
                content,
                original=original,
                image=image,
            )
            return self._call(call, job, stopped)

        pre, verdict = review("pre_review", prompt)
        if verdict.value["decision"] != "pass":
            archive.finish_job(job, "blocked")
            raise ImageError("内容未通过 NSFW 审核：" + verdict.value["reason"])
        _, rewritten = review("rewrite", prompt)
        post, verdict = review(
            "post_review", rewritten.value["prompt"], original=prompt
        )
        if verdict.value["decision"] != "pass":
            archive.finish_job(job, "blocked")
            raise ImageError("润色结果未通过 NSFW 审核：" + verdict.value["reason"])
        archive.prepare_draft(job, rewritten.value["prompt"], pre, post)
        short = str(job)[:8]
        return (
            f"任务 {short} 待确认\n提示词：{rewritten.value['prompt']}\n"
            f"发送 /codex image confirm {short} 开始生成，或 /codex image cancel {short} 取消。"
            "10 分钟内有效。"
        )

    def confirm(self, msg, client, prefix: str) -> str:
        """确认后只发起一次收费生成；成功返回回图说明，失败抛出可展示错误。"""
        archive = self.archive
        owner = self.owner(msg)
        job = archive.owned_job_by_prefix(prefix, owner)
        login = self.login_provider()
        archive.claim_confirmation(job["id"], owner)
        image = archive.image(job["id"], "reference")
        try:
            call = codex_call(
                job["reviewed_prompt"], login.access, login.account_id, image=image
            )
            self._call(call, job["id"], threading.Event())
        except (ProviderError, TransportError, ArchiveError) as error:
            code = getattr(error, "code", None) or str(error)
            state = "unknown" if "UNCERTAIN" in code else "failed"
            archive.finish_job(job["id"], state)
            raise ImageError(
                f"生成未完成（{code}）。结果不确定时不会自动重试，"
                f"请用 /codex image status {prefix} 查看记录。"
            ) from None
        archive.finish_job(job["id"], "succeeded")
        data = archive.image(job["id"], "original")
        if data is None:
            raise ImageError("生成已完成但未取到图片存档，请检查数据库。")
        sent = msg.reply_with("已生成图片")
        sent.set_img(data, "image/png", False)
        client.send_message(sent)
        return "已生成图片并发送到当前群。"

    def status(self, msg, prefix: str) -> str:
        job = self.archive.owned_job_by_prefix(prefix, self.owner(msg))
        return f"任务 {prefix} 当前状态：{job['state']}。"

    def cancel(self, msg, prefix: str) -> str:
        job = self.archive.owned_job_by_prefix(prefix, self.owner(msg))
        self.archive.cancel(job["id"], self.owner(msg))
        return f"任务 {prefix} 已取消。"
