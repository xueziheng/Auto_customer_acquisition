"""Catalog Policy 变更的 durable 状态机与安全审批事件关联。"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol

from domains.approvals.service import (
    ApprovalService,
    ApprovalState,
    CatalogApprovalContractError,
)
from domains.products.service import CatalogProposalService, ProductActor
from shared.errors import TransientError, ValidationError
from shared.events.bus import EventHandler
from shared.events.catalog import ApprovalDecided, DomainEvent
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogProposalPolicyVersionId,
    RunId,
    TenantId,
)
from workflows.catalog_product_proposal.mapping import policy_workflow_context
from workflows.catalog_product_proposal.policy_steps import (
    ApplyPolicyStep,
    AssemblePackageStep,
    ExpirePolicyStep,
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

CATALOG_POLICY_WORKFLOW_TYPE = "catalog_proposal_policy_change"
_POLICY_REF = re.compile(r"catalog-policy:(cpv_[^:\s]{1,36}):[0-9a-f]{64}\Z")


class OutboxHandlerRegistry(Protocol):
    def register_handler(
        self,
        event_type: type[DomainEvent],
        handler_name: str,
        handler: EventHandler[DomainEvent],
    ) -> None: ...


def catalog_policy_change_idempotency_key(
    tenant_id: TenantId,
    policy_version_id: CatalogProposalPolicyVersionId | str,
) -> str:
    """生成策略主体唯一且可重放的 workflow 启动键。"""
    if (
        not isinstance(tenant_id, str)
        or not tenant_id
        or tenant_id != tenant_id.strip()
        or not isinstance(policy_version_id, str)
        or not policy_version_id.startswith("cpv_")
        or policy_version_id != policy_version_id.strip()
        or len(policy_version_id) > 40
    ):
        raise ValidationError("目录策略 workflow 启动身份无效")
    return f"catalog-policy-change:{tenant_id}:{policy_version_id}"


def build_catalog_policy_workflow_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type=CATALOG_POLICY_WORKFLOW_TYPE,
        version=1,
        steps=(
            StepDefinition("assemble_package", "catalog_product_policy.assemble"),
            StepDefinition("submit_approval", "catalog_product_policy.submit"),
            StepDefinition(
                "wait_decision",
                "catalog_product_policy.wait",
                timeout_context_key="approval_timeout_seconds",
                on_timeout="expire_policy",
                wait_event_type="ApprovalDecided",
                run_on_entry=True,
            ),
            StepDefinition("apply_policy", "catalog_product_policy.apply"),
            StepDefinition("expire_policy", "catalog_product_policy.expire"),
            StepDefinition("mark_applied", "catalog_product_policy.mark_applied"),
        ),
        transitions={
            "assemble_package": ("submit_approval",),
            "submit_approval": ("wait_decision",),
            "wait_decision": ("apply_policy", "expire_policy"),
            "apply_policy": ("mark_applied",),
            "expire_policy": (),
            "mark_applied": (),
        },
    )


def build_catalog_policy_workflow_handlers(
    products: CatalogProposalService,
    approvals: ApprovalService,
    system_actor: ProductActor,
    *,
    now: Callable[[], datetime] | None = None,
) -> Mapping[str, StepHandler]:
    clock = now or (lambda: datetime.now(UTC))
    return {
        "catalog_product_policy.assemble": AssemblePackageStep(products, system_actor),
        "catalog_product_policy.submit": SubmitApprovalStep(
            products, approvals, system_actor, now=clock
        ),
        "catalog_product_policy.wait": WaitDecisionStep(
            products, approvals, system_actor
        ),
        "catalog_product_policy.apply": ApplyPolicyStep(
            products, approvals, system_actor
        ),
        "catalog_product_policy.expire": ExpirePolicyStep(
            products, approvals, system_actor
        ),
        "catalog_product_policy.mark_applied": MarkAppliedStep(
            products, approvals, system_actor
        ),
    }


class CatalogPolicyApprovalDecidedHandler:
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
                raise TransientError("目录策略审批事件事实暂不可用") from None
            raise ValidationError("目录策略审批事件事实无效") from None
        except TransientError:
            raise TransientError("目录策略审批事件事实暂不可用") from None
        except Exception:  # noqa: BLE001 -- 事件投影不得泄露跨域异常文本
            raise TransientError("目录策略审批事件事实暂不可用") from None
        if raw.approval_type != CATALOG_POLICY_WORKFLOW_TYPE:
            return
        try:
            fact = await self._approvals.read_catalog_fact(
                event.tenant_id, approval_id
            )
        except CatalogApprovalContractError as error:
            if error.code == "catalog_storage_unavailable":
                raise TransientError("目录策略审批事件事实暂不可用") from None
            raise ValidationError("目录策略审批事件事实无效") from None
        except TransientError:
            raise TransientError("目录策略审批事件事实暂不可用") from None
        except Exception:  # noqa: BLE001 -- strict reader 异常同样固定脱敏
            raise TransientError("目录策略审批事件事实暂不可用") from None
        match = _POLICY_REF.fullmatch(fact.change_set_ref)
        if (
            fact.tenant_id != event.tenant_id
            or fact.approval_id != approval_id
            or match is None
            or fact.proposed_change.policy_version_id != match.group(1)
        ):
            raise ValidationError("目录策略审批事件与审批事实不匹配")
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
            raise ValidationError("目录策略审批事件决定与 canonical 事实不匹配")
        payload = {
            "approval_id": str(approval_id),
            "decision": event.decision,
            "decided_by": (None if event.decided_by is None else str(event.decided_by)),
        }
        policy_id = match.group(1)
        run_id: RunId | None = await self._engine.find_active_run(
            event.tenant_id, CATALOG_POLICY_WORKFLOW_TYPE, policy_id
        )
        if run_id is None:
            if await self._engine.has_delivered_event(
                event.tenant_id,
                CATALOG_POLICY_WORKFLOW_TYPE,
                policy_id,
                "ApprovalDecided",
                payload,
            ):
                return
            raise TransientError("目录策略审批活动流程暂不可用")
        accepted = await self._engine.deliver_event(
            event.tenant_id, run_id, "ApprovalDecided", payload
        )
        if not accepted:
            raise TransientError("目录策略审批活动流程暂不可用")


def register_catalog_policy_workflow(
    engine: WorkflowEngine,
    registry: OutboxHandlerRegistry,
    approvals: ApprovalService,
) -> None:
    """注册策略 workflow 与共享 ApprovalDecided 处理器。"""
    engine.register(build_catalog_policy_workflow_definition())
    registry.register_handler(
        ApprovalDecided,
        "catalog_product_policy.approval_decided",
        CatalogPolicyApprovalDecidedHandler(engine, approvals),  # type: ignore[arg-type]
    )


__all__ = (
    "CATALOG_POLICY_WORKFLOW_TYPE",
    "CatalogPolicyApprovalDecidedHandler",
    "build_catalog_policy_workflow_definition",
    "build_catalog_policy_workflow_handlers",
    "catalog_policy_change_idempotency_key",
    "policy_workflow_context",
    "register_catalog_policy_workflow",
)
