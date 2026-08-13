"""邮件投递反馈 durable workflow。"""

from workflows.email_feedback.flow import FeedbackPageProcessor, FeedbackPageResult
from workflows.email_feedback.repository import (
    FeedbackCursor,
    FeedbackCursorRepository,
    FeedbackPageUnitOfWork,
    FeedbackQuarantine,
    FeedbackQuarantineRepository,
    FeedbackReceipt,
    FeedbackReceiptAppendResult,
    FeedbackReceiptAppendStatus,
    FeedbackReceiptRepository,
    UnsubscribeTokenRecord,
    UnsubscribeTokenRepository,
)
from workflows.email_feedback.unsubscribe import (
    UnsubscribeKeyReference,
    UnsubscribeKeyRing,
    UnsubscribeLink,
    UnsubscribeMetrics,
    UnsubscribeService,
    UnsubscribeServiceImpl,
)

__all__ = [
    "FeedbackCursor",
    "FeedbackCursorRepository",
    "FeedbackPageProcessor",
    "FeedbackPageResult",
    "FeedbackPageUnitOfWork",
    "FeedbackQuarantine",
    "FeedbackQuarantineRepository",
    "FeedbackReceipt",
    "FeedbackReceiptAppendResult",
    "FeedbackReceiptAppendStatus",
    "FeedbackReceiptRepository",
    "UnsubscribeKeyReference",
    "UnsubscribeKeyRing",
    "UnsubscribeLink",
    "UnsubscribeMetrics",
    "UnsubscribeService",
    "UnsubscribeServiceImpl",
    "UnsubscribeTokenRecord",
    "UnsubscribeTokenRepository",
]
