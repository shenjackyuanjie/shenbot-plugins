"""SDK 直连协议的核心回归：required 审核、收费请求不重试、图片严格解析。

只保留一条完整流程，不枚举畸形分支，也不访问网络。
"""

import base64
import json
import threading
import unittest
from io import BytesIO

import httpx2
from PIL import Image

from plugins.codex_support.error_messages import format_error
from plugins.codex_support.providers import (
    ProviderError,
    codex_call,
    deepseek_call,
    openai_image_call,
    openai_text_call,
)
from plugins.codex_support.review import ReviewError, parse_responses_verdict
from plugins.codex_support.transport import HTTPResult, post_json, post_multipart


def verdict(name):
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "fixture-call",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": '{"reason": "虚构裁决"}',
                            },
                        }
                    ],
                },
            }
        ]
    }


class SDKFlowTests(unittest.TestCase):
    def test_review_rewrite_and_subscription_image_share_one_sdk_path(self):
        """一条流程覆盖：审核 required、润色 high 思考、图片回包与失败不重试。"""
        stream = BytesIO()
        Image.new("RGB", (2, 2), "green").save(stream, "PNG")
        image = stream.getvalue()
        seen = []

        def handle(request):
            body = json.loads(request.content)
            seen.append((request.url.path, body))
            if request.url.path.endswith("/chat/completions"):
                if body.get("tool_choice") == "required":
                    return httpx2.Response(200, json=verdict("pass"))
                return httpx2.Response(
                    200,
                    json={
                        "choices": [
                            {"finish_reason": "stop", "message": {"content": "润色后"}}
                        ]
                    },
                )
            return httpx2.Response(
                200,
                json={
                    "data": [{"b64_json": base64.b64encode(image).decode()}],
                    "model": body["model"],
                },
            )

        client = httpx2.Client(
            transport=httpx2.MockTransport(handle), follow_redirects=False
        )

        def send(call):
            return post_json(
                call.url,
                call.headers,
                call.payload,
                timeout=1,
                maximum_bytes=100000,
                stopped=threading.Event(),
                http_client=client,
            )

        review = deepseek_call(
            "https://api.deepseek.com",
            "fixture-key",
            "deepseek-flash",
            "pre_review",
            "纸鹤",
            images=(image,),
        )
        self.assertEqual(review.parse(send(review)).value["decision"], "pass")
        self.assertNotIn("data:image", json.dumps(review.archive_payload))

        rewrite = deepseek_call(
            "https://api.deepseek.com",
            "fixture-key",
            "deepseek-flash",
            "rewrite",
            "纸鹤",
        )
        self.assertEqual(rewrite.payload["thinking"], {"type": "enabled"})
        self.assertEqual(rewrite.payload["reasoning_effort"], "high")
        rewrite.parse(send(rewrite))

        edit = codex_call(
            "润色后", "fixture-access", "fixture-account", images=(image,)
        )
        parsed = edit.parse(send(edit))
        self.assertEqual(parsed.images[0][1], image)
        self.assertNotIn("b64_json", json.dumps(parsed.value))
        self.assertEqual(seen[0][1]["thinking"], {"type": "disabled"})

        # 正文声称通过但不是工具调用时绝不能放行。
        with self.assertRaises(ReviewError):
            review.parse(
                HTTPResult(
                    200,
                    {
                        "choices": [
                            {"finish_reason": "stop", "message": {"content": "通过"}}
                        ]
                    },
                    b"",
                )
            )

        # 收费请求 503 时 SDK 只能发一次。
        attempts = []
        failing = httpx2.Client(
            transport=httpx2.MockTransport(
                lambda request: (
                    attempts.append(request)
                    or httpx2.Response(503, json={"error": {"type": "server_error"}})
                )
            ),
            follow_redirects=False,
        )
        result = post_json(
            edit.url,
            edit.headers,
            edit.payload,
            timeout=1,
            maximum_bytes=100000,
            stopped=threading.Event(),
            http_client=failing,
        )
        self.assertEqual((result.status, len(attempts)), (503, 1))

    def test_openai_image_edit_uploads_multipart_while_generation_stays_json(self):
        """官方 images/edits 只接受表单上传；改图退回 JSON 会被上游拒绝。"""
        stream = BytesIO()
        Image.new("RGB", (2, 2), "green").save(stream, "PNG")
        image = stream.getvalue()
        seen = []

        def handle(request):
            seen.append(request)
            return httpx2.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(image).decode()}]},
            )

        client = httpx2.Client(
            transport=httpx2.MockTransport(handle), follow_redirects=False
        )
        edit = openai_image_call(
            "改图", "fixture-key", base_url="https://api.openai.com/v1", images=(image,)
        )
        result = post_multipart(
            edit.url,
            edit.headers,
            edit.payload,
            list(edit.files),
            timeout=1,
            maximum_bytes=100000,
            stopped=threading.Event(),
            http_client=client,
        )
        edit.parse(result)
        request = seen[0]
        self.assertIn("multipart/form-data", request.headers["content-type"])
        for expected in (b'name="image[]"', b'name="model"', b"reference-1.png"):
            self.assertIn(expected, request.content)

        # 文生图仍是 JSON，不引入 multipart。
        generate = openai_image_call(
            "画图", "fixture-key", base_url="https://api.openai.com/v1"
        )
        self.assertEqual(generate.files, ())
        self.assertNotIn("images", generate.payload)

    def test_responses_review_accepts_real_shape_and_rejects_incomplete_output(self):
        """防止上游多带字段或空正文被误杀，也防止截断回包被当成裁决放行。"""
        reasoning = {
            "type": "reasoning",
            "summary": [],
            "encrypted_content": "fixture-reasoning",
        }
        call = {
            "type": "function_call",
            "name": "pass",
            "call_id": "fixture-call",
            "arguments": json.dumps(
                {"reason": "普通宠物场景", "confidence": 0.99}, ensure_ascii=False
            ),
        }
        empty_message = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": ""}],
        }
        verdict = parse_responses_verdict(
            {
                "status": "completed",
                "output_text": "",
                "output": [reasoning, call, empty_message],
            }
        )
        self.assertEqual((verdict.decision, verdict.reason), ("pass", "普通宠物场景"))

        # 有真实正文时仍拒绝：正文不能覆盖工具裁决。
        with self.assertRaises(ReviewError):
            parse_responses_verdict(
                {
                    "status": "completed",
                    "output": [
                        {
                            **empty_message,
                            "content": [{"type": "output_text", "text": "通过"}],
                        },
                        call,
                    ],
                }
            )
        # 截断回包必须拒绝，即使工具调用看起来完整。
        with self.assertRaises(ReviewError):
            parse_responses_verdict(
                {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [call],
                }
            )

    def test_openai_api_rejections_are_explicit_and_never_retry_or_switch_model(self):
        """防止 API 模式把 401/429/模型不可用悄悄重发、换模型或给出模棱两可的回执。"""
        call = openai_text_call(
            "https://api.openai.com/v1",
            "fixture-key",
            "gpt-6-luna",
            "pre_review",
            "纸鹤",
        )
        for status, code, kind in (
            (401, "invalid_api_key", "invalid_request_error"),
            (429, "rate_limit_exceeded", "rate_limit_error"),
            (404, "model_not_found", "invalid_request_error"),
        ):
            attempts = []
            reply = {"error": {"code": code, "type": kind, "message": "fixture"}}

            def handle(request, status=status, reply=reply, attempts=attempts):
                attempts.append(request)
                return httpx2.Response(status, json=reply)

            client = httpx2.Client(
                transport=httpx2.MockTransport(handle), follow_redirects=False
            )
            result = post_json(
                call.url,
                call.headers,
                call.payload,
                timeout=1,
                maximum_bytes=100000,
                stopped=threading.Event(),
                http_client=client,
            )
            with self.assertRaises(ProviderError) as failure:
                call.parse(result)
            text = format_error(failure.exception)
            # 一次拒绝只发一次请求，且请求仍是配置的模型，没有自动回退。
            self.assertEqual(len(attempts), 1)
            self.assertEqual(json.loads(attempts[0].content)["model"], call.model)
            # 错误码是用户判断原因的依据；提示怎么措辞不归测试管。
            self.assertIn(code, text)

    def test_error_codes_distinguish_quota_causes_and_preserve_unknown_errors(self):
        # 同样的 HTTP 429/insufficient_quota 也可能是余额耗尽或项目支出上限，不能一律提示稍后重试。
        call = codex_call("fixture prompt", "fixture-access", "fixture-account")

        def render(status, body):
            with self.assertRaises(ProviderError) as failure:
                call.parse(HTTPResult(status, body, b""))
            return format_error(failure.exception)

        credit = render(
            429,
            {
                "error": {
                    "code": "credit_balance_exhausted",
                    "type": "insufficient_quota",
                }
            },
        )
        project = render(
            429,
            {
                "error": {
                    "code": "project_spend_limit_exceeded",
                    "type": "insufficient_quota",
                }
            },
        )
        self.assertNotEqual(credit.splitlines()[0], project.splitlines()[0])
        # 旧额度码即使配有宽泛的 rate_limit_error，仍必须优先按额度解释。
        legacy_quota = render(
            429, {"error": {"code": "insufficient_quota", "type": "rate_limit_error"}}
        )
        rate_limit = render(429, {"error": {"type": "rate_limit_error"}})
        self.assertNotEqual(legacy_quota.splitlines()[0], rate_limit.splitlines()[0])
        future = {
            "error": {
                "code": "fixture_future_code",
                "message": "fixture future explanation",
            }
        }
        unknown = render(520, future)
        self.assertIn(future["error"]["code"], unknown)
        self.assertIn(future["error"]["message"], unknown)


if __name__ == "__main__":
    unittest.main()
