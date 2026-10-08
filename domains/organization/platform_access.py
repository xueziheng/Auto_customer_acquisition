"""平台管理员的独立授权与只读企业概览；不向企业工作台授予跨企业权限。"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from shared.authentication import AuthPrincipal
from shared.errors import PermissionDenied
from shared.schemas.identifiers import TenantId


class PlatformIdentity(BaseModel):
    """仅由控制租户当前授权记录生成的安全身份。"""
    model_config = ConfigDict(frozen=True, extra="forbid")
    username: str
    display_name: str
    role: Literal["platform_admin"] = "platform_admin"


class EnterpriseDirectoryEntry(BaseModel):
    """平台控制租户持有的企业目录，不包含企业连接或凭证。"""
    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: TenantId
    name: str
    enabled: bool


class PlatformMember(BaseModel):
    """概览允许公开的成员姓名及企业身份，不含登录或私人资料。"""
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    role: Literal["admin", "employee"]


class EnterpriseOverview(EnterpriseDirectoryEntry):
    """不可取得的统计保留 null，不把故障或未配置表示为零。"""
    available: bool
    active_members: int | None = Field(default=None, ge=0)
    admins: int | None = Field(default=None, ge=0)
    employees: int | None = Field(default=None, ge=0)
    customers: int | None = Field(default=None, ge=0)
    validated_needs: int | None = Field(default=None, ge=0)
    opportunities: int | None = Field(default=None, ge=0)
    active_tasks: int | None = Field(default=None, ge=0)
    members: tuple[PlatformMember, ...] = ()


class PlatformOverview(BaseModel):
    """各企业独立只读快照；generated_at 为概览组装时间。"""
    model_config = ConfigDict(frozen=True, extra="forbid")
    generated_at: datetime
    enterprises: tuple[EnterpriseOverview, ...]


class PlatformAccessRepository(Protocol):
    """控制租户授权、目录和审计；实现必须显式限定控制租户。"""

    async def authorize(self, principal: AuthPrincipal) -> PlatformIdentity:
        """校验当前 grant、账号启用状态以及 employee/user 的精确绑定。"""
        ...

    async def list_enterprises(self) -> tuple[EnterpriseDirectoryEntry, ...]:
        """只读取控制租户目录，不扫描所有企业业务表。"""
        ...

    async def record_overview(
        self, principal: AuthPrincipal, enterprises: tuple[EnterpriseOverview, ...],
    ) -> None:
        """返回前重新校验授权并原子记录逐企业只读审计；失败不得交付数据。"""
        ...


class EnterpriseOverviewReader(Protocol):
    """固定企业只读端口，不提供任何写方法。"""

    async def read(self, enterprise: EnterpriseDirectoryEntry) -> EnterpriseOverview:
        """以同一企业只读事务返回安全统计；错误结果不伪装为零。"""
        ...


class PlatformAccessService:
    """明确平台身份才可读取概览，企业角色或名称不能替代平台授权。"""

    def __init__(
        self, control_tenant: TenantId, repository: PlatformAccessRepository,
        readers: dict[TenantId, EnterpriseOverviewReader],
    ) -> None:
        self._tenant = control_tenant
        self._repository = repository
        self._readers = dict(readers)
        if control_tenant in readers:
            raise ValueError("平台控制租户不得作为业务企业读取")

    async def authorize(self, principal: AuthPrincipal) -> PlatformIdentity:
        """每次请求重新验证独立平台授权。"""
        if principal.tenant_id != self._tenant:
            raise PermissionDenied("平台权限不足")
        return await self._repository.authorize(principal)

    async def overview(self, principal: AuthPrincipal) -> PlatformOverview:
        """仅遍历已登记企业，以当前授权复核与审计完成作为返回条件。"""
        await self.authorize(principal)
        enterprises = await self._repository.list_enterprises()
        results = []
        for enterprise in enterprises:
            if enterprise.tenant_id == self._tenant:
                raise PermissionDenied("平台目录无效")
            reader = self._readers.get(enterprise.tenant_id)
            if reader is None or not enterprise.enabled:
                result = EnterpriseOverview(**enterprise.model_dump(), available=False)
            else:
                result = await reader.read(enterprise)
                if (
                    result.tenant_id != enterprise.tenant_id
                    or result.name != enterprise.name
                    or result.enabled != enterprise.enabled
                ):
                    raise PermissionDenied("平台企业范围无效")
            results.append(result)
        rows = tuple(results)
        await self._repository.record_overview(principal, rows)
        return PlatformOverview(generated_at=datetime.now(UTC), enterprises=rows)
