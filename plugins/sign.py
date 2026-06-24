from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from shenbot_api import PluginManifest, Scheduler

if TYPE_CHECKING:
    from ica_typing import IcaClient, IcaNewMessage

VERSION = "0.3.2"
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
"""记录明天计划啥时候签到"""
SIGN_PLAN: dict[int, datetime] = {}

SIGN_TIME = datetime.now()


def reset_daily_state(now: datetime) -> None:
    global SIGN_TIME, SIGN_REC, SIGN_PLAN
    if now.date() == SIGN_TIME.date():
        return
    SIGN_TIME = now
    SIGN_REC = SIGN_PLAN.copy()
    SIGN_PLAN = {}


def can_handle_message(msg: IcaNewMessage, client: IcaClient) -> bool:
    return msg.is_from_self or msg.sender_id in client.status.admins


def group_rooms(client: IcaClient) -> list[Any]:
    return [room for room in client.status.rooms if room.is_group()]


def build_sign_plan(now: datetime, rooms: list[Any]) -> tuple[list[float], list[Any], float]:
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


def send_room_signs(client: IcaClient, delays: list[float], rooms: list[Any]) -> list[str]:
    signed = []
    for delay, room in zip(delays, rooms):
        client.send_room_sign_in(room.room_id)
        signed.append(str(room.room_id))
        time.sleep(delay)
        SIGN_REC[room.room_id] = datetime.now()
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
        if room.utime // 1000 < SIGN_TIME.timestamp() - active_seconds:
            continue
        active_rooms.append(room)
    delays = delays[: len(active_rooms)]
    active_rooms.sort(key=lambda x: x.utime, reverse=True)
    if len(active_rooms) == 0:
        return

    reply = msg.reply_with(f"将要签到 {len(active_rooms)} 个 {label}的群\n需要 {use_time} 秒")
    client.send_message(reply)

    signed = send_room_signs(client, delays, active_rooms)
    cost_time = time.time() - start_time
    reply = msg.reply_with(f"✅已签到 {len(signed)} 个 {label}的群\n耗时{cost_time:.2f}秒")
    client.send_message(reply)


def handle_sign_want(msg: IcaNewMessage, client: IcaClient) -> None:
    msg_room_id = msg.room_id
    try:
        # 提取用户输入的时间（例如："12:34"）
        time_str = msg.content.split()[2]
        user_time = datetime.strptime(time_str, "%H:%M").time()
        now = datetime.now()

        # 创建目标时间（使用当前日期）
        want_time = now.replace(
            hour=user_time.hour,
            minute=user_time.minute,
            second=0,
            microsecond=0,
        )

        # 如果当前时间已过目标时间，则计划到明天
        if now >= want_time:
            want_time += timedelta(days=1)
            if msg_room_id in SIGN_PLAN:
                if SIGN_PLAN[msg_room_id] >= want_time:
                    SIGN_PLAN[msg_room_id] = want_time
                    fmt_time = want_time.strftime("%m-%d %H:%M")
                    client.send_message(msg.reply_with(f"将提前到 {fmt_time} 开始签到"))
                else:
                    fmt_time = SIGN_PLAN[msg_room_id].strftime("%H:%M")
                    client.send_message(
                        msg.reply_with(f"不是说在 {fmt_time} 签到吗, 怎么延后了")
                    )
                    return
            else:
                SIGN_PLAN[msg_room_id] = want_time
                fmt_time = want_time.strftime("%m-%d %H:%M")
                client.send_message(msg.reply_with(f"将在 {fmt_time} 开始签到"))
        else:
            if msg_room_id in SIGN_REC:
                client.send_message(msg.reply_with("拜托, 签过了欸"))
                return

            fmt_time = want_time.strftime("%m-%d %H:%M")
            client.send_message(msg.reply_with(f"将在 {fmt_time} 开始签到"))

        time_d = want_time - now

        def callback() -> None:
            client.send_room_sign_in(msg_room_id)
            SIGN_REC[msg_room_id] = datetime.now()
            client.send_message(msg.reply_with(f"已签到 {msg_room_id}"))

        caller = Scheduler(callback, time_d)
        caller.start()

    except (IndexError, ValueError) as e:
        # 处理输入格式错误
        print(f"Invalid input: {e}")


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
