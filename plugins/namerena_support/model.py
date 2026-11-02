"""请求语义和进程适配；展示字符串仅在内存中使用。"""
from __future__ import annotations

import json
import re
from contextvars import ContextVar
from pathlib import Path

VERSION = 1
CURRENT = ContextVar("namerena_request", default=None)
ITEM = ContextVar("namerena_item", default=None)


def split_parts(text, separator="+"):
    """只在 JSON/引号/括号之外切分，技能值中的 + 不是组分隔符。"""
    parts, start, index = [], 0, 0
    decoder = json.JSONDecoder()
    while index < len(text):
        if text.startswith("+ol:", index):
            _, end = decoder.raw_decode(text, index + 4)
            index = end
            continue
        if text.startswith("+diy[", index):
            _, end = decoder.raw_decode(text, index + 4)
            if text[end:].lstrip().startswith("{"):
                end += len(text[end:]) - len(text[end:].lstrip())
                _, end = decoder.raw_decode(text, end)
            index = end
            continue
        if text.startswith(separator, index):
            parts.append(text[start:index])
            index += len(separator)
            start = index
            continue
        index += 1
    parts.append(text[start:])
    return [part for part in parts if part]


def member(text):
    overlays = []
    decoder = json.JSONDecoder()
    while True:
        marker = re.search(r"\+(ol:|diy\[)", text)
        if marker is None:
            break
        start = marker.start()
        if marker.group(1) == "ol:":
            value, end = decoder.raw_decode(text, marker.end())
            overlays.append({"format": "ol", "value": value})
        else:
            attrs, end = decoder.raw_decode(text, marker.end() - 1)
            skills = None
            if text[end:].lstrip().startswith("{"):
                end += len(text[end:]) - len(text[end:].lstrip())
                skills, end = decoder.raw_decode(text, end)
            overlays.append({"format": "diy", "attrs": attrs, "skills": skills})
        text = text[:start] + text[end:]
    pieces = text.split("+")
    identity = pieces.pop(0)
    identity_parts = identity.split("@")
    return {"identity_parts": identity_parts, "effective_identity": None,
            "weapons": pieces, "overlays": overlays}


def group(line, mode="plus"):
    if mode == "member":
        pieces = [line]
    elif len(split_parts(line, "++")) > 1:
        pieces = split_parts(line, "++")
    else:
        pieces = []
        for part in split_parts(line):
            if part.startswith(("ol:", "diy[")) and pieces:
                pieces[-1] += "+" + part
            else:
                pieces.append(part)
        if mode == "diy" and not all("@" in part.split("+", 1)[0] for part in pieces):
            pieces = [line]
    return {"members": [{"member_index": i, **member(part)} for i, part in enumerate(pieces)]}


def parse_input(text, command, rounds):
    body = text[len(command):]
    local_mode = command.endswith(("peek", "peeks", "team", "teams", "base"))
    if local_mode:
        body = body.partition("\n")[2] if "\n" in body else ""
        trim = command.endswith(("team", "teams"))
        values = [line.strip() if trim else line for line in body.split("\n")]
        groups = []
        for value in values:
            if not value:
                continue
            name, at, team = value.rpartition("@")
            groups.append({"input_index": len(groups), "members": [{"member_index": 0,
                "name": name if at else value, "team": (team or name) if at else value,
                "team_explicit": bool(at), "weapons": [], "overlays": []}]})
        return {"version": VERSION, "command": command, "groups": groups, "settings": {}}
    body = body.partition("\n")[2] if "\n" in body else body
    lines = body.lstrip("\ufeff").splitlines()
    mode = "member" if command.endswith(("peek", "peeks", "team", "teams", "base")) else "plus"
    if command.endswith("todiy"):
        mode = "diy"
    groups = []
    if command in ("/namer", "/namerena"):
        parsed = engine_input(body)
        return {"version": VERSION, "command": command, "groups": [{"input_index": i, **item} for i, item in enumerate(parsed["groups"])], "settings": {"rounds_requested": rounds, **parsed["settings"]}}
    bench, bang = False, False
    for line in lines:
        if line.strip() == "!test!":
            bench = True
            continue
        if line.strip() == "!":
            bang = True
            continue
        if not line.strip():
            continue
        seed = None
        if command.endswith("fight"):
            pieces = split_parts(line)
            if len(pieces) > 2 and pieces[-1].startswith("seed:") and pieces[-1].endswith("@!"):
                seed = {"kind": "seed_control", "value": pieces.pop()[5:-2]}
                line = "+".join(pieces)
        groups.append({"input_index": len(groups), **group(line.strip() if command.endswith(("todiy", "cqp", "cp", "fp", "wcp", "fsp", "pjp")) else line, mode), "seed": seed})
    return {"version": VERSION, "command": command, "groups": groups,
            "settings": {"rounds_requested": rounds, "bench": bench, "bang": bang}}


def engine_input(text):
    """无损词法结构，不冒充 md5/tswn 各自的有效身份解析。

    identity_parts 保留全部 @ 段及空白；控制号/继承队名由引擎解释。
    没有空行时每行是独立组；有空行时显式空行分组。
    """
    lines = text.lstrip("\ufeff").split("\n")
    settings = {"bench": bool(lines and lines[0].strip() == "!test!"), "bang": False,
                "effective_seed": None, "identity_semantics": "engine_defined"}
    if settings["bench"]:
        lines = lines[1:]
        settings["bang"] = bool(lines and lines[0].strip() == "!")
        if settings["bang"]:
            lines = lines[1:]
    while lines and not lines[-1]:
        lines.pop()
    explicit = "" in lines
    groups, members = [], []
    for line in lines:
        if not line:
            if members:
                groups.append({"members": members})
                members = []
            continue
        if not explicit and members:
            groups.append({"members": members})
            members = []
        value = member(line)
        identity = value["identity_parts"]
        if len(identity) > 1 and identity[1] == "!" and identity[0].startswith("seed:"):
            value["control"] = {"kind": "seed", "value": identity[0][5:]}
        value["member_index"] = len(members)
        members.append(value)
    if members:
        groups.append({"members": members})
    return {"groups": groups, "settings": settings,
            "grouping": "blank_lines" if explicit else "one_line_per_group"}


def numeric(value):
    number = float(value)
    if not (-float("inf") < number < float("inf")):
        raise ValueError("non_finite_result")
    return number


def parse_tswn(text, operation):
    if operation == "fight":
        for line in reversed(text.splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result":
                result = event["data"]
                result["final_states"] = [{key: item[key] for key in
                    ("id", "id_name", "base_name", "team_index", "hp", "alive", "active", "attack", "defense", "speed", "agility", "magic", "resistance", "wisdom") if key in item}
                    for item in result.get("final_states", [])]
                return {"version": VERSION, "kind": "fight", "result": result}
        raise ValueError("missing_fight_result")
    if operation == "namer-pf":
        rows = []
        keys = ("pp", "pd", "qp", "qd", "sum")
        scores = {}
        label = None
        mode = None
        for line in text.splitlines():
            if re.fullmatch(r"-?\d+(?:\.\d+)?(?:\s*\|\s*-?\d+(?:\.\d+)?){3,4}", line.strip()):
                if mode == "named":
                    raise ValueError("mixed_pf_formats")
                mode = "pipe"
                values = [numeric(value) for value in line.split("|")]
                rows.append({"input_index": len(rows), "scores": dict(zip(keys, values))})
                continue
            match = re.fullmatch(r"(.*)\s+(pp|pd|qp|qd|sum):\s*(-?\d+(?:\.\d+)?)", line)
            if match:
                if mode == "pipe":
                    raise ValueError("mixed_pf_formats")
                mode = "named"
                name, metric, value = match.groups()
                if metric != keys[len(scores)] or (scores and name != label):
                    raise ValueError("incomplete_pf_result")
                label = name
                scores[metric] = numeric(value)
                if metric == "sum":
                    rows.append({"input_index": len(rows), "scores": scores})
                    scores = {}
        if not rows or scores:
            raise ValueError("invalid_pf_result")
        return {"version": VERSION, "kind": "pf", "items": rows, "precision": "engine_reported"}
    metrics = []
    for label, value, wins, total in re.findall(
            r"(普通评分|!评分|胜率|汇总胜率):\s*([\d.]+)(?:%| / 10000)\s*\((\d+)/(\d+)\)", text):
        metrics.append({"metric": {"普通评分": "normal_score", "!评分": "bang_score",
                                   "胜率": "win_rate_percent", "汇总胜率": "aggregate_win_rate_percent"}[label],
                        "value": numeric(value), "wins": int(wins), "rounds_completed": int(total)})
    if not metrics:
        raise ValueError("invalid_bench_result")
    return {"version": VERSION, "kind": "bench", "metrics": metrics, "precision": "engine_reported"}


def md5_text(output):
    kind = output["kind"]
    if kind == "fight":
        winner = output["winner"]
        return winner if output.get("fight_only") else f"赢家:|{winner}|"
    rounds = output["rounds_completed"] or output["rounds_requested"]
    if kind == "win_rate":
        return f"最终胜率:|{output['wins'] * 100 / rounds:.4f}%|({rounds}轮)"
    score = output["score_accumulated"] * 10000 / rounds
    return f"分数:|{score:.2f}|({rounds}轮)\n{score:.2f}({rounds})"


def tswn_fight_text(output):
    result = output["result"]
    # JSON 里的 winner_ids 是引擎玩家 ID，不能冒充用户输入序号。
    winners = result.get("winner_ids", [])
    names = [item.get("id_name", "") for item in result.get("final_states", []) if item.get("id") in winners]
    index = result.get("winner_team_indices", [])
    return "赢家:\n" + "\n".join("- " + name for name in names) + f"\nwin_idx={index}"


def file_snapshot(path):
    import hashlib
    data = Path(path).read_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
