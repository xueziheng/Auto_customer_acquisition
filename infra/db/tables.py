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


class _CostingEvidenceColumns:
    """四类只增确认共享的数据库列；不放业务规则。"""

    tenant_id: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(postgresql.JSONB)
    confirmed_by: Mapped[str] = mapped_column(String(40))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CostingPolicyRow(_CostingEvidenceColumns, Base):
    """costing_policies 的租户隔离、只增确认行。"""

    __tablename__ = "costing_policies"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "policy_id", name="pk_costing_policies"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_costing_policies_key"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'", name="ck_costing_policies_hash"),
        CheckConstraint("btrim(tenant_id) <> '' AND btrim(confirmed_by) <> '' AND btrim(idempotency_key) <> '' AND jsonb_typeof(payload) = 'object'", name="ck_costing_policies_confirmation"),
        ForeignKeyConstraint(["tenant_id", "artifact_id"], ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"], name="fk_costing_policies_artifact", ondelete="RESTRICT"),
        Index("ix_costing_policies_effective", "tenant_id", "category", "effective_from"),
    )

    policy_id: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    artifact_id: Mapped[str] = mapped_column(String(32))
    category: Mapped[str | None] = mapped_column(String(200))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CostingPriceEvidenceRow(_CostingEvidenceColumns, Base):
    """costing_price_evidence 的租户隔离、只增确认行。"""

    __tablename__ = "costing_price_evidence"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "evidence_id", name="pk_costing_price_evidence"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_costing_price_evidence_key"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$' AND evidence_hash ~ '^[0-9a-f]{64}$'", name="ck_costing_price_evidence_hash"),
        CheckConstraint("btrim(tenant_id) <> '' AND btrim(confirmed_by) <> '' AND btrim(idempotency_key) <> '' AND jsonb_typeof(payload) = 'object'", name="ck_costing_price_evidence_confirmation"),
        ForeignKeyConstraint(["tenant_id", "artifact_id"], ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"], name="fk_costing_price_evidence_artifact", ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "opportunity_id"], ["opportunities.tenant_id", "opportunities.opportunity_id"], name="fk_costing_price_evidence_opportunity", ondelete="RESTRICT"),
    )

    evidence_id: Mapped[str] = mapped_column(String(64))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    artifact_id: Mapped[str] = mapped_column(String(32))
    opportunity_id: Mapped[str] = mapped_column(String(40))


class CostingCoverageRow(_CostingEvidenceColumns, Base):
    """costing_coverage 的租户隔离、只增确认行。"""

    __tablename__ = "costing_coverage"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "coverage_id", name="pk_costing_coverage"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_costing_coverage_key"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'", name="ck_costing_coverage_hash"),
        CheckConstraint("btrim(tenant_id) <> '' AND btrim(confirmed_by) <> '' AND btrim(idempotency_key) <> '' AND jsonb_typeof(payload) = 'object'", name="ck_costing_coverage_confirmation"),
        ForeignKeyConstraint(["tenant_id", "cost_sheet_id"], ["cost_sheets.tenant_id", "cost_sheets.cost_sheet_id"], name="fk_costing_coverage_sheet", ondelete="RESTRICT"),
        CheckConstraint("sheet_hash ~ '^[0-9a-f]{64}$'", name="ck_costing_coverage_sheet_hash"),
        Index("ix_costing_coverage_sheet_hash","tenant_id","cost_sheet_id","sheet_hash","confirmed_at","coverage_id"),
    )

    coverage_id: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    cost_sheet_id: Mapped[str] = mapped_column(String(40))
    sheet_hash: Mapped[str] = mapped_column(String(64))


class CostingQuoteFxRow(_CostingEvidenceColumns, Base):
    """costing_quote_fx 的租户隔离、只增确认行。"""

    __tablename__ = "costing_quote_fx"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "fx_id", name="pk_costing_quote_fx"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_costing_quote_fx_key"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'", name="ck_costing_quote_fx_hash"),
        CheckConstraint("btrim(tenant_id) <> '' AND btrim(confirmed_by) <> '' AND btrim(idempotency_key) <> '' AND jsonb_typeof(payload) = 'object'", name="ck_costing_quote_fx_confirmation"),
        ForeignKeyConstraint(["tenant_id", "artifact_id"], ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"], name="fk_costing_quote_fx_artifact", ondelete="RESTRICT"),
    )

    fx_id: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    artifact_id: Mapped[str] = mapped_column(String(32))


class SearchQuotaAccountRow(Base):
    """数据库级 Tavily 单账户槽；全局唯一防止租户或 key 引用复制额度。"""

    __tablename__ = "search_quota_accounts"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "provider", name="pk_search_quota_accounts"),
        UniqueConstraint("provider", name="uq_search_quota_accounts_provider"),
        CheckConstraint("provider = 'tavily'", name="ck_search_quota_accounts_provider"),
        CheckConstraint("ceiling IS NULL OR ceiling >= 0", name="ck_search_quota_accounts_ceiling"),
        CheckConstraint("reservations >= 0", name="ck_search_quota_accounts_reservations"),
        CheckConstraint("cost_status IN ('free','paid','unknown')", name="ck_search_quota_accounts_cost_status"),
        CheckConstraint("usage_limit IS NULL OR usage_limit >= 0", name="ck_search_quota_accounts_usage_limit"),
        CheckConstraint("usage_used IS NULL OR usage_used >= 0", name="ck_search_quota_accounts_usage_used"),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(16))
    ceiling: Mapped[int | None] = mapped_column(BigInteger)
    reservations: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    cost_status: Mapped[str] = mapped_column(String(16), server_default=text("'unknown'"))
    usage_limit: Mapped[int | None] = mapped_column(BigInteger)
    usage_used: Mapped[int | None] = mapped_column(BigInteger)
    paygo_enabled: Mapped[bool | None] = mapped_column(Boolean)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SearchQuotaRunRow(Base):
    """保存不可替换的 Run 指纹版本与停止原因，不保存输入或供应商原文。"""

    __tablename__ = "search_quota_runs"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "run_id", name="pk_search_quota_runs"),
        CheckConstraint(
            "stop_reason IN ('quota_exhausted','usage_unknown','paid_enabled','request_uncertain','unsupported')",
            name="ck_search_quota_runs_stop_reason",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    run_id: Mapped[str] = mapped_column(String(40))
    fingerprint_version: Mapped[str | None] = mapped_column(String(100))
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SearchQuotaReservationRow(Base):
    """本地每次搜索恒为 basic 一信用额；状态只前进，不提供自动释放。"""

    __tablename__ = "search_quota_reservations"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "provider", "run_id", "request_key", name="pk_search_quota_reservations"),
        ForeignKeyConstraint(
            ["tenant_id", "provider"],
            ["search_quota_accounts.tenant_id", "search_quota_accounts.provider"],
            name="fk_search_quota_reservations_account",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('reserved','uncertain','consumed')",
            name="ck_search_quota_reservations_status",
        ),
        CheckConstraint(
            "request_key ~ '^[a-f0-9]{64}$'",
            name="ck_search_quota_reservations_request_key",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(16))
    run_id: Mapped[str] = mapped_column(String(40))
    request_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DirectiveProposalRow(Base):
    """老板自然语言解析后的待确认提案。"""

    __tablename__ = "directive_proposals"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "proposal_id", name="pk_directive_proposals"
        ),
        CheckConstraint(
            "state IN ('pending_confirmation','confirmed','rejected','expired')",
            name="ck_directive_proposals_state",
        ),
        CheckConstraint(
            "jsonb_typeof(parsed_content) = 'object' AND "
            "jsonb_typeof(expected_behavior_changes) = 'array' AND "
            "jsonb_array_length(expected_behavior_changes) > 0",
            name="ck_directive_proposals_jsonb",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(proposal_id) <> '' AND "
            "btrim(raw_text) <> '' AND btrim(interpretation_summary) <> '' AND "
            "btrim(parsed_by) <> ''",
            name="ck_directive_proposals_core_nonblank",
        ),
        CheckConstraint(
            "(state = 'pending_confirmation' AND decided_at IS NULL AND "
            "decided_by IS NULL) OR "
            "(state = 'expired' AND decided_at IS NOT NULL) OR "
            "(state IN ('confirmed','rejected') AND decided_at IS NOT NULL AND "
            "decided_by IS NOT NULL)",
            name="ck_directive_proposals_decision",
        ),
        Index(
            "ix_directive_proposals_tenant_state_created",
            "tenant_id",
            "state",
            "created_at",
            "proposal_id",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    proposal_id: Mapped[str] = mapped_column(String(40))
    raw_text: Mapped[str] = mapped_column(Text)
    parsed_content: Mapped[dict] = mapped_column(postgresql.JSONB)
    interpretation_summary: Mapped[str] = mapped_column(Text)
    expected_behavior_changes: Mapped[list] = mapped_column(postgresql.JSONB)
    parsed_by: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(
        String(32), server_default=text("'pending_confirmation'")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(40))


class DirectiveVersionRow(Base):
    """不可原地改写内容的老板指令历史版本。"""

    __tablename__ = "directive_versions"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "directive_id", name="pk_directive_versions"
        ),
        UniqueConstraint(
            "tenant_id", "version", name="uq_directive_versions_tenant_version"
        ),
        UniqueConstraint(
            "tenant_id",
            "directive_id",
            "version",
            name="uq_directive_versions_pointer",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_proposal_id"],
            ["directive_proposals.tenant_id", "directive_proposals.proposal_id"],
            ondelete="RESTRICT",
            name="fk_directive_versions_proposal",
        ),
        CheckConstraint("version >= 1", name="ck_directive_versions_version"),
        CheckConstraint(
            "jsonb_typeof(content) = 'object'",
            name="ck_directive_versions_content_jsonb",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(directive_id) <> '' AND "
            "btrim(source_proposal_id) <> '' AND btrim(activated_by) <> ''",
            name="ck_directive_versions_core_nonblank",
        ),
        CheckConstraint(
            "superseded_at IS NULL OR superseded_at >= activated_at",
            name="ck_directive_versions_superseded_at",
        ),
        CheckConstraint(
            "rollback_of IS NULL OR (rollback_of >= 1 AND rollback_of < version)",
            name="ck_directive_versions_rollback",
        ),
        Index(
            "ix_directive_versions_tenant_version",
            "tenant_id",
            "version",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    directive_id: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(postgresql.JSONB)
    source_proposal_id: Mapped[str] = mapped_column(String(40))
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    activated_by: Mapped[str] = mapped_column(String(40))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rollback_of: Mapped[int | None] = mapped_column(Integer)


class BossDirectiveRow(Base):
    """每租户唯一的当前生效指令指针。"""

    __tablename__ = "boss_directives"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", name="pk_boss_directives"),
        ForeignKeyConstraint(
            ["tenant_id", "directive_id", "version"],
            [
                "directive_versions.tenant_id",
                "directive_versions.directive_id",
                "directive_versions.version",
            ],
            ondelete="RESTRICT",
            name="fk_boss_directives_version",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(directive_id) <> '' AND version >= 1",
            name="ck_boss_directives_core",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    directive_id: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ApprovalPackageRow(Base):
    """不可改写内容、只推进状态的人工审批包。"""

    __tablename__ = "approval_packages"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "approval_id", name="pk_approval_packages"),
        CheckConstraint(
            "state IN ('pending','approved','applied','apply_failed','rejected','expired')",
            name="ck_approval_packages_state",
        ),
        CheckConstraint(
            "jsonb_typeof(proposed_change) = 'object' AND "
            "jsonb_typeof(blast_radius) = 'object' AND "
            "jsonb_typeof(evidence_refs) = 'array'",
            name="ck_approval_packages_jsonb",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(approval_id) <> '' AND "
            "btrim(approval_type) <> '' AND btrim(title) <> '' AND btrim(reason) <> ''",
            name="ck_approval_packages_core_nonblank",
        ),
        CheckConstraint("expires_at > created_at", name="ck_approval_packages_expiry"),
        CheckConstraint(
            """(contract_namespace IS NULL AND request_hash IS NULL AND NOT
            (lower(btrim(coalesce(change_set_ref,''))) LIKE 'quote:%' OR
             lower(btrim(coalesce(proposed_change->>'schema_version',''))) LIKE 'quote-approval%')) OR
            coalesce((contract_namespace='quote-approval-v1' AND request_hash ~ '^[0-9a-f]{64}$'
              AND expires_at_limit IS NOT NULL AND expires_at <= expires_at_limit
              AND proposed_change->>'schema_version'='quote-approval-v1'
              AND proposed_change->>'tenant_id'=tenant_id
              AND proposed_change->>'approval_type'=approval_type
              AND proposed_change->>'prepared_by'=proposed_by_employee
              AND proposed_change->>'submitted_owner_id'=owner_employee
              AND jsonb_typeof(proposed_change->'quote_version')='number'
              AND (proposed_change->>'quote_version') ~ '^[1-9][0-9]*$'
              AND proposed_change->>'quote_id' ~ '^quo_[0-9A-HJKMNP-TV-Z]{26}$'
              AND proposed_change->>'content_hash' ~ '^[0-9a-f]{64}$'
              AND approval_type IN ('quote_send','margin_floor_override','discount','delivery_commitment','payment_terms','certification_commitment')
              AND change_set_ref='quote:'||(proposed_change->>'quote_id')||':'||
                  (proposed_change->>'content_hash')||':'||approval_type),false)""",
            name="ck_approval_quote_contract",
        ),
        CheckConstraint(
            "(state = 'pending' AND decided_at IS NULL AND decided_by IS NULL) OR "
            "(state = 'expired' AND decided_at IS NULL AND decided_by IS NULL) OR "
            "(state IN ('approved','applied','apply_failed','rejected') AND "
            "decided_at IS NOT NULL AND decided_by IS NOT NULL)",
            name="ck_approval_packages_decision",
        ),
        CheckConstraint(
            "(state = 'applied' AND applied_at IS NOT NULL AND apply_error IS NULL) OR "
            "(state = 'apply_failed' AND applied_at IS NULL AND apply_error IS NOT NULL) OR "
            "(state NOT IN ('applied','apply_failed') AND applied_at IS NULL AND apply_error IS NULL)",
            name="ck_approval_packages_application",
        ),
        Index(
            "ix_approval_packages_tenant_state_expiry",
            "tenant_id",
            "state",
            "expires_at",
            "approval_id",
        ),
        Index(
            "ix_approval_packages_tenant_change_set",
            "tenant_id",
            "change_set_ref",
            "created_at",
        ),
        Index(
            "uq_approval_packages_pending_change_set",
            "tenant_id",
            "change_set_ref",
            unique=True,
            postgresql_where=text("state = 'pending' AND change_set_ref IS NOT NULL"),
        ),
        Index(
            "uq_approval_quote_change_set", "tenant_id", "change_set_ref",
            unique=True,
            postgresql_where=text("contract_namespace='quote-approval-v1'"),
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    approval_id: Mapped[str] = mapped_column(String(40))
    approval_type: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    proposed_change: Mapped[dict] = mapped_column(postgresql.JSONB)
    reason: Mapped[str] = mapped_column(Text)
    blast_radius: Mapped[dict] = mapped_column(postgresql.JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(32), server_default=text("'pending'"))
    proposed_by_run: Mapped[str | None] = mapped_column(String(40))
    proposed_by_employee: Mapped[str | None] = mapped_column(String(40))
    evidence_refs: Mapped[list] = mapped_column(postgresql.JSONB)
    change_set_ref: Mapped[str | None] = mapped_column(String(200))
    owner_employee: Mapped[str | None] = mapped_column(String(40))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(40))
    decision_note: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    apply_error: Mapped[str | None] = mapped_column(Text)
    contract_namespace: Mapped[str | None] = mapped_column(String(32))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    expires_at_limit: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalApplicationRow(Base):
    """每个审批最多一次的幂等应用事实。"""

    __tablename__ = "approval_applications"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "approval_id", name="pk_approval_applications"
        ),
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_approval_applications_key"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "approval_id"],
            ["approval_packages.tenant_id", "approval_packages.approval_id"],
            ondelete="RESTRICT",
            name="fk_approval_applications_package",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(approval_id) <> '' AND "
            "btrim(idempotency_key) <> ''",
            name="ck_approval_applications_nonblank",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    approval_id: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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
            "'page_access_forbidden','login_or_captcha','unsafe_redirect',"
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
            "'page_access_forbidden','login_or_captcha','unsafe_redirect',"
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
        ForeignKeyConstraint(
            ["tenant_id", "source_hypothesis_id"],
            ["need_hypotheses.tenant_id", "need_hypotheses.hypothesis_id"],
            ondelete="RESTRICT",
            name="fk_outreach_enrollments_source_hypothesis",
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
        Index(
            "ix_outreach_enrollments_tenant_source_hypothesis",
            "tenant_id",
            "source_hypothesis_id",
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
    source_hypothesis_id: Mapped[str | None] = mapped_column(String(40))


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


_HYPOTHESIS_STATUSES = "'inferred','contacting','validated','rejected'"
_NEED_STATUSES = (
    "'validated','sourcing_ready','handed_to_sourcing',"
    "'fulfilled','withdrawn','lost'"
)


class NeedHypothesisRow(Base):
    """``need_hypotheses`` 行（spec 2026-08-17 §4.1）。"""

    __tablename__ = "need_hypotheses"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "hypothesis_id", name="pk_need_hypotheses"),
        ForeignKeyConstraint(
            ["tenant_id", "validated_need_id"],
            ["validated_needs.tenant_id", "validated_needs.need_id"],
            name="fk_need_hypotheses_validated_need",
        ),
        Index(
            "uq_need_hypotheses_active_account_category",
            "tenant_id",
            "account_id",
            "category",
            unique=True,
            postgresql_where=text("status IN ('inferred','contacting')"),
        ),
        CheckConstraint(
            f"status IN ({_HYPOTHESIS_STATUSES})", name="ck_need_hypotheses_status"
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(hypothesis_id) <> '' AND "
            "btrim(account_id) <> '' AND btrim(category) <> ''",
            name="ck_need_hypotheses_core_nonblank",
        ),
        CheckConstraint(
            "btrim(category) <> ''",
            name="ck_need_hypotheses_category_nonblank",
        ),
        CheckConstraint(
            "jsonb_typeof(reasoning) = 'object'",
            name="ck_need_hypotheses_reasoning_jsonb",
        ),
        CheckConstraint(
            "jsonb_typeof(signal_ids) = 'array'",
            name="ck_need_hypotheses_signal_ids_jsonb",
        ),
        CheckConstraint(
            "(status = 'rejected') = "
            "(rejection_reason IS NOT NULL AND btrim(rejection_reason) <> '')",
            name="ck_need_hypotheses_rejection_reason",
        ),
        CheckConstraint(
            "(status = 'validated') = (validated_need_id IS NOT NULL)",
            name="ck_need_hypotheses_validated_link",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    hypothesis_id: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[str] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(200))
    reasoning: Mapped[dict] = mapped_column(postgresql.JSONB)
    signal_ids: Mapped[list] = mapped_column(postgresql.JSONB)
    status: Mapped[str] = mapped_column(String(20), server_default=text("'inferred'"))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    validated_need_id: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NeedClusterRow(Base):
    """Phase 1 需求簇；只记录归簇，不参与寻源排序。"""

    __tablename__ = "need_clusters"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "cluster_id", name="pk_need_clusters"),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(cluster_id) <> '' AND "
            "btrim(category) <> ''",
            name="ck_need_clusters_core_nonblank",
        ),
        CheckConstraint(
            "jsonb_typeof(keywords) = 'array' AND "
            "jsonb_typeof(countries) = 'array'",
            name="ck_need_clusters_arrays_jsonb",
        ),
        CheckConstraint(
            "total_potential_quantity IS NULL OR total_potential_quantity >= 0",
            name="ck_need_clusters_quantity_nonnegative",
        ),
        Index(
            "ix_need_clusters_tenant_category",
            "tenant_id",
            "category",
            "created_at",
            "cluster_id",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    cluster_id: Mapped[str] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(200))
    keywords: Mapped[list] = mapped_column(postgresql.JSONB)
    countries: Mapped[list] = mapped_column(postgresql.JSONB)
    total_potential_quantity: Mapped[int | None] = mapped_column(Integer)
    recurring_demand: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NeedClusterMemberRow(Base):
    """需求簇成员关系；租户复合外键同时约束簇与已验证需求。"""

    __tablename__ = "need_cluster_members"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "cluster_id",
            "need_id",
            name="pk_need_cluster_members",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cluster_id"],
            ["need_clusters.tenant_id", "need_clusters.cluster_id"],
            ondelete="CASCADE",
            name="fk_need_cluster_members_cluster",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "need_id"],
            ["validated_needs.tenant_id", "validated_needs.need_id"],
            ondelete="CASCADE",
            name="fk_need_cluster_members_need",
        ),
        Index(
            "uq_need_cluster_members_need",
            "tenant_id",
            "need_id",
            unique=True,
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    cluster_id: Mapped[str] = mapped_column(String(40))
    need_id: Mapped[str] = mapped_column(String(40))
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ValidatedNeedRow(Base):
    """``validated_needs`` 行（spec 2026-08-17 §4.2）。"""

    __tablename__ = "validated_needs"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "need_id", name="pk_validated_needs"),
        ForeignKeyConstraint(
            ["tenant_id", "need_id", "unit_confirmation_id"],
            ["need_unit_confirmations.tenant_id", "need_unit_confirmations.need_id",
             "need_unit_confirmations.confirmation_id"],
            name="fk_validated_needs_unit_confirmation", use_alter=True,
        ),
        CheckConstraint(
            "(unit IS NULL AND unit_quantity_fact_hash IS NULL AND unit_confirmation_id IS NULL) OR "
            "(unit IS NOT NULL AND unit_quantity_fact_hash IS NOT NULL AND unit_confirmation_id IS NOT NULL)",
            name="ck_validated_needs_unit_binding",
        ),
        CheckConstraint("unit IS NULL OR jsonb_typeof(unit) = 'object'",
                        name="ck_validated_needs_unit_jsonb"),
        CheckConstraint("unit_quantity_fact_hash IS NULL OR unit_quantity_fact_hash ~ '^[0-9a-f]{64}$'",
                        name="ck_validated_needs_unit_hash"),
        ForeignKeyConstraint(
            ["tenant_id", "cluster_id"],
            ["need_clusters.tenant_id", "need_clusters.cluster_id"],
            name="fk_validated_needs_cluster",
        ),
        CheckConstraint(
            f"status IN ({_NEED_STATUSES})", name="ck_validated_needs_status"
        ),
        CheckConstraint(
            "jsonb_typeof(product_category) = 'object'",
            name="ck_validated_needs_category_jsonb",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(need_id) <> '' AND "
            "btrim(account_id) <> '' AND btrim(source_message_id) <> ''",
            name="ck_validated_needs_core_nonblank",
        ),
        CheckConstraint(
            "btrim(source_message_id) <> ''",
            name="ck_validated_needs_source_message_nonblank",
        ),
        CheckConstraint(
            "(application IS NULL) OR jsonb_typeof(application) = 'object'",
            name="ck_validated_needs_application_jsonb",
        ),
        CheckConstraint(
            "(material IS NULL) OR jsonb_typeof(material) = 'object'",
            name="ck_validated_needs_material_jsonb",
        ),
        CheckConstraint(
            "(size_spec IS NULL) OR jsonb_typeof(size_spec) = 'object'",
            name="ck_validated_needs_size_spec_jsonb",
        ),
        CheckConstraint(
            "(quantity IS NULL) OR jsonb_typeof(quantity) = 'object'",
            name="ck_validated_needs_quantity_jsonb",
        ),
        CheckConstraint(
            "(packaging IS NULL) OR jsonb_typeof(packaging) = 'object'",
            name="ck_validated_needs_packaging_jsonb",
        ),
        CheckConstraint(
            "(destination IS NULL) OR jsonb_typeof(destination) = 'object'",
            name="ck_validated_needs_destination_jsonb",
        ),
        CheckConstraint(
            "(required_by IS NULL) OR jsonb_typeof(required_by) = 'object'",
            name="ck_validated_needs_required_by_jsonb",
        ),
        CheckConstraint(
            "(target_price IS NULL) OR jsonb_typeof(target_price) = 'object'",
            name="ck_validated_needs_target_price_jsonb",
        ),
        CheckConstraint(
            "(current_supply_issue IS NULL) OR jsonb_typeof(current_supply_issue) = 'object'",
            name="ck_validated_needs_current_supply_issue_jsonb",
        ),
        CheckConstraint(
            "(certification_required IS NULL) OR jsonb_typeof(certification_required) = 'object'",
            name="ck_validated_needs_certification_required_jsonb",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    need_id: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[str] = mapped_column(String(40))
    product_category: Mapped[dict] = mapped_column(postgresql.JSONB)
    source_message_id: Mapped[str] = mapped_column(String(40))
    source_conversation_id: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), server_default=text("'validated'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 可选事实字段的缺省值必须是 SQL NULL；JSON ``null`` 不携带 provenance，
    # 且会违反上面的对象形状约束。
    application: Mapped[dict | None] = mapped_column(
        postgresql.JSONB(none_as_null=True)
    )
    material: Mapped[dict | None] = mapped_column(
        postgresql.JSONB(none_as_null=True)
    )
    size_spec: Mapped[dict | None] = mapped_column(
        postgresql.JSONB(none_as_null=True)
    )
    quantity: Mapped[dict | None] = mapped_column(
        postgresql.JSONB(none_as_null=True)
    )
    packaging: Mapped[dict | None] = mapped_column(
        postgresql.JSONB(none_as_null=True)
    )
    destination: Mapped[dict | None] = mapped_column(
        postgresql.JSONB(none_as_null=True)
    )
    required_by: Mapped[dict | None] = mapped_column(
        postgresql.JSONB(none_as_null=True)
    )
    target_price: Mapped[dict | None] = mapped_column(
        postgresql.JSONB(none_as_null=True)
    )
    current_supply_issue: Mapped[dict | None] = mapped_column(
        postgresql.JSONB(none_as_null=True)
    )
    certification_required: Mapped[dict | None] = mapped_column(
        postgresql.JSONB(none_as_null=True)
    )
    confirmed_by: Mapped[str | None] = mapped_column(String(40))
    cluster_id: Mapped[str | None] = mapped_column(String(40))

    unit: Mapped[dict | None] = mapped_column(postgresql.JSONB(none_as_null=True))
    unit_quantity_fact_hash: Mapped[str | None] = mapped_column(String(64))
    unit_confirmation_id: Mapped[str | None] = mapped_column(String(40))


class NeedUnitConfirmationRow(Base):
    """客户单位不可变确认；与Need、原件均以tenant复合外键绑定。"""

    __tablename__ = "need_unit_confirmations"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "confirmation_id", name="pk_need_unit_confirmations"),
        UniqueConstraint("tenant_id", "need_id", "confirmation_id", name="uq_need_unit_confirmations_need_id"),
        UniqueConstraint("tenant_id", "need_id", "idempotency_key", name="uq_need_unit_confirmations_key"),
        ForeignKeyConstraint(["tenant_id", "need_id"],
            ["validated_needs.tenant_id", "validated_needs.need_id"],
            name="fk_need_unit_confirmations_need"),
        ForeignKeyConstraint(["tenant_id", "artifact_id"],
            ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"],
            name="fk_need_unit_confirmations_artifact"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$' AND quantity_fact_hash ~ '^[0-9a-f]{64}$'",
            name="ck_need_unit_confirmations_hash"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_need_unit_confirmations_payload"),
        CheckConstraint("btrim(tenant_id) <> '' AND btrim(confirmation_id) <> '' AND "
            "btrim(need_id) <> '' AND btrim(artifact_id) <> '' AND btrim(source_message_id) <> '' "
            "AND btrim(confirmed_by) <> '' AND btrim(idempotency_key) <> ''",
            name="ck_need_unit_confirmations_nonblank"),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    confirmation_id: Mapped[str] = mapped_column(String(40))
    need_id: Mapped[str] = mapped_column(String(40))
    artifact_id: Mapped[str] = mapped_column(String(40))
    source_message_id: Mapped[str] = mapped_column(String(40))
    confirmed_by: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    quantity_fact_hash: Mapped[str] = mapped_column(String(64))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(postgresql.JSONB)


class ValidatedNeedFieldHistoryRow(Base):
    """``validated_need_field_history`` 行（spec 2026-08-17 §4.3；append-only）。"""

    __tablename__ = "validated_need_field_history"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "history_id", name="pk_validated_need_field_history"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "need_id"],
            ["validated_needs.tenant_id", "validated_needs.need_id"],
            name="fk_validated_need_field_history_need",
        ),
        Index(
            "ix_validated_need_field_history_need",
            "tenant_id",
            "need_id",
            "changed_at",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(history_id) <> '' AND "
            "btrim(need_id) <> '' AND btrim(field_name) <> '' AND "
            "btrim(new_value) <> '' AND btrim(source_message_id) <> ''",
            name="ck_validated_need_field_history_core_nonblank",
        ),
        CheckConstraint(
            "btrim(field_name) <> ''",
            name="ck_validated_need_field_history_field_name_nonblank",
        ),
        CheckConstraint(
            "btrim(new_value) <> ''",
            name="ck_validated_need_field_history_new_value_nonblank",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    history_id: Mapped[str] = mapped_column(String(40))
    need_id: Mapped[str] = mapped_column(String(40))
    field_name: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str] = mapped_column(Text)
    source_message_id: Mapped[str] = mapped_column(String(40))
    changed_by: Mapped[str | None] = mapped_column(String(40))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConversationRow(Base):
    """``conversations`` 会话（跨渠道，Phase 1 只有邮件）；tenant+account+channel 唯一。"""

    __tablename__ = "conversations"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "conversation_id", name="pk_conversations"),
        UniqueConstraint(
            "tenant_id",
            "account_id",
            "channel",
            name="uq_conversations_tenant_account_channel",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    conversation_id: Mapped[str] = mapped_column(String(32))
    account_id: Mapped[str] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_inbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_outbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class MessageRow(Base):
    """``messages`` 消息（含方向、语言、原文引用）；external Message-ID 租户内唯一。"""

    __tablename__ = "messages"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "message_id", name="pk_messages"),
        UniqueConstraint(
            "tenant_id",
            "external_message_id",
            name="uq_messages_tenant_external_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.conversation_id"],
            ondelete="RESTRICT",
            name="fk_messages_conversation",
        ),
        CheckConstraint(
            "direction IN ('inbound', 'outbound')",
            name="ck_messages_direction",
        ),
        CheckConstraint(
            "length(btrim(external_message_id)) > 0",
            name="ck_messages_external_id_nonblank",
        ),
        CheckConstraint(
            "length(btrim(raw_artifact_ref)) > 0",
            name="ck_messages_raw_ref_nonblank",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    message_id: Mapped[str] = mapped_column(String(32))
    conversation_id: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(16))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    language: Mapped[str | None] = mapped_column(String(16))
    raw_artifact_ref: Mapped[str] = mapped_column(String(100))
    external_message_id: Mapped[str] = mapped_column(String(256))
    outbound_message_id: Mapped[str | None] = mapped_column(String(256))


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
            "(kind='email_draft' AND mime_type='application/vnd.tradeos.email-draft+json' AND subject_ref ~ '^enr_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND idempotency_key=subject_ref || ':' || sequence_number::text || ':' || 'draft' AND generated_by ~ '^[a-z][a-z0-9_-]{0,63}$') OR (kind='quote_pdf' AND mime_type='application/pdf' AND subject_ref ~ '^quo_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND generated_by='quote_pdf_v1' AND idempotency_key=subject_ref || ':' || sequence_number::text || ':quote_pdf:' || generated_by)",
            name="ck_artifacts_binding",
        ),
        CheckConstraint(
            "object_key = 'generated/' || tenant_id || '/' || artifact_id",
            name="ck_artifacts_object_key",
        ),
        CheckConstraint(
            "workflow_run_id ~ '^run_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_artifacts_run",
        ),
        CheckConstraint("sequence_number > 0", name="ck_artifacts_sequence"),
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


class QuotationFileRow(Base):
    """报价文件只增metadata关联，三个hash职责不混淆。"""

    __tablename__ = "quotation_files"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "file_id", name="pk_quotation_files"),
        UniqueConstraint("tenant_id", "quote_id", "template_version", name="uq_quote_file_template"),
        ForeignKeyConstraint(["tenant_id", "quote_id"], ["quotations.tenant_id", "quotations.quote_id"], name="fk_quote_file_quote", ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "artifact_id"], ["artifacts.tenant_id", "artifacts.artifact_id"], name="fk_quote_file_artifact", ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "quote_id"], ["quotation_approval_receipts.tenant_id", "quotation_approval_receipts.quote_id"], name="fk_quote_file_receipt", ondelete="RESTRICT"),
        CheckConstraint("quote_version>0 AND size_bytes>0 AND isfinite(generated_at)", name="ck_quote_file_positive"),
        CheckConstraint("template_version='quote_pdf_v1'", name="ck_quote_file_template"),
        CheckConstraint("quote_content_hash ~ '^[0-9a-f]{64}$' AND customer_content_hash ~ '^[0-9a-f]{64}$' AND artifact_hash ~ '^[0-9a-f]{64}$'", name="ck_quote_file_hash"),
        CheckConstraint("tenant_id ~ '^tn_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND file_id ~ '^qfl_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND quote_id ~ '^quo_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND artifact_id ~ '^art_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND approval_run_id ~ '^run_[0-7][0-9A-HJKMNP-TV-Z]{25}$'", name="ck_quote_file_ids"),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    file_id: Mapped[str] = mapped_column(String(40))
    quote_id: Mapped[str] = mapped_column(String(40))
    quote_version: Mapped[int] = mapped_column(Integer)
    artifact_id: Mapped[str] = mapped_column(String(32))
    quote_content_hash: Mapped[str] = mapped_column(String(64))
    customer_content_hash: Mapped[str] = mapped_column(String(64))
    artifact_hash: Mapped[str] = mapped_column(String(64))
    template_version: Mapped[str] = mapped_column(String(64))
    approval_run_id: Mapped[str] = mapped_column(String(40))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConversationClassificationRow(Base):
    """``conversation_classifications``：回复分类留痕。

    每 (tenant, message) 至多一条（PK 唯一约束在并发下强制单行；跨版本重评
    由服务层显式拒绝）；classified_by 为分类者标识（模型版本/人工标识），
    用于按版本评估；不存任何数值置信度（硬边界 3）。
    """

    __tablename__ = "conversation_classifications"
    __table_args__ = (
        # 契约：每 (tenant, message) 至多一条分类（跨版本重评显式拒绝）——
        # 唯一约束在并发下强制单行，服务层 check-then-insert 不是唯一保障
        PrimaryKeyConstraint(
            "tenant_id",
            "message_id",
            name="pk_conversation_classifications",
        ),
        CheckConstraint(
            "category IN ('clear_interest','willing_to_continue','requests_materials',"
            "'requests_quote','requests_sample','provides_specification',"
            "'no_current_need','future_need_possible','refers_other_contact',"
            "'rejection','unsubscribe','bounce','auto_reply','complaint')",
            name="ck_conversation_classifications_category",
        ),
        CheckConstraint(
            "jsonb_typeof(candidate_fields) = 'array'",
            name="ck_conversation_classifications_candidate_fields",
        ),
        CheckConstraint(
            "(category = 'unsubscribe' AND suppress_scope IN ('contact','account')) "
            "OR (category <> 'unsubscribe' AND suppress_scope IS NULL)",
            name="ck_conversation_classifications_suppress_scope",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    message_id: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(40))
    classified_by: Mapped[str] = mapped_column(String(100))
    classified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    candidate_fields: Mapped[list[dict[str, str]]] = mapped_column(
        postgresql.JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    suppress_scope: Mapped[str | None] = mapped_column(String(16))


class ConversationReplyWorkRow(Base):
    """回复产生的 metadata-only owner queue；message+action 天然幂等。"""

    __tablename__ = "conversation_reply_work"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "action_id",
            name="pk_conversation_reply_work",
        ),
        UniqueConstraint(
            "tenant_id",
            "message_id",
            "action",
            name="uq_conversation_reply_work_message_action",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_conversation_reply_work_idempotency",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "message_id"],
            ["messages.tenant_id", "messages.message_id"],
            ondelete="RESTRICT",
            name="fk_conversation_reply_work_message",
        ),
        CheckConstraint(
            "action IN ('start_qualification','mark_future_restart',"
            "'create_follow_up','intake_new_contact')",
            name="ck_conversation_reply_work_action",
        ),
        CheckConstraint(
            "status IN ('pending','in_progress','completed','cancelled')",
            name="ck_conversation_reply_work_status",
        ),
        CheckConstraint(
            "(action = 'start_qualification' AND owner_queue = 'need_qualification') OR "
            "(action = 'mark_future_restart' AND owner_queue = 'future_restart_review') OR "
            "(action = 'create_follow_up' AND owner_queue = 'follow_up') OR "
            "(action = 'intake_new_contact' AND "
            "owner_queue = 'verified_contact_intake_review')",
            name="ck_conversation_reply_work_queue",
        ),
        Index(
            "ix_conversation_reply_work_owner_queue",
            "tenant_id",
            "status",
            "owner_queue",
            "created_at",
            "action_id",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    action_id: Mapped[str] = mapped_column(String(32))
    message_id: Mapped[str] = mapped_column(String(32))
    outbound_message_id: Mapped[str] = mapped_column(String(256))
    enrollment_id: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[str] = mapped_column(String(40))
    contact_point_id: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(40))
    owner_queue: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConversationClassificationCorrectionRow(Base):
    """``conversation_classification_corrections``：人工纠正分类留痕。

    append-only：PK(tenant, correction_id)；UNIQUE(tenant, message,
    corrected_by, corrected_category) 是 DB 幂等键（同键并发只一行）；不同
    纠正即使同一 corrected_at 也保留多行（评估集样本只增不改）。不存正文/
    凭证/置信度。
    """

    __tablename__ = "conversation_classification_corrections"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "correction_id",
            name="pk_conversation_classification_corrections",
        ),
        UniqueConstraint(
            "tenant_id",
            "message_id",
            "corrected_by",
            "corrected_category",
            name="uq_conversation_classification_corrections_idem",
        ),
        CheckConstraint(
            "corrected_category IN ('clear_interest','willing_to_continue',"
            "'requests_materials','requests_quote','requests_sample',"
            "'provides_specification','no_current_need','future_need_possible',"
            "'refers_other_contact','rejection','unsubscribe','bounce',"
            "'auto_reply','complaint')",
            name="ck_conversation_classification_corrections_category",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    correction_id: Mapped[str] = mapped_column(String(32))
    message_id: Mapped[str] = mapped_column(String(100))
    corrected_category: Mapped[str] = mapped_column(String(40))
    corrected_by: Mapped[str] = mapped_column(String(100))
    corrected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


_DEMAND_SIGNAL_TYPES = (
    "'public_rfq','tender_notice','inbound_inquiry','trade_show_request',"
    "'historical_unclosed_need','supplier_referral','product_line_expansion',"
    "'facility_expansion','new_market_entry','procurement_role_hiring',"
    "'distributor_change','new_certification','large_contract_won',"
    "'funding_or_merger','stockout_observed','negative_product_review',"
    "'supplier_complaint','marketplace_seller_activity','catalog_gap',"
    "'value_chain_adjacency','complementary_category'"
)
_DEMAND_SIGNAL_STATUSES = "'captured','linked_to_hypothesis','discarded'"
_DEMAND_SOURCE_TYPES = (
    "'conversation','web_page','upload','employee_input',"
    "'agent_inference','external_api'"
)


class DemandSignalRow(Base):
    """``demand_signals`` 行（规格 2026-08-16 §5）：模型字段 + Provenance 展开列。"""

    __tablename__ = "demand_signals"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "signal_id", name="pk_demand_signals"),
        UniqueConstraint(
            "tenant_id",
            "entity_name",
            "signal_type",
            "source_type",
            "source_id",
            "discovery_key",
            name="uq_demand_signals_source_identity",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "snapshot_artifact_ref"],
            ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"],
            name="fk_demand_signals_snapshot_artifact",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"signal_type IN ({_DEMAND_SIGNAL_TYPES})", name="ck_demand_signals_type"
        ),
        CheckConstraint(
            f"status IN ({_DEMAND_SIGNAL_STATUSES})", name="ck_demand_signals_status"
        ),
        CheckConstraint(
            f"source_type IN ({_DEMAND_SOURCE_TYPES})",
            name="ck_demand_signals_source_type",
        ),
        CheckConstraint(
            "(confirmed_by IS NULL) = (confirmed_at IS NULL)",
            name="ck_demand_signals_confirmed_pair",
        ),
        CheckConstraint(
            "source_type <> 'web_page' OR "
            "(source_url IS NOT NULL AND btrim(source_url) <> '' AND "
            "page_hash IS NOT NULL AND btrim(page_hash) <> '' AND "
            "page_hash ~ '^[0-9a-f]{64}$' AND "
            "source_id = page_hash AND "
            "snapshot_artifact_ref IS NOT NULL AND "
            "snapshot_artifact_ref ~ '^art_[0-7][0-9A-HJKMNP-TV-Z]{25}$')",
            name="ck_demand_signals_web_evidence",
        ),
        CheckConstraint(
            "(status = 'discarded') = "
            "(discard_reason IS NOT NULL AND btrim(discard_reason) <> '')",
            name="ck_demand_signals_discard_reason",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(signal_id) <> '' AND "
            "btrim(entity_name) <> '' AND btrim(raw_observation) <> '' AND "
            "btrim(source_id) <> '' AND btrim(extracted_by) <> ''",
            name="ck_demand_signals_core_nonblank",
        ),
        CheckConstraint(
            "(possible_need IS NULL OR btrim(possible_need) <> '') AND "
            "(source_url IS NULL OR btrim(source_url) <> '') AND "
            "(page_hash IS NULL OR btrim(page_hash) <> '') AND "
            "(source_type = 'web_page' OR snapshot_artifact_ref IS NULL)",
            name="ck_demand_signals_optional_nonblank",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    signal_id: Mapped[str] = mapped_column(String(32))
    signal_type: Mapped[str] = mapped_column(String(40))
    entity_name: Mapped[str] = mapped_column(String(200))
    raw_observation: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), server_default=text("'captured'"))
    possible_need: Mapped[str | None] = mapped_column(Text)
    account_id: Mapped[str | None] = mapped_column(String(32))
    discard_reason: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(200))
    extracted_by: Mapped[str] = mapped_column(String(64))
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(String(32))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_url: Mapped[str | None] = mapped_column(String(2000))
    page_hash: Mapped[str | None] = mapped_column(String(200))
    snapshot_artifact_ref: Mapped[str | None] = mapped_column(String(40))
    research_evidence: Mapped[dict[str, object] | None] = mapped_column(postgresql.JSONB)
    discovery_key: Mapped[str] = mapped_column(String(64), server_default=text("''"))


class ProspectAccountRow(Base):
    """潜在企业；非空 canonical domain 在租户内唯一。"""

    __tablename__ = "prospect_accounts"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "account_id", name="pk_prospect_accounts"),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(account_id) <> '' AND "
            "btrim(name) <> '' AND btrim(country) <> ''",
            name="ck_prospect_accounts_core_nonblank",
        ),
        CheckConstraint(
            "(website_domain IS NULL OR btrim(website_domain) <> '') AND "
            "(entity_type IS NULL OR btrim(entity_type) <> '') AND "
            "(industry IS NULL OR btrim(industry) <> '') AND "
            "(size_hint IS NULL OR btrim(size_hint) <> '')",
            name="ck_prospect_accounts_optional_nonblank",
        ),
        CheckConstraint(
            "jsonb_typeof(source_signal_refs) = 'array'",
            name="ck_prospect_accounts_source_refs_jsonb",
        ),
        CheckConstraint(
            "jsonb_typeof(field_provenance) = 'object'",
            name="ck_prospect_accounts_field_provenance_jsonb",
        ),
        Index(
            "uq_prospect_accounts_domain",
            "tenant_id",
            "website_domain",
            unique=True,
            postgresql_where=text("website_domain IS NOT NULL"),
        ),
        Index("ix_prospect_accounts_name", "tenant_id", "country", "name"),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    country: Mapped[str] = mapped_column(String(16))
    website_domain: Mapped[str | None] = mapped_column(String(253))
    entity_type: Mapped[str | None] = mapped_column(String(80))
    industry: Mapped[str | None] = mapped_column(String(160))
    size_hint: Mapped[str | None] = mapped_column(String(80))
    source_signal_refs: Mapped[list[str]] = mapped_column(postgresql.JSONB)
    field_provenance: Mapped[dict[str, object]] = mapped_column(
        postgresql.JSONB, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProspectContactRow(Base):
    """潜在联系人；企业外键始终带 tenant。"""

    __tablename__ = "prospect_contacts"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "contact_id", name="pk_prospect_contacts"),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["prospect_accounts.tenant_id", "prospect_accounts.account_id"],
            name="fk_prospect_contacts_account",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(contact_id) <> '' AND "
            "btrim(account_id) <> ''",
            name="ck_prospect_contacts_core_nonblank",
        ),
        CheckConstraint(
            "(full_name IS NULL OR btrim(full_name) <> '') AND "
            "(role_title IS NULL OR btrim(role_title) <> '') AND "
            "(language IS NULL OR btrim(language) <> '')",
            name="ck_prospect_contacts_optional_nonblank",
        ),
        Index(
            "ix_prospect_contacts_account",
            "tenant_id",
            "account_id",
            "created_at",
            "contact_id",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    contact_id: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[str] = mapped_column(String(40))
    full_name: Mapped[str | None] = mapped_column(String(200))
    role_title: Mapped[str | None] = mapped_column(String(200))
    language: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ContactPointRow(Base):
    """联系方式；只有确定性 hash 用于去重，原值绝不进入 outbox。"""

    __tablename__ = "contact_points"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "contact_point_id", name="pk_contact_points"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "contact_id"],
            ["prospect_contacts.tenant_id", "prospect_contacts.contact_id"],
            name="fk_contact_points_contact",
        ),
        UniqueConstraint(
            "tenant_id", "kind", "value_hash", name="uq_contact_points_value_hash"
        ),
        CheckConstraint("kind IN ('email','phone')", name="ck_contact_points_kind"),
        CheckConstraint(
            "verification_status IN ('unverified','verified','risky','invalid')",
            name="ck_contact_points_verification_status",
        ),
        CheckConstraint(
            "value_hash ~ '^[0-9a-f]{64}$'", name="ck_contact_points_value_hash"
        ),
        CheckConstraint(
            "(verification_status = 'verified') = (verified_at IS NOT NULL)",
            name="ck_contact_points_verified_pair",
        ),
        CheckConstraint(
            "(verification_checked_at IS NULL AND "
            "verification_cost_note IS NULL AND "
            "((verification_status = 'unverified' AND "
            "verification_provider IS NULL) OR "
            "(verification_status <> 'unverified' AND "
            "verification_provider IS NOT NULL))) OR "
            "(verification_checked_at IS NOT NULL AND "
            "verification_provider IS NOT NULL AND "
            "verification_cost_note IS NOT NULL)",
            name="ck_contact_points_verification_observation",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(contact_point_id) <> '' AND "
            "btrim(contact_id) <> '' AND btrim(value) <> ''",
            name="ck_contact_points_core_nonblank",
        ),
        CheckConstraint(
            "(verification_provider IS NULL OR btrim(verification_provider) <> '') "
            "AND (verification_cost_note IS NULL OR "
            "btrim(verification_cost_note) <> '') "
            "AND (enrichment_cost_note IS NULL OR btrim(enrichment_cost_note) <> '')",
            name="ck_contact_points_optional_nonblank",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    contact_point_id: Mapped[str] = mapped_column(String(40))
    contact_id: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(16))
    value: Mapped[str] = mapped_column(String(320))
    value_hash: Mapped[str] = mapped_column(String(64))
    verification_status: Mapped[str] = mapped_column(
        String(20), server_default=text("'unverified'")
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_provider: Mapped[str | None] = mapped_column(String(100))
    verification_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    verification_cost_note: Mapped[str | None] = mapped_column(String(200))
    enrichment_cost_note: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ContactLegalBasisRow(Base):
    """联系方式的一对一法律依据留痕。"""

    __tablename__ = "contact_legal_basis"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "contact_point_id", name="pk_contact_legal_basis"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "contact_point_id"],
            ["contact_points.tenant_id", "contact_points.contact_point_id"],
            ondelete="CASCADE",
            name="fk_contact_legal_basis_contact_point",
        ),
        CheckConstraint(
            "basis IN ('legitimate_interest','consent','existing_customer')",
            name="ck_contact_legal_basis_basis",
        ),
        CheckConstraint(
            "subject_type IN ('legal_entity','sole_trader','natural_person')",
            name="ck_contact_legal_basis_subject_type",
        ),
        CheckConstraint(
            "contact_type IN ('role_based','personal_business')",
            name="ck_contact_legal_basis_contact_type",
        ),
        CheckConstraint(
            "basis <> 'legitimate_interest' OR "
            "(assessment_ref IS NOT NULL AND btrim(assessment_ref) <> '')",
            name="ck_contact_legal_basis_li_assessment",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(contact_point_id) <> '' AND "
            "btrim(source) <> ''",
            name="ck_contact_legal_basis_core_nonblank",
        ),
        CheckConstraint(
            "(source_url IS NULL OR btrim(source_url) <> '') AND "
            "(assessment_ref IS NULL OR btrim(assessment_ref) <> '')",
            name="ck_contact_legal_basis_optional_nonblank",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    contact_point_id: Mapped[str] = mapped_column(String(40))
    basis: Mapped[str] = mapped_column(String(32))
    subject_type: Mapped[str] = mapped_column(String(32))
    contact_type: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(100))
    source_url: Mapped[str | None] = mapped_column(String(2000))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    assessment_ref: Mapped[str | None] = mapped_column(String(200))


class ProspectingErasureSuppressionRow(Base):
    """删除请求最小事实；数据库 trigger 强制 append-only。"""

    __tablename__ = "prospecting_erasure_suppressions"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "value_hash", name="pk_prospecting_erasure_suppressions"
        ),
        CheckConstraint(
            "btrim(tenant_id) <> ''", name="ck_prospecting_erasure_tenant_nonblank"
        ),
        CheckConstraint(
            "value_hash ~ '^[0-9a-f]{64}$'",
            name="ck_prospecting_erasure_value_hash",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    value_hash: Mapped[str] = mapped_column(String(64))
    erased_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CommitmentRow(Base):
    """员工和客户承诺；确认前不进入到期扫描。"""

    __tablename__ = "commitments"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "commitment_id", name="pk_commitments"),
        UniqueConstraint(
            "tenant_id",
            "source_message_id",
            "action",
            name="uq_commitments_source_action",
        ),
        CheckConstraint(
            "commitment_type IN ('employee','customer')",
            name="ck_commitments_type",
        ),
        CheckConstraint(
            "status IN ('pending','waiting_customer','fulfilled','overdue','cancelled')",
            name="ck_commitments_status",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(commitment_id) <> '' AND "
            "btrim(owner) <> '' AND btrim(action) <> '' AND "
            "btrim(source_message_id) <> '' AND btrim(verbatim) <> ''",
            name="ck_commitments_core_nonblank",
        ),
        CheckConstraint(
            "(extracted_by IS NULL OR btrim(extracted_by) <> '') AND "
            "char_length(action) <= 4000 AND char_length(verbatim) <= 8000",
            name="ck_commitments_optional_nonblank",
        ),
        CheckConstraint(
            "(confirmed_by IS NULL AND confirmed_at IS NULL) OR "
            "(confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
            name="ck_commitments_confirmation_pair",
        ),
        CheckConstraint(
            "(status = 'fulfilled' AND fulfilled_at IS NOT NULL) OR "
            "(status <> 'fulfilled' AND fulfilled_at IS NULL)",
            name="ck_commitments_fulfilled_pair",
        ),
        Index(
            "ix_commitments_tenant_owner_status_due",
            "tenant_id",
            "owner",
            "status",
            "due_at",
            "commitment_id",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    commitment_id: Mapped[str] = mapped_column(String(40))
    commitment_type: Mapped[str] = mapped_column(String(16))
    owner: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_at_uncertain: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )
    source_message_id: Mapped[str] = mapped_column(String(200))
    verbatim: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), server_default=text("'pending'"))
    account_id: Mapped[str | None] = mapped_column(String(40))
    opportunity_id: Mapped[str | None] = mapped_column(String(40))
    extracted_by: Mapped[str | None] = mapped_column(String(128))
    confirmed_by: Mapped[str | None] = mapped_column(String(40))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkUploadRow(Base):
    """员工上传批次；原始 bytes 只存在 Artifact Store。"""

    __tablename__ = "work_uploads"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "upload_id", name="pk_work_uploads"),
        UniqueConstraint(
            "tenant_id", "artifact_id", name="uq_work_uploads_artifact"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"],
            name="fk_work_uploads_artifact",
        ),
        CheckConstraint(
            "source_kind IN ('chat_transcript','email_text','pdf_text',"
            "'spreadsheet_text','audio_transcript','image_ocr')",
            name="ck_work_uploads_source_kind",
        ),
        CheckConstraint(
            "status IN ('uploaded','extracting','awaiting_confirmation',"
            "'confirmed','failed')",
            name="ck_work_uploads_status",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(upload_id) <> '' AND "
            "btrim(artifact_id) <> '' AND btrim(employee_id) <> '' AND "
            "btrim(customer_timezone) <> ''",
            name="ck_work_uploads_core_nonblank",
        ),
        Index(
            "ix_work_uploads_tenant_employee_created",
            "tenant_id",
            "employee_id",
            "created_at",
            "upload_id",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    upload_id: Mapped[str] = mapped_column(String(40))
    artifact_id: Mapped[str] = mapped_column(String(40))
    employee_id: Mapped[str] = mapped_column(String(40))
    source_kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    customer_timezone: Mapped[str] = mapped_column(String(100))
    account_id: Mapped[str | None] = mapped_column(String(40))
    opportunity_id: Mapped[str | None] = mapped_column(String(40))
    need_id: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExtractedFactRow(Base):
    """Agent 对某次上传的原始结构化提取；只增不改。"""

    __tablename__ = "extracted_facts"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "extraction_id", name="pk_extracted_facts"
        ),
        UniqueConstraint(
            "tenant_id", "upload_id", name="uq_extracted_facts_upload"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "upload_id"],
            ["work_uploads.tenant_id", "work_uploads.upload_id"],
            name="fk_extracted_facts_upload",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_extracted_facts_payload_jsonb",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(extraction_id) <> '' AND "
            "btrim(upload_id) <> '' AND btrim(extracted_by) <> ''",
            name="ck_extracted_facts_core_nonblank",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    extraction_id: Mapped[str] = mapped_column(String(40))
    upload_id: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(postgresql.JSONB)
    extracted_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EmployeeConfirmationRow(Base):
    """员工最终确认版本；与 Agent 原始提取分表且只增不改。"""

    __tablename__ = "employee_confirmations"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "confirmation_id", name="pk_employee_confirmations"
        ),
        UniqueConstraint(
            "tenant_id", "extraction_id", name="uq_employee_confirmations_extraction"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "extraction_id"],
            ["extracted_facts.tenant_id", "extracted_facts.extraction_id"],
            name="fk_employee_confirmations_extraction",
        ),
        CheckConstraint("revision > 0", name="ck_employee_confirmations_revision"),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_employee_confirmations_payload_jsonb",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(confirmation_id) <> '' AND "
            "btrim(extraction_id) <> '' AND btrim(confirmed_by) <> ''",
            name="ck_employee_confirmations_core_nonblank",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    confirmation_id: Mapped[str] = mapped_column(String(40))
    extraction_id: Mapped[str] = mapped_column(String(40))
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(postgresql.JSONB)
    confirmed_by: Mapped[str] = mapped_column(String(40))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CostSheetRow(Base):
    """人工成本表版本；锁定后的不可变性由数据库触发器兜底。"""

    __tablename__ = "cost_sheets"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "cost_sheet_id", name="pk_cost_sheets"),
        UniqueConstraint(
            "tenant_id",
            "opportunity_id",
            "version_type",
            "version_number",
            name="uq_cost_sheets_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "opportunity_id"],
            ["opportunities.tenant_id", "opportunities.opportunity_id"],
            ondelete="RESTRICT",
            name="fk_cost_sheets_opportunity",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_sourcing_case_id"],
            ["sourcing_cases.tenant_id", "sourcing_cases.case_id"],
            ondelete="RESTRICT",
            name="fk_cost_sheets_sourcing_case",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_sourcing_case_id", "source_option_id"],
            [
                "sourcing_supply_options.tenant_id",
                "sourcing_supply_options.case_id",
                "sourcing_supply_options.option_id",
            ],
            ondelete="RESTRICT",
            name="fk_cost_sheets_sourcing_option",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_product_id"],
            ["products.tenant_id", "products.product_id"],
            ondelete="RESTRICT",
            name="fk_cost_sheets_sourcing_product",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "source_sourcing_case_id",
                "source_option_id",
                "source_candidate_id",
            ],
            [
                "sourcing_supply_options.tenant_id",
                "sourcing_supply_options.case_id",
                "sourcing_supply_options.option_id",
                "sourcing_supply_options.supplier_candidate_id",
            ],
            ondelete="RESTRICT",
            name="fk_cost_sheets_sourcing_candidate_path",
        ),
        CheckConstraint(
            "version_type IN ('estimated','quoted','actual')",
            name="ck_cost_sheets_version_type",
        ),
        CheckConstraint(
            "version_number > 0 AND quantity > 0",
            name="ck_cost_sheets_positive_dimensions",
        ),
        CheckConstraint(
            "base_currency ~ '^[A-Z]{3}$' AND quote_currency ~ '^[A-Z]{3}$'",
            name="ck_cost_sheets_currencies",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(cost_sheet_id) <> '' AND "
            "btrim(opportunity_id) <> '' AND "
            "(created_by IS NULL OR btrim(created_by) <> '') AND "
            "(fx_snapshot_id IS NULL OR btrim(fx_snapshot_id) <> '')",
            name="ck_cost_sheets_core_nonblank",
        ),
        CheckConstraint(
            "version_type <> 'quoted' OR fx_snapshot_id IS NOT NULL",
            name="ck_cost_sheets_quoted_fx_snapshot",
        ),
        CheckConstraint(
            "locked_at IS NULL OR locked_at >= created_at",
            name="ck_cost_sheets_locked_at",
        ),
        CheckConstraint(
            "(risk_accepted_by IS NULL AND risk_accepted_at IS NULL AND "
            "risk_justification IS NULL) OR "
            "(risk_accepted_by IS NOT NULL AND risk_accepted_at IS NOT NULL AND "
            "risk_justification IS NOT NULL AND "
            "btrim(risk_accepted_by) <> '' AND btrim(risk_justification) <> '')",
            name="ck_cost_sheets_risk_acceptance",
        ),
        CheckConstraint(
            "(source_sourcing_case_id IS NULL AND source_option_id IS NULL "
            "AND source_product_id IS NULL AND source_candidate_id IS NULL) OR "
            "(source_sourcing_case_id IS NOT NULL AND source_option_id IS NOT NULL "
            "AND source_product_id IS NOT NULL)",
            name="ck_cost_sheets_sourcing_origin",
        ),
        Index(
            "ix_cost_sheets_tenant_opportunity_version",
            "tenant_id",
            "opportunity_id",
            "version_type",
            "version_number",
        ),
        Index(
            "uq_cost_sheets_sourcing_case",
            "tenant_id",
            "source_sourcing_case_id",
            unique=True,
            postgresql_where=text("source_sourcing_case_id IS NOT NULL"),
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    cost_sheet_id: Mapped[str] = mapped_column(String(40))
    opportunity_id: Mapped[str] = mapped_column(String(40))
    version_type: Mapped[str] = mapped_column(String(16))
    version_number: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    base_currency: Mapped[str] = mapped_column(CHAR(3))
    quote_currency: Mapped[str] = mapped_column(CHAR(3))
    fx_snapshot_id: Mapped[str | None] = mapped_column(String(40))
    created_by: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    risk_accepted_by: Mapped[str | None] = mapped_column(String(40))
    risk_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    risk_justification: Mapped[str | None] = mapped_column(Text)
    source_sourcing_case_id: Mapped[str | None] = mapped_column(String(40))
    source_option_id: Mapped[str | None] = mapped_column(String(40))
    source_product_id: Mapped[str | None] = mapped_column(String(40))
    source_candidate_id: Mapped[str | None] = mapped_column(String(40))


class CostItemRow(Base):
    """成本项；序号只标识成本表内的稳定显示顺序。"""

    __tablename__ = "cost_items"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "cost_sheet_id", "item_sequence", name="pk_cost_items"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cost_sheet_id"],
            ["cost_sheets.tenant_id", "cost_sheets.cost_sheet_id"],
            ondelete="CASCADE",
            name="fk_cost_items_sheet",
        ),
        CheckConstraint("item_sequence > 0", name="ck_cost_items_sequence"),
        CheckConstraint("amount >= 0", name="ck_cost_items_amount"),
        CheckConstraint(
            "item_type IN ("
            "'product_purchase','sample_fee','mold_fee','customization_fee',"
            "'logo_printing','packaging','quality_inspection','wastage',"
            "'domestic_freight','international_freight','insurance',"
            "'customs_clearance','duties_and_taxes','destination_freight',"
            "'warehousing','payment_fees','sales_commission',"
            "'customer_acquisition','contact_data_cost','ad_allocation',"
            "'agent_api_allocation','returns_reserve')",
            name="ck_cost_items_item_type",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_cost_items_currency"),
        CheckConstraint(
            "price_basis IN ('indicative','quoted','actual')",
            name="ck_cost_items_price_basis",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(cost_sheet_id) <> '' AND "
            "btrim(item_type) <> '' AND "
            "(source_ref IS NULL OR btrim(source_ref) <> '') AND "
            "(entered_by IS NULL OR btrim(entered_by) <> '')",
            name="ck_cost_items_core_nonblank",
        ),
        CheckConstraint(
            "entered_by IS NULL OR source_ref IS NOT NULL",
            name="ck_cost_items_confirmed_source",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    cost_sheet_id: Mapped[str] = mapped_column(String(40))
    item_sequence: Mapped[int] = mapped_column(Integer)
    item_type: Mapped[str] = mapped_column(String(40))
    amount: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    currency: Mapped[str] = mapped_column(CHAR(3))
    price_basis: Mapped[str] = mapped_column(String(16))
    is_per_unit: Mapped[bool] = mapped_column(Boolean)
    note: Mapped[str | None] = mapped_column(Text)
    source_ref: Mapped[str | None] = mapped_column(String(200))
    entered_by: Mapped[str | None] = mapped_column(String(40))


class CostSheetFxRateRow(Base):
    """成本表绑定的显式直连汇率快照行。"""

    __tablename__ = "cost_sheet_fx_rates"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "cost_sheet_id",
            "base_currency",
            name="pk_cost_sheet_fx_rates",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cost_sheet_id"],
            ["cost_sheets.tenant_id", "cost_sheets.cost_sheet_id"],
            ondelete="CASCADE",
            name="fk_cost_sheet_fx_rates_sheet",
        ),
        CheckConstraint("rate > 0", name="ck_cost_sheet_fx_rates_rate"),
        CheckConstraint(
            "base_currency ~ '^[A-Z]{3}$' AND quote_currency ~ '^[A-Z]{3}$'",
            name="ck_cost_sheet_fx_rates_currencies",
        ),
        CheckConstraint(
            "base_currency <> quote_currency OR rate = 1",
            name="ck_cost_sheet_fx_rates_identity",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(cost_sheet_id) <> '' AND "
            "btrim(source) <> ''",
            name="ck_cost_sheet_fx_rates_core_nonblank",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    cost_sheet_id: Mapped[str] = mapped_column(String(40))
    base_currency: Mapped[str] = mapped_column(CHAR(3))
    quote_currency: Mapped[str] = mapped_column(CHAR(3))
    rate: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(200))


class MarginRuleRow(Base):
    """显式利润规则历史；没有记录时业务层必须 fail closed。"""

    __tablename__ = "margin_rules"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "margin_rule_id", name="pk_margin_rules"
        ),
        CheckConstraint(
            "minimum_margin_rate >= 0 AND "
            "minimum_margin_rate <= target_margin_rate AND "
            "target_margin_rate < 1",
            name="ck_margin_rules_rates",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(margin_rule_id) <> '' AND "
            "(category IS NULL OR btrim(category) <> '')",
            name="ck_margin_rules_core_nonblank",
        ),
        Index(
            "ix_margin_rules_tenant_category_effective",
            "tenant_id",
            "category",
            "effective_from",
            "margin_rule_id",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    margin_rule_id: Mapped[str] = mapped_column(String(40))
    category: Mapped[str | None] = mapped_column(String(200))
    minimum_margin_rate: Mapped[Decimal] = mapped_column(Numeric(18, 12))
    target_margin_rate: Mapped[Decimal] = mapped_column(Numeric(18, 12))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CompanyPlaybookVersionRow(Base):
    """不可变 Company Playbook 候选版本。"""

    __tablename__ = "company_playbook_versions"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "playbook_version_id",
            name="pk_company_playbook_versions",
        ),
        UniqueConstraint(
            "tenant_id",
            "version_number",
            name="uq_company_playbook_versions_number",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_company_playbook_versions_idempotency",
        ),
        CheckConstraint(
            "version_number > 0 AND minimum_deal_amount >= 0 AND "
            "(monthly_budget_credits IS NULL OR monthly_budget_credits >= 0)",
            name="ck_company_playbook_versions_nonnegative",
        ),
        CheckConstraint(
            "minimum_deal_currency ~ '^[A-Z]{3}$'",
            name="ck_company_playbook_versions_currency",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$' AND "
            "(base_content_hash IS NULL OR base_content_hash ~ '^[0-9a-f]{64}$')",
            name="ck_company_playbook_versions_hashes",
        ),
        CheckConstraint(
            "(base_version_id IS NULL AND base_content_hash IS NULL) OR "
            "(base_version_id IS NOT NULL AND base_content_hash IS NOT NULL)",
            name="ck_company_playbook_versions_base_pair",
        ),
        CheckConstraint(
            "jsonb_typeof(excluded_categories) = 'array' AND "
            "jsonb_array_length(excluded_categories) <= 200 AND "
            "jsonb_typeof(sourcing_regions) = 'array' AND "
            "jsonb_array_length(sourcing_regions) <= 200 AND "
            "jsonb_typeof(excluded_countries) = 'array' AND "
            "jsonb_array_length(excluded_countries) <= 200 AND "
            "jsonb_typeof(approval_requirements) = 'array' AND "
            "jsonb_array_length(approval_requirements) <= 200",
            name="ck_company_playbook_versions_json_arrays",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(playbook_version_id) <> '' AND "
            "btrim(company_type) <> '' AND btrim(proposed_by) <> '' AND "
            "btrim(idempotency_key) <> '' AND "
            "(supply_capabilities_note IS NULL OR "
            "btrim(supply_capabilities_note) <> '')",
            name="ck_company_playbook_versions_core_nonblank",
        ),
        CheckConstraint(
            "source_type = 'employee_input' AND "
            "source_id = playbook_version_id AND "
            "extracted_by = 'human:' || proposed_by AND "
            "extracted_at = proposed_at",
            name="ck_company_playbook_versions_provenance",
        ),
        Index(
            "ix_company_playbook_versions_tenant_number",
            "tenant_id",
            "version_number",
            "playbook_version_id",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    playbook_version_id: Mapped[str] = mapped_column(String(40))
    version_number: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(CHAR(64))
    base_version_id: Mapped[str | None] = mapped_column(String(40))
    base_content_hash: Mapped[str | None] = mapped_column(CHAR(64))
    company_type: Mapped[str] = mapped_column(String(200))
    minimum_deal_amount: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    minimum_deal_currency: Mapped[str] = mapped_column(CHAR(3))
    excluded_categories: Mapped[list[str]] = mapped_column(postgresql.JSONB)
    sourcing_regions: Mapped[list[str]] = mapped_column(postgresql.JSONB)
    excluded_countries: Mapped[list[str]] = mapped_column(postgresql.JSONB)
    monthly_budget_credits: Mapped[int | None] = mapped_column(BigInteger)
    approval_requirements: Mapped[list[str]] = mapped_column(postgresql.JSONB)
    supply_capabilities_note: Mapped[str | None] = mapped_column(Text)
    proposed_by: Mapped[str] = mapped_column(String(40))
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(40))
    extracted_by: Mapped[str] = mapped_column(String(200))
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CompanyPlaybookActivationRow(Base):
    """人工批准与系统生效时间分离的 append-only 激活事实。"""

    __tablename__ = "company_playbook_activations"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "activation_id",
            name="pk_company_playbook_activations",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "playbook_version_id"],
            [
                "company_playbook_versions.tenant_id",
                "company_playbook_versions.playbook_version_id",
            ],
            ondelete="RESTRICT",
            name="fk_company_playbook_activations_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "playbook_version_id",
            name="uq_company_playbook_activations_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "approval_id",
            name="uq_company_playbook_activations_approval",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_company_playbook_activations_hash",
        ),
        CheckConstraint(
            "change_set_ref = 'playbook:' || playbook_version_id || ':' || "
            "content_hash",
            name="ck_company_playbook_activations_change_set",
        ),
        CheckConstraint(
            "approved_at <= activated_at",
            name="ck_company_playbook_activations_times",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(activation_id) <> '' AND "
            "btrim(playbook_version_id) <> '' AND btrim(approval_id) <> '' AND "
            "btrim(change_set_ref) <> '' AND btrim(approved_by) <> '' AND "
            "btrim(activated_by) <> ''",
            name="ck_company_playbook_activations_core_nonblank",
        ),
        Index(
            "ix_company_playbook_activations_tenant_current",
            "tenant_id",
            "activated_at",
            "activation_id",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    activation_id: Mapped[str] = mapped_column(String(40))
    playbook_version_id: Mapped[str] = mapped_column(String(40))
    content_hash: Mapped[str] = mapped_column(CHAR(64))
    approval_id: Mapped[str] = mapped_column(String(40))
    change_set_ref: Mapped[str] = mapped_column(String(160))
    approved_by: Mapped[str] = mapped_column(String(40))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    activated_by: Mapped[str] = mapped_column(String(200))
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CountryPolicyVersionRow(Base):
    """不可变国家政策候选版本；字段来源保存在独立关系表。"""

    __tablename__ = "country_policy_versions"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "country_policy_version_id",
            name="pk_country_policy_versions",
        ),
        UniqueConstraint(
            "tenant_id",
            "country_key",
            "version_number",
            name="uq_country_policy_versions_country_number",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_country_policy_versions_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "country_policy_version_id",
            "country_key",
            "content_hash",
            name="uq_country_policy_versions_activation_target",
        ),
        CheckConstraint(
            "version_number > 0 AND "
            "(opt_out_deadline_days IS NULL OR "
            "opt_out_deadline_days BETWEEN 1 AND 365)",
            name="ck_country_policy_versions_bounds",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$' AND "
            "(base_content_hash IS NULL OR "
            "base_content_hash ~ '^[0-9a-f]{64}$')",
            name="ck_country_policy_versions_hashes",
        ),
        CheckConstraint(
            "(base_version_id IS NULL AND base_content_hash IS NULL) OR "
            "(base_version_id IS NOT NULL AND base_content_hash IS NOT NULL)",
            name="ck_country_policy_versions_base_pair",
        ),
        CheckConstraint(
            "jsonb_typeof(requirements) = 'array' AND "
            "jsonb_array_length(requirements) <= 100",
            name="ck_country_policy_versions_requirements",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND "
            "btrim(country_policy_version_id) <> '' AND "
            "btrim(country) <> '' AND btrim(country_key) <> '' AND "
            "btrim(notes) <> '' AND btrim(proposed_by) <> '' AND "
            "btrim(idempotency_key) <> ''",
            name="ck_country_policy_versions_core_nonblank",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    country_policy_version_id: Mapped[str] = mapped_column(String(40))
    country: Mapped[str] = mapped_column(String(64))
    country_key: Mapped[str] = mapped_column(String(64))
    version_number: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(CHAR(64))
    base_version_id: Mapped[str | None] = mapped_column(String(40))
    base_content_hash: Mapped[str | None] = mapped_column(CHAR(64))
    public_research_allowed: Mapped[bool] = mapped_column(Boolean)
    contact_enrichment_allowed: Mapped[bool] = mapped_column(Boolean)
    cold_b2b_email_allowed: Mapped[bool] = mapped_column(Boolean)
    personal_data_basis_required: Mapped[bool] = mapped_column(Boolean)
    subject_type_affects_judgment: Mapped[bool] = mapped_column(Boolean)
    contact_type_affects_judgment: Mapped[bool] = mapped_column(Boolean)
    opt_out_deadline_days: Mapped[int | None] = mapped_column(Integer)
    local_representative_required: Mapped[bool] = mapped_column(Boolean)
    requirements: Mapped[list[str]] = mapped_column(postgresql.JSONB)
    notes: Mapped[str] = mapped_column(Text)
    proposed_by: Mapped[str] = mapped_column(String(40))
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(200))


class CountryPolicyFieldProvenanceRow(Base):
    """国家政策决策字段逐字段、人工确认的关系型 Provenance。"""

    __tablename__ = "country_policy_field_provenance"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "country_policy_version_id",
            "field_name",
            name="pk_country_policy_field_provenance",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "country_policy_version_id"],
            [
                "country_policy_versions.tenant_id",
                "country_policy_versions.country_policy_version_id",
            ],
            ondelete="RESTRICT",
            name="fk_country_policy_field_provenance_version",
        ),
        CheckConstraint(
            "field_name IN ("
            "'public_research_allowed','contact_enrichment_allowed',"
            "'cold_b2b_email_allowed','personal_data_basis_required',"
            "'subject_type_affects_judgment','contact_type_affects_judgment',"
            "'opt_out_deadline_days','local_representative_required',"
            "'requirements')",
            name="ck_country_policy_field_provenance_field",
        ),
        CheckConstraint(
            "source_type IN ('web_page','upload','employee_input') AND "
            "source_type <> 'agent_inference'",
            name="ck_country_policy_field_provenance_source",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND "
            "btrim(country_policy_version_id) <> '' AND "
            "btrim(field_name) <> '' AND btrim(source_id) <> '' AND "
            "btrim(extracted_by) <> '' AND btrim(confirmed_by) <> '' AND "
            "extracted_by = 'human:' || confirmed_by AND "
            "confirmed_at = extracted_at",
            name="ck_country_policy_field_provenance_human_confirmed",
        ),
        CheckConstraint(
            "(source_type = 'web_page' AND source_url IS NOT NULL AND "
            "btrim(source_url) <> '' AND page_hash ~ '^[0-9a-f]{64}$') OR "
            "(source_type <> 'web_page' AND source_url IS NULL AND "
            "page_hash IS NULL)",
            name="ck_country_policy_field_provenance_web_shape",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    country_policy_version_id: Mapped[str] = mapped_column(String(40))
    field_name: Mapped[str] = mapped_column(String(64))
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(200))
    extracted_by: Mapped[str] = mapped_column(String(200))
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str] = mapped_column(String(40))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    page_hash: Mapped[str | None] = mapped_column(CHAR(64))


class CountryPolicyActivationRow(Base):
    """人工批准与系统应用分离、按国家单调排序的激活事实。"""

    __tablename__ = "country_policy_activations"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "country_policy_activation_id",
            name="pk_country_policy_activations",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "country_policy_version_id",
                "country_key",
                "content_hash",
            ],
            [
                "country_policy_versions.tenant_id",
                "country_policy_versions.country_policy_version_id",
                "country_policy_versions.country_key",
                "country_policy_versions.content_hash",
            ],
            ondelete="RESTRICT",
            name="fk_country_policy_activations_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "country_key",
            "activation_sequence",
            name="uq_country_policy_activations_country_sequence",
        ),
        UniqueConstraint(
            "tenant_id",
            "country_policy_version_id",
            name="uq_country_policy_activations_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "approval_id",
            name="uq_country_policy_activations_approval",
        ),
        CheckConstraint(
            "activation_sequence > 0 AND "
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_country_policy_activations_sequence_hash",
        ),
        CheckConstraint(
            "change_set_ref = 'country_policy:' || "
            "country_policy_version_id || ':' || content_hash",
            name="ck_country_policy_activations_change_set",
        ),
        CheckConstraint(
            "approved_at <= activated_at",
            name="ck_country_policy_activations_times",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND "
            "btrim(country_policy_activation_id) <> '' AND "
            "btrim(country_key) <> '' AND "
            "btrim(country_policy_version_id) <> '' AND "
            "btrim(approval_id) <> '' AND btrim(change_set_ref) <> '' AND "
            "btrim(approved_by) <> '' AND btrim(activated_by) <> ''",
            name="ck_country_policy_activations_core_nonblank",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    country_policy_activation_id: Mapped[str] = mapped_column(String(40))
    country_key: Mapped[str] = mapped_column(String(64))
    activation_sequence: Mapped[int] = mapped_column(Integer)
    country_policy_version_id: Mapped[str] = mapped_column(String(40))
    content_hash: Mapped[str] = mapped_column(CHAR(64))
    approval_id: Mapped[str] = mapped_column(String(40))
    change_set_ref: Mapped[str] = mapped_column(String(160))
    approved_by: Mapped[str] = mapped_column(String(40))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    activated_by: Mapped[str] = mapped_column(String(200))
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProviderReadinessEventRow(Base):
    """tenant-scoped Provider readiness append-only 事件事实。"""

    __tablename__ = "provider_readiness_events"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "provider_readiness_event_id",
            name="pk_provider_readiness_events",
        ),
        UniqueConstraint(
            "tenant_id",
            "provider",
            "capability_set",
            "sequence",
            name="uq_provider_readiness_events_stream_sequence",
        ),
        UniqueConstraint(
            "tenant_id",
            "provider",
            "capability_set",
            "idempotency_key",
            name="uq_provider_readiness_events_stream_idempotency",
        ),
        CheckConstraint(
            "sequence > 0",
            name="ck_provider_readiness_events_sequence",
        ),
        CheckConstraint(
            "event_type IN ("
            "'configured','validation_started','validation_passed',"
            "'validation_failed','runtime_composed')",
            name="ck_provider_readiness_events_type",
        ),
        CheckConstraint(
            "provider ~ '^[a-z][a-z0-9._-]{0,31}$' AND "
            "configuration_version ~ '^[a-z0-9][a-z0-9._-]{0,31}$' AND "
            "connector_profile_version ~ '^[a-z0-9][a-z0-9._-]{0,31}$' AND "
            "transport_profile ~ '^[a-z0-9][a-z0-9._-]{0,63}$' AND "
            "api_key_version ~ '^[a-z0-9][a-z0-9._-]{0,31}$'",
            name="ck_provider_readiness_events_lowercase_labels",
        ),
        CheckConstraint(
            "configuration_hash ~ '^[0-9a-f]{64}$'",
            name="ck_provider_readiness_events_hash",
        ),
        CheckConstraint(
            "cardinality(capability_set) = 2 AND capability_set = "
            "ARRAY['contact.enrich','contact.verify']::varchar(64)[]",
            name="ck_provider_readiness_events_capabilities",
        ),
        CheckConstraint(
            "outcome_code IS NULL OR outcome_code IN ("
            "'auth_required','rate_limited','provider_transient',"
            "'provider_permanent','response_invalid',"
            "'reconciliation_required')",
            name="ck_provider_readiness_events_outcome",
        ),
        CheckConstraint(
            "(event_type = 'configured' AND validation_key IS NULL AND "
            "outcome_code IS NULL AND evidence_ref IS NULL) OR "
            "(event_type = 'validation_started' AND validation_key IS NOT NULL "
            "AND outcome_code IS NULL AND evidence_ref IS NULL) OR "
            "(event_type = 'validation_passed' AND validation_key IS NOT NULL "
            "AND outcome_code IS NULL AND evidence_ref IS NOT NULL) OR "
            "(event_type = 'validation_failed' AND validation_key IS NOT NULL "
            "AND outcome_code IS NOT NULL AND evidence_ref IS NULL) OR "
            "(event_type = 'runtime_composed' AND validation_key IS NULL AND "
            "outcome_code IS NULL AND evidence_ref IS NULL)",
            name="ck_provider_readiness_events_event_fields",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND "
            "provider_readiness_event_id ~ '^pre_[0-7][0-9A-HJKMNP-TV-Z]{25}$' "
            "AND btrim(actor_id) <> '' AND btrim(idempotency_key) <> '' AND "
            "(validation_key IS NULL OR btrim(validation_key) <> '') AND "
            "(evidence_ref IS NULL OR btrim(evidence_ref) <> '')",
            name="ck_provider_readiness_events_core",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    provider_readiness_event_id: Mapped[str] = mapped_column(String(30))
    provider: Mapped[str] = mapped_column(String(32))
    capability_set: Mapped[list[str]] = mapped_column(
        postgresql.ARRAY(String(64))
    )
    sequence: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(32))
    configuration_version: Mapped[str] = mapped_column(String(32))
    configuration_hash: Mapped[str] = mapped_column(CHAR(64))
    connector_profile_version: Mapped[str] = mapped_column(String(32))
    transport_profile: Mapped[str] = mapped_column(String(64))
    api_key_version: Mapped[str] = mapped_column(String(32))
    validation_key: Mapped[str | None] = mapped_column(String(200))
    outcome_code: Mapped[str | None] = mapped_column(String(64))
    evidence_ref: Mapped[str | None] = mapped_column(String(200))
    actor_id: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(200))


class CostScopeConfirmationRow(Base):
    """人工适用性确认，只增且绑定完整Need。"""

    __tablename__ = "cost_scope_confirmations"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id","confirmation_id",name="pk_cost_scope_confirmations"),
        UniqueConstraint("tenant_id","idempotency_key",name="uq_cost_scope_confirmations_key"),
        ForeignKeyConstraint(["tenant_id","cost_sheet_id"],["cost_sheets.tenant_id","cost_sheets.cost_sheet_id"],name="fk_cost_scope_confirmations_cost_sheet_id",ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id","coverage_id"],["costing_coverage.tenant_id","costing_coverage.coverage_id"],name="fk_cost_scope_confirmations_coverage_id",ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id","need_id"],["validated_needs.tenant_id","validated_needs.need_id"],name="fk_cost_scope_confirmations_need_id",ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id","confirmed_by"],["employees.tenant_id","employees.employee_id"],name="fk_cost_scope_confirmations_confirmed_by",ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id","opportunity_id"],["opportunities.tenant_id","opportunities.opportunity_id"],name="fk_cost_scope_confirmations_opportunity_id",ondelete="RESTRICT"),
        CheckConstraint("jsonb_typeof(payload)='object'",name="ck_cost_scope_confirmations_json"),
        CheckConstraint("isfinite(confirmed_at)",name="ck_cost_scope_confirmations_time"),
        CheckConstraint(" AND ".join(f"{name} ~ '^[0-9a-f]{{64}}$'" for name in (
            "request_hash","content_hash","sheet_hash","need_facts_hash","specification_hash","terms_hash")),name="ck_cost_scope_confirmations_hash"),
        CheckConstraint("idempotency_key=btrim(idempotency_key) AND length(idempotency_key)>0 AND idempotency_key !~ '[[:cntrl:]]'",name="ck_cost_scope_confirmations_key"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    confirmation_id: Mapped[str] = mapped_column(String(40))
    cost_sheet_id: Mapped[str] = mapped_column(String(40))
    opportunity_id: Mapped[str] = mapped_column(String(40))
    need_id: Mapped[str] = mapped_column(String(40))
    coverage_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    sheet_hash: Mapped[str] = mapped_column(String(64))
    need_facts_hash: Mapped[str] = mapped_column(String(64))
    specification_hash: Mapped[str] = mapped_column(String(64))
    terms_hash: Mapped[str] = mapped_column(String(64))
    confirmed_by: Mapped[str] = mapped_column(String(40))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, object]] = mapped_column(postgresql.JSONB)


class CostingQuoteBasisRow(Base):
    """冻结依据与操作双向延迟FK，内容不可改。"""

    __tablename__ = "costing_quote_bases"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id","basis_id",name="pk_costing_quote_bases"),
        UniqueConstraint("tenant_id","operation_id",name="uq_costing_quote_bases_operation"),
        ForeignKeyConstraint(["tenant_id","scope_confirmation_id"],["cost_scope_confirmations.tenant_id","cost_scope_confirmations.confirmation_id"],name="fk_costing_quote_bases_scope_confirmation_id",ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id","cost_sheet_id"],["cost_sheets.tenant_id","cost_sheets.cost_sheet_id"],name="fk_costing_quote_bases_cost_sheet_id",ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id","policy_id"],["costing_policies.tenant_id","costing_policies.policy_id"],name="fk_costing_quote_bases_policy_id",ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id","opportunity_id"],["opportunities.tenant_id","opportunities.opportunity_id"],name="fk_costing_quote_bases_opportunity_id",ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id","operation_id"],["quote_creation_operations.tenant_id","quote_creation_operations.operation_id"],name="fk_costing_quote_bases_operation_id",ondelete="RESTRICT",deferrable=True,initially="DEFERRED",use_alter=True),
        CheckConstraint("jsonb_typeof(payload)='object'",name="ck_costing_quote_bases_json"),
        CheckConstraint(" AND ".join(f"{name} ~ '^[0-9a-f]{{64}}$'" for name in (
            "request_hash","context_hash","sheet_hash","basis_hash")),name="ck_costing_quote_bases_hash"),
        CheckConstraint("isfinite(valid_until) AND isfinite(frozen_at) AND valid_until>frozen_at",name="ck_costing_quote_bases_time"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    basis_id: Mapped[str] = mapped_column(String(40))
    operation_id: Mapped[str] = mapped_column(String(40))
    cost_sheet_id: Mapped[str] = mapped_column(String(40))
    opportunity_id: Mapped[str] = mapped_column(String(40))
    scope_confirmation_id: Mapped[str] = mapped_column(String(40))
    request_hash: Mapped[str] = mapped_column(String(64))
    context_hash: Mapped[str] = mapped_column(String(64))
    sheet_hash: Mapped[str] = mapped_column(String(64))
    basis_hash: Mapped[str] = mapped_column(String(64))
    policy_id: Mapped[str] = mapped_column(String(64))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, object]] = mapped_column(postgresql.JSONB)


class QuoteCreationOperationRow(Base):
    """同成本表仅一个pending；完成后可用显式修订新建记录。"""

    __tablename__ = "quote_creation_operations"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id","operation_id",name="pk_quote_creation_operations"),
        UniqueConstraint("tenant_id","idempotency_key",name="uq_quote_creation_operations_key"),
        ForeignKeyConstraint(["tenant_id","cost_sheet_id"],["cost_sheets.tenant_id","cost_sheets.cost_sheet_id"],name="fk_quote_creation_operations_cost_sheet_id",ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id","basis_id"],["costing_quote_bases.tenant_id","costing_quote_bases.basis_id"],name="fk_quote_creation_operations_basis_id",ondelete="RESTRICT",deferrable=True,initially="DEFERRED"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'",name="ck_quote_creation_operations_hash"),
        CheckConstraint("isfinite(created_at) AND (completed_at IS NULL OR isfinite(completed_at))",name="ck_quote_creation_operations_time"),
        CheckConstraint("jsonb_typeof(intent)='object' AND (completion IS NULL OR jsonb_typeof(completion)='object')",name="ck_quote_creation_operations_json"),
        CheckConstraint("(state='frozen' AND completion IS NULL AND completed_at IS NULL) OR (state='completed' AND completion IS NOT NULL AND completed_at IS NOT NULL AND completed_at>=created_at)",name="ck_quote_creation_operations_state"),
        CheckConstraint("idempotency_key=btrim(idempotency_key) AND length(idempotency_key)>0 AND idempotency_key !~ '[[:cntrl:]]'",name="ck_quote_creation_operations_key"),
        Index("uq_quote_creation_operations_pending","tenant_id","cost_sheet_id",unique=True,postgresql_where=text("state='frozen'")),
        Index("ix_quote_creation_operations_sheet","tenant_id","cost_sheet_id"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    operation_id: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    cost_sheet_id: Mapped[str] = mapped_column(String(40))
    basis_id: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    intent: Mapped[dict[str, object]] = mapped_column(postgresql.JSONB)
    completion: Mapped[dict[str, object] | None] = mapped_column(postgresql.JSONB(none_as_null=True),nullable=True)


class QuotationIssuerRow(Base):
    """quotation_issuers只增或受审计状态持久映射。"""

    __tablename__ = "quotation_issuers"
    __table_args__ = (
        PrimaryKeyConstraint('tenant_id','issuer_id',name='pk_quotation_issuers'),
        UniqueConstraint('tenant_id','version',name='uq_quotation_issuers_version'),
        UniqueConstraint('tenant_id','idempotency_key',name='uq_quotation_issuers_key'),
        ForeignKeyConstraint(['tenant_id','confirmed_by'],['employees.tenant_id','employees.employee_id'],name='fk_quotation_issuers_employee',ondelete='RESTRICT'),
        CheckConstraint("version>0 AND idempotency_key=btrim(idempotency_key) AND length(idempotency_key)>0 AND idempotency_key !~ '[[:cntrl:]]'",name='ck_quotation_issuers_input'),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'",name='ck_quotation_issuers_hash'),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    issuer_id: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    confirmed_by: Mapped[str] = mapped_column(String(40))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, object]] = mapped_column(postgresql.JSONB)


class QuotationRow(Base):
    """quotations只增或受审计状态持久映射。"""

    __tablename__ = "quotations"
    __table_args__ = (
        PrimaryKeyConstraint('tenant_id','quote_id',name='pk_quotations'),
        UniqueConstraint('tenant_id','opportunity_id','version',name='uq_quotations_version'),
        UniqueConstraint('tenant_id','operation_id',name='uq_quotations_operation'),
        ForeignKeyConstraint(['tenant_id','opportunity_id'],['opportunities.tenant_id','opportunities.opportunity_id'],name='fk_quotations_opportunity_id',ondelete='RESTRICT'),
        ForeignKeyConstraint(['tenant_id','operation_id'],['quote_creation_operations.tenant_id','quote_creation_operations.operation_id'],name='fk_quotations_operation_id',ondelete='RESTRICT'),
        ForeignKeyConstraint(['tenant_id','basis_id'],['costing_quote_bases.tenant_id','costing_quote_bases.basis_id'],name='fk_quotations_basis_id',ondelete='RESTRICT'),
        ForeignKeyConstraint(['tenant_id','cost_sheet_id'],['cost_sheets.tenant_id','cost_sheets.cost_sheet_id'],name='fk_quotations_cost_sheet_id',ondelete='RESTRICT'),
        ForeignKeyConstraint(['tenant_id','issuer_id'],['quotation_issuers.tenant_id','quotation_issuers.issuer_id'],name='fk_quotations_issuer_id',ondelete='RESTRICT'),
        ForeignKeyConstraint(['tenant_id','prepared_by'],['employees.tenant_id','employees.employee_id'],name='fk_quotations_prepared_by',ondelete='RESTRICT'),
        ForeignKeyConstraint(['tenant_id','owner_id'],['employees.tenant_id','employees.employee_id'],name='fk_quotations_owner_id',ondelete='RESTRICT'),
        ForeignKeyConstraint(['tenant_id','replaces_quote_id'],['quotations.tenant_id','quotations.quote_id'],name='fk_quotations_replaces_quote_id',ondelete='RESTRICT'),
        CheckConstraint("version>0 AND isfinite(valid_until) AND isfinite(created_at) AND valid_until>created_at",name='ck_quotations_time'),
        CheckConstraint("(replaces_quote_id IS NULL AND replaced_quote_version IS NULL) OR (replaces_quote_id IS NOT NULL AND replaced_quote_version>0)",name='ck_quotations_revision'),
        CheckConstraint("state IN ('draft','pending_approval','approved','sent','accepted','rejected','expired','superseded')",name='ck_quotations_state'),
        Index('uq_quotations_active','tenant_id','opportunity_id',unique=True,postgresql_where=text("state IN ('draft','pending_approval','approved','sent')")),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'",name='ck_quotations_hash'),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    quote_id: Mapped[str] = mapped_column(String(40))
    opportunity_id: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20))
    operation_id: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    basis_id: Mapped[str] = mapped_column(String(64))
    cost_sheet_id: Mapped[str] = mapped_column(String(40))
    issuer_id: Mapped[str] = mapped_column(String(40))
    content_hash: Mapped[str] = mapped_column(String(64))
    prepared_by: Mapped[str] = mapped_column(String(40))
    owner_id: Mapped[str] = mapped_column(String(40))
    replaces_quote_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    replaced_quote_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content: Mapped[dict[str, object]] = mapped_column(postgresql.JSONB)


class QuotationLineRow(Base):
    """quotation_lines只增或受审计状态持久映射。"""

    __tablename__ = "quotation_lines"
    __table_args__ = (
        PrimaryKeyConstraint('tenant_id','quote_id','line_number',name='pk_quotation_lines'),
        CheckConstraint('line_number=1',name='ck_quotation_lines_one'),
        ForeignKeyConstraint(['tenant_id','quote_id'],['quotations.tenant_id','quotations.quote_id'],name='fk_quotation_lines_quote',ondelete='RESTRICT'),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    quote_id: Mapped[str] = mapped_column(String(40))
    line_number: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, object]] = mapped_column(postgresql.JSONB)


class QuotationEvidenceRefRow(Base):
    """quotation_evidence_refs只增或受审计状态持久映射。"""

    __tablename__ = "quotation_evidence_refs"
    __table_args__ = (
        PrimaryKeyConstraint('tenant_id','quote_id','evidence_id',name='pk_quotation_evidence_refs'),
        ForeignKeyConstraint(['tenant_id','evidence_id'],['costing_price_evidence.tenant_id','costing_price_evidence.evidence_id'],name='fk_quotation_evidence_refs_evidence',ondelete='RESTRICT'),
        ForeignKeyConstraint(['tenant_id','quote_id'],['quotations.tenant_id','quotations.quote_id'],name='fk_quotation_evidence_refs_quote',ondelete='RESTRICT'),
        CheckConstraint("evidence_hash ~ '^[0-9a-f]{64}$'",name='ck_quotation_evidence_refs_hash'),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    quote_id: Mapped[str] = mapped_column(String(40))
    evidence_id: Mapped[str] = mapped_column(String(64))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))


class QuotationStateEventRow(Base):
    """quotation_state_events只增或受审计状态持久映射。"""

    __tablename__ = "quotation_state_events"
    __table_args__ = (
        PrimaryKeyConstraint('tenant_id','event_id',name='pk_quotation_state_events'),
        ForeignKeyConstraint(['tenant_id','actor_id'],['employees.tenant_id','employees.employee_id'],name='fk_quotation_state_events_employee',ondelete='RESTRICT'),
        UniqueConstraint('tenant_id','quote_id','to_state',name='uq_quotation_state_events_target'),
        ForeignKeyConstraint(['tenant_id','quote_id'],['quotations.tenant_id','quotations.quote_id'],name='fk_quotation_state_events_quote',ondelete='RESTRICT'),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    event_id: Mapped[str] = mapped_column(String(40))
    quote_id: Mapped[str] = mapped_column(String(40))
    from_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_state: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reason: Mapped[str] = mapped_column(String(32))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reference_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class QuotationApprovalBindingRow(Base):
    """quotation_approval_bindings只增或受审计状态持久映射。"""

    __tablename__ = "quotation_approval_bindings"
    __table_args__ = (
        PrimaryKeyConstraint('tenant_id','quote_id','approval_type','approval_id',name='pk_quotation_approval_bindings'),
        ForeignKeyConstraint(['tenant_id','approval_id'],['approval_packages.tenant_id','approval_packages.approval_id'],name='fk_quotation_approval_bindings_approval',ondelete='RESTRICT'),
        ForeignKeyConstraint(['tenant_id','quote_id'],['quotations.tenant_id','quotations.quote_id'],name='fk_quotation_approval_bindings_quote',ondelete='RESTRICT'),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'",name='ck_quotation_approval_bindings_hash'),
        UniqueConstraint("tenant_id", "quote_id", "approval_type", name="uq_quotation_approval_type"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    quote_id: Mapped[str] = mapped_column(String(40))
    approval_type: Mapped[str] = mapped_column(String(64))
    approval_id: Mapped[str] = mapped_column(String(40))
    quote_version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_hash: Mapped[str] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64))
    fact: Mapped[dict] = mapped_column(postgresql.JSONB)


class QuotationApprovalReceiptRow(Base):
    """一次报价成功批准的不可变事实，不从审批APPLIED反推。"""

    __tablename__ = "quotation_approval_receipts"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "quote_id", name="pk_quotation_approval_receipts"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "quote_id"],
            ["quotations.tenant_id", "quotations.quote_id"],
            name="fk_quote_approval_receipt_quote",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "quote_send_decider"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_quote_approval_receipt_decider",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "approval_run_id"],
            ["workflow_runs.tenant_id", "workflow_runs.run_id"],
            name="fk_quote_approval_receipt_run",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "quote_version>0 AND content_hash ~ '^[0-9a-f]{64}$' AND facts_hash ~ '^[0-9a-f]{64}$' AND jsonb_typeof(decisions)='array'",
            name="ck_quote_approval_receipt_shape",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    quote_id: Mapped[str] = mapped_column(String(40))
    quote_version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    facts_hash: Mapped[str] = mapped_column(String(64))
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    quote_send_decider: Mapped[str] = mapped_column(String(40))
    approval_run_id: Mapped[str] = mapped_column(String(40))
    decisions: Mapped[list] = mapped_column(postgresql.JSONB)


class QuotationSendReceiptRow(Base):
    """quotation_send_receipts只增或受审计状态持久映射。"""

    __tablename__ = "quotation_send_receipts"
    __table_args__ = (
        PrimaryKeyConstraint('tenant_id','attempt_id',name='pk_quotation_send_receipts'),
        ForeignKeyConstraint(['tenant_id','attempt_id'],['outreach_message_attempts.tenant_id','outreach_message_attempts.attempt_id'],name='fk_quotation_send_receipts_attempt',ondelete='RESTRICT'),
        ForeignKeyConstraint(['tenant_id','quote_id'],['quotations.tenant_id','quotations.quote_id'],name='fk_quotation_send_receipts_quote',ondelete='RESTRICT'),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'",name='ck_quotation_send_receipts_hash'),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    attempt_id: Mapped[str] = mapped_column(String(40))
    quote_id: Mapped[str] = mapped_column(String(40))
    content_hash: Mapped[str] = mapped_column(String(64))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourcingCaseRow(Base):
    """V2 寻源案例；活跃唯一索引防同需求重复开案。"""

    __tablename__ = "sourcing_cases"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "case_id", name="pk_sourcing_cases"),
        UniqueConstraint("tenant_id", "trigger_key", name="uq_sourcing_cases_trigger"),
        ForeignKeyConstraint(
            ["tenant_id", "need_id"],
            ["validated_needs.tenant_id", "validated_needs.need_id"],
            name="fk_sourcing_cases_need",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "opportunity_id"],
            ["opportunities.tenant_id", "opportunities.opportunity_id"],
            name="fk_sourcing_cases_opportunity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "active_search_plan_id"],
            [
                "sourcing_public_plans.tenant_id",
                "sourcing_public_plans.case_id",
                "sourcing_public_plans.plan_id",
            ],
            name="fk_sourcing_cases_active_plan",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('opened','discovering','verifying','candidates_ready','handed_to_costing','failed')",
            name="ck_sourcing_cases_state",
        ),
        CheckConstraint("workflow_version >= 1 AND version >= 1", name="ck_sourcing_cases_versions"),
        CheckConstraint("jsonb_typeof(need_snapshot) = 'object'", name="ck_sourcing_cases_snapshot_json"),
        CheckConstraint(
            "jsonb_typeof(sealed_candidate_ids) = 'array' AND "
            "NOT jsonb_path_exists(sealed_candidate_ids, '$[*] ? (@.type() != \"string\")') AND "
            "((jsonb_array_length(sealed_candidate_ids) = 0 AND candidate_set_hash IS NULL "
            "AND candidates_verified_at IS NULL) OR "
            "(jsonb_array_length(sealed_candidate_ids) > 0 AND candidate_set_hash ~ '^[0-9a-f]{64}$' "
            "AND candidates_verified_at IS NOT NULL))",
            name="ck_sourcing_cases_candidate_seal",
        ),
        CheckConstraint("need_snapshot_hash ~ '^[0-9a-f]{64}$'", name="ck_sourcing_cases_snapshot_hash"),
        CheckConstraint("ladder_checked_to IS NULL OR ladder_checked_to BETWEEN 1 AND 7", name="ck_sourcing_cases_ladder"),
        CheckConstraint(
            "stop_code IS NULL OR stop_code IN ('approval_required','quota_status_unknown','paid_usage_enabled','quota_exhausted','provider_timeout','provider_rate_limited','page_access_forbidden','login_or_captcha','unsafe_redirect','no_search_results','no_verifiable_supplier','no_qualified_candidate','reconciliation_required','opportunity_required','need_incomplete','plan_confirmation_required','free_quota_unavailable','budget_exhausted','no_qualified_supply','manual_stop')",
            name="ck_sourcing_cases_stop_code",
        ),
        CheckConstraint(
            "stop_detail IS NULL OR (jsonb_typeof(stop_detail) = 'object' "
            "AND stop_detail ? 'stage' "
            "AND (stop_detail - 'stage' - 'query_index' - 'provider_http_status' "
            "- 'observed_count' - 'configured_limit') = '{}'::jsonb "
            "AND jsonb_typeof(stop_detail->'stage') = 'string' "
            "AND stop_detail->>'stage' IN ('intake','plan','quota','provider','page','candidate','review','cost_handoff') "
            "AND (NOT stop_detail ? 'query_index' "
            "OR jsonb_typeof(stop_detail->'query_index') = 'null' OR CASE "
            "WHEN jsonb_typeof(stop_detail->'query_index') = 'number' "
            "THEN (stop_detail->>'query_index')::numeric >= 0 "
            "AND (stop_detail->>'query_index')::numeric = trunc((stop_detail->>'query_index')::numeric) "
            "ELSE false END) "
            "AND (NOT stop_detail ? 'provider_http_status' "
            "OR jsonb_typeof(stop_detail->'provider_http_status') = 'null' OR CASE "
            "WHEN jsonb_typeof(stop_detail->'provider_http_status') = 'number' "
            "THEN (stop_detail->>'provider_http_status')::numeric BETWEEN 100 AND 599 "
            "AND (stop_detail->>'provider_http_status')::numeric = "
            "trunc((stop_detail->>'provider_http_status')::numeric) ELSE false END) "
            "AND (NOT stop_detail ? 'observed_count' "
            "OR jsonb_typeof(stop_detail->'observed_count') = 'null' OR CASE "
            "WHEN jsonb_typeof(stop_detail->'observed_count') = 'number' "
            "THEN (stop_detail->>'observed_count')::numeric >= 0 "
            "AND (stop_detail->>'observed_count')::numeric = "
            "trunc((stop_detail->>'observed_count')::numeric) ELSE false END) "
            "AND (NOT stop_detail ? 'configured_limit' "
            "OR jsonb_typeof(stop_detail->'configured_limit') = 'null' OR CASE "
            "WHEN jsonb_typeof(stop_detail->'configured_limit') = 'number' "
            "THEN (stop_detail->>'configured_limit')::numeric >= 0 "
            "AND (stop_detail->>'configured_limit')::numeric = "
            "trunc((stop_detail->>'configured_limit')::numeric) ELSE false END))",
            name="ck_sourcing_cases_stop_detail_json",
        ),
        CheckConstraint("btrim(tenant_id) <> '' AND btrim(case_id) <> '' AND btrim(need_id) <> '' AND btrim(trigger_key) <> ''", name="ck_sourcing_cases_core_nonblank"),
        CheckConstraint("(state = 'failed') = (failed_reason IS NOT NULL AND btrim(failed_reason) <> '')", name="ck_sourcing_cases_failure"),
        CheckConstraint("(state = 'handed_to_costing') = (completed_at IS NOT NULL)", name="ck_sourcing_cases_completed_at"),
        Index(
            "uq_sourcing_cases_active_need",
            "tenant_id",
            "need_id",
            "workflow_version",
            unique=True,
            postgresql_where=text("state IN ('opened','discovering','verifying','candidates_ready')"),
        ),
        Index("ix_sourcing_cases_queue", "tenant_id", "state", "opened_at", "case_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    case_id: Mapped[str] = mapped_column(String(40))
    need_id: Mapped[str] = mapped_column(String(40))
    opportunity_id: Mapped[str | None] = mapped_column(String(32))
    workflow_version: Mapped[int] = mapped_column(Integer)
    trigger_key: Mapped[str] = mapped_column(String(200))
    need_snapshot: Mapped[dict] = mapped_column(postgresql.JSONB)
    need_snapshot_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), server_default=text("'opened'"))
    ladder_checked_to: Mapped[int | None] = mapped_column(Integer)
    active_search_plan_id: Mapped[str | None] = mapped_column(String(40))
    sealed_candidate_ids: Mapped[list] = mapped_column(
        postgresql.JSONB, server_default=text("'[]'::jsonb")
    )
    candidate_set_hash: Mapped[str | None] = mapped_column(String(64))
    candidates_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    stop_code: Mapped[str | None] = mapped_column(String(40))
    stop_detail: Mapped[dict[str, object] | None] = mapped_column(
        postgresql.JSONB(none_as_null=True)
    )
    assigned_to: Mapped[str | None] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_reason: Mapped[str | None] = mapped_column(Text)


class SourcingLadderCheckRow(Base):
    """逐级且不可变的供给匹配检查事实。"""

    __tablename__ = "sourcing_ladder_checks"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "check_id", name="pk_sourcing_ladder_checks"),
        UniqueConstraint("tenant_id", "case_id", "sequence_number", name="uq_sourcing_ladder_checks_sequence"),
        UniqueConstraint("tenant_id", "case_id", "rung", name="uq_sourcing_ladder_checks_rung"),
        ForeignKeyConstraint(["tenant_id", "case_id"], ["sourcing_cases.tenant_id", "sourcing_cases.case_id"], name="fk_sourcing_ladder_checks_case", ondelete="RESTRICT"),
        CheckConstraint("sequence_number = rung AND rung BETWEEN 1 AND 7", name="ck_sourcing_ladder_checks_order"),
        CheckConstraint("outcome IN ('no_qualified_supply','qualified_supply_found')", name="ck_sourcing_ladder_checks_outcome"),
        CheckConstraint("jsonb_typeof(input_snapshot) = 'object'", name="ck_sourcing_ladder_checks_input_json"),
        CheckConstraint("input_snapshot_hash ~ '^[0-9a-f]{64}$'", name="ck_sourcing_ladder_checks_input_hash"),
        CheckConstraint("jsonb_typeof(spec_comparisons) = 'array'", name="ck_sourcing_ladder_checks_comparisons_json"),
        CheckConstraint("jsonb_typeof(evidence_refs) = 'array'", name="ck_sourcing_ladder_checks_evidence_json"),
        CheckConstraint("(match_object_type IS NULL) = (match_object_id IS NULL) AND btrim(conclusion) <> '' AND btrim(checked_by) <> ''", name="ck_sourcing_ladder_checks_core"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    check_id: Mapped[str] = mapped_column(String(40))
    case_id: Mapped[str] = mapped_column(String(40))
    sequence_number: Mapped[int] = mapped_column(Integer)
    rung: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(40))
    input_snapshot: Mapped[dict] = mapped_column(postgresql.JSONB)
    input_snapshot_hash: Mapped[str] = mapped_column(String(64))
    conclusion: Mapped[str] = mapped_column(Text)
    match_object_type: Mapped[str | None] = mapped_column(String(40))
    match_object_id: Mapped[str | None] = mapped_column(String(40))
    spec_comparisons: Mapped[list] = mapped_column(postgresql.JSONB)
    evidence_refs: Mapped[list] = mapped_column(postgresql.JSONB)
    checked_by: Mapped[str] = mapped_column(String(40))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourcingPublicPlanRow(Base):
    """老板确认的精确公开寻源范围。"""

    __tablename__ = "sourcing_public_plans"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "plan_id", name="pk_sourcing_public_plans"),
        UniqueConstraint("tenant_id", "case_id", "plan_id", name="uq_sourcing_public_plans_case_plan"),
        UniqueConstraint("tenant_id", "case_id", "version", name="uq_sourcing_public_plans_version"),
        ForeignKeyConstraint(["tenant_id", "case_id"], ["sourcing_cases.tenant_id", "sourcing_cases.case_id"], name="fk_sourcing_public_plans_case", ondelete="RESTRICT"),
        CheckConstraint("jsonb_typeof(target_countries) = 'array' AND jsonb_array_length(target_countries) > 0", name="ck_sourcing_public_plans_countries_json"),
        CheckConstraint(
            "jsonb_typeof(queries) = 'array' AND jsonb_array_length(queries) > 0 "
            "AND NOT jsonb_path_exists(queries, '$[*] ? (@.type() != \"object\" || "
            "!exists(@.query_text) || @.query_text.type() != \"string\" || "
            "!exists(@.target_country) || @.target_country.type() != \"string\")')",
            name="ck_sourcing_public_plans_queries_json",
        ),
        CheckConstraint("max_search_queries >= jsonb_array_length(queries) AND max_pages_read >= 1", name="ck_sourcing_public_plans_limits"),
        CheckConstraint("usage_credits_remaining >= 0 AND worst_case_credits >= 1", name="ck_sourcing_public_plans_credits"),
        CheckConstraint("provider = 'tavily' AND search_depth = 'basic'", name="ck_sourcing_public_plans_provider"),
        CheckConstraint("status IN ('pending_confirmation','authorized','running','exhausted','blocked','completed')", name="ck_sourcing_public_plans_status"),
        CheckConstraint("version >= 1 AND expected_case_version >= 1", name="ck_sourcing_public_plans_versions"),
        CheckConstraint("plan_hash ~ '^[0-9a-f]{64}$' AND (authorized_plan_hash IS NULL OR authorized_plan_hash ~ '^[0-9a-f]{64}$')", name="ck_sourcing_public_plans_hashes"),
        CheckConstraint("(confirmed_by IS NULL AND confirmed_at IS NULL AND authorized_plan_hash IS NULL AND status = 'pending_confirmation') OR (confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL AND authorized_plan_hash = plan_hash AND status <> 'pending_confirmation')", name="ck_sourcing_public_plans_confirmation"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    plan_id: Mapped[str] = mapped_column(String(40))
    case_id: Mapped[str] = mapped_column(String(40))
    target_countries: Mapped[list] = mapped_column(postgresql.JSONB)
    product_category: Mapped[str] = mapped_column(String(100))
    queries: Mapped[list] = mapped_column(postgresql.JSONB)
    max_search_queries: Mapped[int] = mapped_column(Integer)
    max_pages_read: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(32))
    search_depth: Mapped[str] = mapped_column(String(16))
    usage_credits_remaining: Mapped[int] = mapped_column(BigInteger)
    worst_case_credits: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer)
    expected_case_version: Mapped[int] = mapped_column(Integer)
    plan_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    confirmed_by: Mapped[str | None] = mapped_column(String(40))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authorized_plan_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourcingCandidateRow(Base):
    """观察事实、供应商声明、推断与参考价结构分离的候选行。"""

    __tablename__ = "sourcing_candidates"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "candidate_id", name="pk_sourcing_candidates"),
        UniqueConstraint("tenant_id", "case_id", "candidate_id", name="uq_sourcing_candidates_case_candidate"),
        ForeignKeyConstraint(["tenant_id", "case_id"], ["sourcing_cases.tenant_id", "sourcing_cases.case_id"], name="fk_sourcing_candidates_case", ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "public_draft_source_key"], ["sourcing_candidate_drafts.tenant_id", "sourcing_candidate_drafts.source_key"], name="fk_sourcing_candidates_public_draft", ondelete="RESTRICT"),
        CheckConstraint("jsonb_typeof(observed_facts) = 'object'", name="ck_sourcing_candidates_observed_json"),
        CheckConstraint("jsonb_typeof(supplier_claims) = 'object'", name="ck_sourcing_candidates_claims_json"),
        CheckConstraint("jsonb_typeof(match_inferences) = 'object'", name="ck_sourcing_candidates_inferences_json"),
        CheckConstraint("jsonb_typeof(verified_specs) = 'array'", name="ck_sourcing_candidates_specs_json"),
        CheckConstraint("jsonb_typeof(indicative_price_tiers) = 'array' AND jsonb_array_length(indicative_price_tiers) > 0 AND NOT jsonb_path_exists(indicative_price_tiers, '$[*] ? (@.type() != \"object\" || !exists(@.minimum_quantity) || @.minimum_quantity.type() != \"number\" || !exists(@.amount) || @.amount.type() != \"string\" || !exists(@.currency) || @.currency.type() != \"string\" || !exists(@.unit) || @.unit.type() != \"string\" || !exists(@.provenance) || @.provenance.type() != \"object\" || !exists(@.evidence_ref) || @.evidence_ref.type() != \"string\")')", name="ck_sourcing_candidates_price_tiers_json"),
        CheckConstraint("jsonb_typeof(rejection_reasons) = 'array'", name="ck_sourcing_candidates_rejections_json"),
        CheckConstraint("match_explanation IS NULL OR jsonb_typeof(match_explanation) = 'object'", name="ck_sourcing_candidates_match_json"),
        CheckConstraint("moq IS NULL OR moq >= 1", name="ck_sourcing_candidates_moq"),
        CheckConstraint("currency IS NULL OR currency ~ '^[A-Z]{3}$'", name="ck_sourcing_candidates_currency"),
        CheckConstraint("btrim(supplier_name) <> '' AND btrim(product_title) <> ''", name="ck_sourcing_candidates_core_nonblank"),
        Index("ix_sourcing_candidates_case_created", "tenant_id", "case_id", "created_at", "candidate_id"),
        Index("uq_sourcing_candidates_public_draft_source", "tenant_id", "public_draft_source_key", unique=True, postgresql_where=text("public_draft_source_key IS NOT NULL")),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    candidate_id: Mapped[str] = mapped_column(String(40))
    case_id: Mapped[str] = mapped_column(String(40))
    supplier_name: Mapped[str] = mapped_column(String(300))
    source_platform: Mapped[str | None] = mapped_column(String(100))
    product_title: Mapped[str] = mapped_column(String(500))
    observed_facts: Mapped[dict] = mapped_column(postgresql.JSONB)
    supplier_claims: Mapped[dict] = mapped_column(postgresql.JSONB)
    match_inferences: Mapped[dict] = mapped_column(postgresql.JSONB)
    verified_specs: Mapped[list] = mapped_column(postgresql.JSONB)
    indicative_price_tiers: Mapped[list] = mapped_column(postgresql.JSONB)
    moq: Mapped[int | None] = mapped_column(Integer)
    price_unit: Mapped[str | None] = mapped_column(String(50))
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    match_explanation: Mapped[dict | None] = mapped_column(postgresql.JSONB)
    rejected: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    rejection_reasons: Mapped[list] = mapped_column(postgresql.JSONB)
    verified_by: Mapped[str | None] = mapped_column(String(40))
    public_draft_source_key: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourcingCandidateEvidenceRow(Base):
    """候选与同租户原始网页 Artifact 的不可替换关系。"""

    __tablename__ = "sourcing_candidate_evidence"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "candidate_id", "artifact_id", name="pk_sourcing_candidate_evidence"),
        ForeignKeyConstraint(["tenant_id", "candidate_id"], ["sourcing_candidates.tenant_id", "sourcing_candidates.candidate_id"], name="fk_sourcing_candidate_evidence_candidate", ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "artifact_id"], ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"], name="fk_sourcing_candidate_evidence_artifact", ondelete="RESTRICT"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$' AND url ~ '^https?://'", name="ck_sourcing_candidate_evidence_locator"),
        Index("ix_sourcing_candidate_evidence_order", "tenant_id", "candidate_id", "observed_at", "artifact_id"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    candidate_id: Mapped[str] = mapped_column(String(40))
    artifact_id: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64))


class SourcingSupplyOptionRow(Base):
    """现有产品与候选产品统一供人工选择的供给选项。"""

    __tablename__ = "sourcing_supply_options"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "option_id", name="pk_sourcing_supply_options"),
        UniqueConstraint("tenant_id", "case_id", "option_id", name="uq_sourcing_supply_options_case_option"),
        UniqueConstraint("tenant_id", "case_id", "option_id", "supplier_candidate_id", name="uq_sourcing_supply_options_candidate_path"),
        UniqueConstraint("tenant_id", "case_id", "supplier_candidate_id", name="uq_sourcing_supply_options_supplier_candidate"),
        Index(
            "uq_sourcing_supply_options_existing_product",
            "tenant_id",
            "case_id",
            "product_id",
            unique=True,
            postgresql_where=text("source = 'existing_product'"),
        ),
        ForeignKeyConstraint(["tenant_id", "case_id"], ["sourcing_cases.tenant_id", "sourcing_cases.case_id"], name="fk_sourcing_supply_options_case", ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "case_id", "supplier_candidate_id"], ["sourcing_candidates.tenant_id", "sourcing_candidates.case_id", "sourcing_candidates.candidate_id"], name="fk_sourcing_supply_options_candidate", ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.product_id"], name="fk_sourcing_supply_options_product", ondelete="RESTRICT"),
        CheckConstraint("(source = 'existing_product' AND supplier_candidate_id IS NULL) OR (source = 'supplier_candidate' AND supplier_candidate_id IS NOT NULL)", name="ck_sourcing_supply_options_source"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    option_id: Mapped[str] = mapped_column(String(40))
    case_id: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(32))
    product_id: Mapped[str] = mapped_column(String(40))
    supplier_candidate_id: Mapped[str | None] = mapped_column(String(40))
    is_qualified: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourcingReviewRow(Base):
    """人工主选、备选及逐次确认事实。"""

    __tablename__ = "sourcing_reviews"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "review_id", name="pk_sourcing_reviews"),
        UniqueConstraint("tenant_id", "case_id", name="uq_sourcing_reviews_case"),
        ForeignKeyConstraint(["tenant_id", "case_id", "primary_option_id"], ["sourcing_supply_options.tenant_id", "sourcing_supply_options.case_id", "sourcing_supply_options.option_id"], name="fk_sourcing_reviews_primary_option", ondelete="RESTRICT"),
        CheckConstraint("jsonb_typeof(primary_selection) = 'object'", name="ck_sourcing_reviews_primary_json"),
        CheckConstraint("jsonb_typeof(alternate_option_ids) = 'array' AND jsonb_array_length(alternate_option_ids) <= 2 AND NOT jsonb_path_exists(alternate_option_ids, '$[*] ? (@.type() != \"string\")')", name="ck_sourcing_reviews_alternates_json"),
        CheckConstraint("expected_case_version >= 1 AND btrim(reason) <> '' AND btrim(submitted_by) <> ''", name="ck_sourcing_reviews_core"),
        CheckConstraint("(confirmed_by IS NULL) = (confirmed_at IS NULL)", name="ck_sourcing_reviews_confirmation"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    review_id: Mapped[str] = mapped_column(String(40))
    case_id: Mapped[str] = mapped_column(String(40))
    primary_option_id: Mapped[str] = mapped_column(String(40))
    primary_selection: Mapped[dict] = mapped_column(postgresql.JSONB)
    alternate_option_ids: Mapped[list] = mapped_column(postgresql.JSONB)
    reason: Mapped[str] = mapped_column(Text)
    expected_case_version: Mapped[int] = mapped_column(Integer)
    submitted_by: Mapped[str] = mapped_column(String(40))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(String(40))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourcingSearchExecutionRow(Base):
    """搜索定位结果回执；不把摘要提升成证据。"""

    __tablename__ = "sourcing_search_executions"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "execution_id", name="pk_sourcing_search_executions"),
        UniqueConstraint("tenant_id", "run_id", "plan_hash", "query_index", name="uq_sourcing_search_executions_query"),
        UniqueConstraint("tenant_id", "request_key", name="uq_sourcing_search_executions_request"),
        ForeignKeyConstraint(["tenant_id", "case_id", "plan_id"], ["sourcing_public_plans.tenant_id", "sourcing_public_plans.case_id", "sourcing_public_plans.plan_id"], name="fk_sourcing_search_executions_plan", ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "run_id"], ["workflow_runs.tenant_id", "workflow_runs.run_id"], name="fk_sourcing_search_executions_run", ondelete="RESTRICT"),
        CheckConstraint("plan_hash ~ '^[0-9a-f]{64}$' AND request_key ~ '^[0-9a-f]{64}$'", name="ck_sourcing_search_executions_hashes"),
        CheckConstraint("query_index >= 0 AND query_hash ~ '^[0-9a-f]{64}$'", name="ck_sourcing_search_executions_query"),
        CheckConstraint("jsonb_typeof(locator_results) = 'array'", name="ck_sourcing_search_executions_locators_json"),
        CheckConstraint("provider_status IN ('succeeded','no_results','uncertain','failed')", name="ck_sourcing_search_executions_status"),
        CheckConstraint("(provider_status IN ('succeeded','no_results')) = (completed_at IS NOT NULL)", name="ck_sourcing_search_executions_completed"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    execution_id: Mapped[str] = mapped_column(String(40))
    case_id: Mapped[str] = mapped_column(String(40))
    plan_id: Mapped[str] = mapped_column(String(40))
    run_id: Mapped[str] = mapped_column(String(40))
    plan_hash: Mapped[str] = mapped_column(String(64))
    query_index: Mapped[int] = mapped_column(Integer)
    request_key: Mapped[str] = mapped_column(String(64))
    query_hash: Mapped[str] = mapped_column(String(64))
    locator_results: Mapped[list] = mapped_column(postgresql.JSONB)
    provider_status: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourcingPageAttemptRow(Base):
    """已授权 Run 的幂等页面尝试；用于重启后继续执行同一预算。"""

    __tablename__ = "sourcing_page_attempts"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "run_id", "plan_hash", "query_index", "result_index",
            name="pk_sourcing_page_attempts",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "plan_id"],
            ["sourcing_public_plans.tenant_id", "sourcing_public_plans.case_id", "sourcing_public_plans.plan_id"],
            name="fk_sourcing_page_attempts_plan", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["workflow_runs.tenant_id", "workflow_runs.run_id"],
            name="fk_sourcing_page_attempts_run", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "draft_id"],
            [
                "sourcing_candidate_drafts.tenant_id",
                "sourcing_candidate_drafts.draft_id",
            ],
            name="fk_sourcing_page_attempts_draft",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "plan_hash ~ '^[0-9a-f]{64}$' AND query_index >= 0 AND result_index >= 0",
            name="ck_sourcing_page_attempts_binding",
        ),
        CheckConstraint(
            "(status = 'claimed' AND outcome IS NULL AND draft_id IS NULL "
            "AND completed_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL AND "
            "((outcome = 'draft_saved' AND draft_id IS NOT NULL) OR "
            "(outcome IN ('page_access_forbidden','login_or_captcha',"
            "'unsafe_redirect','provider_rate_limited','provider_timeout',"
            "'reconciliation_required') "
            "AND draft_id IS NULL)))",
            name="ck_sourcing_page_attempts_state",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    case_id: Mapped[str] = mapped_column(String(40))
    plan_id: Mapped[str] = mapped_column(String(40))
    run_id: Mapped[str] = mapped_column(String(40))
    plan_hash: Mapped[str] = mapped_column(String(64))
    query_index: Mapped[int] = mapped_column(Integer)
    result_index: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), server_default=text("'claimed'"))
    outcome: Mapped[str | None] = mapped_column(String(40))
    draft_id: Mapped[str | None] = mapped_column(String(40))
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourcingCandidateDraftRow(Base):
    """未核验公开页面草稿；原文、联系方式与模型输出不入库。"""

    __tablename__ = "sourcing_candidate_drafts"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "draft_id", name="pk_sourcing_candidate_drafts"),
        UniqueConstraint("tenant_id", "source_key", name="uq_sourcing_candidate_drafts_source"),
        UniqueConstraint("tenant_id", "case_id", "run_id", "plan_hash", "query_index", "result_index", "evidence_artifact_ref", name="uq_sourcing_candidate_drafts_location"),
        ForeignKeyConstraint(["tenant_id", "case_id", "plan_id"], ["sourcing_public_plans.tenant_id", "sourcing_public_plans.case_id", "sourcing_public_plans.plan_id"], name="fk_sourcing_candidate_drafts_plan", ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "run_id"], ["workflow_runs.tenant_id", "workflow_runs.run_id"], name="fk_sourcing_candidate_drafts_run", ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "evidence_artifact_ref"], ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"], name="fk_sourcing_candidate_drafts_artifact", ondelete="RESTRICT"),
        CheckConstraint("plan_hash ~ '^[0-9a-f]{64}$' AND source_key ~ '^[0-9a-f]{64}$' AND evidence_hash ~ '^[0-9a-f]{64}$'", name="ck_sourcing_candidate_drafts_hashes"),
        CheckConstraint("query_index >= 0 AND result_index >= 0", name="ck_sourcing_candidate_drafts_indexes"),
        CheckConstraint("jsonb_typeof(specs) = 'array' AND jsonb_typeof(indicative_price_tiers) = 'array' AND jsonb_typeof(rejection_codes) = 'array'", name="ck_sourcing_candidate_drafts_json"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    draft_id: Mapped[str] = mapped_column(String(40))
    case_id: Mapped[str] = mapped_column(String(40))
    run_id: Mapped[str] = mapped_column(String(40))
    plan_id: Mapped[str] = mapped_column(String(40))
    plan_hash: Mapped[str] = mapped_column(String(64))
    query_index: Mapped[int] = mapped_column(Integer)
    result_index: Mapped[int] = mapped_column(Integer)
    source_key: Mapped[str] = mapped_column(String(64))
    supplier_name: Mapped[str | None] = mapped_column(String(300))
    product_title: Mapped[str | None] = mapped_column(String(500))
    specs: Mapped[list] = mapped_column(postgresql.JSONB)
    moq: Mapped[int | None] = mapped_column(Integer)
    indicative_price_tiers: Mapped[list] = mapped_column(postgresql.JSONB)
    rejection_codes: Mapped[list] = mapped_column(postgresql.JSONB)
    evidence_url: Mapped[str] = mapped_column(Text)
    evidence_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    evidence_artifact_ref: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourcingSearchReconciliationRow(Base):
    """不确定 Provider 结果的只增人工核对事实。"""

    __tablename__ = "sourcing_search_reconciliations"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "reconciliation_id", name="pk_sourcing_search_reconciliations"),
        UniqueConstraint("tenant_id", "execution_id", name="uq_sourcing_search_reconciliations_execution"),
        ForeignKeyConstraint(["tenant_id", "execution_id"], ["sourcing_search_executions.tenant_id", "sourcing_search_executions.execution_id"], name="fk_sourcing_search_reconciliations_execution", ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "provider_usage_artifact_ref"], ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"], name="fk_sourcing_search_reconciliations_artifact", ondelete="RESTRICT"),
        CheckConstraint("status IN ('required','confirmed_consumed','confirmed_not_consumed')", name="ck_sourcing_search_reconciliations_status"),
        CheckConstraint("provider_usage_artifact_ref ~ '^art_[0-7][0-9A-HJKMNP-TV-Z]{25}$'", name="ck_sourcing_search_reconciliations_artifact"),
        CheckConstraint("btrim(reason) <> ''", name="ck_sourcing_search_reconciliations_reason"),
        CheckConstraint("(status = 'required' AND reconciled_by IS NULL AND reconciled_at IS NULL) OR (status <> 'required' AND reconciled_by IS NOT NULL AND reconciled_at IS NOT NULL)", name="ck_sourcing_search_reconciliations_resolution"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    reconciliation_id: Mapped[str] = mapped_column(String(40))
    execution_id: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    provider_usage_artifact_ref: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reconciled_by: Mapped[str | None] = mapped_column(String(40))
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SupplierRow(Base):
    """租户内供应商能力索引。"""

    __tablename__ = "suppliers"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "supplier_id", name="pk_suppliers"),
        UniqueConstraint("tenant_id", "normalized_name", name="uq_suppliers_normalized_name"),
        CheckConstraint("jsonb_typeof(platform_refs) = 'array'", name="ck_suppliers_platform_refs_json"),
        CheckConstraint("jsonb_typeof(capability_tags) = 'array'", name="ck_suppliers_capability_tags_json"),
        CheckConstraint("verification IN ('unverified','basic_checked','transacted')", name="ck_suppliers_verification"),
        CheckConstraint("btrim(name) <> '' AND btrim(normalized_name) <> ''", name="ck_suppliers_core_nonblank"),
        Index("ix_suppliers_capability_tags", "tenant_id", "verification"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    supplier_id: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(300))
    normalized_name: Mapped[str] = mapped_column(String(300))
    region: Mapped[str | None] = mapped_column(String(100))
    platform_refs: Mapped[list] = mapped_column(postgresql.JSONB)
    capability_tags: Mapped[list] = mapped_column(postgresql.JSONB)
    verification: Mapped[str] = mapped_column(String(24))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProductRow(Base):
    """三个产品池共享行；成本来源字段必须成组存在。"""

    __tablename__ = "products"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "product_id", name="pk_products"),
        ForeignKeyConstraint(["tenant_id", "supplier_id"], ["suppliers.tenant_id", "suppliers.supplier_id"], name="fk_products_supplier", ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "internal_cost_source_ref"], ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"], name="fk_products_internal_cost_artifact", ondelete="RESTRICT"),
        CheckConstraint("pool IN ('formal','candidate','capability')", name="ck_products_pool"),
        CheckConstraint("(pool = 'candidate' AND candidate_status IS NOT NULL AND candidate_status IN ('source_only','partial','not_approved')) OR (pool <> 'candidate' AND candidate_status IS NULL)", name="ck_products_candidate_status"),
        CheckConstraint("moq IS NULL OR moq >= 1", name="ck_products_moq"),
        CheckConstraint("(lead_time_days_min IS NULL AND lead_time_days_max IS NULL) OR (lead_time_days_min IS NOT NULL AND lead_time_days_max IS NOT NULL AND lead_time_days_min >= 0 AND lead_time_days_max >= lead_time_days_min)", name="ck_products_lead_time"),
        CheckConstraint("(internal_cost_amount IS NULL AND internal_cost_currency IS NULL AND internal_cost_basis IS NULL AND internal_cost_unit IS NULL AND internal_cost_source_ref IS NULL) OR (internal_cost_amount IS NOT NULL AND internal_cost_amount >= 0 AND internal_cost_currency IS NOT NULL AND internal_cost_currency ~ '^[A-Z]{3}$' AND internal_cost_basis IS NOT NULL AND btrim(internal_cost_basis) <> '' AND internal_cost_unit IS NOT NULL AND btrim(internal_cost_unit) <> '' AND internal_cost_source_ref IS NOT NULL)", name="ck_products_internal_cost_complete"),
        CheckConstraint("(allowed_price_min_amount IS NULL AND allowed_price_min_currency IS NULL) OR (allowed_price_min_amount IS NOT NULL AND allowed_price_min_amount >= 0 AND allowed_price_min_currency IS NOT NULL AND allowed_price_min_currency ~ '^[A-Z]{3}$')", name="ck_products_allowed_min_pair"),
        CheckConstraint("(allowed_price_max_amount IS NULL AND allowed_price_max_currency IS NULL) OR (allowed_price_max_amount IS NOT NULL AND allowed_price_max_amount >= 0 AND allowed_price_max_currency IS NOT NULL AND allowed_price_max_currency ~ '^[A-Z]{3}$')", name="ck_products_allowed_max_pair"),
        CheckConstraint("allowed_price_min_amount IS NULL OR allowed_price_max_amount IS NULL OR (allowed_price_min_currency = allowed_price_max_currency AND allowed_price_min_amount <= allowed_price_max_amount)", name="ck_products_allowed_range"),
        CheckConstraint("jsonb_typeof(sellable_markets) = 'array' AND jsonb_typeof(selling_points) = 'array' AND jsonb_typeof(known_issues) = 'array'", name="ck_products_lists_json"),
        CheckConstraint("btrim(name_zh) <> '' AND btrim(name_en) <> '' AND btrim(category) <> '' AND btrim(normalized_category) <> ''", name="ck_products_core_nonblank"),
        Index("ix_products_pool_category", "tenant_id", "pool", "normalized_category", "product_id"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    product_id: Mapped[str] = mapped_column(String(40))
    pool: Mapped[str] = mapped_column(String(24))
    candidate_status: Mapped[str | None] = mapped_column(String(24))
    name_zh: Mapped[str] = mapped_column(String(300))
    name_en: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(100))
    normalized_category: Mapped[str] = mapped_column(String(100))
    spec_summary: Mapped[str | None] = mapped_column(Text)
    moq: Mapped[int | None] = mapped_column(Integer)
    lead_time_days_min: Mapped[int | None] = mapped_column(Integer)
    lead_time_days_max: Mapped[int | None] = mapped_column(Integer)
    supplier_id: Mapped[str | None] = mapped_column(String(40))
    internal_cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    internal_cost_currency: Mapped[str | None] = mapped_column(CHAR(3))
    internal_cost_basis: Mapped[str | None] = mapped_column(Text)
    internal_cost_unit: Mapped[str | None] = mapped_column(String(50))
    internal_cost_source_ref: Mapped[str | None] = mapped_column(String(32))
    allowed_price_min_amount: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    allowed_price_min_currency: Mapped[str | None] = mapped_column(CHAR(3))
    allowed_price_max_amount: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    allowed_price_max_currency: Mapped[str | None] = mapped_column(CHAR(3))
    sellable_markets: Mapped[list] = mapped_column(postgresql.JSONB)
    customizable: Mapped[bool] = mapped_column(Boolean)
    selling_points: Mapped[list] = mapped_column(postgresql.JSONB)
    known_issues: Mapped[list] = mapped_column(postgresql.JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProductMatchSpecRow(Base):
    """产品逐项规格事实；Evidence 必须属于同租户不可变 Raw Artifact。"""

    __tablename__ = "product_match_specs"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "product_id",
            "normalized_spec_name",
            name="pk_product_match_specs",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["products.tenant_id", "products.product_id"],
            name="fk_product_match_specs_product",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "evidence_ref"],
            ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"],
            name="fk_product_match_specs_artifact",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "btrim(normalized_spec_name) <> '' AND btrim(value) <> ''",
            name="ck_product_match_specs_nonblank",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    product_id: Mapped[str] = mapped_column(String(40))
    normalized_spec_name: Mapped[str] = mapped_column(String(100))
    value: Mapped[str] = mapped_column(Text)
    evidence_ref: Mapped[str] = mapped_column(String(32))


class ProductVariantRow(Base):
    """产品 SKU 与规格属性。"""

    __tablename__ = "product_variants"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "variant_id", name="pk_product_variants"),
        UniqueConstraint("tenant_id", "sku", name="uq_product_variants_sku"),
        ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.product_id"], name="fk_product_variants_product", ondelete="RESTRICT"),
        CheckConstraint("btrim(sku) <> '' AND jsonb_typeof(attributes) = 'object'", name="ck_product_variants_core"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    variant_id: Mapped[str] = mapped_column(String(40))
    product_id: Mapped[str] = mapped_column(String(40))
    sku: Mapped[str] = mapped_column(String(100))
    attributes: Mapped[dict] = mapped_column(postgresql.JSONB)


class SupplyCapabilityRow(Base):
    """无固定 SKU 的供给能力池。"""

    __tablename__ = "supply_capabilities"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "capability_id", name="pk_supply_capabilities"),
        UniqueConstraint("tenant_id", "normalized_kind", "capability_id", name="uq_supply_capabilities_kind_id"),
        CheckConstraint("jsonb_typeof(proof_refs) = 'array'", name="ck_supply_capabilities_proof_json"),
        CheckConstraint("btrim(kind) <> '' AND btrim(normalized_kind) <> '' AND btrim(description) <> ''", name="ck_supply_capabilities_core"),
        Index("ix_supply_capabilities_kind", "tenant_id", "normalized_kind", "capability_id"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    capability_id: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(100))
    normalized_kind: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text)
    proof_refs: Mapped[list] = mapped_column(postgresql.JSONB)


class ProductCandidateSourceRow(Base):
    """候选产品卡的 tenant-bound Sourcing 来源幂等键。"""

    __tablename__ = "product_candidate_sources"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "product_id", name="pk_product_candidate_sources"),
        UniqueConstraint("tenant_id", "sourcing_case_id", "supplier_candidate_id", name="uq_product_candidate_sources_origin"),
        ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.product_id"], name="fk_product_candidate_sources_product", ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "sourcing_case_id", "supplier_candidate_id"], ["sourcing_candidates.tenant_id", "sourcing_candidates.case_id", "sourcing_candidates.candidate_id"], name="fk_product_candidate_sources_candidate", ondelete="RESTRICT"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    product_id: Mapped[str] = mapped_column(String(40))
    sourcing_case_id: Mapped[str] = mapped_column(String(40))
    supplier_candidate_id: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProductCandidatePriceRefRow(Base):
    """候选产品卡的精确 Decimal 数量档与证据引用。"""

    __tablename__ = "product_candidate_price_refs"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "product_id", "minimum_quantity", name="pk_product_candidate_price_refs"),
        ForeignKeyConstraint(["tenant_id", "product_id"], ["product_candidate_sources.tenant_id", "product_candidate_sources.product_id"], name="fk_product_candidate_price_refs_source", ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "artifact_id"], ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"], name="fk_product_candidate_price_refs_artifact", ondelete="RESTRICT"),
        CheckConstraint("minimum_quantity >= 1 AND unit_amount > 0", name="ck_product_candidate_price_refs_positive"),
        CheckConstraint("currency ~ '^[A-Z]{3}$' AND btrim(unit) <> ''", name="ck_product_candidate_price_refs_unit"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    product_id: Mapped[str] = mapped_column(String(40))
    minimum_quantity: Mapped[int] = mapped_column(Integer)
    unit_amount: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    currency: Mapped[str] = mapped_column(CHAR(3))
    unit: Mapped[str] = mapped_column(String(50))
    artifact_id: Mapped[str] = mapped_column(String(32))


class SupplierPriceRecordRow(Base):
    """只增供应商参考价或带有效期的报价依据。"""

    __tablename__ = "supplier_price_records"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "price_record_id", name="pk_supplier_price_records"),
        UniqueConstraint("tenant_id", "supplier_id", "product_desc", "quantity_tier", "observed_at", "artifact_id", name="uq_supplier_price_records_observation"),
        ForeignKeyConstraint(["tenant_id", "supplier_id"], ["suppliers.tenant_id", "suppliers.supplier_id"], name="fk_supplier_price_records_supplier", ondelete="RESTRICT"),
        ForeignKeyConstraint(["tenant_id", "artifact_id"], ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"], name="fk_supplier_price_records_artifact", ondelete="RESTRICT"),
        CheckConstraint("quantity_tier >= 1 AND unit_amount > 0", name="ck_supplier_price_records_positive"),
        CheckConstraint("currency ~ '^[A-Z]{3}$' AND basis IN ('indicative','quoted')", name="ck_supplier_price_records_price"),
        CheckConstraint("btrim(product_desc) <> ''", name="ck_supplier_price_records_description"),
        CheckConstraint("(basis = 'quoted' AND valid_until IS NOT NULL AND valid_until > observed_at) OR (basis = 'indicative' AND valid_until IS NULL)", name="ck_supplier_price_records_validity"),
        Index("ix_supplier_price_records_lookup", "tenant_id", "supplier_id", "observed_at", "price_record_id"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40))
    price_record_id: Mapped[str] = mapped_column(String(40))
    supplier_id: Mapped[str] = mapped_column(String(40))
    product_desc: Mapped[str] = mapped_column(Text)
    quantity_tier: Mapped[int] = mapped_column(Integer)
    unit_amount: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    currency: Mapped[str] = mapped_column(CHAR(3))
    basis: Mapped[str] = mapped_column(String(16))
    artifact_id: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
