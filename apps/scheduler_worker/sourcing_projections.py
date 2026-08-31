"""Sourcing 封存候选集的产品卡投影编排。"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from domains.products.permissions import ProductActor
from domains.products.schemas import (
    CandidateIndicativePriceRef,
    CandidateProductCreate,
)
from domains.products.service import ProductService
from domains.sourcing.permissions import SourcingActor
from domains.sourcing.schemas import (
    SourcingCandidateProductInput,
    SourcingCandidateProductInputs,
)
from domains.sourcing.service import SourcingService
from shared.errors import TransientError, ValidationError
from shared.events.catalog import SourcingCandidatesVerified
from shared.schemas.identifiers import (
    ProductId,
    RunId,
    SourcingCaseId,
    SourcingSupplyOptionId,
    TenantId,
)
from workflows.engine.runner import StepStatus, WorkflowEngine, WorkflowRun

_WORKFLOW_TYPE = "sourcing_case"
_WORKFLOW_VERSION = 2
_WAIT_STEP = "await_product_cards"


def _raise_dependency_error(
    *, transient: bool, failed: bool, transient_message: str, permanent_message: str
) -> None:
    """在原异常上下文外抛固定错误，不保留下层自由文本。"""

    if transient:
        raise TransientError(transient_message)
    if failed:
        raise ValidationError(permanent_message)


async def _dependency[T](
    awaitable: Awaitable[T],
    *,
    transient_message: str,
    permanent_message: str,
) -> T:
    transient = False
    failed = False
    try:
        return await awaitable
    except TransientError:
        transient = True
    except Exception:  # noqa: BLE001 -- Product/Sourcing/Engine 自由错误不跨界
        failed = True
    _raise_dependency_error(
        transient=transient,
        failed=failed,
        transient_message=transient_message,
        permanent_message=permanent_message,
    )
    raise AssertionError("依赖错误必须终止投影")


async def _engine_dependency[T](awaitable: Awaitable[T], *, message: str) -> T:
    """Engine 位于 Ready 事务之外，任何存储失败都只能安全重试。"""

    failed = False
    try:
        return await awaitable
    except Exception:  # noqa: BLE001 -- Engine 自由错误不可跨边界且必须可重试
        failed = True
    if failed:
        raise TransientError(message)
    raise AssertionError("Engine 依赖错误必须终止投影")


def _bound_run(
    run: WorkflowRun | None,
    tenant_id: TenantId,
    case_id: SourcingCaseId,
    candidate_ids: list[str],
    case_version: int,
    candidate_set_hash: str,
) -> bool:
    return (
        isinstance(run, WorkflowRun)
        and run.tenant_id == tenant_id
        and run.workflow_type == _WORKFLOW_TYPE
        and run.workflow_version == _WORKFLOW_VERSION
        and run.subject_ref == str(case_id)
        and run.status in {StepStatus.RUNNING, StepStatus.WAITING_EVENT}
        and isinstance(run.context, dict)
        and run.context.get("case_id") == str(case_id)
        and run.context.get("supplier_candidate_ids") == candidate_ids
        and run.context.get("candidate_case_version") == case_version
        and run.context.get("candidate_set_hash") == candidate_set_hash
    )


class SourcingCandidateProductProjector:
    """把已封存供应商候选投影为真实产品与供应选项。"""

    def __init__(
        self,
        *,
        sourcing: SourcingService,
        products: ProductService,
        engine: WorkflowEngine,
        tenant_id: TenantId,
        sourcing_actor: SourcingActor,
        product_actor: ProductActor,
    ) -> None:
        self._sourcing = sourcing
        self._products = products
        self._engine = engine
        self._tenant_id = tenant_id
        self._sourcing_actor = sourcing_actor
        self._product_actor = product_actor

    async def _prepared(
        self,
        case_id: SourcingCaseId,
        run_id: RunId,
        payload: dict[str, Any],
        required_context: dict[str, Any],
    ) -> bool:
        delivered = await _engine_dependency(
            self._engine.has_delivered_event(
                self._tenant_id,
                _WORKFLOW_TYPE,
                str(case_id),
                "SourcingProductCardsPrepared",
                payload,
                workflow_version=_WORKFLOW_VERSION,
                required_context=required_context,
                run_id=run_id,
            ),
            message="候选产品卡工作流事件证据暂不可用",
        )
        if not isinstance(delivered, bool):
            raise ValidationError("候选产品卡工作流事件证据无效")
        return delivered

    async def _deliver_prepared(
        self, case_id: SourcingCaseId, payload: dict[str, Any]
    ) -> None:
        run_id = await _engine_dependency(
            self._engine.find_active_run(
                self._tenant_id, _WORKFLOW_TYPE, str(case_id)
            ),
            message="候选产品卡所属工作流暂不可用",
        )
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValidationError("候选产品卡缺少唯一活动 V2 Run")
        bound_run_id = RunId(run_id)
        run = await _engine_dependency(
            self._engine.get_run(self._tenant_id, bound_run_id),
            message="候选产品卡所属工作流暂不可用",
        )
        candidate_ids = payload.get("candidate_ids")
        case_version = payload.get("case_version")
        candidate_set_hash = payload.get("candidate_set_hash")
        if (
            not isinstance(candidate_ids, list)
            or not isinstance(case_version, int)
            or isinstance(case_version, bool)
            or not isinstance(candidate_set_hash, str)
            or not _bound_run(
                run,
                self._tenant_id,
                case_id,
                candidate_ids,
                case_version,
                candidate_set_hash,
            )
        ):
            raise ValidationError("候选产品卡所属 V2 Run generation 绑定无效")
        assert run is not None
        if run.current_step == "prepare_candidates":
            raise TransientError("候选产品卡所属 V2 Run 尚未进入产品卡等待边界")
        required_context = {
            "case_id": str(case_id),
            "supplier_candidate_ids": candidate_ids,
            "candidate_case_version": case_version,
            "candidate_set_hash": candidate_set_hash,
        }
        if await self._prepared(case_id, bound_run_id, payload, required_context):
            return
        if run.current_step != _WAIT_STEP:
            raise ValidationError("候选产品卡所属 V2 Run 不在等待边界")
        accepted = await _engine_dependency(
            self._engine.deliver_event(
                self._tenant_id, run.run_id,
                "SourcingProductCardsPrepared",
                payload,
            ),
            message="候选产品卡工作流唤醒暂不可用",
        )
        if not isinstance(accepted, bool):
            raise ValidationError("候选产品卡工作流唤醒结果无效")
        if not accepted and not await self._prepared(
            case_id, bound_run_id, payload, required_context
        ):
            raise ValidationError("候选产品卡工作流未接受就绪事件")

    @staticmethod
    def _command(value: SourcingCandidateProductInput) -> CandidateProductCreate:
        if not isinstance(value, SourcingCandidateProductInput):
            raise ValidationError("候选产品卡输入无效")
        failed = False
        command: CandidateProductCreate | None = None
        try:
            command = CandidateProductCreate(
                sourcing_case_id=value.sourcing_case_id,
                supplier_candidate_id=value.supplier_candidate_id,
                name_zh=value.name_zh,
                name_en=value.name_en,
                category=value.category,
                spec_summary=value.spec_summary,
                moq=value.moq,
                evidence_refs=value.evidence_refs,
                indicative_prices=tuple(
                    CandidateIndicativePriceRef(
                        minimum_quantity=item.minimum_quantity,
                        unit_amount=item.unit_amount,
                        currency=item.currency,
                        unit=item.unit,
                        evidence_ref=item.evidence_ref,
                    )
                    for item in value.indicative_prices
                ),
            )
        except Exception:  # noqa: BLE001 -- Pydantic 原始细节不跨编排边界
            failed = True
        if failed or command is None:
            raise ValidationError("候选产品卡输入无效")
        return command

    async def handle(self, event: object) -> None:
        """仅投影已封存事实；重投可复用 Product、Option 与 Ready 事实。"""

        if not isinstance(event, SourcingCandidatesVerified):
            raise ValidationError("产品卡投影只接受 SourcingCandidatesVerified")
        if event.tenant_id != self._tenant_id:
            return
        if len(event.candidate_ids) > 3:
            raise ValidationError("单个 Case 至多三个合格候选产品卡")
        projection = await _dependency(
            self._sourcing.get_candidate_product_inputs(
                self._tenant_id,
                event.case_id,
                event.candidate_ids,
                expected_case_version=event.case_version,
                expected_candidate_set_hash=event.candidate_set_hash,
                actor=self._sourcing_actor,
            ),
            transient_message="封存候选产品输入暂不可用",
            permanent_message="封存候选产品输入读取失败",
        )
        if (
            not isinstance(projection, SourcingCandidateProductInputs)
            or projection.tenant_id != self._tenant_id
            or projection.case_id != event.case_id
            or projection.candidate_ids != event.candidate_ids
            or projection.case_version != event.case_version
            or projection.candidate_set_hash != event.candidate_set_hash
        ):
            raise ValidationError("封存候选产品输入与事件 generation 不一致")

        product_ids: list[ProductId] = []
        option_ids: list[SourcingSupplyOptionId] = []
        for item in projection.commands:
            command = self._command(item)
            product_id = await _dependency(
                self._products.create_candidate_from_sourcing(
                    self._tenant_id, command, actor=self._product_actor
                ),
                transient_message="候选产品卡创建暂不可用",
                permanent_message="候选产品卡创建失败",
            )
            if not isinstance(product_id, str) or not product_id.strip():
                raise ValidationError("候选产品卡 ProductId 无效")
            option_id = await _dependency(
                self._sourcing.register_supplier_candidate_option(
                    self._tenant_id,
                    event.case_id,
                    item.supplier_candidate_id,
                    product_id,
                    expected_case_version=event.case_version,
                    expected_candidate_set_hash=event.candidate_set_hash,
                    actor=self._sourcing_actor,
                ),
                transient_message="候选供应选项登记暂不可用",
                permanent_message="候选供应选项登记失败",
            )
            if not isinstance(option_id, str) or not option_id.strip():
                raise ValidationError("候选供应选项 ID 无效")
            product_ids.append(product_id)
            option_ids.append(option_id)

        await _dependency(
            self._sourcing.mark_candidates_ready(
                self._tenant_id,
                event.case_id,
                tuple(option_ids),
                event.candidate_ids,
                expected_case_version=event.case_version,
                expected_candidate_set_hash=event.candidate_set_hash,
                actor=self._sourcing_actor,
            ),
            transient_message="候选集合就绪迁移暂不可用",
            permanent_message="候选集合就绪迁移失败",
        )
        await self._deliver_prepared(
            event.case_id,
            {
                "case_id": str(event.case_id),
                "candidate_ids": [str(item) for item in event.candidate_ids],
                "product_ids": [str(item) for item in product_ids],
                "option_ids": [str(item) for item in option_ids],
                "case_version": event.case_version,
                "candidate_set_hash": event.candidate_set_hash,
            },
        )


__all__ = ("SourcingCandidateProductProjector",)
