"""Codex 专用 PostgreSQL 归档与确认闸门，不依赖 DSH 进程或运行配置。

每次外部调用必须先提交请求记录；确认与生成占位在数据库中原子完成，
因此插件重载、重复消息和重复确认都不能悄悄增加一次收费请求。
此模块只归档数据，不请求模型、不发送 QQ，也不自动重放未完成任务。
"""

from __future__ import annotations

import hashlib
import json
import re
import warnings
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from PIL import Image
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SCHEMA_VERSION = 4
# 一个任务最多引用几张图；命令层与存档层共用同一个上限。
MAX_REFERENCES = 5
# 操作范围：机器人账号、来源群、发起者；发起者为 None 表示管理员代办，不限发送者。
Scope = tuple[int, int, int | None]


class ArchiveError(RuntimeError):
    """固定的用户可见错误；不透传可能包含连接信息的驱动异常。"""


_SECRET_KEYS = {
    "access",
    "refresh",
    "token",
    "dsn",
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "password",
    "connectionstring",
    "connection_string",
    "pg_connection",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-dsh-auth-token",
}


def sanitized(value):
    """递归剔除认证字段；正文、工具裁决和 usage 按用户要求原样归档。"""
    if isinstance(value, dict):
        return {
            key: "[已剔除]" if str(key).lower() in _SECRET_KEYS else sanitized(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitized(item) for item in value]
    return value


def _image_meta(data: bytes) -> tuple[str, str, int, int]:
    """解码并校验待归档图片，返回摘要、MIME 与尺寸；不通过就拒绝，不猜测格式。"""
    if not 0 < len(data) <= 50 * 1024 * 1024:
        raise ArchiveError("图片超过存档大小限制。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
                media_type = {
                    "PNG": "image/png",
                    "JPEG": "image/jpeg",
                    "WEBP": "image/webp",
                }.get(image.format)
                if media_type is None or width * height > 64_000_000:
                    raise ArchiveError("图片格式或像素量超出支持范围。")
                image.verify()
    except (
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise ArchiveError("无法解码待存档图片。") from None
    return hashlib.sha256(data).hexdigest(), media_type, width, height


class Archive:
    """每个方法使用短事务；网络生成期间不持有数据库事务或连接。"""

    def __init__(self, connection: str, *, schema: str = "codex"):
        # 测试只能使用明确前缀的临时 schema，不能拿任意 DSH schema 做迁移。
        if not re.fullmatch(r"codex|codex_test_[0-9a-f]{16}", schema):
            raise ArchiveError("归档 schema 不在允许范围内。")
        self.__connection = connection
        self.schema = schema

    def _sql(self, query: str):
        return sql.SQL(query).format(schema=sql.Identifier(self.schema))

    @contextmanager
    def _db(self):
        try:
            with psycopg.connect(
                self.__connection,
                connect_timeout=5,
                row_factory=dict_row,
                options="-c statement_timeout=10000 -c lock_timeout=3000",
            ) as db:
                yield db
        except psycopg.Error:
            raise ArchiveError(
                "Codex 存档数据库操作失败，本次不继续外部操作。"
            ) from None

    def initialize(self) -> None:
        """显式迁移，只创建本模块所有的 schema；发现未知存量结构时拒绝接管。"""
        with self._db() as db:
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("ica:" + self.schema,),
            )
            tables = db.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=%s", (self.schema,)
            ).fetchall()
            if tables:
                if "schema_version" not in {row["tablename"] for row in tables}:
                    raise ArchiveError("codex schema 已有未知数据，不自动覆盖或迁移。")
                row = db.execute(
                    self._sql("SELECT version FROM {schema}.schema_version")
                ).fetchall()
                if len(row) != 1:
                    raise ArchiveError("codex 存档版本不兼容，停止初始化。")
                version = row[0]["version"]
                if version == 1:
                    # 1 → 2：把生成参数固化到任务上，确认阶段不能再从命令改写。
                    db.execute(
                        self._sql(
                            "ALTER TABLE {schema}.jobs ADD COLUMN IF NOT EXISTS "
                            # 花括号需要转义，_sql 会把 {{}} 还原成 jsonb 空对象字面量。
                            "image_params jsonb NOT NULL DEFAULT '{{}}'::jsonb"
                        )
                    )
                    db.execute(
                        self._sql("UPDATE {schema}.schema_version SET version=2")
                    )
                    version = 2
                if version == 2:
                    # 2 → 3：一条任务允许带多张参考图，用 position 区分并保留顺序。
                    db.execute(
                        self._sql(
                            "ALTER TABLE {schema}.job_assets ADD COLUMN IF NOT EXISTS "
                            "position integer NOT NULL DEFAULT 0"
                        )
                    )
                    db.execute(
                        self._sql(
                            "ALTER TABLE {schema}.job_assets DROP CONSTRAINT IF EXISTS job_assets_pkey"
                        )
                    )
                    db.execute(
                        self._sql(
                            "ALTER TABLE {schema}.job_assets "
                            "ADD PRIMARY KEY (job_id, role, position)"
                        )
                    )
                    db.execute(
                        self._sql("UPDATE {schema}.schema_version SET version=3")
                    )
                    version = 3
                if version == 3:
                    # 3 → 4：按显式确认的尝试次数防重，保留每次失败请求而不是覆盖旧记录。
                    db.execute(
                        self._sql(
                            "ALTER TABLE {schema}.jobs ADD COLUMN generation_attempt integer NOT NULL DEFAULT 0;"
                            "ALTER TABLE {schema}.requests ADD COLUMN generation_attempt integer NOT NULL DEFAULT 0;"
                            "DROP INDEX {schema}.one_generation_per_job;"
                            "CREATE UNIQUE INDEX one_generation_per_attempt ON {schema}.requests(job_id,generation_attempt) "
                            "WHERE stage='generate';"
                            "UPDATE {schema}.schema_version SET version=4"
                        )
                    )
                    version = 4
                if version != SCHEMA_VERSION:
                    raise ArchiveError("codex 存档版本不兼容，停止初始化。")
                return
            migration = (
                Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
            )
            db.execute(self._sql(migration))

    def create_job(
        self,
        owner: tuple[int, int, int],
        source_id: str,
        prompt: str,
        params: dict | None = None,
    ) -> UUID:
        """同一平台来源只建立一个任务；生成过的旧来源不会因重放变成新任务。

        params 在创建时冻结尺寸、质量与背景；重放同一来源不会改写已记录的参数。
        """
        if not source_id or not prompt.strip():
            raise ArchiveError("任务缺少可靠来源或图片描述。")
        with self._db() as db:
            row = db.execute(
                self._sql(
                    "INSERT INTO {schema}.jobs(id,account,room_id,sender_id,source_message_id,original_prompt,image_params) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(account,room_id,source_message_id) "
                    "DO NOTHING RETURNING id"
                ),
                (uuid4(), *owner, source_id, prompt, Jsonb(sanitized(params or {}))),
            ).fetchone()
            if row is None:
                row = db.execute(
                    self._sql(
                        "SELECT id,sender_id FROM {schema}.jobs WHERE account=%s AND room_id=%s AND source_message_id=%s"
                    ),
                    (owner[0], owner[1], source_id),
                ).fetchone()
                if row["sender_id"] != owner[2]:
                    raise ArchiveError("消息来源与任务所有者不匹配。")
            return row["id"]

    def owned_job(self, job_id: UUID, owner: Scope) -> dict:
        """群命令查询匹配机器人账号、来源群和发起者；管理员按 scope 放开发起者。"""
        with self._db() as db:
            row = db.execute(
                self._sql(
                    "SELECT * FROM {schema}.jobs WHERE id=%s AND account=%s AND room_id=%s "
                    "AND (%s::bigint IS NULL OR sender_id=%s)"
                ),
                (job_id, owner[0], owner[1], owner[2], owner[2]),
            ).fetchone()
            if row is None:
                raise ArchiveError("未找到属于你的当前群任务。")
            return row

    def owned_job_by_prefix(self, prefix: str, owner: Scope) -> dict:
        """群内确认使用任务编号短前缀；账号与群始终强制匹配，且必须唯一。"""
        if not re.fullmatch(r"[0-9a-f]{8}", prefix):
            raise ArchiveError("任务编号格式无效。")
        with self._db() as db:
            rows = db.execute(
                self._sql(
                    "SELECT * FROM {schema}.jobs WHERE id::text LIKE %s AND account=%s "
                    "AND room_id=%s AND (%s::bigint IS NULL OR sender_id=%s)"
                ),
                (prefix + "%", owner[0], owner[1], owner[2], owner[2]),
            ).fetchall()
            if len(rows) != 1:
                raise ArchiveError("未找到唯一的可操作任务。")
            return rows[0]

    def pending_job(self, owner: Scope) -> dict:
        """返回该范围内唯一可确认的任务；没有或多个都要求明确编号。"""
        with self._db() as db:
            rows = db.execute(
                self._sql(
                    "SELECT * FROM {schema}.jobs WHERE account=%s AND room_id=%s "
                    "AND (%s::bigint IS NULL OR sender_id=%s) "
                    "AND state='awaiting_confirmation' AND expires_at>now() ORDER BY created_at"
                ),
                (owner[0], owner[1], owner[2], owner[2]),
            ).fetchall()
        if not rows:
            raise ArchiveError("当前没有待确认的任务。")
        if len(rows) > 1:
            listing = "、".join(str(row["id"])[:8] for row in rows)
            raise ArchiveError(f"有多个待确认任务，请带上编号：{listing}")
        return rows[0]

    def recent_jobs(self, owner: Scope, limit: int = 5) -> list[dict]:
        """最近的任务摘要；没有编号时用它查看记录，不返回提示词正文。"""
        with self._db() as db:
            return db.execute(
                self._sql(
                    "SELECT left(id::text,8) AS short, state, created_at FROM {schema}.jobs "
                    "WHERE account=%s AND room_id=%s AND (%s::bigint IS NULL OR sender_id=%s) "
                    "ORDER BY created_at DESC LIMIT %s"
                ),
                (owner[0], owner[1], owner[2], owner[2], limit),
            ).fetchall()

    def resolve_job(self, prefix: str | None, owner: Scope) -> dict:
        """有编号按前缀定位，没有编号就用范围内唯一的待确认任务。"""
        if prefix:
            return self.owned_job_by_prefix(prefix, owner)
        return self.pending_job(owner)

    def begin_request(
        self, job_id: UUID | None, provider: str, stage: str, model: str, payload: dict
    ) -> UUID:
        """调用上游之前提交 started；生成阶段额外受任务状态和唯一索引双重保护。"""
        request_id = uuid4()
        with self._db() as db:
            attempt = 0
            if job_id is not None:
                job = db.execute(
                    self._sql("SELECT * FROM {schema}.jobs WHERE id=%s FOR UPDATE"),
                    (job_id,),
                ).fetchone()
                expected = "generating" if stage == "generate" else "reviewing"
                if job is None or job["state"] != expected:
                    raise ArchiveError("任务状态不允许发起本次模型请求。")
                if (
                    stage == "generate"
                    and payload.get("prompt") != job["reviewed_prompt"]
                ):
                    raise ArchiveError("生成提示词与已审核的草稿不一致。")
                if stage == "generate":
                    attempt = job["generation_attempt"]
                    if payload.get("reference_sha256", []) != self._reference_hashes(
                        db, job_id
                    ):
                        raise ArchiveError("生成参考图与已审核的草稿不一致。")
            elif stage not in {"quota_check", "quota_poll"}:
                raise ArchiveError("图片与审核请求必须属于一个持久任务。")
            db.execute(
                self._sql(
                    "INSERT INTO {schema}.requests(id,job_id,provider,stage,model,request_payload,generation_attempt) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)"
                ),
                (
                    request_id,
                    job_id,
                    provider,
                    stage,
                    model,
                    Jsonb(sanitized(payload)),
                    attempt,
                ),
            )
        return request_id

    def finish_request(
        self,
        request_id: UUID,
        state: str,
        response: dict | None = None,
        *,
        http_status: int | None = None,
        upstream_id: str | None = None,
        error_code: str | None = None,
        usage: dict | None = None,
    ) -> None:
        """完成结果只能提交一次；错误码由客户端分类，不接受原始认证或连接异常。"""
        if state not in {"succeeded", "failed", "unknown"}:
            raise ArchiveError("无效的请求终态。")
        with self._db() as db:
            row = db.execute(
                self._sql(
                    "UPDATE {schema}.requests SET state=%s,response_payload=%s,http_status=%s,upstream_request_id=%s,"
                    "error_code=%s,usage=%s,finished_at=now() WHERE id=%s AND state='started' RETURNING id"
                ),
                (
                    state,
                    Jsonb(sanitized(response)) if response is not None else None,
                    http_status,
                    upstream_id,
                    error_code,
                    Jsonb(sanitized(usage)) if usage is not None else None,
                    request_id,
                ),
            ).fetchone()
            if row is None:
                raise ArchiveError("请求已归档或不存在，不覆盖既有结果。")

    def prepare_draft(
        self, job_id: UUID, prompt: str, pre_request: UUID, post_request: UUID
    ) -> str:
        """只有两次独立 pass 才能冻结草稿；图像引用也纳入摘要，确认后不能换图。"""
        if not prompt.strip() or pre_request == post_request:
            raise ArchiveError("草稿或独立审核记录无效。")
        with self._db() as db:
            job = db.execute(
                self._sql("SELECT * FROM {schema}.jobs WHERE id=%s FOR UPDATE"),
                (job_id,),
            ).fetchone()
            if job is None or job["state"] != "reviewing":
                raise ArchiveError("任务不在审核阶段，不能替换已冻结草稿。")
            references = self._reference_hashes(db, job_id)
            for request_id, stage, reviewed in (
                (pre_request, "pre_review", job["original_prompt"]),
                (post_request, "post_review", prompt),
            ):
                verdict = db.execute(
                    self._sql(
                        "SELECT request_payload FROM {schema}.requests WHERE id=%s AND job_id=%s AND stage=%s "
                        "AND state='succeeded' AND response_payload->>'decision'='pass'"
                    ),
                    (request_id, job_id, stage),
                ).fetchone()
                if verdict is None:
                    raise ArchiveError("缺少独立且通过的前置或后置审核。")
                reviewed_input = verdict["request_payload"]
                # 放行属于特定输入，而不是可以挪用到任何提示词或图片的通行证。
                if (
                    reviewed_input.get("prompt") != reviewed
                    or reviewed_input.get("reference_sha256", []) != references
                ):
                    raise ArchiveError("审核输入与待确认的提示词或图片不一致。")
            frozen = json.dumps(
                [job["original_prompt"], prompt, references],
                ensure_ascii=False,
            )
            digest = hashlib.sha256(frozen.encode("utf-8")).hexdigest()
            db.execute(
                self._sql(
                    "UPDATE {schema}.jobs SET reviewed_prompt=%s,approval_digest=%s,state='awaiting_confirmation',"
                    "updated_at=now(),expires_at=now()+interval '10 minutes' WHERE id=%s"
                ),
                (prompt, digest, job_id),
            )
            self._event(
                db,
                job_id,
                "draft_ready",
                {
                    "pre_request": str(pre_request),
                    "post_request": str(post_request),
                    "digest": digest,
                },
            )
            return digest

    def update_image_params(self, job_id: UUID, owner: Scope, params: dict) -> dict:
        """确认前调整生成参数；仍绑定账号、群与发起者，且草稿必须还在有效期内。

        只合并给出的键，提示词与审核记录不动：参数不参与 NSFW 判定。
        """
        if not params:
            raise ArchiveError("没有需要更新的生成参数。")
        with self._db() as db:
            row = db.execute(
                self._sql(
                    "UPDATE {schema}.jobs SET image_params=image_params||%s,updated_at=now() "
                    "WHERE id=%s AND account=%s AND room_id=%s AND (%s::bigint IS NULL OR sender_id=%s) "
                    "AND state='awaiting_confirmation' AND expires_at>now() RETURNING image_params"
                ),
                (
                    Jsonb(sanitized(params)),
                    job_id,
                    owner[0],
                    owner[1],
                    owner[2],
                    owner[2],
                ),
            ).fetchone()
            if row is None:
                raise ArchiveError(
                    "任务不可调整参数：请检查所属群、发起者、有效期及当前状态。"
                )
            self._event(
                db, job_id, "params_updated", {"image_params": sanitized(params)}
            )
            return row["image_params"]

    def claim_confirmation(self, job_id: UUID, owner: Scope) -> dict:
        """原子消费确认，防止并发确认、跨群操作和已过期草稿重复收费。"""
        with self._db() as db:
            row = db.execute(
                self._sql(
                    "UPDATE {schema}.jobs SET state='generating',updated_at=now() WHERE id=%s AND account=%s "
                    "AND room_id=%s AND (%s::bigint IS NULL OR sender_id=%s) "
                    "AND state='awaiting_confirmation' AND expires_at>now() RETURNING *"
                ),
                (job_id, owner[0], owner[1], owner[2], owner[2]),
            ).fetchone()
            if row is None:
                raise ArchiveError(
                    "任务不可确认：请检查所属群、发起者、有效期及当前状态。"
                )
            self._event(db, job_id, "confirmed", {"digest": row["approval_digest"]})
            return row

    def claim_retry(self, job_id: UUID, owner: Scope, source_id: str) -> dict:
        """原子领取一次手动重试；只接受已确认且收到 HTTP 错误的生成失败。

        不受草稿有效期限制：沿用已经确认的内容，不重新审核或改变参数。
        同一条 QQ 命令即使在再次失败后重放，也不能增加一次生成。
        """
        if not source_id:
            raise ArchiveError("重试命令缺少可靠消息来源。")
        with self._db() as db:
            job = db.execute(
                self._sql(
                    "SELECT * FROM {schema}.jobs WHERE id=%s AND account=%s AND room_id=%s "
                    "AND (%s::bigint IS NULL OR sender_id=%s) FOR UPDATE"
                ),
                (job_id, owner[0], owner[1], owner[2], owner[2]),
            ).fetchone()
            if job is None:
                raise ArchiveError("未找到属于你的当前群任务。")
            if job["state"] == "blocked":
                raise ArchiveError(
                    "任务已因审核未通过而停止，不能重试；请调整描述或参考图后手动重新发起。"
                )
            if job["state"] != "failed" or not job["approval_digest"]:
                raise ArchiveError(
                    "只能重试已确认且明确失败的生图任务；进行中、已成功或结果不确定的任务不重试。"
                )
            previous = db.execute(
                self._sql(
                    "SELECT id,state,http_status,error_code,response_payload->'error'->>'code' AS upstream_error_code "
                    "FROM {schema}.requests "
                    "WHERE job_id=%s AND stage='generate' AND generation_attempt=%s"
                ),
                (job_id, job["generation_attempt"]),
            ).fetchone()
            # 旧版本把上游审核拒绝也归为 HTTP 错误，重试历史记录时同样要识别。
            if previous and (
                previous["error_code"] == "CONTENT_REJECTED"
                or previous["upstream_error_code"] == "moderation_blocked"
            ):
                raise ArchiveError(
                    "任务未通过上游内容审核，请调整描述后重新发起，不能重试。"
                )
            if (
                previous is None
                or previous["state"] != "failed"
                or previous["http_status"] is None
                or not 400 <= previous["http_status"] <= 599
            ):
                raise ArchiveError(
                    "本次生成没有明确的 HTTP 失败记录，结果可能不确定，不能重试。"
                )
            replay = db.execute(
                self._sql(
                    "SELECT 1 FROM {schema}.events WHERE job_id=%s AND kind='generation_retry' "
                    "AND details->>'source_message_id'=%s LIMIT 1"
                ),
                (job_id, source_id),
            ).fetchone()
            if replay is not None:
                raise ArchiveError(
                    "这条重试命令已经处理过；如需再次重试，请发送一条新命令。"
                )
            row = db.execute(
                self._sql(
                    "UPDATE {schema}.jobs SET state='generating',generation_attempt=generation_attempt+1,"
                    "updated_at=now() WHERE id=%s RETURNING *"
                ),
                (job_id,),
            ).fetchone()
            self._event(
                db,
                job_id,
                "generation_retry",
                {
                    "previous_request": str(previous["id"]),
                    "attempt": row["generation_attempt"],
                    "source_message_id": source_id,
                    "digest": row["approval_digest"],
                },
            )
            return row

    def cancel(self, job_id: UUID, owner: Scope) -> None:
        """取消不代表上游未执行；保留所有请求，后续结果仍须归档但不能自动回图。"""
        with self._db() as db:
            row = db.execute(
                self._sql(
                    "UPDATE {schema}.jobs SET state='cancelled',updated_at=now() WHERE id=%s AND account=%s "
                    "AND room_id=%s AND (%s::bigint IS NULL OR sender_id=%s) "
                    "AND state IN ('reviewing','awaiting_confirmation','generating','failed') RETURNING id"
                ),
                (job_id, owner[0], owner[1], owner[2], owner[2]),
            ).fetchone()
            if row is None:
                raise ArchiveError("任务不可取消或不属于你。")
            self._event(db, job_id, "cancelled", {})

    def finish_job(self, job_id: UUID, state: str) -> bool:
        """保留取消终态；迟到结果可存档，但不能把已取消任务重新变为可发送。"""
        if state not in {"succeeded", "blocked", "failed", "unknown", "expired"}:
            raise ArchiveError("无效的任务终态。")
        with self._db() as db:
            row = db.execute(
                self._sql(
                    "UPDATE {schema}.jobs SET state=%s,updated_at=now() WHERE id=%s "
                    "AND state IN ('reviewing','awaiting_confirmation','generating') RETURNING id"
                ),
                (state, job_id),
            ).fetchone()
            return row is not None

    def put_image(
        self,
        job_id: UUID,
        role: str,
        data: bytes,
        request_id: UUID | None = None,
        position: int = 0,
    ) -> str:
        """保存实际图片二进制并去重；参考图在审核开始后不可替换，防止审核与执行换图。

        position 用于多图参考的顺序，同一用途的不同位置互不覆盖。
        """
        if role not in {"reference", "original", "delivery"} or position < 0:
            raise ArchiveError("图片用途无效或超过存档大小限制。")
        digest, media_type, width, height = _image_meta(data)
        with self._db() as db:
            job = db.execute(
                self._sql("SELECT state FROM {schema}.jobs WHERE id=%s FOR UPDATE"),
                (job_id,),
            ).fetchone()
            if job is None:
                raise ArchiveError("图片所属任务不存在。")
            if role == "reference":
                started = db.execute(
                    self._sql(
                        "SELECT id FROM {schema}.requests WHERE job_id=%s LIMIT 1"
                    ),
                    (job_id,),
                ).fetchone()
                if job["state"] != "reviewing" or started:
                    raise ArchiveError("审核已经开始，不能改变参考图。")
            if request_id is not None:
                row = db.execute(
                    self._sql(
                        "SELECT id FROM {schema}.requests WHERE id=%s AND job_id=%s"
                    ),
                    (request_id, job_id),
                ).fetchone()
                if row is None:
                    raise ArchiveError("图片与请求所属任务不一致。")
            db.execute(
                self._sql(
                    "INSERT INTO {schema}.assets(sha256,media_type,width,height,bytes,data) VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(sha256) DO NOTHING"
                ),
                (digest, media_type, width, height, len(data), data),
            )
            row = db.execute(
                self._sql(
                    "INSERT INTO {schema}.job_assets(job_id,sha256,role,position,request_id) VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT(job_id,role,position) DO UPDATE SET sha256=EXCLUDED.sha256 "
                    "WHERE {schema}.job_assets.sha256=EXCLUDED.sha256 RETURNING sha256"
                ),
                (job_id, digest, role, position, request_id),
            ).fetchone()
            if row is None:
                raise ArchiveError("该用途已有不同图片，不覆盖既有存档。")
        return digest

    def append_references(self, job_id: UUID, owner: Scope, images: list[bytes]) -> int:
        """确认前追加参考图：全部成功才算数，并把草稿退回审核状态等待重审。

        参考图参与审核，追加后原草稿必须作废，否则放行结论会落到没审过的图上。
        """
        if not images:
            raise ArchiveError("没有需要追加的参考图。")
        metas = [_image_meta(data) for data in images]
        with self._db() as db:
            job = db.execute(
                self._sql(
                    "SELECT state, expires_at > now() AS valid FROM {schema}.jobs "
                    "WHERE id=%s AND account=%s AND room_id=%s AND (%s::bigint IS NULL OR sender_id=%s) FOR UPDATE"
                ),
                (job_id, owner[0], owner[1], owner[2], owner[2]),
            ).fetchone()
            if job is None:
                raise ArchiveError("未找到属于你的当前群任务。")
            if job["state"] != "awaiting_confirmation" or not job["valid"]:
                raise ArchiveError("只有待确认且未过期的任务可以追加参考图。")
            existing = db.execute(
                self._sql(
                    "SELECT count(*) AS n, coalesce(max(position), -1) AS last FROM {schema}.job_assets "
                    "WHERE job_id=%s AND role='reference'"
                ),
                (job_id,),
            ).fetchone()
            if existing["n"] + len(images) > MAX_REFERENCES:
                raise ArchiveError(f"参考图最多 {MAX_REFERENCES} 张。")
            first = existing["last"] + 1
            for offset, (digest, media_type, width, height) in enumerate(metas):
                data = images[offset]
                db.execute(
                    self._sql(
                        "INSERT INTO {schema}.assets(sha256,media_type,width,height,bytes,data) "
                        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(sha256) DO NOTHING"
                    ),
                    (digest, media_type, width, height, len(data), data),
                )
                db.execute(
                    self._sql(
                        "INSERT INTO {schema}.job_assets(job_id,sha256,role,position,request_id) "
                        "VALUES (%s,%s,'reference',%s,NULL)"
                    ),
                    (job_id, digest, first + offset),
                )
            db.execute(
                self._sql(
                    "UPDATE {schema}.jobs SET state='reviewing',reviewed_prompt=NULL,approval_digest=NULL,"
                    "updated_at=now() WHERE id=%s"
                ),
                (job_id,),
            )
            self._event(db, job_id, "references_appended", {"count": len(images)})
            return existing["n"] + len(images)

    def has_stage(self, job_id: UUID, stage: str) -> bool:
        """判断该任务是否跑过某个阶段；追加参考图时据此沿用原润色/直出模式。"""
        with self._db() as db:
            row = db.execute(
                self._sql(
                    "SELECT 1 AS hit FROM {schema}.requests WHERE job_id=%s AND stage=%s LIMIT 1"
                ),
                (job_id, stage),
            ).fetchone()
            return row is not None

    def images(self, job_id: UUID, role: str) -> list[bytes]:
        """按 position 顺序读取某个用途的全部图片；多图参考按引用顺序参与改图。"""
        with self._db() as db:
            rows = db.execute(
                self._sql(
                    "SELECT a.data,a.sha256 FROM {schema}.assets a JOIN {schema}.job_assets j USING(sha256) "
                    "WHERE j.job_id=%s AND j.role=%s ORDER BY j.position"
                ),
                (job_id, role),
            ).fetchall()
        stored: list[bytes] = []
        for row in rows:
            data = bytes(row["data"])
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ArchiveError("图片存档摘要不匹配，停止发送。")
            stored.append(data)
        return stored

    def image(self, job_id: UUID, role: str) -> bytes | None:
        """单张用途（原图、发送副本）；多图参考请用 images。"""
        stored = self.images(job_id, role)
        return stored[0] if stored else None

    def _reference_hashes(self, db, job_id: UUID) -> list[str]:
        rows = db.execute(
            self._sql(
                "SELECT sha256 FROM {schema}.job_assets WHERE job_id=%s AND role='reference' ORDER BY position"
            ),
            (job_id,),
        ).fetchall()
        return [row["sha256"] for row in rows]

    def _event(self, db, job_id: UUID, kind: str, details: dict) -> None:
        db.execute(
            self._sql(
                "INSERT INTO {schema}.events(job_id,kind,details) VALUES (%s,%s,%s)"
            ),
            (job_id, kind, Jsonb(sanitized(details))),
        )

    def event(self, job_id: UUID, kind: str, details: dict) -> None:
        """追加回图、恢复等审计事件；传输受理与平台送达必须使用不同语义。"""
        with self._db() as db:
            self._event(db, job_id, kind, details)
