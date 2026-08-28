"""人工适用性确认与可恢复冻结；只依赖本域仓储和显式外部受控端口。"""

from collections.abc import Callable
from datetime import datetime

from pydantic import TypeAdapter
from pydantic import ValidationError as SchemaError

from domains.costing.calculation import compute_breakdown
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
    FrozenCostBasis,
    StoredCostScope,
)
from domains.costing.models import CostSheet, CostSheetVersion, MarginRule
from domains.costing.permissions import (
    CostingAction,
    CostingActor,
    CostingActorReader,
    Phase1CostingAuthorizer,
)
from domains.costing.quote_lock import (
    basis_content_hash,
    require_completion,
    require_context,
    require_evidence_applicability,
    require_scope_evidence,
    require_scope_integrity,
    scope_content_hash,
    validate_cost_coverage,
)
from domains.costing.schemas import (
    CalculationSnapshot,
    CostCoverageView,
    PriceEvidenceView,
    PricingOptions,
    PricingPolicyView,
    QuoteFxView,
)
from domains.costing.service import (
    CostScopeSourceAccess,
    NeedFactsValidator,
    QuoteCreationCompletionReader,
    cost_sheet_content_hash,
)
from shared.errors import PermissionDenied
from shared.errors import ValidationError as DomainValidationError
from shared.schemas.identifiers import CostSheetId, EmployeeId, TenantId, new_id
from shared.schemas.money import FxRate
from shared.schemas.provenance import Provenance, SourceType
from shared.schemas.quote_creation import (
    QuoteCreationCompletion,
    QuoteCreationIntent,
    QuoteCreationOperationView,
    QuoteKey,
    canonical_creation_hash,
    quote_creation_request_hash,
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
            now = self._clock()
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

    def _clock(self) -> datetime:
        """只在本轮全部成本锁取得后读取一次明确时钟。"""
        try:
            return fact_utc(self._now())
        except (ValueError, TypeError, AttributeError):
            raise CostFreezeError("invalid_input") from None

    async def _pricing(
        self,
        uow: CostingFreezeUow,
        tenant_id: TenantId,
        sheet: CostSheet,
        context: CostingContext,
        options: PricingOptions,
        quote_fx_ref: str | None,
        now: datetime,
    ) -> tuple[PricingPolicyView, PricingOptions, QuoteFxView | None]:
        """只读取已确认政策与直连报价FX，不信任调用方自由来源字符串。"""
        try:
            policy = await uow.policies.get_effective(tenant_id, context.category, now)
            if policy is None:
                raise CostFreezeError("policy_missing")
            if sheet.base_currency == sheet.quote_currency:
                if quote_fx_ref is not None:
                    raise CostFreezeError("fx_missing")
                if options.quote_fx is not None and (
                    options.quote_fx.base,
                    options.quote_fx.quote,
                    options.quote_fx.rate,
                ) != (sheet.base_currency, sheet.quote_currency, 1):
                    raise CostFreezeError("fx_missing")
                return policy.value, options, None
            if quote_fx_ref is None:
                raise CostFreezeError("fx_missing")
            fx = await uow.quote_fx.get(tenant_id, quote_fx_ref)
            if fx is None or (fx.value.base_currency, fx.value.quote_currency) != (
                sheet.base_currency,
                sheet.quote_currency,
            ):
                raise CostFreezeError("fx_missing")
            value = fx.value
            if value.confirmed_at > now or value.observed_at > now:
                raise CostFreezeError("fx_missing")
            rate = FxRate(
                base=value.base_currency,
                quote=value.quote_currency,
                rate=value.rate,
                observed_at=value.observed_at,
                source=value.source_ref,
            )
            if options.quote_fx is not None and options.quote_fx != rate:
                raise CostFreezeError("fx_missing")
            return policy.value, options.model_copy(update={"quote_fx": rate}), value
        except (SchemaError, InvalidPricingEvidenceError, ValueError, TypeError):
            raise CostFreezeError("facts_corrupt") from None

    def _calculate(
        self,
        sheet: CostSheet,
        context: CostingContext,
        policy: PricingPolicyView,
        coverage: CostCoverageView,
        options: PricingOptions,
        now: datetime,
    ) -> CalculationSnapshot:
        """唯一T1计算入口；低利润保持真实数字，不授予审批例外。"""
        if (
            sheet.version_type is not CostSheetVersion.QUOTED
            or not sheet.fx_snapshot_id
        ):
            raise CostFreezeError("evidence_invalid")
        rule = MarginRule(
            tenant_id=sheet.tenant_id,
            category=policy.category,
            effective_from=policy.effective_from,
            minimum_margin_rate=policy.minimum_margin_rate,
            target_margin_rate=policy.target_margin_rate,
        )
        try:
            return compute_breakdown(
                sheet,
                rule,
                policy=policy,
                options=options,
                coverage_hash=coverage.content_hash,
                context_hash=context.context_hash,
                now=now,
            )
        except DomainValidationError:
            raise CostFreezeError("invalid_input") from None

    async def calculate(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        options: PricingOptions,
        context: CostingContext,
        *,
        quote_fx_ref: str | None,
        actor: CostingActor,
    ) -> CalculationSnapshot:
        """同lease下只读测算；当前清单必须精确匹配sheet hash，不写冻结标记。"""
        await self._require(tenant_id, actor, CostingAction.QUOTE_CALCULATE, context)
        async with self._factory(tenant_id) as uow:
            sheet = await uow.sheets.get_for_update(tenant_id, cost_sheet_id)
            if sheet is None:
                raise CostFreezeError("record_not_found")
            await uow.policies.lock_selection(tenant_id, exclusive=False)
            now = self._clock()
            require_context(context, sheet)
            self._need.require_current_unit(context.need_facts)
            coverage = await uow.coverage.get_for_sheet_hash(
                tenant_id, cost_sheet_id, cost_sheet_content_hash(sheet)
            )
            if coverage is None:
                raise CostFreezeError("coverage_stale")
            evidence = await self._evidence(uow, tenant_id, coverage.value)
            try:
                validate_cost_coverage(sheet, coverage.value, evidence, now=now)
            except InvalidPricingEvidenceError:
                raise CostFreezeError("evidence_invalid") from None
            require_evidence_applicability(context, evidence, valid_until=None, now=now)
            policy, resolved, _ = await self._pricing(
                uow, tenant_id, sheet, context, options, quote_fx_ref, now
            )
            return self._calculate(
                sheet, context, policy, coverage.value, resolved, now
            )

    async def freeze(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        options: PricingOptions,
        context: CostingContext,
        *,
        idempotency_key: str,
        intent: QuoteCreationIntent,
        actor: CostingActor,
    ) -> FrozenCostBasis:
        """完整意图、scope和成本同事务冻结；同键可恢复，新键不能绕过pending。"""
        await self._require(tenant_id, actor, CostingAction.QUOTE_FREEZE, context)
        try:
            key = TypeAdapter(QuoteKey).validate_python(idempotency_key)
            intent = QuoteCreationIntent.model_validate_json(intent.model_dump_json())
            options = PricingOptions.model_validate_json(options.model_dump_json())
        except (SchemaError, ValueError, TypeError):
            raise CostFreezeError("invalid_input") from None
        request_hash = quote_creation_request_hash(intent)
        async with self._factory(tenant_id) as uow:
            await uow.freezes.lock_key(tenant_id, "creation", key)
            winner = await uow.freezes.get_operation_by_key(tenant_id, key)
            if winner is not None and winner.request_hash != request_hash:
                raise CostFreezeError("idempotency_conflict")
            if (
                intent.tenant_id,
                intent.cost_sheet_id,
                intent.opportunity_id,
                intent.prepared_by,
            ) != (
                tenant_id,
                cost_sheet_id,
                context.opportunity_id,
                actor.actor_id,
            ) or context.prepared_by != intent.prepared_by:
                raise CostFreezeError("invalid_input")
            if (
                options.mode != "manual"
                or options.unit_price != intent.unit_price
                or options.rounding.model_dump() != intent.rounding.model_dump()
            ):
                raise CostFreezeError("invalid_input")
            sheet = await uow.sheets.get_for_update(tenant_id, cost_sheet_id)
            if sheet is None:
                raise CostFreezeError("record_not_found")
            pending = await uow.freezes.pending_for_sheet(tenant_id, cost_sheet_id)
            if pending is not None and (
                winner is None or pending.operation_id != winner.operation_id
            ):
                raise CostFreezeError("operation_pending")
            await uow.policies.lock_selection(tenant_id, exclusive=False)
            now = self._clock()
            scope = await uow.freezes.get_scope(tenant_id, intent.scope_confirmation_id)
            if scope is None:
                raise CostFreezeError("scope_stale")
            require_scope_integrity(scope)
            if (
                scope.content_hash,
                scope.tenant_id,
                scope.cost_sheet_id,
                scope.opportunity_id,
                scope.need_id,
                scope.need_facts_hash,
                scope.specification,
                scope.specification_hash,
                scope.terms,
                scope.valid_until,
            ) != (
                intent.scope_confirmation_hash,
                tenant_id,
                cost_sheet_id,
                context.opportunity_id,
                context.need_id,
                context.need_facts_hash,
                context.specification,
                context.specification_hash,
                intent.terms,
                intent.valid_until,
            ):
                raise CostFreezeError("scope_stale")
            if intent.expected_context_hash != context.context_hash:
                raise CostFreezeError("context_changed")
            sheet_hash = cost_sheet_content_hash(sheet)
            if (
                sheet_hash != intent.expected_sheet_hash
                or scope.sheet_hash != sheet_hash
            ):
                raise CostFreezeError("coverage_stale")
            require_context(context, sheet)
            self._need.require_current_unit(context.need_facts)
            coverage = await uow.coverage.get(tenant_id, scope.coverage_id)
            if coverage is None or (
                coverage.value.content_hash,
                coverage.value.cost_sheet_id,
                coverage.value.expected_sheet_hash,
            ) != (scope.coverage_hash, cost_sheet_id, sheet_hash):
                raise CostFreezeError("coverage_stale")
            evidence = await self._evidence(uow, tenant_id, coverage.value)
            require_scope_evidence(
                context,
                evidence,
                scope.evidence_bindings,
                valid_until=intent.valid_until,
                now=now,
            )
            try:
                validate_cost_coverage(sheet, coverage.value, evidence, now=now)
            except InvalidPricingEvidenceError:
                raise CostFreezeError("evidence_invalid") from None
            policy, resolved, fx = await self._pricing(
                uow, tenant_id, sheet, context, options, intent.quote_fx_ref, now
            )
            calculation = self._calculate(
                sheet, context, policy, coverage.value, resolved, now
            )
            if winner is not None:
                basis = await uow.freezes.get_basis(tenant_id, winner.basis_id)
                if basis is None:
                    raise CostFreezeError("facts_corrupt")
                if basis.calculation.inputs_hash != calculation.inputs_hash:
                    raise CostFreezeError("context_changed")
                return basis
            if sheet.locked_at is not None:
                if (
                    intent.replaces_quote_id is None
                    or intent.expected_quote_version is None
                ):
                    raise CostFreezeError("revision_conflict")
                previous = await uow.freezes.completed_for_revision(
                    tenant_id,
                    cost_sheet_id,
                    intent.replaces_quote_id,
                    intent.expected_quote_version,
                )
                if (
                    previous is None
                    or previous.intent.scope_confirmation_id == scope.confirmation_id
                ):
                    raise CostFreezeError("revision_conflict")
            operation_id = new_id("qco")
            values = {
                "tenant_id": tenant_id,
                "basis_id": new_id("qcb"),
                "operation_id": operation_id,
                "request_hash": request_hash,
                "opportunity_id": context.opportunity_id,
                "cost_sheet_id": cost_sheet_id,
                "context_hash": context.context_hash,
                "sheet_hash": sheet_hash,
                "policy_id": policy.policy_id,
                "quantity": context.quantity,
                "specification": context.specification,
                "unit": context.unit,
                "destination": context.destination,
                "need_facts": context.need_facts,
                "scope_confirmation": scope,
                "policy": policy,
                "coverage": coverage.value,
                "calculation": calculation,
                "price_evidence": evidence,
                "pricing_options": resolved,
                "cost_fx_rates": sheet.fx_rates,
                "quote_fx": fx,
                "valid_until": intent.valid_until,
                "frozen_at": now,
            }
            basis = FrozenCostBasis(**values, basis_hash=basis_content_hash(values))
            operation = QuoteCreationOperationView(
                tenant_id=tenant_id,
                operation_id=operation_id,
                idempotency_key=key,
                request_hash=request_hash,
                intent=intent,
                basis_id=basis.basis_id,
                state="frozen",
                created_at=now,
                completion=None,
                completed_at=None,
            )
            await uow.freezes.add_frozen(tenant_id, basis, operation)
            await uow.freezes.mark_sheet_locked_once(tenant_id, cost_sheet_id, now)
            return basis

    async def get_frozen(
        self, tenant_id: TenantId, basis_id: str, *, actor: CostingActor
    ) -> FrozenCostBasis:
        """仅内部历史读取，不等于当前报价授权。"""
        await self._require(tenant_id, actor, CostingAction.QUOTE_OPERATION_READ)
        async with self._factory(tenant_id) as uow:
            result = await uow.freezes.get_basis(tenant_id, basis_id)
            if result is None:
                raise CostFreezeError("record_not_found")
            return result

    async def get_creation(
        self, tenant_id: TenantId, idempotency_key: str, *, actor: CostingActor
    ) -> QuoteCreationOperationView | None:
        """原键恢复入口，读取完整意图与首次回执，不创建新键。"""
        await self._require(tenant_id, actor, CostingAction.QUOTE_OPERATION_READ)
        try:
            key = TypeAdapter(QuoteKey).validate_python(idempotency_key)
        except (SchemaError, ValueError, TypeError):
            raise CostFreezeError("invalid_input") from None
        async with self._factory(tenant_id) as uow:
            return await uow.freezes.get_operation_by_key(tenant_id, key)

    async def complete_creation(
        self, tenant_id: TenantId, operation_id: str, *, actor: CostingActor
    ) -> QuoteCreationOperationView:
        """可信receipt在零锁阶段读取；仅同绑定首次完结，未知提交仍按原键恢复。"""
        await self._require(tenant_id, actor, CostingAction.QUOTE_OPERATION_COMPLETE)
        async with self._factory(tenant_id) as uow:
            original = await uow.freezes.get_operation(tenant_id, operation_id)
        if original is None:
            raise CostFreezeError("record_not_found")
        try:
            receipt = await self._completions.read(
                tenant_id, operation_id, actor_id=EmployeeId(actor.actor_id)
            )
        except PermissionDenied:
            raise CostFreezePermissionError("permission_denied") from None
        except Exception:  # noqa: BLE001 -- 可信外部回执依赖必须固定脱敏
            raise CostFreezeUnavailableError("dependency_unavailable") from None
        if receipt is None:
            raise CostFreezeError("operation_pending")
        try:
            receipt = QuoteCreationCompletion.model_validate(
                receipt.model_dump(mode="python")
            )
        except (SchemaError, ValueError, TypeError, AttributeError):
            raise CostFreezeError("revision_conflict") from None
        await self._require(tenant_id, actor, CostingAction.QUOTE_OPERATION_COMPLETE)
        async with self._factory(tenant_id) as uow:
            await uow.freezes.lock_key(tenant_id, "creation", original.idempotency_key)
            await uow.sheets.get_for_update(tenant_id, original.intent.cost_sheet_id)
            current = await uow.freezes.get_operation(tenant_id, operation_id)
            if current is None:
                raise CostFreezeError("facts_corrupt")
            require_completion(current, receipt)
            await uow.freezes.complete(tenant_id, operation_id, receipt, self._clock())
            result = await uow.freezes.get_operation(tenant_id, operation_id)
            if result is None:
                raise CostFreezeError("facts_corrupt")
            return result
