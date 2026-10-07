"""tests.conftest 数据库夹具冒烟测试（真实 ORM 行为，无 mock、无 Docker）。

两个被测行为：
1. 经 db_session 的 ORM write → flush → select 往返成立。
2. 租户 A/B 两行以显式租户过滤查询，各自只读到本租户行（数据层隔离预演硬边界 8）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.schemas.identifiers import TenantId

# FixtureTenantRow 延迟导入：conftest 未就绪时夹具解析先行失败
# （RED 为 "fixture not found" 的配置级失败），避免模块级导入在收集期抛错。


async def test_roundtrip_through_db_session(db_session: AsyncSession, tenant_id: TenantId) -> None:
    """ORM write → flush → select 经 db_session 往返成立。"""
    from tests.conftest import FixtureTenantRow

    row = FixtureTenantRow(tenant_id=tenant_id, name="roundtrip")
    db_session.add(row)
    await db_session.flush()
    result = await db_session.execute(
        select(FixtureTenantRow).where(FixtureTenantRow.id == row.id)
    )
    fetched = result.scalar_one()
    assert fetched is row
    assert fetched.name == "roundtrip"
    assert fetched.tenant_id == tenant_id


async def test_tenant_filter_isolates_rows(db_session: AsyncSession) -> None:
    """显式租户过滤：A/B 两行各自只读到本租户行。"""
    from tests.conftest import FixtureTenantRow

    tenant_a = TenantId("tenant_a")
    tenant_b = TenantId("tenant_b")
    db_session.add(FixtureTenantRow(tenant_id=tenant_a, name="a-row"))
    db_session.add(FixtureTenantRow(tenant_id=tenant_b, name="b-row"))
    await db_session.flush()
    a_rows = (
        await db_session.execute(
            select(FixtureTenantRow).where(FixtureTenantRow.tenant_id == tenant_a)
        )
    ).scalars().all()
    b_rows = (
        await db_session.execute(
            select(FixtureTenantRow).where(FixtureTenantRow.tenant_id == tenant_b)
        )
    ).scalars().all()
    assert [r.name for r in a_rows] == ["a-row"]
    assert [r.name for r in b_rows] == ["b-row"]
