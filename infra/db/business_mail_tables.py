"""普通来信持久事实、UID收据与追加分类历史；迁移0085管理。"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from infra.db.tables import Base


class BusinessMailboxRow(Base):
    __tablename__ = "business_mailboxes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "address", name="uq_business_mailbox_address"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_id"],
            ["employees.tenant_id", "employees.employee_id"],
            ondelete="RESTRICT",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(40))
    user_id: Mapped[str] = mapped_column(String(128))
    address: Mapped[str] = mapped_column(String(320))
    binding_operation_id: Mapped[str] = mapped_column(String(40))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger)
    after_uid: Mapped[int] = mapped_column(BigInteger, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    model_blocked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(40))


class BusinessMailRow(Base):
    __tablename__ = "business_mail_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "mailbox_id"],
            ["business_mailboxes.tenant_id", "business_mailboxes.mailbox_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "mailbox_id", "content_sha256", name="uq_business_mail_content"
        ),
        UniqueConstraint(
            "tenant_id",
            "message_id",
            "mailbox_id",
            name="uq_business_mail_message_mailbox",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(source_kind='full_mime' AND mime_sha256=source_sha256) OR (source_kind='headers_only' AND mime_sha256 IS NULL)",
            name="ck_business_mail_source",
        ),
        CheckConstraint(
            "uidvalidity BETWEEN 1 AND 4294967295 AND uid BETWEEN 1 AND 4294967295 AND revision >= 0",
            name="ck_business_mail_bounds",
        ),
        UniqueConstraint("tenant_id", "run_id", name="uq_business_mail_run"),
        CheckConstraint(
            "category IN ('pending','business','other','review')",
            name="ck_business_mail_category",
        ),
        CheckConstraint(
            "classification_status IN ('pending','classifying','classified','failed')",
            name="ck_business_mail_status",
        ),
        Index("ix_business_mail_inbox", "tenant_id", "mailbox_id", "received_at"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(String(40))
    sender: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text)
    message_header: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_sha256: Mapped[str] = mapped_column(String(64))
    mime_sha256: Mapped[str | None] = mapped_column(String(64))
    source_sha256: Mapped[str] = mapped_column(String(64))
    source_kind: Mapped[str] = mapped_column(String(20))
    artifact_id: Mapped[str] = mapped_column(String(40))
    uidvalidity: Mapped[int] = mapped_column(BigInteger)
    uid: Mapped[int] = mapped_column(BigInteger)
    category: Mapped[str] = mapped_column(String(20), default="pending")
    classification_status: Mapped[str] = mapped_column(String(20), default="pending")
    revision: Mapped[int] = mapped_column(Integer, default=0)
    failure_code: Mapped[str | None] = mapped_column(String(40))
    run_id: Mapped[str] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(128))
    configuration_version: Mapped[str | None] = mapped_column(String(128))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BusinessMailReceiptRow(Base):
    __tablename__ = "business_mail_receipts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "message_id", "mailbox_id"],
            [
                "business_mail_messages.tenant_id",
                "business_mail_messages.message_id",
                "business_mail_messages.mailbox_id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(source_kind='full_mime' AND mime_sha256=source_sha256) OR (source_kind='headers_only' AND mime_sha256 IS NULL)",
            name="ck_business_receipt_source",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    mailbox_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    uidvalidity: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    uid: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(40))
    mime_sha256: Mapped[str | None] = mapped_column(String(64))
    source_sha256: Mapped[str] = mapped_column(String(64))
    source_kind: Mapped[str] = mapped_column(String(20))
    artifact_id: Mapped[str] = mapped_column(String(40))


class BusinessMailClassificationRow(Base):
    __tablename__ = "business_mail_classifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "message_id"],
            ["business_mail_messages.tenant_id", "business_mail_messages.message_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "message_id",
            "revision",
            name="uq_business_mail_classification_revision",
        ),
        CheckConstraint(
            "category IN ('business','other','review') AND origin IN ('model','human','system')",
            name="ck_business_mail_classification",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    classification_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(40))
    revision: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(20))
    origin: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    evidence_quote: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(128))
    configuration_version: Mapped[str | None] = mapped_column(String(128))
    actor_id: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
