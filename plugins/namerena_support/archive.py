"""独立的 PostgreSQL jobs/calls；不保存命令原文、输出文案或连接信息。"""
from __future__ import annotations

import re
from contextlib import contextmanager
from uuid import uuid4


class ArchiveError(RuntimeError):
    """固定错误分类，驱动错误与凭据不外传。"""

    def __init__(self, code="archive_unavailable"):
        self.code = code
        super().__init__(code)


class Archive:
    def __init__(self, connection: str, schema="namerena"):
        if not re.fullmatch(r"namerena|namerena_test_[0-9a-f]{16}", schema):
            raise ArchiveError("invalid_schema")
        self._connection = connection
        self.schema = schema

    @contextmanager
    def db(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
            with psycopg.connect(self._connection, connect_timeout=5, row_factory=dict_row,
                                  options="-c statement_timeout=10000 -c lock_timeout=3000") as db:
                yield db
        except ArchiveError:
            raise
        except Exception:  # noqa: BLE001 - 存档边界统一分类，不泄露驱动异常里的连接信息
            raise ArchiveError() from None

    def query(self, text):
        from psycopg import sql
        return sql.SQL(text).format(s=sql.Identifier(self.schema))

    @staticmethod
    def json(value):
        from psycopg.types.json import Jsonb
        return Jsonb(value)

    def initialize(self):
        with self.db() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("ica:" + self.schema,))
            tables = db.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s", (self.schema,)).fetchall()
            if tables:
                if {row["tablename"] for row in tables} != {"schema_version", "jobs", "calls"}:
                    raise ArchiveError("unknown_schema")
                version = db.execute(self.query("SELECT version FROM {s}.schema_version")).fetchall()
                if version != [{"version": 1}]:
                    raise ArchiveError("unsupported_schema_version")
                return
            db.execute(self.query("""
                CREATE SCHEMA IF NOT EXISTS {s};
                CREATE TABLE {s}.schema_version (
                    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton), version integer NOT NULL);
                INSERT INTO {s}.schema_version(version) VALUES (1);
                CREATE TABLE {s}.jobs (
                    id uuid PRIMARY KEY, platform text NOT NULL, account text NOT NULL,
                    room_id text NOT NULL, source_message_id text NOT NULL, sender_id text NOT NULL,
                    plugin_version text NOT NULL, input jsonb NOT NULL, result jsonb,
                    provenance jsonb NOT NULL, deliveries jsonb NOT NULL DEFAULT '[]',
                    state text NOT NULL CHECK (state IN ('started','succeeded','partial','failed')),
                    started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
                    UNIQUE(platform, account, room_id, source_message_id));
                CREATE TABLE {s}.calls (
                    id uuid PRIMARY KEY, job_id uuid NOT NULL REFERENCES {s}.jobs(id),
                    engine text NOT NULL, operation text NOT NULL, input jsonb NOT NULL, output jsonb,
                    state text NOT NULL CHECK (state IN ('started','succeeded','failed')),
                    error jsonb, exit_code integer, elapsed_seconds double precision,
                    started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz);
                CREATE INDEX calls_by_job ON {s}.calls(job_id, started_at);
            """))

    def begin_job(self, source, payload, version):
        identity = uuid4()
        with self.db() as db:
            row = db.execute(self.query("""INSERT INTO {s}.jobs
                (id,platform,account,room_id,source_message_id,sender_id,plugin_version,input,provenance,state)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'started')
                ON CONFLICT(platform,account,room_id,source_message_id) DO NOTHING RETURNING id"""),
                (identity, source["platform"], source["account"], source["room_id"],
                 source["message_id"], source["sender_id"], version, self.json(payload),
                 self.json({"origin": "live", "parser_version": 1, "reconstruction": False}))).fetchone()
            return row["id"] if row else None

    def begin_call(self, job, engine, operation, payload):
        identity = uuid4()
        with self.db() as db:
            row = db.execute(self.query("""INSERT INTO {s}.calls(id,job_id,engine,operation,input,state)
                SELECT %s,id,%s,%s,%s,'started' FROM {s}.jobs WHERE id=%s AND state='started' RETURNING id"""),
                (identity, engine, operation, self.json(payload), job)).fetchone()
            if not row:
                raise ArchiveError("job_not_running")
        return identity

    def finish_call(self, identity, output, error, exit_code, elapsed):
        with self.db() as db:
            row = db.execute(self.query("""UPDATE {s}.calls SET output=%s,error=%s,state=%s,
                exit_code=%s,elapsed_seconds=%s,finished_at=now() WHERE id=%s AND state='started' RETURNING id"""),
                (self.json(output) if output is not None else None,
                 self.json(error) if error else None, "failed" if error else "succeeded",
                 exit_code, elapsed, identity)).fetchone()
            if not row:
                raise ArchiveError("call_already_finished")

    def delivery(self, job, event):
        with self.db() as db:
            db.execute(self.query("UPDATE {s}.jobs SET deliveries=deliveries || %s WHERE id=%s"),
                       (self.json([event]), job))

    def finish_job(self, job, state, result):
        with self.db() as db:
            db.execute(self.query("""UPDATE {s}.jobs SET state=%s,result=%s,finished_at=now()
                WHERE id=%s AND state='started'"""), (state, self.json(result), job))
