"""请求隔离、后台生命周期和实际进程调用的结构化记录。"""
from __future__ import annotations

import contextvars

PROCESS_TIMEOUT = contextvars.ContextVar("namerena_process_timeout", default=None)
PROCESS_DEADLINE = contextvars.ContextVar("namerena_process_deadline", default=None)
import json
import threading
import time
from pathlib import Path

from .archive import ArchiveError
from .model import (
    CURRENT,
    ITEM,
    VERSION,
    engine_input,
    file_snapshot,
    group,
    md5_text,
    parse_tswn,
    tswn_fight_text,
)


class Session:
    def __init__(self, archive, identity, payload):
        self.archive, self.identity, self.payload = archive, identity, payload
        self.calls = []
        self.errors = []
        self.pending = 1
        self.lock = threading.Lock()
        self.broken = False

    def begin(self, engine, operation, payload):
        if self.broken:
            raise ArchiveError()
        payload = {"version": VERSION, "input_indexes": ITEM.get(), **payload}
        metric = None
        if engine == "md5" and self.payload["command"].endswith("-pf"):
            count = sum(call["engine"] == "md5" and call["input_indexes"] == payload["input_indexes"] for call in self.calls)
            metric = ("pp", "pd", "qp", "qd")[count % 4]
        elif self.payload["command"].rsplit("-", 1)[-1] in ("pp", "pd", "qp", "qd"):
            metric = self.payload["command"].rsplit("-", 1)[-1]
        payload["metric"] = metric
        identity = self.archive.begin_call(self.identity, engine, operation, payload)
        entry = {"call_id": str(identity), "engine": engine, "operation": operation,
                 "input_indexes": payload["input_indexes"], "metric": metric, "state": "started", "output": None}
        self.calls.append(entry)
        return identity, entry, time.monotonic()

    def end(self, handle, output=None, error=None, exit_code=None):
        identity, entry, started = handle
        try:
            self.archive.finish_call(identity, output, error, exit_code, time.monotonic() - started)
        except ArchiveError:
            self.broken = True
            raise
        entry.update(output=output, error=error, state="failed" if error else "succeeded")

    def error(self, code, **params):
        self.errors.append({"code": code, "params": params})

    def release(self):
        with self.lock:
            self.pending -= 1
            if self.pending:
                return
        if self.broken:
            return  # 保留 started，不把无法提交的结果伪报完成。
        result = self.summary()
        failures = bool(self.errors) or any(call["state"] != "succeeded" for call in self.calls) or any(item["state"] not in ("succeeded", "skipped") for item in result["items"])
        success = any(engine["state"] in ("succeeded", "partial") for item in result["items"] for engine in item["engines"].values())
        state = "partial" if failures and success else "failed" if failures else "succeeded"
        self.archive.finish_job(self.identity, state, result)


    def summary(self):
        items = [{"input_index": item["input_index"], "engines": {}} for item in self.payload["groups"]]
        for call in self.calls:
            if call["operation"] == "to-diy" and not self.payload["command"].endswith("todiy"):
                continue
            indexes = call["input_indexes"]
            if indexes is None:
                indexes = list(range(len(items)))
            output = call["output"] or {}
            values = output.get("items")
            for offset, index in enumerate(indexes):
                if index >= len(items):
                    continue
                engine = items[index]["engines"].setdefault(call["engine"], {
                    "state": "succeeded", "scores": {}, "values": [], "call_ids": [], "errors": []})
                engine["call_ids"].append(call["call_id"])
                if call["state"] != "succeeded":
                    engine["errors"].append(call.get("error") or {"code": "incomplete"})
                    engine["state"] = "partial" if engine["values"] or engine["scores"] else "failed"
                    continue
                value = values[offset] if values is not None and offset < len(values) else output
                if value.get("error"):
                    engine["errors"].append(value["error"])
                    engine["state"] = "partial" if engine["values"] else "failed"
                    continue
                engine["scores"].update(value.get("scores", {}))
                if call["metric"] and output.get("kind") == "score":
                    rounds = output.get("rounds_completed")
                    if rounds:
                        engine["scores"][call["metric"]] = output["score_accumulated"] * 10000 / rounds
                engine["values"].append(value)
                engine["state"] = "partial" if engine["errors"] else "succeeded"
        for item in items:
            engines = item["engines"]
            command = self.payload["command"]
            if command.endswith(("peek", "peeks", "team", "teams", "base")):
                expected = ["python"]
            elif command.endswith(("todiy", "cqp", "-cp", "-fp", "-wcp", "-fsp", "-pjp")):
                expected = ["tswn"]
            else:
                expected = ["md5"] + (["tswn"] if self.payload["settings"].get("tswn_compare") else [])
            for name in expected:
                if name not in engines:
                    engines[name] = {"state": "failed" if self.errors or name != "tswn" else "skipped",
                        "scores": {}, "values": [], "call_ids": [],
                        "errors": self.errors or [{"code": "engine_not_run", "params": {}}]}
            md5 = engines.get("md5", {}).get("scores", {})
            if self.payload["command"].endswith("-pf") and all(key in md5 for key in ("pp", "pd", "qp", "qd")):
                md5["sum"] = sum(md5[key] for key in ("pp", "pd", "qp", "qd"))
            tswn = engines.get("tswn", {}).get("scores", {})
            item["comparison"] = {key: {"signed_difference": md5[key] - tswn[key],
                "absolute_difference": abs(md5[key] - tswn[key])} for key in md5.keys() & tswn.keys()}
            states = [engine["state"] for engine in engines.values()]
            item["state"] = "partial" if "partial" in states or ("failed" in states and "succeeded" in states) else "failed" if not states or "failed" in states else "succeeded"
        return {"version": VERSION, "command": self.payload["command"], "items": items, "errors": self.errors}


class ClientProxy:
    def __init__(self, client, session):
        self.client, self.session = client, session

    def __getattr__(self, name):
        return getattr(self.client, name)

    def send_message(self, message):
        try:
            accepted = self.client.send_message(message)
        except Exception:
            self.session.archive.delivery(self.session.identity, {"state": "unknown", "time": time.time()})
            raise
        self.session.archive.delivery(self.session.identity, {
            "state": "accepted" if accepted else "not_accepted", "time": time.time()})
        return accepted


def background(target):
    session = CURRENT.get()
    context = contextvars.copy_context()
    if session:
        with session.lock:
            session.pending += 1

    def run():
        try:
            target()
        except ArchiveError:
            if session:
                session.broken = True
        except Exception as exc:  # noqa: BLE001 - 后台任务边界必须记录未知失败并收尾
            if session:
                session.error("background_failed", exception_type=type(exc).__name__)
        finally:
            if session:
                session.release()
    try:
        threading.Thread(target=lambda: context.run(run), daemon=True).start()
    except Exception:
        if session:
            session.release()
        raise


def local_output(value):
    session = CURRENT.get()
    if session:
        session.local_result = {"version": VERSION, **value}


def local_call(function):
    def invoke(msg, client, *args, **kwargs):
        session = CURRENT.get()
        if not session:
            return function(msg, client, *args, **kwargs)
        handle = session.begin("python", function.__name__, {"request": session.payload})
        session.local_result = None
        try:
            result = function(msg, client, *args, **kwargs)
        except ArchiveError:
            raise
        except Exception as exc:
            session.end(handle, error={"code": "local_failed", "params": {"type": type(exc).__name__}})
            raise
        if session.local_result is None:
            session.end(handle, error={"code": "invalid_input", "params": {}})
        else:
            session.end(handle, session.local_result)
        return result
    return invoke


def process(run, command, *, engine_version=None, **kwargs):
    session = CURRENT.get()
    if session is None:
        return run(command, **kwargs)
    command = list(command)
    if PROCESS_TIMEOUT.get() is not None:
        remaining = PROCESS_TIMEOUT.get()
        if PROCESS_DEADLINE.get() is not None:
            remaining = min(remaining, PROCESS_DEADLINE.get() - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("request_timeout")
        kwargs.setdefault("timeout", remaining)
    md5 = any("md5-api.js" in str(part) for part in command)
    if md5:
        engine, operation = "md5", command[2]
        payload = engine_input(kwargs.get("input", ""))
        payload["settings"]["rounds_requested"] = int(command[3])
        command.append("--json")
    else:
        engine = "tswn"
        position = next((i for i, part in enumerate(command) if part in ("fight", "bench", "to-diy", "namer-pf")), None)
        if position is None:
            raise ArchiveError("unknown_engine_operation")
        operation = command[position]
        if operation == "bench":
            operation = command[position + 1]
        if operation == "pair" and "-n" in command:
            command[command.index("-n") + 1] = str(session.payload["settings"].get("pair_rounds", command[command.index("-n") + 1]))
        payload = engine_input(kwargs.get("input", ""))
        params = {}
        for flag, field in (("-n", "rounds_requested"), ("--head", "head")):
            if flag in command:
                params[field] = int(command[command.index(flag) + 1])
        for flag in ("--target-factored", "--teammate-factored", "--keep-rq"):
            params[flag[2:].replace("-", "_")] = flag in command
        payload["settings"].update(params)
        if operation == "fight":
            command.extend(["--jsonl", "--max-rounds", "20000"])
            payload["settings"]["max_rounds_requested"] = 20000
        if operation in ("pair", "cqp"):
            command.append("--log")
        for flag, field in (("-l", "targets"), ("--teammate-list", "teammates")):
            if flag in command:
                payload[field] = file_snapshot(command[command.index(flag) + 1])
        source_flag = "-p" if operation in ("pair", "cqp") else "-f" if operation == "to-diy" else None
        if source_flag:
            text = Path(command[command.index(source_flag) + 1]).read_text(encoding="utf-8")
            payload["groups"] = [group(line, "diy" if operation == "to-diy" else "plus")
                                 for line in text.splitlines() if line.strip()]
    if md5:
        payload["runtime_version"] = engine_version
        runner_path = Path(next(str(part) for part in command if "md5-api.js" in str(part)))
        payload["engine_asset"] = file_snapshot(runner_path.with_name("md5.js"))
    else:
        payload["engine_version"] = engine_version
    handle = session.begin(engine, operation, payload)
    try:
        result = run(command, **kwargs)
    except Exception as exc:
        session.end(handle, error={"code": "process_failed", "params": {"type": type(exc).__name__}})
        raise
    if result.returncode:
        session.end(handle, error={"code": "engine_exit", "params": {}}, exit_code=result.returncode)
        return result
    try:
        if md5:
            output = json.loads(result.stdout)
            if output.get("version") != VERSION or output.get("kind") not in ("fight", "score", "win_rate"):
                raise ValueError("invalid_md5_output")
            result.stdout = md5_text(output)
        elif operation == "to-diy":
            path = Path(command[command.index("-o") + 1])
            output = {"version": VERSION, "kind": "diy", "items": [
                {"input_index": i, **group(line, "plus")} for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()) if line.strip()]}
            if len(output["items"]) != len(payload["groups"]):
                raise ValueError("result_count_mismatch")
        elif operation in ("pair", "cqp"):
            path = Path(command[command.index("-o") + 1])
            items = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if len(items) != len(payload["groups"]):
                raise ValueError("result_count_mismatch")
            lines = []
            for i, item in enumerate(items):
                label = item.pop("label")
                item["input_index"] = i
                for pair in item.get("top_pairs", []):
                    pair["teammate"] = group(pair["teammate"], "diy")
                lines.append(f"{item.get('score', item.get('avg_win_rate')):.3f} {label}")
            output = {"version": VERSION, "kind": operation, "items": items, "precision": "engine_reported"}
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            output = parse_tswn(result.stdout, operation)
            if operation == "namer-pf" and len(output["items"]) != len(payload["groups"]):
                raise ValueError("result_count_mismatch")
            if operation == "fight":
                result.stdout = tswn_fight_text(output)
        session.end(handle, output, exit_code=result.returncode)
    except ArchiveError:
        raise
    except (ValueError, KeyError, TypeError, OSError):
        session.end(handle, error={"code": "parse_failed", "params": {"operation": operation}}, exit_code=result.returncode)
        result.returncode = 1
        result.stderr = "引擎结果无法解析"
    return result
