"""报价准备的人工事实确认；外部原文由可信受控 reader 提供。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Literal, cast

from pydantic import BaseModel

from domains.costing.calculation import canonical_pricing_hash
from domains.costing.errors import CostCoverageConflict, InvalidPricingEvidenceError
from domains.costing.models import CostSheet
from domains.costing.permissions import (
    CostingAction,
    CostingActor,
    CostingActorReader,
    Phase1CostingAuthorizer,
)
from domains.costing.quote_repository import EvidenceRecord
from domains.costing.repository import CostingUnitOfWork, CostingUnitOfWorkFactory
from domains.costing.schemas import (
    CostCoverageCreate,
    CostCoverageView,
    ExpenseEvidenceCreate,
    ExpenseEvidenceView,
    PriceEvidenceCreate,
    PriceEvidenceView,
    PricingPolicyCreate,
    PricingPolicyView,
    QuoteFxCreate,
    QuoteFxView,
    SourceEvidence,
    SupplierPriceEvidenceCreate,
    SupplierPriceEvidenceView,
)
from domains.costing.service import PricingEvidenceReader, cost_sheet_content_hash
from shared.errors import IdempotencyConflict, PermissionDenied, ValidationError
from shared.schemas.identifiers import CostSheetId, EmployeeId, TenantId, new_id
from shared.schemas.provenance import Provenance, SourceType


def provenance_fields(payload: dict[str, object], prefix: str = "") -> tuple[str, ...]:
    """逐个业务叶字段生成可追溯路径，包含每类费用的核算归类。"""
    fields: list[str] = []
    for key, value in payload.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            fields.extend(provenance_fields(value, name))
        elif isinstance(value, (list, tuple)) and value:
            fields.extend(
                provenance_fields({str(i): item for i, item in enumerate(value)}, name)
            )
        else:
            fields.append(name)
    return tuple(fields)


def confirmed_payload(
    command: BaseModel, source: SourceEvidence, actor: CostingActor, now: datetime
) -> dict[str, object]:
    """把本次人工确认绑定到真实来源；不把模型提取写成最终事实。"""
    payload = command.model_dump(mode="python")
    provenance = Provenance(
        source_type=SourceType(source.source_type),
        source_id=source.source_ref,
        extracted_by="human",
        extracted_at=now,
        confirmed_by=EmployeeId(actor.actor_id),
        confirmed_at=now,
        source_url=source.source_url,
        page_hash=source.content_hash if source.source_type == "web_page" else None,
    )
    return {
        **payload,
        "source": source,
        "confirmed_by": actor.actor_id,
        "confirmed_at": now,
        "field_provenance": {field: provenance for field in provenance_fields(payload)},
    }


def view_content_hash(view: BaseModel) -> str:
    """确认内容身份包含来源和确认事实，但不包含随机记录 ID。"""
    return canonical_pricing_hash(
        view.model_dump(
            mode="python",
            exclude={
                "policy_id",
                "evidence_id",
                "fx_id",
                "coverage_id",
                "content_hash",
                "evidence_hash",
            },
        )
    )


class CostingQuoteServiceImpl:
    """确认前及来源读取后均检查当前员工，写入使用同事务幂等锁。"""

    def __init__(
        self,
        uow_factory: CostingUnitOfWorkFactory,
        evidence_reader: PricingEvidenceReader,
        *,
        actor_reader: CostingActorReader,
        now: Callable[[], datetime],
    ) -> None:
        self._factory = uow_factory
        self._reader = evidence_reader
        self._actors = actor_reader
        self._now = now

    def _clock(self) -> datetime:
        """注入时钟必须含时区，不能悄悄替换成服务器时间。"""
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValidationError("确认时间必须含时区")
        return now

    async def _require(
        self, tenant_id: TenantId, actor: CostingActor, action: CostingAction
    ) -> None:
        """当前身份与服务端传入身份必须一致，离职和角色变化默认拒绝。"""
        try:
            current = await self._actors.read_current(tenant_id, actor.actor_id)
        except Exception as exc:
            raise PermissionDenied("无法核验当前员工身份") from exc
        if current is None or current != actor:
            raise PermissionDenied("当前员工已离职或身份已变更")
        Phase1CostingAuthorizer(tenant_id).require(current, action, tenant_id)

    async def _source(
        self, tenant_id: TenantId, source_ref: str, locator: str, *, actor: CostingActor
    ) -> SourceEvidence:
        """读取并检查可信安全投影，不读取原文或凭证，不推断来源类别。"""
        try:
            source = await self._reader.read_verified(
                tenant_id, source_ref, locator, actor_id=EmployeeId(actor.actor_id)
            )
            source = SourceEvidence.model_validate(source.model_dump(mode="python"))
        except Exception as exc:
            raise InvalidPricingEvidenceError("无法核验来源") from exc
        if (
            source.tenant_id != tenant_id
            or source.source_ref != source_ref
            or source.locator != locator
            or source.observed_at > self._clock()
        ):
            raise InvalidPricingEvidenceError("来源租户、引用、定位或观察时间不一致")
        return source

    async def _confirm(
        self,
        tenant_id: TenantId,
        command: BaseModel,
        *,
        actor: CostingActor,
        idempotency_key: str,
        action: CostingAction,
        repository: Literal["policies", "prices", "quote_fx"],
        view_type: type[BaseModel],
        id_field: str,
        hash_field: str,
    ) -> BaseModel:
        """只增加带完整来源的事实，同键内容冲突不覆盖或自动换键。"""
        await self._require(tenant_id, actor, action)
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > 200
        ):
            raise ValidationError("幂等键无效")
        command = type(command).model_validate(command.model_dump(mode="python"))
        payload = command.model_dump(mode="python")
        request_hash = canonical_pricing_hash(
            {"command": payload, "actor": actor.actor_id}
        )
        async with self._factory(tenant_id) as uow:
            repo = getattr(uow, repository)
            previous = await repo.get_by_key_for_update(tenant_id, idempotency_key)
            if previous is not None:
                if previous.request_hash != request_hash:
                    raise IdempotencyConflict(
                        "同一幂等键不能确认另一份内容或更换确认人"
                    )
                return previous.value
            source = await self._source(
                tenant_id,
                str(payload["source_ref"]),
                str(payload.get("locator", "$")),
                actor=actor,
            )
            await self._require(tenant_id, actor, action)
            now = self._clock()
            observed = payload.get("quoted_at", payload.get("observed_at"))
            if isinstance(observed, datetime) and observed > now:
                raise InvalidPricingEvidenceError(
                    "不能确认尚未发生的报价或费用观察事实"
                )
            value = view_type.model_validate(
                {
                    **confirmed_payload(command, source, actor, now),
                    id_field: new_id("ce"),
                    hash_field: "0" * 64,
                }
            )
            value = value.model_copy(update={hash_field: view_content_hash(value)})
            await repo.add(
                EvidenceRecord(
                    tenant_id,
                    getattr(value, id_field),
                    idempotency_key,
                    request_hash,
                    value,
                )
            )
            return value

    async def confirm_policy(
        self,
        tenant_id: TenantId,
        command: PricingPolicyCreate,
        *,
        actor: CostingActor,
        idempotency_key: str,
    ) -> PricingPolicyView:
        """老板确认利润政策及22项归类，不改变旧 margin_rules。"""
        return cast(
            PricingPolicyView,
            await self._confirm(
                tenant_id,
                command,
                actor=actor,
                idempotency_key=idempotency_key,
                action=CostingAction.POLICY_CONFIRM,
                repository="policies",
                view_type=PricingPolicyView,
                id_field="policy_id",
                hash_field="content_hash",
            ),
        )

    async def confirm_price(
        self,
        tenant_id: TenantId,
        command: PriceEvidenceCreate,
        *,
        actor: CostingActor,
        idempotency_key: str,
    ) -> PriceEvidenceView:
        """采购与费用依据保留不同量纲和基准，参考采购价只供估算。"""
        if not isinstance(
            command, (SupplierPriceEvidenceCreate, ExpenseEvidenceCreate)
        ):
            raise ValidationError("价格依据输入类型无效")
        view_type = (
            SupplierPriceEvidenceView
            if isinstance(command, SupplierPriceEvidenceCreate)
            else ExpenseEvidenceView
        )
        return cast(
            PriceEvidenceView,
            await self._confirm(
                tenant_id,
                command,
                actor=actor,
                idempotency_key=idempotency_key,
                action=CostingAction.EVIDENCE_CONFIRM,
                repository="prices",
                view_type=view_type,
                id_field="evidence_id",
                hash_field="evidence_hash",
            ),
        )

    async def confirm_quote_fx(
        self,
        tenant_id: TenantId,
        command: QuoteFxCreate,
        *,
        actor: CostingActor,
        idempotency_key: str,
    ) -> QuoteFxView:
        """只确认核算到报价方向的独立汇率，不查反向路径。"""
        return cast(
            QuoteFxView,
            await self._confirm(
                tenant_id,
                command,
                actor=actor,
                idempotency_key=idempotency_key,
                action=CostingAction.QUOTE_FX_CONFIRM,
                repository="quote_fx",
                view_type=QuoteFxView,
                id_field="fx_id",
                hash_field="content_hash",
            ),
        )

    async def get_policy(
        self, tenant_id: TenantId, category: str | None, *, actor: CostingActor
    ) -> PricingPolicyView:
        """只读当前已确认政策，未配置则明确阻断定价。"""
        await self._require(tenant_id, actor, CostingAction.SHEET_READ)
        async with self._factory(tenant_id) as uow:
            record = await uow.policies.get_effective(
                tenant_id, category, self._clock()
            )
            if record is None:
                raise ValidationError("已确认利润政策未配置")
            return record.value

    async def get_quote_fx(
        self, tenant_id: TenantId, fx_id: str, *, actor: CostingActor
    ) -> QuoteFxView:
        """独立读取已确认汇率，始终检查当前员工与租户。"""
        await self._require(tenant_id, actor, CostingAction.SHEET_READ)
        async with self._factory(tenant_id) as uow:
            record = await uow.quote_fx.get(tenant_id, fx_id)
            if record is None:
                raise ValidationError("已确认报价汇率不存在")
            return record.value

    async def _validate_coverage(
        self,
        tenant_id: TenantId,
        sheet: CostSheet,
        command: CostCoverageCreate,
        uow: CostingUnitOfWork,
    ) -> None:
        """逐项核对已确认金额、计价口径、数量与原文费用行，绝不把缺项当零。"""
        confirmed = {
            item.item_sequence: item
            for item in sheet.items
            if item.entered_by is not None
        }
        if (
            None in confirmed
            or not confirmed
            or len(confirmed)
            != sum(item.entered_by is not None for item in sheet.items)
        ):
            raise InvalidPricingEvidenceError("成本表没有有效已确认明细或序号重复")
        bound: set[int] = set()
        lines: set[tuple[str, str, str]] = set()
        detail_types = {"contact_data_cost", "ad_allocation", "agent_api_allocation"}
        for decision in command.decisions:
            if decision.applicable and (
                (
                    command.acquisition_mode == "summary"
                    and decision.item_type in detail_types
                )
                or (
                    command.acquisition_mode == "detail"
                    and decision.item_type == "customer_acquisition"
                )
            ):
                raise InvalidPricingEvidenceError("获客汇总与明细口径不能混用")
            for binding in decision.item_bindings:
                item = confirmed.get(binding.item_sequence)
                if (
                    item is None
                    or item.item_type.value != decision.item_type
                    or binding.item_sequence in bound
                ):
                    raise InvalidPricingEvidenceError("费用绑定未确认、类型错配或重复")
                record = await uow.prices.get(tenant_id, binding.evidence_id)
                if record is None:
                    raise InvalidPricingEvidenceError("费用确认依据不存在")
                evidence = record.value
                if (
                    evidence.opportunity_id != sheet.opportunity_id
                    or item.source_ref
                    not in {
                        evidence.source_ref,
                        evidence.source.artifact_id,
                        evidence.evidence_id,
                    }
                    or binding.source_line_ref != evidence.locator
                    or item.amount.currency != evidence.currency
                    or item.price_basis != evidence.basis
                ):
                    raise InvalidPricingEvidenceError(
                        "费用来源、机会、币种或计价基准不一致"
                    )
                # 使用可信 artifact 身份归一别名，防止同一原件换 source_ref 重复计入。
                line = (
                    evidence.source.artifact_id,
                    binding.source_line_ref,
                    binding.allocation_scope,
                )
                if line in lines:
                    raise InvalidPricingEvidenceError("同一原文费用明细及分摊范围重复")
                lines.add(line)
                if (
                    evidence.valid_until is not None
                    and evidence.valid_until <= self._clock()
                ):
                    raise InvalidPricingEvidenceError("费用或采购依据已过期")
                if isinstance(evidence, SupplierPriceEvidenceView):
                    if (
                        decision.item_type != "product_purchase"
                        or evidence.basis != "quoted"
                        or not evidence.quantity_min
                        <= sheet.quantity
                        <= evidence.quantity_max
                        or sheet.quantity < evidence.moq
                    ):
                        raise InvalidPricingEvidenceError(
                            "采购必须为适用数量档内的供应商实报价"
                        )
                    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
                        expected = (
                            evidence.amount
                            if item.is_per_unit
                            else evidence.amount * Decimal(sheet.quantity)
                        )
                    if item.amount.amount != expected:
                        raise InvalidPricingEvidenceError(
                            "采购单件/整单金额与供应商单价不一致"
                        )
                elif (
                    evidence.item_type != decision.item_type
                    or evidence.amount != item.amount.amount
                    or evidence.is_per_unit != item.is_per_unit
                    or evidence.quantity != sheet.quantity
                    or evidence.allocation_scope != binding.allocation_scope
                ):
                    raise InvalidPricingEvidenceError(
                        "费用金额、适用数量或分摊口径不一致"
                    )
                bound.add(binding.item_sequence)
        if bound != set(confirmed):
            raise InvalidPricingEvidenceError(
                "清单遗漏已确认费用或把已有费用标为不适用"
            )

    async def confirm_coverage(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        command: CostCoverageCreate,
        *,
        actor: CostingActor,
        idempotency_key: str,
    ) -> str:
        """锁行确认当前成本清单，追加成本后旧 expected hash 必须失效。"""
        await self._require(tenant_id, actor, CostingAction.COVERAGE_CONFIRM)
        command = CostCoverageCreate.model_validate(command.model_dump(mode="python"))
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > 200
        ):
            raise ValidationError("幂等键无效")
        payload = {
            **command.model_dump(mode="python"),
            "cost_sheet_id": str(cost_sheet_id),
        }
        request_hash = canonical_pricing_hash(
            {"command": payload, "actor": actor.actor_id}
        )
        async with self._factory(tenant_id) as uow:
            previous = await uow.coverage.get_by_key_for_update(
                tenant_id, idempotency_key
            )
            if previous is not None:
                if previous.request_hash != request_hash:
                    raise IdempotencyConflict("同一幂等键不能确认另一份费用清单")
                return previous.value.content_hash
            sheet = await uow.sheets.get_for_update(tenant_id, cost_sheet_id)
            if (
                sheet is None
                or cost_sheet_content_hash(sheet) != command.expected_sheet_hash
            ):
                raise CostCoverageConflict("成本表已改变或不存在，请重新核对费用清单")
            await self._validate_coverage(tenant_id, sheet, command, uow)
            await self._require(tenant_id, actor, CostingAction.COVERAGE_CONFIRM)
            now = self._clock()
            provenance = Provenance(
                source_type=SourceType.EMPLOYEE_INPUT,
                source_id=f"{cost_sheet_id}:{request_hash}",
                extracted_by="human",
                extracted_at=now,
                confirmed_by=EmployeeId(actor.actor_id),
                confirmed_at=now,
            )
            value = CostCoverageView.model_validate(
                {
                    **payload,
                    "coverage_id": "0" * 64,
                    "content_hash": "0" * 64,
                    "confirmed_by": actor.actor_id,
                    "confirmed_at": now,
                    "field_provenance": {
                        field: provenance for field in provenance_fields(payload)
                    },
                }
            )
            content_hash = view_content_hash(value)
            value = value.model_copy(
                update={"coverage_id": content_hash, "content_hash": content_hash}
            )
            await uow.coverage.add(
                EvidenceRecord(
                    tenant_id, content_hash, idempotency_key, request_hash, value
                )
            )
            return content_hash
