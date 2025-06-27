from __future__ import annotations
import io
import time
import tomli
import requests
import traceback

# import PIL

from typing import TYPE_CHECKING, TypeVar, Optional, Tuple, List

if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient, TailchatClient
else:
    IcaNewMessage = TypeVar("NewMessage")
    IcaClient = TypeVar("IcaClient")


COOKIE = None

_version_ = "2.9.1-rs"
backend_version = "unknown"

from shenbot_api import PluginManifest

PLUGIN_MANIFEST = PluginManifest(
    plugin_id="bmcl",
    name="openbmclapi查询",
    version=_version_,
    description="查询 openbmclapi 的各项数据",
    authors=[
        "shenjack"
    ]
)


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


def format_hit_count(count: int) -> str:
    """数据分段, 四位一个下划线

    Args:
        count (int): 数据

    Returns:
        str: 格式化后的数据
    1 -> 1
    1000 -> 1000
    10000 -> 1_0000
    100000 -> 10_0000
    1000000 -> 100_0000
    """
    count_str = str(count)
    count_len = len(count_str)
    if count_len <= 4:
        return count_str
    else:
        # 先倒序
        # 再插入
        # 最后再倒序
        count_str = count_str[::-1]
        count_str = "_".join([count_str[i : i + 4] for i in range(0, count_len, 4)])
        count_str = count_str[::-1]
        return count_str


def wrap_request(
    url: str, msg: IcaNewMessage, client: IcaClient | TailchatClient
) -> Optional[dict]:
    try:
        if COOKIE is None:
            response = requests.get(url)
        else:
            # print(f"cookie: |{COOKIE}|")
            response = requests.get(url, cookies={"openbmclapi-jwt": COOKIE})
    except requests.exceptions.RequestException:
        warn_msg = f"数据请求失败, 请检查网络\n{traceback.format_exc()}"
        reply = msg.reply_with(warn_msg)
        client.send_and_warn(reply)  # pyright: ignore reportArgumentType
        return None
    except Exception as _:
        warn_msg = f"数据请求中发生未知错误, 请呼叫 shenjack\n{traceback.format_exc()}"
        reply = msg.reply_with(warn_msg)
        client.send_and_warn(reply)  # pyright: ignore reportArgumentType
        return None
    if not response.status_code == 200 or response.reason != "OK":
        warn_msg = f"请求失败, 请检查网络\n{response.status_code} {response.reason}"
        reply = msg.reply_with(warn_msg)
        client.send_and_warn(reply)  # pyright: ignore reportArgumentType
        return None
    return response.json()


def bmcl_dashboard(msg: IcaNewMessage, client: IcaClient | TailchatClient) -> None:
    req_time = time.time()
    # 记录请求时间
    data = wrap_request(
        "https://bd.bangbang93.com/openbmclapi/metric/dashboard", msg, client
    )
    dashboard_status = wrap_request(
        "https://bd.bangbang93.com/openbmclapi/metric/version", msg, client
    )
    if data is None or dashboard_status is None:
        return
    global backend_version
    backend_version = dashboard_status["version"]
    backend_commit = dashboard_status["_resolved"].split("#")[1][:7]
    data_bytes: float = data["bytes"]
    data_hits: int = data["hits"]
    data_bandwidth: float = data["currentBandwidth"]
    load_str: float = data["load"] * 100
    online_node: int = data["currentNodes"]
    online_bandwidth: int = data["bandwidth"]
    data_len = format_data_size(data_bytes)
    hits_count = format_hit_count(data_hits)

    report_msg = (
        f"OpenBMCLAPI 面板v{_version_}-状态\n"
        f"api版本 {backend_version} commit:{backend_commit}\n"
        f"实时信息: {online_node}  理论可用带宽: {online_bandwidth}Mbps\n"
        f"负载: {load_str:.2f}% 实际带宽: {data_bandwidth:.2f}Mbps\n"
        f"当日请求: {hits_count} 数据量: {data_len}\n"
        f"请求时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(req_time))}\n"
        "数据源: https://bd.bangbang93.com/pages/dashboard"
    )
    client.debug(report_msg)
    reply = msg.reply_with(report_msg)
    client.send_message(reply)  # pyright: ignore reportArgumentType


def check_is_full_data(data: list) -> bool:
    return "user" in data[0]


def display_rank_min(ranks: list, req_time) -> str:
    cache = io.StringIO()
    cache.write(f"bmclapi v{_version_}-排名({len(ranks)})")
    if check_is_full_data(ranks):
        cache.write("完整\n")
        for rank in ranks:
            cache.write("✅" if rank["isEnabled"] else "❌")
            if "fullSize" in rank:
                cache.write("🌕" if rank["fullSize"] else "🌘")
            if "version" in rank:
                cache.write("🟢" if rank["version"] == backend_version else "🟠")
            cache.write(f"-{rank['index']+1:3}")
            cache.write(f"|{rank['name']}\n")
    else:
        cache.write("无cookie\n")
        for rank in ranks:
            cache.write("✅" if rank["isEnabled"] else "❌")
            cache.write(f"-{rank['index']+1:3}")
            cache.write(f"|{rank['name']}\n")
    cache.write(
        f"请求时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(req_time))}"
    )
    return cache.getvalue()


def display_rank_full(ranks: list, req_time) -> str:
    cache = io.StringIO()
    cache.write(f"bmclapi v{_version_}-排名({len(ranks)})")
    if check_is_full_data(ranks):
        cache.write("完整\n")
        for rank in ranks:
            # 基本信息
            cache.write("✅" if rank["isEnabled"] else "❌")
            if "fullSize" in rank:
                cache.write("🌕" if rank["fullSize"] else "🌘")
            cache.write(f"|{rank['index']+1:3}|")
            cache.write(f"{rank['name']}")
            if "version" in rank:
                cache.write(f"|{rank['version']}")
                cache.write("🟢" if rank["version"] == backend_version else "🟠")
            cache.write("\n")
            # 用户/赞助信息
            if ("user" in rank) and (rank["user"] is not None):
                cache.write(f"所有者:{rank['user']['name']}")
            if "sponsor" in rank:
                cache.write(f"|赞助者:{rank['sponsor']['name']}")
            if "sponsor" in rank or ("user" in rank and rank["user"] is not None):
                cache.write("\n")
            # 数据信息
            if "metric" in rank:
                hits = format_hit_count(rank["metric"]["hits"])
                data = format_data_size(rank["metric"]["bytes"])
                cache.write(f"hit/data|{hits}|{data}")
                cache.write("\n")
    else:
        cache.write("无cookie\n")
        for rank in ranks:
            cache.write("✅" if rank["isEnabled"] else "❌")
            cache.write(f"-{rank['index']+1:3}")
            cache.write(f"|{rank['name']}|\n")
            if "sponsor" in rank:
                cache.write(f"赞助者: {rank['sponsor']['name']}|")
            if "metric" in rank:
                cache.write(
                    f"hit/data|{format_hit_count(rank['metric']['hits'])}|{format_data_size(rank['metric']['bytes'])}\n"
                )
    cache.write(
        f"请求时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(req_time))}"
    )
    return cache.getvalue()


def bmcl_rank_general(msg, client):
    req_time = time.time()
    # 记录请求时间
    rank_data = wrap_request(
        "https://bd.bangbang93.com/openbmclapi/metric/rank", msg, client
    )
    if rank_data is None:
        return
    # 预处理数据
    for i, r in enumerate(rank_data):
        r["index"] = i
    # 显示前3名
    ranks = rank_data[:3]
    # ranks = rank_data

    # image = PIL.Image.new("RGB", (100, 100), (255, 255, 255))
    # img_cache = io.BytesIO()
    # image.save(img_cache, format="JPEG")
    # raw_img = img_cache.getvalue()
    # img_cache.close()

    reply = msg.reply_with(display_rank_full(ranks, req_time))
    # reply.set_img(raw_img, "image/jpeg", False)
    client.send_message(reply)


FULL_DISPLAY = 5
MAX_DISPLAY = 25


def bmcl_rank(
    msg: IcaNewMessage, client: IcaClient | TailchatClient, name: str
) -> None:
    req_time = time.time()
    # 记录请求时间
    rank_data = wrap_request(
        "https://bd.bangbang93.com/openbmclapi/metric/rank", msg, client
    )
    if rank_data is None:
        return
    # 预处理数据
    for i, r in enumerate(rank_data):
        r["index"] = i
    # 搜索是否有这个名字的节点
    names: List[str] = [r["name"].lower() for r in rank_data]
    # try:
    #     import regexrs
    #     pattern = regexrs.compile(name)
    #     finds = [pattern.match(n) for n in names]
    # except Exception as e:
    finds = [name.lower() in n for n in names]
    if not any(finds):
        reply = msg.reply_with("未找到指定名字的节点")
        client.send_message(reply)  # pyright: ignore reportArgumentType
        return
    # 如果找到 > 3 个节点, 则提示 不显示
    counts = [f for f in finds if f]
    ranks = [rank_data[i] for i, f in enumerate(finds) if f]
    if len(counts) > FULL_DISPLAY:
        if len(counts) > MAX_DISPLAY:
            reply = msg.reply_with(f"搜索到{len(counts)}个节点, 请用更精确的名字")
        else:
            # 4~10  个节点 只显示名称和次序
            report_msg = display_rank_min(ranks, req_time)
            reply = msg.reply_with(report_msg)
        client.send_message(reply)  # pyright: ignore reportArgumentType
        return
    # 如果找到 <= 3 个节点, 则显示全部信息
    report_msg = display_rank_full(ranks, req_time)
    client.debug(report_msg)
    reply = msg.reply_with(report_msg)
    client.send_message(reply)  # pyright: ignore reportArgumentType


# def bangbang_img(msg: IcaNewMessage, client: IcaClient) -> None:
#     data = requests.get("https://api.bangbang93.top/api/link")
#     if data.status_code != 200:
#         reply = msg.reply_with(f"请求失败 {data.status_code} {data.reason}")
#         client.send_message(reply)
#         return
#     raw_name = data.url.split("/")[-1]
#     img_suffix = raw_name.split(".")[-1]
#     # mine 映射一下
#     if img_suffix.lower() in ("jpeg", "jpg"):
#         img_suffix = "jpeg"
#     img_name = raw_name[:-len(img_suffix) - 1]
#     img_name = urllib.parse.unquote(img_name)
#     mime_format = f"image/{img_suffix}"
#     client.info(f"获取到随机怪图: {img_name} {img_suffix}")
#     reply = msg.reply_with(img_name)
#     reply.set_img(data.content, mime_format, True)
#     client.send_message(reply)


help = f"""/bmcl -> dashboard
/bmcl rank -> all rank
/bmcl rank <name> -> rank of <name>
/brrs <name> -> rank of <name>
搜索限制:
1-{FULL_DISPLAY} 显示全部信息
{FULL_DISPLAY+1}-{MAX_DISPLAY} 显示状态、名称
{MAX_DISPLAY+1}+  不显示
"""
# /bm93 -> 随机怪图


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    if not (msg.is_from_self or msg.is_reply):
        if "\n" in msg.content:
            return
        try:
            if not msg.content.startswith("/b"):
                return
            global backend_version
            if backend_version == "unknown":
                dashboard_status = wrap_request(
                    "https://bd.bangbang93.com/openbmclapi/metric/version", msg, client
                )
                if dashboard_status is None:
                    return
                backend_version = dashboard_status["version"]
            if msg.content.startswith("/bmcl"):
                if msg.content == "/bmcl":
                    bmcl_dashboard(msg, client)
                elif msg.content == "/bmcl rank":
                    bmcl_rank_general(msg, client)
                elif msg.content.startswith("/bmcl rank") and len(msg.content) > 11:
                    name = msg.content[11:]
                    bmcl_rank(msg, client, name)
                else:
                    reply = msg.reply_with(help)
                    client.send_message(reply)
            elif msg.content.startswith("/brrs"):
                if msg.content == "/brrs":
                    reply = msg.reply_with(help)
                    client.send_message(reply)
                else:
                    name = msg.content.split(" ")
                    if len(name) > 1:
                        name = name[1]
                        bmcl_rank(msg, client, name)
            # elif msg.content == "/bm93":
            #     bangbang_img(msg, client)
        except:  # noqa
            report_msg = f"bmcl插件发生错误,请呼叫shenjack\n{traceback.format_exc()}"
            client.warn(report_msg)
            if len(report_msg) > 200:
                report_msg = report_msg[:200] + "..."  # 防止消息过长
            reply = msg.reply_with(report_msg)
            client.send_and_warn(reply)


def on_tailchat_message(msg, client: TailchatClient) -> None:
    if not msg.is_reply:
        if "\n" in msg.content:
            return
        try:
            if not msg.content.startswith("/bm") or not msg.content.startswith("/brrs"):
                return
            global backend_version
            if backend_version == "unknown":
                dashboard_status = wrap_request(
                    "https://bd.bangbang93.com/openbmclapi/metric/version", msg, client
                )
                if dashboard_status is None:
                    return
                backend_version = dashboard_status["version"]
            if msg.content.startswith("/bmcl"):
                if msg.content == "/bmcl":
                    bmcl_dashboard(msg, client)
                elif msg.content == "/bmcl rank":
                    bmcl_rank_general(msg, client)
                elif msg.content.startswith("/bmcl rank") and len(msg.content) > 11:
                    name = msg.content[11:]
                    bmcl_rank(msg, client, name)
                else:
                    reply = msg.reply_with(help)
                    client.send_message(reply)
            elif msg.content.startswith("/brrs"):
                if msg.content == "/brrs":
                    reply = msg.reply_with(help)
                    client.send_message(reply)
                else:
                    name = msg.content.split(" ")
                    if len(name) > 1:
                        name = name[1]
                        bmcl_rank(msg, client, name)
            # elif msg.content == "/bm93":
            #     bangbang_img(msg, client)
        except:  # noqa
            report_msg = f"bmcl插件发生错误,请呼叫shenjack\n{traceback.format_exc()}"
            client.warn(report_msg)
            if len(report_msg) > 200:
                report_msg = report_msg[:200] + "..."  # 防止消息过长
            reply = msg.reply_with(report_msg)
            client.send_and_warn(reply)


def require_config() -> Tuple[str, str]:
    return ("bmcl.toml", """cookie = \"填写你的 cookie\"""")


def on_config(data: bytes):
    string = data.decode("utf-8")
    load_data = tomli.loads(string)
    global COOKIE
    if "cookie" in load_data:
        if load_data["cookie"] == "填写你的 cookie":
            return
        COOKIE = load_data["cookie"]
