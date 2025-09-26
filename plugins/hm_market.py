from __future__ import annotations

import io
import datetime

from typing import TYPE_CHECKING
from shenbot_api import PluginManifest, ConfigStorage

import requests

if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient

_version_ = "0.1.0"

API_URL: str

cfg = ConfigStorage(
    api_url = "localhost:3000"
)


PLUGIN_MANIFEST = PluginManifest(
    plugin_id="hm_market",
    name="鸿蒙应用信息查询",
    version=_version_,
    description="查询 鸿蒙 NEXT 某个应用的下载量",
    authors=["shenjack"],
    config={"main": cfg}
)

HELP_MSG = """用法:
/hm pkg 包名
/hm app id 应用ID
/hm info 获取应用市场当前数据
或者直接发送应用市场链接"""

MARKET_PREFIX = "https://appgallery.huawei.com/app/detail?id="

def reqeust_info(name: str, method: str) -> dict | None:
    try:
        data = requests.get(f"{API_URL}/api/apps/{method}/{name}")
        json_data = data.json()
        if "error" in json_data:
            return None
        return json_data
    except requests.RequestException as e:
        print(f"yeeeee {e}")
        return None

# https://appgallery.huawei.com/app/detail?id=com.bzl.bosszhipin&channelId=SHARE&source=appshare
# -> com.bzl.bosszshipin

def format_data(data: dict) -> str:
    cache = io.StringIO()
    data = data['data']
    if data['is_new'][0]:
        cache.write("更新应用基础信息 ")
    if data['is_new'][1]:
        _ = cache.write("更新应用评分信息 ")
    if not data['is_new'][0] and not data['is_new'][1]:
        _ = cache.write("应用信息无更新 ")
    _ = cache.write(f"包名: {data['info']['pkg_name']}\n")
    _ = cache.write(f"名称: {data['info']['name']}[{data['metric']['version']}] 类型: {data["info"]["kind_name"]}-{data['info']['kind_type_name']}\n")
    _ = cache.write(f"下载量: {data['metric']['download_count']} 评分: {data['metric']['info_score']}({data['metric']['info_rate_count']}) ")
    if 'rating' in data and data['rating'] is not None:
        rate = data['rating']
        _ = cache.write(f"显示评分: {data['rating']['average_rating']}[{rate['total_star_rating_count']}]")
        _ = cache.write(f"({rate['star_1_rating_count']},{rate['star_2_rating_count']},{rate['star_3_rating_count']},")
        _ = cache.write(f"{rate['star_4_rating_count']},{rate['star_5_rating_count']})\n")
    else:
        _ = cache.write("无评分卡片数据\n")
    _ = cache.write(f"目标sdk: {data['metric']['target_sdk']} 最小sdk: {data['metric']['minsdk']} 应用版本代码: {data['metric']['version_code']}\n")
    release_date = datetime.datetime.fromtimestamp(data['metric']['release_date'] / 1000.0)
    _ = cache.write(f"应用更新日期: {release_date.strftime('%Y-%m-%d %H:%M:%S')}")
    return cache.getvalue()

def query_pkg(msg: IcaNewMessage, client: IcaClient, pkg_name: str, method: str) -> None:
    data = reqeust_info(pkg_name, method)
    if data is not None:
        reply = msg.reply_with(format_data(data))
    else:
        reply = msg.reply_with(f"获取到新的包名: {pkg_name}, 但是数据是空的")
    _ = client.send_message(reply)

def query_info(msg: IcaNewMessage, client: IcaClient) -> None:
    def helper(method: str):
        try:
            data = requests.get(f"{API_URL}/api/{method}")
            json_data = data.json()
            if "error" in json_data:
                return None
            return json_data
        except requests.RequestException as e:
            print(f"yeeeee {e}")
            return None
    market_data = helper("market_info")
    star_data = helper("charts/star-distribution")
    if market_data is not None and star_data is not None:
        market_data = market_data['data']
        star_data = star_data['data']
        cache = io.StringIO()
        _ = cache.write("鸿蒙next应用市场数据:\n")
        _ = cache.write(f"爬取应用/元服务:{market_data['full_count']}, 应用: {market_data['app_count']} 元服务: {market_data['atomic_services_count']}\n")
        _ = cache.write(f"已知开发者数量: {market_data['developer_count']}\n")
        _ = cache.write("应用评分计数:\n")
        _ = cache.write(f"0-1分: {star_data['star_1']}|")
        _ = cache.write(f"1-2分: {star_data['star_2']}|")
        _ = cache.write(f"2-3分: {star_data['star_3']}|")
        _ = cache.write(f"3-4分: {star_data['star_4']}|")
        _ = cache.write(f"4-5分: {star_data['star_5']}\n")
        reply = msg.reply_with(cache.getvalue())
    else:
        reply = msg.reply_with("获取应用市场数据, 但是数据是空的")
    _ = client.send_message(reply)

def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    if msg.content.startswith(MARKET_PREFIX):
        pkg_end = msg.content.find("&", len(MARKET_PREFIX))
        pkg_name = msg.content[len(MARKET_PREFIX):pkg_end]
        print(f"获取到新的链接: {pkg_name}")
        query_pkg(msg, client, pkg_name, "pkg_name")
    elif msg.content.startswith("/hm pkg "):
        pkg_name = msg.content[len("/hm pkg "):]
        print(f"获取到新的链接: {pkg_name}")
        query_pkg(msg, client, pkg_name, "pkg_name")
    elif msg.content.startswith("/hm app id "):
        pkg_name = msg.content[len("/hm app id "):]
        print(f"获取到新的链接: {pkg_name}")
        query_pkg(msg, client, pkg_name, "app_id")
    elif msg.content.startswith("/hm info"):
        query_info(msg, client)
    elif msg.content.startswith("/hm"):
        # help msg
        reply = msg.reply_with(HELP_MSG)
        client.send_message(reply)

def on_load():
    global API_URL
    API_URL = str(PLUGIN_MANIFEST.config_unchecked("main").get_value("api_url")) or ""
