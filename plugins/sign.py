from __future__ import annotations

import io
import time
import random
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient
else:
    IcaNewMessage = TypeVar("NewMessage")
    IcaClient = TypeVar("IcaClient")

from shenbot_api import Scheduler

_version_ = "0.2.0"

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

def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    now = datetime.now()
    # 看看是不是同一天
    global SIGN_TIME, SIGN_REC, SIGN_PLAN
    if now.date() != SIGN_TIME.date():
        SIGN_TIME = now
        SIGN_REC = SIGN_PLAN.copy()
        SIGN_PLAN = {}

    if msg.is_from_self or msg.sender_id in client.status.admins:
        # 上号发的消息 / 管理员发的消息
        start_time = time.time()

        if msg.content == "/bot-sign" or msg.content == "/bot-sign help":
            client.send_message(msg.reply_with(HELP_MSG))
        elif msg.content == "/bot-sign all":

            all_room = [room for room in client.status.rooms if room.is_group() and not room.room_id in SIGN_REC]
            signed = []
            reply = msg.reply_with(f"将要签到 {len(all_room)} 个 群\n预计需要 {len(all_room) * 3 / 2} 秒\n(random摇出来的，只能说大概这么久)")
            client.send_message(reply)
            for room in all_room:
                client.send_room_sign_in(room.room_id)
                signed.append(str(room.room_id))
                time.sleep(random.random() * 3)
                SIGN_REC[room.room_id] = datetime.now()
            # reply = msg.reply_with(f"已签到房间: {', '.join(signed)}")
            # client.send_message(reply)
            cost_time = time.time() - start_time
            reply = msg.reply_with(f"✅已签到 {len(signed)} 个 群\n耗时 {cost_time:.2f} 秒")
            client.send_message(reply)
        elif msg.content == "/bot-sign warm":
            all_room = client.status.rooms
            hot_room = []
            signed = []
            for room in all_room:
                if room.room_id in SIGN_REC:
                    continue
                if not room.is_group():
                    continue
                # fmted_time = datetime.fromtimestamp(room.utime // 1000).strftime("%Y-%m-%d %H:%M:%S")
                # print(f"{room.room_id=} {room.utime=} {room.is_group()=} {fmted_time=}")
                last_time = room.utime // 1000
                # 7天内
                if start_time - last_time < 86400 * 7:
                    hot_room.append(room)
            # 排序
            hot_room.sort(key=lambda x: x.utime, reverse=True)
            # reply = io.StringIO()
            # for r in hot_room:
            #     fmt_time = datetime.fromtimestamp(r.utime // 1000).strftime("%Y-%m-%d %H:%M:%S")
            #     reply.write(f"{abs(r.room_id)} 上次活跃: {fmt_time}\n")
            # client.send_message(msg.reply_with(reply.getvalue().strip()))
            reply = msg.reply_with(f"将要签到 {len(hot_room)} 个 7天内有活动的群\n预计需要 {len(hot_room) * 3 / 2}秒\n(random摇出来的，只能说大概这么久)")
            client.send_message(reply)

            for room in hot_room:
                client.send_room_sign_in(room.room_id)
                signed.append(str(room.room_id))
                time.sleep(random.random() * 3)
                SIGN_REC[room.room_id] = datetime.now()
            cost_time = time.time() - start_time
            reply = msg.reply_with(f"✅已签到 {len(signed)} 个 7天内有活动的群，耗时{cost_time:.2f}秒")
            client.send_message(reply)

        elif msg.content == "/bot-sign hot":
            all_room = client.status.rooms
            hot_room = []
            signed = []
            for room in all_room:
                if room.room_id in SIGN_REC:
                    continue
                if not room.is_group():
                    continue
                last_time = room.utime // 1000
                if start_time - last_time < 43200:
                    hot_room.append(room)
            # 排序
            hot_room.sort(key=lambda x: x.utime, reverse=True)
            reply = msg.reply_with(f"将要签到 {len(hot_room)} 个 半天内有活动的群\n预计需要 {len(hot_room) * 3 / 2}秒\n(random摇出来的，只能说大概这么久)")
            client.send_message(reply)

            for room in hot_room:
                client.send_room_sign_in(room.room_id)
                signed.append(str(room.room_id))
                time.sleep(random.random() * 3)
                SIGN_REC[room.room_id] = datetime.now()
            cost_time = time.time() - start_time
            reply = msg.reply_with(f"✅已签到 {len(signed)} 个 半天内有活动的群，耗时{cost_time:.2f}秒")
            client.send_message(reply)

        elif msg.content.startswith("/bot-sign want"):
            room_id = msg.room_id
            try:
                # 提取用户输入的时间（例如："12:34"）
                time_str = msg.content.split()[2]
                user_time = datetime.strptime(time_str, "%H:%M").time()
                now = datetime.now()

                # 创建目标时间（使用当前日期）
                want_time = datetime.combine(now.date(), user_time)

                # 如果当前时间已过目标时间，则计划到明天
                if now >= want_time:
                    want_time += timedelta(days=1)
                    if room_id in SIGN_PLAN:
                        if SIGN_PLAN[room_id] >= want_time:
                            SIGN_PLAN[room_id] = want_time
                            fmt_time = want_time.strftime("%m-%d %H:%M")
                            client.send_message(msg.reply_with(f"将提前到 {fmt_time} 开始签到"))
                        else:
                            fmt_time = want_time.strftime("%H:%M")
                            client.send_message(msg.reply_with(f"不是说在 {fmt_time} 签到吗, 怎么延后了"))
                            return
                else:
                    if room_id in SIGN_REC:
                        client.send_message(msg.reply_with("拜托, 签过了欸"))
                        return

                    fmt_time = want_time.strftime("%m-%d %H:%M")
                    client.send_message(msg.reply_with(f"将在 {fmt_time} 开始签到"))


                time_d = want_time - now

                def callback():
                    client.send_room_sign_in(room_id)
                    SIGN_REC[room_id] = datetime.now()
                    client.send_message(msg.reply_with(f"已签到 {room_id}"))

                caller = Scheduler(callback, time_d)
                caller.start()

            except (IndexError, ValueError) as e:
                # 处理输入格式错误
                print(f"Invalid input: {e}")
