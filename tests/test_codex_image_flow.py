"""生图命令编排的核心回归：审核拦截与本人确认各一次，不联网、不消耗额度。

真实 SDK 协议由 test_codex_clients.py 覆盖；这里只验证业务流程的放行与拦截。
"""

import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image

from plugins.codex_support import image_service
from plugins.codex_support.image_service import ImageError, ImageService, ImageSettings


class FakeArchive:
    """只记录关键状态转换，不模拟 SQL；真实持久化由 test_codex_archive 覆盖。"""

    def __init__(self):
        self.job = {"id": "job-1", "reviewed_prompt": "润色后", "state": "reviewing"}
        self.blocked = False
        self.drafted = False
        self.confirmed = False
        self.generated = False

    def create_job(self, owner, source, prompt, params=None):
        self.job["image_params"] = params or {}
        return "abcdef12-3456-7890-abcd-ef1234567890"

    def put_image(self, job, role, data, request=None):
        return "digest-" + role

    def begin_request(self, job, provider, stage, model, payload):
        return "request-" + stage

    def finish_request(self, request, state, response=None, **kwargs):
        pass

    def prepare_draft(self, job, prompt, pre, post):
        self.drafted = True

    def finish_job(self, job, state):
        self.blocked = state == "blocked"
        self.generated = state == "succeeded"
        return True

    def owned_job_by_prefix(self, prefix, owner):
        return self.job

    def claim_confirmation(self, job, owner):
        if self.confirmed:
            raise image_service.ArchiveError("已确认")
        self.confirmed = True
        return self.job

    def image(self, job, role):
        stream = BytesIO()
        Image.new("RGB", (2, 2), "red").save(stream, "PNG")
        return stream.getvalue()

    def cancel(self, job, owner):
        pass


def message(texts=None):
    """texts 收集发出的文本，便于断言回执顺序。"""

    def reply_with(text):
        if texts is not None:
            texts.append(text)
        return SimpleNamespace(set_img=lambda *a: None)

    return SimpleNamespace(
        id="msg-1",
        room_id=-897291716,
        sender_id=10003,
        account=10001,
        reply=None,
        reply_with=reply_with,
    )


class ImageFlowTests(unittest.TestCase):
    def service(self, archive):
        return ImageService(
            ImageSettings(
                pg_connection="postgresql://unused", deepseek_api_key="fixture"
            ),
            lambda: SimpleNamespace(
                access="fixture-access", account_id="fixture-account"
            ),
            10001,
            archive=archive,
        )

    def test_review_blocks_then_confirmation_generates_once(self):
        archive = FakeArchive()
        service = self.service(archive)
        blocked = iter([{"decision": "nsfw", "reason": "虚构拦截"}])
        service._call = Mock(
            side_effect=lambda call, job, stopped: (
                "request",
                SimpleNamespace(value=next(blocked)),
            )
        )
        with self.assertRaises(ImageError):
            service.create(message(), Mock(), "虚构图片要求")
        self.assertTrue(archive.blocked)
        self.assertFalse(archive.drafted)

        values = iter(
            [
                {"decision": "pass", "reason": "可以"},
                {"prompt": "润色后"},
                {"decision": "pass", "reason": "可以"},
                {"image_sha256": "fixture-digest"},
            ]
        )
        service._call = Mock(
            side_effect=lambda call, job, stopped: (
                "request",
                SimpleNamespace(value=next(values)),
            )
        )
        preview = service.create(message(), Mock(), "虚构图片要求")
        self.assertIn("润色后", preview)
        self.assertIn("confirm", preview)
        texts = []
        client = Mock()
        self.assertIsNone(service.confirm(message(texts), client, "abcdef12"))
        # 确认后立即回执、成功后再回图，不再追加额外收尾文本。
        self.assertEqual(client.send_message.call_count, 2)
        self.assertIn("已确认", texts[0])
        self.assertTrue(archive.generated)
        with self.assertRaises(image_service.ArchiveError):
            service.confirm(message(), client, "abcdef12")

    def test_direct_skips_rewrite_but_keeps_both_reviews(self):
        archive = FakeArchive()
        service = self.service(archive)
        stages = []
        verdicts = iter(
            [
                {"decision": "pass", "reason": "可以"},
                {"decision": "pass", "reason": "可以"},
            ]
        )

        def record(call, job, stopped):
            stages.append(call.stage)
            return "request", SimpleNamespace(value=next(verdicts))

        service._call = Mock(side_effect=record)
        preview = service.create(message(), Mock(), "虚构图片要求", direct=True)
        self.assertIn("直接生成", preview)
        # direct 不改写提示词，但前审与后审仍是两次独立请求，草稿要求两次 pass。
        self.assertEqual(stages, ["pre_review", "post_review"])
        self.assertTrue(archive.drafted)

    def test_direct_freezes_image_options_for_generation(self):
        archive = FakeArchive()
        service = self.service(archive)
        calls = []
        values = iter(
            [
                {"decision": "pass", "reason": "可以"},
                {"decision": "pass", "reason": "可以"},
                {"image_sha256": "fixture-digest"},
            ]
        )

        def record(call, job, stopped):
            calls.append(call)
            return "request", SimpleNamespace(value=next(values))

        service._call = Mock(side_effect=record)
        lines = []
        service._log_sink = lambda level, message: lines.append((level, message))
        preview = service.create(
            message(),
            Mock(),
            "虚构图片要求",
            direct=True,
            params={"size": "1024x1536", "quality": "high"},
        )
        # 阶段日志交给注入的宿主日志出口，而不是直接 print。
        self.assertIn("draft ready", " ".join(message for _, message in lines))
        self.assertIn("1024x1536", preview)
        # 未显式给出的参数按 auto 冻结，预览展示的就是最终发给上游的值。
        self.assertIn("背景 auto", preview)
        service.confirm(message(), Mock(), "abcdef12")
        self.assertEqual(calls[-1].payload["size"], "1024x1536")
        self.assertEqual(calls[-1].payload["quality"], "high")
        self.assertEqual(calls[-1].payload["background"], "auto")


if __name__ == "__main__":
    unittest.main()
