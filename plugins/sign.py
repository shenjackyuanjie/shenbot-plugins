from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from shenbot_api import PluginManifest, Scheduler

if TYPE_CHECKING:
    from ica_typing import IcaClient, IcaNewMessage

VERSION = "0.3.4"
CMD_PREFIX = "/bot-sign"
HELP_CMD = f"{CMD_PREFIX} help"
ALL_CMD = f"{CMD_PREFIX} all"
WARM_CMD = f"{CMD_PREFIX} warm"
HOT_CMD = f"{CMD_PREFIX} hot"
WANT_CMD = f"{CMD_PREFIX} want"

PLUGIN_MANIFEST = PluginManifest(
    plugin_id="signer",
    name="签到器",
    version=VERSION,
    description="自动签到",
    authors=["shenjack"],
)

HELP_MSG = f"""bot sign v{VERSION} - 似乎有点用的自动签到
{ALL_CMD} - 签到所有群
{WARM_CMD} - 签到7天内有活动的群
{HOT_CMD} - 签到半天内有活动的群
{WANT_CMD} <24小时制(10:00)> - 在下一次指定时间签到当前群
{HELP_CMD} - 查看帮助信息
{CMD_PREFIX} - 查看帮助信息
"""

"""记录哪些群已经签过了"""
SIGN_REC: dict[int, datetime] = {}
"""记录当前有效的签到预约（包括今天和明天）"""
SIGN_PLAN: dict[int, datetime] = {}
_sign_tokens: dict[int, object] = {}
_sign_lock = threading.RLock()

SIGN_TIME = datetime.now()


def reset_daily_state(now: datetime) -> None:
    global SIGN_TIME
    with _sign_lock:
        if now.date() == SIGN_TIME.date():
            return
        SIGN_TIME = now
        for room_id, signed_at in list(SIGN_REC.items()):
            if signed_at.date() != now.date():
                del SIGN_REC[room_id]
        # 预约不等于已签到，跨日后仍保留其有效回调。
        for room_id, planned_at in list(SIGN_PLAN.items()):
            if planned_at.date() < now.date():
                del SIGN_PLAN[room_id]
                _sign_tokens.pop(room_id, None)


def can_handle_message(msg: IcaNewMessage, client: IcaClient) -> bool:
    return msg.is_from_self or msg.sender_id in client.status.admins


def group_rooms(client: IcaClient) -> list[Any]:
    return [room for room in client.status.rooms if room.is_group()]


def build_sign_plan(
    now: datetime, rooms: list[Any]
) -> tuple[list[float], list[Any], float]:
    delays = [random.random() * 3 for _ in range(len(rooms))]
    use_time = 0.0
    sign_room = []
    for delay, room in zip(delays, rooms):
        use_time += delay
        plan_time = now + timedelta(seconds=use_time)
        if room.room_id in SIGN_REC and SIGN_REC[room.room_id] < plan_time:
            # 他先签到的
            continue
        sign_room.append(room)
    return delays[: len(sign_room)], sign_room, use_time


def send_room_signs(
    client: IcaClient, delays: list[float], rooms: list[Any]
) -> list[str]:
    signed = []
    for delay, room in zip(delays, rooms):
        with _sign_lock:
            now = datetime.now()
            if SIGN_REC.get(room.room_id, datetime.min).date() == now.date():
                continue
            if not client.send_room_sign_in(room.room_id):
                continue
            SIGN_REC[room.room_id] = now
            signed.append(str(room.room_id))
        time.sleep(delay)
    return signed


def handle_sign_all(msg: IcaNewMessage, client: IcaClient, now: datetime) -> None:
    start_time = time.time()
    delays, sign_room, use_time = build_sign_plan(now, group_rooms(client))

    reply = msg.reply_with(f"将要签到{len(sign_room)}个群\n需要 {use_time} 秒")
    client.send_message(reply)

    signed = send_room_signs(client, delays, sign_room)
    cost_time = time.time() - start_time
    reply = msg.reply_with(f"✅已签到 {len(signed)} 个 群\n耗时 {cost_time:.2f} 秒")
    client.send_message(reply)


def handle_sign_recent(
    msg: IcaNewMessage,
    client: IcaClient,
    now: datetime,
    active_seconds: int,
    label: str,
) -> None:
    start_time = time.time()
    rooms = group_rooms(client)
    delays = [random.random() * 3 for _ in range(len(rooms))]
    use_time = 0.0
    active_rooms = []
    for delay, room in zip(delays, rooms):
        use_time += delay
        plan_time = now + timedelta(seconds=use_time)
        if room.room_id in SIGN_REC and SIGN_REC[room.room_id] < plan_time:
            # 他先签到的
            continue
        if room.utime // 1000 < now.timestamp() - active_seconds:
            continue
        active_rooms.append(room)
    delays = delays[: len(active_rooms)]
    active_rooms.sort(key=lambda x: x.utime, reverse=True)
    if len(active_rooms) == 0:
        return

    reply = msg.reply_with(
        f"将要签到 {len(active_rooms)} 个 {label}的群\n需要 {use_time} 秒"
    )
    client.send_message(reply)

    signed = send_room_signs(client, delays, active_rooms)
    cost_time = time.time() - start_time
    reply = msg.reply_with(
        f"✅已签到 {len(signed)} 个 {label}的群\n耗时{cost_time:.2f}秒"
    )
    client.send_message(reply)


def handle_sign_want(msg: IcaNewMessage, client: IcaClient) -> None:
    room_id = msg.room_id
    try:
        parts = msg.content.split()
        if len(parts) != 3 or parts[:2] != [CMD_PREFIX, "want"]:
            raise ValueError("预约格式错误")
        user_time = datetime.strptime(parts[2], "%H:%M").time()
    except ValueError:
        client.send_message(msg.reply_with(f"用法：{WANT_CMD} <HH:MM>，例如 13:00"))
        return

    now = datetime.now()
    want_time = now.replace(
        hour=user_time.hour, minute=user_time.minute, second=0, microsecond=0
    )
    if now >= want_time:
        want_time += timedelta(days=1)
    fmt_time = want_time.strftime("%m-%d %H:%M")
    token = object()
    with _sign_lock:
        if SIGN_REC.get(room_id, datetime.min).date() == want_time.date():
            client.send_message(msg.reply_with("拜托, 签过了欸"))
            return
        previous_time = SIGN_PLAN.get(room_id)
        previous_token = _sign_tokens.get(room_id)
        if previous_time is not None:
            if previous_time == want_time:
                client.send_message(
                    msg.reply_with(f"已有 {fmt_time} 的签到预约，无需重复预约")
                )
                return
            if previous_time < want_time:
                previous_text = previous_time.strftime("%m-%d %H:%M")
                client.send_message(
                    msg.reply_with(f"已有 {previous_text} 的签到预约，保留原计划")
                )
                return
        SIGN_PLAN[room_id] = want_time
        _sign_tokens[room_id] = token

    def callback() -> None:
        with _sign_lock:
            if _sign_tokens.get(room_id) is not token:
                return
            del _sign_tokens[room_id]
            SIGN_PLAN.pop(room_id, None)
            callback_now = datetime.now()
            if callback_now.date() != want_time.date():
                return
            if SIGN_REC.get(room_id, datetime.min).date() == callback_now.date():
                return
            if not client.send_room_sign_in(room_id):
                client.send_message(msg.reply_with("签到请求未受理，可重新预约"))
                return
            SIGN_REC[room_id] = callback_now
        client.send_message(msg.reply_with("已提交当前群的签到请求"))

    try:
        Scheduler(callback, want_time - now).start()
    except Exception:
        with _sign_lock:
            if _sign_tokens.get(room_id) is token:
                if previous_time is not None and previous_token is not None:
                    SIGN_PLAN[room_id] = previous_time
                    _sign_tokens[room_id] = previous_token
                else:
                    SIGN_PLAN.pop(room_id, None)
                    _sign_tokens.pop(room_id, None)
        client.send_message(msg.reply_with("创建签到预约失败，请稍后重试"))
        return
    action = "将提前到" if previous_time is not None else "将在"
    client.send_message(msg.reply_with(f"{action} {fmt_time} 开始签到"))


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    now = datetime.now()
    reset_daily_state(now)

    if not can_handle_message(msg, client):
        return

    content = msg.content.strip()
    if not content.startswith(CMD_PREFIX):
        return

    if content in (CMD_PREFIX, HELP_CMD):
        client.send_message(msg.reply_with(HELP_MSG))
    elif content == ALL_CMD:
        handle_sign_all(msg, client, now)
    elif content == WARM_CMD:
        handle_sign_recent(msg, client, now, 7 * 24 * 3600, "7天内有活动")
    elif content == HOT_CMD:
        handle_sign_recent(msg, client, now, 12 * 3600, "半天内有活动")
    elif content.startswith(WANT_CMD):
        handle_sign_want(msg, client)
