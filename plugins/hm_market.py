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

MARKET_PREFIX = "https://appgallery.huawei.com/app/detail?id="

def reqeust_info(name: str) -> dict | None:
    try:
        data = requests.get(f"{API_URL}/{name}")
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
    cache.write(f"名称: {data['info']['name']}[{data['metric']['version']}] 类型: {data["info"]["kind_name"]}-{data['info']['kind_type_name']}\n")
    cache.write(f"下载量: {data['metric']['download_count']} 评分: {data['metric']['info_score']}({data['metric']['info_rate_count']}) 显示评分: {data['metric']['page_average_rating']}")
    cache.write(f"({data['metric']['page_star_1_rating_count']},{data['metric']['page_star_2_rating_count']},{data['metric']['page_star_3_rating_count']},")
    cache.write(f"{data['metric']['page_star_4_rating_count']},{data['metric']['page_star_5_rating_count']}={data['metric']['page_total_star_rating_count']})\n")
    cache.write(f"目标sdk: {data['metric']['target_sdk']} 最小sdk: {data['metric']['minsdk']} 应用版本代码: {data['metric']['version_code']}\n")
    release_date = datetime.datetime.fromtimestamp(data['metric']['release_date'] / 1000.0)
    cache.write(f"应用更新日期: {release_date.strftime('%Y-%m-%d %H:%M:%S')}")
    return cache.getvalue()

def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    if msg.content.startswith(MARKET_PREFIX):
        pkg_end = msg.content.find("&", len(MARKET_PREFIX))
        pkg_name = msg.content[len(MARKET_PREFIX):pkg_end]
        print(f"获取到新的链接: {pkg_name}")
        data = reqeust_info(pkg_name)
        if data is not None:
            if data['is_new']:
                reply = msg.reply_with(f"获取到新的包名: {pkg_name}\n信息: {format_data(data)}")
            else:
                reply = msg.reply_with(f"包名: {pkg_name} 已存在\n信息: {format_data(data)}")
        else:
            reply = msg.reply_with(f"获取到新的包名: {pkg_name}, 但是数据是空的")
        client.send_message(reply)
        ...

def on_load():
    global API_URL
    API_URL = str(PLUGIN_MANIFEST.config_unchecked("main").get_value("api_url")) or ""
