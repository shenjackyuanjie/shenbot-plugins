from __future__ import annotations

import time

from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient
    from ica_typing import TailchatReciveMessage, TailchatClient
else:
    IcaNewMessage = TypeVar("NewMessage")
    IcaClient = TypeVar("IcaClient")
    TailchatReciveMessage = TypeVar("TailchatReciveMessage")
    TailchatClient = TypeVar("TailchatClient")


class Counter:
    def __init__(self, count_time: int = 60):
        self.msg_pre_min_ica = 0.0
        self.ica_msg: list[float] = []
        self.msg_pre_min_tc = 0.0
        self.tc_msg: list[float] = []
        self.count_time = count_time

    def ica_update(self):
        now = time.time()
        self.ica_msg.append(now)
        self.ica_msg = [x for x in self.ica_msg if now - x <= self.count_time]
        self.msg_pre_min_ica = len(self.ica_msg) / (self.count_time / 60)

    def tc_update(self):
        now = time.time()
        self.tc_msg.append(now)
        self.tc_msg = [x for x in self.tc_msg if now - x <= self.count_time]
        self.msg_pre_min_tc = len(self.tc_msg) / (self.count_time / 60)

    @property
    def ica_frequence(self):
        return self.msg_pre_min_ica

    @property
    def tc_frequence(self):
        return self.msg_pre_min_tc


COUNTER = Counter(120)

def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    if msg.is_from_self:
        return
    COUNTER.ica_update()
    if msg.is_reply:
        return
    if msg.content == "/bot-count":
        reply = f"ica 每分钟消息数: {COUNTER.ica_frequence:.2f}\ntailchat 每分钟消息数: {COUNTER.tc_frequence:.2f}"
        client.send_message(msg.reply_with(reply))


def on_tailchat_message(msg: TailchatReciveMessage, client: TailchatClient) -> None:
    if msg.is_from_self:
        return
    COUNTER.tc_update()
    if msg.is_reply:
        return
    if msg.content == "/bot-count":
        reply = f"ica 每分钟消息数: {COUNTER.ica_frequence:.2f}\ntailchat 每分钟消息数: {COUNTER.tc_frequence:.2f}"
        client.send_message(msg.reply_with(reply))
