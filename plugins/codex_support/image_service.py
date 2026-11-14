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
from .error_messages import format_error
from .providers import (
    ProviderError,
    codex_call,
    deepseek_call,
    openai_image_call,
    openai_text_call,
)
from .transport import TransportError

MAX_PROMPT = 2000
MAX_REPLY_IMAGE_BYTES = 8 * 1024 * 1024
# 多图改图沿用上游工具的同一次引用上限，避免一次请求塞进过多参考。
MAX_REFERENCE_IMAGES = 5
# 多张参考图会一起进审核请求，限制总量避免超长 Base64 请求体被上游直接拒绝。
MAX_REFERENCE_TOTAL_BYTES = 16 * 1024 * 1024


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
    image_backend: str = "codex"
    text_backend: str = "deepseek"
    codex_image_model: str = "gpt-image-2.6-flare"
    openai_text_api_key: str = field(repr=False, default="")
    openai_image_api_key: str = field(repr=False, default="")
    openai_base_url: str = "https://api.openai.com/v1"
    openai_text_model: str = "gpt-6-luna"
    openai_image_model: str = "gpt-image-2.6-flare"
    openai_reasoning_effort: str = "high"
    # 本机直连 api.openai.com 会超时；代理只影响 API 模式，不参与执行配置冻结。
    openai_proxy: str = field(repr=False, default="")

    def execution_config(self) -> dict:
        """仅冻结非秘密字段；密钥始终按用途读取当前配置。"""
        api_root = self.openai_base_url.rstrip("/").removesuffix("/v1") + "/v1"
        return {
            "image_backend": self.image_backend,
            "image_model": self.openai_image_model
            if self.image_backend == "openai"
            else self.codex_image_model,
            "image_base_url": api_root
            if self.image_backend == "openai"
            else "https://chatgpt.com/backend-api/codex",
            "text_backend": self.text_backend,
            "text_model": self.openai_text_model
            if self.text_backend == "openai"
            else self.deepseek_model,
            "text_base_url": api_root
            if self.text_backend == "openai"
            else self.deepseek_base_url.rstrip("/").removesuffix("/v1"),
            "text_reasoning_effort": self.openai_reasoning_effort
            if self.text_backend == "openai"
            else "high",
        }


# 新任务默认 2.6；保留已冻结的 2.5 别名与快照，不在升级时替换历史模型。
IMAGE_QUALITIES = ("auto", "low", "medium", "high", "xhigh", "max")
IMAGE_MODELS = frozenset(
    {
        "gpt-image-2.6-flare",
        "gpt-image-2.6-sunburst",
        "gpt-image-2.5-flare",
        "gpt-image-2.5-flare-2026-09-08",
        "gpt-image-2.5-sunburst",
        "gpt-image-2.5-sunburst-2026-09-08",
    }
)


def _select_image_model(execution: dict, params: dict) -> tuple[dict, dict]:
    """命令短名只用于选模型，实际身份保存在执行快照，不混进图片 API 参数。"""
    params = dict(params)
    execution = dict(execution)
    model = params.pop("model", None)
    if model is not None:
        if model not in {"flare", "sunburst"}:
            raise ImageError("模型可选：flare/sunburst。")
        execution["image_model"] = "gpt-image-2.6-" + model
    return execution, params


def _check_backend_params(execution: dict, params: dict) -> None:
    """按冻结模型与后端校验；旧模型在审核或消费确认前直接拒绝。"""
    if execution.get("image_model", "gpt-image-2.5-flare") not in IMAGE_MODELS:
        raise ImageError(
            "当前配置的模型不受支持，请改用 GPT Image 2.6 Flare/Sunburst 后重新发起。"
        )
    if params.get("quality", "auto") not in IMAGE_QUALITIES:
        raise ImageError(f"质量可选：{'/'.join(IMAGE_QUALITIES)}。")
    if execution.get("image_backend") != "openai":
        return
    size = params.get("size", "auto")
    width, _, height = size.partition("x")
    if size != "auto" and not (
        width.isdigit()
        and height.isdigit()
        and int(width) % 16 == 0
        and int(height) % 16 == 0
    ):
        raise ImageError("OpenAI API 模式的尺寸宽高需能被 16 整除，例如 1024x1536。")


def _backend_label(config: dict) -> str:
    model = config.get("image_model", "gpt-image-2.5-flare")
    return (
        f"OpenAI API（独立计费） / {model}"
        if config.get("image_backend", "codex") == "openai"
        else f"Codex 订阅 / 请求模型 {model}"
    )


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
                raise ImageError("参考图太大了，单张请控制在 8MB 以内。")
            chunks, size = [], 0
            for block in response.iter_content(65536):
                size += len(block)
                if size > MAX_REPLY_IMAGE_BYTES:
                    raise ImageError("参考图太大了，单张请控制在 8MB 以内。")
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
    total = 0
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
        data = _download(url)
        total += len(data)
        if total > MAX_REFERENCE_TOTAL_BYTES:
            raise ImageError("引用图片总量过大，请减少数量或压缩后重试。")
        collected.append(data)
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
        admins: frozenset[int] | None = None,
    ):
        self.settings = settings
        self.login_provider = login_provider
        self.account = account
        self._archive = archive
        # 宿主管理员列表；命中时可以在当前群代办别人的任务。
        self._admins = admins or frozenset()
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
            archive = Archive(self.settings.pg_connection)
            archive.initialize()
            self._archive = archive
        return self._archive

    def owner(self, msg) -> tuple[int, int, int]:
        """消息本身不携带机器人账号，必须用连接状态的 self_id 绑定归属。"""
        if not isinstance(self.account, int) or self.account <= 0:
            raise ImageError("当前连接缺少可用机器人账号。")
        return self.account, msg.room_id, msg.sender_id

    def scope(self, msg) -> tuple[int, int, int | None]:
        """管理员可以在当前群代办别人的任务；其他人仍然只能操作自己的。"""
        account, room, sender = self.owner(msg)
        delegated = sender in self._admins
        return (account, room, None if delegated else sender)

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
            # 失败也保留响应正文，否则只能看到分类码，没法判断是截断还是格式问题。
            archive.finish_request(
                request_id,
                "failed",
                result.body,
                http_status=result.status,
                error_code=str(error),
                upstream_id=result.request_id,
                usage=result.body.get("usage")
                if isinstance(result.body, dict)
                else None,
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
        """直出前审一次，润色前后各审一次；冻结草稿与参数并返回预览文本。"""
        prompt = prompt.strip()
        if not 0 < len(prompt) <= MAX_PROMPT:
            raise ImageError(f"图片描述需为 1–{MAX_PROMPT} 字。")
        execution, params = _select_image_model(
            self.settings.execution_config(), params or {}
        )
        self._text_key(execution)
        if (
            execution["image_backend"] == "openai"
            and not self.settings.openai_image_api_key
        ):
            raise ImageError("尚未配置 openai_image_api_key，生图功能不可用。")
        images = reference_images(client, msg)
        archive = self.archive
        # 缺省项一律按 auto 补齐：预览展示的参数就是确认后真正发给上游的参数。
        params = {
            "size": "auto",
            "quality": "auto",
            "background": "auto",
            **(params or {}),
        }
        _check_backend_params(execution, params)
        job = archive.create_job(
            self.owner(msg), msg.id, prompt, params, execution_config=execution
        )
        short = str(job)[:8]
        self.log("create", job=short, chars=len(prompt), references=len(images))
        for position, data in enumerate(images):
            archive.put_image(job, "reference", data, position=position)
        return self._review(
            job, prompt, images, direct=direct, params=params, execution=execution
        )

    def _review(
        self,
        job,
        prompt: str,
        images: list[bytes],
        *,
        direct: bool,
        params: dict,
        execution: dict,
    ) -> str:
        """直出只审核原始输入；润色后再独立后审，放行对象与生成输入保持一致。"""
        archive = self.archive
        short = str(job)[:8]
        stopped = threading.Event()

        def review(stage: str, content: str, original: str | None = None):
            if execution.get("text_backend", "deepseek") == "openai":
                call = openai_text_call(
                    execution["text_base_url"],
                    self._text_key(execution),
                    execution["text_model"],
                    stage,
                    content,
                    original=original,
                    images=tuple(images),
                    reasoning_effort=execution["text_reasoning_effort"],
                    proxy=self.settings.openai_proxy,
                )
            else:
                call = deepseek_call(
                    execution.get("text_base_url", self.settings.deepseek_base_url),
                    self._text_key(execution),
                    execution.get("text_model", self.settings.deepseek_model),
                    stage,
                    content,
                    original=original,
                    images=tuple(images),
                )
            return self._call(call, job, stopped)

        try:
            pre, verdict = review("pre_review", prompt)
            self.log("pre_review", job=short, decision=verdict.value["decision"])
            if verdict.value["decision"] != "pass":
                archive.finish_job(job, "blocked")
                self.log("blocked", level="warn", job=short, stage="pre_review")
                raise ImageError("内容未通过 NSFW 审核：" + verdict.value["reason"])
            post = None
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
                    raise ImageError(
                        "润色结果未通过 NSFW 审核：" + verdict.value["reason"]
                    )
            archive.prepare_draft(job, final_prompt, pre, post)
        except (ProviderError, TransportError) as error:
            # 上游格式异常也带上任务号，否则群里只看到一句无法追查的失败提示。
            code = getattr(error, "code", None) or str(error)
            archive.finish_job(job, "failed")
            self.log("review failed", level="warn", job=short, code=code)
            raise ImageError(
                f"任务 {short} 的提示词处理失败\n{format_error(error)}"
                + f"\n可用 /codex status {short} 查看记录。"
            ) from None
        self.log("draft ready", job=short)
        if direct:
            # direct 没有润色，提示词就是用户原话，不再复述一遍。
            head = "模式：直接生成（按你的原始描述）\n"
        else:
            head = f"模式：润色\n提示词：{final_prompt}\n"
        return (
            f"任务 {short} 待确认\n"
            + f"后端：{_backend_label(execution)}\n"
            + head
            + f"参数：尺寸 {params['size']} / 质量 {params['quality']} / 背景 {params['background']}\n"
            "回复 /codex confirm 开始生成、/codex cancel 取消、/codex set --quality high 调参数。\n"
            "引用带图片的消息发 /codex attach 可以追加参考图；需要指定任务时带上开头的编号。\n"
            "10 分钟内有效。"
        )

    def _text_key(self, execution: dict) -> str:
        """文本和图片密钥不互借，包括其中一把尚未配置的情况。"""
        if execution.get("text_backend", "deepseek") == "openai":
            key, name = self.settings.openai_text_api_key, "openai_text_api_key"
        else:
            key, name = self.settings.deepseek_api_key, "deepseek_api_key"
        if not key:
            raise ImageError(f"尚未配置 {name}，提示词处理不可用。")
        return key

    def _receipt(self, client, msg, text: str, short: str) -> None:
        """回执与确认属于同一流程：发送失败只记日志，不改变已消费的确认。"""
        try:
            accepted = client.send_message(msg.reply_with(text))
        except (RuntimeError, ValueError):
            accepted = False
        if not accepted:
            self.log("receipt not accepted", job=short)

    def confirm(self, msg, client, prefix: str | None, *, retry: bool = False) -> None:
        """确认或显式重试后只生成一次；两者共用归档、回执与回图流程。"""
        if retry and not prefix:
            raise ImageError("重试请指定任务：/codex retry <8 位任务编号>。")
        archive = self.archive
        owner = self.scope(msg)
        job = archive.resolve_job(prefix, owner)
        short = str(job["id"])[:8]
        execution = job.get("execution_config") or {}
        # 升级前冻结的任务可能带着 API 端不接受的参数，确认前复核，避免消费确认后必然失败。
        _check_backend_params(execution, job.get("image_params") or {})
        # 验证用途凭据后才消费确认；API 任务完全不读取订阅 OAuth。
        login = None
        if execution.get("image_backend", "codex") == "openai":
            if not self.settings.openai_image_api_key:
                raise ImageError(
                    "尚未配置 openai_image_api_key，请在配置里填写后再确认。"
                )
        else:
            login = self.login_provider()
        if retry:
            job = archive.claim_retry(
                job["id"], owner, msg.id, operator_id=msg.sender_id
            )
        else:
            job = archive.claim_confirmation(job["id"], owner)
        execution = job.get("execution_config") or {}
        delegated = job["sender_id"] != msg.sender_id
        forced = retry and job.get("forced_retry", False)
        # 使用原子领取时的快照，参数、提示词与参考图都沿用原来已确认的内容。
        params = job.get("image_params") or {}
        images = archive.images(job["id"], "reference")
        self.log(
            "retry" if retry else "confirmed",
            job=short,
            references=len(images),
            forced=forced,
        )
        # 生成是分钟级操作，先回执再发请求，避免确认后群里长时间没有任何反馈。
        self._receipt(
            client,
            msg,
            f"{'管理员强制重试：' if forced else ('管理员代办：' if delegated else '')}"
            f"任务 {short} {'已开始重试（会再次消耗生成额度）' if retry else '已确认'}，"
            f"正在生成图片，通常需要 1–2 分钟。\n后端：{_backend_label(execution)}",
            short,
        )
        try:
            execution = job.get("execution_config") or {}
            options = {
                "images": tuple(images),
                "size": params.get("size", "auto"),
                "quality": params.get("quality", "auto"),
                "background": params.get("background", "auto"),
                "model": execution.get("image_model", "gpt-image-2.5-flare"),
            }
            if execution.get("image_backend", "codex") == "openai":
                call = openai_image_call(
                    job["reviewed_prompt"],
                    self.settings.openai_image_api_key,
                    base_url=execution["image_base_url"],
                    proxy=self.settings.openai_proxy,
                    **options,
                )
            else:
                call = codex_call(
                    job["reviewed_prompt"], login.access, login.account_id, **options
                )
            # 只计本次生成请求（含响应处理与存档），不计排队、审核和回图发送。
            started = time.monotonic()
            self._call(call, job["id"], threading.Event())
            generation_time = _elapsed(started)
        except (ProviderError, TransportError, ArchiveError) as error:
            code = getattr(error, "code", None) or str(error)
            state = (
                "blocked"
                if code == "CONTENT_REJECTED"
                else ("unknown" if "UNCERTAIN" in code else "failed")
            )
            archive.finish_job(job["id"], state)
            self.log("generate failed", level="warn", job=short, state=state, code=code)
            http_status = getattr(error, "http_status", None)
            detail = f"任务 {short} 生成失败\n{format_error(error)}"
            if code == "CONTENT_REJECTED":
                raise ImageError(
                    detail
                    + f"\n管理员可发送 /codex retry {short} 强制重试（会再次消耗额度）。"
                ) from None
            if http_status is not None and 400 <= http_status <= 599:
                raise ImageError(
                    detail
                    + f"\n发送 /codex retry {short} 可按原内容重试（会再次消耗额度）。"
                ) from None
            if code.startswith("INVALID_"):
                raise ImageError(
                    detail
                    + "\n请修正配置或参数后重新发起。"
                ) from None
            raise ImageError(
                detail
                + f"\n请用 /codex status {short} 查看任务记录。"
            ) from None
        if not archive.finish_job(job["id"], "succeeded"):
            # 取消终态优先；迟到结果已经归档，但不再自动回图。
            self.log("generation stopped", job=short)
            return
        data = archive.image(job["id"], "original")
        if data is None:
            self.log("image missing", level="warn", job=short)
            raise ImageError("生成已完成但未取到图片存档，请检查数据库。")
        self.log("generated", job=short, bytes=len(data), cost=generation_time)
        sent = msg.reply_with(f"已生成图片，用时 {generation_time}")
        sent.set_img(data, "image/png", False)
        client.send_message(sent)
        self.log("delivered", job=short)

    def attach(self, msg, client, prefix: str | None) -> str:
        """给待确认任务追加参考图；参考图参与审核，追加后必须重跑审核再给新预览。"""
        archive = self.archive
        owner = self.scope(msg)
        job = archive.resolve_job(prefix, owner)
        execution = job.get("execution_config") or {}
        self._text_key(execution)
        images = reference_images(client, msg)
        if not images:
            raise ImageError("请引用带图片的消息再执行 attach。")
        total = archive.append_references(job["id"], owner, images)
        merged = archive.images(job["id"], "reference")
        self.log(
            "references appended",
            job=str(job["id"])[:8],
            added=len(images),
            total=total,
        )
        # 沿用原任务的模式：跑过润色的任务继续润色，direct 任务保持直出。
        direct = not archive.has_stage(job["id"], "rewrite")
        stored = job.get("image_params") or {}
        params = {
            "size": stored.get("size", "auto"),
            "quality": stored.get("quality", "auto"),
            "background": stored.get("background", "auto"),
        }
        return self._review(
            job["id"],
            job["original_prompt"],
            merged,
            direct=direct,
            params=params,
            execution=execution,
        )

    def set_params(self, msg, prefix: str | None, params: dict) -> str:
        """确认前调整生成参数与图片模型；后端、审核模型和端点保持冻结。"""
        archive = self.archive
        owner = self.scope(msg)
        job = archive.resolve_job(prefix, owner)
        short = str(job["id"])[:8]
        selected_model = "model" in params
        execution, params = _select_image_model(
            job.get("execution_config") or {}, params
        )
        _check_backend_params(execution, {**(job.get("image_params") or {}), **params})
        updated = archive.update_image_params(
            job["id"],
            owner,
            params,
            image_model=execution["image_model"] if selected_model else None,
        )
        merged = updated["image_params"]
        execution = updated["execution_config"]
        self.log("params updated", job=short, keys=",".join(sorted(params)))
        return (
            f"任务 {short} 参数已更新：尺寸 {merged.get('size', 'auto')} / "
            f"质量 {merged.get('quality', 'auto')} / 背景 {merged.get('background', 'auto')}。\n"
            f"后端：{_backend_label(execution)}\n"
            f"发送 /codex image confirm {short} 开始生成。"
        )

    def status(self, msg, prefix: str | None) -> str:
        """有编号查那个任务；没有编号先看待确认任务，否则列出最近几条。"""
        owner = self.scope(msg)
        if prefix:
            job = self.archive.owned_job_by_prefix(prefix, owner)
            return f"任务 {prefix} 当前状态：{job['state']}。\n后端：{_backend_label(job.get('execution_config') or {})}"
        try:
            job = self.archive.pending_job(owner)
        except ArchiveError as error:
            rows = self.archive.recent_jobs(owner)
            if not rows:
                return str(error)
            lines = ["最近的任务（任务号：状态）："]
            lines.extend(
                f"{row['short']}：{row['state']}（{row['created_at']:%m-%d %H:%M}）"
                for row in rows
            )
            return "\n".join(lines)
        return f"待确认任务 {str(job['id'])[:8]}，10 分钟内有效。\n后端：{_backend_label(job.get('execution_config') or {})}"

    def cancel(self, msg, prefix: str | None) -> str:
        owner = self.scope(msg)
        job = self.archive.resolve_job(prefix, owner)
        short = str(job["id"])[:8]
        self.archive.cancel(job["id"], owner)
        self.log("cancelled", job=short)
        return f"任务 {short} 已取消。"
