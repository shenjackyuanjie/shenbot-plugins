"""只读复用 DSH 当前 Codex 登录态；不登录、不刷新、不保存凭据。"""

from __future__ import annotations

import json
import math
import os
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

import yaml
from shenbot_api import ConfigStorage, PluginManifest, Scheduler, watch_ica_connection

if TYPE_CHECKING:
    from ica_typing import IcaClient, IcaNewMessage
    from shenbot_api import IcaConnectionState, IcaConnectionWatch

VERSION = "0.3.0"
POLL_SECONDS = 120
THRESHOLD_STEP = 5
COMMAND = "/codex"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
DISPLAY_ZONE = timezone(timedelta(hours=8))
LOGIN_HELP = "请启动 DSH，在「设置 → Codex 订阅」刷新额度或重新登录后再试。"
HELP_TEXT = (
    "Codex 订阅帮助\n"
    "/codex：查询 DSH 当前账号的套餐、剩余额度和重置时间。\n"
    "/codex help：显示本帮助，不联网，不占用查询冷却。\n"
    f"自动提醒：每 {POLL_SECONDS // 60} 分钟检查，剩余额度跨过 95%、90%、85%……0% 时提醒。\n"
    "reset 警告：剩余额度比上次上涨时报告；上次不为 0% 时提示“浪费了xx%额度😭”。\n"
    "仅白名单群可用；登录失效请在 DSH 中刷新。"
)

PLUGIN_MANIFEST = PluginManifest(
    plugin_id="codex",
    name="Codex 订阅查询",
    version=VERSION,
    description="指定群使用 /codex 查询订阅、/codex help 查看帮助，每两分钟检查额度下降与 reset",
    authors=["shenjack"],
    config={
        "main": ConfigStorage(
            allowed_groups=[],
            dsh_home="",
            timeout_seconds=15,
            cooldown_seconds=15,
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
        for group in sorted(state.settings.allowed_groups):
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
                    print("[codex] 额度提醒发送结果不确定，不自动重发本次档位")
                if not accepted:
                    print("[codex] 额度提醒未受理，保留该群基准等待下轮检查")
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
                print(f"[codex] 自动轮询暂时失败（{category}），后续轮询继续重试")
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
            or not state.settings.allowed_groups
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
        print(f"[codex] 自动轮询未能启动（{type(error).__name__}）")


def _schedule_poll(state: Runtime, delay: int = POLL_SECONDS) -> None:
    with state.lock:
        if state.stopped or not state.settings.allowed_groups:
            return

    def tick() -> None:
        try:
            _start_poll(state)
        finally:
            _schedule_poll(state)

    Scheduler(tick, timedelta(seconds=delay)).start()


def on_load() -> None:
    global _runtime
    cfg = PLUGIN_MANIFEST.config_unchecked("main")
    groups = cfg.get_value("allowed_groups") or []
    if not isinstance(groups, list) or any(
        type(group) is not int or group == 0 for group in groups
    ):
        raise ValueError("codex：allowed_groups 必须为非零整数群号列表")
    home = cfg.get_value("dsh_home") or ""
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
        Settings(frozenset(abs(group) for group in groups), home, timeout, cooldown)
    )
    # 首次尽快记录静默基准，之后每两分钟检查。宿主仅在插件提交后开始计时。
    _schedule_poll(_runtime, delay=1)


def on_unload() -> None:
    state = _runtime
    with state.lock:
        state.stopped = True
        watch = state.poll_watch
    if watch is not None:
        watch.close()


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    state = _runtime
    command = (msg.content or "").split()
    if (
        msg.is_from_self
        or msg.room_id >= 0
        or -msg.room_id not in state.settings.allowed_groups
        or command not in ([COMMAND], [COMMAND, "help"])
    ):
        return
    if command == [COMMAND, "help"]:
        _reply(state, msg, client, HELP_TEXT)
        return
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
