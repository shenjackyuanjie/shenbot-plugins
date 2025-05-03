from __future__ import annotations

import io
import sys
import time
import tomli
import sqlite3
import traceback
import subprocess

from pathlib import Path

from typing import TYPE_CHECKING, TypeVar

if str(Path(__file__).parent.absolute()) not in sys.path:
    sys.path.append(str(Path(__file__).parent.absolute()))

import name_utils

if TYPE_CHECKING:
    from ica_typing import (
        IcaNewMessage,
        IcaClient,
        ReciveMessage,
        TailchatReciveMessage,
    )

else:
    IcaNewMessage = TypeVar("NewMessage")
    IcaClient = TypeVar("IcaClient")
    ReciveMessage = TypeVar("ReciveMessage")
    TailchatReciveMessage = TypeVar("TailchatReciveMessage")


USE_BUN = True

_version_ = "0.9.1"

CMD_PREFIX = "/namer"

EVAL_CMD = "/namerena"
EVAL_SIMPLE_CMD = f"{CMD_PREFIX}"  # 用于简化输入
# 评分四兄弟
EVAL_PP_CMD = f"{CMD_PREFIX}-pp"
EVAL_PD_CMD = f"{CMD_PREFIX}-pd"
EVAL_QP_CMD = f"{CMD_PREFIX}-qp"
EVAL_QD_CMD = f"{CMD_PREFIX}-qd"
CONVERT_CMD = f"{CMD_PREFIX}-peek"
FIGHT_CMD = f"{CMD_PREFIX}-fight"
HELP_CMD = f"{CMD_PREFIX}-help"

HELP_MSG = f"""namerena-v[{_version_}]
名字竞技场 一款不建议入坑的文字类游戏

- {HELP_CMD} - 查看帮助
- {EVAL_CMD} - 运行名字竞技场, 每一行是一个输入, 输入格式与网页版相同
- {EVAL_SIMPLE_CMD} - 简化输入
- {CMD_PREFIX}-[pd|pd|qp|qd] - 你懂的评分
    - 一行一个名字/+连接的多个名字
- {CONVERT_CMD} - 查看一个名字的属性, 每一行一个名字
- {FIGHT_CMD} - 1v1 战斗, 格式是 "AAA+BBB+[seed]"
    - 例如: "AAA+BBB+seed:123@!" 表示 AAA 和 BBB 以 123@! 为种子进行战斗
    - 可以输入多行"""

bun_hint = "bun\npowered by https://bun.sh"


def init_sqlite_db():
    ...


def out_msg(cost_time: float) -> str:
    use_bun = USE_BUN
    return (
        f"耗时: {cost_time:.3f}s\n版本: {_version_}-{bun_hint if use_bun else 'node'}"
    )


def convert_name(msg: ReciveMessage, client) -> None:
    # 也是多行
    if msg.content.find("\n") == -1:
        client.send_message(
            msg.reply_with(
                f"请使用 {CONVERT_CMD} 命令，然后换行输入名字，例如：\n{CONVERT_CMD}\n张三\n李四\n王五\n"
            )
        )
        return
    # 去掉 prefix
    names = msg.content[len(CONVERT_CMD) :]
    # 去掉第一个 \n
    names = names[names.find("\n") + 1 :]
    cache = io.StringIO()
    raw_players = [x for x in names.split("\n") if x != ""]
    players = [name_utils.Player() for _ in raw_players]
    for i, player in enumerate(players):
        if not player.load(raw_players[i]):
            cache.write(f"{i+1} {raw_players[i]} 无法解析\n")
            raw_players[i] = ""
    for i, player in enumerate(players):
        if raw_players[i] == "":
            continue
        cache.write(player.display())
        cache.write("\n")
    reply = msg.reply_with(f"{cache.getvalue()}版本:{_version_}")
    client.send_message(reply)


def run_namerena(input_text: str, fight_mode: bool = False) -> tuple[str, float]:
    """运行namerena"""
    root_path = Path(__file__).parent
    use_bun = USE_BUN
    runner_path = root_path / "md5" / ("md5-api.ts" if use_bun else "md5-api.js")
    if not runner_path.exists():
        return "未找到namerena运行文件", 0.0
    run_cmd = ["bun" if use_bun else "node", runner_path]
    if fight_mode:
        run_cmd += ["fight"]

    start_time = time.time()
    try:
        with open(root_path / "md5" / "input.txt", "w", encoding="utf-8") as f:
            f.write(input_text)
        result = subprocess.run(
            run_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode == 0:
            result = result.stdout.decode("utf-8")
        else:
            result = result.stderr.decode("utf-8")
    except Exception as e:
        result = f"发生错误: {e}\n{traceback.format_exc()}"
    end_time = time.time()
    return result.strip(), end_time - start_time


def eval_fight(msg: ReciveMessage, client) -> None:
    if msg.content.find("\n") == -1:
        # 在判断一下是不是 /xxx xxxx
        if msg.content.find(" ") != -1:
            client.send_message(
                msg.reply_with(
                    f"请使用 {EVAL_CMD} 命令，然后换行输入名字，例如：\n{EVAL_CMD}\n张三\n李四\n王五\n"
                )
            )
            return
    # 去掉 prefix, 先判断是完整的还是短的
    names = (
        msg.content[len(EVAL_CMD) :]
        if msg.content.startswith(EVAL_CMD)
        else msg.content[len(EVAL_SIMPLE_CMD) :]
    )
    # 去掉第一个 \n
    names = names[names.find("\n") + 1 :]
    # 判空, 别报错了
    if names.strip() == "":
        client.send_message(msg.reply_with("请输入名字"))
        return

    result = run_namerena(names)
    client.send_message(msg.reply_with(f"{result[0]}\n{out_msg(result[1])}"))


def run_fights(msg: ReciveMessage, client) -> None:
    # 先解析出要运行的东西
    # 格式
    # aaaa+bbb+seed:123@!
    # aaa+bbb
    content = msg.content[len(FIGHT_CMD) :]
    # 去掉第一个 \n
    content = content[content.find("\n") + 1 :]
    # 判空, 别报错了
    if content.strip() == "":
        client.send_message(msg.reply_with("请输入名字"))
        return
    # 以换行分割
    fights = content.split("\n")
    results = []
    start_time = time.time()
    for fight in fights:
        # 以 + 分割
        names = fight.split("+")
        if len(names) < 2:
            results.append(f"输入错误, 只有{len(names)} 个部分")
            continue
        result = run_namerena("\n".join(names), fight_mode=True)[0]
        if result in names:
            results.append(f"{names.index(result)}")
        else:
            results.append(result)
    # 输出
    end_time = time.time()
    reply = msg.reply_with(f"{'|'.join(results)}\n{out_msg(end_time - start_time)}")
    client.send_message(reply)


def eval_score(msg: ReciveMessage, client, template: str) -> None:
    content = msg.content[len(EVAL_PP_CMD) :]
    # 去掉第一个 \n
    content = content[content.find("\n") + 1 :]
    # 判空, 别报错了
    if content.strip() == "":
        client.send_message(msg.reply_with("请输入名字"))
        return
    names = content.split("\n")
    results = []
    start_time = time.time()
    for name in names:
        if name.strip() == "":
            continue
        name = name.split("+")
        name = "\n".join(name)
        runs = template.format(test=name)
        result = run_namerena(runs)
        # 只取最后一行括号之前的内容
        last_line = result[0].split("\n")[-1]
        last_line = last_line.split("(")[0]
        results.append([last_line, result[1]])
    end_time = time.time()
    content = "\n".join((f"{score}-{cost_time:.2f}s" for (score, cost_time) in results))
    reply = msg.reply_with(f"{content}\n{out_msg(end_time - start_time)}")
    client.send_message(reply)


def dispatch_msg(msg: ReciveMessage, client) -> None:
    if msg.is_reply or msg.is_from_self:
        return
    if msg.content == HELP_CMD:
        reply = msg.reply_with(HELP_MSG)
        client.send_message(reply)
    elif msg.content.startswith(EVAL_CMD):
        eval_fight(msg, client)
    elif msg.content.startswith(FIGHT_CMD):
        run_fights(msg, client)
    elif msg.content.startswith(CONVERT_CMD):
        convert_name(msg, client)
    elif msg.content.startswith(EVAL_PP_CMD):
        eval_score(msg, client, "!test!\n\n{test}")
    elif msg.content.startswith(EVAL_PD_CMD):
        eval_score(msg, client, "!test!\n\n{test}\n{test}")
    elif msg.content.startswith(EVAL_QP_CMD):
        eval_score(msg, client, "!test!\n!\n\n{test}")
    elif msg.content.startswith(EVAL_QD_CMD):
        eval_score(msg, client, "!test!\n!\n\n{test}\n{test}")
    elif msg.content.startswith(EVAL_SIMPLE_CMD):
        # 放在最后, 避免覆盖 前面的命令
        # 同时过滤掉别的 /namer-xxxxx
        if not msg.content.startswith(f"{EVAL_SIMPLE_CMD}-"):
            eval_fight(msg, client)


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    dispatch_msg(msg, client)  # type: ignore


def on_tailchat_message(msg: TailchatReciveMessage, client) -> None:
    dispatch_msg(msg, client)  # type: ignore


def require_config() -> tuple[str, str]:
    return ("namer.toml", "use_bun = false # 是否使用 bun")


def on_config(data: bytes):
    global USE_BUN
    string = data.decode("utf-8")
    config = tomli.loads(string)
    USE_BUN = config.get("use_bun", False)
