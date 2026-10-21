"""候选库存持久表：公共池与公司事实各自绑定固定tenant角色。"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from infra.db.tables import Base


class PublicCandidateRow(Base):
    __tablename__ = "public_candidates"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "canonical_domain", name="uq_public_candidate_domain"
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    canonical_domain: Mapped[str] = mapped_column(String(253))
    payload: Mapped[dict] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InventoryQuotaRow(Base):
    __tablename__ = "inventory_quotas"
    __table_args__ = (
        CheckConstraint(
            "daily_limit >= 0 AND used_today >= 0",
            name="ck_inventory_quota_nonnegative",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    daily_limit: Mapped[int] = mapped_column(Integer)
    quota_day: Mapped[date] = mapped_column(Date)
    used_today: Mapped[int] = mapped_column(Integer)


class InventoryRequestRow(Base):
    __tablename__ = "inventory_requests"
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    job_id: Mapped[str | None] = mapped_column(String(40))


class CandidateAllocationRow(Base):
    __tablename__ = "candidate_allocations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "canonical_domain", name="uq_candidate_allocation_domain"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "key_hash"],
            ["inventory_requests.tenant_id", "inventory_requests.key_hash"],
            name="fk_allocation_request",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["prospect_accounts.tenant_id", "prospect_accounts.account_id"],
            name="fk_allocation_account",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    allocation_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    canonical_domain: Mapped[str] = mapped_column(String(253))
    employee_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    allocated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_by: Mapped[str | None] = mapped_column(String(40))
    account_id: Mapped[str | None] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSONB)


class AllocationEmailRow(Base):
    __tablename__ = "candidate_allocation_emails"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "allocation_id"],
            ["candidate_allocations.tenant_id", "candidate_allocations.allocation_id"],
            name="fk_allocation_email",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    allocation_id: Mapped[str] = mapped_column(String(40))


class AcquisitionJobRow(Base):
    __tablename__ = "acquisition_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "key_hash"],
            ["inventory_requests.tenant_id", "inventory_requests.key_hash"],
            name="fk_acquisition_request",
        ),
        UniqueConstraint("tenant_id", "key_hash", name="uq_acquisition_request"),
        CheckConstraint(
            "status IN ('queued','processing','complete','failed')",
            name="ck_acquisition_status",
        ),
        CheckConstraint(
            "collection_reserved_seconds BETWEEN 0 AND 480",
            name="ck_acquisition_collection_budget",
        ),
        CheckConstraint(
            "remaining_count >= 0 AND requested_count >= remaining_count AND attempts >= 0",
            name="ck_acquisition_counts",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    employee_id: Mapped[str] = mapped_column(String(40))
    query: Mapped[dict] = mapped_column(JSONB)
    search_binding: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    search_reservations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    fit_batches: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    source_plan: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    collection_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    collection_reserved_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    requested_count: Mapped[int] = mapped_column(Integer)
    remaining_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(30))


class AcquisitionAttemptRow(Base):
    __tablename__ = "acquisition_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["acquisition_jobs.tenant_id", "acquisition_jobs.job_id"],
            name="fk_acquisition_attempt_job",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[str] = mapped_column(String(80))
    outcome: Mapped[str] = mapped_column(String(30))
    valid_new_count: Mapped[int] = mapped_column(Integer)
    diagnostic: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InventoryFollowupRequestRow(Base):
    """原搜索请求事务内保存的私有委托，不扫描历史取得记录补造授权。"""
    __tablename__ = "inventory_followup_requests"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "key_hash"], ["inventory_requests.tenant_id", "inventory_requests.key_hash"], name="fk_followup_request"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    authorization: Mapped[dict] = mapped_column(JSONB)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class CustomerPreparationItemRow(Base):
    """每次授权交付唯一准备项；仅存安全引用、租约和固定状态。"""
    __tablename__ = "customer_preparation_items"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "key_hash"], ["inventory_followup_requests.tenant_id", "inventory_followup_requests.key_hash"], name="fk_preparation_request"),
        ForeignKeyConstraint(["tenant_id", "allocation_id"], ["candidate_allocations.tenant_id", "candidate_allocations.allocation_id"], name="fk_preparation_allocation"),
        CheckConstraint("state IN ('queued','processing','blocked','enrolled')", name="ck_preparation_state"),
        CheckConstraint("version >= 0", name="ck_preparation_version"),
    )
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    allocation_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="queued")
    stage: Mapped[str] = mapped_column(String(30), default="authorize")
    version: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(40))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_started: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_reason: Mapped[str | None] = mapped_column(String(60))
    result_refs: Mapped[dict] = mapped_column(JSONB, default=dict)
    ownership_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
