from __future__ import annotations

from typing import TYPE_CHECKING

import requests


if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient


API_URL = "http://shenjack.top:10002"

CMD_PREFIX = "/sr"
LAST_CMD = f"{CMD_PREFIX} last"
LAST_SHIP_CMD = f"{CMD_PREFIX} last ship"
LAST_SAVE_CMD = f"{CMD_PREFIX} last save"
INFO_CMD = f"{CMD_PREFIX} info" # info xxxxx(int)


def data_type_fmt(data_type: str) -> str:
    if data_type == "ship":
        return "飞船"
    elif data_type == "save":
        return "存档"
    elif data_type == "none":
        return "无数据"
    elif data_type == "unknown":
        return "未知"
    else:
        return f"未知类型: {data_type}"


def format_data_size(data_bytes: float) -> str:
    data_lens = ["B", "KB", "MB", "GB", "TB"]
    data_len = "0B"
    for i in range(5):
        if data_bytes < 1024:
            data_bytes = round(data_bytes, 5)
            data_len = f"{data_bytes}{data_lens[i]}"
            break
        else:
            data_bytes /= 1024
    return data_len


def last_data(path: str) -> str:
    try:
        res = requests.get(f"{API_URL}/last/{path}", timeout=5)
        data = res.json()
    except (requests.RequestException, requests.Timeout) as e:
        return f"请求中出现问题: {e} {res}"

    if data["code"] != 200:
        return f"请求失败: {data['msg']}"
    
    ship = data["data"]
    if path == "data":
        d_type_str = f"类型: {data_type_fmt(ship['save_type'])}"
    else:
        d_type_str = ""
    return f"ID: {ship['save_id']}\n{d_type_str}\n数据长度: {format_data_size(ship['len'])}\nblake3 hash: {ship['blake_hash']}"


def get_ship_info(msg: IcaNewMessage, client: IcaClient):
    if len(msg.content) <= len(INFO_CMD) + 1:
        client.send_message(msg.reply_with("参数不足"))
        return
    ship_id = msg.content[len(INFO_CMD) + 1:]
    if not ship_id.isdigit():
        client.send_message(msg.reply_with("ID 必须是数字"))
        return
    try:
        res = requests.get(f"{API_URL}/info/{ship_id}", timeout=5)
        data = res.json()
    except (requests.RequestException, requests.Timeout) as e:
        client.send_and_warn(msg.reply_with(f"请求中出现问题: {e} {res}"))
        return
    
    if data["code"] != 200:
        client.send_and_warn(msg.reply_with(f"请求失败: {data['msg']}"))
        return
    
    ship = data["data"]
    formatted = f"ID: {ship['save_id']}\n类型: {data_type_fmt(ship['save_type'])}\n数据长度: {format_data_size(ship['len'])}\nblake3 hash: {ship['blake_hash']}"
    client.send_message(msg.reply_with(formatted))


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    if msg.is_from_self or not msg.is_room_msg:
        return
    
    if not msg.content.startswith(CMD_PREFIX):
        return
    
    if msg.content == LAST_SHIP_CMD:
        client.send_message(msg.reply_with(last_data("ship")))
    elif msg.content == LAST_SAVE_CMD:
        client.send_message(msg.reply_with(last_data("save")))
    elif msg.content == LAST_CMD:
        client.send_message(msg.reply_with(last_data("data")))
    elif msg.content.startswith(INFO_CMD):
        get_ship_info(msg, client)
