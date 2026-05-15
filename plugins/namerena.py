from __future__ import annotations

import io
import re
import shutil
import sys
import time
import traceback
import subprocess

from pathlib import Path
from typing import TYPE_CHECKING, TypeVar
from shenbot_api import PluginManifest, ConfigStorage

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

_version_ = "0.10.3"

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
    use_bun=False,
    # 是否启用遥测
    telemetry=True,
    # 是否启用 tswn-cli 对比
    use_tswn_compare=True,
    # tswn-cli 路径, 支持直接填 exe / 仓库根目录 / crates/tswn_core
    tswn_cli_path="",
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
USE_TSWN_COMPARE = True
TSWN_CLI_PATH = ""
TSWN_RUNNER: tuple[list[str], str | None] | None = None
TSWN_RUNNER_FAILED = False
TSWN_COMPARE_ROUNDS = 10000
VERSION_CACHE: dict[tuple[str, ...], str | None] = {}


def out_msg(cost_time: float) -> str:
    use_bun = USE_BUN
    runtime = "bun" if use_bun else "node"
    lines = [f"耗时: {cost_time:.3f}s", f"版本: {_version_}-{runtime}"]

    runtime_version = get_runtime_version()
    if runtime_version:
        lines.append(f"{runtime}: {runtime_version}")

    tswn_version = get_tswn_version()
    if tswn_version:
        lines.append(f"tswn: {tswn_version}")

    if use_bun:
        lines.append("powered by https://bun.sh")

    return "\n".join(lines)


def join_non_empty(*parts: str) -> str:
    return "\n".join(part for part in parts if part)


def _resolve_tswn_runner_candidate(
    raw_path: str,
) -> tuple[list[str], str | None] | None:
    candidate = Path(raw_path)
    if candidate.exists():
        if candidate.is_file():
            return [str(candidate)], None

        crate_dir = candidate
        if not (crate_dir / "Cargo.toml").exists():
            nested_crate = candidate / "crates" / "tswn_core"
            if (nested_crate / "Cargo.toml").exists():
                crate_dir = nested_crate

        workspace_dir = crate_dir
        if crate_dir.name == "tswn_core" and crate_dir.parent.name == "crates":
            workspace_dir = crate_dir.parent.parent

        exe_name = "tswn-cli.exe" if sys.platform.startswith("win") else "tswn-cli"
        for exe_path in (
            workspace_dir / "target" / "debug" / exe_name,
            workspace_dir / "target" / "release" / exe_name,
        ):
            if exe_path.exists():
                return [str(exe_path)], None

        if (crate_dir / "Cargo.toml").exists():
            return ["cargo", "run", "--bin", "tswn-cli", "--"], str(crate_dir)

    resolved = shutil.which(raw_path)
    if resolved is not None:
        return [resolved], None

    return None


def resolve_tswn_runner() -> tuple[list[str], str | None] | None:
    global TSWN_RUNNER, TSWN_RUNNER_FAILED

    if TSWN_RUNNER is not None:
        return TSWN_RUNNER
    if TSWN_RUNNER_FAILED:
        return None

    if TSWN_CLI_PATH.strip():
        resolved = _resolve_tswn_runner_candidate(TSWN_CLI_PATH.strip())
        if resolved is not None:
            TSWN_RUNNER = resolved
            return resolved

    plugin_root = Path(__file__).resolve().parent
    exe_name = "tswn-cli.exe" if sys.platform.startswith("win") else "tswn-cli"
    for candidate in (
        plugin_root / "name_utils" / exe_name,
        plugin_root / "name_utils" / "tswn-cli",
    ):
        resolved = _resolve_tswn_runner_candidate(str(candidate))
        if resolved is not None:
            TSWN_RUNNER = resolved
            return resolved

    path_runner = _resolve_tswn_runner_candidate("tswn-cli")
    if path_runner is not None:
        TSWN_RUNNER = path_runner
        return path_runner

    workspace_root = Path(__file__).resolve().parent.parent
    tswn_repo = workspace_root.parent.parent / "namer" / "tswn-core"
    for candidate in (
        tswn_repo / "target" / "debug" / exe_name,
        tswn_repo / "target" / "release" / exe_name,
        tswn_repo / "crates" / "tswn_core",
        tswn_repo,
    ):
        resolved = _resolve_tswn_runner_candidate(str(candidate))
        if resolved is not None:
            TSWN_RUNNER = resolved
            return resolved

    TSWN_RUNNER_FAILED = True
    return None


def run_tswn_cli(input_text: str, *args: str) -> tuple[str, float] | None:
    global TSWN_RUNNER_FAILED

    if not USE_TSWN_COMPARE:
        return None

    runner = resolve_tswn_runner()
    if runner is None:
        return None

    command, cwd = runner
    start_time = time.time()
    try:
        result = subprocess.run(
            [*command, *args],
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            encoding="utf-8",
            cwd=cwd,
        )
        output = (
            result.stdout
            if result.returncode == 0
            else (result.stderr or result.stdout)
        )
    except FileNotFoundError:
        TSWN_RUNNER_FAILED = True
        return None
    except Exception as e:
        output = f"发生错误: {e}\n{traceback.format_exc()}"
    return output.strip(), time.time() - start_time


def last_non_empty_line(output: str) -> str:
    for line in reversed(output.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def resolve_command_version(command: list[str], cwd: str | None = None) -> str | None:
    cache_key = tuple([*command, f"cwd={cwd or ''}"])
    if cache_key in VERSION_CACHE:
        return VERSION_CACHE[cache_key]

    try:
        result = subprocess.run(
            [*command, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            encoding="utf-8",
            cwd=cwd,
        )
    except Exception:
        VERSION_CACHE[cache_key] = None
        return None

    output = result.stdout or result.stderr
    version = last_non_empty_line(output)
    VERSION_CACHE[cache_key] = version or None
    return VERSION_CACHE[cache_key]


def get_runtime_version() -> str | None:
    return resolve_command_version(["bun" if USE_BUN else "node"])


def get_tswn_version() -> str | None:
    if not USE_TSWN_COMPARE:
        return None

    runner = resolve_tswn_runner()
    if runner is None:
        return None

    command, cwd = runner
    version = resolve_command_version(command, cwd)
    if version is None:
        return None
    if version.startswith("tswn-cli "):
        return version[len("tswn-cli ") :]
    return version


def is_bench_input(input_text: str) -> bool:
    raw = input_text.lstrip("\ufeff").lstrip()
    return raw.startswith("!test!")


def parse_tswn_winner_names(output: str) -> list[str]:
    winners = []
    in_winner_block = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line == "赢家:":
            in_winner_block = True
            continue
        if not in_winner_block:
            continue
        if line.startswith("总战斗分:") or line.startswith("win_idx="):
            break
        if line.startswith("- "):
            winners.append(line[2:].split(" (", 1)[0])
    return winners


def summarize_tswn_fight(output: str) -> str:
    winners = parse_tswn_winner_names(output)
    win_idx = ""
    unresolved = ""
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("win_idx="):
            win_idx = line
        elif "未分出胜负" in line:
            unresolved = line

    parts = []
    if winners:
        parts.append(f"赢家={'|'.join(winners)}")
    elif unresolved:
        parts.append(unresolved)
    if win_idx:
        parts.append(win_idx)
    if parts:
        return ", ".join(parts)
    return last_non_empty_line(output) or "无结果"


def summarize_tswn_fight_for_names(output: str, names: list[str]) -> str:
    winners = parse_tswn_winner_names(output)
    if len(winners) == 1 and winners[0] in names:
        return str(names.index(winners[0]))
    if winners:
        return "|".join(winners)
    return summarize_tswn_fight(output)


def summarize_tswn_bench(output: str) -> str:
    normal_score = ""
    bang_score = ""
    win_rate = ""
    for raw_line in output.splitlines():
        line = raw_line.strip()
        normal_match = re.search(r"普通评分:\s*[^\r\n(]+", line)
        bang_match = re.search(r"!评分:\s*[^\r\n(]+", line)
        win_rate_match = re.search(r"胜率:\s*[^\r\n(]+", line)

        if normal_match is not None:
            normal_score = normal_match.group(0).strip()
        if bang_match is not None:
            bang_score = bang_match.group(0).strip()
        if win_rate_match is not None:
            win_rate = win_rate_match.group(0).strip()

    if normal_score or bang_score:
        return " / ".join(part for part in (normal_score, bang_score) if part)
    if win_rate:
        return win_rate
    return last_non_empty_line(output) or "无结果"


def _parse_win_rate_pct(text: str) -> float | None:
    """从文本中提取胜率百分比数值, 如 '45.63%' -> 45.63"""
    # 匹配 namerena 输出: 45.63%(10000) 或 最终胜率:|45.6300%|(10000轮)
    m = re.search(r"(\d+\.?\d*)%", text)
    if m is None:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _compute_bench_diff(
    namerena_output: str, tswn_summary: str
) -> str | None:
    """计算 namerena 与 tswn 在胜率模式下的差值, 返回 'diff! = 2' 或 None"""
    # 从 namerena 输出提取胜率: 优先取 最终胜率 行, 否则取最后一行
    namerena_rate: float | None = None
    for raw_line in namerena_output.splitlines():
        line = raw_line.strip()
        if line.startswith("最终胜率:"):
            namerena_rate = _parse_win_rate_pct(line)
            break
    if namerena_rate is None:
        # 取最后非空行
        last = last_non_empty_line(namerena_output)
        if last:
            namerena_rate = _parse_win_rate_pct(last)

    # 从 tswn 汇总提取胜率
    tswn_rate = _parse_win_rate_pct(tswn_summary)

    if namerena_rate is None or tswn_rate is None:
        return None

    diff = round(abs(namerena_rate - tswn_rate) * 100)
    return f"diff! = {diff}"


def run_tswn_fight_compare(input_text: str) -> tuple[str, float] | None:
    result = run_tswn_cli(input_text, "fight")
    if result is None:
        return None
    return summarize_tswn_fight(result[0]), result[1]


def run_tswn_bench_compare(input_text: str) -> tuple[str, float] | None:
    result = run_tswn_cli(input_text, "raw", "-n", str(TSWN_COMPARE_ROUNDS))
    if result is None:
        return None
    return summarize_tswn_bench(result[0]), result[1]


def run_tswn_compare(input_text: str) -> tuple[str, float] | None:
    if is_bench_input(input_text):
        return run_tswn_bench_compare(input_text)
    return run_tswn_fight_compare(input_text)


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
    if int(sqrtools.SQRTOOLS_VERSION.split(".")[1]) < 3:
        client.send_message(msg.reply_with("错误: 内部依赖库版本错误\n"))
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
            msg.reply_with("请输入名字\n为防止刷屏，一次只能转换一个名字\n")
        )
        return
    current_player = sqrtools.Name()
    if not current_player.load(raw_players[0]):
        client.send_message(msg.reply_with("错误: 名字载入失败\n"))
        return
    r = current_player.namebase[0:32]
    hpcache = "(" + ",".join(str(i) for i in r[0:10]) + ")\n"
    r[0:10] = sorted(r[0:10])
    cache.write("HP: " + str(154 + sum(r[3:7])) + " / " + str(154 + sum(r[4:8])) + "\n")
    cache.write(hpcache)
    propcnt = 1
    for i in range(10, 31, 3):
        cache.write(sqrtools.propname[propcnt] + ": ")
        cache.write(" ".join(str(j).zfill(2) for j in r[i : i + 3]) + " ")
        r[i : i + 3] = sorted(r[i : i + 3])
        cache.write("-> " + str(r[i + 1] + 36) + " / " + str(r[i + 2] + 36) + "\n")
        propcnt += 1
    cache.write("\n")
    current_player.calcskill(False)
    doubleflag = -1
    for i in range(15, -1, -1):
        if current_player.nameskill[i][1] > 0 and current_player.nameskill[i][0] < 25:
            doubleflag = i
            break
    for i in range(16):
        _ = cache.write(
            "#"
            + str(i).zfill(2)
            + " "
            + sqrtools.sklname[current_player.nameskill[i][0]]
        )
        if current_player.nameskill[i][0] >= 35:
            _ = cache.write("\n")
        else:
            r = current_player.namebase[i * 4 + 64 : i * 4 + 68]
            _ = cache.write(
                ": "
                + " ".join(str(j).zfill(2) for j in r)
                + " -> "
                + str(current_player.nameskill[i][1]).zfill(2)
                + " / "
            )
            r = sorted(r)
            if i < 14:
                if doubleflag == i:
                    cache.write(
                        str((r[1] - 10) * 2 if r[1] > 10 else 0).zfill(2)
                        + "\n    ↑末尾主动\n"
                    )
                else:
                    cache.write(str(r[1] - 10 if r[1] > 10 else 0).zfill(2) + "\n")
            else:
                if current_player.nameskill[i][1] > 0:
                    if doubleflag == i:
                        cache.write(
                            str((r[1] - 10) * 2 if r[1] > 10 else 0).zfill(2)
                            + "\n    ↑末尾主动\n"
                        )
                    else:
                        a = (
                            r[1]
                            - 10
                            + min(
                                [r[1] - 10]
                                + current_player.namebase[32 + i * 2 : 34 + i * 2]
                            )
                        )
                        b = (
                            r[0]
                            - 10
                            + min(
                                r[0] - 10,
                                max(current_player.namebase[32 + i * 2 : 34 + i * 2]),
                            )
                        )
                        cache.write(
                            str(a if a > b else b).zfill(2)
                            + "\n    ↑末尾座位加成: "
                            + " ".join(
                                str(j).zfill(2)
                                for j in current_player.namebase[
                                    32 + i * 2 : 34 + i * 2
                                ]
                            )
                            + "\n"
                        )
                else:
                    cache.write(str(r[1] - 10 if r[1] > 10 else 0).zfill(2) + "\n")
    cache.write("\n")
    reply = msg.reply_with(
        f"{cache.getvalue()}版本: {_version_} (sqrtools {sqrtools.SQRTOOLS_VERSION})"
    )
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
    tswn_result = run_tswn_compare(names)
    compare_line = (
        f"tswn: {tswn_result[0]}-{tswn_result[1]:.2f}s"
        if tswn_result is not None
        else ""
    )
    diff_line = (
        _compute_bench_diff(result[0], tswn_result[0]) or ""
        if tswn_result is not None
        else ""
    )
    client.send_message(
        msg.reply_with(join_non_empty(result[0], compare_line, diff_line, out_msg(result[1])))
    )


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
    tswn_results = []
    tswn_total_time = 0.0
    has_tswn_compare = USE_TSWN_COMPARE and resolve_tswn_runner() is not None
    start_time = time.time()
    for fight in fights:
        # 以 + 分割
        names = fight.split("+")
        if len(names) < 2:
            results.append(f"输入错误, 只有{len(names)} 个部分")
            if has_tswn_compare:
                tswn_results.append(f"输入错误, 只有{len(names)} 个部分")
            continue
        result = run_namerena("\n".join(names), fight_mode=True)[0]
        if result in names:
            results.append(f"{names.index(result)}")
        else:
            results.append(result)
        tswn_result = (
            run_tswn_cli("\n".join(names), "fight") if has_tswn_compare else None
        )
        if tswn_result is not None:
            tswn_results.append(summarize_tswn_fight_for_names(tswn_result[0], names))
            tswn_total_time += tswn_result[1]
    # 输出
    end_time = time.time()
    reply = msg.reply_with(
        join_non_empty(
            "|".join(results),
            f"tswn: {'|'.join(tswn_results)}-{tswn_total_time:.2f}s"
            if tswn_results
            else "",
            out_msg(end_time - start_time),
        )
    )
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
        tswn_result = run_tswn_bench_compare(runs)
        # 只取最后一行括号之前的内容
        last_line = result[0].split("\n")[-1]
        last_line = last_line.split("(")[0]

        tswn_score = tswn_result[0] if tswn_result is not None else ""
        tswn_cost = tswn_result[1] if tswn_result is not None else 0.0
        diff_line = (
            _compute_bench_diff(result[0], tswn_score)
            if tswn_result is not None
            else ""
        )

        results.append(
            [
                last_line,
                result[1],
                tswn_score,
                tswn_cost,
                diff_line,
            ]
        )
    end_time = time.time()
    content = "\n".join(
        join_non_empty(
            f"{score}-{cost_time:.2f}s",
            f"tswn: {tswn_score}-{tswn_cost:.2f}s" if tswn_score else "",
            diff_line if diff_line else "",
        )
        for (score, cost_time, tswn_score, tswn_cost, diff_line) in results
    )
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
    has_tswn_compare = USE_TSWN_COMPARE and resolve_tswn_runner() is not None
    client.send_message(
        msg.reply_with(
            f"开始计算, 预计一个至少需要11s的时间, 大约需要 {len(names) * 11}s"
            + ("\n已启用 tswn 对比, 总耗时会更久" if has_tswn_compare else "")
        )
    )
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
        tswn_scores = []
        tswn_all_time = 0.0
        diffs = []
        for run in runs:
            if name.strip() == "":
                continue
            name = name.split("+")
            name = "\n".join(name)
            bench = run.format(test=name)
            result = run_namerena(bench)
            tswn_result = run_tswn_bench_compare(bench)
            cost_time = result[1]
            all_time += cost_time
            # 只取最后一行括号之前的内容
            last_line = result[0].split("\n")[-1]
            last_line = last_line.split("(")[0]
            if last_line.endswith(".00"):
                last_line = last_line[:-3]
            scores.append(last_line)
            if tswn_result is not None:
                tswn_scores.append(tswn_result[0])
                tswn_all_time += tswn_result[1]
                diff_line = _compute_bench_diff(result[0], tswn_result[0])
                diffs.append(diff_line if diff_line else "")
            else:
                diffs.append("")
        if all(x.isdigit() for x in scores):
            scores.append(str(sum(map(int, scores))))
        results.append(["|".join(scores), all_time, tswn_scores, tswn_all_time, diffs])
    end_time = time.time()
    content = "\n".join(
        join_non_empty(
            f"{score}-{cost_time:.2f}s",
            f"tswn: {' | '.join(tswn_scores)}-{tswn_cost:.2f}s" if tswn_scores else "",
            f"diff: {' | '.join(diffs)}" if any(diffs) else "",
        )
        for (score, cost_time, tswn_scores, tswn_cost, diffs) in results
    )
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
    global \
        USE_BUN, \
        USE_TSWN_COMPARE, \
        TSWN_CLI_PATH, \
        TSWN_RUNNER, \
        TSWN_RUNNER_FAILED, \
        VERSION_CACHE

    main_cfg = PLUGIN_MANIFEST.config_unchecked("main")
    USE_BUN = main_cfg.get_value("use_bun") or False
    use_tswn_compare = main_cfg.get_value("use_tswn_compare")
    USE_TSWN_COMPARE = True if use_tswn_compare is None else bool(use_tswn_compare)
    TSWN_CLI_PATH = str(main_cfg.get_value("tswn_cli_path") or "")
    TSWN_RUNNER = None
    TSWN_RUNNER_FAILED = False
    VERSION_CACHE = {}
    # conn = get_db_connection()
    # conn.close()
