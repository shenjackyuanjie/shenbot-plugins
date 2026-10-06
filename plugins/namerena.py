from __future__ import annotations

import io
import json
import re
import shutil
import sys
import time
import traceback
import subprocess
import tempfile
import threading

from pathlib import Path
from typing import TYPE_CHECKING
from shenbot_api import PluginManifest, ConfigStorage

if str(Path(__file__).parent.absolute()) not in sys.path:
    sys.path.append(str(Path(__file__).parent.absolute()))

import name_utils
import sqrtools

TELEMETRY = False
try:
    import psycopg  # noqa: F401 - 仅用于检测可选遥测依赖

    TELEMETRY = True
except ImportError:
    pass

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib

if TYPE_CHECKING:
    from ica_typing import (
        IcaNewMessage,
        IcaClient,
        ReciveMessage,
        TailchatClient,
        TailchatReciveMessage,
    )

VERSION = "0.10.4"

CMD_PREFIX = "/namer"

EVAL_CMD = "/namerena"
EVAL_SIMPLE_CMD = f"{CMD_PREFIX}"  # 用于简化输入
# 评分四兄弟
EVAL_PP_CMD = f"{CMD_PREFIX}-pp"
EVAL_PD_CMD = f"{CMD_PREFIX}-pd"
EVAL_QP_CMD = f"{CMD_PREFIX}-qp"
EVAL_QD_CMD = f"{CMD_PREFIX}-qd"
EVAL_PF_CMD = f"{CMD_PREFIX}-pf"
CQP_CMD = f"{CMD_PREFIX}-cqp"
PAIR_COMMANDS = {
    f"{CMD_PREFIX}-cp": ("刺评", "teammate_fz.toml", 4),
    f"{CMD_PREFIX}-fp": ("辅评", "teammate_bc.toml", 4),
    f"{CMD_PREFIX}-wcp": ("无刺评", "teammate_wc.toml", 4),
    f"{CMD_PREFIX}-fsp": ("分身评", "teammate_pj.toml", 4),
    f"{CMD_PREFIX}-pjp": ("配件评", "teammate_fs.toml", 4),
}
CONVERT_CMD = f"{CMD_PREFIX}-peek"
CONVERT_RAW_CMD = f"{CMD_PREFIX}-peeks"
BASE_CMD = f"{CMD_PREFIX}-base"
TEAM_CMD = f"{CMD_PREFIX}-team"
TEAM_RAW_CMD = f"{CMD_PREFIX}-teams"
FIGHT_CMD = f"{CMD_PREFIX}-fight"
HELP_CMD = f"{CMD_PREFIX}-help"

HELP_MSG = f"""namerena-v[{VERSION}]
名字竞技场 一款不建议入坑的文字类游戏
PF
- {HELP_CMD} - 查看帮助
- {EVAL_CMD} - 运行名字竞技场, 每一行是一个输入, 输入格式与网页版相同
- {EVAL_SIMPLE_CMD} - 简化输入
- {CMD_PREFIX}-[pd|pd|qp|qd] - 你懂的评分
    - 一行一个名字/+连接的多个名字
- {EVAL_PF_CMD} - 一下子全评
    - 一行一个名字/+连接的多个名字
- {CQP_CMD} - 使用 tswn 计算 100% CQP, 一行一组，双人组用 + 连接
- /namer-cp /namer-fp /namer-wcp /namer-fsp /namer-pjp - Openbox 队友评测
- {CONVERT_CMD} - 查看一个名字的属性, 每一行一个名字
- {CONVERT_RAW_CMD} - 查看一个名字的属性, 技能按原始顺序输出
- {BASE_CMD} - base 工具, 只支持单个名字 (避免刷屏)
- {TEAM_CMD} - 查看同队加成后的属性和熟练度, 每行一个名字
- {TEAM_RAW_CMD} - 查看同队加成后的属性和熟练度, 技能按原始顺序输出
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
    # tswn-openbox 资产目录 (含 targets/ 与 teammates/ 的那一层),
    # 留空则自动在 tswn-cli 附近与仓库相对位置查找
    tswn_assets_path="",
)

PLUGIN_MANIFEST = PluginManifest(
    plugin_id="namer",
    name="名竞小工具",
    version=VERSION,
    description="namerena 的一堆小工具",
    authors=["shenjack"],
    config={"main": cfg},
)

USE_BUN = False
USE_TSWN_COMPARE = True
TSWN_CLI_PATH = ""
TSWN_ASSETS_PATH = ""
TSWN_RUNNER: tuple[list[str], str | None] | None = None
TSWN_RUNNER_FAILED = False
TSWN_COMPARE_ROUNDS = 10000
VERSION_CACHE: dict[tuple[str, ...], str | None] = {}
# 后台线程完成计算后据此判断插件是否已被卸载，避免向旧插件代回发消息
_shutdown = threading.Event()


def out_msg(cost_time: float) -> str:
    runtime = get_js_runtime()
    use_bun = runtime == "bun"
    runtime_label = runtime or "no-runtime"
    lines = [f"耗时: {cost_time:.3f}s", f"版本: {VERSION}-{runtime_label}"]

    runtime_version = get_runtime_version()
    if runtime_version:
        lines.append(f"{runtime}: {runtime_version}")
    elif runtime is None:
        lines.append("运行时: 未找到 Node.js 或 Bun")

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
    repo_candidates = [
        workspace_root.parent / "tswn-new",
        workspace_root.parent / "tswn-core",
        workspace_root.parent.parent / "namer" / "tswn-core",
    ]
    for tswn_repo in repo_candidates:
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
    runtime = get_js_runtime()
    if runtime is None:
        return None
    return resolve_command_version([runtime])


def get_js_runtime() -> str | None:
    preferred_runtime = "bun" if USE_BUN else "node"
    fallback_runtime = "node" if USE_BUN else "bun"
    if shutil.which(preferred_runtime) is not None:
        return preferred_runtime
    if shutil.which(fallback_runtime) is not None:
        return fallback_runtime
    return None


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


PF_LABELS = ("pp", "pd", "qp", "qd", "sum")


def _parse_pf_score_row(text: str) -> list[int] | None:
    parts = text.strip().split("|")
    if len(parts) < len(PF_LABELS) - 1:
        return None
    try:
        values = [int(float(part.strip())) for part in parts[: len(PF_LABELS)]]
    except ValueError:
        return None
    if len(values) == len(PF_LABELS) - 1:
        values.append(sum(values))
    return values


def _parse_tswn_pf_rows(output: str) -> list[list[int]]:
    rows = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line == "pp|pd|qp|qd":
            continue
        row = _parse_pf_score_row(line)
        if row is not None:
            rows.append(row)
    return rows


def _compute_pf_diff_lines(
    md5_score_rows: list[str], tswn_output: str
) -> list[str]:
    tswn_rows = _parse_tswn_pf_rows(tswn_output)
    diff_lines = []
    multi_row = len(md5_score_rows) > 1

    for idx, md5_score in enumerate(md5_score_rows):
        md5_row = _parse_pf_score_row(md5_score)
        tswn_row = tswn_rows[idx] if idx < len(tswn_rows) else None
        if md5_row is None or tswn_row is None:
            prefix = f"diff[{idx + 1}]" if multi_row else "diff"
            diff_lines.append(f"{prefix}: 无法解析")
            continue

        diffs = [
            (label, abs(md5_value - tswn_value))
            for label, md5_value, tswn_value in zip(PF_LABELS, md5_row, tswn_row)
            if md5_value != tswn_value
        ]
        if diffs:
            prefix = f"diff[{idx + 1}]" if multi_row else "diff"
            diff_lines.append(
                f"{prefix}: "
                + ", ".join(f"{label}={diff}" for label, diff in diffs)
            )

    if not diff_lines:
        return ["diff = 0"]
    return diff_lines


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


def run_tswn_pf_compare(input_text: str) -> tuple[str, float] | None:
    result = run_tswn_cli(input_text, "namer-pf", "-n", str(TSWN_COMPARE_ROUNDS))
    if result is None:
        return None
    return result[0], result[1]


def run_tswn_compare(input_text: str) -> tuple[str, float] | None:
    if is_bench_input(input_text):
        return run_tswn_bench_compare(input_text)
    return run_tswn_fight_compare(input_text)


def _tswn_asset_roots() -> list[Path]:
    """收集可能包含 tswn 仓库的根目录，按优先级排列。

    先看已解析的 tswn-cli：它的 cwd 和二进制所在目录的每一级祖先。
    再退回插件文件往上数第 4/3/2 层，覆盖把 tswn 检出放在工作区
    或工作区同级目录的本地布局。
    """
    candidates: list[Path] = []
    runner = resolve_tswn_runner()
    if runner is not None:
        command, cwd = runner
        if cwd:
            candidates.append(Path(cwd))
        candidates.extend(Path(command[0]).resolve().parents)
    candidates.extend(
        Path(__file__).resolve().parents[index] for index in (3, 2, 1)
    )
    return candidates


def _iter_tswn_repo_bases() -> list[Path]:
    """把候选根展开成实际要探测的 tswn 仓库目录，并去重。"""
    seen: set[Path] = set()
    bases: list[Path] = []
    for root in _tswn_asset_roots():
        for base in (root, root / "tswn-new", root / "tswn-core"):
            resolved = base.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            bases.append(resolved)
    return bases


def _configured_asset_dirs() -> list[Path]:
    """tswn_assets_path 指定的候选资产目录。

    允许填资产目录本身（含 targets/ 与 teammates/ 的那一层），
    也允许填仓库根目录，两种写法都能命中。
    """
    raw = TSWN_ASSETS_PATH.strip()
    if not raw:
        return []
    base = Path(raw).expanduser()
    return [
        base,
        base / "assets",
        base / "crates" / "tswn_openbox" / "assets",
        base / "crates" / "tswn_openbox_backend" / "assets",
    ]


def _iter_tswn_asset_dirs() -> list[Path]:
    """按优先级返回候选资产目录：显式配置的最优先，其次自动探测。"""
    seen: set[Path] = set()
    dirs: list[Path] = []

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            dirs.append(resolved)

    for configured in _configured_asset_dirs():
        add(configured)
    for base in _iter_tswn_repo_bases():
        for package in ("tswn_openbox", "tswn_openbox_backend"):
            add(base / "crates" / package / "assets")
    return dirs


def _find_tswn_openbox_assets() -> tuple[Path, Path] | None:
    """Locate tswn-openbox's standard single/double CQP target lists."""
    for assets in _iter_tswn_asset_dirs():
        targets = assets / "targets"
        new_target1 = targets / "newTarget1.toml"
        new_target2 = targets / "newTarget2.toml"
        if new_target1.is_file() and new_target2.is_file():
            return new_target1, new_target2
        target1 = targets / "target1.txt"
        target2 = targets / "target2.txt"
        if target1.is_file() and target2.is_file():
            return target1, target2
    return None


def _find_tswn_teammate_file(filename: str) -> Path | None:
    assets = _find_tswn_backend_assets()
    if assets is None:
        return None
    path = assets / "teammates" / filename
    return path if path.is_file() else None


def _find_tswn_backend_assets() -> Path | None:
    for assets in _iter_tswn_asset_dirs():
        if (assets / "settings.toml").is_file():
            return assets
    return None


def _prepare_weighted_target_diy(source: Path, output: Path) -> None:
    data = tomllib.loads(source.read_text(encoding="utf-8"))
    targets = data.get("targets", [])
    raw_groups = [target.get("players", []) for target in targets]
    with tempfile.TemporaryDirectory(prefix="namer_target_diy_") as temp:
        raw_file = Path(temp) / "players.txt"
        diy_file = Path(temp) / "players_diy.txt"
        raw_file.write_text("\n".join("+".join(players) for players in raw_groups) + "\n", encoding="utf-8")
        _run_tswn_to_diy_file(raw_file, diy_file)
        converted_groups = [line.strip() for line in diy_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(converted_groups) != len(raw_groups):
        raise RuntimeError("靶子 DIY 转换后的组数量不一致")

    decoder = json.JSONDecoder()

    def split_converted_group(line: str) -> list[str]:
        values = []
        cursor = 0
        while cursor < len(line):
            if line[cursor] == "+":
                cursor += 1
            marker = line.find("+ol:", cursor)
            if marker < 0:
                raise RuntimeError("无法解析 tswn DIY 组输出")
            name = line[cursor:marker]
            json_start = marker + len("+ol:")
            _, json_end = decoder.raw_decode(line, json_start)
            values.append(name + line[marker:json_end])
            cursor = json_end
        return values

    converted = [split_converted_group(line) for line in converted_groups]
    lines = []
    for target, players in zip(targets, converted):
        factor = target.get("factor")
        lines.append("[[targets]]")
        lines.append(f"factor = {factor!r}")
        lines.append("players = [" + ", ".join(json.dumps(value, ensure_ascii=False) for value in players) + "]")
        lines.append("")
    output.write_text("\n".join(lines), encoding="utf-8")


def _run_tswn_to_diy_file(input_file: Path, output_file: Path) -> None:
    runner = resolve_tswn_runner()
    if runner is None:
        raise RuntimeError("未找到 tswn-cli，无法转换 DIY")
    command, cwd = runner
    result = subprocess.run(
        [*command, "to-diy", "-f", str(input_file), "-o", str(output_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"tswn DIY 转换失败（exit {result.returncode}）\n{details[-800:]}")


def run_tswn_pair_scores(players: list[str], teammate_file: Path, head: int) -> str:
    """Calculate latest Openbox weighted pair scores with DIY-frozen targets."""
    assets = _find_tswn_backend_assets()
    if assets is None:
        raise RuntimeError(
            "未找到最新 tswn-openbox_backend 资源；"
            "请在 namer 配置里把 tswn_assets_path 指到 tswn-openbox 的资产目录"
        )
    target2 = assets / "targets" / "newTarget2.toml"
    if not target2.is_file():
        raise RuntimeError(f"未找到最新 newTarget2.toml（已在 {assets}）")

    runner = resolve_tswn_runner()
    if runner is None:
        raise RuntimeError("未找到 tswn-cli，无法计算队友评")
    command, cwd = runner
    with tempfile.TemporaryDirectory(prefix="namer_pair_") as temp:
        temp_root = Path(temp)
        players_in = temp_root / "players.txt"
        target_diy = temp_root / "target_diy.toml"
        scores_file = temp_root / "scores.txt"
        players_in.write_text("\n".join(players) + "\n", encoding="utf-8")
        _prepare_weighted_target_diy(target2, target_diy)
        result = subprocess.run(
            [
                *command,
                "bench",
                "pair",
                "-l",
                str(target_diy),
                "--target-factored",
                "-p",
                str(players_in),
                "--teammate-list",
                str(teammate_file),
                "--teammate-factored",
                "--head",
                str(head),
                "-n",
                "1000",
                "--keep-rq",
                "-o",
                str(scores_file),
                "-f",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"tswn 队友评计算失败（exit {result.returncode}）\n{details[-800:]}")
        score_lines = [line for line in scores_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(score_lines) != len(players):
            raise RuntimeError(f"tswn 返回结果数量不一致: {len(score_lines)} / {len(players)}")
        return "\n".join(
            f"{line.split(None, 1)[0]} {player}"
            for player, line in zip(players, score_lines)
            if line.split(None, 1)
        )


def cmd_pair_rating(
    msg: ReciveMessage, client: IcaClient | TailchatClient, command: str
) -> None:
    if "\n" not in msg.content:
        client.send_message(msg.reply_with(f"请使用 {command} 命令，然后换行输入待测名字，每行一个"))
        return
    raw = msg.content[len(command) :]
    raw = raw[raw.find("\n") + 1 :]
    players = [line.strip() for line in raw.splitlines() if line.strip()]
    if not players:
        client.send_message(msg.reply_with("请输入至少一个待测名字"))
        return
    _, teammate_name, head = PAIR_COMMANDS[command]
    teammate_file = _find_tswn_teammate_file(teammate_name)
    if teammate_file is None:
        client.send_message(msg.reply_with(f"找不到队友评文件: {teammate_name}"))
        return

    def calculate() -> None:
        try:
            output = run_tswn_pair_scores(players, teammate_file, head)
        except Exception as exc:
            output = f"队友评计算失败: {exc}"
        if not _shutdown.is_set():
            client.send_message(msg.reply_with(output))

    threading.Thread(target=calculate, daemon=True).start()


def _run_tswn_cqp_file(player_file: Path, target_file: Path, target_factored: bool = False) -> list[str]:
    runner = resolve_tswn_runner()
    if runner is None:
        raise RuntimeError("未找到 tswn-cli，无法计算 CQP")
    command, cwd = runner
    with tempfile.TemporaryDirectory(prefix="namer_cqp_") as temp:
        output_file = Path(temp) / "cqp.txt"
        result = subprocess.run(
            [
                *command,
                "bench",
                "cqp",
                "-l",
                str(target_file),
                "-p",
                str(player_file),
                *( ["--target-factored"] if target_factored else [] ),
                "-n",
                str(TSWN_COMPARE_ROUNDS),
                "--keep-rq",
                "-o",
                str(output_file),
                "-f",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"tswn CQP 计算失败（exit {result.returncode}）\n{details[-800:]}")
        if not output_file.exists():
            raise RuntimeError("tswn CQP 没有生成结果文件")
        return [line for line in output_file.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_tswn_cqp(raw_groups: list[str]) -> str:
    """Run 100% CQP for mixed one-player/two-player input groups."""
    assets = _find_tswn_openbox_assets()
    if assets is None:
        raise RuntimeError(
            "未找到 tswn-openbox 的 target1.txt/target2.txt；"
            "请在 namer 配置里把 tswn_assets_path 指到 tswn-openbox 的资产目录"
        )
    target1, target2 = assets
    indexed: dict[int, list[tuple[int, str]]] = {1: [], 2: []}
    for index, group in enumerate(raw_groups):
        count = len([part for part in group.split("+") if part.strip()])
        if count not in indexed:
            raise ValueError(f"第 {index + 1} 组必须是单人组或双人组（使用 + 连接）")
        indexed[count].append((index, group))

    results: dict[int, str] = {}
    for count, entries in indexed.items():
        if not entries:
            continue
        with tempfile.TemporaryDirectory(prefix="namer_cqp_players_") as temp:
            player_file = Path(temp) / "players.txt"
            player_diy_file = Path(temp) / "players_diy.txt"
            player_file.write_text("\n".join(group for _, group in entries) + "\n", encoding="utf-8")
            _run_tswn_to_diy_file(player_file, player_diy_file)
            target = target1 if count == 1 else target2
            target_for_run = target
            target_factored = target.suffix.lower() == ".toml"
            if target_factored:
                target_for_run = Path(temp) / f"target{count}_diy.toml"
                _prepare_weighted_target_diy(target, target_for_run)
            for (index, original), result in zip(entries, _run_tswn_cqp_file(player_diy_file, target_for_run, target_factored)):
                parts = result.split(None, 1)
                results[index] = f"{parts[0]} {original}" if parts else original
    return "\n".join(results[index] for index in range(len(raw_groups)))


def cmd_cqp(msg: ReciveMessage, client: IcaClient | TailchatClient) -> None:
    if "\n" not in msg.content:
        client.send_message(msg.reply_with(f"请使用 {CQP_CMD} 命令，然后换行输入名字组，每行一组，双人组用 + 连接"))
        return
    raw = msg.content[len(CQP_CMD) :]
    raw = raw[raw.find("\n") + 1 :]
    groups = [line.strip() for line in raw.splitlines() if line.strip()]
    if not groups:
        client.send_message(msg.reply_with("请输入至少一组名字"))
        return

    def calculate() -> None:
        try:
            output = run_tswn_cqp(groups)
        except Exception as exc:
            output = f"CQP 计算失败: {exc}"
        if not _shutdown.is_set():
            client.send_message(msg.reply_with(output))

    threading.Thread(target=calculate, daemon=True).start()


def convert_name(
    msg: ReciveMessage, client: IcaClient | TailchatClient, sort_skills: bool = True
) -> None:
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
        cache.write(player.display(sort_skills=sort_skills))
        cache.write("\n")
    reply = msg.reply_with(f"{cache.getvalue()}版本:{VERSION}")
    client.send_message(reply)


def _team_upgrade_player(player: name_utils.Player, other: name_utils.Player) -> None:
    """Apply tswn-core's same-team raw name-base upgrade to one player."""
    raw = player._team_raw_name_base
    other_raw = other._team_raw_name_base

    for index in range(7, 128):
        if (
            other_raw[index - 1] == raw[index]
            and other_raw[index] > player.name_base[index]
        ):
            player.name_base[index] = other_raw[index]

    # This is the special two-position rule used when the player's name is
    # also their team name (the same condition as tswn-core's upgrade()).
    if player.name == player.team:
        for index in range(5, 128):
            if (
                other_raw[index - 2] == raw[index]
                and other_raw[index] > player.name_base[index]
            ):
                player.name_base[index] = other_raw[index]


def _team_attrs(player: name_utils.Player) -> list[int]:
    """Return [attack, defense, speed, agility, magic, resistance, wisdom, HP]."""
    values = player.name_base[:32]
    for offset in range(10, 31, 3):
        values[offset : offset + 3] = sorted(values[offset : offset + 3])
    values[0:10] = sorted(values[0:10])
    return [
        *(values[offset + 1] + 36 for offset in range(10, 31, 3)),
        154 + sum(values[3:7]),
    ]


def _team_skill_levels(player: name_utils.Player) -> dict[int, int]:
    """Recompute final normal skill levels after a team name-base upgrade."""
    levels = [0] * 40
    boosted = [False] * 40

    for slot, offset in enumerate(range(64, 128, 4)):
        small = min(player.name_base[offset : offset + 4])
        skill_id = player.skl_id[slot]
        if small <= 10 or skill_id >= 35:
            continue
        levels[skill_id] = small - 10
        boosted[skill_id] = min(player._team_raw_name_base[offset : offset + 4]) <= 10

    # Last active skill (the same reverse action-order scan as tswn-core).
    for slot in range(15, -1, -1):
        skill_id = player.skl_id[slot]
        if skill_id >= 25 or levels[skill_id] == 0 or boosted[skill_id]:
            continue
        levels[skill_id] *= 2
        boosted[skill_id] = True
        break

    # Slot 14/15 passive boosts.
    for slot, left, right in ((14, 60, 61), (15, 62, 63)):
        skill_id = player.skl_id[slot]
        if skill_id >= 35 or levels[skill_id] == 0 or boosted[skill_id]:
            continue
        amount = min(player.name_base[left], player.name_base[right], levels[skill_id])
        levels[skill_id] += amount
        boosted[skill_id] = True

    return {
        skill_id: levels[skill_id]
        for skill_id in player.skl_id[:16]
        if levels[skill_id] > 0
    }


def _team_display(
    player: name_utils.Player,
    original_attrs: list[int],
    original_skills: dict[int, int],
    sort_skills: bool = True,
) -> str:
    attrs = _team_attrs(player)
    attr_names = ("攻", "防", "速", "敏", "魔", "抗", "智")
    hp_bonus = attrs[7] - original_attrs[7]
    hp_suffix = f"(+{hp_bonus})" if hp_bonus > 0 else ""
    parts = [f"{player.name}@{player.team}", f"HP:{attrs[7]}{hp_suffix}"]
    for index, label in enumerate(attr_names):
        value = attrs[index]
        bonus = value - original_attrs[index]
        suffix = f"(+{bonus})" if bonus > 0 else ""
        parts.append(f"{label}:{value}{suffix}")
    parts.append(f"八围:{sum(attrs[:7]) + round(attrs[7] / 3)}")

    skills = _team_skill_levels(player)
    skill_items = sorted(skills.items(), key=lambda item: item[1], reverse=True) if sort_skills else skills.items()
    skill_text = "|".join(
        f"{name_utils.sklname[skill_id]}:{level}"
        + (
            f"(+{level - original_skills.get(skill_id, 0)})"
            if level > original_skills.get(skill_id, 0)
            else ""
        )
        for skill_id, level in skill_items
    )
    return "|".join(parts) + (f"\n{skill_text}" if skill_text else "")


def convert_team(
    msg: ReciveMessage, client: IcaClient | TailchatClient, sort_skills: bool = True
) -> None:
    if "\n" not in msg.content:
        client.send_message(
            msg.reply_with(
                f"请使用 {TEAM_CMD} 命令，然后换行输入名字，例如：\n"
                f"{TEAM_CMD}\n名字A@队伍\n名字B@队伍"
            )
        )
        return

    names = msg.content[len(TEAM_CMD) :]
    names = names[names.find("\n") + 1 :]
    raw_players = [line.strip() for line in names.splitlines() if line.strip()]
    if not raw_players:
        client.send_message(msg.reply_with("请输入至少一个名字\n"))
        return

    players = []
    original_attrs = []
    original_skills = []
    for raw_name in raw_players:
        player = name_utils.Player()
        if any(len(part.encode("utf-8")) > 255 for part in raw_name.split("@")):
            client.send_message(msg.reply_with(f"错误: 名字或队伍名超过 255 字节: {raw_name}"))
            return
        if not player.load(raw_name):
            client.send_message(msg.reply_with(f"错误: 名字载入失败: {raw_name}"))
            return
        player._team_raw_name_base = player.name_base[:]
        players.append(player)
        original_attrs.append(_team_attrs(player))
        original_skills.append(_team_skill_levels(player))

    ordered = sorted(players, key=lambda player: f"{player.name}@{player.team}")
    for left_index in range(len(ordered)):
        for right_index in range(left_index + 1, len(ordered)):
            left = ordered[left_index]
            right = ordered[right_index]
            if left.team != right.team:
                continue
            _team_upgrade_player(left, right)
            _team_upgrade_player(right, left)

    output = "\n".join(
        _team_display(player, original_attrs[index], original_skills[index], sort_skills=sort_skills)
        for index, player in enumerate(players)
    )
    client.send_message(msg.reply_with(f"{output}\n版本: {VERSION}"))


def convert_base(msg: ReciveMessage, client: IcaClient | TailchatClient) -> None:
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
        raw_prop = " ".join(str(j).zfill(2) for j in r[i : i + 3])
        r[i : i + 3] = sorted(r[i : i + 3])
        cache.write(
            str(r[i + 1] + 36)
            + " / "
            + str(r[i + 2] + 36)
            + " <- "
            + raw_prop
            + "\n"
        )
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
            raw_skill = " ".join(str(j).zfill(2) for j in r)
            r = sorted(r)
            skill_result = ""
            if i < 14:
                if doubleflag == i:
                    skill_result = str((r[1] - 10) * 2 if r[1] > 10 else 0).zfill(2)
                    skill_result += "\n    ↑末尾主动"
                else:
                    skill_result = str(r[1] - 10 if r[1] > 10 else 0).zfill(2)
            else:
                if current_player.nameskill[i][1] > 0:
                    if doubleflag == i:
                        skill_result = str((r[1] - 10) * 2 if r[1] > 10 else 0).zfill(2)
                        skill_result += "\n    ↑末尾主动"
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
                        skill_result = (
                            str(a if a > b else b).zfill(2)
                            + "\n    ↑末尾座位加成: "
                            + " ".join(
                                str(j).zfill(2)
                                for j in current_player.namebase[32 + i * 2 : 34 + i * 2]
                            )
                        )
                else:
                    skill_result = str(r[1] - 10 if r[1] > 10 else 0).zfill(2)
            cache.write(
                ": "
                + str(current_player.nameskill[i][1]).zfill(2)
                + " / "
                + skill_result.split("\n", 1)[0]
                + " <- "
                + raw_skill
                + ("\n" + skill_result.split("\n", 1)[1] if "\n" in skill_result else "")
                + "\n"
            )
    cache.write("\n")
    reply = msg.reply_with(
        f"{cache.getvalue()}版本: {VERSION} (sqrtools {sqrtools.SQRTOOLS_VERSION})"
    )
    client.send_message(reply)


def run_namerena(input_text: str, fight_mode: bool = False) -> tuple[str, float]:
    """运行namerena"""
    root_path = Path(__file__).parent
    runner_path = (root_path / "md5" / "md5-api.js").resolve()
    if not runner_path.exists():
        return "未找到namerena运行文件", 0.0
    runtime = get_js_runtime()
    if runtime is None:
        return "未找到 JavaScript 运行时，请安装 Node.js 或 Bun", 0.0
    run_cmd = [runtime, str(runner_path), "any", str(TSWN_COMPARE_ROUNDS)]
    if fight_mode:
        run_cmd[2] = "fight"

    start_time = time.time()
    try:
        result = subprocess.run(
            run_cmd,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            encoding="utf-8",
            cwd=root_path / "md5",
        )
        if result.returncode == 0:
            result = result.stdout
        else:
            result = result.stderr
    except Exception as e:
        result = f"发生错误: {e}\n{traceback.format_exc()}"
    end_time = time.time()
    return result.strip(), end_time - start_time


def eval_fight(msg: ReciveMessage, client: IcaClient | TailchatClient) -> None:
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


def run_fights(msg: ReciveMessage, client: IcaClient | TailchatClient) -> None:
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


def score_all(msg: ReciveMessage, client: IcaClient | TailchatClient) -> None:
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
    tswn_pf_result = run_tswn_pf_compare(content) if has_tswn_compare else None
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
            cost_time = result[1]
            all_time += cost_time
            # 只取最后一行括号之前的内容
            last_line = result[0].split("\n")[-1]
            last_line = last_line.split("(")[0]
            if last_line.endswith(".00"):
                last_line = last_line[:-3]
            scores.append(last_line)
            diffs.append("")
        if all(x.isdigit() for x in scores):
            scores.append(str(sum(map(int, scores))))
        results.append(["|".join(scores), all_time, tswn_scores, tswn_all_time, diffs])
    end_time = time.time()
    content = "\n".join(
        join_non_empty(
            f"{score}-{cost_time:.2f}s",
            "",
            f"diff: {' | '.join(diffs)}" if any(diffs) else "",
        )
        for (score, cost_time, tswn_scores, tswn_cost, diffs) in results
    )
    tswn_line = (
        f"tswn:\n{tswn_pf_result[0]}-{tswn_pf_result[1]:.2f}s"
        if tswn_pf_result is not None
        else ""
    )
    pf_diff_line = (
        "\n".join(
            _compute_pf_diff_lines(
                [score for (score, *_rest) in results],
                tswn_pf_result[0],
            )
        )
        if tswn_pf_result is not None
        else ""
    )
    reply = msg.reply_with(
        join_non_empty(
            f"pp|pd|qp|qd\n{content}",
            tswn_line,
            pf_diff_line,
            out_msg(end_time - start_time),
        )
    )
    client.send_message(reply)


def dispatch_msg(msg: ReciveMessage, client: IcaClient | TailchatClient) -> None:
    if msg.is_reply or msg.is_from_self:
        return
    if msg.content == HELP_CMD:
        reply = msg.reply_with(HELP_MSG)
        client.send_message(reply)
    elif msg.content.startswith(EVAL_CMD):
        eval_fight(msg, client)
    elif msg.content.startswith(FIGHT_CMD):
        run_fights(msg, client)
    elif msg.content.startswith(CQP_CMD):
        cmd_cqp(msg, client)
    elif next((command for command in PAIR_COMMANDS if msg.content.startswith(command)), None) is not None:
        command = next(command for command in PAIR_COMMANDS if msg.content.startswith(command))
        cmd_pair_rating(msg, client, command)
    elif msg.content.startswith(CONVERT_RAW_CMD):
        # 长命令放前面，避免被 /namer-peek 的前缀匹配抢走
        convert_name(msg, client, sort_skills=False)
    elif msg.content.startswith(CONVERT_CMD):
        convert_name(msg, client)
    elif msg.content.startswith(TEAM_RAW_CMD):
        convert_team(msg, client, sort_skills=False)
    elif msg.content.startswith(TEAM_CMD):
        convert_team(msg, client)
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
        # 同时过滤掉别的 /namer-xxxxx / /namer_xxxxx
        if not msg.content.startswith(f"{EVAL_SIMPLE_CMD}-") and not msg.content.startswith(f"{EVAL_SIMPLE_CMD}_"):
            eval_fight(msg, client)


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    dispatch_msg(msg, client)  # type: ignore


def on_tailchat_message(
    msg: TailchatReciveMessage, client: TailchatClient
) -> None:
    dispatch_msg(msg, client)  # type: ignore


def on_load() -> None:
    global \
        USE_BUN, \
        USE_TSWN_COMPARE, \
        TSWN_CLI_PATH, \
        TSWN_ASSETS_PATH, \
        TSWN_RUNNER, \
        TSWN_RUNNER_FAILED, \
        VERSION_CACHE

    main_cfg = PLUGIN_MANIFEST.config_unchecked("main")
    USE_BUN = main_cfg.get_value("use_bun") or False
    use_tswn_compare = main_cfg.get_value("use_tswn_compare")
    USE_TSWN_COMPARE = True if use_tswn_compare is None else bool(use_tswn_compare)
    TSWN_CLI_PATH = str(main_cfg.get_value("tswn_cli_path") or "")
    TSWN_ASSETS_PATH = str(main_cfg.get_value("tswn_assets_path") or "")
    TSWN_RUNNER = None
    TSWN_RUNNER_FAILED = False
    VERSION_CACHE = {}
    _shutdown.clear()
    # conn = get_db_connection()
    # conn.close()


def on_unload() -> None:
    """卸载前置位 _shutdown，让仍在跑的后台计算线程不再回发消息。"""
    _shutdown.set()
