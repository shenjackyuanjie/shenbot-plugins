"""
group_door.py - 群门卫

配置示例 (config/group_door.toml):

```toml
[main]
# 默认禁言时长。支持秒数，或 30m / 6h / 2d / 2day / 2天 这类写法。
default_mute_duration = "2d"

[request_forward]
# 接收某个群的加群申请，并转发到一个或多个群。
# room id 可以写正群号，也可以写 ica 内部负 room_id。
rules = [
    { receive = 707355501, send = 123456789 },
    { receive = -888888888, send = [-111111111, -222222222] },
]

[mute]
# 仅对这里指定的群生效；未写 duration 时使用 main.default_mute_duration。
rules = [
    { room = 707355501, duration = "2d" },
    { room = -888888888, duration = "6h" },
]
```
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from ica_typing import IcaClient, IcaNewMessage, IcaJoinRequest

from shenbot_api import ConfigStorage, PluginManifest, python_config_path

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

VERSION = "0.1.0"
DEFAULT_MUTE_DURATION = 2 * 24 * 60 * 60
CONFIG_FILE = "group_door.toml"

JOIN_MSG_ID_RE = re.compile(r"^\d+-([1-9]\d*)-([1-9]\d*)$")
JOIN_CONTENT_RE = re.compile(r"^.+\((\d+)\)\s*加入了本群\s*$")
DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z\u4e00-\u9fff]*)\s*$")

PLUGIN_MANIFEST = PluginManifest(
    plugin_id="group_door",
    name="群门卫",
    version=VERSION,
    description="转发加群申请，并对指定群的新入群成员自动禁言",
    authors=["shenjack"],
    config={
        "main": ConfigStorage(
            default_mute_duration="2d",
        ),
        "request_forward": ConfigStorage(
            rules=[],
        ),
        "mute": ConfigStorage(
            rules=[],
        ),
    },
)

REQUEST_FORWARDS: dict[int, list[int]] = {}
MUTE_RULES: dict[int, int] = {}
DEFAULT_MUTE_SECONDS = DEFAULT_MUTE_DURATION


def log(client: Any, level: str, message: str) -> None:
    logger = getattr(client, level, None)
    if callable(logger):
        logger(f"[group-door] {message}")
    else:
        print(f"[group-door] {message}")


def config_path() -> str:
    return os.path.join(python_config_path(), CONFIG_FILE)


def read_toml_config() -> dict[str, Any] | None:
    if tomllib is None:
        return None

    path = config_path()
    if not os.path.isfile(path):
        return None

    with open(path, "rb") as f:
        parsed = tomllib.load(f)
    return parsed if isinstance(parsed, dict) else None


def to_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError(f"不能把 bool 当作数字配置: {value!r}")
    if isinstance(value, (int, float)):
        return int(value)
    return int(str(value).strip())


def normalize_group_room_id(value: object) -> int:
    room_id = to_int(value)
    if room_id == 0:
        return 0
    return -abs(room_id)


def parse_duration(value: object, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"不能把 bool 当作禁言时长: {value!r}")
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip()
    match = DURATION_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"无法解析禁言时长: {value!r}")

    amount = float(match.group(1))
    unit = match.group(2).lower()
    unit_seconds = {
        "": 1,
        "s": 1,
        "sec": 1,
        "secs": 1,
        "second": 1,
        "seconds": 1,
        "秒": 1,
        "m": 60,
        "min": 60,
        "mins": 60,
        "minute": 60,
        "minutes": 60,
        "分钟": 60,
        "h": 60 * 60,
        "hour": 60 * 60,
        "hours": 60 * 60,
        "小时": 60 * 60,
        "d": 24 * 60 * 60,
        "day": 24 * 60 * 60,
        "days": 24 * 60 * 60,
        "天": 24 * 60 * 60,
    }
    if unit not in unit_seconds:
        raise ValueError(f"未知禁言时长单位: {value!r}")
    return int(amount * unit_seconds[unit])


def as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)
    return [value]


def iter_forward_rule_items(table: object) -> list[object]:
    if not isinstance(table, dict):
        return []

    items: list[object] = []
    for key in ("rules", "pairs", "forwards"):
        raw_items = table.get(key)
        if isinstance(raw_items, list):
            items.extend(raw_items)

    rooms = table.get("rooms")
    if isinstance(rooms, dict):
        for receive, send in rooms.items():
            items.append({"receive": receive, "send": send})

    reserved_keys = {"rules", "pairs", "forwards", "rooms"}
    for receive, send in table.items():
        if receive not in reserved_keys:
            items.append({"receive": receive, "send": send})

    return items


def iter_mute_rule_items(table: object) -> list[object]:
    if not isinstance(table, dict):
        return []

    items: list[object] = []
    raw_rules = table.get("rules")
    if isinstance(raw_rules, list):
        items.extend(raw_rules)

    rooms = table.get("rooms")
    if isinstance(rooms, dict):
        for room, duration in rooms.items():
            items.append({"room": room, "duration": duration})
    elif isinstance(rooms, list):
        for room in rooms:
            items.append({"room": room})

    reserved_keys = {"rules", "rooms", "default_duration"}
    for room, duration in table.items():
        if room not in reserved_keys:
            items.append({"room": room, "duration": duration})

    return items


def add_forward_rule(receive: object, send: object) -> None:
    receive_room_id = normalize_group_room_id(receive)
    if receive_room_id == 0:
        return

    send_room_ids: list[int] = []
    for item in as_list(send):
        room_id = normalize_group_room_id(item)
        if room_id != 0:
            send_room_ids.append(room_id)

    if not send_room_ids:
        return

    room_ids = REQUEST_FORWARDS.setdefault(receive_room_id, [])
    for room_id in send_room_ids:
        if room_id not in room_ids:
            room_ids.append(room_id)


def load_forward_rules(table: object) -> None:
    for item in iter_forward_rule_items(table):
        if not isinstance(item, dict):
            continue

        receive = (
            item.get("receive")
            if "receive" in item
            else item.get("recive", item.get("recv", item.get("from")))
        )
        send = item.get("send") if "send" in item else item.get("to")
        if receive is None or send is None:
            continue
        add_forward_rule(receive, send)


def load_mute_rules(table: object, default_duration: int) -> None:
    for item in iter_mute_rule_items(table):
        if not isinstance(item, dict):
            continue

        room = item.get("room") if "room" in item else item.get("room_id")
        if room is None:
            continue

        room_id = normalize_group_room_id(room)
        if room_id == 0:
            continue

        duration = parse_duration(item.get("duration"), default_duration)
        MUTE_RULES[room_id] = duration


def load_config() -> None:
    global DEFAULT_MUTE_SECONDS

    REQUEST_FORWARDS.clear()
    MUTE_RULES.clear()
    DEFAULT_MUTE_SECONDS = DEFAULT_MUTE_DURATION

    parsed = read_toml_config()
    if parsed is not None:
        main_table = parsed.get("main", {})
        if isinstance(main_table, dict):
            DEFAULT_MUTE_SECONDS = parse_duration(
                main_table.get("default_mute_duration"),
                DEFAULT_MUTE_DURATION,
            )

        mute_table = parsed.get("mute", {})
        if isinstance(mute_table, dict) and "default_duration" in mute_table:
            DEFAULT_MUTE_SECONDS = parse_duration(
                mute_table.get("default_duration"),
                DEFAULT_MUTE_SECONDS,
            )

        load_forward_rules(parsed.get("request_forward", {}))
        load_mute_rules(mute_table, DEFAULT_MUTE_SECONDS)
        return

    main_cfg = PLUGIN_MANIFEST.config_unchecked("main")
    DEFAULT_MUTE_SECONDS = parse_duration(
        main_cfg.get_value("default_mute_duration"),
        DEFAULT_MUTE_DURATION,
    )

    forward_cfg = PLUGIN_MANIFEST.config_unchecked("request_forward")
    load_forward_rules({"rules": forward_cfg.get_value("rules")})

    mute_cfg = PLUGIN_MANIFEST.config_unchecked("mute")
    load_mute_rules({"rules": mute_cfg.get_value("rules")}, DEFAULT_MUTE_SECONDS)


def on_load() -> None:
    load_config()


def find_room(client: IcaClient, room_id: int) -> Any | None:
    for room in client.status.rooms:
        if room.room_id == room_id:
            return room
    return None


def send_text_to_room(client: IcaClient, room_id: int, text: str) -> bool:
    target_room = find_room(client, room_id)
    if target_room is None:
        return False
    return bool(client.send_message(target_room.new_message_to(text)))


def format_join_request(event: IcaJoinRequest, receive_room_id: int) -> str:
    comment = event.comment.strip()
    tips = event.tips.strip()
    flag = event.flag.strip()

    lines = [
        "🚪 收到加群申请",
        f"群：{event.group_name} ({abs(receive_room_id)})",
        f"用户：{event.nickname} ({event.user_id})",
    ]
    if comment:
        lines.append(f"验证：{comment}")
    if tips:
        lines.append(f"提示：{tips}")
    if flag:
        lines.append(f"flag：{flag}")
    return "\n".join(lines)


def on_ica_join_request(event: IcaJoinRequest, client: IcaClient) -> None:
    print("")
    if event.request_type != "group":
        return
    if event.sub_type != "add":
        return

    receive_room_id = normalize_group_room_id(event.group_id)
    send_room_ids = REQUEST_FORWARDS.get(receive_room_id, [])
    if not send_room_ids:
        return

    text = format_join_request(event, receive_room_id)
    for room_id in send_room_ids:
        if not send_text_to_room(client, room_id, text):
            log(client, "warn", f"转发加群申请失败，找不到目标群 {room_id}")


def parse_join_notice(msg: IcaNewMessage, room_id: int) -> int | None:
    id_match = JOIN_MSG_ID_RE.fullmatch(str(msg.id))
    if id_match is None:
        return None

    msg_group_id = int(id_match.group(1))
    user_id = int(id_match.group(2))
    if msg_group_id != abs(room_id):
        return None

    content_match = JOIN_CONTENT_RE.fullmatch(msg.content)
    if content_match is None:
        return None

    content_user_id = int(content_match.group(1))
    if content_user_id != user_id:
        return None

    return user_id


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    if not msg.is_room_msg:
        return

    room_id = normalize_group_room_id(msg.room_id)
    duration = MUTE_RULES.get(room_id)
    if duration is None:
        return

    user_id = parse_join_notice(msg, room_id)
    if user_id is None:
        return

    ok = bool(cast(Any, client).set_group_ban(room_id, user_id, duration))
    if ok:
        log(client, "info", f"已在群 {room_id} 禁言新成员 {user_id} {duration} 秒")
    else:
        log(client, "warn", f"在群 {room_id} 禁言新成员 {user_id} 失败")
