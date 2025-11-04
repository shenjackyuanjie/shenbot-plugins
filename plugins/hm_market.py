from __future__ import annotations

import io
import re
import datetime
import traceback

from typing import TYPE_CHECKING
from shenbot_api import PluginManifest, ConfigStorage

import requests

if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient

_version_ = "0.8.4"

API_URL: str

cfg = ConfigStorage(
    api_url = "localhost:3000"
)


PLUGIN_MANIFEST = PluginManifest(
    plugin_id="hm_market",
    name="鸿蒙应用信息查询",
    version=_version_,
    description="查询 鸿蒙 NEXT 某个应用的下载量 和一些其他查询",
    authors=["shenjack"],
    config={"main": cfg}
)

HELP_MSG = f"""鸿蒙应用市场信息查询-v{_version_}:
/hm pkg <包名>
/hm id <应用ID>
/hm info 获取应用市场当前数据
/hm rank 获取应用市场下载量排名
/hm down rank 获取近一天的下载量增量排行
/hm substance <专题ID>
或者直接发送应用市场链接/应用市场专题链接"""

MARKET_PREFIX = "https://appgallery.huawei.com/app/detail?id="
SUBSTANCE_PREFIX = "https://appgallery.huawei.com/substance/detail?id="
GAME_PREFIX = "https://game.cloud.huawei.com/gc/link/detail?id="

# https://appgallery.huawei.com/app/detail?id=com.bzl.bosszhipin&channelId=SHARE&source=appshare
# -> com.bzl.bosszshipin
# https://appgallery.huawei.com/substance/detail?id=8ef9cb813bf94143b549f5865f12acee&v=1460400000&source=substanceshare
# -> 8ef9cb813bf94143b549f5865f12acee


def format_number(number: int | str) -> str:
    """
    将数字四位一分割格式化

    Args:
        number: 要格式化的数字，可以是整数或字符串

    Returns:
        四位一分割后的字符串
    """
    num_str = str(number)
    result = []

    # 从右往左每4位分割一次
    for i in range(len(num_str), 0, -4):
        start = max(0, i - 4)
        result.append(num_str[start:i])

    # 反转结果并用逗号连接
    return ','.join(reversed(result))

def get_id_from_link(link: str) -> str:
    """
    从给定的链接中提取包名。

    Args:
        link: 包含包名的链接字符串。

    Returns:
        提取到的包名字符串。如果未找到，则返回空字符串。
    """
    if not link:
        return ""

    # 正则表达式 (?:<=id=)[\w\.]+
    # (?<=id=) 是一个后行断言，它会查找 "id="，但不会把它包含在匹配结果中。
    # [\w\.]+ 匹配一个或多个字母、数字、下划线或点。
    regex = r"(?<=id=)[a-zA-Z0-9_\.]+"  # 更精确的匹配 [\w\.] 等同于 [a-zA-Z0-9_\.]
    match = re.search(regex, link)

    if match:
        return match.group(0)  # group(0) 返回整个匹配的字符串
    else:
        return ""

def reqeust_info(name: str, method: str, sender_name: str) -> dict | None:
    try:
        # data = requests.get(f"{API_URL}/api/v0/apps/{method}/{name}")
        send_data = {
            method: name,
            "comment": {"user": sender_name, "platform": f"shenbot-{_version_}"}
        }
        data = requests.post(f"{API_URL}/api/v0/submit", json=send_data)
        json_data = data.json()
        if not json_data['success']:
            return None
        return json_data
    except requests.RequestException as e:
        print(f"yeeeee {e}")
        return None

def request_substance(substance_id: str, sender_name: str) -> dict | None:
    try:
        send_data = {
            "comment": {"user": sender_name, "platform": f"shenbot-{_version_}"}
        }
        data = requests.post(f"{API_URL}/api/v0/submit_substance/{substance_id}", json=send_data)
        json_data = data.json()
        if not json_data['success']:
            return None
        return json_data
    except requests.RequestException as e:
        print(f"yeeeee {e}")
        return None

def format_substance(data: dict) -> str:
    cache = io.StringIO()
    full_len = data['total']
    is_new = data['data']['is_new']
    data = data['data']['data']
    if is_new:
        _ = cache.write("新专题!\n")
    _ = cache.write(f"获取到专题: 共{full_len}个应用\n")
    _ = cache.write(f"{data['title']}")
    if 'subtitle' in data:
        _ = cache.write(f" - {data['subtitle']}")
    if 'name' in data:
        _ = cache.write(f"\n{data['name']}")
    _ = cache.write("\n")
    _ = cache.write(f"专题ID: {data['id']}")

    return cache.getvalue()

def format_data(data: dict) -> str:
    cache = io.StringIO()
    if not data['success']:
        del cache
        return f"报错了 {data['data']['error']}"
    data = data['data']
    if data['new_app']:
        _ = cache.write("新app!\n")
    else:
        if data['new_info']:
            _ = cache.write("更新应用信息 ")
        if data['new_metric']:
            _ = cache.write("更新下载量之类的 ")
        if data['new_rating']:
            _ = cache.write("更新评分信息 ")
        if not data['new_info'] and not data['new_metric'] and not data['new_rating']:
            _ = cache.write("应用信息无更新")
            _ = cache.write("\n")
    app = data['full_info']
    _ = cache.write(f"包名: {app['pkg_name']} app_id: {app['app_id']}\n")
    _ = cache.write(f"名称: {app['name']}[{app['version']}] 类型: {app["kind_name"]}-{app['kind_type_name']}\n")
    _ = cache.write(f"下载量: {format_number(app['download_count'])} 评分: {app['info_score']}({app['info_rate_count']}) ")
    if 'average_rating' in app and app['average_rating'] is not None:
        _ = cache.write(f"显示评分: {app['average_rating']}[{app['total_star_rating_count']}]")
        _ = cache.write(f"({app['star_1_rating_count']},{app['star_2_rating_count']},{app['star_3_rating_count']},")
        _ = cache.write(f"{app['star_4_rating_count']},{app['star_5_rating_count']})\n")
    else:
        _ = cache.write("无评分卡片数据\n")
    _ = cache.write(f"目标sdk: {app['target_sdk']} 最小sdk: {app['minsdk']} 应用版本代码: {app['version_code']}\n")
    release_date = datetime.datetime.fromtimestamp(app['release_date'] / 1000.0)
    _ = cache.write(f"应用更新日期: {release_date.strftime('%Y-%m-%d %H:%M:%S')}")
    return cache.getvalue()

def map_sender(name: str) -> str:
    if name == "You":
        return "shenjack"
    return name

def query_substance(msg: IcaNewMessage, client: IcaClient, substance_id: str) -> None:
    data = request_substance(substance_id, map_sender(msg.sender_name))
    if data is not None:
        try:
            reply = msg.reply_with(format_substance(data))
        except Exception as e:
            reply = msg.reply_with(f"格式化数据时发生错误: {e}")
            traceback.print_exc()
    else:
        reply = msg.reply_with(f"获取到新的专题ID: {substance_id}, 但是数据是空的")
    _ = client.send_message(reply)

def query_pkg(msg: IcaNewMessage, client: IcaClient, pkg_name: str, method: str) -> None:
    data = reqeust_info(pkg_name, method, map_sender(msg.sender_name))
    if data is not None:
        try:
            reply = msg.reply_with(format_data(data))
        except Exception as e:
            reply = msg.reply_with(f"格式化数据时发生错误: {e}")
            print(f"raw data: {data}")
    else:
        reply = msg.reply_with(f"获取到新的包名: {pkg_name}, 但是数据是空的")
    _ = client.send_message(reply)

def api_helper(method: str):
    try:
        data = requests.get(f"{API_URL}/api/v0/{method}")
        json_data = data.json()
        if "error" in json_data or not json_data['success']:
            return None
        return json_data
    except requests.RequestException as e:
        print(f"yeeeee {e}")
        return None

def fmt_info(show_sync: bool = False) -> str:
    market_data = api_helper("market_info")
    star_data = api_helper("charts/rating")
    if market_data is not None and star_data is not None:
        market_data = market_data['data']
        star_data = star_data['data']
        cache = io.StringIO()
        _ = cache.write(f"鸿蒙next应用市场数据-{market_data['crate_version']}\n")
        _ = cache.write(f"爬取应用/元服务:{market_data['app_count']['total']}, 应用: {market_data['app_count']['apps']} 元服务: {market_data['app_count']['atomic_services']}\n")
        _ = cache.write(f"已知开发者数量: {market_data['developer_count']}\n")
        if show_sync:
            _ = cache.write("同步状态\n")
            sync_statue = market_data['sync_status']
            cost_time = datetime.timedelta(seconds=sync_statue['elapsed_time']['secs'])
            if sync_statue['is_syncing_all']:
                _ = cache.write(f"同步中 {sync_statue['progress'][0]}/{sync_statue['progress'][1]}({sync_statue['progress'][0] / sync_statue['progress'][1] * 100}%)\n")
                # 把可能 > 60 的秒数格式化成正常的时间
                _ = cache.write(f"已经用时: {cost_time} ")
                estimated_total_time = datetime.timedelta(seconds=sync_statue['estimated_total_time']['secs'])
                _ = cache.write(f"预计总时间: {estimated_total_time}\n")
            else:
                next_sync = datetime.timedelta(seconds=sync_statue['next_sync_countdown']['secs'])
                _ = cache.write(f"上次同步用时: {cost_time}\n")
                _ = cache.write(f"下次同步倒计时: {next_sync}\n")
            _ = cache.write("总 处理/插入/失败/跳过\n")
            _ = cache.write(f"{sync_statue['total_processed']}|{sync_statue['total_inserted']}|{sync_statue['total_failed']}|{sync_statue['total_skipped']}\n")

        _ = cache.write("应用评分计数:\n")
        _ = cache.write(f"无评分: {star_data['star_1']}|")
        _ = cache.write(f"1-2分: {star_data['star_2']}|")
        _ = cache.write(f"2-3分: {star_data['star_3']}|")
        _ = cache.write(f"3-4分: {star_data['star_4']}|")
        _ = cache.write(f"4-5分: {star_data['star_5']}")
        return cache.getvalue()
    else:
        return "获取应用市场数据, 但是数据是空的"

def query_info(msg: IcaNewMessage, client: IcaClient) -> None:
    _ = client.send_message(msg.reply_with(fmt_info(True)))

def query_rank(msg: IcaNewMessage, client: IcaClient) -> None:
    cache = io.StringIO()
    _ = cache.write(fmt_info())
    _ = cache.write("\n")
    _ = cache.write("===所有应用的下载量排行===\n")
    top_down_info = api_helper("rankings/top-downloads?limit=5")
    if top_down_info is not None:
        top_down_data = top_down_info['data']
        for idx, app in enumerate(top_down_data):
            release_date = datetime.datetime.fromtimestamp(app['release_date'] / 1000.0)
            _ = cache.write(f"({idx + 1}) {app['name']} {app['kind_name']}-{app['kind_type_name']}\n")
            _ = cache.write(f"下载量: {format_number(app['download_count'])}\n")
            _ = cache.write(f"应用更新日期: {release_date.strftime('%Y-%m-%d %H:%M:%S')}\n")
    else:
        _ = cache.write("获取应用市场数据, 但是数据是空的")
    _ = cache.write("===不包含华为内置应用的下载量排行===\n")
    top_down_info = api_helper("rankings/top-downloads?limit=5&exclude_pattern=huawei")
    if top_down_info is not None:
        top_down_data = top_down_info['data']
        for idx, app in enumerate(top_down_data):
            release_date = datetime.datetime.fromtimestamp(app['release_date'] / 1000.0)
            _ = cache.write(f"({idx + 1}) {app['name']} {app['kind_name']}-{app['kind_type_name']}\n")
            _ = cache.write(f"下载量: {format_number(app['download_count'])}\n")
            _ = cache.write(f"应用更新日期: {release_date.strftime('%Y-%m-%d %H:%M:%S')}\n")
    else:
        _ = cache.write("获取应用市场数据, 但是数据是空的")
    reply = msg.reply_with(cache.getvalue()).remove_reply()
    _ = client.send_message(reply)

def query_down_rank(msg: IcaNewMessage, client: IcaClient) -> None:
    data = api_helper("rankings/download_increase?limit=10")
    cache = io.StringIO()
    if data is not None:
        _ = cache.write("===近一天下载量增量排行前十===\n")
        _ = cache.write("昨天 + 增量 = 今天\n")
        data = data['data']
        for idx, app in enumerate(data):
            _ = cache.write(f"({idx + 1}) {app['name']}\n")
            _ = cache.write(f" {format_number(app['prior_download_count'])}+")
            _ = cache.write(format_number(app['download_increment']))
            _ = cache.write(f"={format_number(app['current_download_count'])}\n")
    data = api_helper("rankings/download_increase?limit=10&days=7")
    if data is not None:
        _ = cache.write("===近一周下载量增量排行前十===\n")
        _ = cache.write("上周 + 增量 = 今天\n")
        data = data['data']
        for idx, app in enumerate(data):
            _ = cache.write(f"({idx + 1}) {app['name']}\n")
            _ = cache.write(f"{format_number(app['prior_download_count'])}+")
            _ = cache.write(format_number(app['download_increment']))
            _ = cache.write(f"={format_number(app['current_download_count'])}\n")
        reply = msg.reply_with(cache.getvalue()).remove_reply()
        _ = client.send_message(reply)

def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    if msg.content.startswith(MARKET_PREFIX):
        # 支持多行
        lines = msg.content.splitlines()
        for line in lines:
            if line.startswith(MARKET_PREFIX):
                pkg_name = get_id_from_link(line)
                print(f"获取到新的链接: {pkg_name}")
                query_pkg(msg, client, pkg_name, "pkg_name")
    elif msg.content.startswith(SUBSTANCE_PREFIX):
        substance_id = get_id_from_link(msg.content)
        print(f"获取到新的专题链接: {substance_id}")
        query_substance(msg, client, substance_id)
    elif msg.content.startswith(GAME_PREFIX):
        game_id = get_id_from_link(msg.content)
        print(f"获取到新的游戏链接: {game_id}")
        query_pkg(msg, client, game_id, "app_id")

    elif msg.content.startswith("/hm pkg "):
        pkg_name = msg.content[len("/hm pkg "):]
        print(f"获取到新的链接: {pkg_name}")
        query_pkg(msg, client, pkg_name, "pkg_name")
    elif msg.content.startswith("/hm id "):
        pkg_name = msg.content[len("/hm id "):]
        print(f"获取到新的链接: {pkg_name}")
        query_pkg(msg, client, pkg_name, "app_id")
    elif msg.content.startswith("/hm substance "):
        substance_id = msg.content[len("/hm substance "):]
        print(f"获取到新的专题链接: {substance_id}")
        query_substance(msg, client, substance_id)

    elif msg.content == "/hm info":
        query_info(msg, client)
    elif msg.content == "/hm rank":
        query_rank(msg, client)
    elif msg.content == "/hm down rank":
        query_down_rank(msg, client)

    elif msg.content.startswith("/hm"):
        # help msg
        reply = msg.reply_with(HELP_MSG)
        client.send_message(reply)

def on_load():
    global API_URL
    API_URL = str(PLUGIN_MANIFEST.config_unchecked("main").get_value("api_url")) or ""
