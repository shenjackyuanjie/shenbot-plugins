from __future__ import annotations

import time
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient
else:
    IcaNewMessage = TypeVar("NewMessage")
    IcaClient = TypeVar("IcaClient")

from shenbot_api import Scheduler, PluginManifest

_version_ = "0.3.1"

"""记录哪些群已经签过了"""
SIGN_REC: dict[int, datetime] = {}
"""记录明天计划啥时候签到"""
SIGN_PLAN: dict[int, datetime] = {}

SIGN_TIME = datetime.now()

HELP_MSG = f"""bot sign v{_version_} - 似乎有点用的自动签到
/bot-sign all - 签到所有群
/bot-sign warm - 签到7天内有活动的群
/bot-sign hot - 签到半天内有活动的群
/bot-sign want <24小时制(10:00)> - 在下一次指定时间签到当前群
/bot-sign help - 查看帮助信息
/bot-sign - 查看帮助信息
"""

PLUGIN_MANIFEST = PluginManifest(
    plugin_id="signer",
    name="签到器",
    version=_version_,
    description="自动签到",
    authors=[
        "shenjack"
    ]
)


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    now = datetime.now()
    # 看看是不是同一天
    global SIGN_TIME, SIGN_REC, SIGN_PLAN
    if now.date() != SIGN_TIME.date():
        SIGN_TIME = now
        SIGN_REC = SIGN_PLAN.copy()
        SIGN_PLAN = {}
    msg_room_id = msg.room_id

    if msg.is_from_self or msg.sender_id in client.status.admins:
        # 上号发的消息 / 管理员发的消息
        start_time = time.time()

        ica_rooms = [room for room in client.status.rooms if room.is_group()]
        if msg.content == "/bot-sign" or msg.content == "/bot-sign help":
            client.send_message(msg.reply_with(HELP_MSG))
        elif msg.content == "/bot-sign all":
            sign_plan = [random.random() * 3 for _ in range(len(ica_rooms))]
            use_time = 0
            sign_room = []
            for t, room in zip(sign_plan, ica_rooms):
                use_time += t
                plan_time = now + timedelta(seconds=use_time)
                if room.room_id in SIGN_REC and SIGN_REC[room.room_id] < plan_time:
                    # 他先签到的
                    continue
                sign_room.append(room)
            sign_plan = sign_plan[:len(sign_room)]
            signed = []

            reply = msg.reply_with(f"将要签到{len(sign_room)}个群\n需要 {use_time} 秒")
            client.send_message(reply)

            for t, room in zip(sign_plan, sign_room):
                client.send_room_sign_in(room.room_id)
                signed.append(str(room.room_id))
                time.sleep(t)
                SIGN_REC[room.room_id] = datetime.now()

            cost_time = time.time() - start_time
            reply = msg.reply_with(
                f"✅已签到 {len(signed)} 个 群\n耗时 {cost_time:.2f} 秒"
            )
            client.send_message(reply)
        elif msg.content == "/bot-sign warm":
            sign_plan = [random.random() * 3 for _ in range(len(ica_rooms))]
            use_time = 0
            hot_room = []
            for t, room in zip(sign_plan, ica_rooms):
                use_time += t
                plan_time = now + timedelta(seconds=use_time)
                if room.room_id in SIGN_REC and SIGN_REC[room.room_id] < plan_time:
                    # 他先签到的
                    continue
                if room.utime // 1000 < SIGN_TIME.timestamp() - 7 * 24 * 3600:
                    # 7天前的群
                    continue
                hot_room.append(room)
            sign_plan = sign_plan[:len(hot_room)]
            signed = []
            # 排序
            hot_room.sort(key=lambda x: x.utime, reverse=True)
            if len(hot_room) == 0:
                return

            reply = msg.reply_with(
                f"将要签到 {len(hot_room)} 个 7天内有活动的群\n需要 {use_time} 秒"
            )
            client.send_message(reply)

            for t, room in zip(sign_plan, hot_room):
                client.send_room_sign_in(room.room_id)
                signed.append(str(room.room_id))
                time.sleep(t)
                SIGN_REC[room.room_id] = datetime.now()
            cost_time = time.time() - start_time
            reply = msg.reply_with(
                f"✅已签到 {len(signed)} 个 7天内有活动的群\n耗时{cost_time:.2f}秒"
            )
            client.send_message(reply)

        elif msg.content == "/bot-sign hot":
            sign_plan = [random.random() * 3 for _ in range(len(ica_rooms))]
            use_time = 0
            hot_room = []
            for t, room in zip(sign_plan, ica_rooms):
                use_time += t
                plan_time = now + timedelta(seconds=use_time)
                if room.room_id in SIGN_REC and SIGN_REC[room.room_id] < plan_time:
                    # 他先签到的
                    continue
                if room.utime // 1000 < SIGN_TIME.timestamp() - 12 * 3600:
                    # 12小时前的群
                    continue
                hot_room.append(room)
            sign_plan = sign_plan[:len(hot_room)]
            signed = []
            # 排序
            hot_room.sort(key=lambda x: x.utime, reverse=True)
            if len(hot_room) == 0:
                return
            reply = msg.reply_with(
                f"将要签到 {len(hot_room)} 个 半天内有活动的群\n需要{use_time}秒"
            )
            client.send_message(reply)

            for (t, room) in zip(sign_plan, hot_room):
                client.send_room_sign_in(room.room_id)
                signed.append(str(room.room_id))
                time.sleep(t)
                SIGN_REC[room.room_id] = datetime.now()
            cost_time = time.time() - start_time
            reply = msg.reply_with(
                f"✅已签到 {len(signed)} 个 半天内有活动的群, 耗时{cost_time:.2f}秒"
            )
            client.send_message(reply)

        elif msg.content.startswith("/bot-sign want"):
            msg_room_id = msg.room_id
            try:
                # 提取用户输入的时间（例如："12:34"）
                time_str = msg.content.split()[2]
                user_time = datetime.strptime(time_str, "%H:%M").time()
                now = datetime.now()

                # 创建目标时间（使用当前日期）
                want_time = now.replace(hour=user_time.hour, minute=user_time.minute, second=0, microsecond=0)

                # 如果当前时间已过目标时间，则计划到明天
                if now >= want_time:
                    want_time += timedelta(days=1)
                    if msg_room_id in SIGN_PLAN:
                        if SIGN_PLAN[msg_room_id] >= want_time:
                            SIGN_PLAN[msg_room_id] = want_time
                            fmt_time = want_time.strftime("%m-%d %H:%M")
                            client.send_message(
                                msg.reply_with(f"将提前到 {fmt_time} 开始签到")
                            )
                        else:
                            fmt_time = SIGN_PLAN[msg_room_id].strftime("%H:%M")
                            client.send_message(
                                msg.reply_with(
                                    f"不是说在 {fmt_time} 签到吗, 怎么延后了"
                                )
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

                def callback():
                    client.send_room_sign_in(msg_room_id)
                    SIGN_REC[msg_room_id] = datetime.now()
                    client.send_message(msg.reply_with(f"已签到 {msg_room_id}"))

                caller = Scheduler(callback, time_d)
                caller.start()

            except (IndexError, ValueError) as e:
                # 处理输入格式错误
                print(f"Invalid input: {e}")
