from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from shenbot_api import PluginManifest

if TYPE_CHECKING:
    from ica_typing import IcaClient, IcaNewMessage

VERSION = "0.1.0"
HISTORY_COMMAND = "/bot-history"
SEARCH_COMMAND = "/bot-search"
MEDIA_COMMAND = "/bot-media"
DEFAULT_LIMIT = 5
MAX_LIMIT = 20
MAX_KEYWORD_LENGTH = 200

PLUGIN_MANIFEST = PluginManifest(
    plugin_id="bridge_query",
    name="Bridge 查询示例",
    version=VERSION,
    description="管理员显式查询当前房间历史、搜索与回复消息媒体结构；不重放历史",
    authors=["shenjack"],
)


def _preview(value: object, limit: int) -> str:
    """展示纯文本摘要，避免把历史里的协议标记再次作为消息节点发送。"""
    text = " ".join(str(value or "").split())
    text = text.translate(str.maketrans({"[": "［", "]": "］", "<": "＜", ">": "＞"}))
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _media_summary(files: list[dict]) -> str:
    labels = {"image": "图片", "audio": "语音", "video": "视频"}
    counts: dict[str, int] = {}
    for file in files:
        kind = str(file.get("type", "file")).split("/", 1)[0].lower()
        label = labels.get(kind, "文件")
        counts[label] = counts.get(label, 0) + 1
    return "、".join(f"{label} {count}" for label, count in counts.items()) or "无附件"


def _message_time(timestamp_ms: int | None) -> str:
    if timestamp_ms is None:
        return "时间未知"
    try:
        # datetime.UTC 从 Python 3.11 才提供；示例仍可被旧宿主加载。
        when = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)  # noqa: UP017
        return when.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return "时间未知"


def _message_summary(message: IcaNewMessage) -> str:
    content = _preview(message.content, 80) or "（无正文）"
    sender = _preview(message.sender_name, 30) or "未知发送者"
    when = _message_time(message.time)
    details = [_media_summary(message.files)]
    if message.at is True:
        details.append("提及当前账号")
    elif message.at == "all":
        details.append("提及全体成员")
    if message.code:
        details.append("含协议附加数据")
    return f"{sender} · {when}\n{content}\n{'；'.join(details)}"


def _reply(client: IcaClient, message: IcaNewMessage, text: str) -> None:
    # bool 只表示发送请求受理，不将它解释为 QQ 已发送成功。
    if not client.send_message(message.reply_with(text)):
        client.warn("bridge_query：摘要发送请求未受理")


def _history(message: IcaNewMessage, client: IcaClient, arguments: str) -> None:
    try:
        limit = int(arguments) if arguments else DEFAULT_LIMIT
    except ValueError:
        limit = 0
    if not 1 <= limit <= MAX_LIMIT:
        _reply(client, message, "用法：/bot-history [N]，N 为 1-20，默认 5。")
        return
    fetch = getattr(client, "fetch_messages", None)
    if not callable(fetch):
        _reply(client, message, "当前 Bot 尚无历史查询 API，请先升级 Bot。")
        return
    records = fetch(message.room_id)
    selected = records[-limit:]
    header = f"当前房间历史：本页 {len(records)} 条，展示末尾 {len(selected)} 条。"
    summaries = [f"{index}. {_message_summary(record)}" for index, record in enumerate(selected, 1)]
    _reply(client, message, "\n\n".join([header, *summaries]) if selected else "当前房间暂无本地历史。")


def _search(message: IcaNewMessage, client: IcaClient, keyword: str) -> None:
    if not keyword or len(keyword) > MAX_KEYWORD_LENGTH:
        _reply(client, message, "用法：/bot-search <关键词>，关键词为 1-200 个字符。")
        return
    search = getattr(client, "search_messages", None)
    if not callable(search):
        _reply(client, message, "当前 Bot 尚无搜索 API，请先升级 Bot。")
        return
    records = search(message.room_id, keyword)
    selected = records[:DEFAULT_LIMIT]
    header = f"当前房间搜索：本页 {len(records)} 条，展示 {len(selected)} 条。"
    summaries = [f"{index}. {_message_summary(record)}" for index, record in enumerate(selected, 1)]
    _reply(client, message, "\n\n".join([header, *summaries]) if selected else "当前房间没有匹配的本地消息。")


def _media(message: IcaNewMessage, client: IcaClient, arguments: str) -> None:
    if arguments or not message.is_reply:
        _reply(client, message, "请回复一条消息并发送 /bot-media，查看其媒体结构摘要。")
        return
    if not hasattr(message, "reply"):
        _reply(client, message, "当前 Bot 尚无结构化回复 API，请先升级 Bot。")
        return
    reply = message.reply
    if not isinstance(reply, dict):
        _reply(client, message, "当前回复未包含可用的原消息结构。")
        return
    files = reply.get("files") or []
    if not files and isinstance(reply.get("file"), dict):
        files = [reply["file"]]
    files = [file for file in files if isinstance(file, dict)]
    content = _preview(reply.get("content"), 80) or "（无正文）"
    _reply(client, message, f"回复消息媒体：{_media_summary(files)}\n正文摘要：{content}")


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    """仅响应管理员明确命令；查询结果不会送回新消息 hook 或逐条发送。"""
    if msg.is_from_self:
        return
    parts = msg.content.strip().split(maxsplit=1)
    if not parts:
        return
    command = parts[0]
    if command not in (HISTORY_COMMAND, SEARCH_COMMAND, MEDIA_COMMAND):
        return
    if msg.sender_id not in client.status.admins:
        return
    arguments = parts[1].strip() if len(parts) == 2 else ""
    try:
        if command == HISTORY_COMMAND:
            _history(msg, client, arguments)
        elif command == SEARCH_COMMAND:
            _search(msg, client, arguments)
        else:
            _media(msg, client, arguments)
    except RuntimeError:
        # 不回显远端原始 payload 或异常文本，避免把凭据/临时资源 URL 放进聊天。
        client.warn(f"bridge_query：{command} 查询失败")
        _reply(client, msg, "查询失败：Bridge 无响应或返回错误，请检查连接与 Bot 日志。")
