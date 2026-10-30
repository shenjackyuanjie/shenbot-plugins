"""生图命令编排的核心回归：审核拦截与本人确认各一次，不联网、不消耗额度。

真实 SDK 协议由 test_codex_clients.py 覆盖；这里只验证业务流程的放行与拦截。
"""

import unittest
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from plugins.codex_support import image_service
from plugins.codex_support.image_service import ImageError, ImageService, ImageSettings


class FakeArchive:
    """只记录关键状态转换，不模拟 SQL；真实持久化由 test_codex_archive 覆盖。"""

    def __init__(self):
        self.job = {
            "id": "job-1",
            "original_prompt": "虚构图片要求",
            "reviewed_prompt": "润色后",
            "state": "reviewing",
            "sender_id": 10003,
        }
        self.appended = 0
        self.blocked = False
        self.drafted = False
        self.confirmed = False
        self.generated = False

    def create_job(self, owner, source, prompt, params=None):
        self.job["image_params"] = params or {}
        return "abcdef12-3456-7890-abcd-ef1234567890"

    def put_image(self, job, role, data, request=None, position=0):
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

    def resolve_job(self, prefix, owner):
        return self.job

    def update_image_params(self, job, owner, params):
        self.job["image_params"] = {**self.job.get("image_params", {}), **params}
        return self.job["image_params"]

    def claim_confirmation(self, job, owner):
        if self.confirmed:
            raise image_service.ArchiveError("已确认")
        self.confirmed = True
        return self.job

    def image(self, job, role):
        stream = BytesIO()
        Image.new("RGB", (2, 2), "red").save(stream, "PNG")
        return stream.getvalue()

    def images(self, job, role):
        return [self.image(job, role)]

    def append_references(self, job, owner, images):
        self.appended = len(images)
        return len(images)

    def has_stage(self, job, stage):
        return False

    def cancel(self, job, owner):
        pass


def message():
    return SimpleNamespace(
        id="msg-1",
        room_id=-897291716,
        sender_id=10003,
        account=10001,
        reply=None,
        reply_with=lambda text: SimpleNamespace(set_img=lambda *a: None),
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
        service.create(message(), Mock(), "虚构图片要求")
        client = Mock()
        self.assertIsNone(service.confirm(message(), client, "abcdef12"))
        # 确认后先回执、成功后再回图，且不再追加额外收尾文本。
        self.assertEqual(client.send_message.call_count, 2)
        self.assertTrue(archive.generated)
        with self.assertRaises(image_service.ArchiveError):
            service.confirm(message(), client, "abcdef12")

    def test_http_failure_can_retry_the_frozen_generation_without_rewriting(self):
        # 防止失败后无法恢复，或恢复时重新润色/丢参数与参考图；防重由真实 PG 验证。
        archive = FakeArchive()
        archive.job["image_params"] = {"quality": "high", "size": "1024x1536"}
        archive.claim_retry = Mock(return_value=archive.job)
        service = self.service(archive)
        calls = []

        def generate(call, job, stopped):
            calls.append(call)
            if len(calls) == 1:
                raise image_service.ProviderError(
                    "UPSTREAM_HTTP_ERROR", http_status=400
                )
            return "request", SimpleNamespace(value={})

        service._call = Mock(side_effect=generate)
        msg, client = message(), Mock()
        with self.assertRaises(ImageError):
            service.confirm(msg, client, "abcdef12")
        msg.id = "fixture-retry"
        service.confirm(msg, client, "abcdef12", retry=True)
        archive.claim_retry.assert_called_once_with(
            archive.job["id"], service.scope(msg), msg.id
        )
        self.assertEqual([call.stage for call in calls], ["generate", "generate"])
        self.assertEqual(calls[1].payload, calls[0].payload)
        self.assertTrue(archive.generated)

    def test_cancel_during_generation_does_not_deliver_late_result(self):
        archive = FakeArchive()
        service = self.service(archive)

        def complete_after_cancel(call, job, stopped):
            archive.finish_job = Mock(return_value=False)
            return "request", SimpleNamespace(value={})

        service._call = Mock(side_effect=complete_after_cancel)
        client = Mock()
        service.confirm(message(), client, "abcdef12")
        # 只有生成前回执；取消后迟到的结果不再自动回图。
        self.assertEqual(client.send_message.call_count, 1)

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
        service.create(message(), Mock(), "虚构图片要求", direct=True)
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
        service.create(
            message(),
            Mock(),
            "虚构图片要求",
            direct=True,
            params={"size": "1024x1536", "quality": "high"},
        )
        service.confirm(message(), Mock(), "abcdef12")
        # 未显式给出的参数按 auto 冻结，确认后按冻结值请求上游。
        self.assertEqual(calls[-1].payload["size"], "1024x1536")
        self.assertEqual(calls[-1].payload["quality"], "high")
        self.assertEqual(calls[-1].payload["background"], "auto")

    def test_multiple_referenced_images_keep_message_order(self):
        first, second = BytesIO(), BytesIO()
        Image.new("RGB", (2, 2), "red").save(first, "PNG")
        Image.new("RGB", (2, 2), "blue").save(second, "PNG")
        msg = message()
        msg.reply = {
            "files": [
                {"type": "image/png", "url": "https://fixture/1.png"},
                {"type": "image/png", "url": "https://fixture/2.png"},
            ]
        }
        # 总量上限作为独立守卫，避免一次引用把审核请求撑爆。
        with patch.object(
            image_service,
            "_download",
            side_effect=[first.getvalue(), second.getvalue()],
        ):
            collected = image_service.reference_images(Mock(), msg)
        # 多图按消息中的顺序全部作为参考，不再因为多图直接拒绝。
        self.assertEqual(collected, [first.getvalue(), second.getvalue()])

    def test_status_without_id_lists_recent_jobs(self):
        archive = FakeArchive()
        service = self.service(archive)
        archive.pending_job = Mock(side_effect=image_service.ArchiveError("没有"))
        archive.recent_jobs = Mock(
            return_value=[
                {
                    "short": "abcdef12",
                    "state": "failed",
                    "created_at": datetime(2026, 10, 26, 21, 58),
                }
            ]
        )
        text = service.status(message(), None)
        # 没有编号时列出最近任务，任务号必须出现。
        self.assertIn("abcdef12", text)

    def test_attach_appends_references_and_reviews_again(self):
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
        msg = message()
        msg.reply = {"files": [{"type": "image/png", "url": "https://fixture/1.png"}]}
        with patch.object(image_service, "_download", return_value=b"fixture"):
            service.attach(msg, Mock(), "abcdef12")
        # 追加参考图作废旧草稿，必须重新跑前审和后审再冻结。
        self.assertEqual(stages, ["pre_review", "post_review"])
        self.assertEqual(archive.appended, 1)

    def test_params_can_be_adjusted_before_confirmation(self):
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
        service.create(message(), Mock(), "虚构图片要求", direct=True)
        service.set_params(
            message(), "abcdef12", {"quality": "high", "background": "transparent"}
        )
        # 调整只合并给出的键，其余保持创建时的值，确认后按新参数生成。
        service.confirm(message(), Mock(), "abcdef12")
        self.assertEqual(calls[-1].payload["quality"], "high")
        self.assertEqual(calls[-1].payload["background"], "transparent")
        self.assertEqual(calls[-1].payload["size"], "auto")


if __name__ == "__main__":
    unittest.main()
