from __future__ import annotations

import io
import random

from typing import TYPE_CHECKING
from pathlib import Path

import requests

from pydantic import BaseModel, Field
from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient

_version_ = "0.1.0"

CMD_PREFIX = "/piaofang"
HELP_CMD = f"{CMD_PREFIX} help"
HISTORY_CMD = f"{CMD_PREFIX} history"
REAL_TIME_CMD = f"{CMD_PREFIX} realtime"

FONT_PATH = "NotoSansMonoCJKsc-VF.ttf"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0"
HISTORY_API = "https://piaofang.maoyan.com/i/api/rank/globalBox/historyRankList"
REAL_TIME_API = "https://piaofang.maoyan.com/dashboard-ajax/movie"

HELP_MSG = f"""piaofang-{_version_}: 获取电影票房信息

{HELP_CMD}: 获取帮助信息
{HISTORY_CMD}: 获取电影票房历史数据
{REAL_TIME_CMD}: 获取实时票房数据"""

CACHE_FONT = None


def get_fonts() -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    global CACHE_FONT
    font_path = Path(__file__).parent / "NotoSansMonoCJKsc-VF.ttf"
    font_size = 20
    font_bold_size = 24
    if CACHE_FONT is None:
        font = ImageFont.truetype(font_path, font_size)
        font_bold = ImageFont.truetype(font_path, font_bold_size)
        CACHE_FONT = font, font_bold
    return CACHE_FONT


def fmt_value(value: int) -> str:
    """
    格式化一下数据
    """
    # 单位定义（按从大到小排列）
    units = [
        ("京", 10**16),
        ("兆", 10**12),
        ("亿", 10**8),
        ("千万", 10**7),
        ("百万", 10**6),
        ("万", 10**4),
        ("千", 10**3),
    ]

    # 遍历所有单位找到合适的量级
    for unit, size in units:
        if value >= size:
            formatted = value / size
            return f"{formatted:.2f}{unit}元"

    # 小于最小单位（千）的直接显示
    return f"{value}元"


def hsv_to_rgb(h, s, v):
    """
    手动实现 HSV 转 RGB
    Args:
        h: 色调 (0-360)
        s: 饱和度 (0-1)
        v: 明度 (0-1)
    Returns:
        (r, g, b) 范围 0-255
    """
    h = h % 360
    c = v * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v - c

    if 0 <= h < 60:
        r, g, b = c, x, 0
    elif 60 <= h < 120:
        r, g, b = x, c, 0
    elif 120 <= h < 180:
        r, g, b = 0, c, x
    elif 180 <= h < 240:
        r, g, b = 0, x, c
    elif 240 <= h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x

    r = int((r + m) * 255)
    g = int((g + m) * 255)
    b = int((b + m) * 255)
    return r, g, b


class SplitUnit(BaseModel):
    num: str
    unit: str


class MovieInfo(BaseModel):
    movieId: int
    movieName: str
    releaseInfo: str


class RealTimeItem(BaseModel):
    avgSeatView: str  # 场均人次（百分比字符串格式）
    avgShowView: str  # 平均场次（数值型字符串）
    boxRate: str  # 票房占比（百分比）
    boxSplitUnit: SplitUnit  # 票房分账单元（含加密数值和单位）
    movieInfo: MovieInfo  # 电影元信息（嵌套对象）
    showCount: int  # 排片场次（整数）
    showCountRate: str  # 排片占比（百分比）
    splitBoxRate: str  # 分账票房占比（百分比）
    splitBoxSplitUnit: SplitUnit  # 分账票房单元（含加密数值和单位）
    sumBoxDesc: str  # 累计票房描述（含单位）
    sumSplitBoxDesc: str  # 分账票房总额描述（含单位）


class HistoryItem(BaseModel):
    rank: float = Field(..., alias="box", description="票房数据（单位：亿）")
    force: bool = Field(..., description="是否强制上榜")
    movie_id: int = Field(..., alias="movieId", description="电影ID")
    movie_name: str = Field(..., alias="movieName", description="电影名称")
    raw_value: int = Field(..., alias="rawValue", description="原始票房值（单位：元）")
    release_time: str = Field(..., alias="releaseTime", description="上映年份")

    def fmt_raw_value(self) -> str:
        """
        格式化一下原始票房数据
        """
        return fmt_value(self.raw_value)


def request_history() -> list[HistoryItem] | None:
    response = requests.get(HISTORY_API, headers={"User-Agent": UA})
    raw_data = response.json()
    if "success" in raw_data:
        if not raw_data["success"]:
            return None
        data: list[dict] = raw_data["data"]["list"]
        return [HistoryItem(**item) for item in data]


def request_real_time() -> list[RealTimeItem] | None:
    response = requests.get(REAL_TIME_API, headers={"User-Agent": UA})
    raw_data = response.json()
    if "movieList" in raw_data:
        data: list[dict] = raw_data["movieList"]["list"]
        return [RealTimeItem(**item) for item in data]


def handle_history(msg: IcaNewMessage, client: IcaClient) -> None:
    history = request_history()
    if history is None:
        client.send_message(msg.reply_with("获取票房数据失败"))
        return

    result = io.StringIO()
    # (年份) (名称) (票房)
    for item in history:
        if item.force:
            # 强制的, 加个标记
            result.write(
                f"* {item.release_time} {item.movie_name} {item.fmt_raw_value()}\n"
            )
        else:
            result.write(
                f"{item.release_time} {item.movie_name} {item.fmt_raw_value()}\n"
            )

    client.send_message(msg.reply_with(result.getvalue().strip()))


def handle_real_time(msg: IcaNewMessage, client: IcaClient) -> None:
    real_time = request_real_time()
    if real_time is None:
        client.send_message(msg.reply_with("获取实时票房数据失败"))
        return

    # result = io.StringIO()
    # # 上映xx天 (名称)
    # # (票房(万)) (占比) (排片占比)
    # for item in real_time[:5]:
    #     result.write(f"{item.movieInfo.releaseInfo:<6} {item.movieInfo.movieName}\n")
    #     result.write(f"{item.sumBoxDesc:<9} {item.boxRate:<5} {item.showCountRate:<5}\n")

    # reply = msg.reply_with(result.getvalue().strip())

    reply = msg.reply_with("数据:")
    # 画图
    normal_font, title_font = get_fonts()
    text_box = normal_font.getbbox("啊")
    line_height = text_box[3] - text_box[1] + 5

    name_width_max = 0
    for item in real_time:
        name_box = normal_font.getbbox(item.movieInfo.movieName)
        name_width_max = max(name_width_max, name_box[2] - name_box[0])

    img_width = 700
    img_height = round(line_height * len(real_time) * 2) + 7
    img = Image.new("RGB", (img_width, img_height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)


    prev_h = random.randint(0, 360)

    for i, item in enumerate(real_time):
        while True:
            new_h = random.randint(0, 360)
            diff = abs(new_h - prev_h)
            min_diff = min(diff, 360 - diff)
            if min_diff >= 50:
                break

        # 更新颜色参数
        prev_h = new_h
        s = random.uniform(0.8, 1.0)  # 饱和度（鲜艳度）
        v = random.uniform(0.4, 0.6)  # 明度（避免太亮或太暗）

        # 手动转换 HSV → RGB
        r, g, b = hsv_to_rgb(new_h, s, v)
        color = (r, g, b)

        # 绘制文本（保持你的原有逻辑）
        y_1 = (i * 2) * line_height
        y_2 = (i * 2 + 1) * line_height
        release_info = item.movieInfo.releaseInfo or "未知"

        draw.text((2, y_1), f"{release_info}", font=normal_font, fill=color)
        draw.text(
            (100, y_1), f"{item.movieInfo.movieName}", font=normal_font, fill=(0, 0, 0)
        )
        draw.text(
            (name_width_max + 115, y_1),
            f"票房:{item.sumBoxDesc:<7} 占比:{item.boxRate}",
            font=normal_font,
            fill=color,
        )
        draw.text(
            (2, y_2),
            f"排片量:{item.showCount:<7} {item.showCountRate:<5}",
            font=normal_font,
            fill=color,
        )

        # 画分割线
        draw.line(
            (0, y_2 + line_height + 0, img_width, y_2 + line_height + 0),
            fill=(0, 0, 0),
            width=1,
        )

    # 画纵列分割线
    draw.line(
        (name_width_max + 110, 0, name_width_max + 110, img_height),
        fill=(0, 0, 0),
        width=1,
    )

    # 输出图片到内存
    img_bytes = io.BytesIO()
    img.save(img_bytes, format="PNG")
    img_bytes.seek(0)
    img_bytes = img_bytes.getvalue()

    reply.set_img(img_bytes, "image/png", False)

    client.send_message(reply)


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    if msg.is_from_self or not msg.is_room_msg:
        return

    if not msg.content.startswith(CMD_PREFIX):
        return

    cmd = msg.content
    if cmd == HELP_CMD:
        client.send_message(msg.reply_with(HELP_MSG))
    elif cmd == HISTORY_CMD:
        handle_history(msg, client)
    elif cmd == REAL_TIME_CMD:
        handle_real_time(msg, client)
