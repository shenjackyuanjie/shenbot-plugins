"""QQ 生图任务的编排：审核、润色、预览确认、生成与原群回图。

只在外层 worker 线程运行；所有外部请求先进入 PG，再携带同一请求 id 发送。
取消、断线和异常都不会自动重放收费生成；结果不确定时保留原记录并明确告知。
登录态由调用方注入，避免本模块反向导入插件入口造成循环依赖。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import requests

from .archive import Archive, ArchiveError
from .providers import ProviderError, codex_call, deepseek_call
from .transport import TransportError

MAX_PROMPT = 2000
MAX_REPLY_IMAGE_BYTES = 8 * 1024 * 1024
# 多图改图沿用上游工具的同一次引用上限，避免一次请求塞进过多参考。
MAX_REFERENCE_IMAGES = 5


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


def _elapsed(started: float) -> str:
    return f"{time.monotonic() - started:.1f}s"


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


def reference_images(client, msg) -> list[bytes]:
    """收集引用消息里的全部图片，按消息中的顺序返回；超过上限直接拒绝。"""
    reply = getattr(msg, "reply", None) or {}
    files = list(reply.get("files") or [])
    if not files and reply.get("file"):
        files = [reply["file"]]
    images = [item for item in files if str(item.get("type", "")).startswith("image/")]
    if not images:
        if files:
            raise ImageError("引用的消息里没有可编辑的图片。")
        return []
    if len(images) > MAX_REFERENCE_IMAGES:
        raise ImageError(f"一次最多引用 {MAX_REFERENCE_IMAGES} 张图片。")
    collected = []
    for item in images:
        url = item.get("url")
        if not url:
            fid = item.get("fid")
            if not fid:
                raise ImageError("无法读取引用图片，请重新发送。")
            try:
                url = client.get_nt_pic_url_by_file_id(fid)
            except (RuntimeError, ValueError):
                raise ImageError("无法从 QQ 取回引用图片，请重新发送。") from None
        collected.append(_download(url))
    return collected


class ImageService:
    """短生命周期实例；认证从注入的 login_provider 读取，不在实例中保存明文副本。"""

    def __init__(
        self,
        settings: ImageSettings,
        login_provider,
        account: int,
        archive: Archive | None = None,
        log=None,
    ):
        self.settings = settings
        self.login_provider = login_provider
        self.account = account
        self._archive = archive
        # 日志出口由插件入口注入宿主 API；未注入时静默，测试与独立调用不产生输出。
        self._log_sink = log

    def log(self, event: str, *, level: str = "info", **fields: object) -> None:
        """阶段日志交给宿主日志，只输出阶段、任务短号与计数。

        提示词、凭据、群消息内容都不进入日志，排查时用任务短号回查 PG 存档。
        """
        if self._log_sink is None:
            return
        detail = " ".join(f"{key}={value}" for key, value in fields.items())
        self._log_sink(level, f"[codex-image] {event}{' ' + detail if detail else ''}")

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
        short = str(job_id)[:8]
        started = time.monotonic()
        request_id = archive.begin_request(
            job_id, call.provider, call.stage, call.model, call.archive_payload
        )
        self.log("request", job=short, stage=call.stage, model=call.model)
        try:
            result = call.send(request_id, stopped)
        except TransportError as error:
            state = "unknown" if error.code.endswith("UNCERTAIN") else "failed"
            archive.finish_request(request_id, state, error_code=error.code)
            self.log(
                "request failed",
                level="warn",
                job=short,
                stage=call.stage,
                code=error.code,
                cost=_elapsed(started),
            )
            raise
        try:
            parsed = call.parse(result)
        except ProviderError as error:
            archive.finish_request(
                request_id, "failed", http_status=result.status, error_code=str(error)
            )
            self.log(
                "request rejected",
                level="warn",
                job=short,
                stage=call.stage,
                http=result.status,
                code=str(error),
                cost=_elapsed(started),
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
            self.log("image stored", job=short, role=role, bytes=len(data))
        self.log(
            "request ok",
            job=short,
            stage=call.stage,
            http=result.status,
            cost=_elapsed(started),
            usage=result.body.get("usage") if isinstance(result.body, dict) else None,
        )
        return request_id, parsed

    def create(
        self,
        msg,
        client,
        prompt: str,
        *,
        direct: bool = False,
        params: dict | None = None,
    ) -> str:
        """依次执行前审、（润色）、后审，冻结草稿与生成参数并返回预览文本。

        direct 只跳过润色：两次审核仍然独立执行，放行对象始终是最终要生成的提示词。
        """
        prompt = prompt.strip()
        if not 0 < len(prompt) <= MAX_PROMPT:
            raise ImageError(f"图片描述需为 1–{MAX_PROMPT} 字。")
        if not self.settings.deepseek_api_key:
            raise ImageError("尚未配置 DeepSeek API Key，生图功能不可用。")
        images = reference_images(client, msg)
        archive = self.archive
        # 缺省项一律按 auto 补齐：预览展示的参数就是确认后真正发给上游的参数。
        params = {
            "size": "auto",
            "quality": "auto",
            "background": "auto",
            **(params or {}),
        }
        job = archive.create_job(self.owner(msg), msg.id, prompt, params)
        short = str(job)[:8]
        self.log("create", job=short, chars=len(prompt), references=len(images))
        for position, data in enumerate(images):
            archive.put_image(job, "reference", data, position=position)
        stopped = threading.Event()

        def review(stage: str, content: str, original: str | None = None):
            call = deepseek_call(
                self.settings.deepseek_base_url,
                self.settings.deepseek_api_key,
                self.settings.deepseek_model,
                stage,
                content,
                original=original,
                images=tuple(images),
            )
            return self._call(call, job, stopped)

        pre, verdict = review("pre_review", prompt)
        self.log("pre_review", job=short, decision=verdict.value["decision"])
        if verdict.value["decision"] != "pass":
            archive.finish_job(job, "blocked")
            self.log("blocked", level="warn", job=short, stage="pre_review")
            raise ImageError("内容未通过 NSFW 审核：" + verdict.value["reason"])
        if direct:
            final_prompt = prompt
            self.log("direct", job=short)
        else:
            _, rewritten = review("rewrite", prompt)
            final_prompt = rewritten.value["prompt"]
            self.log("rewrite", job=short, chars=len(final_prompt))
        post, verdict = review("post_review", final_prompt, original=prompt)
        self.log("post_review", job=short, decision=verdict.value["decision"])
        if verdict.value["decision"] != "pass":
            archive.finish_job(job, "blocked")
            self.log("blocked", level="warn", job=short, stage="post_review")
            raise ImageError("润色结果未通过 NSFW 审核：" + verdict.value["reason"])
        archive.prepare_draft(job, final_prompt, pre, post)
        self.log("draft ready", job=short)
        mode = "直接生成（未润色）" if direct else "润色"
        return (
            f"任务 {short} 待确认\n模式：{mode}\n提示词：{final_prompt}\n"
            f"参数：尺寸 {params['size']} / 质量 {params['quality']} / 背景 {params['background']}"
            f"（可用 /codex image set {short} 调整）\n"
            f"发送 /codex image confirm {short} 开始生成，或 /codex image cancel {short} 取消。"
            "10 分钟内有效。"
        )

    def _receipt(self, client, msg, text: str, short: str) -> None:
        """回执与确认属于同一流程：发送失败只记日志，不改变已消费的确认。"""
        try:
            accepted = client.send_message(msg.reply_with(text))
        except (RuntimeError, ValueError):
            accepted = False
        if not accepted:
            self.log("receipt not accepted", job=short)

    def confirm(self, msg, client, prefix: str) -> None:
        """确认后只发起一次收费生成；先回执再回图，失败抛出可展示错误。"""
        archive = self.archive
        owner = self.owner(msg)
        job = archive.owned_job_by_prefix(prefix, owner)
        short = str(job["id"])[:8]
        login = self.login_provider()
        # 参数只从任务读取：确认命令不接受尺寸等选项，避免预览与生成不一致。
        params = job.get("image_params") or {}
        archive.claim_confirmation(job["id"], owner)
        images = archive.images(job["id"], "reference")
        self.log("confirmed", job=short, references=len(images))
        # 生成是分钟级操作，先回执再发请求，避免确认后群里长时间没有任何反馈。
        self._receipt(
            client,
            msg,
            f"任务 {prefix} 已确认，正在生成图片，通常需要 1–2 分钟。",
            short,
        )
        try:
            call = codex_call(
                job["reviewed_prompt"],
                login.access,
                login.account_id,
                images=tuple(images),
                size=params.get("size", "auto"),
                quality=params.get("quality", "auto"),
                background=params.get("background", "auto"),
            )
            self._call(call, job["id"], threading.Event())
        except (ProviderError, TransportError, ArchiveError) as error:
            code = getattr(error, "code", None) or str(error)
            state = "unknown" if "UNCERTAIN" in code else "failed"
            archive.finish_job(job["id"], state)
            self.log("generate failed", level="warn", job=short, state=state, code=code)
            raise ImageError(
                f"生成未完成（{code}）。结果不确定时不会自动重试，"
                f"请用 /codex image status {prefix} 查看记录。"
            ) from None
        archive.finish_job(job["id"], "succeeded")
        data = archive.image(job["id"], "original")
        if data is None:
            self.log("image missing", level="warn", job=short)
            raise ImageError("生成已完成但未取到图片存档，请检查数据库。")
        self.log("generated", job=short, bytes=len(data))
        sent = msg.reply_with("已生成图片")
        sent.set_img(data, "image/png", False)
        client.send_message(sent)
        self.log("delivered", job=short)

    def set_params(self, msg, prefix: str, params: dict) -> str:
        """确认前调整尺寸、质量或背景；返回合并后的完整参数供群里核对。"""
        archive = self.archive
        owner = self.owner(msg)
        job = archive.owned_job_by_prefix(prefix, owner)
        merged = archive.update_image_params(job["id"], owner, params)
        self.log(
            "params updated",
            job=str(job["id"])[:8],
            keys=",".join(sorted(params)),
        )
        return (
            f"任务 {prefix} 参数已更新：尺寸 {merged.get('size', 'auto')} / "
            f"质量 {merged.get('quality', 'auto')} / 背景 {merged.get('background', 'auto')}。\n"
            f"发送 /codex image confirm {prefix} 开始生成。"
        )

    def status(self, msg, prefix: str) -> str:
        job = self.archive.owned_job_by_prefix(prefix, self.owner(msg))
        return f"任务 {prefix} 当前状态：{job['state']}。"

    def cancel(self, msg, prefix: str) -> str:
        job = self.archive.owned_job_by_prefix(prefix, self.owner(msg))
        self.archive.cancel(job["id"], self.owner(msg))
        self.log("cancelled", job=str(job["id"])[:8])
        return f"任务 {prefix} 已取消。"
