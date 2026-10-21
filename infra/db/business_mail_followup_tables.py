"""普通来信处理投影与不可变事件、草稿。"""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from infra.db.tables import Base


class BusinessMailFollowupRow(Base):
    __tablename__ = "business_mail_followups"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "message_id"],
            ["business_mail_messages.tenant_id", "business_mail_messages.message_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "revision >= 0 AND status IN ('pending','in_progress','waiting_customer','completed')",
            name="ck_business_followup",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))
    draft_id: Mapped[str | None] = mapped_column(String(40))


class BusinessMailReplyDraftRow(Base):
    __tablename__ = "business_mail_reply_drafts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "message_id"],
            ["business_mail_messages.tenant_id", "business_mail_messages.message_id"],
            ondelete="RESTRICT",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    draft_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(40))
    subject: Mapped[str] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BusinessMailFollowupEventRow(Base):
    __tablename__ = "business_mail_followup_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "message_id"],
            ["business_mail_messages.tenant_id", "business_mail_messages.message_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "message_id", "revision", name="uq_business_followup_revision"
        ),
        UniqueConstraint(
            "tenant_id", "request_key", name="uq_business_followup_request"
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(30))
    revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))
    note: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    draft_id: Mapped[str | None] = mapped_column(String(40))
    request_key: Mapped[str] = mapped_column(String(200))
    payload_sha256: Mapped[str] = mapped_column(String(64))
