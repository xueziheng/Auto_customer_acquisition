"""三档计划、保守额度、耐久批次及筛选证据的租户绑定映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    PrimaryKeyConstraint,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from infra.db.tables import Base


class ProspectingServicePlanRow(Base):
    """配置版本不随后台额度/回执推进变化；pending key 不对外暴露。"""

    __tablename__ = "prospecting_service_plans"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", name="pk_prospecting_service_plans"),
        CheckConstraint(
            "length(trim(tenant_id)) > 0 AND length(trim(assigned_by)) > 0",
            name="ck_prospecting_plan_identity",
        ),
        CheckConstraint(
            "tier IN ('basic', 'standard', 'advanced') AND version >= 1",
            name="ck_prospecting_plan_tier",
        ),
        CheckConstraint(
            "period_ends_at > period_started_at", name="ck_prospecting_plan_period"
        ),
        CheckConstraint(
            "candidates_reserved >= 0 AND searches_reserved >= 0 AND contacts_reserved >= 0",
            name="ck_prospecting_plan_usage",
        ),
        CheckConstraint(
            "NOT schedule_enabled OR (acting_user_id IS NOT NULL AND next_run_at IS NOT NULL AND ((target_kind = 'legacy_research' AND proposal_id IS NOT NULL) OR (target_kind = 'inventory_request' AND inventory_query IS NOT NULL AND employee_id IS NOT NULL)))",
            name="ck_prospecting_plan_schedule",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    tier: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer)
    period_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    candidates_reserved: Mapped[int] = mapped_column(Integer)
    searches_reserved: Mapped[int] = mapped_column(Integer)
    contacts_reserved: Mapped[int] = mapped_column(Integer)
    schedule_enabled: Mapped[bool] = mapped_column(Boolean)
    target_kind: Mapped[str] = mapped_column(String(24), default="legacy_research", server_default="legacy_research")
    inventory_query: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    employee_id: Mapped[str | None] = mapped_column(String(64))
    proposal_id: Mapped[str | None] = mapped_column(String(64))
    acting_user_id: Mapped[str | None] = mapped_column(String(64))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_cycle_key: Mapped[str | None] = mapped_column(String(200))
    assigned_by: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProspectingServiceReservationRow(Base):
    """请求幂等身份跨续期保留，切档和重启均不能退还保守用量。"""

    __tablename__ = "prospecting_service_reservations"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "reservation_key", name="pk_prospecting_service_reservations"
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["prospecting_service_plans.tenant_id"],
            name="fk_prospecting_reservation_plan",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(trim(reservation_key)) > 0", name="ck_prospecting_reservation_key"
        ),
        CheckConstraint(
            "tier IN ('basic', 'standard', 'advanced')",
            name="ck_prospecting_reservation_tier",
        ),
        CheckConstraint(
            "candidates >= 0 AND searches >= 0 AND contacts >= 0 AND candidates + searches + contacts > 0",
            name="ck_prospecting_reservation_usage",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    reservation_key: Mapped[str] = mapped_column(String(200))
    tier: Mapped[str] = mapped_column(String(16))
    execution_usage: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    candidates: Mapped[int] = mapped_column(Integer)
    searches: Mapped[int] = mapped_column(Integer)
    contacts: Mapped[int] = mapped_column(Integer)
    period_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProspectingDiscoveryCycleRow(Base):
    """先记录批次授权再调用 engine；未知回执只能用原身份恢复。"""

    __tablename__ = "prospecting_discovery_cycles"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "cycle_key", name="pk_prospecting_discovery_cycles"
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["prospecting_service_plans.tenant_id"],
            name="fk_prospecting_cycle_plan",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "plan_version >= 1 AND length(trim(cycle_key)) > 0 AND length(trim(acting_user_id)) > 0 AND ((target_kind = 'legacy_research' AND proposal_id IS NOT NULL) OR (target_kind = 'inventory_request' AND inventory_query IS NOT NULL AND employee_id IS NOT NULL))",
            name="ck_prospecting_cycle_identity",
        ),
        CheckConstraint(
            "(run_id IS NULL AND request_key_hash IS NULL AND bound_at IS NULL) OR (target_kind = 'legacy_research' AND run_id IS NOT NULL AND request_key_hash IS NULL AND bound_at IS NOT NULL) OR (target_kind = 'inventory_request' AND run_id IS NULL AND request_key_hash IS NOT NULL AND bound_at IS NOT NULL)",
            name="ck_prospecting_cycle_binding",
        ),
        CheckConstraint(
            "(superseded_at IS NULL AND superseded_by_version IS NULL) OR "
            "(superseded_at IS NOT NULL AND superseded_by_version IS NOT NULL "
            "AND superseded_by_version > plan_version "
            "AND start_attempted_at IS NULL AND run_id IS NULL AND request_key_hash IS NULL)",
            name="ck_prospecting_cycle_superseded",
        ),
        CheckConstraint(
            "(run_id IS NULL AND request_key_hash IS NULL) OR start_attempted_at IS NOT NULL",
            name="ck_prospecting_cycle_start",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    cycle_key: Mapped[str] = mapped_column(String(200))
    plan_version: Mapped[int] = mapped_column(Integer)
    target_kind: Mapped[str] = mapped_column(String(24), default="legacy_research", server_default="legacy_research")
    inventory_query: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    employee_id: Mapped[str | None] = mapped_column(String(64))
    request_key_hash: Mapped[str | None] = mapped_column(String(64))
    proposal_id: Mapped[str | None] = mapped_column(String(64))
    acting_user_id: Mapped[str] = mapped_column(String(64))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    start_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_version: Mapped[int | None] = mapped_column(Integer)
    run_id: Mapped[str | None] = mapped_column(String(64))
    bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProspectingScreeningEvidenceRow(Base):
    """证据内容只增；复合外键不能把另一个租户的企业接到当前证据。"""

    __tablename__ = "prospecting_screening_evidence"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "account_id",
            "content_hash",
            name="pk_prospecting_screening_evidence",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["prospect_accounts.tenant_id", "prospect_accounts.account_id"],
            name="fk_prospecting_screening_account",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(content_hash) = 64", name="ck_prospecting_screening_hash"
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[str] = mapped_column(String(40))
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


SERVICE_PLAN_TABLES = tuple(
    row.__table__
    for row in (
        ProspectingServicePlanRow,
        ProspectingServiceReservationRow,
        ProspectingDiscoveryCycleRow,
        ProspectingScreeningEvidenceRow,
    )
)
