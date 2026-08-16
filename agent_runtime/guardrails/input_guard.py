"""输入护栏：模型永不接触凭证（硬边界 1）——``InputContentGuard`` 的默认实现。

客户回复正文可能夹杂凭证/API key/token-like 文本（转发邮件、误贴密钥等）。
本实现按 subject/body 原始串做确定性标记检测：命中即抛 ``ValidationError``
（固定安全摘要，不回显内容），调用方（workflow 步骤）把 run 转 FAILED 可观测。

- 词表与既有跨层共享词表一致（shared/schemas/email_feedback.py 的
  ``_SECRET_MARKERS``），另加常见 API key 形状；保守偏误拦（漏放一条凭证
  进模型的代价远大于误拦一封正文）。
- 只做文本标记检测：不解析系统 secret resolver、不接触真实凭证。
- 本模块只依赖 ``shared.*``，不导入 workflows/domains（依赖方向硬边界 9）。

guardrails/ 是 agent_runtime 的深模块（见 agent_runtime/AGENTS.md）：
模型输出落库前过护栏；本文件是**输入侧**护栏，与 rails.py 的输出侧
ChangeSet 护栏互补。
"""

from __future__ import annotations

import re

from shared.errors import ValidationError

#: 与 shared/schemas/email_feedback.py 共享词表一致的凭证标记（大小写不敏感）。
_SECRET_MARKERS = (
    "authorization",
    "bearer",
    "cookie",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "client_secret",
    "oauth",
)

#: 常见 API key 形状（确定性检测，防 "sk-..." 之类无标记文本漏网）。
_API_KEY_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{8,}"),
)

#: 固定安全摘要：不回显命中内容或命中位置。
_REJECT_MESSAGE = "回复消息疑似含凭证内容：已拒绝送入模型"


class CredentialMarkerGuard:
    """确定性输入护栏：subject/body 命中凭证标记或 API key 形状即拒绝。"""

    def check(self, *, subject: str | None, body: str) -> None:
        if not isinstance(subject, str) and subject is not None:
            raise ValidationError("回复消息主题视图无效")
        if not isinstance(body, str):
            raise ValidationError("回复消息正文视图无效")
        haystack = " ".join(
            part for part in (subject, body) if part is not None
        ).casefold()
        if any(marker in haystack for marker in _SECRET_MARKERS):
            raise ValidationError(_REJECT_MESSAGE)
        if any(pattern.search(body) for pattern in _API_KEY_PATTERNS):
            raise ValidationError(_REJECT_MESSAGE)
