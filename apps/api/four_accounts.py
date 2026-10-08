"""四个指定账号的可信维护流程；双租户同事务，历史身份保留但撤销访问。"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.authentication.service import PostgresAuthentication
from infra.db.tables import (
    AuthAccountRow,
    AuthRateLimitRow,
    AuthSessionRow,
    EmployeeRow,
    OwnershipLockRow,
    PlatformAdminGrantRow,
    PlatformEnterpriseRow,
)
from shared.authentication import AuthenticationInputInvalid
from shared.schemas.identifiers import EmployeeId, TenantId, UserId, new_id

ACCOUNT_NAMES = frozenset({"xue", "jslt", "qihao", "qikai"})


@dataclass(frozen=True)
class FourAccountIdentity:
    """无密码的初始化结果，供受信交付检查身份与租户。"""
    username: str
    tenant_id: TenantId
    employee_id: EmployeeId
    user_id: UserId
    role: str


async def replace_with_four_accounts(
    factory: async_sessionmaker[AsyncSession], *,
    business_tenant: TenantId, control_tenant: TenantId,
    passwords: Mapping[str, SecretStr],
    session: AsyncSession | None = None,
    allow_test_passwords: bool = False,
) -> tuple[FourAccountIdentity, ...]:
    """显式维护窗口调用；所有账号/授权/旧会话在同一事务中改变。

    不删除旧员工、历史记录或客户。存在客户归属时拒绝，先完成业务交接。
    重复初始化拒绝，绝不静默重置已经创建的四个账号。
    """
    if (
        not business_tenant or not control_tenant or business_tenant == control_tenant
        or set(passwords) != ACCOUNT_NAMES
        or any(not isinstance(value, SecretStr) for value in passwords.values())
        or (session is not None and not session.in_transaction())
    ):
        raise AuthenticationInputInvalid()
    identities = tuple(FourAccountIdentity(
        name, control_tenant if name == "xue" else business_tenant,
        EmployeeId(new_id("emp")), UserId(new_id("usr")),
        "viewer" if name == "xue" else "boss" if name == "jslt" else "sales",
    ) for name in ("xue", "jslt", "qihao", "qikai"))
    by_name = {identity.username: identity for identity in identities}

    async def apply(target: AsyncSession) -> None:
        # 与认证一致：先固定次序取得租户桶，之后才读账号和员工。
        for tenant in sorted((business_tenant, control_tenant)):
            await target.execute(insert(AuthRateLimitRow).values(
                tenant_id=tenant, bucket="attempts", count=0, started_at=func.clock_timestamp(),
            ).on_conflict_do_nothing(index_elements=["tenant_id", "bucket"]))
            await target.scalar(select(AuthRateLimitRow).where(
                AuthRateLimitRow.tenant_id == tenant, AuthRateLimitRow.bucket == "attempts",
            ).with_for_update())
            names = (await target.scalars(select(AuthAccountRow.username).where(
                AuthAccountRow.tenant_id == tenant,
            ))).all()
            if (tenant == control_tenant and names) or ACCOUNT_NAMES.intersection(names):
                raise AuthenticationInputInvalid()
        ownerships = await target.scalar(select(func.count()).select_from(OwnershipLockRow).where(
            OwnershipLockRow.tenant_id == business_tenant,
        ))
        if ownerships:
            raise AuthenticationInputInvalid()
        for identity in identities:
            target.add(EmployeeRow(
                tenant_id=identity.tenant_id, employee_id=identity.employee_id,
                user_id=identity.user_id, name=identity.username, role=identity.role,
                is_active=True, manager_id=(
                    by_name["jslt"].employee_id if identity.username in {"qihao", "qikai"} else None
                ),
            ))
        await target.flush()
        for identity in identities:
            await PostgresAuthentication(factory, identity.tenant_id).create_account(
                identity.username, passwords[identity.username], identity.employee_id, session=target,
                test_password=allow_test_passwords,
            )
        now = datetime.now(UTC)
        xue = by_name["xue"]
        target.add(PlatformAdminGrantRow(
            tenant_id=control_tenant, employee_id=xue.employee_id, user_id=xue.user_id,
            enabled=True, created_at=now,
        ))
        target.add(PlatformEnterpriseRow(
            tenant_id=control_tenant, enterprise_tenant_id=business_tenant,
            name="JSLT", enabled=True, created_at=now,
        ))
        for tenant in (business_tenant, control_tenant):
            keep = [item.employee_id for item in identities if item.tenant_id == tenant]
            await target.execute(update(AuthAccountRow).where(
                AuthAccountRow.tenant_id == tenant, AuthAccountRow.employee_id.not_in(keep),
            ).values(enabled=False, version=AuthAccountRow.version + 1))
            await target.execute(update(AuthSessionRow).where(
                AuthSessionRow.tenant_id == tenant, AuthSessionRow.revoked_at.is_(None),
            ).values(revoked_at=now))
            await target.execute(update(EmployeeRow).where(
                EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id.not_in(keep),
            ).values(is_active=False))
        await target.flush()

    if session is None:
        async with factory.begin() as owned:
            await apply(owned)
    else:
        await apply(session)
    return identities
