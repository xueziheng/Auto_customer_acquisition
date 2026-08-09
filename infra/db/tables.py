"""声明式 ORM 映射（业务表列与 0002 迁移逐列一致；0005 为 outbox 扩展）。

schema 由 Alembic 迁移管理——``Base.metadata.create_all`` 不是迁移的平行真相，
本模块只提供查询用的映射。列、约束、索引、FK 与迁移逐列一致，
金额 ``Numeric(18,2)`` + ``CHAR(3)`` 成对（硬边界 2）。触发器由数据库持有，
ORM 不表达触发器、也不绕过其只增语义。``outbox_events`` 的
``next_attempt_at``/``last_error`` 与 ``outbox_deliveries`` 由 0005 落地
（0005 自管 guard，不修改 0002）。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """声明式基类（schema 归迁移管理）。"""


class OpportunityRow(Base):
    """``opportunities`` 行。"""

    __tablename__ = "opportunities"
    __table_args__ = (
        UniqueConstraint("tenant_id", "need_id", name="uq_opportunities_tenant_need"),
        UniqueConstraint("tenant_id", "opportunity_id", name="uq_opportunities_tenant_opp"),
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
        ForeignKeyConstraint(
            ["tenant_id", "opportunity_id"],
            ["opportunities.tenant_id", "opportunities.opportunity_id"],
            ondelete="RESTRICT",
            name="fk_handoffs_opportunity",
        ),
        Index("ix_handoffs_tenant_state_requested", "tenant_id", "state", "requested_at"),
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
        Index("ix_loss_records_tenant_reason_state", "tenant_id", "loss_reason", "died_at_state"),
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
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmployeeRow(Base):
    """``employees`` 行。"""

    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_employees_tenant_user"),
        UniqueConstraint("tenant_id", "employee_id", name="uq_employees_tenant_employee"),
        Index("ix_employees_tenant_role", "tenant_id", "role"),
        Index("ix_employees_tenant_active", "tenant_id", "is_active"),
    )

    employee_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    user_id: Mapped[str | None] = mapped_column(String(32))
    team_id: Mapped[str | None] = mapped_column(String(32))
    manager_id: Mapped[str | None] = mapped_column(String(32))
    languages: Mapped[list[str]] = mapped_column(postgresql.ARRAY(String), server_default=text("'{}'"))
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
    countries: Mapped[list[str]] = mapped_column(postgresql.ARRAY(String), server_default=text("'{}'"))
    product_categories: Mapped[list[str]] = mapped_column(postgresql.ARRAY(String), server_default=text("'{}'"))
    need_categories: Mapped[list[str]] = mapped_column(postgresql.ARRAY(String), server_default=text("'{}'"))
    buyer_types: Mapped[list[str]] = mapped_column(postgresql.ARRAY(String), server_default=text("'{}'"))
    languages: Mapped[list[str]] = mapped_column(postgresql.ARRAY(String), server_default=text("'{}'"))
    manager_id: Mapped[str | None] = mapped_column(String(32))
    backup_employee_id: Mapped[str | None] = mapped_column(String(32))
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OwnershipLockRow(Base):
    """``ownership_locks`` 行。"""

    __tablename__ = "ownership_locks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "account_id", name="uq_ownership_locks_tenant_account"),
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
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_workflow_runs_tenant_key"),
        UniqueConstraint("tenant_id", "run_id", name="uq_workflow_runs_tenant_run"),
        Index("ix_workflow_runs_tenant_status_poll", "tenant_id", "status", "next_poll_at"),
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
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_workflow_steps_tenant_key"),
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
