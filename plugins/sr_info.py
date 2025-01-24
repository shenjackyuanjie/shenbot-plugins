from __future__ import annotations

from typing import TYPE_CHECKING

import re

import requests


if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient


API_URL = "http://192.168.3.22:10002"

CMD_PREFIX = "/sr"
LAST_CMD = f"{CMD_PREFIX} last"
LAST_SHIP_CMD = f"{CMD_PREFIX} last ship"
LAST_SAVE_CMD = f"{CMD_PREFIX} last save"
INFO_CMD = f"{CMD_PREFIX} info" # info xxxxx(int)

# http://jundroo.com/ViewShip.html?id=1323466
SHIP_URL_PREFIX = "http://jundroo.com/ViewShip.html?id="

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
        return f"请求中出现问题: {e}"

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


NUM_PATTERN = re.compile(r'(\d+)')


def parse_url(url: str) -> int | None:
    """解析 url
    找到连续的前几个数字

    Args:
        url (str): 123123xxxxxxx

    Returns:
        int | None: 解析完的 id
    """
    match = re.match(r'(\d+)', url)
    if match:
        return int(match.group(1))
    return None


def handle_command(msg: IcaNewMessage, client: IcaClient) -> None:
    if msg.content == LAST_SHIP_CMD:
        client.send_message(msg.reply_with(last_data("ship")))
    elif msg.content == LAST_SAVE_CMD:
        client.send_message(msg.reply_with(last_data("save")))
    elif msg.content == LAST_CMD:
        client.send_message(msg.reply_with(last_data("data")))
    elif msg.content.startswith(INFO_CMD):
        get_ship_info(msg, client)


def handle_url(msg: IcaNewMessage, client: IcaClient) -> None:
    # 找到所有的飞船链接
    urls = msg.content.split(SHIP_URL_PREFIX)
    # 因为已经判断过有了, 所以 len >= 2
    urls.pop(0)
    vaild_urls = [url for url in map(parse_url, urls) if url is not None]
    # 去重
    vaild_urls = list(set(vaild_urls))
    if len(vaild_urls) == 0:
        return
    get_infos = []
    for ship_id in vaild_urls:
        try:
            res = requests.get(f"{API_URL}/info/{ship_id}", timeout=5)
            data = res.json()
        except (requests.RequestException, requests.Timeout) as e:
            get_infos.append(f"请求中出现问题: {e} {res}")            
        if data["code"] != 200:
            get_infos.append(f"请求失败: {data['msg']}")
        print(fmt_msg)
        fmt_msg = f"ID: {data['data']['save_id']}-长度: {format_data_size(data['data']['len'])}\nblake3 hash: {data['data']['blake_hash']}"
        get_infos.append(fmt_msg)
    client.send_message(msg.reply_with("\n\n".join(get_infos)))
        

def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    if msg.is_from_self or not msg.is_room_msg:
        return
    
    if msg.content.startswith(CMD_PREFIX):
        return handle_command(msg, client)

    if SHIP_URL_PREFIX in msg.content:
        handle_url(msg, client)
