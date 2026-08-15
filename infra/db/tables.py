"""声明式 ORM 映射（业务表列、约束与当前 Alembic head 逐项一致）。

schema 由 Alembic 迁移管理——``Base.metadata.create_all`` 不是迁移的平行真相，
本模块只提供查询用的映射。列、约束、索引、FK 与迁移逐列一致，
金额 ``Numeric(18,2)`` + ``CHAR(3)`` 成对（硬边界 2）。触发器由数据库持有，
ORM 不表达触发器、也不绕过其只增语义。``outbox_events`` 的
触发器只由数据库迁移持有；ORM 只表达可静态核对的列、索引、外键与 CHECK。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """声明式基类（schema 归迁移管理）。"""


class ToolCallRow(Base):
    """``tool_calls`` durable invocation 与 canonical claim 行。"""

    __tablename__ = "tool_calls"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "tool_call_id", name="pk_tool_calls"),
        UniqueConstraint(
            "tenant_id", "tool_call_id", name="uq_tool_calls_tenant_call"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "duplicate_of"],
            ["tool_calls.tenant_id", "tool_calls.tool_call_id"],
            deferrable=True,
            initially="DEFERRED",
            name="fk_tool_calls_duplicate",
        ),
        CheckConstraint(
            "status IN ('received','claimed','executing','succeeded','rejected',"
            "'duplicate','failed_transient','failed_permanent')",
            name="ck_tool_calls_status",
        ),
        CheckConstraint(
            "risk_level IN ('low','medium','high')", name="ck_tool_calls_risk"
        ),
        CheckConstraint(
            "cost_class IN ('free','low','medium','high')", name="ck_tool_calls_cost"
        ),
        CheckConstraint(
            "tool_id ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$' AND "
            "tool_version ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$' AND "
            "user_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$' AND "
            "(idempotency_key IS NULL OR idempotency_key ~ "
            "'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$') AND "
            "(lease_owner IS NULL OR lease_owner ~ "
            "'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$') AND "
            "(run_id IS NULL OR run_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$') "
            "AND (campaign_id IS NULL OR campaign_id ~ "
            "'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$') AND "
            "(message_attempt_id IS NULL OR message_attempt_id ~ "
            "'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$') AND "
            "lower(tool_version) !~ "
            "'(^|[_.:-])(bearer|token|secret|password|authorization)([_.:-]|$)' "
            "AND (idempotency_key IS NULL OR lower(idempotency_key) !~ "
            "'(^|[_.:-])(bearer|token|secret|password|authorization)([_.:-]|$)') "
            "AND (lease_owner IS NULL OR lower(lease_owner) !~ "
            "'(^|[_.:-])(bearer|token|secret|password|authorization)([_.:-]|$)')",
            name="ck_tool_calls_safe_labels",
        ),
        CheckConstraint(
            "error_category IS NULL OR error_category IN "
            "('validation','permission_denied','suppressed','approval_required',"
            "'idempotency_conflict','in_progress','rate_limited',"
            "'provider_auth_required','provider_permanent','provider_transient',"
            "'reconciliation_required','unexpected')",
            name="ck_tool_calls_error_category",
        ),
        CheckConstraint(
            "(request_fingerprint IS NULL AND fingerprint_version IS NULL) OR "
            "(request_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "fingerprint_version IS NOT NULL)",
            name="ck_tool_calls_fingerprint_pair",
        ),
        CheckConstraint(
            "(status IN ('claimed','executing','succeeded','failed_transient',"
            "'failed_permanent') AND idempotency_key IS NOT NULL AND "
            "request_fingerprint IS NOT NULL AND fingerprint_version IS NOT NULL) OR "
            "(status IN ('received','rejected','duplicate') AND "
            "idempotency_key IS NULL)",
            name="ck_tool_calls_canonical_fields",
        ),
        CheckConstraint(
            "(status='duplicate' AND duplicate_of IS NOT NULL) OR "
            "(status<>'duplicate' AND duplicate_of IS NULL)",
            name="ck_tool_calls_duplicate_fields",
        ),
        CheckConstraint(
            "(status IN ('claimed','executing','failed_transient') AND "
            "lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status NOT IN ('claimed','executing','failed_transient') AND "
            "lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_tool_calls_lease_fields",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND "
            "((idempotency_key IS NOT NULL AND attempt_count >= 1) OR "
            "(idempotency_key IS NULL AND attempt_count = 0))",
            name="ck_tool_calls_attempt_count",
        ),
        CheckConstraint(
            "(status IN ('received','claimed','executing') AND provider_ref IS NULL "
            "AND error_category IS NULL AND retry_after_at IS NULL AND "
            "completed_at IS NULL) OR "
            "(status='succeeded' AND provider_ref IS NOT NULL AND "
            "error_category IS NULL AND retry_after_at IS NULL AND "
            "completed_at IS NOT NULL) OR "
            "(status='rejected' AND provider_ref IS NULL AND "
            "error_category IS NOT NULL AND retry_after_at IS NULL AND "
            "completed_at IS NOT NULL) OR "
            "(status='duplicate' AND provider_ref IS NULL AND "
            "error_category IS NULL AND retry_after_at IS NULL AND "
            "completed_at IS NOT NULL) OR "
            "(status='failed_transient' AND provider_ref IS NULL AND "
            "error_category IS NOT NULL AND completed_at IS NULL) OR "
            "(status='failed_permanent' AND provider_ref IS NULL AND "
            "error_category IS NOT NULL AND retry_after_at IS NULL AND "
            "completed_at IS NOT NULL)",
            name="ck_tool_calls_result_fields",
        ),
        CheckConstraint(
            "provider_ref IS NULL OR (char_length(provider_ref) BETWEEN 1 AND 200 "
            "AND provider_ref=btrim(provider_ref) "
            "AND provider_ref !~ '[[:space:]]' AND position('@' in provider_ref)=0 "
            "AND position('://' in provider_ref)=0 "
            "AND lower(provider_ref) !~ '(bearer|token|secret|password|authorization)')",
            name="ck_tool_calls_provider_ref",
        ),
        Index(
            "uq_tool_calls_tenant_tool_key",
            "tenant_id",
            "tool_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index(
            "ix_tool_calls_tenant_status_retry",
            "tenant_id",
            "status",
            "retry_after_at",
            "updated_at",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    tool_call_id: Mapped[str] = mapped_column(String(32))
    tool_id: Mapped[str] = mapped_column(String(100))
    tool_version: Mapped[str] = mapped_column(String(100))
    risk_level: Mapped[str] = mapped_column(String(16))
    cost_class: Mapped[str] = mapped_column(String(16))
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    fingerprint_version: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32))
    duplicate_of: Mapped[str | None] = mapped_column(String(32))
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[str | None] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(String(32))
    campaign_id: Mapped[str | None] = mapped_column(String(32))
    message_attempt_id: Mapped[str | None] = mapped_column(String(32))
    provider_ref: Mapped[str | None] = mapped_column(String(200))
    error_category: Mapped[str | None] = mapped_column(String(32))
    retry_after_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolCallEventRow(Base):
    """``tool_call_events`` 只增阶段审计行。"""

    __tablename__ = "tool_call_events"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "event_id", name="pk_tool_call_events"),
        ForeignKeyConstraint(
            ["tenant_id", "tool_call_id"],
            ["tool_calls.tenant_id", "tool_calls.tool_call_id"],
            deferrable=True,
            initially="DEFERRED",
            name="fk_tool_call_events_call",
        ),
        CheckConstraint("duration_ms >= 0", name="ck_tool_call_events_duration"),
        CheckConstraint(
            "category IS NULL OR category IN "
            "('validation','permission_denied','suppressed','approval_required',"
            "'idempotency_conflict','in_progress','rate_limited',"
            "'provider_auth_required','provider_permanent','provider_transient',"
            "'reconciliation_required','unexpected')",
            name="ck_tool_call_events_category",
        ),
        CheckConstraint(
            "stage ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$' AND "
            "outcome ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$' AND "
            "actor_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$' AND "
            "(rule IS NULL OR rule ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$') "
            "AND (run_id IS NULL OR run_id ~ "
            "'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$') AND "
            "(campaign_id IS NULL OR campaign_id ~ "
            "'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$') AND "
            "(message_attempt_id IS NULL OR message_attempt_id ~ "
            "'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$') AND "
            "(cost_note IS NULL OR cost_note ~ "
            "'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$') AND "
            "lower(stage) !~ "
            "'(^|[_.:-])(bearer|token|secret|password|authorization)([_.:-]|$)' "
            "AND lower(outcome) !~ "
            "'(^|[_.:-])(bearer|token|secret|password|authorization)([_.:-]|$)' "
            "AND (rule IS NULL OR lower(rule) !~ "
            "'(^|[_.:-])(bearer|token|secret|password|authorization)([_.:-]|$)') "
            "AND (cost_note IS NULL OR lower(cost_note) !~ "
            "'(^|[_.:-])(bearer|token|secret|password|authorization)([_.:-]|$)')",
            name="ck_tool_call_events_safe_labels",
        ),
        Index(
            "ix_tool_call_events_tenant_call_occurred",
            "tenant_id",
            "tool_call_id",
            "occurred_at",
            "event_id",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    event_id: Mapped[str] = mapped_column(String(32))
    tool_call_id: Mapped[str] = mapped_column(String(32))
    stage: Mapped[str] = mapped_column(String(100))
    outcome: Mapped[str] = mapped_column(String(100))
    rule: Mapped[str | None] = mapped_column(String(100))
    category: Mapped[str | None] = mapped_column(String(32))
    actor_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(32))
    campaign_id: Mapped[str | None] = mapped_column(String(32))
    message_attempt_id: Mapped[str | None] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(Integer)
    cost_note: Mapped[str | None] = mapped_column(String(100))


class SendingDomainRow(Base):
    """``sending_domains`` 行；租户内域角色不可变。"""

    __tablename__ = "sending_domains"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "domain", name="pk_sending_domains"),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    domain: Mapped[str] = mapped_column(String(253))
    role: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SendingIdentityRow(Base):
    """``sending_identities`` 行；比率均为确定性 ``Numeric``。"""

    __tablename__ = "sending_identities"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "identity_id", name="pk_sending_identities"),
        UniqueConstraint(
            "tenant_id", "address", name="uq_sending_identities_tenant_address"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "domain"],
            ["sending_domains.tenant_id", "sending_domains.domain"],
            ondelete="RESTRICT",
            name="fk_sending_identities_domain",
        ),
        CheckConstraint(
            "target_daily_volume IS NULL OR target_daily_volume BETWEEN 5 AND 100",
            name="ck_sending_identity_target_volume",
        ),
        CheckConstraint("version >= 0", name="ck_sending_identity_version"),
        CheckConstraint(
            "minimum_sample >= 0", name="ck_sending_identity_minimum_sample"
        ),
        CheckConstraint(
            "(warmup_started_on IS NULL) = (target_daily_volume IS NULL)",
            name="ck_sending_identity_warmup_pair",
        ),
        CheckConstraint(
            "split_part(address, '@', 2) = domain",
            name="ck_sending_identity_address_domain",
        ),
        CheckConstraint(
            "(state IN ('throttled', 'suspended') AND "
            "sendable_state_before_restriction IS NOT NULL AND "
            "sendable_state_before_restriction IN ('warming', 'active')) OR "
            "(state NOT IN ('throttled', 'suspended') AND "
            "sendable_state_before_restriction IS NULL)",
            name="ck_sending_identity_restriction_state",
        ),
        CheckConstraint(
            "(state = 'suspended' AND suspension_category IS NOT NULL AND "
            "suspension_category IN ('authentication_regression', 'hard_bounce_rate', "
            "'complaint_rate', 'spam_trap', 'blocklisted')) OR "
            "(state <> 'suspended' AND suspension_category IS NULL)",
            name="ck_sending_identity_suspension_category",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    identity_id: Mapped[str] = mapped_column(String(32))
    domain: Mapped[str] = mapped_column(String(253))
    address: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(32))
    connector_ref: Mapped[str | None] = mapped_column(String(64))
    warmup_started_on: Mapped[date | None] = mapped_column(Date)
    target_daily_volume: Mapped[int | None] = mapped_column(Integer)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sendable_state_before_restriction: Mapped[str | None] = mapped_column(String(32))
    suspension_category: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    throttle_hard_bounce_rate: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    suspend_hard_bounce_rate: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    throttle_complaint_rate: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    suspend_complaint_rate: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    suspend_on_spam_trap: Mapped[bool] = mapped_column(Boolean)
    suspend_on_blocklist: Mapped[bool] = mapped_column(Boolean)
    minimum_sample: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuthenticationCheckRow(Base):
    """``sending_auth_checks`` 只增行。"""

    __tablename__ = "sending_auth_checks"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "auth_check_id", name="pk_sending_auth_checks"
        ),
        UniqueConstraint(
            "tenant_id",
            "identity_id",
            "check_ref",
            name="uq_sending_auth_tenant_identity_ref",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "identity_id"],
            ["sending_identities.tenant_id", "sending_identities.identity_id"],
            ondelete="RESTRICT",
            name="fk_sending_auth_identity",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    auth_check_id: Mapped[str] = mapped_column(String(32))
    identity_id: Mapped[str] = mapped_column(String(32))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    spf_passed: Mapped[bool] = mapped_column(Boolean)
    dkim_passed: Mapped[bool] = mapped_column(Boolean)
    dmarc_passed: Mapped[bool] = mapped_column(Boolean)
    failures: Mapped[list[dict[str, str]]] = mapped_column(postgresql.JSONB)
    check_ref: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuthenticationCheckRequestRow(Base):
    """``sending_auth_check_requests`` 持久状态机。"""

    __tablename__ = "sending_auth_check_requests"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "request_id", name="pk_sending_auth_check_requests"
        ),
        UniqueConstraint(
            "tenant_id", "request_key", name="uq_sending_auth_request_tenant_key"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "sending_identity_id"],
            ["sending_identities.tenant_id", "sending_identities.identity_id"],
            ondelete="RESTRICT",
            name="fk_sending_auth_request_identity",
        ),
        CheckConstraint(
            "status IN ('requested','running','succeeded','failed')",
            name="ck_sending_auth_request_status",
        ),
        CheckConstraint(
            "(status IN ('requested','running') AND completed_at IS NULL) OR "
            "(status IN ('succeeded','failed') AND completed_at IS NOT NULL)",
            name="ck_sending_auth_request_completion",
        ),
        CheckConstraint(
            "request_id ~ '^acr_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND "
            "sending_identity_id ~ '^sid_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_sending_auth_request_ids",
        ),
        CheckConstraint(
            "request_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$' AND "
            "lower(request_key) !~ '(bearer|token|secret|password|authorization)'",
            name="ck_sending_auth_request_key",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    request_id: Mapped[str] = mapped_column(String(32))
    sending_identity_id: Mapped[str] = mapped_column(String(32))
    request_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReputationEventRow(Base):
    """``sending_reputation_events`` 只增行。"""

    __tablename__ = "sending_reputation_events"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "reputation_event_id", name="pk_sending_reputation_events"
        ),
        UniqueConstraint(
            "tenant_id", "dedup_key", name="uq_sending_reputation_tenant_dedup"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "identity_id"],
            ["sending_identities.tenant_id", "sending_identities.identity_id"],
            ondelete="RESTRICT",
            name="fk_sending_reputation_identity",
        ),
        Index(
            "ix_sending_reputation_tenant_identity_occurred",
            "tenant_id",
            "identity_id",
            "occurred_at",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    reputation_event_id: Mapped[str] = mapped_column(String(32))
    identity_id: Mapped[str] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dedup_key: Mapped[str] = mapped_column(String(200))
    source_ref: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SendCounterRow(Base):
    """``sending_daily_counters`` 单调计数行。"""

    __tablename__ = "sending_daily_counters"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "identity_id", "on_day", name="pk_sending_daily_counters"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "identity_id"],
            ["sending_identities.tenant_id", "sending_identities.identity_id"],
            ondelete="RESTRICT",
            name="fk_sending_counter_identity",
        ),
        CheckConstraint("sent_attempts >= 0", name="ck_sending_counter_nonnegative"),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    identity_id: Mapped[str] = mapped_column(String(32))
    on_day: Mapped[date] = mapped_column(Date)
    sent_attempts: Mapped[int] = mapped_column(Integer)


class SendReservationRow(Base):
    """``sending_send_reservations`` 不可退款的只增预留。"""

    __tablename__ = "sending_send_reservations"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "reservation_id", name="pk_sending_send_reservations"
        ),
        UniqueConstraint(
            "tenant_id",
            "identity_id",
            "reservation_key",
            name="uq_sending_reservation_tenant_identity_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "identity_id",
            "on_day",
            "sequence",
            name="uq_sending_reservation_tenant_identity_day_sequence",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "identity_id"],
            ["sending_identities.tenant_id", "sending_identities.identity_id"],
            ondelete="RESTRICT",
            name="fk_sending_reservation_identity",
        ),
        CheckConstraint("sequence >= 1", name="ck_sending_reservation_sequence"),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    reservation_id: Mapped[str] = mapped_column(String(32))
    identity_id: Mapped[str] = mapped_column(String(32))
    reservation_key: Mapped[str] = mapped_column(String(200))
    on_day: Mapped[date] = mapped_column(Date)
    sequence: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IdentityActionRow(Base):
    """``sending_identity_actions`` 只增审计行。"""

    __tablename__ = "sending_identity_actions"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "action_id", name="pk_sending_identity_actions"
        ),
        UniqueConstraint(
            "tenant_id",
            "identity_id",
            "action_key",
            name="uq_sending_action_tenant_identity_key",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "identity_id"],
            ["sending_identities.tenant_id", "sending_identities.identity_id"],
            ondelete="RESTRICT",
            name="fk_sending_action_identity",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    action_id: Mapped[str] = mapped_column(String(32))
    identity_id: Mapped[str] = mapped_column(String(32))
    action_key: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(64))
    before_state: Mapped[str | None] = mapped_column(String(32))
    after_state: Mapped[str | None] = mapped_column(String(32))
    actor_id: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(32))
    rule: Mapped[str] = mapped_column(String(128))
    note: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OutreachCampaignRow(Base):
    """``outreach_campaigns`` 当前状态与不可变版本指针。"""

    __tablename__ = "outreach_campaigns"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "campaign_id", name="pk_outreach_campaigns"),
        CheckConstraint(
            "state IN ('draft','pending_approval','active','paused','completed','cancelled')",
            name="ck_outreach_campaign_state",
        ),
        CheckConstraint("current_version >= 1", name="ck_outreach_campaign_version"),
        CheckConstraint("round_robin_cursor >= -1", name="ck_outreach_campaign_cursor"),
        CheckConstraint(
            "(approval_id IS NULL AND approved_by IS NULL AND approved_at IS NULL) OR "
            "(approval_id IS NOT NULL AND approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_outreach_campaign_approval_tuple",
        ),
        Index(
            "ix_outreach_campaigns_tenant_state_created",
            "tenant_id",
            "state",
            "created_at",
            "campaign_id",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    campaign_id: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32))
    current_version: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    round_robin_cursor: Mapped[int] = mapped_column(Integer)
    approval_id: Mapped[str | None] = mapped_column(String(32))
    approved_by: Mapped[str | None] = mapped_column(String(32))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_reason: Mapped[str | None] = mapped_column(String(200))


class OutreachCampaignVersionRow(Base):
    """``outreach_campaign_versions`` 只增版本边界。"""

    __tablename__ = "outreach_campaign_versions"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "campaign_id",
            "version",
            name="pk_outreach_campaign_versions",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["outreach_campaigns.tenant_id", "outreach_campaigns.campaign_id"],
            ondelete="RESTRICT",
            name="fk_outreach_campaign_versions_campaign",
        ),
        CheckConstraint("version >= 1", name="ck_outreach_version_number"),
        CheckConstraint(
            "daily_new_contact_limit > 0 AND daily_total_message_limit > 0 "
            "AND daily_new_contact_limit <= daily_total_message_limit",
            name="ck_outreach_version_quotas",
        ),
        CheckConstraint(
            "stop_on_reply IS TRUE", name="ck_outreach_version_stop_on_reply"
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    campaign_id: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    markets: Mapped[list[str]] = mapped_column(postgresql.JSONB)
    target_entity_types: Mapped[list[str]] = mapped_column(postgresql.JSONB)
    allowed_categories: Mapped[list[str]] = mapped_column(postgresql.JSONB)
    sender_identity_ids: Mapped[list[str]] = mapped_column(postgresql.JSONB)
    daily_new_contact_limit: Mapped[int] = mapped_column(Integer)
    daily_total_message_limit: Mapped[int] = mapped_column(Integer)
    handoff_triggers: Mapped[list[str]] = mapped_column(postgresql.JSONB)
    stop_on_reply: Mapped[bool] = mapped_column(Boolean)
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OutreachSequenceStepRow(Base):
    """``outreach_sequence_steps`` 只增规范化步骤。"""

    __tablename__ = "outreach_sequence_steps"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "campaign_id",
            "version",
            "step_number",
            name="pk_outreach_sequence_steps",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "campaign_id", "version"],
            [
                "outreach_campaign_versions.tenant_id",
                "outreach_campaign_versions.campaign_id",
                "outreach_campaign_versions.version",
            ],
            ondelete="RESTRICT",
            name="fk_outreach_steps_version",
        ),
        CheckConstraint("step_number BETWEEN 1 AND 5", name="ck_outreach_step_number"),
        CheckConstraint(
            "intent IN ('discovery','presentation','follow_up')",
            name="ck_outreach_step_intent",
        ),
        CheckConstraint("wait_days >= 0", name="ck_outreach_step_wait"),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    campaign_id: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    step_number: Mapped[int] = mapped_column(Integer)
    intent: Mapped[str] = mapped_column(String(32))
    wait_days: Mapped[int] = mapped_column(Integer)


class OutreachEnrollmentRow(Base):
    """``outreach_enrollments`` 当前序列状态。"""

    __tablename__ = "outreach_enrollments"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "enrollment_id", name="pk_outreach_enrollments"
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_outreach_enrollments_tenant_key",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "campaign_id", "campaign_version"],
            [
                "outreach_campaign_versions.tenant_id",
                "outreach_campaign_versions.campaign_id",
                "outreach_campaign_versions.version",
            ],
            ondelete="RESTRICT",
            name="fk_outreach_enrollments_version",
        ),
        CheckConstraint(
            "state IN ('enrolled','in_sequence','replied','completed',"
            "'stopped_suppressed','stopped_bounced','stopped_manual',"
            "'stopped_identity_unavailable')",
            name="ck_outreach_enrollment_state",
        ),
        CheckConstraint(
            "current_step BETWEEN 0 AND 5", name="ck_outreach_enrollment_step"
        ),
        CheckConstraint(
            "(state IN ('enrolled','in_sequence') AND stopped_at IS NULL AND "
            "stop_reason IS NULL) OR "
            "(state='replied' AND stopped_at IS NOT NULL AND stop_reason='reply') OR "
            "(state='completed' AND stopped_at IS NOT NULL AND stop_reason IS NULL) OR "
            "(state='stopped_suppressed' AND stopped_at IS NOT NULL AND "
            "stop_reason='suppression') OR "
            "(state='stopped_bounced' AND stopped_at IS NOT NULL AND "
            "stop_reason='hard_bounce') OR "
            "(state='stopped_manual' AND stopped_at IS NOT NULL AND "
            "stop_reason='manual') OR "
            "(state='stopped_identity_unavailable' AND stopped_at IS NOT NULL AND "
            "stop_reason='identity_unavailable')",
            name="ck_outreach_enrollment_stop_fields",
        ),
        Index(
            "uq_outreach_enrollments_active_account",
            "tenant_id",
            "account_id",
            unique=True,
            postgresql_where=text("state IN ('enrolled','in_sequence')"),
        ),
        Index(
            "ix_outreach_enrollments_tenant_campaign_state",
            "tenant_id",
            "campaign_id",
            "state",
            "enrolled_at",
            "enrollment_id",
        ),
        Index(
            "ix_outreach_enrollments_tenant_contact_state",
            "tenant_id",
            "contact_point_id",
            "state",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    enrollment_id: Mapped[str] = mapped_column(String(32))
    campaign_id: Mapped[str] = mapped_column(String(32))
    campaign_version: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[str] = mapped_column(String(32))
    contact_point_id: Mapped[str] = mapped_column(String(32))
    sending_identity_id: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(40))
    current_step: Mapped[int] = mapped_column(Integer)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(200))


class OutreachSuppressionRow(Base):
    """``outreach_suppressions`` append-only 全局抑制事实。"""

    __tablename__ = "outreach_suppressions"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "suppression_id", name="pk_outreach_suppressions"
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_outreach_suppressions_tenant_key",
        ),
        CheckConstraint(
            "(contact_point_id IS NOT NULL AND account_id IS NULL) OR "
            "(contact_point_id IS NULL AND account_id IS NOT NULL)",
            name="ck_outreach_suppression_exact_target",
        ),
        CheckConstraint(
            "reason IN ('unsubscribe','complaint','hard_bounce','manual_block',"
            "'competitor','existing_customer_conflict')",
            name="ck_outreach_suppression_reason",
        ),
        Index(
            "ix_outreach_suppressions_tenant_contact",
            "tenant_id",
            "contact_point_id",
            "occurred_at",
            postgresql_where=text("contact_point_id IS NOT NULL"),
        ),
        Index(
            "ix_outreach_suppressions_tenant_account",
            "tenant_id",
            "account_id",
            "occurred_at",
            postgresql_where=text("account_id IS NOT NULL"),
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    suppression_id: Mapped[str] = mapped_column(String(32))
    contact_point_id: Mapped[str | None] = mapped_column(String(32))
    account_id: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(40))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_ref: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OutreachDailyQuotaRow(Base):
    """``outreach_daily_quotas`` Campaign 单调额度计数。"""

    __tablename__ = "outreach_daily_quotas"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "campaign_id",
            "on_day",
            name="pk_outreach_daily_quotas",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["outreach_campaigns.tenant_id", "outreach_campaigns.campaign_id"],
            ondelete="RESTRICT",
            name="fk_outreach_quotas_campaign",
        ),
        CheckConstraint(
            "new_contacts_reserved >= 0 AND messages_reserved >= 0",
            name="ck_outreach_quota_nonnegative",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    campaign_id: Mapped[str] = mapped_column(String(32))
    on_day: Mapped[date] = mapped_column(Date)
    new_contacts_reserved: Mapped[int] = mapped_column(Integer)
    messages_reserved: Mapped[int] = mapped_column(Integer)


class OutreachMessageAttemptRow(Base):
    """``outreach_message_attempts`` durable 发送尝试。"""

    __tablename__ = "outreach_message_attempts"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "attempt_id", name="pk_outreach_message_attempts"
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_outreach_attempts_tenant_key",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "enrollment_id"],
            ["outreach_enrollments.tenant_id", "outreach_enrollments.enrollment_id"],
            ondelete="RESTRICT",
            name="fk_outreach_attempts_enrollment",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "campaign_id", "campaign_version"],
            [
                "outreach_campaign_versions.tenant_id",
                "outreach_campaign_versions.campaign_id",
                "outreach_campaign_versions.version",
            ],
            ondelete="RESTRICT",
            name="fk_outreach_attempts_version",
        ),
        CheckConstraint("step_number BETWEEN 1 AND 5", name="ck_outreach_attempt_step"),
        CheckConstraint(
            "(state='reserved' AND provider_ref IS NULL AND failure_category IS NULL "
            "AND send_claimed_at IS NULL) OR "
            "(state='sending' AND provider_ref IS NULL AND failure_category IS NULL "
            "AND send_claimed_at IS NOT NULL) OR "
            "(state='sent' AND provider_ref IS NOT NULL AND failure_category IS NULL) OR "
            "(state='failed_transient' AND provider_ref IS NULL AND "
            "failure_category IN ('rate_limited','provider_transient',"
            "'provider_auth_required')) OR "
            "(state='failed_permanent' AND provider_ref IS NULL AND "
            "failure_category IN ('provider_permanent','identity_unavailable'))",
            name="ck_outreach_attempt_state_fields",
        ),
        CheckConstraint(
            "(deterministic_message_id IS NULL AND idempotency_header IS NULL) OR "
            "(deterministic_message_id IS NOT NULL AND idempotency_header IS NOT NULL)",
            name="ck_outreach_attempt_correlation_pair",
        ),
        CheckConstraint(
            "deterministic_message_id IS NULL OR "
            "(deterministic_message_id ~ "
            "'^<[a-z0-9-]{1,32}\\.[0-9a-f]{64}@messages\\.tradeos\\.invalid>$' "
            "AND idempotency_header ~ '^[a-z0-9-]{1,32}\\.[0-9a-f]{64}$' "
            "AND substring(deterministic_message_id FROM "
            "'^<([a-z0-9-]{1,32}\\.[0-9a-f]{64})@messages\\.tradeos\\.invalid>$') "
            "= idempotency_header AND lower(idempotency_header) !~ "
            "'(^|[-.])(bearer|token|secret|password)([-.]|$)')",
            name="ck_outreach_attempt_correlation_grammar",
        ),
        Index(
            "ix_outreach_attempts_tenant_enrollment_created",
            "tenant_id",
            "enrollment_id",
            "created_at",
            "attempt_id",
        ),
        Index(
            "uq_outreach_attempts_tenant_message_id",
            "tenant_id",
            "deterministic_message_id",
            unique=True,
            postgresql_where=text("deterministic_message_id IS NOT NULL"),
        ),
        Index(
            "uq_outreach_attempts_tenant_idempotency_header",
            "tenant_id",
            "idempotency_header",
            unique=True,
            postgresql_where=text("idempotency_header IS NOT NULL"),
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    attempt_id: Mapped[str] = mapped_column(String(32))
    message_id: Mapped[str] = mapped_column(String(32))
    campaign_id: Mapped[str] = mapped_column(String(32))
    enrollment_id: Mapped[str] = mapped_column(String(32))
    campaign_version: Mapped[int] = mapped_column(Integer)
    step_number: Mapped[int] = mapped_column(Integer)
    sending_identity_id: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(32))
    provider_ref: Mapped[str | None] = mapped_column(String(200))
    failure_category: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    send_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deterministic_message_id: Mapped[str | None] = mapped_column(String(256))
    idempotency_header: Mapped[str | None] = mapped_column(String(128))


class OutreachActionRow(Base):
    """``outreach_actions`` append-only 业务动作审计。"""

    __tablename__ = "outreach_actions"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "action_id", name="pk_outreach_actions"),
        UniqueConstraint(
            "tenant_id", "action_key", name="uq_outreach_actions_tenant_key"
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    action_id: Mapped[str] = mapped_column(String(32))
    action_key: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EmailFeedbackCursorRow(Base):
    """邮箱反馈 cursor；只保存不透明 provider cursor，不建立值索引。"""

    __tablename__ = "email_feedback_cursors"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "mailbox_alias", name="pk_email_feedback_cursors"
        ),
        CheckConstraint(
            "tenant_id ~ '^tn_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_email_feedback_cursor_tenant",
        ),
        CheckConstraint(
            "mailbox_alias ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="ck_email_feedback_cursor_mailbox",
        ),
        CheckConstraint("version >= 0", name="ck_email_feedback_cursor_version"),
        CheckConstraint(
            "provider_cursor IS NULL OR (length(provider_cursor) BETWEEN 1 AND 32768)",
            name="ck_email_feedback_cursor_value",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    mailbox_alias: Mapped[str] = mapped_column(String(32))
    provider_cursor: Mapped[str | None] = mapped_column(String(32768))
    version: Mapped[int] = mapped_column(Integer)
    bootstrap_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailFeedbackReceiptRow(Base):
    """Provider feedback 的 append-only 低敏处理收据。"""

    __tablename__ = "email_feedback_receipts"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "mailbox_alias",
            "provider_event_id",
            name="pk_email_feedback_receipts",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "mailbox_alias"],
            [
                "email_feedback_cursors.tenant_id",
                "email_feedback_cursors.mailbox_alias",
            ],
            ondelete="RESTRICT",
            name="fk_email_feedback_receipts_cursor",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "attempt_id"],
            [
                "outreach_message_attempts.tenant_id",
                "outreach_message_attempts.attempt_id",
            ],
            ondelete="RESTRICT",
            name="fk_email_feedback_receipts_attempt",
        ),
        CheckConstraint(
            "tenant_id ~ '^tn_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_email_feedback_receipt_tenant",
        ),
        CheckConstraint(
            "mailbox_alias ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="ck_email_feedback_receipt_mailbox",
        ),
        CheckConstraint(
            "provider_event_id ~ '^[0-9a-f]{64}$'",
            name="ck_email_feedback_receipt_event",
        ),
        CheckConstraint(
            "item_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_email_feedback_receipt_fingerprint",
        ),
        CheckConstraint(
            "ordinal BETWEEN 0 AND 99", name="ck_email_feedback_receipt_ordinal"
        ),
        CheckConstraint(
            "kind IN ('hard_bounce','soft_bounce','unparseable','complaint')",
            name="ck_email_feedback_receipt_kind",
        ),
        CheckConstraint(
            "result IN ('applied','recorded','quarantined')",
            name="ck_email_feedback_receipt_result",
        ),
        CheckConstraint(
            "(result='quarantined' AND kind='unparseable' AND attempt_id IS NULL "
            "AND enrollment_id IS NULL AND account_id IS NULL "
            "AND contact_point_id IS NULL AND sending_identity_id IS NULL) OR "
            "(result IN ('applied','recorded') AND "
            "kind IN ('hard_bounce','soft_bounce','complaint') "
            "AND attempt_id IS NOT NULL AND enrollment_id IS NOT NULL "
            "AND account_id IS NOT NULL AND contact_point_id IS NOT NULL "
            "AND sending_identity_id IS NOT NULL)",
            name="ck_email_feedback_receipt_target",
        ),
        Index(
            "ix_email_feedback_receipts_tenant_mailbox_created",
            "tenant_id",
            "mailbox_alias",
            "created_at",
            "provider_event_id",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    mailbox_alias: Mapped[str] = mapped_column(String(32))
    provider_event_id: Mapped[str] = mapped_column(String(64))
    item_fingerprint: Mapped[str] = mapped_column(String(64))
    ordinal: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result: Mapped[str] = mapped_column(String(32))
    attempt_id: Mapped[str | None] = mapped_column(String(32))
    enrollment_id: Mapped[str | None] = mapped_column(String(32))
    account_id: Mapped[str | None] = mapped_column(String(32))
    contact_point_id: Mapped[str | None] = mapped_column(String(32))
    sending_identity_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EmailFeedbackQuarantineRow(Base):
    """确定性隔离分类；不保存原始 provider 内容。"""

    __tablename__ = "email_feedback_quarantines"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "mailbox_alias",
            "provider_event_id",
            name="pk_email_feedback_quarantines",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "mailbox_alias", "provider_event_id"],
            [
                "email_feedback_receipts.tenant_id",
                "email_feedback_receipts.mailbox_alias",
                "email_feedback_receipts.provider_event_id",
            ],
            ondelete="RESTRICT",
            name="fk_email_feedback_quarantine_receipt",
        ),
        CheckConstraint(
            "reason IN ('malformed','unsupported','missing-correlation',"
            "'ambiguous-correlation','cross-tenant-correlation')",
            name="ck_email_feedback_quarantine_reason",
        ),
        CheckConstraint(
            "provider_ref_digest ~ '^[0-9a-f]{64}$'",
            name="ck_email_feedback_quarantine_digest",
        ),
        Index(
            "ix_email_feedback_quarantines_tenant_created",
            "tenant_id",
            "created_at",
            "provider_event_id",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    mailbox_alias: Mapped[str] = mapped_column(String(32))
    provider_event_id: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(40))
    provider_ref_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UnsubscribeTokenRow(Base):
    """One-click unsubscribe token 的 nonce 摘要与一次性消费状态。"""

    __tablename__ = "unsubscribe_tokens"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "nonce_sha256", name="pk_unsubscribe_tokens"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "message_attempt_id"],
            [
                "outreach_message_attempts.tenant_id",
                "outreach_message_attempts.attempt_id",
            ],
            ondelete="RESTRICT",
            name="fk_unsubscribe_token_attempt",
        ),
        CheckConstraint(
            "tenant_id ~ '^tn_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_unsubscribe_token_tenant",
        ),
        CheckConstraint(
            "octet_length(nonce_sha256)=32", name="ck_unsubscribe_token_nonce"
        ),
        CheckConstraint(
            "contact_point_id ~ '^cp_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_unsubscribe_token_contact",
        ),
        CheckConstraint(
            "message_attempt_id ~ '^mat_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_unsubscribe_token_attempt",
        ),
        CheckConstraint(
            "key_id ~ '^[a-z0-9-]{1,32}$'",
            name="ck_unsubscribe_token_key",
        ),
        CheckConstraint(
            "expires_at = created_at + interval '90 days'",
            name="ck_unsubscribe_token_expiry",
        ),
        CheckConstraint(
            "consumed_at IS NULL OR (consumed_at >= created_at AND consumed_at < expires_at)",
            name="ck_unsubscribe_token_consumed",
        ),
        Index(
            "ix_unsubscribe_tokens_tenant_attempt",
            "tenant_id",
            "message_attempt_id",
            "created_at",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    nonce_sha256: Mapped[bytes] = mapped_column(LargeBinary(32))
    contact_point_id: Mapped[str] = mapped_column(String(32))
    message_attempt_id: Mapped[str] = mapped_column(String(32))
    key_id: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OpportunityRow(Base):
    """``opportunities`` 行。"""

    __tablename__ = "opportunities"
    __table_args__ = (
        UniqueConstraint("tenant_id", "need_id", name="uq_opportunities_tenant_need"),
        UniqueConstraint(
            "tenant_id", "opportunity_id", name="uq_opportunities_tenant_opp"
        ),
        CheckConstraint(
            "(target_price_amount IS NULL AND target_price_currency IS NULL) OR "
            "(target_price_amount IS NOT NULL AND target_price_currency IS NOT NULL)",
            name="ck_opportunities_target_price_pair",
        ),
        CheckConstraint(
            "(estimated_cost_amount IS NULL AND estimated_cost_currency IS NULL) OR "
            "(estimated_cost_amount IS NOT NULL AND estimated_cost_currency IS NOT NULL)",
            name="ck_opportunities_estimated_cost_pair",
        ),
        CheckConstraint(
            "(estimated_profit_amount IS NULL AND estimated_profit_currency IS NULL) OR "
            "(estimated_profit_amount IS NOT NULL AND estimated_profit_currency IS NOT NULL)",
            name="ck_opportunities_estimated_profit_pair",
        ),
        CheckConstraint(
            "state <> 'lost' OR (loss_reason IS NOT NULL AND died_at_state IS NOT NULL "
            "AND closed_by IS NOT NULL AND closed_at IS NOT NULL)",
            name="ck_opportunities_lost_closed",
        ),
        CheckConstraint(
            "state <> 'won' OR (closed_by IS NOT NULL AND closed_at IS NOT NULL)",
            name="ck_opportunities_won_closed",
        ),
        CheckConstraint(
            "closed_at IS NULL OR state IN ('won', 'lost')",
            name="ck_opportunities_closed_only_terminal",
        ),
        Index("ix_opportunities_tenant_state", "tenant_id", "state"),
        Index("ix_opportunities_tenant_owner_state", "tenant_id", "owner", "state"),
    )

    opportunity_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    account_id: Mapped[str] = mapped_column(String(32))
    account_name: Mapped[str] = mapped_column(String(200))
    country: Mapped[str] = mapped_column(String(100))
    need_id: Mapped[str] = mapped_column(String(32))
    product_category: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20), server_default=text("'qualified'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    quantity: Mapped[int | None] = mapped_column(Integer)
    spec_summary: Mapped[str | None] = mapped_column(Text)
    application: Mapped[str | None] = mapped_column(Text)
    destination: Mapped[str | None] = mapped_column(String(100))
    required_by: Mapped[date | None] = mapped_column(Date)
    target_price_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    target_price_currency: Mapped[str | None] = mapped_column(CHAR(3))
    decision_maker: Mapped[str | None] = mapped_column(Text)
    current_supply_solution: Mapped[str | None] = mapped_column(Text)
    current_supply_problem: Mapped[str | None] = mapped_column(Text)
    can_source: Mapped[bool | None] = mapped_column(Boolean)
    estimated_cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    estimated_cost_currency: Mapped[str | None] = mapped_column(CHAR(3))
    estimated_profit_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    estimated_profit_currency: Mapped[str | None] = mapped_column(CHAR(3))
    owner: Mapped[str | None] = mapped_column(String(32))
    assigned_by: Mapped[str | None] = mapped_column(String(32))
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_action: Mapped[str | None] = mapped_column(Text)
    next_action_due: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    loss_reason: Mapped[str | None] = mapped_column(String(32))
    died_at_state: Mapped[str | None] = mapped_column(String(20))
    closed_by: Mapped[str | None] = mapped_column(String(32))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScoreSnapshotRow(Base):
    """``score_snapshots`` 行（只增；opportunity_id 无 FK，失败门槛快照可悬空）。"""

    __tablename__ = "score_snapshots"
    __table_args__ = (
        CheckConstraint(
            "(estimated_value_amount IS NULL AND estimated_value_currency IS NULL) OR "
            "(estimated_value_amount IS NOT NULL AND estimated_value_currency IS NOT NULL)",
            name="ck_score_snapshots_value_pair",
        ),
        Index(
            "ix_score_snapshots_tenant_opp_scored",
            "tenant_id",
            "opportunity_id",
            "scored_at",
        ),
    )

    snapshot_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    opportunity_id: Mapped[str] = mapped_column(String(32))  # 无 FK：失败门槛快照可悬空
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scorer_version: Mapped[str] = mapped_column(String(32))
    passed_gates: Mapped[list] = mapped_column(postgresql.JSONB)
    failed_gates: Mapped[list] = mapped_column(postgresql.JSONB)
    evidence_tier: Mapped[str | None] = mapped_column(String(32))
    estimated_value_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    estimated_value_currency: Mapped[str | None] = mapped_column(CHAR(3))
    supply_available: Mapped[bool | None] = mapped_column(Boolean)
    sort_evidence_rank: Mapped[int] = mapped_column(Integer)
    sort_value_band: Mapped[int] = mapped_column(Integer)
    sort_supply_rank: Mapped[int] = mapped_column(Integer)
    gate_reasons: Mapped[dict] = mapped_column(postgresql.JSONB)
    rank_bucket: Mapped[str] = mapped_column(String(10))


class HandoffRow(Base):
    """``handoffs`` 行（复合 FK → opportunities）。"""

    __tablename__ = "handoffs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "handoff_id", name="uq_handoffs_tenant_handoff"),
        ForeignKeyConstraint(
            ["tenant_id", "opportunity_id"],
            ["opportunities.tenant_id", "opportunities.opportunity_id"],
            ondelete="RESTRICT",
            name="fk_handoffs_opportunity",
        ),
        Index(
            "ix_handoffs_tenant_state_requested", "tenant_id", "state", "requested_at"
        ),
    )

    handoff_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    opportunity_id: Mapped[str] = mapped_column(String(32))
    trigger: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(20), server_default=text("'requested'"))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    account_name: Mapped[str] = mapped_column(String(200))
    country: Mapped[str] = mapped_column(String(100))
    why_valuable: Mapped[str] = mapped_column(Text)
    customer_verbatim: Mapped[str] = mapped_column(Text)
    assigned_to: Mapped[str | None] = mapped_column(String(32))
    manager: Mapped[str | None] = mapped_column(String(32))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by: Mapped[str | None] = mapped_column(String(32))
    how_we_found_them: Mapped[str | None] = mapped_column(Text)
    validated_need_summary: Mapped[str | None] = mapped_column(Text)
    missing_information: Mapped[list | None] = mapped_column(postgresql.JSONB)
    already_sent: Mapped[list | None] = mapped_column(postgresql.JSONB)
    commitments_made: Mapped[list | None] = mapped_column(postgresql.JSONB)
    evidence_links: Mapped[list | None] = mapped_column(postgresql.JSONB)
    conversation_summary: Mapped[str | None] = mapped_column(Text)
    suggested_next_step: Mapped[str | None] = mapped_column(Text)


class HandoffEscalationRow(Base):
    """``handoff_escalations`` 行（复合 FK → handoffs；只增）。"""

    __tablename__ = "handoff_escalations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "handoff_id"],
            ["handoffs.tenant_id", "handoffs.handoff_id"],
            ondelete="RESTRICT",
            name="fk_handoff_escalations_handoff",
        ),
        UniqueConstraint(
            "tenant_id",
            "handoff_id",
            "level",
            name="uq_handoff_escalations_tenant_handoff_level",
        ),
    )

    escalation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    handoff_id: Mapped[str] = mapped_column(String(32))
    level: Mapped[int] = mapped_column(Integer)
    escalated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)


class LossRecordRow(Base):
    """``loss_records`` 行（复合 FK → opportunities；只增；人工确认必留痕）。"""

    __tablename__ = "loss_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "opportunity_id"],
            ["opportunities.tenant_id", "opportunities.opportunity_id"],
            ondelete="RESTRICT",
            name="fk_loss_records_opportunity",
        ),
        Index(
            "ix_loss_records_tenant_reason_state",
            "tenant_id",
            "loss_reason",
            "died_at_state",
        ),
    )

    loss_record_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    opportunity_id: Mapped[str] = mapped_column(String(32))
    loss_reason: Mapped[str] = mapped_column(String(32))
    died_at_state: Mapped[str] = mapped_column(String(20))
    detail: Mapped[str | None] = mapped_column(Text)
    evidence_tier: Mapped[str | None] = mapped_column(String(32))
    confirmed_by: Mapped[str] = mapped_column(String(32))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProvenanceRecordRow(Base):
    """``provenance_records`` 行（多态 entity，无 FK；只增；同字段多版本历史）。"""

    __tablename__ = "provenance_records"
    __table_args__ = (
        Index(
            "ix_provenance_lookup",
            "tenant_id",
            "entity_type",
            "entity_id",
            "field_name",
            "extracted_at",
        ),
    )

    provenance_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(32))
    field_name: Mapped[str] = mapped_column(String(64))
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(200))
    extracted_by: Mapped[str] = mapped_column(String(64))
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(String(32))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_url: Mapped[str | None] = mapped_column(String(2000))
    page_hash: Mapped[str | None] = mapped_column(String(200))


class OutboxEventRow(Base):
    """``outbox_events`` 行（非只增；仅 status/delivered_at/attempt/next_attempt_at/
    last_error 可更新，由 0005 自管 DB guard 强制；UNIQUE(tenant_id,event_id) 供
    outbox_deliveries 复合 FK 引用）。"""

    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "event_id", name="uq_outbox_events_tenant_event"),
        CheckConstraint("attempt >= 1", name="ck_outbox_attempt_min"),
        CheckConstraint(
            "status IN ('pending', 'delivered', 'dead')", name="ck_outbox_status"
        ),
        Index("ix_outbox_tenant_status", "tenant_id", "status"),
    )

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(64))
    event_payload: Mapped[dict] = mapped_column(postgresql.JSONB)
    attempt: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trace_id: Mapped[str] = mapped_column(String(32))
    run_id: Mapped[str | None] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class OutboxDeliveryRow(Base):
    """``outbox_deliveries`` 行（durable per-handler 投递状态；复合 FK →
    outbox_events，禁止跨租户引用；UNIQUE(tenant_id,event_id,handler_name)）。"""

    __tablename__ = "outbox_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "event_id",
            "handler_name",
            name="uq_outbox_deliveries_tenant_event_handler",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "event_id"],
            ["outbox_events.tenant_id", "outbox_events.event_id"],
            name="fk_outbox_deliveries_event",
        ),
        CheckConstraint(
            "status IN ('pending', 'delivered', 'dead')",
            name="ck_outbox_deliveries_status",
        ),
    )

    delivery_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    event_id: Mapped[str] = mapped_column(String(32))
    handler_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationDeliveryRow(Base):
    """``notification_deliveries`` 行（durable 通知去重状态；0006）。

    ``UNIQUE(tenant_id, dedup_key, channel_name)``：同一通知同一渠道只允许一行，
    部分渠道失败可 durable resume（仅续投失败渠道，不重复投递已成功渠道）。
    ``status``/``attempts`` 走 server_default：pending / 0。
    """

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "dedup_key",
            "channel_name",
            name="uq_notification_deliveries_tenant_dedup_channel",
        ),
    )

    delivery_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    dedup_key: Mapped[str] = mapped_column(String(200))
    channel_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    claim_token: Mapped[str | None] = mapped_column(String(32))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationJobRow(Base):
    """``notification_jobs`` durable 投影行；每一事件受唯一键保护。"""

    __tablename__ = "notification_jobs"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "notification_job_id", name="pk_notification_jobs"),
        UniqueConstraint("tenant_id", "source_event_fingerprint", "recipient_employee_id", "context_kind", name="uq_notification_jobs_source_recipient_kind"),
        CheckConstraint("status IN ('pending','processing','completed','rejected')", name="ck_notification_jobs_status"),
        CheckConstraint("priority IN ('urgent','normal','low')", name="ck_notification_jobs_priority"),
        CheckConstraint("attempt_count >= 0", name="ck_notification_jobs_attempt_count"),
        Index("ix_notification_jobs_tenant_due", "tenant_id", "status", "available_at"),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    notification_job_id: Mapped[str] = mapped_column(String(32))
    source_event_fingerprint: Mapped[str] = mapped_column(String(64))
    source_event: Mapped[str] = mapped_column(String(100))
    recipient_employee_id: Mapped[str] = mapped_column(String(32))
    priority: Mapped[str] = mapped_column(String(16))
    context_kind: Mapped[str] = mapped_column(String(64))
    primary_id: Mapped[str] = mapped_column(String(100))
    secondary_id: Mapped[str | None] = mapped_column(String(100))
    reason_code: Mapped[str | None] = mapped_column(String(100))
    level: Mapped[int | None] = mapped_column(Integer)
    dedup_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_token: Mapped[str | None] = mapped_column(String(32))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InAppNotificationRow(Base):
    """``in_app_notifications`` 不可变内容行；read_at 是唯一可变字段。"""

    __tablename__ = "in_app_notifications"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "notification_id", name="pk_in_app_notifications"),
        UniqueConstraint("tenant_id", "source_job_id", name="uq_in_app_notifications_source_job"),
        ForeignKeyConstraint(["tenant_id", "source_job_id"], ["notification_jobs.tenant_id", "notification_jobs.notification_job_id"], ondelete="RESTRICT", name="fk_in_app_notifications_job"),
        CheckConstraint("priority IN ('urgent','normal','low')", name="ck_in_app_notifications_priority"),
        Index("ix_in_app_notifications_recipient_created", "tenant_id", "recipient_employee_id", "created_at", "notification_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    notification_id: Mapped[str] = mapped_column(String(32))
    recipient_employee_id: Mapped[str] = mapped_column(String(32))
    priority: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    context_kind: Mapped[str] = mapped_column(String(64))
    primary_id: Mapped[str] = mapped_column(String(100))
    secondary_id: Mapped[str | None] = mapped_column(String(100))
    reason_code: Mapped[str | None] = mapped_column(String(100))
    level: Mapped[int | None] = mapped_column(Integer)
    relative_link: Mapped[str | None] = mapped_column(String(500))
    source_job_id: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmployeeRow(Base):
    """``employees`` 行。"""

    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_employees_tenant_user"),
        UniqueConstraint(
            "tenant_id", "employee_id", name="uq_employees_tenant_employee"
        ),
        Index("ix_employees_tenant_role", "tenant_id", "role"),
        Index("ix_employees_tenant_active", "tenant_id", "is_active"),
    )

    employee_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    user_id: Mapped[str | None] = mapped_column(String(32))
    team_id: Mapped[str | None] = mapped_column(String(32))
    manager_id: Mapped[str | None] = mapped_column(String(32))
    languages: Mapped[list[str]] = mapped_column(
        postgresql.ARRAY(String), server_default=text("'{}'")
    )
    timezone: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    max_active_accounts: Mapped[int | None] = mapped_column(Integer)


class TerritoryAssignmentRow(Base):
    """``territory_assignments`` 行。"""

    __tablename__ = "territory_assignments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_territory_employee",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "manager_id"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_territory_manager",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "backup_employee_id"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_territory_backup",
        ),
        Index("ix_territory_tenant_priority", "tenant_id", "priority"),
    )

    assignment_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    employee_id: Mapped[str] = mapped_column(String(32))
    priority: Mapped[int] = mapped_column(Integer)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    countries: Mapped[list[str]] = mapped_column(
        postgresql.ARRAY(String), server_default=text("'{}'")
    )
    product_categories: Mapped[list[str]] = mapped_column(
        postgresql.ARRAY(String), server_default=text("'{}'")
    )
    need_categories: Mapped[list[str]] = mapped_column(
        postgresql.ARRAY(String), server_default=text("'{}'")
    )
    buyer_types: Mapped[list[str]] = mapped_column(
        postgresql.ARRAY(String), server_default=text("'{}'")
    )
    languages: Mapped[list[str]] = mapped_column(
        postgresql.ARRAY(String), server_default=text("'{}'")
    )
    manager_id: Mapped[str | None] = mapped_column(String(32))
    backup_employee_id: Mapped[str | None] = mapped_column(String(32))
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OwnershipLockRow(Base):
    """``ownership_locks`` 行。"""

    __tablename__ = "ownership_locks"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "account_id", name="uq_ownership_locks_tenant_account"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "owner"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_ownership_locks_owner",
        ),
    )

    lock_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    account_id: Mapped[str] = mapped_column(String(32))
    owner: Mapped[str] = mapped_column(String(32))
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    locked_by_rule: Mapped[str] = mapped_column(String(64))


class OwnershipTransferHistoryRow(Base):
    """``ownership_transfer_history`` 行（只增；reason 去空白非空 CHECK）。"""

    __tablename__ = "ownership_transfer_history"
    __table_args__ = (
        CheckConstraint("btrim(reason) <> ''", name="ck_transfer_reason_nonblank"),
        ForeignKeyConstraint(
            ["tenant_id", "from_owner"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_transfer_from_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "to_owner"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_transfer_to_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "transferred_by"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_transfer_transferred_by",
        ),
        Index("ix_transfer_tenant_account", "tenant_id", "account_id"),
    )

    transfer_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    account_id: Mapped[str] = mapped_column(String(32))
    from_owner: Mapped[str | None] = mapped_column(String(32))
    to_owner: Mapped[str] = mapped_column(String(32))
    transferred_by: Mapped[str] = mapped_column(String(32))
    transferred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(Text)


class WorkflowRunRow(Base):
    """``workflow_runs`` 行（引擎调度扫描主表）。

    与 0004 迁移逐列一致；``UNIQUE(tenant_id, idempotency_key)`` 支撑 start 幂等，
    ``UNIQUE(tenant_id, run_id)`` 供子表复合 FK 引用。``context`` JSONB 承载
    run 级上下文与引擎保留键（事件指纹）。
    """

    __tablename__ = "workflow_runs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_workflow_runs_tenant_key"
        ),
        UniqueConstraint("tenant_id", "run_id", name="uq_workflow_runs_tenant_run"),
        Index(
            "ix_workflow_runs_tenant_status_poll", "tenant_id", "status", "next_poll_at"
        ),
    )

    run_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    workflow_type: Mapped[str] = mapped_column(String(64))
    workflow_version: Mapped[int] = mapped_column(Integer)
    subject_ref: Mapped[str] = mapped_column(String(64))
    current_step: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), server_default=text("'running'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    context: Mapped[dict] = mapped_column(postgresql.JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(200))


class WorkflowStepRow(Base):
    """``workflow_steps`` 行（调度扫描单元；复合 FK → workflow_runs）。

    与 0004 迁移逐列一致；``UNIQUE(tenant_id, idempotency_key)`` 防止同一
    (run, step_name) 重复创建。``due_at`` 为下次可被 ``poll_due`` 领取的时间。
    """

    __tablename__ = "workflow_steps"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_workflow_steps_tenant_key"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["workflow_runs.tenant_id", "workflow_runs.run_id"],
            ondelete="CASCADE",
            name="fk_workflow_steps_run",
        ),
        Index("ix_workflow_steps_tenant_status_due", "tenant_id", "status", "due_at"),
    )

    step_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32))
    tenant_id: Mapped[str] = mapped_column(String(32))
    step_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), server_default=text("'pending'"))
    data: Mapped[dict] = mapped_column(postgresql.JSONB)
    attempt: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class RawArtifactRow(Base):
    """``raw_artifacts`` 原始证据 metadata 行；不含内容 bytes。"""

    __tablename__ = "raw_artifacts"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "artifact_id", name="pk_raw_artifacts"),
        UniqueConstraint(
            "tenant_id",
            "kind",
            "content_hash",
            name="uq_raw_artifacts_tenant_kind_hash",
        ),
        CheckConstraint(
            "tenant_id ~ '^tn_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_raw_artifacts_tenant",
        ),
        CheckConstraint(
            "artifact_id ~ '^art_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_raw_artifacts_id",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_raw_artifacts_hash"
        ),
        CheckConstraint("size_bytes > 0", name="ck_raw_artifacts_size"),
        CheckConstraint(
            "(kind='email_raw' AND mime_type='message/rfc822') OR "
            "(kind='chat_screenshot' AND mime_type IN "
            "('image/png','image/jpeg','image/webp')) OR "
            "(kind='pdf' AND mime_type='application/pdf') OR "
            "(kind='word' AND mime_type="
            "'application/vnd.openxmlformats-officedocument.wordprocessingml.document') OR "
            "(kind='excel' AND mime_type IN "
            "('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',"
            "'text/csv')) OR "
            "(kind='web_snapshot' AND mime_type='text/html') OR "
            "(kind='image' AND mime_type IN "
            "('image/png','image/jpeg','image/webp')) OR "
            "(kind='audio' AND mime_type IN "
            "('audio/mpeg','audio/wav','audio/mp4'))",
            name="ck_raw_artifacts_kind_mime",
        ),
        CheckConstraint(
            "object_key = 'raw/' || tenant_id || '/' || artifact_id",
            name="ck_raw_artifacts_object_key",
        ),
        CheckConstraint(
            "uploaded_by IS NULL OR uploaded_by ~ "
            "'^usr_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_raw_artifacts_uploader",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    artifact_id: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))
    content_hash: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mime_type: Mapped[str] = mapped_column(String(100))
    object_key: Mapped[str] = mapped_column(String(128))
    uploaded_by: Mapped[str | None] = mapped_column(String(32))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GeneratedArtifactRow(Base):
    """``artifacts`` 系统派生产物 metadata 行；不含邮件 subject/body。"""

    __tablename__ = "artifacts"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "artifact_id", name="pk_artifacts"),
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_artifacts_tenant_key"
        ),
        CheckConstraint(
            "tenant_id ~ '^tn_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_artifacts_tenant",
        ),
        CheckConstraint(
            "artifact_id ~ '^art_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_artifacts_id",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_artifacts_hash"
        ),
        CheckConstraint("size_bytes > 0", name="ck_artifacts_size"),
        CheckConstraint(
            "kind='email_draft' AND mime_type="
            "'application/vnd.tradeos.email-draft+json'",
            name="ck_artifacts_kind_mime",
        ),
        CheckConstraint(
            "object_key = 'generated/' || tenant_id || '/' || artifact_id",
            name="ck_artifacts_object_key",
        ),
        CheckConstraint(
            "workflow_run_id ~ '^run_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_artifacts_run",
        ),
        CheckConstraint(
            "subject_ref ~ '^enr_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_artifacts_subject",
        ),
        CheckConstraint("sequence_number > 0", name="ck_artifacts_sequence"),
        CheckConstraint(
            "idempotency_key = subject_ref || ':' || sequence_number::text "
            "|| ':' || 'draft'",
            name="ck_artifacts_idempotency",
        ),
        CheckConstraint(
            "generated_by ~ '^[a-z][a-z0-9_-]{0,63}$'",
            name="ck_artifacts_generated_by",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    artifact_id: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))
    content_hash: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mime_type: Mapped[str] = mapped_column(String(100))
    object_key: Mapped[str] = mapped_column(String(128))
    workflow_run_id: Mapped[str] = mapped_column(String(32))
    subject_ref: Mapped[str] = mapped_column(String(32))
    sequence_number: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    generated_by: Mapped[str] = mapped_column(String(64))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
