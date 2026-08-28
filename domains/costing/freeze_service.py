"""人工适用性确认与可恢复冻结；只依赖本域仓储和显式外部受控端口。"""

from collections.abc import Callable
from datetime import datetime

from pydantic import TypeAdapter
from pydantic import ValidationError as SchemaError

from domains.costing.errors import (
    CostFreezeError,
    CostFreezePermissionError,
    CostFreezeUnavailableError,
    InvalidPricingEvidenceError,
)
from domains.costing.freeze_repository import CostingFreezeUow, CostingFreezeUowFactory
from domains.costing.freeze_schemas import (
    CostingContext,
    CostScopeAccess,
    CostScopeConfirmationCommand,
    CostScopeConfirmationView,
    StoredCostScope,
)
from domains.costing.permissions import (
    CostingAction,
    CostingActor,
    CostingActorReader,
    Phase1CostingAuthorizer,
)
from domains.costing.quote_lock import (
    require_context,
    require_scope_evidence,
    scope_content_hash,
    validate_cost_coverage,
)
from domains.costing.schemas import CostCoverageView, PriceEvidenceView
from domains.costing.service import (
    CostScopeSourceAccess,
    NeedFactsValidator,
    QuoteCreationCompletionReader,
    cost_sheet_content_hash,
)
from shared.errors import PermissionDenied
from shared.schemas.identifiers import CostSheetId, EmployeeId, TenantId, new_id
from shared.schemas.provenance import Provenance, SourceType
from shared.schemas.quote_creation import (
    QuoteKey,
    canonical_creation_hash,
    quote_terms_hash,
)
from shared.schemas.quote_facts import fact_utc


class CostingFreezeServiceImpl:
    """每次重读当前员工，原件授权只在prepare_scope_access零锁阶段执行。"""

    def __init__(
        self,
        uow_factory: CostingFreezeUowFactory,
        actor_reader: CostingActorReader,
        need_validator: NeedFactsValidator,
        source_access: CostScopeSourceAccess,
        completion_reader: QuoteCreationCompletionReader,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._factory, self._actors, self._need = (
            uow_factory,
            actor_reader,
            need_validator,
        )
        self._sources, self._completions, self._now = (
            source_access,
            completion_reader,
            now,
        )

    async def _require(
        self,
        tenant_id: TenantId,
        actor: CostingActor,
        action: CostingAction,
        context: CostingContext | None = None,
    ) -> None:
        """拒绝过期请求身份和runtime错配，不以hash替代权限。"""
        try:
            current = await self._actors.read_current(
                tenant_id, EmployeeId(actor.actor_id)
            )
        except Exception:  # noqa: BLE001 -- 身份依赖失败固定脱敏
            raise CostFreezeUnavailableError("dependency_unavailable") from None
        try:
            if current is None or current != actor:
                raise PermissionDenied("身份已变更")
            Phase1CostingAuthorizer(tenant_id).require(actor, action, tenant_id)
            if context is not None:
                runtime = context.runtime.current_actor
                if (
                    runtime.tenant_id,
                    runtime.employee_id,
                    runtime.role,
                    runtime.is_active,
                ) != (tenant_id, actor.actor_id, actor.role, True):
                    raise PermissionDenied("上下文身份已变更")
        except PermissionDenied:
            raise CostFreezePermissionError("permission_denied") from None

    async def _evidence(
        self, uow: CostingFreezeUow, tenant_id: TenantId, coverage: CostCoverageView
    ) -> tuple[PriceEvidenceView, ...]:
        """只读明确清单引用的全部持久依据，不接受请求自带quoted事实。"""
        result = []
        for identity in sorted(
            {b.evidence_id for d in coverage.decisions for b in d.item_bindings}
        ):
            record = await uow.prices.get(tenant_id, identity)
            if record is None:
                raise CostFreezeError("evidence_invalid")
            result.append(record.value)
        return tuple(result)

    async def prepare_scope_access(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        command: CostScopeConfirmationCommand,
        *,
        actor: CostingActor,
    ) -> CostScopeAccess:
        """短UoW结束后才访问来源权限；不能在guard或成本锁内调用。"""
        await self._require(tenant_id, actor, CostingAction.SCOPE_CONFIRM)
        async with self._factory(tenant_id) as uow:
            sheet = await uow.sheets.get(tenant_id, cost_sheet_id)
            record = await uow.coverage.get(tenant_id, command.coverage_id)
            if (
                sheet is None
                or record is None
                or record.value.cost_sheet_id != cost_sheet_id
            ):
                raise CostFreezeError("coverage_stale")
            evidence = await self._evidence(uow, tenant_id, record.value)
        if len(command.evidence_bindings) != len(evidence) or {
            b.evidence_id: b.evidence_hash for b in command.evidence_bindings
        } != {p.evidence_id: p.evidence_hash for p in evidence}:
            raise CostFreezeError("evidence_invalid")
        try:
            await self._sources.require(
                tenant_id, evidence, actor_id=EmployeeId(actor.actor_id)
            )
        except PermissionDenied:
            raise CostFreezePermissionError("permission_denied") from None
        except Exception:  # noqa: BLE001 -- 来源依赖不能输出原文
            raise CostFreezeUnavailableError("dependency_unavailable") from None
        return CostScopeAccess(
            tenant_id=tenant_id,
            actor_id=EmployeeId(actor.actor_id),
            evidence_bindings=command.evidence_bindings,
        )

    async def confirm_scope(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        command: CostScopeConfirmationCommand,
        context: CostingContext,
        *,
        actor: CostingActor,
        idempotency_key: str,
        source_access: CostScopeAccess,
    ) -> CostScopeConfirmationView:
        """明确人工映射与当前完整Need绑定，保存成功前一直处于外层lease内。"""
        await self._require(tenant_id, actor, CostingAction.SCOPE_CONFIRM, context)
        try:
            key = TypeAdapter(QuoteKey).validate_python(idempotency_key)
            command = CostScopeConfirmationCommand.model_validate(
                command.model_dump(mode="python")
            )
        except (SchemaError, ValueError, TypeError):
            raise CostFreezeError("invalid_input") from None
        if (
            source_access.tenant_id,
            source_access.actor_id,
            source_access.evidence_bindings,
        ) != (tenant_id, actor.actor_id, command.evidence_bindings):
            raise CostFreezePermissionError("permission_denied")
        request_hash = canonical_creation_hash(
            {
                "version": "cost-scope-request-v1",
                "tenant_id": tenant_id,
                "cost_sheet_id": cost_sheet_id,
                "actor_id": actor.actor_id,
                "command": command,
                "need_facts_hash": context.need_facts_hash,
            }
        )
        async with self._factory(tenant_id) as uow:
            await uow.freezes.lock_key(tenant_id, "scope", key)
            previous = await uow.freezes.get_scope_by_key(tenant_id, key)
            if previous is not None:
                if previous.request_hash != request_hash:
                    raise CostFreezeError("idempotency_conflict")
                return previous.view
            sheet = await uow.sheets.get_for_update(tenant_id, cost_sheet_id)
            now = fact_utc(self._now())
            coverage = await uow.coverage.get(tenant_id, command.coverage_id)
            if (
                sheet is None
                or coverage is None
                or coverage.value.cost_sheet_id != cost_sheet_id
                or cost_sheet_content_hash(sheet) != command.expected_sheet_hash
                or coverage.value.expected_sheet_hash != command.expected_sheet_hash
                or coverage.value.content_hash != command.expected_coverage_hash
            ):
                raise CostFreezeError("coverage_stale")
            if context.need_facts_hash != command.expected_need_facts_hash:
                raise CostFreezeError("scope_stale")
            require_context(context, sheet)
            self._need.require_current_unit(context.need_facts)
            evidence = await self._evidence(uow, tenant_id, coverage.value)
            require_scope_evidence(
                context,
                evidence,
                command.evidence_bindings,
                valid_until=command.valid_until,
                now=now,
            )
            try:
                validate_cost_coverage(sheet, coverage.value, evidence, now=now)
            except InvalidPricingEvidenceError:
                raise CostFreezeError("evidence_invalid") from None
            identity = new_id("csc")
            provenance = Provenance(
                SourceType.EMPLOYEE_INPUT,
                identity,
                actor.actor_id,
                now,
                EmployeeId(actor.actor_id),
                now,
            )
            values = {
                "tenant_id": tenant_id,
                "confirmation_id": identity,
                "opportunity_id": context.opportunity_id,
                "need_id": context.need_id,
                "cost_sheet_id": cost_sheet_id,
                "sheet_hash": command.expected_sheet_hash,
                "coverage_id": command.coverage_id,
                "coverage_hash": command.expected_coverage_hash,
                "need_facts": context.need_facts,
                "need_facts_hash": context.need_facts_hash,
                "specification": context.specification,
                "specification_hash": context.specification_hash,
                "terms": command.terms,
                "terms_hash": quote_terms_hash(command.terms),
                "valid_until": command.valid_until,
                "evidence_bindings": command.evidence_bindings,
                "provenance": provenance,
            }
            result = CostScopeConfirmationView(
                **values, content_hash=scope_content_hash(values)
            )
            await uow.freezes.add_scope(
                tenant_id,
                StoredCostScope(
                    view=result, idempotency_key=key, request_hash=request_hash
                ),
            )
            return result

    async def get_scope(
        self, tenant_id: TenantId, confirmation_id: str, *, actor: CostingActor
    ) -> CostScopeConfirmationView:
        """历史内部读取不等于当前可用于报价。"""
        await self._require(tenant_id, actor, CostingAction.QUOTE_OPERATION_READ)
        async with self._factory(tenant_id) as uow:
            result = await uow.freezes.get_scope(tenant_id, confirmation_id)
            if result is None:
                raise CostFreezeError("record_not_found")
            return result
