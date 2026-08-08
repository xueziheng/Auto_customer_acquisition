"""会话域特有错误。"""

from __future__ import annotations

from shared.errors import ValidationError


class TooManyQuestionsError(ValidationError):
    """下一问建议超过两个主题。

    审讯式追问会直接终结对话。要问的东西多，说明应该转人工，
    不是把问题全抛给客户。
    """


class MissingRawArtifactError(ValidationError):
    """消息缺原文引用。

    模型摘要不能替代原文——所有需求字段的 Provenance 最终指向
    原始消息，没有原文就没有证据链。
    """
