"""老板指令域存储接口。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from typing import Protocol, Self, runtime_checkable

from domains.directives.models import Directive, DirectiveProposal
from shared.events.bus import EventBus
from shared.schemas.identifiers import AgentTurnId, DirectiveId, TenantId


@runtime_checkable
class ProposalRepository(Protocol):
    async def add_once(
        self,
        proposal: DirectiveProposal,
        source_turn_id: AgentTurnId,
        source_version: int,
        request_hmac: str,
    ) -> str: ...

    async def add(self, proposal: DirectiveProposal) -> None: ...

    async def get(
        self, tenant_id: TenantId, proposal_id: str
    ) -> DirectiveProposal | None: ...

    async def get_for_update(
        self, tenant_id: TenantId, proposal_id: str
    ) -> DirectiveProposal | None: ...

    async def update(self, proposal: DirectiveProposal) -> None: ...

    async def list_pending(self, tenant_id: TenantId) -> list[DirectiveProposal]: ...

    async def list_rejected(
        self, tenant_id: TenantId, limit: int
    ) -> list[DirectiveProposal]:
        """被否决的提案。改进解析 prompt 的素材来源，
        也是 ``tests/evals`` 指令解析样本的采集口。"""
        ...


@runtime_checkable
class DirectiveRepository(Protocol):
    async def add(self, directive: Directive) -> None:
        """存新版本。**只增**——不提供删除，不提供原地更新内容的方法。
        唯一允许的更新是给前一版本盖 ``superseded_at`` 戳。"""
        ...

    async def get_active(self, tenant_id: TenantId) -> Directive | None:
        """当前生效版本（``superseded_at`` 为空的那个，有且最多一个）。

        这个查询全系统高频调用，值得缓存；但**缓存失效必须挂在
        ``DirectiveActivated`` 事件上**，不能用 TTL——指令生效后
        还按旧配置跑几分钟是不可接受的。
        """
        ...

    async def get_active_for_update(self, tenant_id: TenantId) -> Directive | None: ...

    async def get_version(
        self, tenant_id: TenantId, version: int
    ) -> Directive | None: ...

    async def next_version(self, tenant_id: TenantId) -> int: ...

    async def mark_superseded(
        self, tenant_id: TenantId, directive_id: DirectiveId
    ) -> None: ...

    async def set_active(self, directive: Directive) -> None: ...

    async def list_versions(
        self, tenant_id: TenantId, limit: int
    ) -> list[Directive]: ...


@runtime_checkable
class DirectiveUnitOfWork(Protocol):
    proposals: ProposalRepository
    directives: DirectiveRepository
    bus: EventBus

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None: ...


@runtime_checkable
class DirectiveUnitOfWorkFactory(Protocol):
    def __call__(self, tenant_id: TenantId) -> DirectiveUnitOfWork: ...
