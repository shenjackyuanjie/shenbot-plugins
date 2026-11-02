"""从只读消息源提取原始命令，离线重算并覆盖 namer 存档；不发送消息。"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import tomllib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins"))


def positive(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return result


def shard_index(message, count):
    """按历史来源身份稳定分片；与内容、Python 哈希随机化和计算指纹无关。"""
    identity = [message.source_alias, message.platform, str(message.room_id), str(message.message_id)]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).digest()
    return int.from_bytes(digest[:8], "big") % count


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--group", type=positive, required=True, help="本次明确授权查询的单个群号")
    value.add_argument("--source-config", type=Path, help="ica-db-query 只读从库配置路径")
    value.add_argument("--source-alias", default="ica_history_replica")
    value.add_argument("--execute", action="store_true", help="明确开启重算与目标库写入；默认仅统计")
    value.add_argument("--target-config", type=Path, default=ROOT / "config" / "codex.toml")
    value.add_argument("--plugin-config", type=Path, default=ROOT / "config" / "namer.toml")
    value.add_argument("--target-database", choices=("namer",), default="namer")
    value.add_argument("--mode", choices=("overwrite", "resume", "retry-failed"), default="overwrite")
    value.add_argument("--commands", help="逗号分隔的规范命令，未指定则全部候选命令")
    value.add_argument("--limit", type=positive, help="本批最多处理的匹配样本数；未指定不限")
    value.add_argument("--batch-size", type=positive, default=100)
    value.add_argument("--shard-count", type=positive, default=1, help="独立进程的总分片数，所有进程保持一致")
    value.add_argument("--shard-index", type=int, default=0, help="本进程分片编号，从0开始；每个编号只启动一次")
    value.add_argument("--rounds", type=positive, default=10000)
    value.add_argument("--pair-rounds", type=positive, default=1000)
    value.add_argument("--timeout", type=positive, default=600, help="每个子进程秒数上限")
    value.add_argument("--request-timeout", type=positive, default=3600, help="每个样本计算总秒数上限")
    return value


def main(argv=None):
    arguments = parser()
    args = arguments.parse_args(argv)
    if not 0 <= args.shard_index < args.shard_count:
        arguments.error("--shard-index 必须在 0 到 --shard-count-1 之间")
    # namerena 导入时重建辅助模块，必须先导入入口，再绑定本轮 support 类型。
    import namerena
    from namerena_support.archive import Archive, ArchiveError
    from namerena_support.history import EngineExecutor, InputError, Settings, recompute
    from namerena_support.history_source import HistorySource, SourceError

    try:
        source = HistorySource(args.source_config, group=args.group, store_alias=args.source_alias)
        if not args.execute:
            print(json.dumps(source.inventory(), ensure_ascii=False, default=str))
            return 0
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        target = tomllib.loads(args.target_config.read_text(encoding="utf-8-sig"))
        raw = target.get("main", {}).get("pg_connection")
        if not isinstance(raw, str) or not raw:
            raise InputError("target_connection_missing")
        try:
            connection = make_conninfo(**{**conninfo_to_dict(raw), "dbname": args.target_database})
        except Exception:  # noqa: BLE001 - libpq解析错误可能包含原连接文本，统一脱敏
            raise InputError("target_connection_invalid") from None
        if args.plugin_config.exists():
            config = tomllib.loads(args.plugin_config.read_text(encoding="utf-8-sig")).get("main", {})
            namerena.USE_BUN = bool(config.get("use_bun", False))
            namerena.TSWN_CLI_PATH = config.get("tswn_cli_path") or ""
            namerena.TSWN_ASSETS_PATH = config.get("tswn_assets_path") or ""
        settings = Settings(args.rounds, args.pair_rounds, args.timeout, args.request_timeout)
        executor = EngineExecutor(namerena, settings)
        snapshot = executor.snapshot()
        archive = Archive(connection)
        with archive.db() as db:
            if db.info.dbname != "namer":
                raise InputError("target_database_mismatch")
        archive.initialize()
        commands = set(args.commands.split(",")) if args.commands else None
        print(json.dumps({"mode": args.mode, "rounds": settings.rounds, "pair_rounds": settings.pair_rounds,
            "limit": args.limit, "commands": sorted(commands) if commands else "all",
            "source": args.source_alias, "snapshot_files": len(snapshot), "workers": 1,
            "shard_count": args.shard_count, "shard_index": args.shard_index}, ensure_ascii=False), flush=True)
        counts = Counter()
        scanned = 0
        started = time.monotonic()
        for message in source.iter_messages(batch_size=args.batch_size):
            scanned += 1
            if commands is not None and message.command not in commands:
                continue
            if args.shard_count > 1 and shard_index(message, args.shard_count) != args.shard_index:
                continue
            if args.limit is not None and counts.total() - counts["skipped"] >= args.limit:
                break
            status = recompute(message, archive, executor, snapshot, mode=args.mode)
            counts[status] += 1
            executed = counts.total() - counts["skipped"]
            if status != "skipped" and executed % 10 == 0:
                print(json.dumps({"scanned": scanned, "executed": executed, "elapsed_seconds": round(time.monotonic() - started, 3), "counts": dict(counts)}, ensure_ascii=False), flush=True)
        print(json.dumps({"scanned": scanned, "executed": counts.total() - counts["skipped"], "elapsed_seconds": round(time.monotonic() - started, 3), "counts": dict(counts)}, ensure_ascii=False), flush=True)
        return 0
    except (ArchiveError, InputError, SourceError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": getattr(exc, "code", type(exc).__name__),
            "message": "已停止；不输出连接串、消息正文或驱动详情。请检查配置后断点继续。"}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
