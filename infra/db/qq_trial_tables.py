"""QQ 验收技术账本；原消息、分类、任务和通知表不变。"""

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


class QQSentAliasRow(Base):
    __tablename__ = "qq_sent_aliases"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "identity_id", "alias_digest", name="uq_qq_sent_alias"
        ),
        UniqueConstraint("tenant_id", "attempt_id", name="uq_qq_alias_attempt"),
        ForeignKeyConstraint(
            ["tenant_id", "attempt_id"],
            [
                "outreach_message_attempts.tenant_id",
                "outreach_message_attempts.attempt_id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "tool_call_id"],
            ["tool_calls.tenant_id", "tool_calls.tool_call_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"],
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    identity_id: Mapped[str] = mapped_column(String(40))
    attempt_id: Mapped[str] = mapped_column(String(40))
    alias_digest: Mapped[str] = mapped_column(String(64))
    provider_header: Mapped[str] = mapped_column(String(998))
    canonical_header: Mapped[str] = mapped_column(String(998))
    artifact_id: Mapped[str] = mapped_column(String(40))
    artifact_hash: Mapped[str] = mapped_column(String(64))
    tool_call_id: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QQTrialRow(Base):
    __tablename__ = "qq_reply_trials"
    __table_args__ = (
        UniqueConstraint("tenant_id", "mailbox_alias", name="uq_qq_trial_mailbox"),
        UniqueConstraint("tenant_id", "attempt_id", name="uq_qq_trial_attempt"),
        ForeignKeyConstraint(
            ["tenant_id", "alias_evidence_id"],
            ["qq_sent_aliases.tenant_id", "qq_sent_aliases.evidence_id"],
        ),
        CheckConstraint(
            "state IN ('active','received','stopped','complete')",
            name="ck_qq_trial_state",
        ),
        CheckConstraint(
            "sessions BETWEEN 0 AND 20 AND searches BETWEEN 0 AND 20 AND headers BETWEEN 0 AND 20 AND bodies BETWEEN 0 AND 1 AND classifications BETWEEN 0 AND 1 AND provider_requests BETWEEN 0 AND 4 AND input_bytes BETWEEN 0 AND 32768 AND output_tokens BETWEEN 0 AND 512",
            name="ck_qq_trial_budget",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    trial_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    mailbox_alias: Mapped[str] = mapped_column(String(32))
    attempt_id: Mapped[str] = mapped_column(String(40))
    alias_evidence_id: Mapped[str] = mapped_column(String(40))
    scope: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(20))
    scan_token: Mapped[str | None] = mapped_column(String(40))
    next_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watermark: Mapped[dict | None] = mapped_column(JSONB)
    message_id: Mapped[str | None] = mapped_column(String(40))
    model_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(40))
    sessions: Mapped[int] = mapped_column(Integer, default=0)
    searches: Mapped[int] = mapped_column(Integer, default=0)
    headers: Mapped[int] = mapped_column(Integer, default=0)
    bodies: Mapped[int] = mapped_column(Integer, default=0)
    classifications: Mapped[int] = mapped_column(Integer, default=0)
    provider_requests: Mapped[int] = mapped_column(Integer, default=0)
    input_bytes: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    provider_pending: Mapped[bool] = mapped_column(Boolean, default=False)


class QQTrialEventRow(Base):
    __tablename__ = "qq_reply_trial_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "trial_id"],
            ["qq_reply_trials.tenant_id", "qq_reply_trials.trial_id"],
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    trial_id: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    input_bytes: Mapped[int] = mapped_column(Integer)
