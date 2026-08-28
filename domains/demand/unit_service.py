"""人工客户单位确认：先零锁核来源，再保护授权与Need原子提交。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime

from pydantic import ValidationError as SchemaError

from domains.demand.errors import (
    NeedUnitError,
    NeedUnitPermissionError,
    NeedUnitUnavailableError,
)
from domains.demand.schemas import (
    NeedQuoteFacts,
    NeedUnitAccess,
    NeedUnitAction,
    NeedUnitConfirmationCommand,
    NeedUnitConfirmationView,
    NeedUnitErrorCode,
    NeedUnitEvidenceQuery,
    NeedUnitStoredConfirmation,
    _unit_text,
    _unit_utc,
)
from domains.demand.service import NeedUnitAuthorizer, NeedUnitEvidenceReader
from domains.demand.unit_facts import canonical_hash, quantity_fact_hash
from domains.demand.unit_repository import NeedUnitUnitOfWork
from shared.errors import PermissionDenied, TradeOSError
from shared.schemas.identifiers import EmployeeId, TenantId, ValidatedNeedId, new_id
from shared.schemas.provenance import FactualField, Provenance, SourceType


async def _dependency[T](operation: Awaitable[T], code: NeedUnitErrorCode) -> T:
    """外部依赖异常按固定code脱敏，保留已分类领域失败且不自动重试。"""
    try:
        return await operation
    except NeedUnitPermissionError:
        raise
    except PermissionDenied:
        raise NeedUnitPermissionError("permission_denied") from None
    except (NeedUnitError, NeedUnitUnavailableError, SchemaError):
        raise
    except Exception:  # noqa: BLE001 -- 外部依赖的自由异常不得进入日志/HTTP
        raise NeedUnitUnavailableError(code) from None


class NeedUnitServiceImpl:
    """依赖必须显式注入；测试reader绝不作为生产默认。"""

    def __init__(
        self,
        uow_factory: Callable[[TenantId], NeedUnitUnitOfWork],
        authorizer: NeedUnitAuthorizer,
        evidence_reader: NeedUnitEvidenceReader,
        *,
        now: Callable[[], datetime],
    ) -> None:
        """声明权限、来源、事务和时间依赖，无默认外部调用。"""
        self._uow = uow_factory
        self._authorizer = authorizer
        self._reader = evidence_reader
        self._now = now

    async def confirm(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        command: NeedUnitConfirmationCommand,
        *,
        actor_id: EmployeeId,
        idempotency_key: str,
    ) -> NeedUnitConfirmationView:
        """按check→短读→来源IO→guard→锁Need→提交顺序确认，不自动重试。"""
        access = await _dependency(
            self._authorizer.check(tenant_id, need_id, actor_id, action="confirm"),
            "dependency_unavailable",
        )
        self._access(access, tenant_id, need_id, actor_id)
        try:
            _unit_text(idempotency_key)
            if len(idempotency_key) > 128:
                raise ValueError
        except (TypeError, AttributeError, ValueError):
            raise NeedUnitError("invalid_input") from None
        request_hash = canonical_hash(
            {
                "version": "need-unit-confirm-request-v1",
                "tenant_id": tenant_id,
                "need_id": need_id,
                "actor_id": actor_id,
                "command": command,
            }
        )
        async with self._uow(tenant_id) as uow:
            stored = await uow.units.find_operation(tenant_id, need_id, idempotency_key)
            current = None if stored else await uow.units.read_facts(tenant_id, need_id)
        if stored:
            return await self._replay(access, stored, request_hash)
        current = self._current(current, access, command)
        if command.unit.casefold() in {"unit", "units", "unknown", "未知", "待确认"}:
            raise NeedUnitError("unit_unspecified")
        assert current.quantity is not None
        query = NeedUnitEvidenceQuery(
            tenant_id=tenant_id,
            need_id=need_id,
            account_id=current.account_id,
            actor_id=actor_id,
            quantity=current.quantity,
            quantity_fact_hash=command.expected_quantity_fact_hash,
            unit=command.unit,
            source_message_id=command.source_message_id,
            locator=command.locator,
            source_quote=command.source_quote,
        )
        source = await _dependency(
            self._reader.read_verified(query), "source_unavailable"
        )
        for name in (
            "tenant_id",
            "need_id",
            "account_id",
            "source_message_id",
            "locator",
            "source_quote",
            "unit",
            "quantity_fact_hash",
        ):
            if getattr(source, name) != getattr(query, name):
                raise NeedUnitError("source_mismatch")
        winner = None
        async with self._guard(
            tenant_id, need_id, actor_id, action="confirm"
        ) as guarded:
            self._access(guarded, tenant_id, need_id, actor_id)
            if (
                guarded.account_id != access.account_id
                or guarded.opportunity_id != access.opportunity_id
            ):
                raise NeedUnitPermissionError("permission_denied")
            async with self._uow(tenant_id) as uow:
                locked = await uow.units.lock_facts(tenant_id, need_id)
                winner = await uow.units.find_operation(
                    tenant_id, need_id, idempotency_key
                )
                if winner is None:
                    locked = self._current(locked, guarded, command)
                    confirmed_at = _unit_utc(self._now())
                    unit = FactualField(
                        command.unit,
                        Provenance(
                            source_type=SourceType.CONVERSATION,
                            source_id=str(source.source_message_id),
                            extracted_by=str(actor_id),
                            extracted_at=confirmed_at,
                            confirmed_by=actor_id,
                            confirmed_at=confirmed_at,
                            source_quote=source.source_quote,
                        ),
                    )
                    result = NeedUnitConfirmationView(
                        tenant_id=tenant_id,
                        need_id=need_id,
                        confirmation_id=new_id("nuc"),
                        quantity_fact_hash=command.expected_quantity_fact_hash,
                        unit=unit,
                        source=source,
                        confirmed_by=actor_id,
                        confirmed_at=confirmed_at,
                    )
                    await uow.units.add_confirmation(
                        tenant_id,
                        NeedUnitStoredConfirmation(
                            view=result,
                            idempotency_key=idempotency_key,
                            request_hash=request_hash,
                        ),
                    )
                    await uow.units.apply_current_unit(tenant_id, need_id, result)
                    await uow.units.append_unit_history(
                        tenant_id, need_id, locked.unit, result
                    )
        if winner is not None:
            return await self._replay(guarded, winner, request_hash)
        return result

    async def get_facts(
        self, tenant_id: TenantId, need_id: ValidatedNeedId, *, actor_id: EmployeeId
    ) -> NeedQuoteFacts:
        """完整事实须当前需求阅读权；不替调用方声明单位有效。"""
        access = await _dependency(
            self._authorizer.check(tenant_id, need_id, actor_id, action="read"),
            "dependency_unavailable",
        )
        self._access(access, tenant_id, need_id, actor_id)
        async with self._uow(tenant_id) as uow:
            result = await uow.units.read_facts(tenant_id, need_id)
        return self._scope(result, access)

    async def get_confirmation(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        confirmation_id: str,
        *,
        actor_id: EmployeeId,
    ) -> NeedUnitConfirmationView:
        """历史确认额外重验当前资料权限，旧receipt不等于当前有效。"""
        access = await _dependency(
            self._authorizer.check(tenant_id, need_id, actor_id, action="read"),
            "dependency_unavailable",
        )
        self._access(access, tenant_id, need_id, actor_id)
        async with self._uow(tenant_id) as uow:
            result = await uow.units.get_confirmation(
                tenant_id, need_id, confirmation_id
            )
        if result is None:
            raise NeedUnitError("confirmation_not_found")
        await self._reference(access, result)
        return result

    @staticmethod
    def _access(
        access: NeedUnitAccess,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        actor_id: EmployeeId,
    ) -> None:
        """不信任适配器返回的另一身份/租户绑定。"""
        if (access.tenant_id, access.need_id, access.actor_id) != (
            tenant_id,
            need_id,
            actor_id,
        ):
            raise NeedUnitPermissionError("permission_denied")

    @staticmethod
    def _scope(facts: NeedQuoteFacts | None, access: NeedUnitAccess) -> NeedQuoteFacts:
        """确认事实与授权客户一致，不泄露其他资源存在性。"""
        if facts is None:
            raise NeedUnitError("need_not_found")
        if (facts.tenant_id, facts.need_id, facts.account_id) != (
            access.tenant_id,
            access.need_id,
            access.account_id,
        ):
            raise NeedUnitPermissionError("permission_denied")
        return facts

    def _current(
        self,
        facts: NeedQuoteFacts | None,
        access: NeedUnitAccess,
        command: NeedUnitConfirmationCommand,
    ) -> NeedQuoteFacts:
        """两次检查相同前置：终态、正数量、人工确认及乐观绑定。"""
        facts = self._scope(facts, access)
        if facts.status in {"fulfilled", "withdrawn", "lost"}:
            raise NeedUnitError("need_terminal")
        quantity = facts.quantity
        if quantity is None or type(quantity.value) is not int or quantity.value <= 0:
            raise NeedUnitError("quantity_invalid")
        if not quantity.provenance.is_human_confirmed:
            raise NeedUnitError("fact_unconfirmed")
        if (
            quantity_fact_hash(facts.tenant_id, facts.need_id, quantity)
            != command.expected_quantity_fact_hash
        ):
            raise NeedUnitError("quantity_changed")
        if facts.unit_confirmation_id != command.expected_unit_confirmation_id:
            raise NeedUnitError("unit_changed")
        return facts

    async def _reference(
        self, access: NeedUnitAccess, result: NeedUnitConfirmationView
    ) -> None:
        """锁外重验receipt范围与当前原件权限，不读取原文。"""
        if (result.tenant_id, result.need_id, result.source.account_id) != (
            access.tenant_id,
            access.need_id,
            access.account_id,
        ):
            raise NeedUnitPermissionError("permission_denied")
        await _dependency(
            self._reader.authorize_reference(
                access.tenant_id, access.need_id, access.actor_id, result.source
            ),
            "source_unavailable",
        )

    @asynccontextmanager
    async def _guard(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        actor_id: EmployeeId,
        *,
        action: NeedUnitAction,
    ) -> AsyncIterator[NeedUnitAccess]:
        """授权适配异常脱敏；内层领域/租户错误保持原语义。"""
        try:
            async with self._authorizer.guard(
                tenant_id, need_id, actor_id, action=action
            ) as access:
                yield access
        except NeedUnitPermissionError:
            raise
        except PermissionDenied:
            raise NeedUnitPermissionError("permission_denied") from None
        except (TradeOSError, SchemaError):
            raise
        except Exception:  # noqa: BLE001 -- 授权依赖错误只给固定不可用码
            raise NeedUnitUnavailableError("dependency_unavailable") from None

    async def _replay(
        self,
        access: NeedUnitAccess,
        stored: NeedUnitStoredConfirmation,
        request_hash: str,
    ) -> NeedUnitConfirmationView:
        """同payload仅回首次receipt，不改变当前unit也不重读原文。"""
        if stored.request_hash != request_hash:
            raise NeedUnitError("idempotency_conflict")
        await self._reference(access, stored.view)
        return stored.view
