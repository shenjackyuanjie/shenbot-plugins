"""用 OpenAI SDK 承担认证、连接、代理和响应读取，不另写通用 HTTP 客户端。

所有请求统一走 SDK 公开的 post 入口，直接取得原始 HTTPX2 响应：
这样能保留原始状态码和体积上限，也不会因 SDK 版本差异依赖内部响应包装。
同步调用由插件自己的有界 worker 承载；取消只阻止后续阶段与回图，不承诺撤销上游。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx2
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    DefaultHttpx2Client,
    OpenAI,
)

# 群命令只能触达这两个订阅图片路径，不能把任意相对路径交给 SDK。
IMAGE_ENDPOINTS = {
    "https://chatgpt.com/backend-api/codex/images/generations": "/images/generations",
    "https://chatgpt.com/backend-api/codex/images/edits": "/images/edits",
}


class TransportError(RuntimeError):
    """只暴露固定分类，不透传可能带代理认证、请求头或原始 URL 的 SDK 异常。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class HTTPResult:
    status: int
    body: dict | None
    raw: bytes = field(repr=False)
    request_id: str | None = None


def post_json(
    url: str,
    headers: dict,
    payload: dict,
    *,
    timeout: float,
    maximum_bytes: int,
    stopped: threading.Event,
    http_client: httpx2.Client | None = None,
) -> HTTPResult:
    """外层先登记请求，再发一次；显式关闭 SDK 重试和重定向，收费请求不静默重复。"""
    if stopped.is_set():
        raise TransportError("CANCELLED_BEFORE_DISPATCH")
    authorization = headers.get("Authorization", "")
    if not authorization.startswith("Bearer ") or not authorization[7:]:
        raise TransportError("INVALID_CREDENTIAL")
    # 认证由 SDK 从 api_key 生成，其余头原样透传，避免重复或覆盖 Authorization。
    extra_headers = {
        key: value for key, value in headers.items() if key.lower() != "authorization"
    }
    if (
        url.endswith("/chat/completions")
        and urlsplit(url).hostname == "api.deepseek.com"
    ):
        base_url, path = "https://api.deepseek.com/v1", "/chat/completions"
    elif url in IMAGE_ENDPOINTS:
        base_url, path = "https://chatgpt.com/backend-api/codex", IMAGE_ENDPOINTS[url]
    else:
        raise TransportError("INVALID_API_URL")
    # SDK 的 close() 也会关闭传入的传输，因此外部注入时由调用方管理生命周期。
    client = OpenAI(
        api_key=authorization[7:],
        base_url=base_url,
        default_headers=extra_headers,
        max_retries=0,
        timeout=timeout,
        http_client=http_client or DefaultHttpx2Client(follow_redirects=False),
    )
    try:
        try:
            response = client.post(path, cast_to=httpx2.Response, body=payload)
        except APIStatusError as error:
            response = error.response
        data = response.content
        if len(data) > maximum_bytes:
            raise TransportError("RESPONSE_TOO_LARGE")
        try:
            decoded = json.loads(data)
            if not isinstance(decoded, dict):
                decoded = None
        except (ValueError, UnicodeError):
            decoded = None
        request_id = response.headers.get("x-request-id") or response.headers.get(
            "openai-request-id"
        )
        if request_id is not None:
            # 只保留可见字符的短关联号，避免把异常响应头原样带进存档。
            request_id = "".join(
                char for char in request_id[:160] if char.isprintable()
            )
        return HTTPResult(response.status_code, decoded, data, request_id)
    except APITimeoutError:
        raise TransportError("TIMEOUT_UNCERTAIN") from None
    except APIConnectionError:
        raise TransportError("NETWORK_UNCERTAIN") from None
    finally:
        if http_client is None:
            client.close()
