"""租户基类行为边界：无绑定租户和无过滤后门必须失败关闭。"""
from __future__ import annotations

import pytest
from sqlalchemy import String
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from infra.db.base import TenantScopedRepository
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import TenantId, UserId


class _Base(DeclarativeBase):
    pass


class _TenantRow(_Base):
    __tablename__ = "tenant_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String)


def test_scoped_query_injects_tenant_filter() -> None:
    repo = TenantScopedRepository(TenantId("tenant_phase1"))
    compiled = repo.scoped_query(_TenantRow).compile(dialect=postgresql.dialect())
    assert "tenant_rows.tenant_id =" in str(compiled)
    assert "tenant_phase1" in compiled.params.values()


@pytest.mark.parametrize("tenant", ["", " ", " tenant", "tenant ", None])
def test_repository_rejects_unbound_or_noncanonical_tenant(tenant: str) -> None:
    with pytest.raises(TenantIsolationViolation):
        TenantScopedRepository(tenant)


@pytest.mark.parametrize("actor,reason", [
    ("ops_alice", "已记录理由"), ("", "理由"), ("ops_alice", ""), (" ", " "),
])
def test_unfiltered_query_is_always_rejected(actor: str, reason: str) -> None:
    repo = TenantScopedRepository(TenantId("tenant_phase1"))
    with pytest.raises(TenantIsolationViolation):
        repo.unsafe_cross_tenant_query(_TenantRow, UserId(actor), reason)
