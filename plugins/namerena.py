from __future__ import annotations

import io
import sys
import time
import traceback
import subprocess

from pathlib import Path
from typing import TYPE_CHECKING, TypeVar
from shenbot_api import PluginManifest, ConfigStorage
from distutils.version import StrictVersion

if str(Path(__file__).parent.absolute()) not in sys.path:
    sys.path.append(str(Path(__file__).parent.absolute()))

import name_utils
import sqrtools

TELEMETRY = False
try:
    import psycopg

    TELEMETRY = True
except ImportError:
    pass

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

_version_ = "0.10.1"

CMD_PREFIX = "/namer"

EVAL_CMD = "/namerena"
EVAL_SIMPLE_CMD = f"{CMD_PREFIX}"  # 用于简化输入
# 评分四兄弟
EVAL_PP_CMD = f"{CMD_PREFIX}-pp"
EVAL_PD_CMD = f"{CMD_PREFIX}-pd"
EVAL_QP_CMD = f"{CMD_PREFIX}-qp"
EVAL_QD_CMD = f"{CMD_PREFIX}-qd"
EVAL_PF_CMD = f"{CMD_PREFIX}-pf"
CONVERT_CMD = f"{CMD_PREFIX}-peek"
BASE_CMD = f"{CMD_PREFIX}-base"
FIGHT_CMD = f"{CMD_PREFIX}-fight"
HELP_CMD = f"{CMD_PREFIX}-help"

HELP_MSG = f"""namerena-v[{_version_}]
名字竞技场 一款不建议入坑的文字类游戏
PF
- {HELP_CMD} - 查看帮助
- {EVAL_CMD} - 运行名字竞技场, 每一行是一个输入, 输入格式与网页版相同
- {EVAL_SIMPLE_CMD} - 简化输入
- {CMD_PREFIX}-[pd|pd|qp|qd] - 你懂的评分
    - 一行一个名字/+连接的多个名字
- {EVAL_PF_CMD} - 一下子全评
    - 一行一个名字/+连接的多个名字
- {CONVERT_CMD} - 查看一个名字的属性, 每一行一个名字
- {BASE_CMD} - base 工具, 只支持单个名字 (避免刷屏)
- {FIGHT_CMD} - 1v1 战斗, 格式是 "AAA+BBB+[seed]"
    - 例如: "AAA+BBB+seed:123@!" 表示 AAA 和 BBB 以 123@! 为种子进行战斗
    - 可以输入多行"""

bun_hint = "bun\npowered by https://bun.sh"

DB_VERSION = 1
"""
数据库版本号
- 1: 20250504 初始版本
"""

cfg = ConfigStorage(
    # 是否启用 bun
    use_bun = False,
    # 是否启用遥测
    telemetry = True,
)

PLUGIN_MANIFEST = PluginManifest(
    plugin_id="namer",
    name="名竞小工具",
    version=_version_,
    description="namerena 的一堆小工具",
    authors=["shenjack"],
    config={"main": cfg},
)

USE_BUN = False

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
            cache.write(f"{i + 1} {raw_players[i]} 无法解析\n")
            raw_players[i] = ""
    for i, player in enumerate(players):
        if raw_players[i] == "":
            continue
        cache.write(player.display())
        cache.write("\n")
    reply = msg.reply_with(f"{cache.getvalue()}版本:{_version_}")
    client.send_message(reply)

def convert_base(msg: ReciveMessage, client) -> None:
    if StrictVersion(sqrtools.SQRTOOLS_VERSION)<StrictVersion("3.3"):
        client.send_message(
            msg.reply_with(
                "错误: 内部依赖库版本错误\n"
            )
        )
        return
    if msg.content.find("\n") == -1:
        client.send_message(
            msg.reply_with(
                f"请使用 {BASE_CMD} 命令\n为防止刷屏，一次只能转换一个名字\n"
            )
        )
        return
    # 去掉 prefix
    names = msg.content[len(BASE_CMD) :]
    # 去掉第一个 \n
    names = names[names.find("\n") + 1 :]
    cache = io.StringIO()
    raw_players = [x for x in names.split("\n") if x != ""]
    if len(raw_players) != 1:
        client.send_message(
            msg.reply_with(
                "请输入名字\n为防止刷屏，一次只能转换一个名字\n"
            )
        )
        return
    current_player = sqrtools.Name()
    if not current_player.load(raw_players[0]):
        client.send_message(
            msg.reply_with(
                "错误: 名字载入失败\n"
            )
        )
        return
    r=name.namebase[0:32]
    hpcache='('+','.join(str(i) for i in r[0:10])+')\n'
    r[0:10]=sorted(r[0:10])
    cache.write("HP: "+str(154+sum(r[3:7]))+' / '+str(154+sum(r[4:8]))+'\n')
    cache.write(hpcache)
    propcnt=1
    for i in range(10,31,3):
        cache.write(sqrtools.propname[propcnt]+': ')
        cache.write(' '.join(str(j).zfill(2) for j in r[i:i+3])+' ')
        r[i:i+3]=sorted(r[i:i+3])
        cache.write("-> "+str(r[i+1]+36)+' / '+str(r[i+2]+36)+'\n')
        propcnt+=1
    cache.write('\n')
    name.calcskill(False)
    doubleflag=-1
    for i in range(15,-1,-1):
        if name.nameskill[i][1]>0 and name.nameskill[i][0]<25:
            doubleflag=i
            break
    for i in range(16):
        cache.write("#"+str(i).zfill(2)+' '+sqrtools.sklname[name.nameskill[i][0]])
        if name.nameskill[i][0]>=35:
            cache.write('\n')
        else:
            r=name.namebase[i*4+64:i*4+68]
            cache.write(': '+' '.join(str(j).zfill(2) for j in r)+" -> "+str(name.nameskill[i][1]).zfill(2)+' / ')
            r=sorted(r)
            if i<14:
                if doubleflag==i:
                    cache.write(str((r[1]-10)*2 if r[1]>10 else 0).zfill(2)+"\n    ↑末尾主动\n")
                else:
                    cache.write(str(r[1]-10 if r[1]>10 else 0).zfill(2)+'\n')
            else:
                if name.nameskill[i][1]>0:
                    if doubleflag==i:
                        cache.write(str((r[1]-10)*2 if r[1]>10 else 0).zfill(2)+"\n    ↑末尾主动\n")
                    else:
                        a=r[1]-10+min([r[1]-10]+name.namebase[32+i*2:34+i*2])
                        b=r[0]-10+min(r[0]-10,max(name.namebase[32+i*2:34+i*2]))
                        cache.write(str(a if a>b else b).zfill(2)+"\n    ↑末尾座位加成: "+' '.join(str(j).zfill(2) for j in name.namebase[32+i*2:34+i*2])+'\n')
                else:
                    cache.write(str(r[1]-10 if r[1]>10 else 0).zfill(2)+'\n')
    cache.write('\n')
    reply = msg.reply_with(f"{cache.getvalue()}版本: {_version_} (sqrtools {sqrtools.SQRTOOLS_VERSION})")
    client.send_message(reply)

def run_namerena(input_text: str, fight_mode: bool = False) -> tuple[str, float]:
    """运行namerena"""
    root_path = Path(__file__).parent
    use_bun = USE_BUN
    print(use_bun)
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


def score_all(msg: ReciveMessage, client) -> None:
    content = msg.content[len(EVAL_PP_CMD) :]
    # 去掉第一个 \n
    content = content[content.find("\n") + 1 :]
    # 判空, 别报错了
    if content.strip() == "":
        client.send_message(msg.reply_with("请输入名字"))
        return
    names = content.split("\n")
    results = []
    client.send_message(msg.reply_with(f"开始计算, 预计一个至少需要11s的时间, 大约需要 {len(names) * 11}s"))
    start_time = time.time()
    runs = [
        "!test!\n\n{test}",
        "!test!\n\n{test}\n{test}",
        "!test!\n!\n\n{test}",
        "!test!\n!\n\n{test}\n{test}",
    ]
    for name in names:
        scores = []
        all_time = 0
        for run in runs:
            if name.strip() == "":
                continue
            name = name.split("+")
            name = "\n".join(name)
            bench = run.format(test=name)
            result = run_namerena(bench)
            cost_time = result[1]
            all_time += cost_time
            # 只取最后一行括号之前的内容
            last_line = result[0].split("\n")[-1]
            last_line = last_line.split("(")[0]
            if last_line.endswith(".00"):
                last_line = last_line[:-3]
            scores.append(last_line)
        if all(x.isdigit() for x in scores):
            scores.append(str(sum(map(int, scores))))
        results.append(["|".join(scores), all_time])
    end_time = time.time()
    content = "\n".join((f"{score}-{cost_time:.2f}s" for (score, cost_time) in results))
    reply = msg.reply_with(f"pp|pd|qp|qd\n{content}\n{out_msg(end_time - start_time)}")
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
    elif msg.content.startswith(BASE_CMD):
        convert_base(msg, client)
    elif msg.content.startswith(EVAL_PP_CMD):
        eval_score(msg, client, "!test!\n\n{test}")
    elif msg.content.startswith(EVAL_PD_CMD):
        eval_score(msg, client, "!test!\n\n{test}\n{test}")
    elif msg.content.startswith(EVAL_QP_CMD):
        eval_score(msg, client, "!test!\n!\n\n{test}")
    elif msg.content.startswith(EVAL_QD_CMD):
        eval_score(msg, client, "!test!\n!\n\n{test}\n{test}")
    elif msg.content.startswith(EVAL_PF_CMD):
        score_all(msg, client)
    elif msg.content.startswith(EVAL_SIMPLE_CMD):
        # 放在最后, 避免覆盖 前面的命令
        # 同时过滤掉别的 /namer-xxxxx
        if not msg.content.startswith(f"{EVAL_SIMPLE_CMD}-"):
            eval_fight(msg, client)


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    dispatch_msg(msg, client)  # type: ignore


def on_tailchat_message(msg: TailchatReciveMessage, client) -> None:
    dispatch_msg(msg, client)  # type: ignore


def on_load() -> None:
    global USE_BUN
    USE_BUN = PLUGIN_MANIFEST.config_unchecked("main").get_value("use_bun") or False
    # conn = get_db_connection()
    # conn.close()
