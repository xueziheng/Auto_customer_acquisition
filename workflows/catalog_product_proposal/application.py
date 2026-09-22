"""Catalog Product Proposal 的应用服务与 metadata-only 事件编排。"""

from __future__ import annotations

import re

from domains.demand.service import DemandService
from domains.products.service import (
    CatalogProposalPolicyContent,
    CatalogProposalPolicyView,
    CatalogProposalService,
    ProductActor,
)
from shared.errors import IdempotencyConflict, TransientError, ValidationError
from shared.events.catalog import (
    AccountCountryFactsChanged,
    CatalogProductProposalCreated,
    CatalogProposalPolicyActivated,
    NeedCatalogFactsChanged,
    NeedClusterMembershipChanged,
)
from shared.schemas.identifiers import (
    CatalogProductProposalId,
    CatalogProposalEvaluationId,
    CatalogProposalPolicyVersionId,
    NeedClusterId,
    ProspectAccountId,
    TenantId,
)
from workflows.catalog_product_proposal.evaluation_flow import (
    CATALOG_EVALUATION_WORKFLOW_TYPE,
    catalog_evaluation_idempotency_key,
)
from workflows.catalog_product_proposal.mapping import (
    cultivation_workflow_context,
    evaluation_workflow_context,
    map_catalog_facts,
    policy_workflow_context,
)
from workflows.catalog_product_proposal.policy_flow import (
    CATALOG_POLICY_WORKFLOW_TYPE,
    catalog_policy_change_idempotency_key,
)
from workflows.catalog_product_proposal.proposal_flow import (
    CATALOG_CULTIVATION_WORKFLOW_TYPE,
    catalog_cultivation_idempotency_key,
)
from workflows.engine.runner import WorkflowEngine

_POLICY_ID = re.compile(r"cpv_[0-7][0-9A-HJKMNP-TV-Z]{25}")


def _policy_id(value: object) -> CatalogProposalPolicyVersionId:
    if not isinstance(value, str) or _POLICY_ID.fullmatch(value) is None:
        raise ValueError("catalog policy id malformed")
    return CatalogProposalPolicyVersionId(value)


def _id(value: object, prefix: str, subject: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(f"{prefix}_")
        or value != value.strip()
        or len(value) > 40
    ):
        raise ValidationError(f"{subject}无效")
    return value


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 200:
        raise ValidationError("目录产品事件批量上限无效")
    return value


def _hash(value: object, subject: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValidationError(f"{subject}无效")
    return value


class CatalogProductApplication:
    """连接 Demand、Products 与 durable workflow，不承载领域门槛。"""

    def __init__(
        self,
        demand: DemandService,
        products: CatalogProposalService,
        engine: WorkflowEngine,
        system_actor: ProductActor,
    ) -> None:
        self._demand = demand
        self._products = products
        self._engine = engine
        self._system_actor = system_actor

    async def submit_policy_candidate(
        self,
        tenant_id: TenantId,
        content: CatalogProposalPolicyContent,
        *,
        idempotency_key: str,
        actor: ProductActor,
    ) -> CatalogProposalPolicyView:
        try:
            policy_id = await self._products.create_policy_candidate(
                tenant_id,
                content,
                idempotency_key=idempotency_key,
                actor=actor,
            )
        except IdempotencyConflict:
            raise
        except ValidationError:
            raise
        except TransientError:
            raise TransientError("目录策略候选暂不可用") from None
        except Exception:  # noqa: BLE001 -- commit 结果未知须原键重试
            raise TransientError("目录策略候选暂不可用") from None
        try:
            checked_policy_id = _policy_id(policy_id)
        except Exception:  # noqa: BLE001 -- create 返回未知 ID 不得继续任何下游 IO
            raise TransientError("目录策略候选暂不可用") from None
        try:
            snapshot = await self._products.get_policy_change_snapshot(
                tenant_id, checked_policy_id, actor=self._system_actor
            )
        except ValidationError:
            raise
        except TransientError:
            raise TransientError("目录策略候选暂不可用") from None
        except Exception:  # noqa: BLE001 -- commit 结果未知须原键重试
            raise TransientError("目录策略候选暂不可用") from None
        try:
            candidate = CatalogProposalPolicyView.model_validate(
                snapshot.candidate.model_dump(mode="python")
            )
            candidate_policy_id = _policy_id(candidate.policy_version_id)
            if candidate_policy_id != checked_policy_id:
                raise ValueError("catalog policy snapshot subject mismatch")
        except Exception:  # noqa: BLE001 -- 适配器返回损坏/错对象须脱敏重试
            raise TransientError("目录策略候选暂不可用") from None
        try:
            await self._engine.start(
                tenant_id,
                CATALOG_POLICY_WORKFLOW_TYPE,
                str(checked_policy_id),
                policy_workflow_context(tenant_id, snapshot),
                catalog_policy_change_idempotency_key(tenant_id, checked_policy_id),
            )
        except Exception:  # noqa: BLE001 -- Products 已提交，不回滚或泄露异常
            raise TransientError("目录策略审批流程启动暂不可用") from None
        return candidate

    async def _evaluate_cluster(
        self, tenant_id: TenantId, cluster_id: NeedClusterId
    ):
        try:
            policy = await self._products.get_active_policy(
                tenant_id, actor=self._system_actor
            )
        except ValidationError:
            raise ValidationError("目录提案活动策略事实无效") from None
        except Exception:  # noqa: BLE001
            raise TransientError("目录提案活动策略暂不可用") from None
        if policy is None:
            return None
        try:
            demand_facts = await self._demand.get_cluster_catalog_facts(
                tenant_id, cluster_id
            )
            context = evaluation_workflow_context(tenant_id, policy, demand_facts)
        except ValidationError:
            raise ValidationError("目录提案 Demand 事实无效") from None
        except TransientError:
            raise TransientError("目录提案 Demand 事实暂不可用") from None
        except Exception:  # noqa: BLE001
            raise TransientError("目录提案 Demand 事实暂不可用") from None
        try:
            return await self._engine.start(
                tenant_id,
                CATALOG_EVALUATION_WORKFLOW_TYPE,
                str(cluster_id),
                context,
                catalog_evaluation_idempotency_key(
                    tenant_id,
                    cluster_id,
                    policy.policy_version_id,
                    demand_facts.facts_hash,
                ),
            )
        except Exception:  # noqa: BLE001 -- start 结果未知由确定性键恢复
            raise TransientError("目录簇评估流程启动暂不可用") from None

    async def handle_need_cluster_membership_changed(
        self, event: NeedClusterMembershipChanged
    ):
        cluster_id = NeedClusterId(
            _id(event.cluster_id, "ncl", "目录需求簇成员事件 cluster_id")
        )
        _id(event.changed_need_id, "need", "目录需求簇成员事件 need_id")
        return await self._evaluate_cluster(event.tenant_id, cluster_id)

    async def handle_need_catalog_facts_changed(
        self, event: NeedCatalogFactsChanged
    ):
        _id(event.need_id, "need", "目录需求事实事件 need_id")
        if event.change_kind not in {"quantity", "unit", "recurring_requirement"}:
            raise ValidationError("目录需求事实事件 change_kind 无效")
        if event.cluster_id is None:
            return None
        cluster_id = NeedClusterId(
            _id(event.cluster_id, "ncl", "目录需求事实事件 cluster_id")
        )
        return await self._evaluate_cluster(event.tenant_id, cluster_id)

    async def handle_account_country_facts_changed(
        self, event: AccountCountryFactsChanged, *, limit: int
    ) -> tuple[object, ...]:
        checked_limit = _limit(limit)
        account_id = ProspectAccountId(
            _id(event.account_id, "acc", "目录账户国家事件 account_id")
        )
        try:
            cluster_ids = await self._demand.list_catalog_cluster_ids_for_account(
                event.tenant_id, account_id, limit=checked_limit
            )
        except ValidationError:
            raise ValidationError("目录账户国家关联事实无效") from None
        except Exception:  # noqa: BLE001
            raise TransientError("目录账户国家关联事实暂不可用") from None
        if len(cluster_ids) > checked_limit:
            raise ValidationError("目录账户国家关联事实无效")
        results = []
        for cluster_id in cluster_ids:
            checked = NeedClusterId(
                _id(cluster_id, "ncl", "目录账户国家关联 cluster_id")
            )
            results.append(await self._evaluate_cluster(event.tenant_id, checked))
        return tuple(results)

    async def handle_catalog_policy_activated(
        self, event: CatalogProposalPolicyActivated, *, limit: int
    ) -> tuple[object, ...]:
        checked_limit = _limit(limit)
        policy_id = CatalogProposalPolicyVersionId(
            _id(event.policy_version_id, "cpv", "目录提案策略激活事件 policy_id")
        )
        content_hash = _hash(
            event.content_hash, "目录提案策略激活事件 content_hash"
        )
        try:
            policy = await self._products.get_active_policy(
                event.tenant_id, actor=self._system_actor
            )
        except ValidationError:
            raise ValidationError("目录提案活动策略事实无效") from None
        except Exception:  # noqa: BLE001
            raise TransientError("目录提案活动策略暂不可用") from None
        if (
            policy is None
            or policy.policy_version_id != policy_id
            or policy.content_hash != content_hash
        ):
            raise ValidationError("目录提案策略激活事件无效")
        try:
            cluster_ids = await self._demand.list_catalog_cluster_ids(
                event.tenant_id, limit=checked_limit
            )
        except ValidationError:
            raise ValidationError("目录提案需求簇列表事实无效") from None
        except Exception:  # noqa: BLE001
            raise TransientError("目录提案需求簇列表暂不可用") from None
        if len(cluster_ids) > checked_limit:
            raise ValidationError("目录提案需求簇列表事实无效")
        results = []
        for cluster_id in cluster_ids:
            checked = NeedClusterId(
                _id(cluster_id, "ncl", "目录提案需求簇列表 cluster_id")
            )
            results.append(await self._evaluate_cluster(event.tenant_id, checked))
        return tuple(results)

    async def handle_catalog_product_proposal_created(
        self, event: CatalogProductProposalCreated
    ):
        proposal_id = CatalogProductProposalId(
            _id(event.proposal_id, "cpr", "目录产品提案事件 proposal_id")
        )
        evaluation_id = CatalogProposalEvaluationId(
            _id(event.evaluation_id, "cpe", "目录产品提案事件 evaluation_id")
        )
        cluster_id = NeedClusterId(
            _id(event.cluster_id, "ncl", "目录产品提案事件 cluster_id")
        )
        policy_id = CatalogProposalPolicyVersionId(
            _id(event.policy_version_id, "cpv", "目录产品提案事件 policy_id")
        )
        facts_hash = _hash(event.facts_hash, "目录产品提案事件 facts_hash")
        try:
            proposal = await self._products.get_proposal(
                event.tenant_id, proposal_id, actor=self._system_actor
            )
            evaluation = await self._products.get_evaluation(
                event.tenant_id, evaluation_id, actor=self._system_actor
            )
            policy = await self._products.get_active_policy(
                event.tenant_id, actor=self._system_actor
            )
        except ValidationError:
            raise ValidationError("目录产品提案事件事实无效") from None
        except Exception:  # noqa: BLE001
            raise TransientError("目录产品提案事件事实暂不可用") from None
        if (
            proposal.proposal_id != proposal_id
            or proposal.evaluation_id != evaluation_id
            or proposal.cluster_id != cluster_id
            or proposal.policy_version_id != policy_id
            or proposal.facts_hash != facts_hash
            or evaluation.evaluation_id != evaluation_id
            or evaluation.cluster_id != cluster_id
            or evaluation.policy_version_id != policy_id
            or evaluation.facts_hash != facts_hash
            or evaluation.proposed_by_run != event.run_id
        ):
            raise ValidationError("目录产品提案事件事实无效")
        if policy is None or policy.policy_version_id != policy_id:
            return None
        try:
            current = map_catalog_facts(
                await self._demand.get_cluster_catalog_facts(
                    event.tenant_id, cluster_id
                )
            )
        except ValidationError:
            raise ValidationError("目录产品提案事件 Demand 事实无效") from None
        except Exception:  # noqa: BLE001
            raise TransientError("目录产品提案事件 Demand 事实暂不可用") from None
        if current.facts_hash != facts_hash or current != evaluation.facts:
            return None
        context = cultivation_workflow_context(
            event.tenant_id, proposal, evaluation, policy
        )
        try:
            return await self._engine.start(
                event.tenant_id,
                CATALOG_CULTIVATION_WORKFLOW_TYPE,
                str(proposal_id),
                context,
                catalog_cultivation_idempotency_key(event.tenant_id, proposal_id),
            )
        except Exception:  # noqa: BLE001
            raise TransientError("目录产品培养审批流程启动暂不可用") from None


__all__ = ("CatalogProductApplication",)
