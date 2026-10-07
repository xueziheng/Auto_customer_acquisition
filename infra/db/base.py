"""仓储查询统一绑定企业；平台查看也必须逐企业授权，禁止无过滤后门。"""
from __future__ import annotations

from typing import Protocol, TypeVar, cast

from sqlalchemy import ColumnElement, Select, select

from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import TenantId, UserId


class TenantScopedModel(Protocol):
    """拥有租户字段的持久化模型。"""

    tenant_id: str


T = TypeVar("T", bound=TenantScopedModel)


class TenantScopedRepository:
    """构造时绑定唯一企业，所有查询保留显式企业条件。"""

    def __init__(self, tenant_id: TenantId) -> None:
        if (
            not isinstance(tenant_id, str)
            or not tenant_id
            or tenant_id != tenant_id.strip()
            or "\x00" in tenant_id
        ):
            raise TenantIsolationViolation("仓储必须绑定有效企业")
        self._tenant_id = tenant_id

    def scoped_query(self, model: type[T]) -> Select[tuple[T]]:
        """生成企业条件；数据库 RLS 是第二层边界，不替代此条件。"""
        tenant_id_col = cast(ColumnElement[str], model.tenant_id)
        return select(model).where(tenant_id_col == self._tenant_id)

    def unsafe_cross_tenant_query(
        self, model: type[T], actor_id: UserId, audit_reason: str
    ) -> Select[tuple[T]]:
        """保留旧接口的拒绝行为，防止调用方误将审计文字视为授权。"""
        del model, actor_id, audit_reason
        raise TenantIsolationViolation("无过滤跨企业查询已禁用")
