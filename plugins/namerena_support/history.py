"""只消费历史原命令的离线重算，不使用消息 client 或旧回复。"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from .archive import ArchiveError
from .model import file_snapshot, member
from .runtime import PROCESS_DEADLINE, PROCESS_TIMEOUT


class InputError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Settings:
    rounds: int = 10000
    pair_rounds: int = 1000
    timeout: float = 600
    request_timeout: float = 3600

    def __post_init__(self):
        if min(self.rounds, self.pair_rounds, self.timeout, self.request_timeout) <= 0:
            raise ValueError("轮数与超时必须大于零")


TEMPLATES = {"pp": "!test!\n\n{test}", "pd": "!test!\n\n{test}\n{test}",
             "qp": "!test!\n!\n\n{test}", "qd": "!test!\n!\n\n{test}\n{test}"}


class EngineExecutor:
    """复用插件低层计算与结构化 Session；模块由入口先加载，避免热重载导入错代。"""
    def __init__(self, module, settings=None):
        settings = settings or Settings()
        self.module, self.settings = module, settings
        module.TSWN_COMPARE_ROUNDS = settings.rounds
        module.USE_TSWN_COMPARE = True

    def snapshot(self):
        module = self.module
        runner = module.resolve_tswn_runner()
        if runner is None or len(runner[0]) != 1 or not Path(runner[0][0]).is_file():
            raise InputError("fixed_tswn_binary_required")
        assets = module._find_tswn_backend_assets()
        if assets is None:
            raise InputError("assets_not_found")
        runtime = module.get_js_runtime()
        if runtime is None:
            raise InputError("runtime_not_found")
        import shutil
        runtime_path = shutil.which(runtime)
        files = {"tswn": (Path(runner[0][0]), file_snapshot(runner[0][0])),
                 "runtime": (Path(runtime_path), file_snapshot(runtime_path))}
        asset_root = Path(__file__).resolve().parent
        for path in (Path(module.__file__), Path(__file__), Path(__file__).with_name("model.py"), Path(__file__).with_name("runtime.py"), Path(__file__).with_name("history_source.py")):
            files["executor/" + path.name] = (path, file_snapshot(path))
        for name in ("md5.js", "md5-api.js"):
            path = asset_root / "md5" / name
            files[name] = (path, file_snapshot(path))
        for path in sorted(assets.rglob("*")):
            if path.is_file():
                files["assets/" + path.relative_to(assets).as_posix()] = (path, file_snapshot(path))
        self._files = files
        self._stats = {name: (path.stat().st_mtime_ns, path.stat().st_size) for name, (path, _) in files.items()}
        # 版本查询只读、也有界；填入原插件缓存后计算不再额外启动版本进程。
        import subprocess
        for command, cwd in (([runtime], None), runner):
            result = subprocess.run([*command, "--version"], cwd=cwd, capture_output=True,
                text=True, encoding="utf-8", timeout=self.settings.timeout, check=False)
            if result.returncode:
                raise InputError("version_query_failed")
            module.VERSION_CACHE[(*command, f"cwd={cwd or ''}")] = module.last_non_empty_line(result.stdout or result.stderr)
        return {name: value for name, (_, value) in files.items()}

    def verify(self):
        for name, (path, expected) in self._files.items():
            actual = (path.stat().st_mtime_ns, path.stat().st_size)
            if actual != self._stats[name]:
                if file_snapshot(path) != expected:
                    raise InputError("engine_or_asset_changed")
                self._stats[name] = actual

    def execute(self, command, body, payload):
        module = self.module
        deadline = time.monotonic() + self.settings.request_timeout

        def invoke(function, *args, **kwargs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InputError("request_timeout")
            token = PROCESS_TIMEOUT.set(min(self.settings.timeout, remaining))
            try:
                return function(*args, **kwargs)
            finally:
                PROCESS_TIMEOUT.reset(token)

        lines = [line for line in body.split("\n") if line.strip()]
        suffix = command.removeprefix("/namer-")
        if command == "/namer-help":
            return
        if not lines:
            raise InputError("invalid_empty_input")
        if command in ("/namer", "/namerena"):
            invoke(module.run_namerena, body)
            invoke(module.run_tswn_compare, body)
        elif suffix in TEMPLATES or suffix == "pf":
            if suffix == "pf":
                invoke(module.run_tswn_pf_compare, body)
            for index, line in enumerate(lines):
                module.ITEM.set([index])
                names = "\n".join(module.split_parts(line))
                for metric in TEMPLATES if suffix == "pf" else (suffix,):
                    bench = TEMPLATES[metric].format(test=names)
                    invoke(module.run_namerena, bench)
                    if suffix != "pf":
                        invoke(module.run_tswn_bench_compare, bench)
        elif suffix == "fight":
            for index, line in enumerate(lines):
                module.ITEM.set([index])
                names = module.split_parts(line)
                if len(names) < 2:
                    module.CURRENT.get().error("invalid_fight", input_index=index)
                    continue
                invoke(module.run_namerena, "\n".join(names), fight_mode=True)
                invoke(module.run_tswn_cli, "\n".join(names), "fight")
        elif suffix == "todiy":
            invoke(module._run_tswn_to_diy_names, lines)
        elif suffix == "cqp":
            invoke(module.run_tswn_cqp, lines)
        elif command in module.PAIR_COMMANDS:
            _, filename, head = module.PAIR_COMMANDS[command]
            teammate = module._find_tswn_teammate_file(filename)
            if teammate is None:
                raise InputError("teammate_not_found")
            invoke(module.run_tswn_pair_scores, lines, teammate, head)
        elif suffix in ("peek", "peeks", "team", "teams", "base"):
            self._local(suffix, body)
        else:
            raise InputError("unsupported_command")

    def _local(self, suffix, body):
        module = self.module
        session = module.CURRENT.get()
        handle = session.begin("python", "history_" + suffix, {"request": session.payload})
        try:
            values = [line for line in body.split("\n") if line]
            if suffix in ("team", "teams"):
                values = [line.strip() for line in values if line.strip()]
            if suffix == "base":
                if len(values) != 1:
                    raise InputError("invalid_base_count")
                player = module.sqrtools.Name()
                if not player.load(values[0]):
                    raise InputError("invalid_player")
                player.calcprops(False)
                player.calcskill(False)
                output = {"kind": "base", "items": [{"input_index": 0, "name_base": player.namebase[:],
                    "attributes": player.nameprop[:], "skills": [{"slot": i, "id": skill, "level": level}
                    for i, (skill, level) in enumerate(player.nameskill)]}]}
            else:
                players, attrs, skills = [], [], []
                for value in values:
                    player = module.name_utils.Player()
                    if not player.load(value):
                        raise InputError("invalid_player")
                    player._team_raw_name_base = player.name_base[:]
                    players.append(player)
                    attrs.append(module._team_attrs(player))
                    skills.append(module._team_skill_levels(player))
                if suffix in ("team", "teams"):
                    ordered = sorted(players, key=lambda p: f"{p.name}@{p.team}")
                    for index, left in enumerate(ordered):
                        for right in ordered[index + 1:]:
                            if left.team == right.team:
                                module._team_upgrade_player(left, right)
                                module._team_upgrade_player(right, left)
                    output = {"kind": "team", "items": [{"input_index": i, "name": p.name, "team": p.team,
                        "before": {"attributes": attrs[i], "skills": skills[i]},
                        "after": {"attributes": module._team_attrs(p), "skills": module._team_skill_levels(p)},
                        "name_base": p.name_base[:], "skill_order": p.skl_id[:16]} for i, p in enumerate(players)]}
                else:
                    output = {"kind": "attributes", "items": [module._player_data(p, i) for i, p in enumerate(players)]}
            session.end(handle, {"version": 1, **output})
        except ArchiveError:
            raise
        except Exception as exc:
            session.end(handle, error={"code": getattr(exc, "code", "local_failed"), "params": {}})
            raise


def recompute(message, archive, executor, snapshot, *, mode="overwrite"):
    module, settings = executor.module, executor.settings
    command = message.command
    body = message.content.partition("\n")[2]
    error = None
    if command is None:
        error = "unsupported_command"
    elif command != "/namer-help" and "\n" not in message.content:
        error = "invalid_missing_newline"
    source_lines = []
    local = bool(command and command.endswith(("peek", "peeks", "team", "teams", "base")))
    for index, line in enumerate(body.split("\n")):
        try:
            syntax = {"kind": "literal_name", "value": line} if local else member(line)
            source_lines.append({"line_index": index, "syntax": syntax})
        except (ValueError, TypeError):
            source_lines.append({"line_index": index, "syntax": {"kind": "unparsed", "value": line},
                                 "error": {"code": "invalid_line"}})
            error = "invalid_input"
    try:
        payload = module.parse_input(command + "\n" + body, command, settings.rounds) if command else {"version": 1, "command": "unsupported", "groups": [], "settings": {}}
    except (ValueError, TypeError):
        payload = {"version": 1, "command": command or "unsupported", "groups": [], "settings": {}}
        error = "invalid_input"
    payload["source_lines"] = source_lines
    first_line = message.content.split("\n", 1)[0]
    raw_command = getattr(message, "raw_command", None)
    if raw_command and first_line.startswith(raw_command):
        payload["inline_arguments"] = {"kind": "unparsed_arguments", "value": first_line[len(raw_command):]}
    payload["settings"].update(rounds_requested=settings.rounds, pair_rounds=settings.pair_rounds,
                               tswn_compare=True, process_timeout=settings.timeout, request_timeout=settings.request_timeout)
    fingerprint = hashlib.sha256(json.dumps({"settings": payload["settings"], "assets": snapshot,
        "command": command, "input": payload, "parser_version": 1, "plugin_version": module.VERSION},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    source = {name: getattr(message, name) for name in
        ("platform", "room_id", "message_id", "sender_id", "source_alias", "requested_at")}
    provenance = {"origin": "history_recompute", "source_alias": message.source_alias,
                  "parser_version": 1, "fingerprint": fingerprint, "execution_snapshot": snapshot,
                  "reconstruction": False, "source_command_alias": getattr(message, "raw_command", command) if getattr(message, "raw_command", command) in ("/namer_peeks", "/namer_teams", "/namer-rhp") else command}
    executor.verify()  # 配置漂移停止整批，不能连续清空后续历史成功结果。
    with archive.history_job(source, payload, module.VERSION, provenance, mode) as writer:
        if writer is None:
            return "skipped"
        session = module.Session(writer, writer.identity, payload)
        token = module.CURRENT.set(session)
        item_token = module.ITEM.set(None)
        deadline_token = PROCESS_DEADLINE.set(time.monotonic() + settings.request_timeout)
        try:
            if error:
                session.error(error)
            else:
                executor.execute(command, body, payload)
        except ArchiveError:
            session.broken = True
            raise
        except Exception as exc:  # noqa: BLE001 - 离线单样本边界只记录分类，继续下一条
            session.error(getattr(exc, "code", "execution_failed"), exception_type=type(exc).__name__)
        finally:
            try:
                session.release()
            finally:
                module.CURRENT.reset(token)
                module.ITEM.reset(item_token)
                PROCESS_DEADLINE.reset(deadline_token)
        result = session.summary()
        states = [item["state"] for item in result["items"]]
        if session.errors or (states and all(state == "failed" for state in states)):
            return "failed"
        return "partial" if any(state != "succeeded" for state in states) else "succeeded"
