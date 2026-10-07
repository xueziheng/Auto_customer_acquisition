"""目录簇评估步骤；只在 durable Run 内调用 Products。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from domains.demand.service import DemandService
from domains.products.service import (
    CatalogEvaluationConflictError,
    CatalogProposalEvaluationView,
    CatalogProposalService,
    ProductActor,
)
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import CatalogProposalPolicyVersionId, NeedClusterId
from workflows.catalog_product_proposal.mapping import map_catalog_facts
from workflows.engine.runner import WorkflowRun


def _text(value: object, field: str, *, prefix: str | None = None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 200
        or (prefix is not None and not value.startswith(f"{prefix}_"))
    ):
        raise ValidationError(f"目录簇评估工作流 {field} 无效")
    return value


def _hash(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValidationError(f"目录簇评估工作流 {field} 无效")
    return text


def _identity(
    run: WorkflowRun,
) -> tuple[NeedClusterId, CatalogProposalPolicyVersionId, str, str]:
    cluster_id = _text(run.context.get("cluster_id"), "cluster_id", prefix="ncl")
    policy_id = _text(
        run.context.get("policy_version_id"), "policy_version_id", prefix="cpv"
    )
    policy_hash = _hash(run.context.get("policy_content_hash"), "policy_content_hash")
    facts_hash = _hash(run.context.get("facts_hash"), "facts_hash")
    if cluster_id != run.subject_ref:
        raise ValidationError("目录簇评估工作流主体不匹配")
    return NeedClusterId(cluster_id), CatalogProposalPolicyVersionId(policy_id), policy_hash, facts_hash


async def _read[T](operation: Callable[[], Awaitable[T]], subject: str) -> T:
    try:
        return await operation()
    except ValidationError:
        raise ValidationError(f"{subject}事实无效") from None
    except TransientError:
        raise TransientError(f"{subject}暂不可用") from None
    except Exception:  # noqa: BLE001 -- 跨域原异常不得进入 durable error
        raise TransientError(f"{subject}暂不可用") from None


class EvaluateClusterStep:
    def __init__(
        self,
        demand: DemandService,
        products: CatalogProposalService,
        actor: ProductActor,
    ) -> None:
        self._demand = demand
        self._products = products
        self._actor = actor

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        cluster_id, policy_id, policy_hash, facts_hash = _identity(run)
        policy = await _read(
            lambda: self._products.get_active_policy(run.tenant_id, actor=self._actor),
            "目录提案活动策略读取",
        )
        if policy is None:
            return ("complete", None, {"evaluation_state": "stale"})
        if policy.policy_version_id != policy_id or policy.content_hash != policy_hash:
            return ("complete", None, {"evaluation_state": "stale"})
        demand_facts = await _read(
            lambda: self._demand.get_cluster_catalog_facts(run.tenant_id, cluster_id),
            "目录提案 Demand 事实读取",
        )
        facts = map_catalog_facts(demand_facts)
        if (
            facts.tenant_id != run.tenant_id
            or facts.cluster_id != cluster_id
            or facts.facts_hash != facts_hash
        ):
            return ("complete", None, {"evaluation_state": "stale"})
        try:
            evaluation = await self._products.evaluate_cluster(
                run.tenant_id,
                facts,
                proposed_by_run=run.run_id,
                actor=self._actor,
            )
        except CatalogEvaluationConflictError:
            raise ValidationError("目录簇评估持久化事实冲突") from None
        except TransientError:
            raise TransientError("目录簇评估暂不可用") from None
        except Exception:  # noqa: BLE001 -- 未知提交结果必须按原 Run 重试
            raise TransientError("目录簇评估暂不可用") from None
        if (
            not isinstance(evaluation, CatalogProposalEvaluationView)
            or evaluation.cluster_id != cluster_id
            or evaluation.policy_version_id != policy_id
            or evaluation.facts_hash != facts_hash
            or evaluation.proposed_by_run != run.run_id
        ):
            raise TransientError("目录簇评估结果暂不可用")
        return (
            "complete",
            None,
            {
                "evaluation_id": str(evaluation.evaluation_id),
                "evaluation_state": "recorded",
            },
        )


__all__ = ("EvaluateClusterStep",)
