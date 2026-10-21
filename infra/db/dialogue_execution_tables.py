"""普通对话耐久执行元数据；正文只在受控不可变Artifact。"""
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from infra.db.tables import Base


class DialogueExecutionRow(Base):
    __tablename__ = "dialogue_executions"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "source_message_id", name="pk_dialogue_executions"),
        ForeignKeyConstraint(["tenant_id", "source_message_id"], ["messages.tenant_id", "messages.message_id"], name="fk_dialogue_execution_message"),
        ForeignKeyConstraint(["tenant_id", "binding_id"], ["dialogue_bindings.tenant_id", "dialogue_bindings.binding_id"], name="fk_dialogue_execution_binding"),
        UniqueConstraint("tenant_id", "run_id", name="uq_dialogue_execution_run"),
        UniqueConstraint("tenant_id", "attempt_id", name="uq_dialogue_execution_attempt"),
        UniqueConstraint("tenant_id", "deterministic_message_id", name="uq_dialogue_execution_header"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_dialogue_execution_key"),
        CheckConstraint("state IN ('queued','claimed','model_started','planned','sending','sent','review','cancelled','unknown')", name="ck_dialogue_execution_state"),
        CheckConstraint("version >= 0 AND model_sequence >= 0", name="ck_dialogue_execution_version"),
        CheckConstraint("(plan_artifact_id IS NULL) = (plan_sha256 IS NULL)", name="ck_dialogue_execution_plan"),
        CheckConstraint("state NOT IN ('planned','sending','sent') OR plan_artifact_id IS NOT NULL", name="ck_dialogue_execution_planned"),
        CheckConstraint("state NOT IN ('sending','sent') OR (attempt_id IS NOT NULL AND deterministic_message_id IS NOT NULL AND idempotency_key IS NOT NULL)", name="ck_dialogue_execution_send"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    source_message_id: Mapped[str] = mapped_column(String(40))
    binding_id: Mapped[str] = mapped_column(String(40))
    run_id: Mapped[str] = mapped_column(String(40))
    model_sequence: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20))
    version: Mapped[int] = mapped_column(Integer)
    lease_token: Mapped[str | None] = mapped_column(String(40))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    plan_artifact_id: Mapped[str | None] = mapped_column(String(40))
    plan_sha256: Mapped[str | None] = mapped_column(String(64))
    need_progress_sha256: Mapped[str | None] = mapped_column(String(64))
    attempt_id: Mapped[str | None] = mapped_column(String(40))
    deterministic_message_id: Mapped[str | None] = mapped_column(String(256))
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    provider_ref: Mapped[str | None] = mapped_column(String(256))
    campaign_quota_reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DialogueBindingRow(Base):
    """授权来源与客户证据祖先；状态停止不删除历史关联。"""

    __tablename__ = "dialogue_bindings"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "binding_id", name="pk_dialogue_bindings"),
        UniqueConstraint("tenant_id", "admitted_message_id", name="uq_dialogue_binding_admission"),
        UniqueConstraint("tenant_id", "business_message_id", name="uq_dialogue_binding_business"),
        ForeignKeyConstraint(["tenant_id", "approval_id"], ["approval_packages.tenant_id", "approval_packages.approval_id"], name="fk_dialogue_binding_approval"),
        ForeignKeyConstraint(["tenant_id", "business_message_id"], ["business_mail_messages.tenant_id", "business_mail_messages.message_id"], name="fk_dialogue_binding_business"),
        ForeignKeyConstraint(["tenant_id", "account_id"], ["prospect_accounts.tenant_id", "prospect_accounts.account_id"], name="fk_dialogue_binding_account"),
        ForeignKeyConstraint(["tenant_id", "contact_point_id"], ["contact_points.tenant_id", "contact_points.contact_point_id"], name="fk_dialogue_binding_contact"),
        ForeignKeyConstraint(["tenant_id", "hypothesis_id"], ["need_hypotheses.tenant_id", "need_hypotheses.hypothesis_id"], name="fk_dialogue_binding_hypothesis"),
        ForeignKeyConstraint(["tenant_id", "sending_identity_id"], ["sending_identities.tenant_id", "sending_identities.identity_id"], name="fk_dialogue_binding_sender"),
        ForeignKeyConstraint(["tenant_id", "campaign_id", "campaign_version"], ["outreach_campaign_versions.tenant_id", "outreach_campaign_versions.campaign_id", "outreach_campaign_versions.version"], name="fk_dialogue_binding_campaign"),
        ForeignKeyConstraint(["tenant_id", "enrollment_id"], ["outreach_enrollments.tenant_id", "outreach_enrollments.enrollment_id"], name="fk_dialogue_binding_enrollment"),
        ForeignKeyConstraint(["tenant_id", "admitted_message_id"], ["messages.tenant_id", "messages.message_id"], name="fk_dialogue_binding_message"),
        ForeignKeyConstraint(["tenant_id", "owner_employee_id"], ["employees.tenant_id", "employees.employee_id"], name="fk_dialogue_binding_owner"),
        CheckConstraint("(source_kind = 'outreach' AND enrollment_id IS NOT NULL AND business_message_id IS NULL) OR (source_kind = 'inquiry' AND enrollment_id IS NULL AND business_message_id IS NOT NULL)", name="ck_dialogue_binding_source"),
        CheckConstraint("state IN ('active','review','stopped')", name="ck_dialogue_binding_state"),
        CheckConstraint("rounds_sent >= 0 AND model_calls >= 0 AND no_progress_rounds >= 0", name="ck_dialogue_binding_counters"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    binding_id: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[str] = mapped_column(String(40))
    contact_point_id: Mapped[str] = mapped_column(String(40))
    hypothesis_id: Mapped[str] = mapped_column(String(40))
    sending_identity_id: Mapped[str] = mapped_column(String(40))
    campaign_id: Mapped[str] = mapped_column(String(40))
    campaign_version: Mapped[int] = mapped_column(Integer)
    approval_id: Mapped[str] = mapped_column(String(40))
    owner_employee_id: Mapped[str] = mapped_column(String(40))
    ownership_locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    mailbox_operation_id: Mapped[str] = mapped_column(String(40))
    source_kind: Mapped[str] = mapped_column(String(20))
    enrollment_id: Mapped[str | None] = mapped_column(String(40))
    business_message_id: Mapped[str | None] = mapped_column(String(40))
    admitted_message_id: Mapped[str] = mapped_column(String(40))
    admitted_source_sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20))
    rounds_sent: Mapped[int] = mapped_column(Integer)
    model_calls: Mapped[int] = mapped_column(Integer)
    no_progress_rounds: Mapped[int] = mapped_column(Integer)
    last_need_progress_sha256: Mapped[str | None] = mapped_column(String(64))
    latest_message_id: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DialogueMessageRow(Base):
    """canonical Message到来源的唯一映射，事件及证据读取共用。"""

    __tablename__ = "dialogue_messages"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "message_id", name="pk_dialogue_messages"),
        ForeignKeyConstraint(["tenant_id", "message_id"], ["messages.tenant_id", "messages.message_id"], name="fk_dialogue_message_source"),
        ForeignKeyConstraint(["tenant_id", "binding_id"], ["dialogue_bindings.tenant_id", "dialogue_bindings.binding_id"], name="fk_dialogue_message_binding"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    message_id: Mapped[str] = mapped_column(String(40))
    binding_id: Mapped[str] = mapped_column(String(40))


class DialogueModelReservationRow(Base):
    """同一对话的所有模型能力共用provider轮计数；失败/未知不回收。"""
    __tablename__ = "dialogue_model_reservations"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "run_id", "capability", "sequence", "provider_round", name="pk_dialogue_model_reservations"),
        ForeignKeyConstraint(["tenant_id", "binding_id"], ["dialogue_bindings.tenant_id", "dialogue_bindings.binding_id"], name="fk_dialogue_model_binding"),
        ForeignKeyConstraint(["tenant_id", "source_message_id"], ["messages.tenant_id", "messages.message_id"], name="fk_dialogue_model_message"),
        CheckConstraint("sequence >= 0 AND provider_round >= 0", name="ck_dialogue_model_round"),
        CheckConstraint("capability IN ('reply_qualification','ordinary_dialogue')", name="ck_dialogue_model_capability"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    binding_id: Mapped[str] = mapped_column(String(40))
    source_message_id: Mapped[str] = mapped_column(String(40))
    run_id: Mapped[str] = mapped_column(String(40))
    capability: Mapped[str] = mapped_column(String(40))
    sequence: Mapped[int] = mapped_column(Integer)
    provider_round: Mapped[int] = mapped_column(Integer)
    employee_id: Mapped[str] = mapped_column(String(40))
    user_id: Mapped[str] = mapped_column(String(128))
    configuration_version: Mapped[str] = mapped_column(String(128))
    model: Mapped[str] = mapped_column(String(128))
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DialogueBusinessReceiptRow(Base):
    """完整本人业务原件只准入一次；不复制原文、不修改冷Enrollment。"""
    __tablename__ = 'dialogue_business_receipts'
    __table_args__ = (
        ForeignKeyConstraint(['tenant_id','business_message_id'], ['business_mail_messages.tenant_id','business_mail_messages.message_id']),
        ForeignKeyConstraint(['tenant_id','binding_id'], ['dialogue_bindings.tenant_id','dialogue_bindings.binding_id']),
        ForeignKeyConstraint(['tenant_id','cold_attempt_id'], ['outreach_message_attempts.tenant_id','outreach_message_attempts.attempt_id']),
        CheckConstraint('(binding_id IS NULL) <> (cold_attempt_id IS NULL)', name='ck_dialogue_business_source'),
        ForeignKeyConstraint(['tenant_id','canonical_message_id'], ['messages.tenant_id','messages.message_id']),
        CheckConstraint("result IN ('received','hard_bounce','soft_bounce','complaint','rejected')", name='ck_dialogue_business_result'),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    business_message_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    binding_id: Mapped[str | None] = mapped_column(String(40))
    cold_attempt_id: Mapped[str | None] = mapped_column(String(40))
    canonical_message_id: Mapped[str | None] = mapped_column(String(40))
    result: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
