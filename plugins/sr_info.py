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
        data_type = "飞船" if ship["save_type"] == "ship" else "存档"
        d_type_str = f"类型: {data_type}"
    else:
        d_type_str = ""
    return f"ID: {ship['save_id']}\n{d_type_str}\n数据长度: {format_data_size(ship['len'])}\nblake3 hash: {ship['blake_hash']}"


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
