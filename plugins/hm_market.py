from __future__ import annotations

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

def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    if msg.content.startswith(MARKET_PREFIX):
        pkg_end = msg.content.find("&", len(MARKET_PREFIX))
        pkg_name = msg.content[len(MARKET_PREFIX)-1]
        print(f"获取到新的链接: {msg.content[len(MARKET_PREFIX)-1:]}")
        reply = msg.reply_with(f"获取到新的链接: {msg.content[len(MARKET_PREFIX)-1:]}")
        client.send_message(reply)
        ...

def on_load():
    global API_URL
    API_URL = str(PLUGIN_MANIFEST.config_unchecked("main").get_value("api_url")) or ""
