import io

# import time
import psutil
import platform

# from pathlib import Path
from datetime import datetime, timezone
from typing import TYPE_CHECKING, TypeVar
# from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient
    from ica_typing import TailchatReciveMessage, TailchatClient
else:
    IcaNewMessage = TypeVar("NewMessage")
    IcaClient = TypeVar("IcaClient")
    TailchatReciveMessage = TypeVar("TailchatReciveMessage")
    TailchatClient = TypeVar("TailchatClient")

from shenbot_api import PluginManifest

PLUGIN_MANIFEST = PluginManifest(
    plugin_id="base",
    name="基本插件",
    version="0.0.1",
    description="提供基本功能的插件",
    authors=["shenjack"],
)


# 生成一张本地信息图
def local_env_info() -> str:
    cache = io.StringIO()
    # 参考 DR 的 (crash report)
    _ = cache.write(f"系统: {platform.platform()}\n")
    # 处理器
    try:
        _ = cache.write(
            "|".join([f"{x}%" for x in psutil.cpu_percent(interval=1, percpu=True)])
        )
        _ = cache.write("\n")
    except OSError:
        _ = cache.write("CPU: 未知\n")
    # Python 版本信息
    _ = cache.write(
        f"{platform.python_implementation()}: {platform.python_version()}-{platform.python_branch()}({platform.python_compiler()})\n"
    )
    # 内存信息
    try:
        memory = psutil.virtual_memory()
        _ = cache.write(
            f"内存: {memory.free / 1024 / 1024 / 1024:.3f}GB/{memory.total / 1024 / 1024 / 1024:.3f}GB\n"
        )
    except OSError:
        _ = cache.write("内存: 未知\n")
    return cache.getvalue()


# def local_env_image() -> bytes:
#     print(Path.cwd())
#     img = Image.new("RGB", (800, 140), (255, 255, 255))
#     # 往图片上写入一些信息
#     draw = ImageDraw.Draw(img)
#     font = ImageFont.truetype("SMILEYSANS-OBLIQUE.TTF", size=25)
#     draw.text((10, 10), local_env_info(), fill=(0, 0, 0), font=font)
#     img_cache = io.BytesIO()
#     img.save(img_cache, format="PNG")
#     raw_img = img_cache.getvalue()
#     img_cache.close()
#     return raw_img


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    if not msg.is_from_self:
        if not msg.is_reply:
            if msg.content == "/bot-py":
                reply = msg.reply_with(
                    f"ica-async-rs({client.version})-sync-py {client.ica_version}"
                )
                _ = client.send_message(reply)
            elif msg.content == "/bot-sys":
                datas = local_env_info()
                reply = msg.reply_with(datas)
                # reply.set_img(local_env_image(), "image/png", False)
                _ = client.send_message(reply)
            elif msg.content == "/bot-uptime":
                uptime = client.startup_time
                up_delta = datetime.now(timezone.utc) - uptime
                reply = msg.reply_with(f"Bot 运行时间: {up_delta}")
                _ = client.send_message(reply)
            elif msg.content == "/bot-poke":
                _ = client.send_poke(msg.room_id, msg.sender_id)
            # elif msg.content == "/bot-签到":
            #     client.send_room_sign_in(msg.room_id)
        else:
            if msg.content == "/bot-rm":
                # 对着某条消息发出 rm 指令(确信)
                # admin only
                sender = msg.sender_id
                admins = client.status.admins
                if sender in admins:
                    rm_msg = msg.reply_msg_id
                    success = client.delete_msg_raw(msg.room_id, rm_msg)  # noqa
                    print(f"删除消息 {rm_msg}-{msg.room_id} 结果: {success}")
                    if not success:
                        reply = msg.reply_with("删除失败")
                        _ = client.send_message(reply)
                else:
                    # reply = msg.reply_with("你不是管理员")
                    # client.send_message(reply)
                    # 静默忽略
                    ...


def on_tailchat_message(msg: TailchatReciveMessage, client: TailchatClient) -> None:
    if not (msg.is_reply or msg.is_from_self):
        if msg.content == "/bot-py":
            reply = msg.reply_with(
                f"tailchat-async-rs({client.version})-sync-py {client.tailchat_version}"
            )
            client.send_message(reply)
        elif msg.content == "/bot-sys":
            datas = local_env_info()
            reply = msg.reply_with(datas)
            # reply.set_img(local_env_image(), "just_img.png")
            client.send_message(reply)
        elif msg.content == "/bot-uptime":
            uptime = client.startup_time
            up_delta = datetime.now(timezone.utc) - uptime
            reply = msg.reply_with(f"Bot 运行时间: {up_delta}")
            client.send_message(reply)
