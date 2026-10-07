"""所有 Repository 的基类：租户过滤在基类统一注入（硬边界 8）。

为什么在基类注入而不是靠每个查询点自觉：靠 review 盯每条 SQL 迟早漏一条，
漏一条就是跨租户数据泄露，且测试环境发现不了（HANDBOOK「最关键的一件」）。

本基类只负责**构造**租户限定的 SQLAlchemy Select 语句，不持有会话——
执行（session.execute）与事务生命周期由调用方掌握。`unsafe_cross_tenant_query`
后门必须在调用处提供非空 actor_id 与 audit_reason，先写结构化审计日志再放行，
把后门做成显式且留痕的，比没有后门更现实。
"""
from __future__ import annotations

import logging
from typing import Protocol, TypeVar, cast

from sqlalchemy import ColumnElement, Select, select

from shared.schemas.identifiers import TenantId, UserId

# 结构化审计专用 logger：名称固定，便于收集与过滤。
_audit_logger = logging.getLogger("infra.db.audit")


class TenantScopedModel(Protocol):
    """带租户维度的模型协议：实例暴露 `tenant_id: str`。

    类级访问（`model.tenant_id`）是 SQL 表达式（InstrumentedAttribute），
    与实例上的 str 不同——查询构造处用一次收窄 cast 处理这个二象性。
    """

    tenant_id: str


T = TypeVar("T", bound=TenantScopedModel)


class TenantScopedRepository:
    """所有 Repository 的基类：构造时绑定租户，查询入口统一注入租户过滤。"""

    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id

    def scoped_query(self, model: type[T]) -> Select[tuple[T]]:
        """返回已注入 `WHERE tenant_id = :tenant_id` 的 select，调用方不得绕过该过滤。"""
        tenant_id_col = cast(ColumnElement[str], model.tenant_id)
        return select(model).where(tenant_id_col == self._tenant_id)

    def unsafe_cross_tenant_query(
        self, model: type[T], actor_id: UserId, audit_reason: str
    ) -> Select[tuple[T]]:
        """运维专用后门：跨租户查询。

        必须在调用处提供非空 `actor_id`（操作者/服务主体）与 `audit_reason`；
        任一为空白即抛 `ValueError`。校验通过后先写结构化审计日志
        （logger `infra.db.audit`，含 actor_id / bound tenant_id / audit_reason），
        再返回未过滤查询。
        """
        if not actor_id or not actor_id.strip():
            raise ValueError("actor_id 不能为空：后门调用必须记录操作者/服务主体")
        if not audit_reason or not audit_reason.strip():
            raise ValueError("audit_reason 不能为空：后门调用必须记录理由")
        _audit_logger.info(
            "unsafe_cross_tenant_query 跨租户后门调用",
            extra={
                "actor_id": actor_id,
                "tenant_id": self._tenant_id,
                "audit_reason": audit_reason,
            },
        )
        return select(model)
