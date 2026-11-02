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
                if version == [{"version": 1}]:
                    self._migrate_history(db)
                    return
                if version != [{"version": 2}]:
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
            self._migrate_history(db)

    def _migrate_history(self, db):
        db.execute(self.query("""
            ALTER TABLE {s}.jobs ALTER COLUMN account DROP NOT NULL;
            ALTER TABLE {s}.jobs ADD COLUMN history_key text UNIQUE;
            ALTER TABLE {s}.jobs ADD COLUMN requested_at timestamptz;
            ALTER TABLE {s}.jobs ADD COLUMN writer_token uuid;
            ALTER TABLE {s}.jobs ADD CONSTRAINT live_account_required CHECK(history_key IS NOT NULL OR account IS NOT NULL);
            UPDATE {s}.schema_version SET version=2;
        """))

    @contextmanager
    def history_job(self, source, payload, version, provenance, mode="overwrite"):
        """同一来源仅一个活 writer；领取时原地清空旧结果，绝不触及 live。"""
        import hashlib
        import json
        key = hashlib.sha256(json.dumps([source["source_alias"], source["platform"], source["room_id"],
            source["message_id"]], ensure_ascii=False).encode()).hexdigest()
        with self.db() as gate:
            locked = gate.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked", ("history:" + key,)).fetchone()["locked"]
            gate.commit()
            gate.autocommit = True
            if not locked:
                raise ArchiveError("history_busy")
            try:
                token = uuid4()
                identity = None
                with self.db() as db:
                    row = db.execute(self.query("SELECT id,state,provenance FROM {s}.jobs WHERE history_key=%s FOR UPDATE"), (key,)).fetchone()
                    skip = ((row and mode == "resume" and row["state"] == "succeeded" and row["provenance"].get("fingerprint") == provenance["fingerprint"])
                            or (mode == "retry-failed" and (not row or row["state"] == "succeeded")))
                    if row and row["provenance"].get("origin") != "history_recompute":
                        raise ArchiveError("not_history_job")
                    if not skip:
                        identity = row["id"] if row else uuid4()
                        values = (self.json(payload), self.json(provenance), version, source["requested_at"], token)
                        if row:
                            db.execute(self.query("DELETE FROM {s}.calls WHERE job_id=%s"), (identity,))
                            db.execute(self.query("""UPDATE {s}.jobs SET input=%s,provenance=%s,plugin_version=%s,
                                requested_at=%s,writer_token=%s,result=NULL,state='started',deliveries='[]',
                                started_at=now(),finished_at=NULL WHERE id=%s AND history_key=%s"""), (*values, identity, key))
                        else:
                            db.execute(self.query("""INSERT INTO {s}.jobs(input,provenance,plugin_version,requested_at,writer_token,
                                id,history_key,platform,account,room_id,source_message_id,sender_id,state)
                                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,'started')"""),
                                (*values, identity, key, source["platform"], source["room_id"], source["message_id"], source["sender_id"]))
                yield HistoryWriter(self, identity, token, gate) if identity else None
            finally:
                gate.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", ("history:" + key,))
                gate.commit()

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

    def begin_call(self, job, engine, operation, payload, writer_token=None):
        identity = uuid4()
        with self.db() as db:
            row = db.execute(self.query("""INSERT INTO {s}.calls(id,job_id,engine,operation,input,state)
                SELECT %s,id,%s,%s,%s,'started' FROM {s}.jobs WHERE id=%s AND state='started' AND writer_token IS NOT DISTINCT FROM %s::uuid FOR UPDATE RETURNING id"""),
                (identity, engine, operation, self.json(payload), job, writer_token)).fetchone()
            if not row:
                raise ArchiveError("job_not_running")
        return identity

    def finish_call(self, identity, output, error, exit_code, elapsed, writer_token=None):
        with self.db() as db:
            row = db.execute(self.query("""UPDATE {s}.calls SET output=%s,error=%s,state=%s,
                exit_code=%s,elapsed_seconds=%s,finished_at=now() WHERE id=%s AND state='started' AND EXISTS
                (SELECT 1 FROM {s}.jobs j WHERE j.id=job_id AND j.writer_token IS NOT DISTINCT FROM %s::uuid) RETURNING id"""),
                (self.json(output) if output is not None else None,
                 self.json(error) if error else None, "failed" if error else "succeeded",
                 exit_code, elapsed, identity, writer_token)).fetchone()
            if not row:
                raise ArchiveError("call_already_finished")

    def delivery(self, job, event, writer_token=None):
        with self.db() as db:
            row = db.execute(self.query("UPDATE {s}.jobs SET deliveries=deliveries || %s WHERE id=%s AND writer_token IS NOT DISTINCT FROM %s::uuid RETURNING id"),
                       (self.json([event]), job, writer_token)).fetchone()
            if not row:
                raise ArchiveError("writer_lost")

    def finish_job(self, job, state, result, writer_token=None):
        with self.db() as db:
            row = db.execute(self.query("""UPDATE {s}.jobs SET state=%s,result=%s,finished_at=now()
                WHERE id=%s AND state='started' AND writer_token IS NOT DISTINCT FROM %s::uuid RETURNING id"""),
                (state, self.json(result), job, writer_token)).fetchone()
            if not row:
                raise ArchiveError('writer_lost')

class HistoryWriter:
    """短期所有权栅栏，不是保留历史版本的 run/attempt。"""
    def __init__(self, archive, identity, token, gate):
        self.archive, self.identity, self.token, self.gate = archive, identity, token, gate

    def check(self):
        try:
            self.gate.execute("SELECT 1")
        except Exception:  # noqa: BLE001 - 锁连接失效必须在下一次实际计算前停止
            raise ArchiveError("history_lock_lost") from None

    def begin_call(self, job, engine, operation, payload):
        self.check()
        if job != self.identity:
            raise ArchiveError("wrong_job")
        return self.archive.begin_call(job, engine, operation, payload, self.token)

    def finish_call(self, identity, output, error, exit_code, elapsed):
        self.check()
        return self.archive.finish_call(identity, output, error, exit_code, elapsed, self.token)

    def delivery(self, job, event):
        self.check()
        if job != self.identity:
            raise ArchiveError("wrong_job")
        return self.archive.delivery(job, event, self.token)

    def finish_job(self, job, state, result):
        self.check()
        if job != self.identity:
            raise ArchiveError("wrong_job")
        return self.archive.finish_job(job, state, result, self.token)
