"""持久确认闸门与图片存档的核心业务回归；只有显式提供配置才连接测试 PG。

数据库用本测试新建的随机 codex_test schema，全部主体为虚构值；
不读取 DSH 表，不请求模型，不发送 QQ，结束只删除自己创建的 schema。
"""

import os
import re
import secrets
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import psycopg
from PIL import Image
from psycopg import sql

from plugins.codex_support.archive import Archive, ArchiveError


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
        with self.archive._db() as db:
            rows = db.execute(
                self.archive._sql(
                    "SELECT request_payload FROM {schema}.requests WHERE job_id=%s"
                ),
                (job,),
            ).fetchall()
            self.assertNotIn("must-not-persist", repr(rows))
            self.assertGreaterEqual(len(rows), 4)


if __name__ == "__main__":
    unittest.main()
