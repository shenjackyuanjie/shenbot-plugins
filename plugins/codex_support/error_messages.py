"""把已知上游错误整理为中文说明；只展示，不改变停止或手动重试规则。

映射依据 OpenAI 的 error-codes 与 image-generation 指南；订阅后端未公开的错误保留原码，
按类型或 HTTP 状态兜底，不靠英文错误句子猜分类，也不为未使用的 API 建错误全集。
"""

from __future__ import annotations

import re

# 兼容旧响应把 insufficient_quota 放在 code、新响应作为宽泛 type 的两种形式。
QUOTA_HINT = (
    "上游余额或用量额度不足",
    "请检查对应账号的余额或额度；订阅额度可用 /codex check 查看。",
)

# 优先使用服务端稳定 code，避免把所有 HTTP 429 都误报成请求频率过高。
CODE_HINTS = {
    "insufficient_quota": QUOTA_HINT,
    "moderation_blocked": (
        "上游内容审核未通过",
        "请调整描述或参考图后再试；管理员可用 retry 重试。",
    ),
    "credit_balance_exhausted": (
        "账户预付余额已用完",
        "请充值后再试。",
    ),
    "organization_spend_limit_exceeded": (
        "已达到组织支出上限",
        "请在控制台调高组织支出上限后再试。",
    ),
    "project_spend_limit_exceeded": (
        "已达到项目支出上限",
        "请在控制台调高项目支出上限后再试。",
    ),
    "organization_usage_limit_exceeded": (
        "已达到组织用量上限",
        "请在控制台检查或申请提高用量上限。",
    ),
    "slow_down": ("请求太快了", "稍等一会再试。"),
    "server_is_overloaded": (
        "上游模型当前过载",
        "稍等一会再重试。",
    ),
}
TYPE_HINTS = {
    "image_generation_user_error": (
        "上游无法按当前输入生成图片",
        "请根据提示调整描述或参考图后再试。",
    ),
    "invalid_request_error": (
        "请求参数或输入有误",
        "请检查参数、描述和参考图后再试。",
    ),
    "authentication_error": (
        "身份认证失败",
        "请检查凭据：订阅模式在 DSH 刷新登录，API 模式检查对应的 API Key。",
    ),
    "permission_error": (
        "当前账号没有访问权限",
        "请检查账号、模型权限和所在地区。",
    ),
    "insufficient_quota": QUOTA_HINT,
    "rate_limit_error": ("请求频率受限", "稍等一会再试。"),
    "server_error": (
        "上游服务出错了",
        "稍等一会再重试。",
    ),
    "service_unavailable_error": ("上游服务暂时不可用", "稍等一会再试。"),
}
HTTP_HINTS = {
    400: TYPE_HINTS["invalid_request_error"],
    401: TYPE_HINTS["authentication_error"],
    403: TYPE_HINTS["permission_error"],
    404: ("找不到对应的模型或资源", "请检查配置中的模型名称和接口地址。"),
    408: ("上游请求超时", "可以先查看任务记录确认状态。"),
    409: ("上游资源状态冲突", "请稍后查看任务记录再试。"),
    413: ("请求内容太大了", "请减少参考图数量或压缩图片后再试。"),
    415: ("不支持该图片格式", "请更换图片格式后再试。"),
    422: ("上游无法处理当前输入", "请检查参数和参考图后再试。"),
    429: (
        "请求频率或额度受限",
        "请检查限流、余额或额度后再试。",
    ),
    500: TYPE_HINTS["server_error"],
    502: ("上游网关出错", "稍等一会再试。"),
    503: TYPE_HINTS["service_unavailable_error"],
    504: ("上游网关超时", "可以先查看任务记录确认状态。"),
}
LOCAL_HINTS = {
    "CONTENT_REJECTED": CODE_HINTS["moderation_blocked"],
    "AUTH_EXPIRED": TYPE_HINTS["authentication_error"],
    "ACCESS_REFUSED": TYPE_HINTS["permission_error"],
    "RATE_LIMITED": HTTP_HINTS[429],
    "TIMEOUT_UNCERTAIN": (
        "请求超时",
        "请查看任务记录确认结果。",
    ),
    "NETWORK_UNCERTAIN": (
        "网络连接中断",
        "请查看任务记录确认结果。",
    ),
    "CANCELLED_BEFORE_DISPATCH": ("请求已取消", "任务已停止。"),
    "INVALID_JSON": (
        "上游返回内容无法解析",
        "请查看任务记录确认结果。",
    ),
    "INVALID_REWRITE": (
        "润色结果格式异常",
        "请重新发起任务。",
    ),
    "INVALID_IMAGE_DATA": (
        "上游返回的图片数据无法解析",
        "请查看任务记录或重新发起。",
    ),
    "INVALID_PNG": ("上游返回的图片格式无效", "请查看任务记录或重新发起。"),
    "EXPECTED_ONE_IMAGE": (
        "上游返回的图片数量不对",
        "请查看任务记录或重新发起。",
    ),
    "INVALID_REFERENCE": (
        "参考图无法识别或格式不支持",
        "请换用 PNG、JPEG 或 WebP 图片。",
    ),
    "REFERENCE_TOO_LARGE": ("参考图太大了", "请压缩或减少参考图后再试。"),
    "INVALID_DEEPSEEK_CONFIGURATION": (
        "审核或润色配置有误",
        "请检查 DeepSeek 模型、接口和密钥配置。",
    ),
    "INVALID_CODEX_CONFIGURATION": (
        "生图配置有误",
        "请检查图片模型和尺寸、质量、背景参数。",
    ),
    "INVALID_REVIEW": (
        "审核返回内容异常",
        "请重新发起任务。",
    ),
    "INVALID_OPENAI_CONFIGURATION": (
        "OpenAI API 配置有误",
        "请检查 openai_base_url、模型名和 reasoning effort 配置。",
    ),
    "INVALID_HTTP_CLIENT": (
        "代理配置有误",
        "请检查 openai_proxy 是否为有效的 HTTP(S) 代理地址。",
    ),
    "INVALID_CREDENTIAL": TYPE_HINTS["authentication_error"],
    "INVALID_API_URL": ("接口地址不受支持", "请检查插件接口配置。"),
    "RESPONSE_TOO_LARGE": (
        "上游返回内容太大了",
        "请查看任务记录确认结果。",
    ),
}
# Responses 官方错误枚举中的图片输入类，以及常见认证/资源码；仅用于说明返回值。
CODE_HINTS.update(
    {
        "invalid_api_key": TYPE_HINTS["authentication_error"],
        "missing_scope": (
            "该 Key 没有当前接口权限",
            "请确认文本审核 Key 和生图 Key 各自配置正确。",
        ),
        "model_not_found": HTTP_HINTS[404],
        "unsupported_country_region_territory": (
            "当前所在地区不受上游支持",
            "请检查上游支持地区和账号访问条件。",
        ),
        "server_error": TYPE_HINTS["server_error"],
        "rate_limit_exceeded": TYPE_HINTS["rate_limit_error"],
        "invalid_prompt": (
            "上游无法接受当前描述",
            "请调整描述后再试。",
        ),
        "invalid_image": LOCAL_HINTS["INVALID_REFERENCE"],
        "invalid_image_format": (
            "参考图格式无效",
            "请转换为支持的格式后再试。",
        ),
        "invalid_base64_image": (
            "参考图编码无效",
            "请重新发送图片。",
        ),
        "invalid_image_url": (
            "参考图地址无效",
            "请重新发送可访问的图片。",
        ),
        "image_too_large": ("参考图尺寸太大了", "请缩小图片尺寸后再试。"),
        "image_too_small": ("参考图尺寸太小了", "请换一张更清晰的图片。"),
        "image_parse_error": LOCAL_HINTS["INVALID_REFERENCE"],
        "image_content_policy_violation": (
            "参考图未通过上游审核",
            "请调整参考图后再试。",
        ),
        "invalid_image_mode": (
            "参考图色彩模式不支持",
            "请转换图片色彩模式后再试。",
        ),
        "image_file_too_large": LOCAL_HINTS["REFERENCE_TOO_LARGE"],
        "unsupported_image_media_type": (
            "参考图类型不支持",
            "请检查图片格式与后缀。",
        ),
        "empty_image_file": ("参考图是空的", "请重新发送有效图片。"),
        "failed_to_download_image": (
            "上游无法下载参考图",
            "请重新发送图片后再试。",
        ),
        "image_file_not_found": (
            "上游找不到参考图",
            "请重新发送图片后再试。",
        ),
    }
)

# 官方公开粗粒度类别；这里只转述上游判定，不自行断言用户内容确实违规。
CATEGORY_NAMES = {
    "harassment": "骚扰",
    "self-harm": "自伤",
    "sexual": "色情或性内容",
    "violence": "暴力",
}
STAGE_NAMES = {
    "input": "输入（描述或参考图）",
    "output": "输出（生成结果）",
    "unknown": "上游未确定",
}


def _text(value: object, limit: int = 160) -> str:
    return "".join(
        char for char in str(value) if char.isprintable() or char == "\n"
    ).strip()[:limit]


def format_error(error: Exception) -> str:
    """具体 code → type → HTTP 状态兜底，未知值仍原样显示，不改业务裁决。"""
    if not hasattr(error, "http_status") and not hasattr(error, "code"):
        return "原因：" + _text(error, 1500)
    local_code = getattr(error, "code", None) or str(error)
    details = getattr(error, "details", {})
    code = _text(details["code"]) if details.get("code") is not None else None
    kind = _text(details["type"]) if details.get("type") is not None else None
    status = getattr(error, "http_status", None)
    label, hint = (
        CODE_HINTS.get(code)
        or TYPE_HINTS.get(kind)
        or LOCAL_HINTS.get(local_code)
        or HTTP_HINTS.get(status)
        or (
            HTTP_HINTS[500] if isinstance(status, int) and 500 <= status < 600 else None
        )
        or ("请求未能完成", "请查看上游说明和任务记录。")
    )
    lines = [f"原因：{label}"]
    moderation = details.get("moderation_details")
    moderation = moderation if isinstance(moderation, dict) else {}
    categories = moderation.get("categories")
    message = getattr(error, "upstream_message", "")
    if not isinstance(categories, list):
        # 兼容订阅后端旧响应中的明确机器字段，不从自然语言推断审核理由。
        match = re.search(r"safety_violations=\[([^\]]*)\]", message)
        categories = (
            [part.strip().strip("\"'") for part in match[1].split(",")] if match else []
        )
    if code == "moderation_blocked" or local_code == "CONTENT_REJECTED":
        names = [
            f"{CATEGORY_NAMES[item]}（{item}）"
            if item in CATEGORY_NAMES
            else _text(item)
            for item in categories
            if isinstance(item, str) and item
        ]
        if names:
            lines.append("上游标记：" + "、".join(names[:8]))
        stage = moderation.get("moderation_stage")
        if isinstance(stage, str):
            lines.append("审核阶段：" + STAGE_NAMES.get(stage, _text(stage)))
    # 本地归类错误的 http_status 可能沿用响应码（如解析失败时的 200），只在真实 HTTP 失败时展示。
    if isinstance(status, int) and 400 <= status <= 599:
        lines.append(f"HTTP：{status}")
    lines.append("错误码：" + _text(code or local_code))
    if kind:
        lines.append("错误类型：" + _text(kind))
    if details.get("param"):
        lines.append("相关参数：" + _text(details["param"]))
    request_id = getattr(error, "request_id", None) or details.get("request_id")
    if not request_id:
        match = re.search(r"request ID ([A-Za-z0-9_-]+)", message, re.IGNORECASE)
        request_id = match[1] if match else None
    if request_id:
        lines.append("请求 ID：" + _text(request_id))
    if message:
        lines.append("上游原文：" + _text(message, 1500))
    lines.append("建议：" + hint)
    return "\n".join(lines)
