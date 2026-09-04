"""单例 scheduler 的有界 Catalog workflow 启动恢复驱动。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from domains.demand.service import (
    CatalogClusterCursor,
    CatalogClusterIdPage,
    DemandService,
)
from domains.products.service import (
    CatalogPolicyReconciliationPage,
    CatalogProposalPolicyView,
    CatalogProposalReconciliationPage,
    CatalogProposalService,
    CatalogReconciliationCursor,
    ProductActor,
    ProductRole,
)
from shared.errors import TradeOSError, TransientError, ValidationError
from shared.schemas.identifiers import RunId, TenantId
from workflows.catalog_product_proposal import (
    CATALOG_CULTIVATION_WORKFLOW_TYPE,
    CATALOG_EVALUATION_WORKFLOW_TYPE,
    CATALOG_POLICY_WORKFLOW_TYPE,
    catalog_cultivation_idempotency_key,
    catalog_evaluation_idempotency_key,
    catalog_policy_change_idempotency_key,
    cultivation_workflow_context,
    evaluation_workflow_context,
    policy_workflow_context,
)
from workflows.engine.runner import WorkflowEngine


@dataclass(frozen=True)
class CatalogProductScanResult:
    """一次有界扫描的安全计数；不包含业务 ID、事实或游标正文。"""

    started_policy_runs: int
    started_proposal_runs: int
    started_evaluation_runs: int
    stop_reason: Literal["completed", "policy_not_configured"]


def _run_result(value: object) -> RunId:
    if (
        not isinstance(value, str)
        or not value
        or not value.startswith("run_")
        or value != value.strip()
        or len(value) > 40
    ):
        raise TransientError("目录产品调度 workflow 结果未知")
    return RunId(value)


class CatalogProductDriver:
    """按三个独立稳定游标恢复策略、提案与簇评估 workflow。"""

    def __init__(
        self,
        *,
        demand: DemandService,
        products: CatalogProposalService,
        engine: WorkflowEngine,
        system_actor: ProductActor,
        tenant_id: TenantId,
        batch_limit: int,
    ) -> None:
        required = (
            (demand, "list_catalog_cluster_id_page"),
            (demand, "get_cluster_catalog_facts"),
            (products, "list_pending_policy_reconciliation"),
            (products, "list_awaiting_proposal_reconciliation"),
            (products, "get_policy_change_snapshot"),
            (products, "get_active_policy"),
            (products, "get_proposal"),
            (products, "get_evaluation"),
            (engine, "start"),
        )
        if (
            any(not callable(getattr(value, name, None)) for value, name in required)
            or not isinstance(system_actor, ProductActor)
            or system_actor.role is not ProductRole.SYSTEM
            or system_actor.tenant_id != tenant_id
            or not isinstance(tenant_id, str)
            or not tenant_id.startswith("tn_")
            or tenant_id != tenant_id.strip()
            or len(tenant_id) > 40
            or type(batch_limit) is not int
            or not 1 <= batch_limit <= 200
        ):
            raise ValidationError("目录产品调度依赖无效")
        self._demand = demand
        self._products = products
        self._engine = engine
        self._actor = system_actor
        self._tenant_id = tenant_id
        self._limit = batch_limit
        self._policy_cursor: CatalogReconciliationCursor | None = None
        self._proposal_cursor: CatalogReconciliationCursor | None = None
        self._cluster_cursor: CatalogClusterCursor | None = None

    async def _start(
        self,
        workflow_type: str,
        subject_ref: str,
        context: dict[str, object],
        idempotency_key: str,
    ) -> None:
        try:
            result = await self._engine.start(
                self._tenant_id,
                workflow_type,
                subject_ref,
                context,
                idempotency_key,
            )
        except Exception:  # noqa: BLE001 -- engine 文本不得跨日志边界
            raise TransientError("目录产品调度 workflow 启动暂不可用") from None
        _run_result(result)

    async def _recover_policies(self) -> int:
        try:
            page = await self._products.list_pending_policy_reconciliation(
                self._tenant_id,
                actor=self._actor,
                limit=self._limit,
                cursor=self._policy_cursor,
            )
        except Exception:  # noqa: BLE001 -- page/cursor 不确定时不推进游标
            raise TransientError("目录产品调度策略恢复页暂不可用") from None
        if (
            not isinstance(page, CatalogPolicyReconciliationPage)
            or page.tenant_id != self._tenant_id
            or len(page.items) > self._limit
        ):
            raise TransientError("目录产品调度策略恢复页结果未知")
        started = 0
        for item in page.items:
            try:
                snapshot = await self._products.get_policy_change_snapshot(
                    self._tenant_id,
                    item.policy_version_id,
                    actor=self._actor,
                )
                await self._start(
                    CATALOG_POLICY_WORKFLOW_TYPE,
                    str(item.policy_version_id),
                    policy_workflow_context(self._tenant_id, snapshot),
                    catalog_policy_change_idempotency_key(
                        self._tenant_id, item.policy_version_id
                    ),
                )
            except TradeOSError as error:
                if error.is_retryable:
                    raise TransientError("目录产品调度策略恢复暂不可用") from None
                continue
            except Exception:  # noqa: BLE001 -- 未知结果保守重放当前页
                raise TransientError("目录产品调度策略恢复暂不可用") from None
            started += 1
        self._policy_cursor = page.next_cursor
        return started

    async def _recover_proposals(self) -> int:
        try:
            page = await self._products.list_awaiting_proposal_reconciliation(
                self._tenant_id,
                actor=self._actor,
                limit=self._limit,
                cursor=self._proposal_cursor,
            )
        except Exception:  # noqa: BLE001 -- page/cursor 不确定时不推进游标
            raise TransientError("目录产品调度提案恢复页暂不可用") from None
        if (
            not isinstance(page, CatalogProposalReconciliationPage)
            or page.tenant_id != self._tenant_id
            or len(page.items) > self._limit
        ):
            raise TransientError("目录产品调度提案恢复页结果未知")
        started = 0
        for item in page.items:
            try:
                proposal = await self._products.get_proposal(
                    self._tenant_id, item.proposal_id, actor=self._actor
                )
                evaluation = await self._products.get_evaluation(
                    self._tenant_id,
                    proposal.evaluation_id,
                    actor=self._actor,
                )
                policy = await self._products.get_active_policy(
                    self._tenant_id, actor=self._actor
                )
                if policy is None:
                    continue
                current = await self._demand.get_cluster_catalog_facts(
                    self._tenant_id, proposal.cluster_id
                )
                if current.facts_hash != proposal.facts_hash:
                    continue
                await self._start(
                    CATALOG_CULTIVATION_WORKFLOW_TYPE,
                    str(proposal.proposal_id),
                    cultivation_workflow_context(
                        self._tenant_id, proposal, evaluation, policy
                    ),
                    catalog_cultivation_idempotency_key(
                        self._tenant_id, proposal.proposal_id
                    ),
                )
            except TradeOSError as error:
                if error.is_retryable:
                    raise TransientError("目录产品调度提案恢复暂不可用") from None
                continue
            except Exception:  # noqa: BLE001 -- 未知结果保守重放当前页
                raise TransientError("目录产品调度提案恢复暂不可用") from None
            started += 1
        self._proposal_cursor = page.next_cursor
        return started

    async def _scan_clusters(self, policy: CatalogProposalPolicyView) -> int:
        try:
            page = await self._demand.list_catalog_cluster_id_page(
                self._tenant_id,
                limit=self._limit,
                cursor=self._cluster_cursor,
            )
        except Exception:  # noqa: BLE001 -- page/cursor 不确定时不推进游标
            raise TransientError("目录产品调度需求簇页面暂不可用") from None
        if (
            not isinstance(page, CatalogClusterIdPage)
            or page.tenant_id != self._tenant_id
            or len(page.cluster_ids) > self._limit
        ):
            raise TransientError("目录产品调度需求簇页面结果未知")
        started = 0
        for cluster_id in page.cluster_ids:
            try:
                facts = await self._demand.get_cluster_catalog_facts(
                    self._tenant_id, cluster_id
                )
                context = evaluation_workflow_context(self._tenant_id, policy, facts)
                await self._start(
                    CATALOG_EVALUATION_WORKFLOW_TYPE,
                    str(cluster_id),
                    context,
                    catalog_evaluation_idempotency_key(
                        self._tenant_id,
                        cluster_id,
                        policy.policy_version_id,
                        facts.facts_hash,
                    ),
                )
            except TradeOSError as error:
                if error.is_retryable:
                    raise TransientError("目录产品调度需求簇评估暂不可用") from None
                continue
            except Exception:  # noqa: BLE001 -- 未知结果保守重放当前页
                raise TransientError("目录产品调度需求簇评估暂不可用") from None
            started += 1
        self._cluster_cursor = page.next_cursor
        return started

    async def scan_once(self) -> CatalogProductScanResult:
        """依序恢复 policy/proposal，再在活动策略下扫描一页真实需求簇。"""
        policies = await self._recover_policies()
        proposals = await self._recover_proposals()
        try:
            policy = await self._products.get_active_policy(
                self._tenant_id, actor=self._actor
            )
        except Exception:  # noqa: BLE001 -- 不暴露底层异常或策略正文
            raise TransientError("目录产品调度活动策略暂不可用") from None
        if policy is None:
            return CatalogProductScanResult(
                policies, proposals, 0, "policy_not_configured"
            )
        evaluations = await self._scan_clusters(policy)
        return CatalogProductScanResult(policies, proposals, evaluations, "completed")


__all__ = ("CatalogProductDriver", "CatalogProductScanResult")
