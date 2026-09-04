"""目录产品培养的 durable 状态机与审批事件安全关联。"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from domains.approvals.service import (
    ApprovalService,
    ApprovalState,
    CatalogApprovalContractError,
)
from domains.demand.service import DemandService
from domains.products.service import CatalogProposalService, ProductActor
from shared.errors import TransientError, ValidationError
from shared.events.catalog import ApprovalDecided
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogProductProposalId,
    RunId,
    TenantId,
)
from workflows.catalog_product_proposal.mapping import cultivation_workflow_context
from workflows.catalog_product_proposal.proposal_steps import (
    ApplyCultivationStep,
    AssemblePackageStep,
    ExpireProposalStep,
    MarkAppliedStep,
    SubmitApprovalStep,
    WaitDecisionStep,
)
from workflows.engine.runner import (
    StepDefinition,
    StepHandler,
    WorkflowDefinition,
    WorkflowEngine,
)

CATALOG_CULTIVATION_WORKFLOW_TYPE = "catalog_product_cultivation"
_CULTIVATION_REF = re.compile(
    r"catalog-cultivation:(cpr_[^:\s]{1,36}):(cpv_[^:\s]{1,36}):[0-9a-f]{64}\Z"
)


def catalog_cultivation_idempotency_key(
    tenant_id: TenantId,
    proposal_id: CatalogProductProposalId,
) -> str:
    """生成提案主体唯一且可重放的 workflow 启动键。"""
    if (
        not isinstance(tenant_id, str)
        or not tenant_id.startswith("tn_")
        or tenant_id != tenant_id.strip()
        or len(tenant_id) > 40
        or not isinstance(proposal_id, str)
        or not proposal_id.startswith("cpr_")
        or proposal_id != proposal_id.strip()
        or len(proposal_id) > 40
    ):
        raise ValidationError("目录产品培养 workflow 启动身份无效")
    return f"catalog-cultivation:{tenant_id}:{proposal_id}"


def build_catalog_product_workflow_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type=CATALOG_CULTIVATION_WORKFLOW_TYPE,
        version=1,
        steps=(
            StepDefinition("assemble_package", "catalog_product_cultivation.assemble"),
            StepDefinition("submit_approval", "catalog_product_cultivation.submit"),
            StepDefinition(
                "wait_decision",
                "catalog_product_cultivation.wait",
                timeout_context_key="approval_timeout_seconds",
                on_timeout="expire_proposal",
                wait_event_type="ApprovalDecided",
                run_on_entry=True,
            ),
            StepDefinition("apply_cultivation", "catalog_product_cultivation.apply"),
            StepDefinition("expire_proposal", "catalog_product_cultivation.expire"),
            StepDefinition("mark_applied", "catalog_product_cultivation.mark_applied"),
        ),
        transitions={
            "assemble_package": ("submit_approval",),
            "submit_approval": ("wait_decision",),
            "wait_decision": ("apply_cultivation", "expire_proposal"),
            "apply_cultivation": ("mark_applied",),
            "expire_proposal": (),
            "mark_applied": (),
        },
    )


def build_catalog_product_workflow_handlers(
    demand: DemandService,
    products: CatalogProposalService,
    approvals: ApprovalService,
    system_actor: ProductActor,
    *,
    now: Callable[[], datetime] | None = None,
) -> Mapping[str, StepHandler]:
    clock = now or (lambda: datetime.now(UTC))
    return {
        "catalog_product_cultivation.assemble": AssemblePackageStep(
            demand, products, system_actor
        ),
        "catalog_product_cultivation.submit": SubmitApprovalStep(
            demand, products, approvals, system_actor, now=clock
        ),
        "catalog_product_cultivation.wait": WaitDecisionStep(
            demand, products, approvals, system_actor
        ),
        "catalog_product_cultivation.apply": ApplyCultivationStep(
            demand, products, approvals, system_actor
        ),
        "catalog_product_cultivation.expire": ExpireProposalStep(
            demand, products, approvals, system_actor
        ),
        "catalog_product_cultivation.mark_applied": MarkAppliedStep(
            demand, products, approvals, system_actor
        ),
    }


class CatalogCultivationApprovalDecidedHandler:
    """审批事件只作唤醒；匹配事实由步骤再次从中央审批读取。"""

    def __init__(self, engine: WorkflowEngine, approvals: ApprovalService) -> None:
        self._engine = engine
        self._approvals = approvals

    async def handle(self, event: ApprovalDecided) -> None:
        approval_id = ApprovalId(str(event.approval_id))
        try:
            raw = await self._approvals.read_fact(event.tenant_id, approval_id)
        except CatalogApprovalContractError as error:
            if error.code == "catalog_storage_unavailable":
                raise TransientError("目录产品培养审批事件事实暂不可用") from None
            raise ValidationError("目录产品培养审批事件事实无效") from None
        except TransientError:
            raise TransientError("目录产品培养审批事件事实暂不可用") from None
        except Exception:  # noqa: BLE001 -- 事件投影不得泄露跨域异常文本
            raise TransientError("目录产品培养审批事件事实暂不可用") from None
        if raw.approval_type != CATALOG_CULTIVATION_WORKFLOW_TYPE:
            return
        try:
            fact = await self._approvals.read_catalog_fact(
                event.tenant_id, approval_id
            )
        except CatalogApprovalContractError as error:
            if error.code == "catalog_storage_unavailable":
                raise TransientError("目录产品培养审批事件事实暂不可用") from None
            raise ValidationError("目录产品培养审批事件事实无效") from None
        except TransientError:
            raise TransientError("目录产品培养审批事件事实暂不可用") from None
        except Exception:  # noqa: BLE001 -- strict reader 异常固定脱敏
            raise TransientError("目录产品培养审批事件事实暂不可用") from None
        match = _CULTIVATION_REF.fullmatch(fact.change_set_ref)
        if (
            fact.tenant_id != event.tenant_id
            or fact.approval_id != approval_id
            or match is None
            or getattr(fact.proposed_change, "proposal_id", None) != match.group(1)
            or getattr(fact.proposed_change, "policy_version_id", None)
            != match.group(2)
        ):
            raise ValidationError("目录产品培养审批事件与审批事实不匹配")
        expected_decision = (
            "approve"
            if fact.state
            in {
                ApprovalState.APPROVED,
                ApprovalState.APPLIED,
                ApprovalState.APPLY_FAILED,
            }
            else "reject"
            if fact.state is ApprovalState.REJECTED
            else None
        )
        if (
            event.decision != expected_decision
            or event.decided_by != fact.decided_by_employee
        ):
            raise ValidationError("目录产品培养审批事件决定与 canonical 事实不匹配")
        payload = {
            "approval_id": str(approval_id),
            "decision": event.decision,
            "decided_by": (
                None if event.decided_by is None else str(event.decided_by)
            ),
        }
        proposal_id = match.group(1)
        run_id: RunId | None = await self._engine.find_active_run(
            event.tenant_id, CATALOG_CULTIVATION_WORKFLOW_TYPE, proposal_id
        )
        if run_id is None:
            if await self._engine.has_delivered_event(
                event.tenant_id,
                CATALOG_CULTIVATION_WORKFLOW_TYPE,
                proposal_id,
                "ApprovalDecided",
                payload,
            ):
                return
            raise TransientError("目录产品培养审批活动流程暂不可用")
        accepted = await self._engine.deliver_event(
            event.tenant_id, run_id, "ApprovalDecided", payload
        )
        if not accepted:
            raise TransientError("目录产品培养审批活动流程暂不可用")


__all__ = (
    "CATALOG_CULTIVATION_WORKFLOW_TYPE",
    "CatalogCultivationApprovalDecidedHandler",
    "build_catalog_product_workflow_definition",
    "build_catalog_product_workflow_handlers",
    "catalog_cultivation_idempotency_key",
    "cultivation_workflow_context",
)
