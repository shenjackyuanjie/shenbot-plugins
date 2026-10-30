"""持久确认闸门与图片存档的核心业务回归；只有显式提供配置才连接测试 PG。

数据库用本测试新建的随机 codex_test schema，全部主体为虚构值；
不读取 DSH 表，不请求模型，不发送 QQ，结束只删除自己创建的 schema。
"""

import hashlib
import os
import re
import secrets
import threading
import time
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import psycopg
from PIL import Image
from psycopg import sql

from plugins import codex
from plugins.codex_support.archive import Archive, ArchiveError
from plugins.codex_support.image_service import ImageError, ImageService, ImageSettings
from plugins.codex_support.providers import Call
from plugins.codex_support.transport import HTTPResult


@unittest.skipUnless(
    os.environ.get("CODEX_TEST_PG_CONFIG"), "显式配置临时 schema 的 PG 集成验证"
)
class ArchiveFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(os.environ["CODEX_TEST_PG_CONFIG"])
        cls.connection = tomllib.loads(path.read_text(encoding="utf-8-sig"))["main"][
            "pg_connection"
        ]
        cls.schema = "codex_test_" + secrets.token_hex(8)
        cls.archive = Archive(cls.connection, schema=cls.schema)
        cls.archive.initialize()
        cls.archive.initialize()  # 同一结构重新启动不会覆盖已有记录。

    @classmethod
    def tearDownClass(cls):
        # 明确核对删除目标：仅随机测试 schema，绝不接受正式 codex 或其他 schema。
        assert re.fullmatch(r"codex_test_[0-9a-f]{16}", cls.schema)
        with psycopg.connect(cls.connection, connect_timeout=5) as db:
            row = db.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname=%s", (cls.schema,)
            ).fetchone()
            assert row == (cls.schema,)
            db.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema))
            )

    def _review(self, job_id, stage, decision, prompt, references=()):
        request = self.archive.begin_request(
            job_id,
            "deepseek",
            stage,
            "fixture-flash",
            {
                "prompt": prompt,
                "reference_sha256": list(references),
                "messages": [{"role": "user", "content": "虚构的模型输入"}],
                "api_key": "must-not-persist",
                "nested": {"Authorization": "must-not-persist"},
            },
        )
        self.archive.finish_request(
            request,
            "succeeded",
            {
                "decision": decision,
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {"name": decision, "arguments": "{}"},
                    }
                ],
            },
            http_status=200,
        )
        return request

    def test_review_gate_atomic_owner_confirmation_and_durable_images(self):
        owner = (10001, -10002, 10003)
        blocked = self.archive.create_job(owner, "fixture-blocked", "虚构图片要求")
        pre = self._review(blocked, "pre_review", "pass", "虚构图片要求")
        post = self._review(blocked, "post_review", "nsfw", "不能生成")
        with self.assertRaises(ArchiveError):
            self.archive.prepare_draft(blocked, "不能生成", pre, post)
        self.archive.finish_job(blocked, "blocked")
        # 强制只针对已经确认并实际生成过的上游拒绝，不能越过本地双审核。
        with self.assertRaises(ArchiveError):
            self.archive.claim_retry(
                blocked,
                (owner[0], owner[1], None),
                "fixture-force-review",
                operator_id=10004,
            )

        job = self.archive.create_job(owner, "fixture-success", "一只纸鹤")
        self.assertEqual(
            job, self.archive.create_job(owner, "fixture-success", "重放不得替换原描述")
        )
        stream = BytesIO()
        Image.new("RGB", (2, 2), "blue").save(stream, "PNG")
        image = stream.getvalue()
        reference = self.archive.put_image(job, "reference", image)
        pre = self._review(job, "pre_review", "pass", "一只纸鹤", [reference])
        with self.assertRaises(ArchiveError):
            self.archive.put_image(job, "reference", image)
        rewrite = self.archive.begin_request(
            job, "deepseek", "rewrite", "fixture-flash", {"prompt": "一只纸鹤"}
        )
        self.archive.finish_request(
            rewrite, "succeeded", {"prompt": "蓝色背景上的纸鹤"}
        )
        post = self._review(job, "post_review", "pass", "蓝色背景上的纸鹤", [reference])
        with self.assertRaises(ArchiveError):
            self.archive.prepare_draft(job, "未经审核的另一张图", pre, post)
        self.archive.prepare_draft(job, "蓝色背景上的纸鹤", pre, post)
        # 确认前可调整参数，只合并给出的键；冻结的提示词和审核记录不受影响。
        self.assertEqual(
            self.archive.update_image_params(job, owner, {"quality": "high"}),
            {"quality": "high"},
        )
        self.assertEqual(
            self.archive.update_image_params(job, owner, {"background": "transparent"}),
            {"quality": "high", "background": "transparent"},
        )
        with self.assertRaises(ArchiveError):
            self.archive.claim_confirmation(job, (10001, -10004, 10003))

        def confirm():
            try:
                return self.archive.claim_confirmation(job, owner)
            except ArchiveError:
                return None

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: confirm(), range(2)))
        self.assertEqual(sum(row is not None for row in results), 1)
        # 确认消费后不能再改参数，否则预览与生成可能不一致。
        with self.assertRaises(ArchiveError):
            self.archive.update_image_params(job, owner, {"quality": "low"})
        payload = {
            "prompt": "蓝色背景上的纸鹤",
            "model": "fixture-image",
            "reference_sha256": [reference],
        }
        request = self.archive.begin_request(
            job, "codex", "generate", "fixture-image", payload
        )
        self.archive.put_image(job, "original", image, request)
        self.archive.finish_request(request, "unknown", error_code="OUTCOME_UNCERTAIN")
        restarted = Archive(self.connection, schema=self.schema)
        with self.assertRaises(ArchiveError):
            restarted.begin_request(job, "codex", "generate", "fixture-image", payload)
        self.assertEqual(restarted.image(job, "reference"), image)
        self.assertEqual(restarted.image(job, "original"), image)
        self.archive.cancel(job, owner)
        self.assertFalse(self.archive.finish_job(job, "succeeded"))
        self.assertEqual(restarted.owned_job(job, owner)["state"], "cancelled")
        # 多图参考：不同 position 互不覆盖，按顺序取回。
        multi = self.archive.create_job(owner, "fixture-multi", "两张参考图")
        red = BytesIO()
        Image.new("RGB", (2, 2), "red").save(red, "PNG")
        self.archive.put_image(multi, "reference", image, position=0)
        self.archive.put_image(multi, "reference", red.getvalue(), position=1)
        self.assertEqual(
            self.archive.images(multi, "reference"), [image, red.getvalue()]
        )
        # 同一位置已经是另一张图时拒绝覆盖，避免审核与执行换图。
        with self.assertRaises(ArchiveError):
            self.archive.put_image(multi, "reference", image, position=1)
        # 追加参考图只在待确认时可用，追加后草稿退回审核状态等待重审。
        multi_hashes = [
            hashlib.sha256(image).hexdigest(),
            hashlib.sha256(red.getvalue()).hexdigest(),
        ]
        pre_m = self._review(multi, "pre_review", "pass", "两张参考图", multi_hashes)
        post_m = self._review(multi, "post_review", "pass", "两张参考图", multi_hashes)
        self.archive.prepare_draft(multi, "两张参考图", pre_m, post_m)
        green = BytesIO()
        Image.new("RGB", (2, 2), "green").save(green, "PNG")
        self.assertEqual(
            self.archive.append_references(multi, owner, [green.getvalue()]), 3
        )
        refreshed = self.archive.owned_job(multi, owner)
        self.assertEqual(refreshed["state"], "reviewing")
        self.assertIsNone(refreshed["reviewed_prompt"])
        self.assertFalse(self.archive.has_stage(multi, "rewrite"))

        # 管理员按群范围代办：不限发送者，但账号与群仍然必须匹配。
        pending = self.archive.create_job(
            (10001, -10002, 19999), "fixture-delegate", "别人的任务"
        )
        pre_d = self._review(pending, "pre_review", "pass", "别人的任务")
        post_d = self._review(pending, "post_review", "pass", "别人的任务")
        self.archive.prepare_draft(pending, "别人的任务", pre_d, post_d)
        with self.assertRaises(ArchiveError):
            self.archive.claim_confirmation(pending, (10001, -10002, 10003))
        # 没有编号时使用范围内唯一的待确认任务。
        delegated = self.archive.resolve_job(None, (10001, -10002, None))
        self.assertEqual(delegated["id"], pending)
        self.archive.claim_confirmation(pending, (10001, -10002, None))
        self.assertEqual(
            self.archive.owned_job(pending, (10001, -10002, None))["state"],
            "generating",
        )

        with self.archive._db() as db:
            rows = db.execute(
                self.archive._sql(
                    "SELECT request_payload FROM {schema}.requests WHERE job_id=%s"
                ),
                (job,),
            ).fetchall()
            self.assertNotIn("must-not-persist", repr(rows))
            self.assertGreaterEqual(len(rows), 4)

    def test_old_http_failure_retries_atomically_and_replayed_command_cannot_regenerate(
        self,
    ):
        """复现 HTTP 400 后任务无法恢复；同时保护迁移、并发及消息重放不重复收费。"""
        owner = (10001, -10002, 10003)
        params = {"size": "1024x1536", "quality": "high", "background": "opaque"}
        job = self.archive.create_job(
            owner, "fixture-retry-original", "一只纸鹤", params
        )
        stream = BytesIO()
        Image.new("RGB", (2, 2), "blue").save(stream, "PNG")
        image = stream.getvalue()
        digest = self.archive.put_image(job, "reference", image)
        pre = self._review(job, "pre_review", "pass", "一只纸鹤", [digest])
        post = self._review(job, "post_review", "pass", "蓝色纸鹤", [digest])
        self.archive.prepare_draft(job, "蓝色纸鹤", pre, post)
        service = ImageService(
            ImageSettings(),
            lambda: SimpleNamespace(
                access="fixture-access", account_id="fixture-account"
            ),
            owner[0],
            archive=self.archive,
        )
        msg = SimpleNamespace(
            id="fixture-confirm",
            room_id=owner[1],
            sender_id=owner[2],
            reply_with=lambda text: SimpleNamespace(set_img=lambda *args: None),
        )
        client = Mock()
        with (
            patch.object(
                Call, "send", return_value=HTTPResult(400, {"error": {}}, b"")
            ),
            self.assertRaises(ImageError),
        ):
            service.confirm(msg, client, str(job)[:8])
        frozen = self.archive.owned_job(job, owner)
        # 在隔离 schema 中还原 v3 布局，验证旧失败任务与图片可无损升级后恢复。
        with self.archive._db() as db:
            db.execute(
                self.archive._sql(
                    "DROP INDEX {schema}.one_generation_per_attempt;"
                    "ALTER TABLE {schema}.jobs DROP COLUMN generation_attempt;"
                    "ALTER TABLE {schema}.requests DROP COLUMN generation_attempt;"
                    "CREATE UNIQUE INDEX one_generation_per_job ON {schema}.requests(job_id) WHERE stage='generate';"
                    "UPDATE {schema}.schema_version SET version=3"
                )
            )
            db.execute(
                self.archive._sql(
                    "UPDATE {schema}.jobs SET expires_at=now()-interval '1 hour' WHERE id=%s"
                ),
                (job,),
            )
        self.archive.initialize()
        self.archive.initialize()
        with self.assertRaises(ArchiveError):
            self.archive.claim_retry(job, (10001, -10004, None), "fixture-wrong-room")
        with self.assertRaises(ArchiveError):
            self.archive.claim_retry(
                job, (10001, -10002, 10004), "fixture-wrong-sender"
            )
        msg.id = "fixture-retry-first"
        with patch.object(
            Call, "send", return_value=HTTPResult(503, {"error": {}}, b"")
        ) as send:
            with self.assertRaises(ImageError):
                service.confirm(msg, client, str(job)[:8], retry=True)
            with self.assertRaises(ArchiveError):
                service.confirm(msg, client, str(job)[:8], retry=True)
            self.assertEqual(send.call_count, 1)
        retried = self.archive.owned_job(job, owner)
        self.assertEqual(retried["reviewed_prompt"], frozen["reviewed_prompt"])
        self.assertEqual(retried["approval_digest"], frozen["approval_digest"])
        self.assertEqual(retried["image_params"], params)
        self.assertEqual(self.archive.images(job, "reference"), [image])

        def retry():
            try:
                return self.archive.claim_retry(job, owner, "fixture-retry-second")
            except ArchiveError:
                return None

        with ThreadPoolExecutor(max_workers=2) as workers:
            claims = list(workers.map(lambda _: retry(), range(2)))
        self.assertEqual(sum(row is not None for row in claims), 1)
        payload = {"prompt": frozen["reviewed_prompt"], "reference_sha256": [digest]}
        request = self.archive.begin_request(
            job, "codex", "generate", "fixture-image", payload
        )
        with self.assertRaises(ArchiveError):
            self.archive.begin_request(
                job, "codex", "generate", "fixture-image", payload
            )
        self.archive.finish_request(request, "unknown", error_code="TIMEOUT_UNCERTAIN")
        self.archive.finish_job(job, "unknown")
        restarted = Archive(self.connection, schema=self.schema)
        with self.assertRaises(ArchiveError):
            restarted.claim_retry(job, owner, "fixture-retry-uncertain")
        with self.assertRaises(ArchiveError):
            restarted.claim_retry(
                job,
                (owner[0], owner[1], None),
                "fixture-admin-force-uncertain",
                operator_id=10004,
            )
        with self.archive._db() as db:
            rows = db.execute(
                self.archive._sql(
                    "SELECT generation_attempt,state,http_status FROM {schema}.requests "
                    "WHERE job_id=%s AND stage='generate' ORDER BY generation_attempt"
                ),
                (job,),
            ).fetchall()
        self.assertEqual(
            [
                (row["generation_attempt"], row["state"], row["http_status"])
                for row in rows
            ],
            [(0, "failed", 400), (1, "failed", 503), (2, "unknown", None)],
        )

    def _failed_generation(self, source, response):
        """构造经双审核且实际收到上游拒绝的任务，不向真实模型发请求。"""
        owner = (10001, -10002, 10003)
        job = self.archive.create_job(owner, source, "一只纸鹤")
        pre = self._review(job, "pre_review", "pass", "一只纸鹤")
        post = self._review(job, "post_review", "pass", "一只纸鹤")
        self.archive.prepare_draft(job, "一只纸鹤", pre, post)
        service = ImageService(
            ImageSettings(),
            lambda: SimpleNamespace(
                access="fixture-access", account_id="fixture-account"
            ),
            owner[0],
            archive=self.archive,
        )
        msg = SimpleNamespace(
            id=source + "-confirm",
            room_id=owner[1],
            sender_id=owner[2],
            reply_with=lambda text: SimpleNamespace(set_img=lambda *args: None),
        )
        with (
            patch.object(Call, "send", return_value=response),
            self.assertRaises(ImageError) as failure,
        ):
            service.confirm(msg, Mock(), str(job)[:8])
        upstream = response.body.get("error", {}).get("message")
        if upstream:
            self.assertIn(upstream, str(failure.exception))
        return job, owner, service, msg

    def test_only_admin_can_force_rejected_generation_including_legacy_errors(self):
        # 普通用户不能强制；管理员手动放行仍受来源防重、账号/群隔离及停止终态保护。
        rejected = HTTPResult(
            400,
            {
                "error": {
                    "code": "moderation_blocked",
                    "message": "fixture upstream rejection",
                }
            },
            b"",
        )
        job, owner, original_service, msg = self._failed_generation(
            "fixture-content-rejected", rejected
        )
        self.assertEqual(self.archive.owned_job(job, owner)["state"], "blocked")
        with self.assertRaises(ArchiveError):
            self.archive.claim_retry(
                job, owner, "fixture-ordinary-force", operator_id=owner[2]
            )
        with self.assertRaises(ArchiveError):
            self.archive.claim_retry(
                job,
                (owner[0], -10004, None),
                "fixture-cross-room-force",
                operator_id=10004,
            )
        with self.assertRaises(ArchiveError):
            self.archive.claim_retry(
                job,
                (10005, owner[1], None),
                "fixture-cross-account-force",
                operator_id=10004,
            )
        admin = ImageService(
            ImageSettings(),
            original_service.login_provider,
            owner[0],
            archive=self.archive,
            admins=frozenset({10004}),
        )
        msg.sender_id, msg.id = 10004, "fixture-admin-force-first"
        client = Mock()
        with patch.object(Call, "send", return_value=rejected) as send:
            with self.assertRaises(ImageError):
                admin.confirm(msg, client, str(job)[:8], retry=True)
            # 再次被上游拒绝后，重放同一条强制命令也不能重复消耗额度。
            with self.assertRaises(ArchiveError):
                admin.confirm(msg, client, str(job)[:8], retry=True)
            self.assertEqual(send.call_count, 1)
            # 旧版本的普通 failed + generic HTTP 错误仍只允许管理员强制。
            with self.archive._db() as db:
                db.execute(
                    self.archive._sql(
                        "UPDATE {schema}.jobs SET state='failed' WHERE id=%s"
                    ),
                    (job,),
                )
                db.execute(
                    self.archive._sql(
                        "UPDATE {schema}.requests SET error_code='UPSTREAM_HTTP_ERROR' WHERE job_id=%s AND stage='generate'"
                    ),
                    (job,),
                )
            with self.assertRaises(ArchiveError):
                self.archive.claim_retry(
                    job, owner, "fixture-ordinary-legacy-force", operator_id=owner[2]
                )
            msg.id = "fixture-admin-force-legacy"
            with self.assertRaises(ImageError):
                admin.confirm(msg, client, str(job)[:8], retry=True)
            self.assertEqual(send.call_count, 2)
            admin.cancel(msg, str(job)[:8])
            msg.id = "fixture-admin-force-cancelled"
            with self.assertRaises(ArchiveError):
                admin.confirm(msg, client, str(job)[:8], retry=True)
            self.assertEqual(send.call_count, 2)
        with self.archive._db() as db:
            events = db.execute(
                self.archive._sql(
                    "SELECT details FROM {schema}.events WHERE job_id=%s AND kind='generation_retry' ORDER BY id"
                ),
                (job,),
            ).fetchall()
        self.assertEqual(
            [row["details"]["operator_id"] for row in events], [10004, 10004]
        )
        self.assertTrue(all(row["details"]["forced"] for row in events))

    def test_queued_retry_can_be_cancelled_before_any_generation_is_sent(self):
        # 防止用户已取消排队重试，出队后仍被发起一次收费请求。
        job, owner, original_service, msg = self._failed_generation(
            "fixture-queued-retry",
            HTTPResult(400, {"error": {"code": "moderation_blocked"}}, b""),
        )
        service = ImageService(
            ImageSettings(),
            original_service.login_provider,
            owner[0],
            archive=self.archive,
            admins=frozenset({10004}),
        )
        msg.sender_id = 10004
        state = codex.Runtime(codex.Settings())
        state.image_slots = codex.GenerationSlots(concurrency=1, max_waiting=1)
        self.assertTrue(state.image_slots.acquire())
        rejected = []

        def run():
            try:
                codex._run_generation(
                    state, msg, Mock(), service, str(job)[:8], retry=True
                )
            except ArchiveError:
                rejected.append(True)

        worker = threading.Thread(target=run, daemon=True)
        with patch.object(Call, "send") as send:
            worker.start()
            try:
                deadline = time.monotonic() + 2
                while (
                    state.image_slots.snapshot()[1] == 0 and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                self.assertEqual(state.image_slots.snapshot(), (1, 1))
                service.cancel(msg, str(job)[:8])
            finally:
                state.image_slots.release()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            send.assert_not_called()
        self.assertEqual(rejected, [True])
        self.assertEqual(self.archive.owned_job(job, owner)["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
