from __future__ import annotations

import datetime
import re
import traceback
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import requests
from shenbot_api import ConfigStorage, PluginManifest

if TYPE_CHECKING:
    from ica_typing import IcaClient, IcaNewMessage


VERSION = "0.9.2"
CMD_PREFIX = "/hm"
REQUEST_TIMEOUT = 10
USER_AGENT = f"shenbot_hm_market/{VERSION}"

API_URL: str

cfg = ConfigStorage(
    api_url="https://ddns.shenjack.top:10003",
)

PLUGIN_MANIFEST = PluginManifest(
    plugin_id="hm_market",
    name="鸿蒙应用信息查询",
    version=VERSION,
    description="查询鸿蒙 NEXT 应用信息、下载趋势和市场排行",
    authors=["shenjack"],
    config={"main": cfg},
)

HELP_MSG = f"""鸿蒙应用市场信息查询 v{VERSION}
/hm pkg <包名>          按包名查询，可继续换行输入多个
/hm id <应用ID>         按 App ID 查询，可继续换行输入多个
/hm detail pkg <包名>   查看应用完整详情
/hm detail id <应用ID>  按 App ID 查看完整详情
/hm search <关键词>     搜索名称、包名和开发者
/hm trend <包名或ID>    查看下载量历史趋势
/hm top                 累计下载量排行
/hm up                  近期下载增量排行
/hm rating              评分排行
/hm recent              最近更新排行
/hm dev                 开发者应用数量排行
/hm info                市场数据及同步状态
/hm substance <专题ID>  查询应用市场专题

多项查询示例：
/hm pkg com.example.first
com.example.second

也可直接发送应用市场应用/专题或游戏中心链接"""

MARKET_PREFIX = "https://appgallery.huawei.com/app/detail?id="
SUBSTANCE_PREFIX = "https://appgallery.huawei.com/substance/detail?id="
GAME_PREFIX = "https://game.cloud.huawei.com/gc/link/"


def _has_value(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def format_number(number: int | str) -> str:
    """按插件原有习惯，每四位添加一个分隔符。"""
    num_str = str(number)
    sign = ""
    if num_str.startswith(("-", "+")):
        sign, num_str = num_str[0], num_str[1:]
    if not num_str:
        return sign
    groups = []
    for index in range(len(num_str), 0, -4):
        groups.append(num_str[max(0, index - 4) : index])
    return sign + ",".join(reversed(groups))


def _format_download_count(value: Any) -> str:
    formatted = format_number(value)
    count = _to_int(value)
    if count is not None and count < 1000:
        return f"{formatted}(假数据)"
    return formatted


def _format_delta(value: int) -> str:
    if value > 0:
        return f"+{format_number(value)}"
    return format_number(value)


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime.datetime | None:
    if not _has_value(value):
        return None
    if isinstance(value, datetime.datetime):
        parsed = value
    elif isinstance(value, datetime.date):
        parsed = datetime.datetime.combine(value, datetime.time())
    elif isinstance(value, (int, float)):
        timestamp = float(value)
        if abs(timestamp) > 100_000_000_000:
            timestamp /= 1000
        try:
            parsed = datetime.datetime.fromtimestamp(timestamp)
        except (OSError, OverflowError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            return _parse_datetime(int(text))
        try:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.datetime.strptime(text[:10], "%Y-%m-%d")
            except ValueError:
                return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _format_datetime(value: Any, date_only: bool = False) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return str(value) if _has_value(value) else ""
    if date_only or (
        isinstance(value, str)
        and len(value.strip()) == 10
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip())
    ):
        return parsed.strftime("%Y-%m-%d")
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _format_size(value: Any) -> str:
    size = _to_int(value)
    if size is None or size < 0:
        return str(value) if _has_value(value) else ""
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(size)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    if unit == "B":
        return f"{size} B"
    return f"{amount:.2f} {unit}"


def _truncate_text(value: Any, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}……（已省略）"


def _join_values(values: list[Any], separator: str = " / ") -> str:
    return separator.join(str(value) for value in values if _has_value(value))


def _format_collection(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return _join_values(list(value), "、")
    return str(value) if _has_value(value) else ""


def _format_device_codes(value: Any) -> str:
    names = {
        "0": "手机",
        "3": "智慧屏",
        "4": "平板",
        "7": "智能手表",
        "9": "运动手表",
        "15": "电脑",
    }
    codes = value if isinstance(value, (list, tuple, set)) else [value]
    return "、".join(
        f"{names.get(str(code), '未知')}({code})"
        for code in codes
        if _has_value(code)
    )


def _extract_app(response: dict[str, Any]) -> dict[str, Any] | None:
    payload = response.get("data")
    if not isinstance(payload, dict):
        return None
    full_info = payload.get("full_info")
    if isinstance(full_info, dict):
        return full_info
    # 兼容可能直接返回完整应用对象的旧缓存响应。
    if _has_value(payload.get("pkg_name")) or _has_value(payload.get("app_id")):
        return payload
    return None


def _extract_list(response: dict[str, Any]) -> list[Any] | None:
    payload = response.get("data")
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    return None


def get_id_from_link(link: str) -> str:
    """从应用市场或游戏中心链接中提取包名/App ID。"""
    if not link:
        return ""
    match = re.search(r"(?:id=|appId=)([a-zA-Z0-9_.]+)", link)
    return match.group(1) if match else ""


def request_api(
    path: str,
    method: str = "GET",
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
) -> dict[str, Any] | None:
    """统一调用 get_market API，并屏蔽网络、JSON 与业务错误。"""
    url = f"{API_URL}/api/v0/{path.lstrip('/')}"
    try:
        kwargs: dict[str, Any] = {
            "headers": {"User-Agent": USER_AGENT},
            "timeout": REQUEST_TIMEOUT,
        }
        if params is not None:
            kwargs["params"] = params
        if method.upper() == "POST":
            if json_body is not None:
                kwargs["json"] = json_body
            response = requests.post(url, **kwargs)
        else:
            response = requests.get(url, **kwargs)
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as error:
        print(f"hm_market 请求失败: {error}")
        return None

    if not isinstance(result, dict):
        print("hm_market API 返回的 JSON 不是对象")
        return None
    if result.get("success") is not True:
        error_data = result.get("data")
        print(f"hm_market API 返回失败: {error_data}")
        return None
    if "data" not in result or result.get("data") is None:
        print("hm_market API 成功响应缺少 data")
        return None
    return result


def request_info(name: str, method: str, sender_name: str) -> dict[str, Any] | None:
    send_data = {
        method: name,
        "comment": {"user": sender_name, "platform": f"shenbot-{VERSION}"},
    }
    return request_api("submit", "POST", json_body=send_data)


def request_substance(
    substance_id: str, sender_name: str
) -> dict[str, Any] | None:
    send_data = {
        "comment": {"user": sender_name, "platform": f"shenbot-{VERSION}"},
    }
    return request_api(
        f"submit_substance/{quote(substance_id, safe='')}",
        "POST",
        json_body=send_data,
    )


def _refresh_status(payload: dict[str, Any]) -> str:
    if payload.get("new_app"):
        return "新应用"
    updates = []
    if payload.get("new_info"):
        updates.append("应用信息")
    if payload.get("new_metric"):
        updates.append("下载数据")
    if payload.get("new_rating"):
        updates.append("评分数据")
    if updates:
        return f"已刷新{'、'.join(updates)}"
    if payload.get("cached"):
        return "缓存数据"
    return "应用信息无更新"


def _format_summary_lines(response: dict[str, Any]) -> list[str]:
    payload = response.get("data")
    payload = payload if isinstance(payload, dict) else {}
    app = _extract_app(response) or {}
    lines = [f"刷新状态: {_refresh_status(payload)}"]

    name_version = _join_values([app.get("name"), app.get("version")], " ")
    if name_version:
        lines.append(f"名称/版本: {name_version}")

    category_parts = []
    category = _join_values(
        [app.get("kind_name"), app.get("kind_type_name"), app.get("tag_name")]
    )
    if category:
        category_parts.append(f"分类: {category}")
    devices = _format_device_codes(app.get("main_device_codes"))
    if devices:
        category_parts.append(f"终端类型: {devices}")
    if category_parts:
        lines.append(" | ".join(category_parts))

    identifiers = []
    if _has_value(app.get("pkg_name")):
        identifiers.append(f"包名: {app['pkg_name']}")
    if _has_value(app.get("app_id")):
        identifiers.append(f"App ID: {app['app_id']}")
    if identifiers:
        lines.append(" | ".join(identifiers))

    if _has_value(app.get("developer_name")):
        lines.append(f"开发者: {app['developer_name']}")
    if _has_value(app.get("download_count")):
        lines.append(f"下载量: {_format_download_count(app['download_count'])}")

    ratings = []
    if _has_value(app.get("info_score")):
        info_rating = f"信息评分: {app['info_score']}"
        if _has_value(app.get("info_rate_count")):
            info_rating += f"（{format_number(app['info_rate_count'])} 人）"
        ratings.append(info_rating)
    if _has_value(app.get("average_rating")):
        card_rating = f"显示评分: {app['average_rating']}"
        if _has_value(app.get("total_star_rating_count")):
            card_rating += (
                f"（{format_number(app['total_star_rating_count'])} 人）"
            )
        ratings.append(card_rating)
    if ratings:
        lines.append(" | ".join(ratings))

    if _has_value(app.get("size_bytes")):
        lines.append(f"大小: {_format_size(app['size_bytes'])}")

    sdk_values = []
    if _has_value(app.get("target_sdk")):
        sdk_values.append(f"目标 SDK {app['target_sdk']}")
    if _has_value(app.get("minsdk")):
        sdk_values.append(f"最低 SDK {app['minsdk']}")
    if sdk_values:
        lines.append("SDK: " + " | ".join(sdk_values))

    if _has_value(app.get("release_date")):
        lines.append(f"更新时间: {_format_datetime(app['release_date'])}")
    return lines


def format_data(response: dict[str, Any], detailed: bool = False) -> str:
    app = _extract_app(response)
    if app is None:
        return "应用数据为空"

    lines = _format_summary_lines(response)
    if not detailed:
        return "\n".join(lines)

    lines.append("")
    lines.append("详细信息")
    if _has_value(app.get("supplier")):
        lines.append(f"供应商: {app['supplier']}")

    commercial = []
    if _has_value(app.get("price")):
        commercial.append(f"价格 {app['price']}")
    if _has_value(app.get("tariff_type")):
        commercial.append(f"资费 {app['tariff_type']}")
    if app.get("iap") is not None:
        commercial.append(f"IAP {'是' if app.get('iap') else '否'}")
    if app.get("hms") is not None:
        commercial.append(f"HMS {'是' if app.get('hms') else '否'}")
    if commercial:
        lines.append("商业信息: " + " | ".join(commercial))

    sdk_details = []
    if _has_value(app.get("compile_sdk_version")):
        sdk_details.append(f"编译 SDK {app['compile_sdk_version']}")
    if _has_value(app.get("min_hmos_api_level")):
        sdk_details.append(f"最低 HarmonyOS API {app['min_hmos_api_level']}")
    if _has_value(app.get("api_release_type")):
        sdk_details.append(f"API 类型 {app['api_release_type']}")
    if sdk_details:
        lines.append("编译/API: " + " | ".join(sdk_details))

    if _has_value(app.get("version_code")):
        lines.append(f"版本代码: {app['version_code']}")
    if _has_value(app.get("main_device_codes")):
        lines.append(f"支持设备: {_format_device_codes(app['main_device_codes'])}")
    if _has_value(app.get("release_countries")):
        lines.append(f"发布地区: {_format_collection(app['release_countries'])}")

    time_fields = (
        ("上架时间", "listed_at"),
        ("应用采集时间", "created_at"),
        ("指标采集时间", "metrics_created_at"),
        ("评分采集时间", "rating_created_at"),
        ("数据更新时间", "updated_at"),
    )
    for label, key in time_fields:
        if _has_value(app.get(key)):
            lines.append(f"{label}: {_format_datetime(app[key])}")

    stars = [
        app.get("star_1_rating_count"),
        app.get("star_2_rating_count"),
        app.get("star_3_rating_count"),
        app.get("star_4_rating_count"),
        app.get("star_5_rating_count"),
    ]
    if any(_has_value(value) for value in stars):
        star_text = " / ".join(
            f"{index}★ {format_number(value)}"
            for index, value in enumerate(stars, 1)
            if _has_value(value)
        )
        lines.append(f"星级分布: {star_text}")

    record_values = []
    for key in (
        "title",
        "app_recordal_info",
        "recordal_entity_title",
        "recordal_entity_name",
    ):
        if _has_value(app.get(key)):
            record_values.append(str(app[key]))
    if record_values:
        lines.append("备案信息: " + " | ".join(record_values))
    if _has_value(app.get("privacy_url")):
        lines.append(f"隐私链接: {app['privacy_url']}")
    if _has_value(app.get("brief_desc")):
        lines.append(f"简介: {_truncate_text(app['brief_desc'])}")

    update_texts = []
    if _has_value(app.get("new_features")):
        update_texts.append(str(app["new_features"]))
    if (
        _has_value(app.get("upgrade_msg"))
        and app.get("upgrade_msg") not in update_texts
    ):
        update_texts.append(str(app["upgrade_msg"]))
    if update_texts:
        lines.append(f"更新说明: {_truncate_text(' '.join(update_texts))}")
    return "\n".join(lines)


def format_substance(response: dict[str, Any]) -> str:
    payload = response.get("data")
    if not isinstance(payload, dict):
        return "专题数据为空"
    substance = payload.get("data")
    if not isinstance(substance, dict):
        return "专题数据为空"

    lines = []
    if payload.get("is_new"):
        lines.append("新专题！")
    if _has_value(response.get("total")):
        lines.append(f"获取到专题，共 {response['total']} 个应用")
    title = _join_values([substance.get("title"), substance.get("subtitle")], " - ")
    if title:
        lines.append(title)
    if _has_value(substance.get("name")):
        lines.append(str(substance["name"]))
    if _has_value(substance.get("id")):
        lines.append(f"专题 ID: {substance['id']}")
    return "\n".join(lines) if lines else "专题数据为空"


def map_sender(name: str) -> str:
    return "shenjack" if name == "You" else name


def _send_text(msg: IcaNewMessage, client: IcaClient, text: str) -> None:
    client.send_message(msg.reply_with(text))


def query_substance(
    msg: IcaNewMessage, client: IcaClient, substance_id: str
) -> None:
    response = request_substance(substance_id, map_sender(msg.sender_name))
    if response is None:
        _send_text(msg, client, f"专题 {substance_id} 查询失败或数据为空")
        return
    try:
        _send_text(msg, client, format_substance(response))
    except Exception as error:
        traceback.print_exc()
        _send_text(msg, client, f"格式化专题数据时发生错误: {error}")


def query_pkg(
    msg: IcaNewMessage,
    client: IcaClient,
    name: str,
    method: str,
    *,
    detailed: bool = False,
) -> None:
    response = request_info(name, method, map_sender(msg.sender_name))
    if response is None:
        label = "App ID" if method == "app_id" else "包名"
        _send_text(msg, client, f"{label} {name} 查询失败或数据为空")
        return
    try:
        _send_text(msg, client, format_data(response, detailed=detailed))
    except Exception as error:
        traceback.print_exc()
        print(f"raw data: {response}")
        _send_text(msg, client, f"格式化应用数据时发生错误: {error}")


def _format_search_result(response: dict[str, Any], keyword: str) -> str:
    apps = _extract_list(response)
    if not apps:
        return f"未找到与“{keyword}”匹配的应用"
    payload = response.get("data")
    nested_total = payload.get("total_count") if isinstance(payload, dict) else None
    total = response.get("total")
    if not _has_value(total):
        total = nested_total
    if not _has_value(total):
        total = len(apps)

    lines = [f"搜索“{keyword}”：共匹配 {format_number(total)} 项，显示前 {len(apps)} 项"]
    for index, raw_app in enumerate(apps, 1):
        app = raw_app if isinstance(raw_app, dict) else {}
        lines.append(f"({index}) {app.get('name') or '未知应用'}")
        if _has_value(app.get("developer_name")):
            lines.append(f"开发者: {app['developer_name']}")
        identifiers = []
        if _has_value(app.get("pkg_name")):
            identifiers.append(f"包名 {app['pkg_name']}")
        if _has_value(app.get("app_id")):
            identifiers.append(f"App ID {app['app_id']}")
        if identifiers:
            lines.append(" | ".join(identifiers))
        metrics = []
        if _has_value(app.get("download_count")):
            metrics.append(f"下载量 {_format_download_count(app['download_count'])}")
        rating = app.get("average_rating")
        if not _has_value(rating):
            rating = app.get("info_score")
        if _has_value(rating):
            metrics.append(f"评分 {rating}")
        if metrics:
            lines.append(" | ".join(metrics))
    return "\n".join(lines)


def query_search(
    msg: IcaNewMessage, client: IcaClient, keyword: str
) -> None:
    pattern = f"%{keyword}%"
    body = {
        "or": [
            {"key": "name", "value": pattern, "op": "ilike"},
            {"key": "pkg_name", "value": pattern, "op": "ilike"},
            {"key": "developer_name", "value": pattern, "op": "ilike"},
        ]
    }
    response = request_api(
        "apps/query",
        "POST",
        params={
            "page": 1,
            "page_size": 5,
            "detail": "true",
            "sort": "download_count",
            "desc": "true",
        },
        json_body=body,
    )
    if response is None:
        _send_text(msg, client, "搜索失败，请稍后重试")
        return
    _send_text(msg, client, _format_search_result(response, keyword))


def _resolve_pkg_name(identifier: str) -> str | None:
    if not re.match(r"^C\d+", identifier, re.IGNORECASE):
        return identifier
    response = request_api(
        f"apps/app_id/{quote(identifier, safe='')}",
        params={"use_cache": "true"},
    )
    if response is None:
        return None
    app = _extract_app(response)
    if app is None or not _has_value(app.get("pkg_name")):
        return None
    return str(app["pkg_name"])


def _find_baseline(
    metrics: list[tuple[datetime.datetime, int]],
    latest_time: datetime.datetime,
    days: int,
) -> tuple[datetime.datetime, int] | None:
    cutoff = latest_time - datetime.timedelta(days=days)
    candidates = [item for item in metrics if item[0] <= cutoff]
    return candidates[-1] if candidates else None


def _format_trend(
    pkg_name: str, metrics_data: list[Any]
) -> str:
    metrics: list[tuple[datetime.datetime, int]] = []
    for raw_metric in metrics_data:
        if not isinstance(raw_metric, dict):
            continue
        created_at = _parse_datetime(raw_metric.get("created_at"))
        download_count = _to_int(raw_metric.get("download_count"))
        if created_at is not None and download_count is not None:
            metrics.append((created_at, download_count))
    metrics.sort(key=lambda item: item[0])

    if not metrics:
        return f"{pkg_name} 暂无下载量历史记录"

    latest_time, latest_count = metrics[-1]
    lines = [
        f"{pkg_name} 下载趋势",
        f"最新下载量: {_format_download_count(latest_count)}（{_format_datetime(latest_time)}）",
    ]
    if len(metrics) >= 2:
        previous_time, previous_count = metrics[-2]
        lines.append(
            f"较上一快照: {_format_delta(latest_count - previous_count)}"
            f"（{_format_datetime(previous_time)}）"
        )
    else:
        lines.append("较上一快照: 数据不足")

    for days in (7, 30):
        baseline = _find_baseline(metrics, latest_time, days)
        if baseline is None:
            lines.append(f"近 {days} 天变化: 数据不足")
        else:
            baseline_time, baseline_count = baseline
            lines.append(
                f"近 {days} 天变化: {_format_delta(latest_count - baseline_count)}"
                f"（基线 {_format_datetime(baseline_time)}）"
            )

    first_time = metrics[0][0]
    lines.append(
        f"历史覆盖: {_format_datetime(first_time)} 至 {_format_datetime(latest_time)}"
        f"，共 {len(metrics)} 条"
    )
    return "\n".join(lines)


def query_trend(
    msg: IcaNewMessage, client: IcaClient, identifier: str
) -> None:
    pkg_name = _resolve_pkg_name(identifier)
    if pkg_name is None:
        _send_text(msg, client, f"无法通过 App ID {identifier} 找到对应包名")
        return
    response = request_api(f"apps/metrics/{quote(pkg_name, safe='')}")
    if response is None:
        _send_text(msg, client, f"{pkg_name} 下载趋势查询失败")
        return
    metrics = _extract_list(response)
    if metrics is None:
        _send_text(msg, client, f"{pkg_name} 下载趋势数据为空")
        return
    _send_text(msg, client, _format_trend(pkg_name, metrics))


def fmt_info(show_sync: bool = False) -> str:
    market_response = request_api("market_info")
    star_response = request_api("charts/rating", "POST")
    if market_response is None or star_response is None:
        return "获取应用市场数据失败或数据为空"
    market_data = market_response.get("data")
    star_data = star_response.get("data")
    if not isinstance(market_data, dict) or not isinstance(star_data, dict):
        return "获取应用市场数据失败或数据为空"

    lines = [
        f"鸿蒙应用市场数据-{market_data.get('crate_version', '未知版本')}"
    ]
    app_count = market_data.get("app_count")
    if isinstance(app_count, dict):
        lines.append(
            "应用/元服务: "
            f"{app_count.get('total', 0)}，应用: {app_count.get('apps', 0)}，"
            f"元服务: {app_count.get('atomic_services', 0)}"
        )
    lines.append(
        f"已知专题数量: {market_data.get('substance_count', 0)}，"
        f"已知开发者数量: {market_data.get('developer_count', 0)}"
    )

    if show_sync and isinstance(market_data.get("sync_status"), dict):
        sync_status = market_data["sync_status"]
        lines.append("同步状态")
        elapsed = sync_status.get("elapsed_time")
        elapsed_secs = elapsed.get("secs", 0) if isinstance(elapsed, dict) else 0
        cost_time = datetime.timedelta(seconds=elapsed_secs)
        progress = sync_status.get("progress")
        if sync_status.get("is_syncing_all") and isinstance(progress, list) and len(progress) >= 2:
            current, total = progress[0], progress[1]
            percentage = current / total * 100 if total else 0
            lines.append(f"同步中 {current}/{total}（{percentage:.2f}%）")
            estimated = sync_status.get("estimated_total_time")
            estimated_secs = (
                estimated.get("secs", 0) if isinstance(estimated, dict) else 0
            )
            lines.append(
                f"已经用时: {cost_time}，预计总时间: "
                f"{datetime.timedelta(seconds=estimated_secs)}"
            )
        else:
            countdown = sync_status.get("next_sync_countdown")
            countdown_secs = (
                countdown.get("secs", 0) if isinstance(countdown, dict) else 0
            )
            lines.append(f"上次同步用时: {cost_time}")
            lines.append(
                f"下次同步倒计时: {datetime.timedelta(seconds=countdown_secs)}"
            )
        lines.append(
            "处理/插入/失败/跳过: "
            f"{sync_status.get('total_processed', 0)}/"
            f"{sync_status.get('total_inserted', 0)}/"
            f"{sync_status.get('total_failed', 0)}/"
            f"{sync_status.get('total_skipped', 0)}"
        )

    lines.append(
        "应用评分计数: "
        f"无评分 {star_data.get('star_1', 0)} | "
        f"1-2 分 {star_data.get('star_2', 0)} | "
        f"2-3 分 {star_data.get('star_3', 0)} | "
        f"3-4 分 {star_data.get('star_4', 0)} | "
        f"4-5 分 {star_data.get('star_5', 0)}"
    )
    return "\n".join(lines)


def query_info(msg: IcaNewMessage, client: IcaClient) -> None:
    _send_text(msg, client, fmt_info(True))


def _format_top_group(title: str, apps: list[Any]) -> list[str]:
    lines = [title]
    for index, raw_app in enumerate(apps, 1):
        app = raw_app if isinstance(raw_app, dict) else {}
        line = f"({index}) {app.get('name') or '未知应用'}"
        if _has_value(app.get("download_count")):
            line += f" · {_format_download_count(app['download_count'])} 下载"
        lines.append(line)
        details = []
        category = _join_values(
            [app.get("kind_name"), app.get("kind_type_name"), app.get("tag_name")]
        )
        if category:
            details.append(category)
        if _has_value(app.get("pkg_name")):
            details.append(str(app["pkg_name"]))
        if details:
            lines.append(" | ".join(details))
    return lines


def query_top(msg: IcaNewMessage, client: IcaClient) -> None:
    params = {
        "page_size": 5,
        "detail": "true",
        "sort": "download_count",
        "desc": "true",
    }
    all_response = request_api("apps/list/1", params=params)
    exclude_params = {**params, "exclude_huawei": "true"}
    non_huawei_response = request_api("apps/list/1", params=exclude_params)
    if all_response is None or non_huawei_response is None:
        _send_text(msg, client, "累计下载量排行查询失败，请稍后重试")
        return
    all_apps = _extract_list(all_response)
    non_huawei_apps = _extract_list(non_huawei_response)
    if not all_apps or not non_huawei_apps:
        _send_text(msg, client, "累计下载量排行暂无数据")
        return
    lines = _format_top_group("全部应用下载量前 5", all_apps[:5])
    lines.append("")
    lines.extend(_format_top_group("排除华为应用下载量前 5", non_huawei_apps[:5]))
    _send_text(msg, client, "\n".join(lines))


def _format_up_group(title: str, apps: list[Any]) -> list[str]:
    first = apps[0] if apps and isinstance(apps[0], dict) else {}
    current_date = _format_datetime(first.get("current_period_date"), date_only=True)
    prior_date = _format_datetime(first.get("prior_period_date"), date_only=True)
    period = f"（{prior_date} → {current_date}）" if current_date and prior_date else ""
    lines = [title + period]
    for index, raw_app in enumerate(apps, 1):
        app = raw_app if isinstance(raw_app, dict) else {}
        increment = _to_int(app.get("download_increment")) or 0
        lines.append(
            f"({index}) {app.get('name') or '未知应用'} · {_format_delta(increment)}"
        )
        prior = app.get("prior_download_count")
        current = app.get("current_download_count")
        details = []
        if _has_value(prior) and _has_value(current):
            details.append(
                f"{_format_download_count(prior)} → {_format_download_count(current)}"
            )
        if _has_value(app.get("pkg_name")):
            details.append(str(app["pkg_name"]))
        if details:
            lines.append(" | ".join(details))
    return lines


def query_up(msg: IcaNewMessage, client: IcaClient) -> None:
    daily_response = request_api(
        "rankings/download_increase", params={"days": 1, "limit": 5}
    )
    recent_response = request_api(
        "rankings/download_increase",
        params={
            "days": 1,
            "limit": 5,
            "exclude_huawei": "true",
            "listed_days": 30,
        },
    )
    if daily_response is None or recent_response is None:
        _send_text(msg, client, "近期下载增量排行查询失败，请稍后重试")
        return
    daily_apps = _extract_list(daily_response)
    recent_apps = _extract_list(recent_response)
    if not daily_apps or not recent_apps:
        _send_text(msg, client, "近期下载增量排行暂无数据")
        return
    lines = _format_up_group("最近一天下载增量前 5", daily_apps[:5])
    lines.append("")
    lines.extend(
        _format_up_group(
            "近 30 天上架且排除华为应用的最近一天增量前 5",
            recent_apps[:5],
        )
    )
    _send_text(msg, client, "\n".join(lines))


def query_rating(msg: IcaNewMessage, client: IcaClient) -> None:
    response = request_api("rankings/ratings", params={"limit": 5})
    if response is None:
        _send_text(msg, client, "评分排行查询失败，请稍后重试")
        return
    apps = _extract_list(response)
    if not apps:
        _send_text(msg, client, "评分排行暂无数据")
        return
    lines = ["评分排行前 5"]
    for index, raw_app in enumerate(apps[:5], 1):
        app = raw_app if isinstance(raw_app, dict) else {}
        lines.append(
            f"({index}) {app.get('name') or '未知应用'} · "
            f"评分 {app.get('average_rating', '暂无')}"
        )
        details = []
        if _has_value(app.get("total_star_rating_count")):
            details.append(
                f"{format_number(app['total_star_rating_count'])} 人评分"
            )
        if _has_value(app.get("download_count")):
            details.append(f"{_format_download_count(app['download_count'])} 下载")
        if _has_value(app.get("pkg_name")):
            details.append(str(app["pkg_name"]))
        if details:
            lines.append(" | ".join(details))
    _send_text(msg, client, "\n".join(lines))


def query_recent(msg: IcaNewMessage, client: IcaClient) -> None:
    response = request_api("rankings/recent", params={"limit": 5})
    if response is None:
        _send_text(msg, client, "最近更新排行查询失败，请稍后重试")
        return
    apps = _extract_list(response)
    if not apps:
        _send_text(msg, client, "最近更新排行暂无数据")
        return
    lines = ["最近更新应用前 5"]
    for index, raw_app in enumerate(apps[:5], 1):
        app = raw_app if isinstance(raw_app, dict) else {}
        version = f" [{app['version']}]" if _has_value(app.get("version")) else ""
        lines.append(f"({index}) {app.get('name') or '未知应用'}{version}")
        details = []
        if _has_value(app.get("release_date")):
            details.append(_format_datetime(app["release_date"]))
        if _has_value(app.get("pkg_name")):
            details.append(str(app["pkg_name"]))
        if details:
            lines.append(" | ".join(details))
    _send_text(msg, client, "\n".join(lines))


def query_developers(msg: IcaNewMessage, client: IcaClient) -> None:
    response = request_api("rankings/developers", params={"limit": 5})
    if response is None:
        _send_text(msg, client, "开发者排行查询失败，请稍后重试")
        return
    developers = _extract_list(response)
    if not developers:
        _send_text(msg, client, "开发者排行暂无数据")
        return
    lines = ["应用数量最多的开发者前 5"]
    for index, raw_developer in enumerate(developers[:5], 1):
        if isinstance(raw_developer, (list, tuple)) and len(raw_developer) >= 3:
            dev_id, developer_name, app_count = raw_developer[:3]
        elif isinstance(raw_developer, dict):
            dev_id = raw_developer.get("dev_id")
            developer_name = raw_developer.get("developer_name")
            app_count = raw_developer.get("app_count")
        else:
            continue
        lines.append(
            f"({index}) {developer_name or '未知开发者'} · "
            f"{format_number(app_count or 0)} 个应用"
        )
        if _has_value(dev_id):
            lines.append(f"开发者 ID: {dev_id}")
    if len(lines) == 1:
        _send_text(msg, client, "开发者排行暂无可用数据")
        return
    _send_text(msg, client, "\n".join(lines))


def _parse_command_lines(content: str, command: str) -> list[str] | None:
    lines = content.splitlines()
    if not lines:
        return None
    first_line = lines[0].strip()
    if first_line == command:
        first_argument = ""
    elif first_line.startswith(command) and len(first_line) > len(command):
        boundary = first_line[len(command)]
        if not boundary.isspace():
            return None
        first_argument = first_line[len(command) :].strip()
    else:
        return None
    arguments = []
    if first_argument:
        arguments.append(first_argument)
    arguments.extend(line.strip() for line in lines[1:] if line.strip())
    return arguments


def _is_exact_command(content: str, command: str) -> bool:
    return content.strip() == command


def _is_hm_command(content: str) -> bool:
    return re.match(r"^/hm(?:\s|$)", content) is not None


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    if msg.is_from_self or msg.is_reply:
        return

    content = msg.content.strip()
    if content.startswith(MARKET_PREFIX):
        # 保留应用市场直链的多行查询能力。
        for line in content.splitlines():
            line = line.strip()
            if line.startswith(MARKET_PREFIX):
                pkg_name = get_id_from_link(line)
                if pkg_name:
                    print(f"获取到新的应用链接: {pkg_name}")
                    query_pkg(msg, client, pkg_name, "pkg_name")
        return

    if content.startswith(SUBSTANCE_PREFIX):
        substance_id = get_id_from_link(content)
        if substance_id:
            print(f"获取到新的专题链接: {substance_id}")
            query_substance(msg, client, substance_id)
        return

    if content.startswith(GAME_PREFIX):
        game_id = get_id_from_link(content)
        if game_id:
            print(f"获取到新的游戏链接: {game_id}")
            query_pkg(msg, client, game_id, "app_id")
        return

    args = _parse_command_lines(content, f"{CMD_PREFIX} pkg")
    if args is not None:
        if not args:
            _send_text(msg, client, HELP_MSG)
            return
        for pkg_name in args:
            query_pkg(msg, client, pkg_name, "pkg_name")
        return

    args = _parse_command_lines(content, f"{CMD_PREFIX} id")
    if args is not None:
        if not args:
            _send_text(msg, client, HELP_MSG)
            return
        for app_id in args:
            query_pkg(msg, client, app_id, "app_id")
        return

    args = _parse_command_lines(content, f"{CMD_PREFIX} detail pkg")
    if args is not None:
        if len(args) != 1:
            _send_text(msg, client, HELP_MSG)
            return
        query_pkg(msg, client, args[0], "pkg_name", detailed=True)
        return

    args = _parse_command_lines(content, f"{CMD_PREFIX} detail id")
    if args is not None:
        if len(args) != 1:
            _send_text(msg, client, HELP_MSG)
            return
        query_pkg(msg, client, args[0], "app_id", detailed=True)
        return

    args = _parse_command_lines(content, f"{CMD_PREFIX} search")
    if args is not None:
        keyword = " ".join(args).strip()
        if not keyword:
            _send_text(msg, client, HELP_MSG)
            return
        query_search(msg, client, keyword)
        return

    args = _parse_command_lines(content, f"{CMD_PREFIX} trend")
    if args is not None:
        if len(args) != 1:
            _send_text(msg, client, HELP_MSG)
            return
        query_trend(msg, client, args[0])
        return

    args = _parse_command_lines(content, f"{CMD_PREFIX} substance")
    if args is not None:
        if len(args) != 1:
            _send_text(msg, client, HELP_MSG)
            return
        query_substance(msg, client, args[0])
        return

    if _is_exact_command(content, f"{CMD_PREFIX} info"):
        query_info(msg, client)
    elif _is_exact_command(content, f"{CMD_PREFIX} top"):
        query_top(msg, client)
    elif _is_exact_command(content, f"{CMD_PREFIX} up"):
        query_up(msg, client)
    elif _is_exact_command(content, f"{CMD_PREFIX} rating"):
        query_rating(msg, client)
    elif _is_exact_command(content, f"{CMD_PREFIX} recent"):
        query_recent(msg, client)
    elif _is_exact_command(content, f"{CMD_PREFIX} dev"):
        query_developers(msg, client)
    elif _is_hm_command(content):
        _send_text(msg, client, HELP_MSG)


def on_load() -> None:
    global API_URL
    API_URL = str(
        PLUGIN_MANIFEST.config_unchecked("main").get_value("api_url")
    ) or ""
