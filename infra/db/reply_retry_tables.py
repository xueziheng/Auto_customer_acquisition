"""恢复授权身份不可变；消耗计数单调，保留原模型与失败步骤。"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from infra.db.tables import Base


class ReplyRetryRow(Base):
    __tablename__ = "reply_classification_retries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_reply_retry_key"),
        UniqueConstraint("tenant_id", "run_id", "sequence", name="uq_reply_retry_run"),
        CheckConstraint("sequence >= 1", name="ck_reply_retry_sequence"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"], ["workflow_runs.tenant_id", "workflow_runs.run_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "original_invocation_id"],
            ["model_invocations.tenant_id", "model_invocations.invocation_id"],
        ),
        CheckConstraint(
            "state IN ('queued','running','classified','failed')",
            name="ck_reply_retry_state",
        ),
        CheckConstraint(
            "classifications BETWEEN 0 AND 1 AND provider_requests BETWEEN 0 AND 4 AND input_bytes BETWEEN 0 AND 32768 AND output_tokens BETWEEN 0 AND 512",
            name="ck_reply_retry_budget",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    sequence: Mapped[int] = mapped_column(
        Integer, primary_key=True, default=1, server_default="1"
    )
    run_id: Mapped[str] = mapped_column(String(32))
    original_invocation_id: Mapped[str] = mapped_column(String(40))
    original_step_id: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    binding: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), default="queued")
    model_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    classifications: Mapped[int] = mapped_column(Integer, default=0)
    provider_requests: Mapped[int] = mapped_column(Integer, default=0)
    input_bytes: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    provider_pending: Mapped[bool] = mapped_column(Boolean, default=False)
