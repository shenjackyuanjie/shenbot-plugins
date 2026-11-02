"""指定群的历史命令只读源；原文仅在内存传递，不输出凭据或消息样例。"""
from __future__ import annotations

import os
import re
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

BEIJING = timezone(timedelta(hours=8))
# 不漏掉未知旧命令，但仅提取消息开头的 /namer 词族，不读普通聊天。
PREFIX_PATTERN = r"^(/namer[^[:space:]]*)([[:space:]]|$)"
KNOWN_COMMANDS = frozenset({
    "/namer", "/namerena", "/namer-help", "/namer-peek", "/namer-peeks",
    "/namer-base", "/namer-team", "/namer-teams", "/namer-fight", "/namer-todiy",
    "/namer-pp", "/namer-pd", "/namer-qp", "/namer-qd", "/namer-pf",
    "/namer-cqp", "/namer-cp", "/namer-fp", "/namer-wcp", "/namer-fsp", "/namer-pjp",
})
# 7f489c2 的 rhp 与 fsp 都使用 teammate_pj.toml/head=4，现名称为 fsp。
ALIASES = {"/namer_peeks": "/namer-peeks", "/namer_teams": "/namer-teams",
           "/namer-rhp": "/namer-fsp"}


class SourceError(RuntimeError):
    """仅允许固定分类，不把数据库异常/配置内容放进错误。"""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def default_source_config():
    return Path(os.environ.get(
        "ICA_DB_QUERY_CONFIG",
        str(Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
            / "ica-db-query/config.toml"),
    ))


def canonical_command(value):
    """接受首命令 token 或完整消息；未知命令保留为 None，交执行器显式跳过。"""
    match = re.match(r"^(/namer[^\s]*)(?:\s|$)", value)
    if not match:
        return None
    command = ALIASES.get(match[1], match[1])
    return command if command in KNOWN_COMMANDS else None


def load_replica_settings(path):
    """沿用 ica-db-query 的本地配置和地址校验；绝无 primary 路径。"""
    try:
        with Path(path).open("rb") as stream:
            config = tomllib.load(stream)
        schema = config["schema"]
        if not isinstance(schema, str) or not re.fullmatch(r"eqq\d+", schema):
            raise ValueError()
        endpoint = config["replica"]
        with Path(endpoint["source_config"]).open("rb") as stream:
            original = tomllib.load(stream)
        url = urlsplit(original["database"]["url"])
        if (url.scheme not in ("postgres", "postgresql")
                or url.hostname != endpoint["expected_host"]
                or (url.port or 5432) != endpoint["expected_port"]):
            raise ValueError()
        # 丢弃原库 options/query；数据库固定为已配置从库的 icalingua。
        dsn = urlunsplit(url._replace(path="/icalingua", query="", fragment=""))
        return schema, {"conninfo": dsn}
    except (OSError, ValueError, KeyError, TypeError):
        raise SourceError("source_config_invalid") from None


@dataclass(frozen=True)
class HistoryMessage:
    message_id: str = field(repr=False)
    sender_id: str | None = field(repr=False)
    requested_at: datetime
    content: str = field(repr=False)
    command: str | None
    raw_command: str = field(repr=False)
    source_alias: str
    room_id: int = field(repr=False)
    platform: str = "ica"
    account: None = None

    @property
    def time_ms(self):
        return (self.requested_at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)


class HistorySource:
    def __init__(self, source_config=None, *, group, store_alias="ica_history_replica"):
        if type(group) is not int or not 0 < group < 2**63:
            raise SourceError("invalid_source_group")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", store_alias):
            raise SourceError("invalid_source_alias")
        self.group, self.room_id = group, -group
        self.store_alias = self.source_alias = store_alias
        self.schema, self._connection = load_replica_settings(source_config or default_source_config())

    @contextmanager
    def _db(self):
        try:
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(
                **self._connection, connect_timeout=5, row_factory=dict_row,
                options="-c default_transaction_read_only=on -c search_path=pg_catalog "
                        "-c statement_timeout=8000 -c lock_timeout=1000",
            ) as connection:
                connection.read_only = True
                try:
                    yield connection
                finally:
                    connection.rollback()
        except SourceError:
            raise
        except Exception:  # noqa: BLE001 - 隐私边界不外传驱动/连接错误
            raise SourceError("source_read_failed") from None

    def _select(self, tail):
        from psycopg import sql

        return sql.SQL(""" FROM {} WHERE "roomId"=%s
            AND deleted IS NOT TRUE AND hide IS NOT TRUE
            AND reveal IS NOT TRUE AND flash IS NOT TRUE
            AND "time" IS NOT NULL AND content ~ %s""").format(
                sql.Identifier(self.schema, "messages")) + sql.SQL(tail)

    def inventory(self):
        """只查询本群命令的统计；未知 token 合并，不让名字混进统计输出。"""
        from psycopg import sql

        query = sql.SQL("""SELECT (regexp_match(content,%s))[1] AS command,
            to_char(to_timestamp("time" / 1000.0) AT TIME ZONE 'Asia/Shanghai','YYYY-MM') AS month,
            count(*) AS count, min("time") AS first_ms, max("time") AS last_ms""")
        query += self._select(" GROUP BY 1,2 ORDER BY 2,1")
        with self._db() as connection:
            rows = connection.execute(query, (PREFIX_PATTERN, self.room_id, PREFIX_PATTERN)).fetchall()
        by_command, by_month = {}, {}
        first, last, count, unsupported = None, None, 0, 0
        for row in rows:
            raw = row["command"]
            canonical = canonical_command(raw)
            key = raw if canonical is not None else "unsupported"
            size = row["count"]
            by_command[key] = by_command.get(key, 0) + size
            by_month[row["month"]] = by_month.get(row["month"], 0) + size
            count += size
            unsupported += size if canonical is None else 0
            first = row["first_ms"] if first is None else min(first, row["first_ms"])
            last = row["last_ms"] if last is None else max(last, row["last_ms"])
        def local_time(value):
            return datetime.fromtimestamp(value / 1000, BEIJING).isoformat() if value is not None else None
        return {
            "count": count, "by_command": by_command, "by_month": by_month,
            "first_time": local_time(first), "last_time": local_time(last),
            "unsupported_commands": unsupported, "truncated_messages": 0,
            "input_parse_failures": None,
            "note": "统计仅覆盖当前从库可见命令；输入解析未在元数据统计中执行，不保证复制完整。",
        }

    def iter_messages(self, batch_size=100, *, after=None, before=None):
        """按 (time,_id) 升序分页；after/before 是开区间游标，默认固定当前最高水位。"""
        from psycopg import sql

        if type(batch_size) is not int or not 1 <= batch_size <= 500:
            raise SourceError("invalid_batch_size")
        for cursor in (after, before):
            if cursor is not None and (not isinstance(cursor, tuple) or len(cursor) != 2
                    or type(cursor[0]) is not int or not isinstance(cursor[1], str)
                    or not cursor[1] or len(cursor[1]) > 255):
                raise SourceError("invalid_source_cursor")
        inclusive = before is None
        if before is None:
            query = sql.SQL('SELECT _id AS message_id, "time" AS time_ms')
            query += self._select(' ORDER BY "time" DESC,_id DESC LIMIT 1')
            with self._db() as connection:
                row = connection.execute(query, (self.room_id, PREFIX_PATTERN)).fetchone()
            if row is None:
                return
            before = row["time_ms"], row["message_id"]
        while True:
            tail, params = '', [self.room_id, PREFIX_PATTERN]
            if after is not None:
                tail += ' AND ("time",_id)>(%s,%s)'
                params.extend(after)
            tail += ' AND ("time",_id)' + ('<=' if inclusive else '<') + '(%s,%s)'
            params.extend(before)
            tail += ' ORDER BY "time" ASC,_id ASC LIMIT %s'
            params.append(batch_size)
            query = sql.SQL('SELECT _id AS message_id, "senderId" AS sender_id, "time" AS time_ms,content')
            query += self._select(tail)
            with self._db() as connection:
                rows = connection.execute(query, params).fetchall()
            for row in rows:
                content = row["content"]
                raw = re.match(r"^(/namer[^\s]*)(?:\s|$)", content)[1]
                yield HistoryMessage(
                    message_id=row["message_id"], sender_id=row["sender_id"],
                    requested_at=datetime.fromtimestamp(row["time_ms"] / 1000, UTC),
                    content=content, command=canonical_command(raw), raw_command=raw,
                    source_alias=self.source_alias, room_id=self.room_id,
                )
            if len(rows) < batch_size:
                return
            after = rows[-1]["time_ms"], rows[-1]["message_id"]
