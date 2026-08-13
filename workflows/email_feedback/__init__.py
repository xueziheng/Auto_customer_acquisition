"""邮件投递反馈 durable workflow。"""

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

__all__ = [
    "FeedbackCursor",
    "FeedbackCursorRepository",
    "FeedbackPageUnitOfWork",
    "FeedbackQuarantine",
    "FeedbackQuarantineRepository",
    "FeedbackReceipt",
    "FeedbackReceiptAppendResult",
    "FeedbackReceiptAppendStatus",
    "FeedbackReceiptRepository",
    "UnsubscribeTokenRecord",
    "UnsubscribeTokenRepository",
]
