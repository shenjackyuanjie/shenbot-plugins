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

    def create_job(self, owner, source, prompt):
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
        client = Mock()
        service.confirm(message(), client, "abcdef12")
        client.send_message.assert_called_once()
        self.assertTrue(archive.generated)
        with self.assertRaises(image_service.ArchiveError):
            service.confirm(message(), client, "abcdef12")


if __name__ == "__main__":
    unittest.main()
