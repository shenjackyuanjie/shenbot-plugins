from __future__ import annotations

# Python 兼容版本 3.8+

from typing import Callable, Union
from datetime import datetime

"""
ica.rs
pub type RoomId = i64;
pub type UserId = i64;
pub type MessageId = String;
"""


class IcaType:
    RoomId = int
    UserId = int
    MessageId = str


"""
tailchat.rs
pub type GroupId = String;
pub type ConverseId = String;
pub type UserId = String;
pub type MessageId = String;
"""


class TailchatType:
    GroupId = str
    ConverseId = str
    UserId = str
    MessageId = str


class IcaStatus:
    """
    ica状态信息
    此类并不存储信息, 所有方法都是实时获取
    """

    @property
    def qq_login(self) -> bool: ...
    @property
    def online(self) -> bool: ...
    @property
    def self_id(self) -> IcaType.UserId: ...
    @property
    def nick_name(self) -> str: ...
    @property
    def ica_version(self) -> str: ...
    @property
    def os_info(self) -> str: ...
    @property
    def resident_set_size(self) -> str: ...
    @property
    def heap_used(self) -> str: ...
    @property
    def loaded_messages_count(self) -> int: ...

    def loaded_messages_count_for(self, room_id: IcaType.RoomId) -> int:
        """获取指定房间最近一次成功加载的消息数量，未知房间返回 0。"""
        ...
    @property
    def load(self) -> str: ...
    @property
    def rooms(self) -> list[IcaRoom]:
        """
        于 2.0.1 添加
        获取当前用户加入的所有房间
        @return: 房间列表
        """
        ...

    @property
    def admins(self) -> list[IcaType.UserId]:
        """
        于 2.0.1 添加
        获取所有管理员
        """
        ...

    @property
    def filtered(self) -> list[IcaType.UserId]:
        """
        于 2.0.1 添加
        获取所有被过滤的用户
        (好像没啥用就是了, 反正被过滤的不会给到插件)
        """
        ...


class IcaRoom:
    """
    于 2.0.1 添加, 用于获取房间信息
    """

    @property
    def room_id(self) -> IcaType.RoomId: ...
    @property
    def room_name(self) -> str: ...
    @property
    def unread_count(self) -> int: ...
    @property
    def priority(self) -> int:
        """
        房间优先级
        @return: 优先级
        """
        ...

    @property
    def utime(self) -> int:
        """
        房间最后更新时间
        @return: 时间戳 (time.time * 1000)
        """
        ...

    def is_group(self) -> bool:
        """
        判断是否为群聊
        @return: 是否为群聊
        """
        ...

    def is_chat(self) -> bool:
        """
        判断是否为私聊
        @return: 是否为私聊
        """
        ...

    def new_message_to(self, content: str) -> IcaSendMessage:
        """
        创建一条发送到这个房间的消息
        @param content: 消息内容
        @return: 消息对象
        """
        ...


class IcaGroupMember:
    @property
    def user_id(self) -> IcaType.UserId: ...
    @property
    def nickname(self) -> str: ...
    @property
    def card(self) -> str: ...
    @property
    def remark(self) -> str: ...
    @property
    def title(self) -> str: ...
    @property
    def level(self) -> str: ...
    @property
    def role(self) -> str: ...
    @property
    def shutup_time(self) -> int:
        """禁言结束时间，Unix 秒。"""
        ...

    def display_name(self) -> str:
        """优先返回群名片，群名片为空时返回昵称。"""
        ...

    def is_muted_at(self, timestamp: int) -> bool: ...
    def is_muted(self) -> bool: ...
    def remaining_mute_seconds_at(self, timestamp: int) -> int: ...
    def remaining_mute_seconds(self) -> int: ...


class IcaReplyMessage: ...


class IcaSendMessage:
    @property
    def content(self) -> str: ...
    @content.setter
    def content(self, value: str) -> None: ...
    @property
    def room_id(self) -> IcaType.RoomId: ...
    @room_id.setter
    def room_id(self, value: IcaType.RoomId) -> None: ...
    def with_content(self, content: str) -> IcaSendMessage:
        """
        为了链式调用, 返回自身
        """
        self.content = content
        return self

    def set_img(self, file: bytes, file_type: str, as_sticker: bool):
        """
        设置消息的图片
        @param file: 图片文件 (实际上是 vec<u8>)
        @param file_type: 图片类型 (MIME) (image/png; image/jpeg)
        @param as_sticker: 是否作为贴纸发送
        """

    def add_img(self, file: bytes, file_type: str):
        """追加一张图片/媒体；多次调用可构造多图消息。"""

    def set_audio(self, file: bytes, file_type: str) -> None:
        """替换为语音媒体并关闭贴纸标记；MIME 必须为 audio/*，转码由 Bridge 处理。"""
        ...

    def set_audio_fid(self, fid: str, file_type: str) -> None:
        """复用协议侧语音资源，不重新上传或编码；失败时抛出 RuntimeError。"""
        ...

    def remove_reply(self) -> IcaSendMessage:
        """删除回复"""
        ...


class IcaDeleteMessage:
    def __str__(self) -> str: ...


class IcaNewMessage:
    """
    Icalingua 接收到新消息
    """

    def reply_with(self, message: str) -> IcaSendMessage:
        """创建一条 回复这条消息 的可发送消息"""
        ...

    def as_deleted(self) -> IcaDeleteMessage:
        """作为一条要被撤回的消息"""
        ...

    def __str__(self) -> str: ...
    @property
    def id(self) -> IcaType.MessageId:
        """消息的 Id"""
        ...

    @property
    def content(self) -> str: ...
    @property
    def sender_id(self) -> IcaType.UserId:
        """获取发送人id"""
        ...

    @property
    def sender_name(self) -> str:
        """获取发送人名字"""
        ...

    @property
    def is_from_self(self) -> bool:
        """是不是自己发的消息"""
        ...

    @property
    def is_reply(self) -> bool:
        """是不是回复消息"""
        ...

    @property
    def is_room_msg(self) -> bool:
        """是否是群聊消息"""
        ...

    @property
    def is_chat_msg(self) -> bool:
        """是否是私聊消息"""
        ...

    @property
    def room_id(self) -> IcaType.RoomId:
        """
        如果是群聊消息, 返回 (-群号)
        如果是私聊消息, 返回 对面qq
        """
        ...

    @property
    def reply_msg_id(self) -> IcaType.MessageId | None:
        """如果是回复消息, 返回被回复的消息id
        于 2.0.2 添加"""
        ...

    @property
    def reply_msg_content(self) -> str | None:
        """如果是回复消息, 返回被回复的消息内容
        于 2.0.2 添加"""
        ...

    @property
    def reply_msg_sender_name(self) -> str | None:
        """如果是回复消息, 返回被回复的消息发送者名称
        于 2.0.2 添加"""
        ...


class IcaClient:
    """
    Icalingua 的客户端
    """

    def report_read(self, message_id: IcaType.MessageId) -> None:
        """主动上报 QQ 已读，不清除 Bridge 会话未读数；失败时抛出 RuntimeError。"""
        ...

    def handle_request(
        self, request_type: str, flag: str, accept: bool = True
    ) -> None:
        """处理 friend/group 申请；成功仅表示请求已发送，不代表 QQ 已执行。"""
        ...

    def get_nt_pic_url_by_file_id(self, file_id: str, app_id: str = "") -> str:
        """请求 QQ NT 图片新地址；12 秒 ACK 超时或无效地址会抛出 RuntimeError。"""
        ...

    def send_room_sign_in(self, room_id: IcaType.RoomId) -> bool:
        """向某个群发送签到
        于 1.6.5 添加"""
        if room_id > 0:
            self.warn("不能向私聊发送签到信息")
            return False
        # send
        return True

    def send_poke(self, room_id: IcaType.RoomId, user_id: IcaType.UserId) -> bool:
        """向指定群/私聊发送戳一戳
        于 1.6.5 添加"""
        ...

    def set_group_ban(
        self,
        room_id: IcaType.RoomId,
        user_id: IcaType.UserId,
        duration: int,
    ) -> bool:
        """禁言或解除禁言群成员；duration 为 0 时解除，最大 2592000 秒。"""
        ...

    def get_group_members(self, room_id: IcaType.RoomId) -> list[IcaGroupMember]:
        """查询群成员；失败时抛出 RuntimeError。"""
        ...

    def get_muted_group_members(self, room_id: IcaType.RoomId) -> list[IcaGroupMember]:
        """查询当前仍处于禁言中的群成员；失败时抛出 RuntimeError。"""
        ...

    # @staticmethod
    # async def send_message_a(client: "IcaClient", message: SendMessage) -> bool:
    #     """
    #     仅作占位, 不能使用
    #     (因为目前来说, rust调用 Python端没法启动一个异步运行时
    #     所以只能 tokio::task::block_in_place 转换成同步调用)
    #     """
    def send_message(self, message: IcaSendMessage) -> bool:
        """发送一条消息"""
        ...

    def send_raw_message(self, raw_msg: str, room_id: IcaType.RoomId) -> bool:
        """
        发送一条原始信息
        警告: 小心使用
        于 2.0.1 添加
        """
        ...

    def send_and_warn(self, message: IcaSendMessage) -> bool:
        """发送消息, 并在日志中输出警告信息"""
        self.warn(message.content)
        return self.send_message(message)

    def delete_message(self, message: IcaDeleteMessage) -> bool:
        """向某条消息发送阿瓦隆指令(确信)"""
        ...

    def delete_msg_raw(self, room_id: IcaType.RoomId, message_id: IcaType.MessageId) -> bool:
        """向某条消息发送阿瓦隆指令(确信)
        现在以原始参数形式提供(捂脸)
        原来的几乎没法用, 私密马赛
        于 2.0.2 添加"""
        ...

    @property
    def status(self) -> IcaStatus: ...
    @property
    def version(self) -> str: ...
    @property
    def version_str(self) -> str:
        """获取一个更完善的版本号信息"""
        ...

    @property
    def client_id(self) -> str:
        """返回一个"唯一"的客户端id"""
        ...

    @property
    def ica_version(self) -> str:
        """shenbot ica 的版本号"""
        ...

    @property
    def startup_time(self) -> datetime:
        """请注意, 此时刻为 UTC 时刻"""
        ...

    @property
    def py_tasks_count(self) -> int:
        """获取当前正在运行的 Python 任务数量
        于 1.6.7 添加"""
        ...

    def sync_status_from_file(self) -> None:
        """重载插件状态"""
        ...

    def reload_plugin(self, plugin_name: str) -> bool:
        """重载插件"""
        ...

    def set_plugin_status(self, plugin_name: str, status: bool) -> None:
        """设置插件状态"""
        ...

    def get_plugin_status(self, plugin_name: str) -> bool | None:
        """获取插件状态"""
        ...

    def sync_status_to_file(self) -> None:
        """将插件状态同步到配置文件"""
        ...

    def debug(self, message: str) -> None:
        """向日志中输出调试信息"""
        ...

    def info(self, message: str) -> None:
        """向日志中输出信息"""
        ...

    def warn(self, message: str) -> None:
        """向日志中输出警告信息"""
        ...


class IcaJoinRequest:
    """
    Icalingua 接收到好友申请、入群申请或群邀请；统一使用 on_ica_join_request hook。
    """

    @property
    def comment(self) -> str:
        """入群请求的附言"""
        ...

    @property
    def group_id(self) -> IcaType.RoomId | None:
        """正群号；好友申请或缺失群号时为 None，使用前先判断 request_type。"""
        ...

    @property
    def group_name(self) -> str:
        """群名称"""
        ...

    @property
    def user_id(self) -> IcaType.UserId:
        """请求者 QQ"""
        ...

    @property
    def nickname(self) -> str:
        """请求者昵称"""
        ...

    @property
    def request_type(self) -> str:
        """请求类型"""
        ...

    @property
    def post_type(self) -> str:
        """事件类型"""
        ...

    @property
    def sub_type(self) -> str:
        """子类型 (add/invite)"""
        ...

    @property
    def time(self) -> int:
        """请求时间 (时间戳)"""
        ...

    @property
    def tips(self) -> str:
        """提示信息"""
        ...

    @property
    def flag(self) -> str:
        """用于处理请求的 flag"""
        ...

    @property
    def source(self) -> str:
        """申请来源，缺失时为空字符串。"""
        ...

    @property
    def self_id(self) -> IcaType.UserId | None: ...
    @property
    def age(self) -> int | None: ...
    @property
    def sex(self) -> str | None: ...


class TailchatReciveMessage:
    """
    Tailchat 接收到的新消息
    """

    @property
    def id(self) -> TailchatType.MessageId: ...
    @property
    def content(self) -> str: ...
    @property
    def sender_id(self) -> TailchatType.UserId: ...
    @property
    def is_from_self(self) -> bool: ...
    @property
    def is_reply(self) -> bool: ...
    @property
    def group_id(self) -> TailchatType.GroupId | None:
        """服务器 Id"""
        ...

    @property
    def converse_id(self) -> TailchatType.ConverseId:
        """会话 Id"""
        ...

    def reply_with(self, message: str) -> "TailchatSendingMessage":
        """创建一条 回复这条消息 的可发送消息"""
        ...

    def as_reply(self, message: str) -> "TailchatSendingMessage":
        """回复这条消息"""
        ...


class TailchatSendingMessage:
    """
    Tailchat 将要发送的信息
    """

    @property
    def content(self) -> str:
        """内容"""
        ...

    @content.setter
    def content(self, value: str) -> None: ...
    @property
    def group_id(self) -> TailchatType.GroupId | None:
        """服务器 Id"""
        ...

    @group_id.setter
    def group_id(self, value: TailchatType.GroupId | None) -> None: ...
    @property
    def converse_id(self) -> TailchatType.ConverseId:
        """会话 Id"""
        ...

    @converse_id.setter
    def converse_id(self, value: TailchatType.ConverseId) -> None: ...
    def clear_meta(self) -> "TailchatSendingMessage":
        """
        清除所有元数据, 可以用于取消 回复
        """
        self.meta = None
        return self

    def with_content(self, content: str) -> "TailchatSendingMessage":
        """
        为了链式调用, 返回自身
        """
        self.content = content
        return self

    def set_img(self, file: bytes, file_name: str):
        """
        设置消息的图片
        @param file: 图片文件 (实际上是 vec<u8>)
        @param file_name: 图片名称 (just_img.png)
        """


class TailchatClient:
    """
    Tailchat 的客户端
    """

    def send_message(self, message: TailchatSendingMessage) -> bool: ...
    def send_and_warn(self, message: TailchatSendingMessage) -> bool:
        """发送消息, 并在日志中输出警告信息"""
        self.warn(message.content)
        return self.send_message(message)

    def new_message(
        self,
        content: str,
        converse_id: TailchatType.ConverseId,
        group_id: TailchatType.GroupId | None = None,
    ) -> "TailchatSendingMessage":
        """创建一条新消息, 可用于发送"""
        ...

    @property
    def version(self) -> str: ...
    @property
    def version_str(self) -> str:
        """获取一个更完善的版本号信息"""
        ...

    @property
    def client_id(self) -> str:
        """返回一个"唯一"的客户端id"""
        ...

    @property
    def tailchat_version(self) -> str:
        """tailchat 的版本号"""
        ...

    @property
    def startup_time(self) -> datetime:
        """请注意, 此时刻为 UTC 时刻"""
        ...

    @property
    def py_tasks_count(self) -> int:
        """获取当前正在运行的 Python 任务数量
        于 1.2.6 添加"""
        ...

    def sync_status_from_file(self) -> None:
        """从状态文件同步插件启用状态。"""
        ...

    def reload_plugin(self, plugin_name: str) -> bool:
        """重载插件"""
        ...

    def set_plugin_status(self, plugin_name: str, status: bool) -> None:
        """设置插件状态"""
        ...

    def get_plugin_status(self, plugin_name: str) -> bool | None:
        """获取插件状态"""
        ...

    def sync_status_to_file(self) -> None:
        """将插件状态同步到配置文件。"""
        ...

    def debug(self, message: str) -> None:
        """向日志中输出调试信息"""

    def info(self, message: str) -> None:
        """向日志中输出信息"""

    def warn(self, message: str) -> None:
        """向日志中输出警告信息"""


# 保留历史拼写；这是两个后端消息类型的联合，而不是可实例化的运行时类型。
ReciveMessage = Union[IcaNewMessage, TailchatReciveMessage]


# PluginHost 在加载或重载时快照以下 hook；运行中重新绑定函数需再次重载插件。
on_load = Callable[[], None]
on_unload = Callable[[], None]
on_ica_message = Callable[[IcaNewMessage, IcaClient], None]
on_ica_system_message = Callable[[IcaNewMessage, IcaClient], None]
on_ica_delete_message = Callable[[IcaType.MessageId, IcaClient], None]
on_ica_join_request = Callable[[IcaJoinRequest, IcaClient], None]
on_tailchat_message = Callable[[TailchatReciveMessage, TailchatClient], None]
