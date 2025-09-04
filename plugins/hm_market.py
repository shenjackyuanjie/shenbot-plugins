from __future__ import annotations

from typing import TYPE_CHECKING
from shenbot_api import PluginManifest, ConfigStorage

import re

import requests


if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient

_version_ = "0.2.0"

API_URL: str

cfg = ConfigStorage(
    api_url = "http://192.168.3.46:5110"
)


PLUGIN_MANIFEST = PluginManifest(
    plugin_id="hm_market",
    name="鸿蒙应用信息查询",
    version=_version_,
    description="查询 鸿蒙 NEXT 某个应用的下载量",
    authors=["shenjack"],
    config={"main": cfg}
)


def on_load():
    global API_URL
    API_URL = str(PLUGIN_MANIFEST.config_unchecked("main").get_value("api_url")) or ""
