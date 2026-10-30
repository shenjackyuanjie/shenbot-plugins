"""把已知上游错误整理为中文说明；只展示，不改变停止或手动重试规则。

映射依据 OpenAI 的 error-codes 与 image-generation 指南；订阅后端未公开的错误保留原码，
按类型或 HTTP 状态兜底，不靠英文错误句子猜分类，也不为未使用的 API 建错误全集。
"""

from __future__ import annotations

import re

# 兼容旧响应把 insufficient_quota 放在 code、新响应作为宽泛 type 的两种形式。
QUOTA_HINT = (
    "上游余额或用量额度不足",
    "先检查对应账号的余额和额度；订阅额度可用 /codex check 查询。",
)

# 优先使用服务端稳定 code，避免把所有 HTTP 429 都误报成请求频率过高。
CODE_HINTS = {
    "insufficient_quota": QUOTA_HINT,
    "moderation_blocked": (
        "上游内容审核未通过",
        "调整描述或参考图后手动重新发起；原任务保持停止。",
    ),
    "credit_balance_exhausted": (
        "上游账户预付余额已用完",
        "请账户管理员处理余额，再手动操作。",
    ),
    "organization_spend_limit_exceeded": (
        "已达到组织支出上限",
        "请账户管理员调整组织支出上限，再手动操作。",
    ),
    "project_spend_limit_exceeded": (
        "已达到项目支出上限",
        "请账户管理员调整项目支出上限，再手动操作。",
    ),
    "organization_usage_limit_exceeded": (
        "已达到组织用量上限",
        "请账户管理员检查或申请提高用量上限。",
    ),
    "slow_down": ("请求速率增长过快", "降低请求频率，等待限流恢复后再手动操作。"),
    "server_is_overloaded": (
        "上游模型暂时过载",
        "稍等后手动重试；持续失败可查看 status.openai.com。",
    ),
}
TYPE_HINTS = {
    "image_generation_user_error": (
        "上游无法按当前输入生成图片",
        "根据上游说明调整描述或参考图，再手动重新发起。",
    ),
    "invalid_request_error": (
        "请求参数或输入无效",
        "根据上游说明检查参数、描述和参考图，再手动操作。",
    ),
    "authentication_error": (
        "上游身份认证失败",
        "检查对应服务凭据；Codex 登录请在 DSH 中刷新。",
    ),
    "permission_error": (
        "当前账号没有访问权限",
        "检查账号、模型权限和所在地区；处理后再手动操作。",
    ),
    "insufficient_quota": QUOTA_HINT,
    "rate_limit_error": ("请求频率受到限制", "减少并发或等待限流恢复，再手动操作。"),
    "server_error": (
        "上游服务内部错误",
        "稍等后手动重试；持续失败可查看 status.openai.com。",
    ),
    "service_unavailable_error": ("上游服务暂时不可用", "等待服务恢复后再手动操作。"),
}
HTTP_HINTS = {
    400: TYPE_HINTS["invalid_request_error"],
    401: TYPE_HINTS["authentication_error"],
    403: TYPE_HINTS["permission_error"],
    404: ("上游资源或模型不存在", "检查所用模型及接口；不会自动切换模型。"),
    408: ("上游请求超时", "先查看任务记录，确认结果后再手动处理。"),
    409: ("上游资源状态冲突", "等待冲突解除并检查任务记录，再手动操作。"),
    413: ("请求内容过大", "减少参考图数量或图片大小，再手动重新发起。"),
    415: ("上游不支持当前文件格式", "检查参考图格式，再手动重新发起。"),
    422: ("上游无法处理当前输入", "根据上游说明检查参数和参考图，再手动重新发起。"),
    429: (
        "上游请求频率或额度受到限制",
        "根据上游错误码检查限流、余额或额度，再手动操作。",
    ),
    500: TYPE_HINTS["server_error"],
    502: ("上游网关错误", "等待上游恢复后再手动操作。"),
    503: TYPE_HINTS["service_unavailable_error"],
    504: ("上游网关超时", "先查看任务记录，确认结果后再手动处理。"),
}
LOCAL_HINTS = {
    "CONTENT_REJECTED": CODE_HINTS["moderation_blocked"],
    "AUTH_EXPIRED": TYPE_HINTS["authentication_error"],
    "ACCESS_REFUSED": TYPE_HINTS["permission_error"],
    "RATE_LIMITED": HTTP_HINTS[429],
    "TIMEOUT_UNCERTAIN": (
        "请求超时，生成结果不确定",
        "已停止，不重新发送；请查看任务记录。",
    ),
    "NETWORK_UNCERTAIN": (
        "连接中断，生成结果不确定",
        "已停止，不重新发送；请查看任务记录。",
    ),
    "CANCELLED_BEFORE_DISPATCH": ("请求在发送前已停止", "保持停止，不会自动恢复。"),
    "INVALID_JSON": (
        "上游返回内容无法解析",
        "已停止；查看任务记录，避免结果不确定时重复生成。",
    ),
    "INVALID_REWRITE": (
        "润色结果不完整或格式异常",
        "已停止；查看任务记录后手动重新发起。",
    ),
    "INVALID_IMAGE_DATA": (
        "上游图片数据无法解析",
        "已停止；请检查已归档响应，不重新生成。",
    ),
    "INVALID_PNG": ("上游返回的图片格式无效", "已停止；请检查已归档响应，不重新生成。"),
    "EXPECTED_ONE_IMAGE": (
        "上游返回的图片数量异常",
        "已停止；请检查已归档响应，不重新生成。",
    ),
    "INVALID_REFERENCE": (
        "参考图无法解码或格式不支持",
        "更换有效的 PNG/JPEG/WebP 图片后手动重新发起。",
    ),
    "REFERENCE_TOO_LARGE": ("参考图超过大小限制", "压缩或减少参考图后手动重新发起。"),
    "INVALID_DEEPSEEK_CONFIGURATION": (
        "审核或润色服务配置无效",
        "检查 DeepSeek 模型、接口和凭据配置。",
    ),
    "INVALID_CODEX_CONFIGURATION": (
        "生成请求配置无效",
        "检查当前图片模型及尺寸、质量、背景参数。",
    ),
    "INVALID_CREDENTIAL": TYPE_HINTS["authentication_error"],
    "INVALID_API_URL": ("模型接口地址不在支持范围内", "检查插件接口配置。"),
    "RESPONSE_TOO_LARGE": (
        "上游响应超出大小限制",
        "已停止；结果可能不确定，不重新发送。",
    ),
}
# Responses 官方错误枚举中的图片输入类，以及常见认证/资源码；仅用于说明返回值。
CODE_HINTS.update(
    {
        "invalid_api_key": TYPE_HINTS["authentication_error"],
        "model_not_found": HTTP_HINTS[404],
        "unsupported_country_region_territory": (
            "当前所在地区不受上游支持",
            "检查上游支持地区和账号访问条件。",
        ),
        "server_error": TYPE_HINTS["server_error"],
        "rate_limit_exceeded": TYPE_HINTS["rate_limit_error"],
        "invalid_prompt": (
            "上游无法接受当前描述",
            "根据上游原文调整描述后手动重新发起。",
        ),
        "invalid_image": LOCAL_HINTS["INVALID_REFERENCE"],
        "invalid_image_format": (
            "参考图格式无效",
            "将参考图转换为受支持的格式后手动重新发起。",
        ),
        "invalid_base64_image": (
            "参考图编码无效",
            "重新发送有效图片；持续失败请检查插件编码。",
        ),
        "invalid_image_url": (
            "参考图地址无效",
            "重新发送可访问的图片，再手动重新发起。",
        ),
        "image_too_large": ("参考图尺寸过大", "缩小图片尺寸后手动重新发起。"),
        "image_too_small": ("参考图尺寸过小", "提供更清晰的图片后手动重新发起。"),
        "image_parse_error": LOCAL_HINTS["INVALID_REFERENCE"],
        "image_content_policy_violation": (
            "上游参考图内容审核未通过",
            "调整参考图内容后手动重新发起。",
        ),
        "invalid_image_mode": (
            "参考图色彩模式不受支持",
            "转换图片色彩模式后手动重新发起。",
        ),
        "image_file_too_large": LOCAL_HINTS["REFERENCE_TOO_LARGE"],
        "unsupported_image_media_type": (
            "参考图媒体类型不受支持",
            "检查图片实际格式与 MIME 类型。",
        ),
        "empty_image_file": ("参考图文件为空", "重新发送有效图片后手动重新发起。"),
        "failed_to_download_image": (
            "上游无法下载参考图",
            "检查图片是否可访问，或重新发送图片。",
        ),
        "image_file_not_found": (
            "上游未找到参考图文件",
            "重新发送有效图片后手动重新发起。",
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
        or ("请求未能完成", "查看上游说明和任务记录后手动处理。")
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
            lines.append("上游标记：" + "、".join(names[:8]) + "（仅为上游判定）")
        stage = moderation.get("moderation_stage")
        if isinstance(stage, str):
            lines.append("审核阶段：" + STAGE_NAMES.get(stage, _text(stage)))
    if status is not None:
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
