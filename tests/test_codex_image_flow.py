"""生图命令编排的核心回归：审核拦截与本人确认各一次，不联网、不消耗额度。

真实 SDK 协议由 test_codex_clients.py 覆盖；这里只验证业务流程的放行与拦截。
"""

import json
import unittest
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from plugins.codex_support import image_service
from plugins.codex_support.image_service import ImageError, ImageService, ImageSettings
from plugins.codex_support.providers import Call
from plugins.codex_support.transport import HTTPResult


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
        self.created = 0

    def create_job(self, owner, source, prompt, params=None, *, execution_config=None):
        self.created += 1
        self.job["image_params"] = params or {}
        self.job["execution_config"] = execution_config or {}
        return "abcdef12-3456-7890-abcd-ef1234567890"

    def put_image(self, job, role, data, request=None, position=0):
        return "digest-" + role

    def begin_request(self, job, provider, stage, model, payload):
        return "request-" + stage

    def finish_request(self, request, state, response=None, **kwargs):
        pass

    def prepare_draft(self, job, prompt, pre, post):
        self.job["reviewed_prompt"] = prompt
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
            archive.job["id"], service.scope(msg), msg.id, operator_id=msg.sender_id
        )
        self.assertEqual([call.stage for call in calls], ["generate", "generate"])
        self.assertEqual(calls[1].payload, calls[0].payload)
        self.assertTrue(archive.generated)

    def test_upstream_rejection_shows_structured_details_and_keeps_stopped(self):
        archive = FakeArchive()
        service = self.service(archive)
        message_text = "fixture upstream rejection"
        body = {
            "error": {
                "code": "moderation_blocked",
                "type": "image_generation_user_error",
                "message": message_text,
                "moderation_details": {
                    "moderation_stage": "output",
                    "categories": ["violence", "fixture-category"],
                },
            }
        }
        with patch.object(
            Call, "send", return_value=HTTPResult(400, body, b"", "fixture-request-id")
        ) as send:
            with self.assertRaises(ImageError) as failure:
                service.confirm(message(), Mock(), "abcdef12")
            send.assert_called_once()
        text = str(failure.exception)
        for value in (
            message_text,
            body["error"]["code"],
            body["error"]["type"],
            "fixture-request-id",
            "fixture-category",
        ):
            self.assertIn(value, text)
        self.assertTrue(archive.blocked)
        self.assertFalse(archive.generated)

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

    def test_direct_reviews_once_and_generates_with_frozen_image_options(self):
        archive = FakeArchive()
        service = self.service(archive)
        calls = []
        values = iter(
            [
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
        # 直出只做一次前审，不润色或后审；确认后按冻结值生成。
        self.assertEqual([call.stage for call in calls], ["pre_review", "generate"])
        self.assertTrue(archive.drafted)
        self.assertEqual(calls[-1].payload["prompt"], "虚构图片要求")
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
        verdicts = iter([{"decision": "pass", "reason": "可以"}])

        def record(call, job, stopped):
            stages.append(call.stage)
            return "request", SimpleNamespace(value=next(verdicts))

        service._call = Mock(side_effect=record)
        msg = message()
        msg.reply = {"files": [{"type": "image/png", "url": "https://fixture/1.png"}]}
        with patch.object(image_service, "_download", return_value=b"fixture"):
            service.attach(msg, Mock(), "abcdef12")
        # 追加参考图作废旧草稿，直出仍只做一次包含新参考图的前审。
        self.assertEqual(stages, ["pre_review"])
        self.assertEqual(archive.appended, 1)

    def test_params_can_be_adjusted_before_confirmation(self):
        archive = FakeArchive()
        service = self.service(archive)
        calls = []
        values = iter(
            [
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


class ApiBackendFlowTests(unittest.TestCase):
    """API 计费模式的核心回归：两把 Key 各司其职，任务按冻结后端执行。"""

    def service(self, archive, logs=None, **overrides):
        values = {
            "image_backend": "openai",
            "text_backend": "openai",
            "openai_text_api_key": "fixture-check-key",
            "openai_image_api_key": "fixture-image-key",
        }
        values.update(overrides)
        return ImageService(
            ImageSettings(
                pg_connection="postgresql://unused",
                deepseek_api_key="fixture-deepseek",
                **values,
            ),
            lambda: SimpleNamespace(
                access="fixture-access", account_id="fixture-account"
            ),
            10001,
            archive=archive,
            log=(lambda level, message: logs.append(message))
            if logs is not None
            else None,
        )

    @staticmethod
    def record(service, *values):
        recorded = []
        pending = iter(values)

        def call(prepared, job, stopped):
            recorded.append(prepared)
            return "request", SimpleNamespace(value=next(pending))

        service._call = Mock(side_effect=call)
        return recorded

    def test_api_mode_uses_separate_keys_and_keeps_them_out_of_archive_and_logs(self):
        """防止两把 Key 互借，或 Key 随请求存档、阶段日志泄露到群聊与 PG。"""
        archive = FakeArchive()
        logs = []
        service = self.service(archive, logs=logs)
        recorded = self.record(
            service,
            {"decision": "pass", "reason": "可以"},
            {"image_sha256": "fixture-digest"},
        )
        archive.images = Mock(return_value=[])
        preview = service.create(
            message(), Mock(), "虚构图片要求", direct=True, params={"quality": "max"}
        )
        service.confirm(message(), Mock(), "abcdef12")

        review, generate = recorded
        self.assertEqual((review.provider, review.stage), ("openai", "pre_review"))
        self.assertEqual(review.model, service.settings.openai_text_model)
        self.assertEqual(
            review.payload["reasoning"],
            {"effort": service.settings.openai_reasoning_effort},
        )
        self.assertTrue(review.url.endswith("/responses"))
        self.assertIn("fixture-check-key", review.headers["Authorization"])
        self.assertNotIn("fixture-image-key", review.headers["Authorization"])
        self.assertEqual((generate.provider, generate.stage), ("openai", "generate"))
        self.assertEqual(generate.model, service.settings.openai_image_model)
        self.assertEqual(generate.payload["quality"], "max")
        self.assertTrue(generate.url.endswith("/images/generations"))
        self.assertIn("fixture-image-key", generate.headers["Authorization"])
        self.assertNotIn("fixture-check-key", generate.headers["Authorization"])
        # 任务预览区分独立计费，不会把 API 消耗说成订阅额度。
        self.assertIn("OpenAI API", preview)
        for prepared in recorded:
            archived = json.dumps(prepared.archive_payload, ensure_ascii=False)
            for key in ("fixture-check-key", "fixture-image-key"):
                self.assertNotIn(key, archived)
        for line in logs:
            for key in ("fixture-check-key", "fixture-image-key"):
                self.assertNotIn(key, line)

    def test_edit_path_and_hot_reload_do_not_change_the_frozen_backend(self):
        """防止热重载换后端/模型，或让 Sunburst 的高质量改图被旧模型规则拦截。"""
        archive = FakeArchive()
        service = self.service(
            archive, openai_image_model="gpt-image-2.5-sunburst-2026-09-08"
        )
        recorded = self.record(
            service,
            {"decision": "pass", "reason": "可以"},
            {"prompt": "润色后"},
            {"decision": "pass", "reason": "可以"},
            {"image_sha256": "fixture-digest"},
        )
        service.create(message(), Mock(), "虚构图片要求", params={"quality": "xhigh"})
        frozen = dict(archive.job["execution_config"])
        # 模拟重载：全球配置改回订阅与另一套模型，任务凭据仍可用。
        service.settings = ImageSettings(
            pg_connection="postgresql://unused",
            deepseek_api_key="fixture-deepseek",
            openai_text_api_key="fixture-check-key",
            openai_image_api_key="fixture-image-key",
            openai_image_model="gpt-image-2",
            openai_text_model="another-text-model",
        )
        service.set_params(message(), "abcdef12", {"quality": "max"})
        service.confirm(message(), Mock(), "abcdef12")
        generate = recorded[-1]
        self.assertEqual(generate.payload["quality"], "max")
        self.assertEqual((generate.provider, generate.stage), ("openai", "generate"))
        self.assertEqual(generate.model, frozen["image_model"])
        self.assertTrue(generate.url.endswith("/images/edits"))
        # 官方 edits 只接受 multipart：图片进 files，不能塞回 JSON 正文。
        self.assertEqual([name for name, _ in generate.files], ["image[]"])
        self.assertNotIn("data:image", json.dumps(generate.payload))
        # 审核请求也按任务冻结的模型与推理强度，而不是重载后的全局值。
        for review in recorded[:-1]:
            self.assertEqual(review.model, frozen["text_model"])
            self.assertEqual(
                review.payload["reasoning"], {"effort": frozen["text_reasoning_effort"]}
            )

    def test_old_models_are_rejected_before_review_or_charge(self):
        """即使质量为 high，两个后端的旧模型及冻结旧任务也不能继续使用。"""
        for backend in ("openai", "codex"):
            with self.subTest(backend=backend):
                archive = FakeArchive()
                service = self.service(
                    archive,
                    image_backend=backend,
                    openai_image_model="gpt-image-2",
                    codex_image_model="gpt-image-2",
                )
                service._call = Mock()
                with self.assertRaises(ImageError):
                    service.create(
                        message(),
                        Mock(),
                        "虚构图片要求",
                        direct=True,
                        params={"quality": "high"},
                    )
                self.assertEqual(archive.created, 0)
                service._call.assert_not_called()

                # 全局配置已改为 2.5，历史任务仍按冻结的旧模型拒绝，不自动换模型。
                archive.job["execution_config"] = service.settings.execution_config()
                archive.job["image_params"] = {"quality": "high"}
                current_service = self.service(archive)
                current_service._call = Mock()
                with self.assertRaises(ImageError):
                    current_service.set_params(message(), "abcdef12", {"quality": "max"})
                self.assertEqual(archive.job["image_params"], {"quality": "high"})
                with self.assertRaises(ImageError):
                    current_service.confirm(message(), Mock(), "abcdef12")
                self.assertFalse(archive.confirmed)
                current_service._call.assert_not_called()

    def test_api_size_is_checked_before_creating_a_job(self):
        archive = FakeArchive()
        service = self.service(archive)
        with self.assertRaises(ImageError) as failure:
            service.create(
                message(),
                Mock(),
                "虚构图片要求",
                direct=True,
                params={"quality": "max", "size": "1000x1000"},
            )
        self.assertIn("16", str(failure.exception))
        self.assertEqual(archive.created, 0)

    def test_missing_key_stops_before_charge_without_borrowing_the_other(self):
        """防止一把 Key 冒充另一用途，或在缺 Key 时消费确认后才发现无法生成。"""
        archive = FakeArchive()
        service = self.service(archive, openai_text_api_key="")
        with self.assertRaises(ImageError) as failure:
            service.create(message(), Mock(), "虚构图片要求")
        self.assertEqual(archive.created, 0)
        self.assertNotIn("fixture-image-key", str(failure.exception))

        archive = FakeArchive()
        service = self.service(archive, openai_image_api_key="")
        with self.assertRaises(ImageError) as failure:
            service.create(message(), Mock(), "虚构图片要求")
        self.assertEqual(archive.created, 0)
        self.assertNotIn("fixture-check-key", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
