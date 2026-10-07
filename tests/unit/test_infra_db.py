"""infra.db.base 租户过滤基类单测（真实 SQLAlchemy 编译断言，无 mock、无 DB 执行）。

每个测试的名称与 docstring 标明它拦截的生产缺陷回归：
1. test_scoped_query_injects_tenant_filter —— scoped_query 忘记注入租户过滤，
   或编译后绑定值不是仓库构造时绑定的 TenantId。
2. test_cross_tenant_rejects_blank_actor_id / ..._whitespace_actor_id
   —— 后门缺 actor_id 非空校验（空串 / 纯空白放行）。
3. test_cross_tenant_rejects_blank_audit_reason / ..._whitespace_audit_reason
   —— 后门缺 audit_reason 非空校验（空串 / 纯空白放行）。
4. test_cross_tenant_valid_returns_unfiltered_and_audits
   —— 后门查询仍带租户过滤 / 漏审计 / 审计缺结构化字段。
"""
from __future__ import annotations

import logging

import pytest
from sqlalchemy import String
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from infra.db.base import TenantScopedRepository
from shared.schemas.identifiers import TenantId, UserId


class _Base(DeclarativeBase):
    pass


class _TenantRow(_Base):
    """最小租户行模型（仅测试用，非业务模型）。"""

    __tablename__ = "tenant_rows"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)


@pytest.fixture
def repo_cls() -> type[TenantScopedRepository]:
    return TenantScopedRepository


def test_scoped_query_injects_tenant_filter(repo_cls: type[TenantScopedRepository]) -> None:
    """拦截回归：scoped_query 未注入租户过滤，或绑定值不是仓库绑定的 TenantId。"""
    repo = repo_cls(TenantId("tenant_phase1"))
    compiled = repo.scoped_query(_TenantRow).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "WHERE" in sql.upper()
    where_fragment = sql.upper().split("WHERE", 1)[1]
    assert "TENANT_ID" in where_fragment
    assert "tenant_phase1" in compiled.params.values()


def test_cross_tenant_rejects_blank_actor_id(repo_cls: type[TenantScopedRepository]) -> None:
    """拦截回归：actor_id 非空校验缺失（空串放行）。"""
    repo = repo_cls(TenantId("tenant_phase1"))
    with pytest.raises(ValueError):
        repo.unsafe_cross_tenant_query(_TenantRow, UserId(""), "生产数据核对")


def test_cross_tenant_rejects_whitespace_actor_id(repo_cls: type[TenantScopedRepository]) -> None:
    """拦截回归：actor_id 全空白未拒绝。"""
    repo = repo_cls(TenantId("tenant_phase1"))
    with pytest.raises(ValueError):
        repo.unsafe_cross_tenant_query(_TenantRow, UserId("   "), "生产数据核对")


def test_cross_tenant_rejects_blank_audit_reason(repo_cls: type[TenantScopedRepository]) -> None:
    """拦截回归：audit_reason 非空校验缺失（空串放行）。"""
    repo = repo_cls(TenantId("tenant_phase1"))
    with pytest.raises(ValueError):
        repo.unsafe_cross_tenant_query(_TenantRow, UserId("ops_alice"), "")


def test_cross_tenant_rejects_whitespace_audit_reason(repo_cls: type[TenantScopedRepository]) -> None:
    """拦截回归：audit_reason 全空白未拒绝。"""
    repo = repo_cls(TenantId("tenant_phase1"))
    with pytest.raises(ValueError):
        repo.unsafe_cross_tenant_query(_TenantRow, UserId("ops_alice"), "   ")


def test_cross_tenant_valid_returns_unfiltered_and_audits(
    repo_cls: type[TenantScopedRepository], caplog: pytest.LogCaptureFixture
) -> None:
    """拦截回归：后门查询仍带租户过滤 / 漏审计 / 审计缺结构化字段。"""
    repo = repo_cls(TenantId("tenant_phase1"))
    with caplog.at_level(logging.INFO, logger="infra.db.audit"):
        stmt = repo.unsafe_cross_tenant_query(_TenantRow, UserId("ops_alice"), "生产数据核对")
    assert "WHERE" not in str(stmt.compile(dialect=postgresql.dialect())).upper()
    infra_records = [r for r in caplog.records if r.name == "infra.db.audit"]
    assert len(infra_records) == 1
    rec = infra_records[0]
    assert rec.__dict__["actor_id"] == "ops_alice"
    assert rec.__dict__["tenant_id"] == "tenant_phase1"
    assert rec.__dict__["audit_reason"] == "生产数据核对"
