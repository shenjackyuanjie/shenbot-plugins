from __future__ import annotations
from typing import TYPE_CHECKING, TypeVar

import time
import requests
import threading

if TYPE_CHECKING:
    from ica_typing import (
        IcaNewMessage,
        IcaClient,
    )

else:
    IcaNewMessage = TypeVar("NewMessage")
    IcaClient = TypeVar("IcaClient")

检查频率 = 10
"""
几秒 检查一下
"""

WORK = {
    "http://query.bjeea.cn/queryService/rest/score/68386": "敬请关注北京教育考试院公布的开通时间。"
}

检测群: dict[int, IcaNewMessage] = {}
ICA_CLIENT: IcaClient | None = None

def get_url(url: str) -> str | None:
    try:
        res = requests.get(url, timeout=5)
        data = res.text
    except Exception:
        return None


def gen_at(user_id: int, user_name: str) -> str:
    """
    source:
    https://github.com/Icalingua-plus-plus/Icalingua-plus-plus/blob/51ae7d2f2403188c08b942ea7e4bd538725dbfaa/icalingua/src/main/adapters/oicqAdapter.ts#L1517-L1533
    """
    return f"<IcalinguaAt qq={user_id}>@{user_name}</IcalinguaAt>"


def gen_at_by_msg(msg: IcaNewMessage) -> str:
    return gen_at(msg.sender_id, msg.sender_name)


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    global ICA_CLIENT
    ICA_CLIENT = client
    if (msg.is_from_self or msg.is_reply):
        return

    if msg.content == "/开启检查":
        if msg.room_id not in 检测群:
            检测群[msg.room_id] = msg
            client.send_message(msg.reply_with("已开启"))
        else:
            client.send_message(msg.reply_with("当前群已开启"))
    elif msg.content == "/关闭检查":
        if msg.room_id in 检测群:
            检测群.pop(msg.room_id)
            client.send_message(msg.reply_with("已关闭"))
        else:
            client.send_message(msg.reply_with("当前群未开启"))
    elif msg.content == "/检查":
        """
        url: xxxx
        msg: xxxx
        """
        if msg.room_id in 检测群:
            任务 = "\n".join(f"url: {url}\nmsg: {check_msg}" for (url, check_msg) in WORK.items())
            client.send_message(msg.reply_with(f"当前群已开启\n{任务}"))
        else:
            client.send_message(msg.reply_with("当前群未开启"))
    elif msg.content == "/检查 test":
        reply = msg.reply_with(f"测试 @ {gen_at_by_msg(msg)}")
        client.send_message(reply)


def check_urls() -> list[str]:
    update = []
    for (url, msg) in WORK.items():
        if (content := get_url(url)) is not None:
            if msg not in content:
                update.append(url)
    return update


def check_urls_thread() -> None:
    while True:
        update_urls = check_urls()
        if update_urls and ICA_CLIENT is not None:
            for (room_id, msg) in 检测群.items():
                new_msg = msg.reply_with(f"{update_urls}\n更新了!!")
                ICA_CLIENT.send_message(new_msg)
            break
        time.sleep(检查频率)

def on_load():
    threading.Thread(target=check_urls_thread).start()
