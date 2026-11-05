"""Codex 订阅查询及可切换订阅/OpenAI API 的图片任务；不刷新订阅凭据。"""

from __future__ import annotations

import calendar
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlsplit

import yaml

from shenbot_api import (
    ConfigStorage,
    PluginManifest,
    Scheduler,
    info,
    warn,
    watch_ica_connection,
)

if TYPE_CHECKING:
    from ica_typing import IcaClient, IcaNewMessage
    from shenbot_api import IcaConnectionState, IcaConnectionWatch

VERSION = "0.8.0"
POLL_SECONDS = 120
THRESHOLD_STEP = 5
COMMAND = "/codex"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
# OpenAI 组织级已花费聚合；只有 Admin key + api.usage.read 可用，且没有「剩余余额」接口。
OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"
# 月度支出上限；threshold_amount 的单位是分，currency 为大写代码。
OPENAI_SPEND_LIMIT_URL = "https://api.openai.com/v1/organization/spend_limit"
OPENAI_USAGE_CACHE_SECONDS = 300
OPENAI_USAGE_DAYS = 7
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
DISPLAY_ZONE = timezone(timedelta(hours=8))
LOGIN_HELP = "请启动 DSH，在「设置 → Codex 订阅」刷新额度或重新登录后再试。"
HELP_TEXT = (
    "Codex 帮助\n"
    "/codex check：查询 DSH 当前 Codex 订阅的套餐、剩余额度和重置时间，与生图后端无关。\n"
    "/codex 或 /codex help：显示本帮助，不联网，不占用查询冷却。\n"
    "/codex image <描述>：润色并审核后预览提示词，本人确认后生成图片；可引用图片改图。\n"
    "/codex image direct <描述>：跳过润色，直接按原描述生成。\n"
    "/codex image attach <编号>：确认前追加参考图（引用含图片的消息）。\n"
    "/codex image set <编号>：确认前调整尺寸、质量、背景与模型。\n"
    "生图支持 --model flare|sunburst；--max 等同于 sunburst + 质量 max。\n"
    "Flare：日常高质量生图、速度最快；Sunburst：能力最强，侧重精细编辑。\n"
    "/codex image confirm|cancel|status <编号>：确认生成、取消或查看任务。\n"
    "/codex retry <编号>：按原内容重试明确失败的生图任务，会再次消耗额度。\n"
    "/codex image help：完整生图帮助，含命令、参数、尺寸与重试规则。\n"
    "生图子命令可以省略 image，例如直接发 /codex confirm、/codex cancel、/codex set。\n"
    f"自动提醒：每 {POLL_SECONDS // 60} 分钟检查，剩余额度跨过 95%、90%、85%……0% 时提醒；"
    "只发配置的提醒群。\n"
    "reset 警告：剩余额度比上次上涨时报告；上次不为 0% 时提示“浪费了xx%额度😭”。\n"
    "订阅额度与 OpenAI API 费用分开：API 计费在 OpenAI 控制台查看，本插件不查询。\n"
    "仅白名单群可用；登录失效请在 DSH 中刷新。"
)
IMAGE_HELP = (
    "Codex 生图速查\n"
    "/codex image [--max] <描述>：审核润色后等待确认；引用图片可改图。\n"
    "/codex direct <描述>：跳过润色，仍须审核与确认。\n"
    "/codex confirm|cancel|status [编号]：确认、取消、查看。\n"
    "/codex set [编号] --model flare|sunburst；--max = Sunburst + 质量 max。\n"
    "Flare：最快日常生图；Sunburst：能力最强，侧重精细编辑。\n"
    "完整命令、参数与规则：/codex image help"
)

IMAGE_DETAILED_HELP = (
    "Codex 生图完整帮助\n"
    "【生成与确认】\n"
    "/codex image <描述> [参数]：前审 → 润色 → 后审 → 预览，确认后才生成。\n"
    "/codex direct <描述> [参数]：不润色，只做一次前审，仍需确认。\n"
    "引用含图片的消息即可改图，最多 5 张；引用无图会拒绝，不退化成文生图。\n"
    "/codex confirm|cancel|status [编号]：确认、取消、查看；只有一个待确认任务可省略编号。\n"
    "草稿 10 分钟有效，确认只生效一次；生成按预览中的后端与参数执行。\n"
    "【确认前调整】\n"
    "/codex set [编号] <参数>：只改给出的参数，确认后不能再改。\n"
    "/codex attach [编号]：引用图片追加参考图并重新审核。\n"
    "【参数与模型】\n"
    "--model flare|sunburst；未指定时新任务沿用管理员配置，旧任务保留原模型。\n"
    "--max：固定 Sunburst + 质量 max，优先于同条命令的 model/quality，与顺序无关。\n"
    "--quality auto/low/medium/high/xhigh/max；--background auto/transparent/opaque。\n"
    "--size auto 或 宽x高；尺寸、质量、背景默认 auto，由上游决定。\n"
    "新任务选择 GPT Image 2.6 Flare/Sunburst；已有任务保留确认的模型。\n"
    "Flare：日常高质量生图的最快模型；Sunburst：能力最强的生图与编辑模型，侧重编辑精度。\n"
    "【尺寸】\n"
    "推荐 1024x1024、1536x1024、1024x1536 或 auto。\n"
    "插件目前每边限 512–2048，API 模式额外检查 16 倍数；这不是官方完整范围。\n"
    "API 官方还要求：比例 1:3～3:1、每边 ≤3840、总像素 655360～8294400；\n"
    "超过 2560x1440 属实验性支持。插件尚未完整校验比例和总像素，建议用推荐尺寸。\n"
    "【重试、权限与计费】\n"
    "/codex retry <编号>：明确 HTTP 失败后手动重试，沿用确认内容，会再次消耗额度。\n"
    "不自动重试；超时、断线或成功响应解析失败等结果不确定的任务不能重试。\n"
    "仅原群本人可操作；宿主管理员可代办 confirm/cancel/retry，并强制重试已确认的上游审核拒绝。\n"
    "后端由管理员配置，订阅额度与 API 独立计费分开；以任务预览为准，不自动换模型或后端。\n"
    "这些子命令可省略 image 前缀；返回简短速查：/codex image"
)

# 这些生图子命令可以省略 image 前缀，例如 /codex confirm 与 /codex image confirm。
IMAGE_SHORTCUTS = frozenset(
    {"confirm", "cancel", "status", "set", "attach", "direct", "retry"}
)

IMAGE_QUALITIES = ("auto", "low", "medium", "high", "xhigh", "max")
IMAGE_BACKGROUNDS = ("auto", "transparent", "opaque")


def _check_size(value: str) -> str | None:
    """只接受 auto 或 512–2048 的宽x高；无法解析的值直接拒绝，不猜上游支持范围。"""
    if value == "auto":
        return None
    width, _, height = value.partition("x")
    if (
        width.isdigit()
        and height.isdigit()
        and all(512 <= int(item) <= 2048 for item in (width, height))
    ):
        return None
    return "尺寸需为 auto 或 512–2048 之间的 宽x高，例如 1024x1536。"


def _check_choice(label: str, choices: tuple[str, ...]):
    def check(value: str) -> str | None:
        if value in choices:
            return None
        return f"{label}可选：{'/'.join(choices)}。"

    return check


IMAGE_OPTIONS = {
    "--size": _check_size,
    "--quality": _check_choice("质量", IMAGE_QUALITIES),
    "--background": _check_choice("背景", IMAGE_BACKGROUNDS),
    "--model": _check_choice("模型", ("flare", "sunburst")),
}


def _split_image_options(parts: list[str]) -> tuple[dict[str, str], str, str | None]:
    """剥离生图选项；--max 固定覆盖为 sunburst + max，与选项顺序无关。

    参数可出现在描述前后；未识别的词一律留在描述里，不吞用户内容。
    """
    options: dict[str, str] = {}
    rest: list[str] = []
    index = 0
    use_max = False
    while index < len(parts):
        token = parts[index]
        if token == "--max":
            use_max = True
            index += 1
            continue
        check = IMAGE_OPTIONS.get(token)
        if check is None:
            rest.append(token)
            index += 1
            continue
        if index + 1 >= len(parts):
            return options, "", f"{token} 需要一个取值。"
        value = parts[index + 1]
        error = check(value)
        if error:
            return options, "", error
        options[token[2:]] = value
        index += 2
    if use_max:
        options.update(model="sunburst", quality="max")
    return options, " ".join(rest).strip(), None


PLUGIN_MANIFEST = PluginManifest(
    plugin_id="codex",
    name="Codex 订阅与生图",
    version=VERSION,
    description="白名单群用 /codex check 查询订阅、/codex image 审核润色后确认生图；"
    "每两分钟检查额度下降与 reset，提醒只发配置的 alert_groups",
    authors=["shenjack"],
    config={
        "main": ConfigStorage(
            allowed_groups=[],
            dsh_home="",
            timeout_seconds=15,
            cooldown_seconds=15,
            # 生图配置：连接串与密钥只放本地被忽略配置，不进入日志或提交。
            pg_connection="",
            deepseek_api_key="",
            deepseek_base_url="https://api.deepseek.com",
            deepseek_model="deepseek-flash",
            image_backend="codex",
            text_backend="deepseek",
            codex_image_model="gpt-image-2.6-flare",
            # 两把用途独立的 Key 直接写在本地被忽略配置里，不进入提交与日志。
            openai_text_api_key="",
            openai_image_api_key="",
            # 可选：组织用量查询（Admin key + api.usage.read）；留空则 check 不发额外请求。
            openai_usage_api_key="",
            openai_base_url="https://api.openai.com/v1",
            openai_text_model="gpt-6-luna",
            openai_image_model="gpt-image-2.6-flare",
            openai_reasoning_effort="high",
            # 本机直连 api.openai.com 会超时，按需填写 HTTP 代理；留空则不使用代理。
            openai_proxy="",
            # 留空时额度提醒沿用 allowed_groups；填写后只发这些群。
            alert_groups=[],
            image_concurrency=3,
            image_queue_limit=5,
        )
    },
)


class QueryError(Exception):
    """只携带可以发送到群里的固定提示，绝不携带原始响应或凭据。"""

    def __init__(self, message: str, retry_after: float = 0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Settings:
    allowed_groups: frozenset[int] = frozenset()
    dsh_home: str = ""
    timeout_seconds: int = 15
    cooldown_seconds: int = 15
    pg_connection: str = field(default="", repr=False)
    deepseek_api_key: str = field(default="", repr=False)
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    image_backend: str = "codex"
    text_backend: str = "deepseek"
    codex_image_model: str = "gpt-image-2.6-flare"
    # 只用于请求头，不参与 repr、日志或存档。
    openai_text_api_key: str = field(default="", repr=False)
    openai_image_api_key: str = field(default="", repr=False)
    openai_usage_api_key: str = field(default="", repr=False)
    openai_base_url: str = "https://api.openai.com/v1"
    openai_text_model: str = "gpt-6-luna"
    openai_image_model: str = "gpt-image-2.6-flare"
    openai_reasoning_effort: str = "high"
    openai_proxy: str = field(default="", repr=False)
    alert_groups: frozenset[int] = frozenset()
    # 生图只有收费生成需要排队；审核与润色不占这两个名额。
    image_concurrency: int = 3
    image_queue_limit: int = 5

    def __post_init__(self) -> None:
        # 未单独配置提醒群时沿用白名单，避免升级后静默失去额度提醒。
        if not self.alert_groups:
            object.__setattr__(self, "alert_groups", self.allowed_groups)


@dataclass(frozen=True)
class Login:
    access: str = field(repr=False)
    account_id: str = field(repr=False)
    owner_id: str = field(default="", repr=False)


@dataclass(frozen=True)
class QuotaWindow:
    remaining: float  # 上次实际余额，用于检测上涨与计算未用额度。
    reset_at: float | None
    lowest_remaining: float  # 本周期最低余额，仅用于下降提醒去重。


@dataclass(frozen=True)
class QuotaSnapshot:
    account_id: str = field(repr=False)
    windows: dict[int, QuotaWindow]


class GenerationSlots:
    """限制同时执行的收费生成；达到并发上限时排队，队列满则直接拒绝。

    只覆盖生成这一步：审核、润色和建任务都不占名额，所以慢任务不会挡住别人提问。
    """

    def __init__(self, concurrency: int, max_waiting: int) -> None:
        self.concurrency = concurrency
        self.max_waiting = max_waiting
        self._condition = threading.Condition()
        self._running = 0
        self._waiting = 0
        self._closed = False

    def snapshot(self) -> tuple[int, int]:
        with self._condition:
            return self._running, self._waiting

    def acquire(self) -> bool:
        """占用一个生成名额；需要排队时阻塞，队列已满或插件卸载时返回 False。"""
        with self._condition:
            if self._running < self.concurrency:
                self._running += 1
                return True
            if self._waiting >= self.max_waiting:
                return False
            self._waiting += 1
            while self._running >= self.concurrency and not self._closed:
                self._condition.wait()
            self._waiting -= 1
            if self._closed:
                return False
            self._running += 1
            return True

    def release(self) -> None:
        with self._condition:
            self._running = max(0, self._running - 1)
            self._condition.notify()

    def close(self) -> None:
        """卸载时唤醒所有等待者，让它们按取消处理，不再发图。"""
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lock = threading.Lock()
        self.stopped = False
        self.busy = False
        self.next_query_at = 0.0
        self.baselines: dict[int, QuotaSnapshot] = {}
        self.poll_watch: IcaConnectionWatch | None = None
        self.poll_error: str | None = None
        self.image_slots = GenerationSlots(
            settings.image_concurrency, settings.image_queue_limit
        )


_runtime = Runtime(Settings())


def _positive_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def read_login(dsh_home: str) -> Login:
    # 每次查询重新读取：跟随 DSH 切账号、刷新凭据、重启，不持有 refresh token。
    home = dsh_home or os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    path = Path(os.path.expandvars(home)).expanduser() / ".credentials.yaml"
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise QueryError("DSH 登录文件超出大小限制，请检查 dsh_home 配置。")
        data = yaml.safe_load(raw.decode("utf-8-sig"))
    except FileNotFoundError:
        raise QueryError("未找到 DSH 登录文件，请检查插件的 dsh_home 配置。") from None
    except (OSError, UnicodeError, yaml.YAMLError):
        raise QueryError(
            "暂时无法读取 DSH 登录文件，请稍后重试并检查文件权限。"
        ) from None

    if not isinstance(data, dict) or data.get("version") != 1:
        raise QueryError("DSH 登录文件格式不兼容，请更新查询插件。")
    records = data.get("records")
    record = (
        records.get("codex-subscription/accounts")
        if isinstance(records, dict)
        else None
    )
    if record is None:
        raise QueryError("DSH 尚无 Codex 订阅账号库。" + LOGIN_HELP)
    payload = record.get("payload") if isinstance(record, dict) else None
    if (
        not isinstance(record, dict)
        or record.get("kind") != "grant"
        or not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(payload.get("activeId"), str)
        or not isinstance(payload.get("accounts"), list)
    ):
        raise QueryError("DSH Codex 账号库格式不兼容，请更新查询插件。")
    selected = [
        account
        for account in payload["accounts"]
        if isinstance(account, dict) and account.get("id") == payload["activeId"]
    ]
    if len(selected) != 1:
        raise QueryError("DSH 当前 Codex 账号不可用，请在 DSH 中重新选择账号。")
    credential = selected[0].get("credential")
    if (
        not isinstance(credential, dict)
        or credential.get("type") != "oauth"
        or not all(
            isinstance(credential.get(key), str) and credential[key].strip()
            for key in ("access", "accountId")
        )
        or not _positive_number(credential.get("expires"))
    ):
        raise QueryError("DSH 当前 Codex 登录态不完整。" + LOGIN_HELP)
    if credential["expires"] <= time.time() * 1000:
        raise QueryError("DSH 的 Codex 登录已过期。" + LOGIN_HELP)
    return Login(credential["access"], credential["accountId"], payload["activeId"])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 认证只发往固定官方地址，不跟随重定向转发到其他服务。
        return None


def _retry_after(value: str | None) -> float:
    try:
        seconds = float(value or "")
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value or "").timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return 60
    return max(1, seconds) if math.isfinite(seconds) else 60


def fetch_usage(login: Login, timeout: int) -> dict:
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": "Bearer " + login.access,
            "ChatGPT-Account-Id": login.account_id,
            "Accept": "application/json",
            "User-Agent": f"shenbot-codex/{VERSION}",
        },
    )
    try:
        # 使用系统/环境代理；不启动 DSH、不创建 OpenAI 会话、不写任何认证文件。
        with urllib.request.build_opener(_NoRedirect()).open(
            request, timeout=timeout
        ) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise QueryError("DSH 的 Codex 登录已失效。" + LOGIN_HELP) from None
        if error.code == 403:
            raise QueryError(
                "额度接口拒绝访问（HTTP 403），请检查网络、代理或账号权限。"
            ) from None
        if error.code == 429:
            raise QueryError(
                "额度接口请求过于频繁，请稍后再试。",
                _retry_after(error.headers.get("Retry-After")),
            ) from None
        if 300 <= error.code < 400:
            raise QueryError(
                "额度接口发生重定向，已停止查询，请检查网络或代理。"
            ) from None
        raise QueryError(
            f"额度服务暂时不可用（HTTP {error.code}），请稍后重试。"
        ) from None
    except (OSError, TimeoutError, urllib.error.URLError):
        raise QueryError("连接额度接口失败或超时，请检查网络与代理后重试。") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise QueryError("额度接口响应过大，已停止查询。")
    try:
        result = json.loads(raw)
    except (UnicodeError, ValueError):
        raise QueryError("额度接口返回了无效数据，请稍后重试。") from None
    if not isinstance(result, dict):
        raise QueryError("额度接口格式不兼容，请更新查询插件。")
    return result


def _when(timestamp: object) -> str:
    if not _positive_number(timestamp):
        return "未知"
    try:
        return datetime.fromtimestamp(timestamp, DISPLAY_ZONE).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except (ValueError, OverflowError, OSError):
        return "未知"


def _reset_time(timestamp: object, now: float) -> str:
    absolute = _when(timestamp)
    if absolute == "未知":
        return absolute
    remaining = timestamp - now
    if remaining <= 0:
        duration = "已到重置时间"
    elif remaining < 60:
        duration = "还剩不到 1 分钟"
    else:
        days, seconds = divmod(int(remaining), 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes = seconds // 60
        parts = []
        if days:
            parts.append(f"{days} 天")
        if days or hours:
            parts.append(f"{hours} 小时")
        if minutes:
            parts.append(f"{minutes} 分钟")
        duration = "还剩 " + " ".join(parts)
    return f"{absolute}（{duration}）"


def _window(value: object, fallback: str, now: float) -> str:
    if value is None:
        return f"{fallback}额度：未返回"
    if not isinstance(value, dict):
        raise QueryError("额度窗口格式不兼容，请更新查询插件。")
    used = value.get("used_percent")
    if (
        not isinstance(used, (int, float))
        or isinstance(used, bool)
        or not math.isfinite(used)
        or not 0 <= used <= 100
    ):
        raise QueryError("额度接口未返回有效的使用百分比，请稍后重试。")
    seconds = value.get("limit_window_seconds")
    label = (
        {18000: "5 小时", 604800: "每周"}.get(seconds, fallback)
        if isinstance(seconds, int)
        else fallback
    )
    if label == fallback and _positive_number(seconds):
        label = f"{seconds / 3600:g} 小时"
    resets = value.get("reset_at")
    if not _positive_number(resets) and _positive_number(
        value.get("reset_after_seconds")
    ):
        resets = now + value["reset_after_seconds"]
    return f"{label}额度：剩余 {100 - used:g}%（已用 {used:g}%）\n  重置：{_reset_time(resets, now)}"


def format_usage(usage: dict, now: float | None = None) -> str:
    now = time.time() if now is None else now
    plans = {
        "free": "Free",
        "go": "Go",
        "plus": "Plus",
        "pro": "Pro",
        "team": "Team",
        "business": "Business",
        "enterprise": "Enterprise",
        "edu": "Edu",
    }
    plan = usage.get("plan_type")
    plan_name = plans.get(plan, "未知") if isinstance(plan, str) else "未知"
    lines = ["Codex 订阅（DSH 当前账号）", f"套餐：{plan_name}"]
    rate = usage.get("rate_limit")
    if rate is None:
        lines.append("订阅额度：未返回（不代表剩余额度为 0）")
    elif isinstance(rate, dict):
        if rate.get("allowed") is False or rate.get("limit_reached") is True:
            lines.append("状态：当前订阅额度受限")
        lines.extend(
            [
                _window(rate.get("primary_window"), "短周期", now),
                _window(rate.get("secondary_window"), "长周期", now),
            ]
        )
    else:
        raise QueryError("额度接口格式不兼容，请更新查询插件。")
    credits = usage.get("credits")
    if isinstance(credits, dict):
        if credits.get("unlimited") is True:
            lines.append("额外 Credits：无限制")
        elif credits.get("has_credits") is False:
            lines.append("额外 Credits：无")
        elif credits.get("has_credits") is True:
            try:
                text = str(credits.get("balance", ""))
                balance = Decimal(text) if len(text) <= 32 else Decimal("NaN")
                lines.append(
                    f"额外 Credits：{balance}"
                    if balance.is_finite()
                    else "额外 Credits：未知"
                )
            except InvalidOperation:
                lines.append("额外 Credits：未知")
    lines.append(f"查询时间：{_when(now)}（UTC+8）")
    return "\n".join(lines)


class UsageError(Exception):
    """用量查询失败；只携带可直接展示的简短原因，不含凭据与原始响应。"""


# 用量结果缓存；配置重载时清空，避免每次都打接口。
_usage_cache: tuple[float, str] | None = None


def _reset_usage_cache() -> None:
    global _usage_cache
    _usage_cache = None


def _month_start(now: float) -> int:
    """UTC 本月 1 日 0 点的时间戳；用 gmtime 避免依赖 3.11 才有的 datetime.UTC。"""
    moment = time.gmtime(now)
    return calendar.timegm((moment.tm_year, moment.tm_mon, 1, 0, 0, 0, 0, 0, 0))


def _cost_query(now: float) -> str:
    """与月度上限同口径：UTC 本月 1 日至今，按天分桶，最多 180 桶。"""
    started = _month_start(now)
    days = min(180, int((now - started) // 86400) + 1)
    return urlencode(
        {
            "start_time": started,
            "bucket_width": "1d",
            "limit": days,
        }
    )


def _usage_error_message(body: bytes) -> str:
    """组织接口的 error 既可能是对象，也可能是一句话，两种都取。"""
    try:
        parsed = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    error = parsed.get("error")
    if isinstance(error, dict):
        return str(error.get("message", ""))
    return error if isinstance(error, str) else ""


def _usage_http_error(error: urllib.error.HTTPError) -> UsageError:
    try:
        message = _usage_error_message(error.read(64 * 1024))
    except OSError:
        message = ""
    if error.code in (401, 403) and "api.management.read" in message:
        return UsageError(
            "月度上限需 api.management.read 权限（Organization Admin key）"
        )
    if error.code in (401, 403) and "api.usage.read" in message:
        return UsageError(
            "该 Key 缺 api.usage.read，需要 Admin key（Settings → Organization → Admin keys）"
        )
    if 300 <= error.code < 400:
        return UsageError("用量接口发生重定向，已停止查询")
    # spend_limit 在该组织没有配置上限时返回 404，不是权限问题。
    if error.code == 404 and "spend limit" in message.lower():
        return UsageError(
            "组织未设置月度预算上限，可在 OpenAI 控制台 Settings → Limits 设置"
        )
    if error.code == 401:
        return UsageError("Key 认证失败（HTTP 401），请检查 openai_usage_api_key")
    if error.code == 403:
        return UsageError("Key 没有组织用量权限（HTTP 403）")
    if error.code == 429:
        return UsageError("用量接口被限流（HTTP 429），请稍后再查")
    return UsageError(f"用量服务暂时不可用（HTTP {error.code}）")


def _openai_get(url: str, settings: Settings, label: str) -> dict:
    """组织级只读接口：不重试、不跟随重定向，Key 只进请求头。"""
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + settings.openai_usage_api_key,
            "Accept": "application/json",
            "User-Agent": f"shenbot-codex/{VERSION}",
        },
    )
    handlers: list = [_NoRedirect()]
    if settings.openai_proxy:
        handlers.insert(
            0,
            urllib.request.ProxyHandler(
                {"http": settings.openai_proxy, "https": settings.openai_proxy}
            ),
        )
    try:
        with urllib.request.build_opener(*handlers).open(
            request, timeout=settings.timeout_seconds
        ) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise _usage_http_error(error) from None
    except (OSError, TimeoutError, urllib.error.URLError):
        raise UsageError(
            f"连接{label}接口失败或超时，请检查网络与 openai_proxy"
        ) from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise UsageError(f"{label}接口响应过大，已停止查询")
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise UsageError(f"{label}接口返回的不是有效 JSON") from None
    return parsed if isinstance(parsed, dict) else {}


def fetch_openai_costs(settings: Settings, now: float | None = None) -> dict:
    """读取 UTC 本月 1 日至今的组织已花费。"""
    return _openai_get(
        OPENAI_COSTS_URL + "?" + _cost_query(now or time.time()), settings, "用量"
    )


def fetch_openai_spend_limit(settings: Settings) -> dict:
    """读取月度支出上限；threshold_amount 单位是分，currency 是大写代码。"""
    return _openai_get(OPENAI_SPEND_LIMIT_URL, settings, "月度上限")


def _money(value: float, currency: str) -> str:
    amount = f"{value:.2f}" if value >= 0.01 else f"{value:.4f}"
    code = (currency or "usd").upper()
    return "$" + amount if code == "USD" else f"{code} {amount}"


def _spend_limit_value(payload: dict) -> tuple[float, str]:
    """threshold_amount 的单位是分（10000 = $100），costs 的金额是美元小数。"""
    threshold = payload.get("threshold_amount")
    if threshold is None:
        raise UsageError("月度上限响应缺少 threshold_amount")
    try:
        limit = float(Decimal(str(threshold))) / 100
    except (InvalidOperation, ValueError):
        raise UsageError("月度上限金额格式异常") from None
    currency = payload.get("currency")
    return limit, currency if isinstance(currency, str) and currency.strip() else "USD"


def format_openai_costs(
    payload: dict,
    *,
    spend_limit: tuple[float, str] | None = None,
    limit_hint: str = "",
) -> str:
    """汇总本月已花费；有月度上限时给出按上限剩余（估算），明细最多两天。"""
    buckets = payload.get("data")
    if not isinstance(buckets, list):
        raise UsageError("用量响应缺少 data 字段")
    days: list[tuple[float, float]] = []
    total = 0.0
    currency = ""
    for bucket in buckets:
        if not isinstance(bucket, dict):
            raise UsageError("用量响应格式异常")
        amount = 0.0
        for item in bucket.get("results") or []:
            money = item.get("amount") if isinstance(item, dict) else None
            value = money.get("value") if isinstance(money, dict) else None
            if value is None:
                continue
            # 上游金额可能是数字或字符串；解析失败必须报错，不能静默按 0 处理。
            try:
                amount += float(Decimal(str(value)))
            except (InvalidOperation, ValueError):
                raise UsageError("用量金额格式异常") from None
            code = money.get("currency")
            if not currency and isinstance(code, str) and code.strip():
                currency = code.strip()
        started = bucket.get("start_time")
        if isinstance(started, (int, float)):
            days.append((float(started), amount))
        total += amount
    lines = ["OpenAI API 用量（本月至今，已花费估算，不是余额）"]
    if spend_limit is not None:
        limit, limit_currency = spend_limit
        parts = [
            f"本月上限 {_money(limit, limit_currency)}",
            f"已花费估算 {_money(total, currency)}",
        ]
        # 只有同一币种才能相减；跨币种并列展示金额，不给出「剩余」。
        if (limit_currency or "usd").upper() == (currency or "usd").upper():
            remaining = max(limit - total, 0.0)
            parts.append(f"按上限剩余（估算）{_money(remaining, limit_currency)}")
        lines.append(" / ".join(parts))
    else:
        lines.append(f"合计：{_money(total, currency)}")
    spent = [(started, amount) for started, amount in days if amount > 0]
    for started, amount in list(reversed(spent))[:2]:
        try:
            stamp = datetime.fromtimestamp(started, DISPLAY_ZONE).strftime("%m-%d")
        except (ValueError, OverflowError, OSError):
            stamp = "未知日期"
        lines.append(f"{stamp}：{_money(amount, currency)}")
    if not spent:
        lines.append("本月至今没有已记录的花费")
    if limit_hint:
        lines.append(limit_hint)
    lines.append("余额与账单请到 OpenAI 控制台查看")
    return "\n".join(lines)


def openai_usage_section(settings: Settings, now: float | None = None) -> str:
    """返回追加到 check 的用量段；未配置 Key 时零请求、零输出。"""
    if not settings.openai_usage_api_key:
        return ""
    global _usage_cache
    moment = now or time.time()
    if _usage_cache is not None and _usage_cache[0] > moment:
        return _usage_cache[1]
    try:
        costs = fetch_openai_costs(settings, moment)
    except UsageError as error:
        text = f"OpenAI API 用量：{error}"
    else:
        spend_limit = None
        hint = ""
        try:
            spend_limit = _spend_limit_value(fetch_openai_spend_limit(settings))
        except UsageError as error:
            # 上限权限、未配置上限或接口失败都只追加一行，不影响已花费展示。
            message = str(error)
            hint = (
                message
                if "api.management.read" in message
                or message.startswith("组织未设置月度预算上限")
                else f"月度上限查询失败：{message}"
            )
        text = format_openai_costs(costs, spend_limit=spend_limit, limit_hint=hint)
    _usage_cache = (moment + OPENAI_USAGE_CACHE_SECONDS, text)
    return text


def query_subscription(settings: Settings) -> str:
    return format_usage(
        fetch_usage(read_login(settings.dsh_home), settings.timeout_seconds)
    )


def _reply(state: Runtime, msg: IcaNewMessage, client: IcaClient, text: str) -> None:
    with state.lock:
        if state.stopped:
            return
        try:
            if not client.send_message(msg.reply_with(text)):
                client.warn("codex：消息发送请求未受理")
        except Exception:
            client.warn("codex：发送查询结果失败")


def quota_snapshot(account_id: str, usage: dict) -> QuotaSnapshot:
    windows = {}
    rate = usage.get("rate_limit")
    if isinstance(rate, dict):
        for key in ("primary_window", "secondary_window"):
            value = rate.get(key)
            if not isinstance(value, dict):
                continue
            seconds = value.get("limit_window_seconds")
            if type(seconds) is not int or seconds <= 0:
                continue
            # format_usage 已校验百分比；缺少时长的窗口不猜测其身份。
            reset = value.get("reset_at")
            remaining = 100 - value["used_percent"]
            windows[seconds] = QuotaWindow(
                remaining, reset if _positive_number(reset) else None, remaining
            )
    return QuotaSnapshot(account_id, windows)


def quota_changes(
    previous: QuotaSnapshot | None, current: QuotaSnapshot
) -> tuple[list[str], QuotaSnapshot]:
    if previous is None or previous.account_id != current.account_id:
        return [], current
    # 缺失窗口保留最后的基准，恢复返回时仍能发现下降，不把缺失当作 0%。
    updated = dict(previous.windows)
    changes = []
    for seconds, value in current.windows.items():
        old = previous.windows.get(seconds)
        updated[seconds] = value
        if old is None:
            continue
        label = {18000: "5 小时", 604800: "每周"}.get(
            seconds, f"{seconds / 3600:g} 小时"
        )
        if value.remaining > old.remaining:
            notice = (
                f"⚠️ {label}额度 reset：剩余 {old.remaining:g}% → {value.remaining:g}%"
            )
            if old.remaining > 0:
                notice += f"，浪费了{old.remaining:g}%额度😭"
            changes.append(notice)
        if old.reset_at is not None and value.reset_at is not None:
            if old.reset_at != value.reset_at:
                continue
        elif value.remaining > old.remaining:
            # 接口未给重置时间时，用额度回升识别新周期。
            continue
        crossed = [
            threshold
            for threshold in range(100 - THRESHOLD_STEP, -1, -THRESHOLD_STEP)
            if value.remaining <= threshold < old.lowest_remaining
        ]
        if crossed:
            levels = " / ".join(f"{threshold}%" for threshold in crossed)
            changes.append(f"{label}额度：跨过 {levels} 提醒线")
        # 实际余额跟随每次成功查询；下降提醒仍按同周期最低值去重。
        updated[seconds] = QuotaWindow(
            value.remaining, value.reset_at, min(old.lowest_remaining, value.remaining)
        )
    return changes, QuotaSnapshot(current.account_id, updated)


def _poll_worker(
    state: Runtime, watch: IcaConnectionWatch, connection: IcaConnectionState
) -> None:
    try:
        login = read_login(state.settings.dsh_home)
        usage = fetch_usage(login, state.settings.timeout_seconds)
        summary = format_usage(usage)
        current = quota_snapshot(login.owner_id or login.account_id, usage)
        with state.lock:
            state.poll_error = None
        for group in sorted(state.settings.alert_groups):
            with state.lock:
                if state.stopped:
                    return
                previous = state.baselines.get(group)
            changes, baseline = quota_changes(previous, current)
            accepted = True
            if changes:
                text = "Codex 额度变动提醒\n" + "\n".join(changes) + "\n\n" + summary
                try:
                    accepted = watch.send_text(
                        connection.generation, connection.account, -group, text, 5.0
                    )
                except Exception:
                    # 宿主约定异常可能已外发；推进基准，避免下一轮盲目重复发送。
                    warn("codex：额度提醒发送结果不确定，不自动重发本次档位")
                if not accepted:
                    warn("codex：额度提醒未受理，保留该群基准等待下轮检查")
            if accepted:
                with state.lock:
                    if not state.stopped:
                        state.baselines[group] = baseline
    except Exception as error:
        with state.lock:
            if isinstance(error, QueryError):
                state.next_query_at = max(
                    state.next_query_at, time.monotonic() + error.retry_after
                )
            category = type(error).__name__
            if not state.stopped and state.poll_error != category:
                warn(f"codex：自动轮询暂时失败（{category}），后续轮询继续重试")
            state.poll_error = category
    finally:
        watch.close()
        with state.lock:
            if state.poll_watch is watch:
                state.poll_watch = None
            state.busy = False


def _start_poll(state: Runtime) -> None:
    with state.lock:
        if (
            state.stopped
            or not state.settings.alert_groups
            or state.busy
            or time.monotonic() < state.next_query_at
        ):
            return
        state.busy = True
        state.next_query_at = time.monotonic() + state.settings.cooldown_seconds
    watch = None
    try:
        # 从托管 Scheduler 回调订阅当前连接，不依赖先收到聊天，也不缓存旧 client。
        watch = watch_ica_connection()
        connection = watch.wait_changed()  # 首次读取是即时快照，不等待连接上线。
        if connection is None or not connection.ready:
            watch.close()
            with state.lock:
                state.busy = False
            return
        with state.lock:
            if state.stopped:
                watch.close()
                state.busy = False
                return
            state.poll_watch = watch
        threading.Thread(
            target=lambda: _poll_worker(state, watch, connection),
            name="codex-poll",
            daemon=True,
        ).start()
    except Exception as error:
        if watch is not None:
            watch.close()
        with state.lock:
            state.poll_watch = None
            state.busy = False
        warn(f"codex：自动轮询未能启动（{type(error).__name__}）")


def _schedule_poll(state: Runtime, delay: int = POLL_SECONDS) -> None:
    with state.lock:
        if state.stopped or not state.settings.alert_groups:
            return

    def tick() -> None:
        try:
            _start_poll(state)
        finally:
            _schedule_poll(state)

    Scheduler(tick, timedelta(seconds=delay)).start()


def _reset_support_modules() -> None:
    """宿主热重载只替换插件模块本身，codex_support 会留在 sys.modules。

    不清理的话，重载后仍会调用旧版辅助模块，导致参数含义错位等隐蔽故障。
    """
    for name in [
        name
        for name in sys.modules
        if name == "codex_support" or name.startswith("codex_support.")
    ]:
        sys.modules.pop(name, None)


def on_load() -> None:
    global _runtime
    _reset_support_modules()
    cfg = PLUGIN_MANIFEST.config_unchecked("main")
    groups = cfg.get_value("allowed_groups") or []
    if not isinstance(groups, list) or any(
        type(group) is not int or group == 0 for group in groups
    ):
        raise ValueError("codex：allowed_groups 必须为非零整数群号列表")
    concurrency = cfg.get_value("image_concurrency")
    queue_limit = cfg.get_value("image_queue_limit")
    if type(concurrency) is not int or not 1 <= concurrency <= 8:
        raise ValueError("codex：image_concurrency 必须为 1-8 的整数")
    if type(queue_limit) is not int or not 0 <= queue_limit <= 50:
        raise ValueError("codex：image_queue_limit 必须为 0-50 的整数")
    alerts = cfg.get_value("alert_groups") or []
    if not isinstance(alerts, list) or any(
        type(group) is not int or group == 0 for group in alerts
    ):
        raise ValueError("codex：alert_groups 必须为非零整数群号列表")
    home = cfg.get_value("dsh_home") or ""
    image = {
        "pg_connection": cfg.get_value("pg_connection") or "",
        "deepseek_api_key": cfg.get_value("deepseek_api_key") or "",
        "deepseek_base_url": cfg.get_value("deepseek_base_url")
        or "https://api.deepseek.com",
        "deepseek_model": cfg.get_value("deepseek_model") or "deepseek-flash",
    }
    for name, default in {
        "image_backend": "codex",
        "text_backend": "deepseek",
        "codex_image_model": "gpt-image-2.6-flare",
        "openai_text_api_key": "",
        "openai_image_api_key": "",
        "openai_usage_api_key": "",
        "openai_base_url": "https://api.openai.com/v1",
        "openai_text_model": "gpt-6-luna",
        "openai_image_model": "gpt-image-2.6-flare",
        "openai_reasoning_effort": "high",
        "openai_proxy": "",
    }.items():
        image[name] = cfg.get_value(name) or default
    if any(not isinstance(value, str) for value in image.values()):
        raise ValueError("codex：生图连接、密钥与模型配置必须为字符串")
    if image["image_backend"] not in {"codex", "openai"} or image[
        "text_backend"
    ] not in {"deepseek", "openai"}:
        raise ValueError(
            "codex：image_backend 需为 codex/openai，text_backend 需为 deepseek/openai"
        )
    if image["openai_base_url"].rstrip("/") not in {
        "https://api.openai.com",
        "https://api.openai.com/v1",
    }:
        raise ValueError("codex：OpenAI 端点仅支持官方 https://api.openai.com/v1")
    if image["openai_reasoning_effort"] not in {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }:
        raise ValueError("codex：OpenAI reasoning effort 配置无效")
    proxy = image["openai_proxy"].strip()
    if proxy:
        try:
            parsed = urlsplit(proxy)
            # SOCKS 需要额外依赖 socksio，未安装时会在运行期才失败，这里只接受 HTTP(S)。
            valid_proxy = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and not parsed.query
                and not parsed.fragment
                and (parsed.port or 0) > 0
            )
        except ValueError:
            valid_proxy = False
        if not valid_proxy:
            # 代理地址可能带凭据，报错不带原值。
            raise ValueError("codex：openai_proxy 需为带端口的 HTTP(S) 代理地址")
    image["openai_proxy"] = proxy
    image["deepseek_api_key"] = image["deepseek_api_key"].strip()
    # 两把 Key 直接来自本地配置。缺 Key 只告警：check 与额度提醒必须继续可用，
    # 真正调用 API 时由生图服务在消费确认前报出具体字段。
    image["openai_text_api_key"] = image["openai_text_api_key"].strip()
    image["openai_image_api_key"] = image["openai_image_api_key"].strip()
    image["openai_usage_api_key"] = image["openai_usage_api_key"].strip()
    if image["text_backend"] == "openai" and not image["openai_text_api_key"]:
        warn(
            "codex：text_backend 为 openai 但 openai_text_api_key 未配置，提示词处理不可用"
        )
    if image["image_backend"] == "openai" and not image["openai_image_api_key"]:
        warn(
            "codex：image_backend 为 openai 但 openai_image_api_key 未配置，生图不可用"
        )
    timeout = cfg.get_value("timeout_seconds")
    cooldown = cfg.get_value("cooldown_seconds")
    if not isinstance(home, str):
        raise ValueError("codex：dsh_home 必须为目录字符串")
    if type(timeout) is not int or not 1 <= timeout <= 60:
        raise ValueError("codex：timeout_seconds 必须为 1-60 的整数")
    if type(cooldown) is not int or not 1 <= cooldown <= 3600:
        raise ValueError("codex：cooldown_seconds 必须为 1-3600 的整数")
    on_unload()
    _runtime = Runtime(
        Settings(
            frozenset(abs(group) for group in groups),
            home,
            timeout,
            cooldown,
            **image,
            alert_groups=frozenset(abs(group) for group in alerts),
            image_concurrency=concurrency,
            image_queue_limit=queue_limit,
        )
    )
    _reset_usage_cache()
    # 首次尽快记录静默基准，之后每两分钟检查。宿主仅在插件提交后开始计时。
    _schedule_poll(_runtime, delay=1)


def on_unload() -> None:
    state = _runtime
    with state.lock:
        state.stopped = True
        watch = state.poll_watch
    if watch is not None:
        watch.close()
    state.image_slots.close()


def _run_generation(
    state: Runtime, msg, client, service, prefix: str | None, *, retry: bool = False
) -> str | None:
    """收费生成只在名额池里执行：满员排队，队列满则拒绝，未确认的草稿不受影响。"""
    slots = state.image_slots
    running, waiting = slots.snapshot()
    if running >= slots.concurrency:
        _reply(
            state,
            msg,
            client,
            f"已有 {running} 个生图任务在跑，本次排队等待（前面 {waiting} 个）。",
        )
    if not slots.acquire():
        with state.lock:
            stopping = state.stopped
        if stopping:
            return ""
        return f"生成队列已满（最多 {slots.max_waiting} 个排队），" + (
            "请稍后再发送重试命令。"
            if retry
            else "请稍后再确认；草稿在有效期内仍可确认。"
        )
    try:
        return service.confirm(msg, client, prefix, retry=retry)
    finally:
        slots.release()


def _image_worker(
    state: Runtime,
    action: str,
    msg: IcaNewMessage,
    client: IcaClient,
    value: str | None,
    options: dict[str, str],
) -> None:
    """后台执行分钟级操作；结束前再查停止标志，卸载后的旧代不回包。"""
    info(f"codex：生图任务开始 action={action}")
    # 宿主用 from_code 直接加载插件文件，没有 plugins 包；此时插件目录才是导入根。
    import sys
    from pathlib import Path

    plugin_root = str(Path(__file__).resolve().parent)
    if plugin_root not in sys.path:
        sys.path.append(plugin_root)
    try:
        from codex_support.archive import ArchiveError
        from codex_support.image_service import ImageError, ImageService, ImageSettings
    except ModuleNotFoundError:  # 从仓库根运行的测试使用包路径
        from plugins.codex_support.archive import ArchiveError
        from plugins.codex_support.image_service import (
            ImageError,
            ImageService,
            ImageSettings,
        )

    settings = ImageSettings(
        pg_connection=state.settings.pg_connection,
        deepseek_api_key=state.settings.deepseek_api_key,
        deepseek_base_url=state.settings.deepseek_base_url,
        deepseek_model=state.settings.deepseek_model,
        image_backend=state.settings.image_backend,
        text_backend=state.settings.text_backend,
        codex_image_model=state.settings.codex_image_model,
        openai_text_api_key=state.settings.openai_text_api_key,
        openai_image_api_key=state.settings.openai_image_api_key,
        openai_base_url=state.settings.openai_base_url,
        openai_text_model=state.settings.openai_text_model,
        openai_image_model=state.settings.openai_image_model,
        openai_reasoning_effort=state.settings.openai_reasoning_effort,
        openai_proxy=state.settings.openai_proxy,
    )
    # 消息对象不携带机器人账号；归属绑定必须使用当前连接的 self_id。
    try:
        status = client.status
        account = status.self_id
    except Exception:
        status = None
        account = None

    def emit(level: str, message: str) -> None:
        """把生图阶段日志接到宿主日志；未知级别按 info 输出。"""
        (warn if level == "warn" else info)(message)

    try:
        admins = frozenset(status.admins or ())
    except (AttributeError, RuntimeError, ValueError):
        admins = frozenset()
    service = ImageService(
        settings,
        lambda: read_login(state.settings.dsh_home),
        account,
        log=emit,
        admins=admins,
    )
    try:
        if action in ("create", "direct"):
            text = service.create(
                msg, client, value, direct=action == "direct", params=options
            )
        elif action == "attach":
            text = service.attach(msg, client, value)
        elif action == "set":
            text = service.set_params(msg, value, options)
        elif action in ("confirm", "retry"):
            text = _run_generation(
                state, msg, client, service, value, retry=action == "retry"
            )
        elif action == "cancel":
            text = service.cancel(msg, value)
        else:
            text = service.status(msg, value)
    except (ImageError, ArchiveError) as error:
        text = str(error)
    except Exception as error:  # 兜底只记录类型，避免上游细节进入群聊。
        # 本地控制台保留完整栈便于定位；群消息仍只给固定文案。
        import traceback

        traceback.print_exc()
        client.warn(f"codex：生图流程异常（{type(error).__name__}: {error}）")
        text = "生图流程失败。发送 /codex image status 可以查看最近任务与状态。"
    with state.lock:
        stopped = state.stopped
    # confirm 已在开始时回执并自行回图，text 为 None 时不再追加任何文本。
    if text and not stopped:
        _reply(state, msg, client, text)


def _handle_image(
    state: Runtime, command: list[str], msg: IcaNewMessage, client: IcaClient
) -> None:
    """命令分派只做参数检查与线程启动；真正网络调用都放到 worker 线程。"""
    parts = command[2:]
    if not parts:
        _reply(state, msg, client, IMAGE_HELP)
        return
    if parts == ["help"]:
        _reply(state, msg, client, IMAGE_DETAILED_HELP)
        return
    action = parts[0]
    options: dict[str, str] = {}
    value: str | None = None
    if action == "retry":
        if len(parts) != 2 or len(parts[1]) != 8:
            _reply(
                state,
                msg,
                client,
                "用法：/codex retry <8 位任务编号>；会再次消耗生成额度。",
            )
            return
        value = parts[1]
    elif action in ("confirm", "cancel", "status", "attach"):
        if len(parts) == 1:
            value = None
        elif len(parts) == 2 and len(parts[1]) == 8:
            value = parts[1]
        else:
            _reply(
                state,
                msg,
                client,
                "用法：/codex image "
                + action
                + " [8 位任务编号]；只剩一个待确认任务时可省略编号。",
            )
            return
    elif action == "set":
        tokens = parts[1:]
        if (
            tokens
            and len(tokens[0]) == 8
            and all(char in "0123456789abcdef" for char in tokens[0])
        ):
            value = tokens[0]
            tokens = tokens[1:]
        options, rest, error = _split_image_options(tokens)
        if error:
            _reply(state, msg, client, error)
            return
        if rest or not options:
            _reply(
                state,
                msg,
                client,
                "参数调整只接受 --size/--quality/--background/--model/--max，且至少给出一个。",
            )
            return
    else:
        if action in ("create", "generate", "direct"):
            action = "direct" if action == "direct" else "create"
            tokens = parts[1:]
        else:
            # 描述本身可以以 confirm 等词开头，因此只有精确关键字才按子命令解析。
            action = "create"
            tokens = parts
        options, value, error = _split_image_options(tokens)
        if error:
            _reply(state, msg, client, error)
            return
        if not value:
            _reply(state, msg, client, "用法：/codex image <图片描述>")
            return
    with state.lock:
        if state.stopped:
            return
    threading.Thread(
        target=_image_worker,
        args=(state, action, msg, client, value, options),
        name="codex-image",
        daemon=True,
    ).start()


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    """只有精确的 check 子命令进入额度查询；帮助始终在本地返回。"""
    state = _runtime
    command = (msg.content or "").split()
    if msg.is_from_self or msg.room_id >= 0:
        return
    if -msg.room_id not in state.settings.allowed_groups:
        return
    # 常用生图子命令支持越级：/codex confirm 等价于 /codex image confirm。
    if len(command) > 1 and command[0] == COMMAND and command[1] in IMAGE_SHORTCUTS:
        command = [COMMAND, "image", *command[1:]]
    # 显式列举已实现的入口，拼错或尚未接入的 image 子命令不能误占查询槽位。
    if command not in ([COMMAND], [COMMAND, "help"], [COMMAND, "check"]) and command[
        :2
    ] != [COMMAND, "image"]:
        return
    if command[:2] == [COMMAND, "image"]:
        _handle_image(state, command, msg, client)
        return
    if command in ([COMMAND], [COMMAND, "help"]):
        _reply(state, msg, client, HELP_TEXT)
        return
    # 订阅额度查询只依赖 DSH 登录态，与生图/文本后端选择无关。
    with state.lock:
        if state.stopped:
            return
        remaining = state.next_query_at - time.monotonic()
        if state.busy:
            notice = "Codex 订阅正在查询，请稍候。"
        elif remaining > 0:
            notice = f"请在 {math.ceil(remaining)} 秒后再查 Codex 订阅。"
        else:
            notice = ""
            state.busy = True
            state.next_query_at = time.monotonic() + state.settings.cooldown_seconds
    if notice:
        _reply(state, msg, client, notice)
        return

    def run_query() -> None:
        try:
            try:
                result = query_subscription(state.settings)
                # 用量异常只影响追加段，绝不能吞掉已经拿到的订阅额度。
                try:
                    usage = openai_usage_section(state.settings)
                except Exception as error:
                    client.warn(f"codex：用量查询异常（{type(error).__name__}）")
                    usage = "OpenAI API 用量：本轮查询失败"
                if usage:
                    result = result + "\n\n" + usage
            except QueryError as error:
                with state.lock:
                    state.next_query_at = max(
                        state.next_query_at, time.monotonic() + error.retry_after
                    )
                result = "Codex 查询失败：" + str(error)
            except Exception as error:
                client.warn(f"codex：查询异常（{type(error).__name__}）")
                result = "Codex 查询失败，请检查插件依赖与配置后重试。"
            _reply(state, msg, client, result)
        finally:
            with state.lock:
                state.busy = False

    try:
        _reply(state, msg, client, "正在查询 DSH 当前 Codex 订阅…")
        threading.Thread(target=run_query, name="codex-query", daemon=True).start()
    except Exception:
        with state.lock:
            state.busy = False
            state.next_query_at = 0
        client.warn("codex：启动查询线程失败")
        _reply(state, msg, client, "Codex 查询未能启动，请稍后重试。")
