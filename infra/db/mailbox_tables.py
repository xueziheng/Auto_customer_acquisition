"""本人邮箱镜像 ORM；schema 只由 0067 迁移管理。"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from infra.db.tables import Base
from shared.schemas.mailbox import MailboxPhase


class MailboxAccountRow(Base):
    __tablename__ = "mailbox_accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_mailbox_account_email"),
        Index("ix_mailbox_owner", "tenant_id", "employee_id"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(40))
    user_id: Mapped[str] = mapped_column(String(40))
    email: Mapped[str] = mapped_column(String(320))
    phase: Mapped[MailboxPhase] = mapped_column(String(20), default="backfill")
    cursor: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(40))
    sync_requested: Mapped[bool] = mapped_column(Boolean, default=True)


class MailboxMessageRow(Base):
    __tablename__ = "mailbox_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "mailbox_id"],
            ["mailbox_accounts.tenant_id", "mailbox_accounts.mailbox_id"],
            ondelete="RESTRICT",
        ),
        Index(
            "ix_mailbox_thread", "tenant_id", "mailbox_id", "thread_id", "occurred_at"
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    subject: Mapped[str] = mapped_column(Text)
    sender: Mapped[str] = mapped_column(Text)
    snippet: Mapped[str] = mapped_column(Text)
    labels: Mapped[list] = mapped_column(JSONB)
    content: Mapped[dict] = mapped_column(JSONB)
    raw: Mapped[dict] = mapped_column(JSONB)
    generation: Mapped[int] = mapped_column(Integer)
