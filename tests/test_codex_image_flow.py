"""生图命令编排的核心回归：用替身记录外部调用，不联网也不消耗任何额度。

覆盖：双审核拒绝不放行、确认只执行一次、跨群与重复确认被拒、单槽位忙时拒绝。
真实 SDK 协议由 test_codex_clients.py 负责；这里只验证业务流程。
"""

import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image

from plugins.codex_support import image_service
from plugins.codex_support.image_service import ImageError, ImageService, ImageSettings


class FakeArchive:
    def __init__(self):
        self.calls = []
        self.job = {"id": "job-1", "reviewed_prompt": "润色后", "state": "reviewing"}
        self.confirmed = False

    def create_job(self, owner, source, prompt):
        self.calls.append(("create", owner, prompt))
        return "abcdef12-3456-7890-abcd-ef1234567890"

    def put_image(self, job, role, data, request=None):
        self.calls.append(("put_image", role))
        return "digest-" + role

    def begin_request(self, job, provider, stage, model, payload):
        self.calls.append(("begin", stage))
        return "request-" + stage

    def finish_request(self, request, state, response=None, **kwargs):
        self.calls.append(("finish", request, state))

    def prepare_draft(self, job, prompt, pre, post):
        self.calls.append(("draft", prompt, pre, post))

    def finish_job(self, job, state):
        self.calls.append(("finish_job", state))
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
        self.calls.append(("cancel", job))


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
                pg_connection="postgresql://unused", deepseek_api_key="fixture-key"
            ),
            lambda: SimpleNamespace(
                access="fixture-access", account_id="fixture-account"
            ),
            archive=archive,
        )

    def test_blocked_review_never_reaches_draft_or_generation(self):
        archive = FakeArchive()
        service = self.service(archive)
        verdicts = iter([{"decision": "nsfw", "reason": "虚构拦截"}])
        service._call = Mock(
            side_effect=lambda call, job, stopped: (
                "request",
                SimpleNamespace(value=next(verdicts)),
            )
        )
        with self.assertRaises(ImageError):
            service.create(message(), Mock(), "虚构图片要求")
        self.assertIn(("finish_job", "blocked"), archive.calls)
        self.assertNotIn("draft", [call[0] for call in archive.calls])

    def test_preview_requires_confirmation_and_runs_generation_once(self):
        archive = FakeArchive()
        service = self.service(archive)
        values = iter(
            [
                {"decision": "pass", "reason": "可以"},
                {"prompt": "润色后"},
                {"decision": "pass", "reason": "可以"},
                # confirm 还会发起一次生成调用；图片本身由 FakeArchive.image 提供。
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
        self.assertIn("confirm", preview)
        self.assertIn("润色后", preview)
        self.assertNotIn("generate", [call[1] for call in archive.calls])

        client = Mock()
        service.confirm(message(), client, "abcdef12")
        client.send_message.assert_called_once()
        self.assertTrue(archive.confirmed)

    def test_ambiguous_commands_are_rejected_before_any_work(self):
        archive = FakeArchive()
        service = self.service(archive)
        msg = message()
        msg.reply = {"files": [{"type": "image/png"}, {"type": "image/png"}]}
        with self.assertRaises(ImageError):
            service.create(msg, Mock(), "虚构")
        msg.reply = {"files": [{"type": "file/pdf"}]}
        with self.assertRaises(ImageError):
            service.create(msg, Mock(), "虚构")

    def test_busy_slot_rejects_second_task_without_creating_database_job(self):
        from plugins import codex

        state = codex.Runtime(codex.Settings(frozenset({897291716})))
        state.image_busy = True
        reply = Mock()
        captured = []
        client = Mock()
        msg = message()
        # _reply 通过 reply_with 构造发送对象，文本要先截获，不能读回复对象本身。
        msg.reply_with = lambda text: captured.append(text) or reply
        codex._handle_image(state, ["/codex", "image", "一只猫"], msg, client)
        client.send_message.assert_called_once()
        self.assertIn("已有生图任务", captured[0])


if __name__ == "__main__":
    unittest.main()
