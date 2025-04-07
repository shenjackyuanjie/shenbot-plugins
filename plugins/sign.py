import io
import time
import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient
else:
    IcaNewMessage = TypeVar("NewMessage")
    IcaClient = TypeVar("IcaClient")


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    if msg.is_from_self or msg.sender_id in client.status.admins:
            # 上号发的消息 / 管理员发的消息
            start_time = time.time()
            if msg.content == "/bot-sign-all":

                all_room = [room for room in client.status.rooms if room.is_group()]
                signed = []
                reply = msg.reply_with(f"将要签到 {len(all_room)} 个 群\n预计需要 {len(all_room) * 3 / 2} 秒\n(random摇出来的，只能说大概这么久)")
                client.send_message(reply)
                for room in all_room:
                    client.send_room_sign_in(room.room_id)
                    signed.append(str(room.room_id))
                    time.sleep(random.random() * 3)
                # reply = msg.reply_with(f"已签到房间: {', '.join(signed)}")
                # client.send_message(reply)
                cost_time = time.time() - start_time
                reply = msg.reply_with(f"✅已签到 {len(signed)} 个 群\n耗时 {cost_time:.2f} 秒")
                client.send_message(reply)
            elif msg.content == "/bot-sign-warm":
                all_room = client.status.rooms
                hot_room = []
                signed = []
                for room in all_room:
                    # fmted_time = datetime.fromtimestamp(room.utime // 1000).strftime("%Y-%m-%d %H:%M:%S")
                    # print(f"{room.room_id=} {room.utime=} {room.is_group()=} {fmted_time=}")
                    last_time = room.utime // 1000
                    # 7天内
                    if start_time - last_time < 86400 * 7 and room.is_group():
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
                cost_time = time.time() - start_time
                reply = msg.reply_with(f"✅已签到 {len(signed)} 个 7天内有活动的群，耗时{cost_time:.2f}秒")
                client.send_message(reply)

            elif msg.content == "/bot-sign-hot":
                all_room = client.status.rooms
                hot_room = []
                signed = []
                for room in all_room:
                    last_time = room.utime // 1000
                    if start_time - last_time < 43200 and room.is_group():
                        hot_room.append(room)
                # 排序
                hot_room.sort(key=lambda x: x.utime, reverse=True)
                reply = msg.reply_with(f"将要签到 {len(hot_room)} 个 半天内有活动的群\n预计需要 {len(hot_room) * 3 / 2}秒\n(random摇出来的，只能说大概这么久)")
                client.send_message(reply)

                for room in hot_room:
                    client.send_room_sign_in(room.room_id)
                    signed.append(str(room.room_id))
                    time.sleep(random.random() * 3)
                cost_time = time.time() - start_time
                reply = msg.reply_with(f"✅已签到 {len(signed)} 个 半天内有活动的群，耗时{cost_time:.2f}秒")
                client.send_message(reply)
