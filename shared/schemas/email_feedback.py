"""邮件投递反馈跨层共享词表。"""

from enum import Enum


class EmailFeedbackKind(str, Enum):
    """Provider-neutral 投递反馈类别。"""

    HARD_BOUNCE = "hard_bounce"
    SOFT_BOUNCE = "soft_bounce"
    UNPARSEABLE = "unparseable"


class EmailFeedbackResult(str, Enum):
    """单条反馈的持久化处理结果。"""

    APPLIED = "applied"
    RECORDED = "recorded"
    QUARANTINED = "quarantined"


class EmailFeedbackQuarantineReason(str, Enum):
    """确定性隔离原因；不得包含 provider 原文。"""

    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"
    MISSING_CORRELATION = "missing-correlation"
    AMBIGUOUS_CORRELATION = "ambiguous-correlation"
    CROSS_TENANT_CORRELATION = "cross-tenant-correlation"
