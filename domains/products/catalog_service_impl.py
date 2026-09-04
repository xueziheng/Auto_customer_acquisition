"""Catalog Product Proposal 策略版本生命周期的 tenant-bound 实现。"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import NoReturn, cast

from pydantic import ValidationError as PydanticValidationError

from domains.products.catalog_rules import (
    catalog_policy_content_hash,
    evaluate_catalog_facts,
)
from domains.products.errors import (
    CatalogCultivationCaseNotFoundError,
    CatalogCultivationConflictError,
    CatalogEvaluationConflictError,
    CatalogEvaluationNotFoundError,
    CatalogPolicyApprovalConflictError,
    CatalogPolicyDecisionInvalidError,
    CatalogPolicyIdempotencyConflictError,
    CatalogPolicyNotFoundError,
    CatalogPolicyStateTransitionError,
    CatalogProposalApprovalConflictError,
    CatalogProposalDecisionInvalidError,
    CatalogProposalNotFoundError,
    CatalogProposalStateTransitionError,
)
from domains.products.models import (
    CatalogCultivationCase,
    CatalogProductProposal,
    CatalogProductProposalState,
    CatalogProposalEvaluation,
    CatalogProposalPolicyState,
    CatalogProposalPolicyVersion,
)
from domains.products.permissions import (
    ProductAction,
    ProductActor,
    ProductAuthorizer,
)
from domains.products.repository import CatalogProductsUnitOfWork
from domains.products.schemas import (
    CatalogApprovalDecisionInput,
    CatalogBlockedFactsInput,
    CatalogClusterFactsInput,
    CatalogCultivationCaseView,
    CatalogPolicyChangeSnapshot,
    CatalogProductProposalView,
    CatalogProposalEvaluationResult,
    CatalogProposalEvaluationView,
    CatalogProposalPolicyContent,
    CatalogProposalPolicyView,
)
from domains.products.service import (
    catalog_cultivation_change_set_ref,
    catalog_policy_creation_request_hash,
)
from shared.errors import (
    IdempotencyConflict,
    InvalidStateTransition,
    TenantIsolationViolation,
    TradeOSError,
    TransientError,
    ValidationError,
)
from shared.events.catalog import (
    CatalogCultivationQueued,
    CatalogProductProposalCreated,
    CatalogProposalPolicyActivated,
)
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogCultivationCaseId,
    CatalogProductProposalId,
    CatalogProposalEvaluationId,
    CatalogProposalPolicyVersionId,
    EmployeeId,
    RunId,
    TenantId,
    new_id,
)

_POLICY_UNAVAILABLE = "目录提案策略暂不可用"
_POLICY_NOT_FOUND = "目录提案策略不存在"
_CREATION_CONFLICT = "目录提案策略创建幂等键冲突"
_APPROVAL_CONFLICT = "目录提案策略审批绑定冲突"
_DECISION_INVALID = "目录提案策略审批事实不匹配"
_EVALUATION_UNAVAILABLE = "目录提案评估暂不可用"
_EVALUATION_NOT_FOUND = "目录提案评估不存在"
_EVALUATION_CONFLICT = "目录提案评估不可变 subject 冲突"
_PROPOSAL_UNAVAILABLE = "目录产品提案暂不可用"
_PROPOSAL_NOT_FOUND = "目录产品提案不存在"
_PROPOSAL_APPROVAL_CONFLICT = "目录产品提案审批绑定冲突"
_PROPOSAL_DECISION_INVALID = "目录产品提案审批事实不匹配"
_CULTIVATION_NOT_FOUND = "目录产品培养 Case 不存在"
_CULTIVATION_CONFLICT = "目录产品培养 Case 不可变 subject 冲突"
_FACTS_INVALID = "目录评估当前事实无效"


def _clock(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValidationError("目录提案策略服务时间必须是 UTC")
    return value.astimezone(UTC)


def _creation_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 200
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValidationError("目录提案策略幂等键无效")
    return value


def _policy_id(value: CatalogProposalPolicyVersionId) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith("cpv_")
        or len(value) <= 4
        or len(value) > 40
        or value != value.strip()
    ):
        raise ValidationError("目录提案策略版本 ID 无效")


def _approval_id(value: ApprovalId) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 40
    ):
        raise ValidationError("目录提案策略审批 ID 无效")


def _request_hash(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValidationError("目录提案策略请求摘要无效")


def _catalog_id(value: str, prefix: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith(f"{prefix}_")
        or len(value) <= len(prefix) + 1
        or len(value) > 40
        or value != value.strip()
    ):
        raise ValidationError(f"{field_name} 无效")


def _run_id(value: RunId) -> None:
    _catalog_id(value, "run", "目录评估 Run ID")


def _limit(value: int, subject: str) -> None:
    if type(value) is not int or not 1 <= value <= 200:
        raise ValidationError(f"{subject} limit 必须为 1..200")


def _evaluation_view(
    evaluation: CatalogProposalEvaluation,
) -> CatalogProposalEvaluationView:
    return CatalogProposalEvaluationView(
        evaluation_id=evaluation.evaluation_id,
        cluster_id=evaluation.cluster_id,
        policy_version_id=evaluation.policy_version_id,
        facts_hash=evaluation.facts_hash,
        facts=evaluation.facts,
        rule_results=evaluation.rule_results,
        overall_passed=evaluation.overall_passed,
        blocked_reason=evaluation.blocked_reason,
        proposed_by_run=evaluation.proposed_by_run,
        created_at=evaluation.created_at,
    )


def _proposal_view(proposal: CatalogProductProposal) -> CatalogProductProposalView:
    return CatalogProductProposalView(
        proposal_id=proposal.proposal_id,
        evaluation_id=proposal.evaluation_id,
        cluster_id=proposal.cluster_id,
        policy_version_id=proposal.policy_version_id,
        facts_hash=proposal.facts_hash,
        owner_employee=proposal.owner_employee,
        proposed_by_run=proposal.proposed_by_run,
        approval_id=proposal.approval_id,
        state=proposal.state.value,
        created_at=proposal.created_at,
        updated_at=proposal.updated_at,
    )


def _cultivation_view(value: CatalogCultivationCase) -> CatalogCultivationCaseView:
    return CatalogCultivationCaseView(
        cultivation_case_id=value.cultivation_case_id,
        proposal_id=value.proposal_id,
        approval_id=value.approval_id,
        cluster_id=value.cluster_id,
        policy_version_id=value.policy_version_id,
        facts_hash=value.facts_hash,
        evidence_refs=value.evidence_refs,
        state="queued",
        queued_at=value.queued_at,
    )


def _evaluation_fact(
    value: CatalogProposalEvaluation,
    tenant_id: TenantId,
    *,
    expected_id: CatalogProposalEvaluationId | None = None,
) -> CatalogProposalEvaluation:
    if (
        not isinstance(value, CatalogProposalEvaluation)
        or value.tenant_id != tenant_id
        or (expected_id is not None and value.evaluation_id != expected_id)
    ):
        raise TransientError(_EVALUATION_UNAVAILABLE)
    try:
        value.__post_init__()
    except Exception:  # noqa: BLE001 -- 损坏持久事实不得泄漏原异常
        raise TransientError(_EVALUATION_UNAVAILABLE) from None
    return value


def _proposal_fact(
    value: CatalogProductProposal,
    tenant_id: TenantId,
    *,
    expected_id: CatalogProductProposalId | None = None,
) -> CatalogProductProposal:
    if (
        not isinstance(value, CatalogProductProposal)
        or value.tenant_id != tenant_id
        or (expected_id is not None and value.proposal_id != expected_id)
    ):
        raise TransientError(_PROPOSAL_UNAVAILABLE)
    try:
        value.__post_init__()
    except Exception:  # noqa: BLE001 -- 损坏持久事实不得泄漏原异常
        raise TransientError(_PROPOSAL_UNAVAILABLE) from None
    return value


def _cultivation_fact(
    value: CatalogCultivationCase,
    tenant_id: TenantId,
    *,
    expected_id: CatalogCultivationCaseId | None = None,
) -> CatalogCultivationCase:
    if (
        not isinstance(value, CatalogCultivationCase)
        or value.tenant_id != tenant_id
        or (expected_id is not None and value.cultivation_case_id != expected_id)
    ):
        raise TransientError(_PROPOSAL_UNAVAILABLE)
    try:
        value.__post_init__()
    except Exception:  # noqa: BLE001 -- 损坏持久事实不得泄漏原异常
        raise TransientError(_PROPOSAL_UNAVAILABLE) from None
    return value


def _evaluation_snapshot(
    tenant_id: TenantId,
    facts: CatalogClusterFactsInput,
    result: CatalogProposalEvaluationResult,
) -> CatalogClusterFactsInput | CatalogBlockedFactsInput:
    try:
        if result.blocked_reason is None:
            snapshot: CatalogClusterFactsInput | CatalogBlockedFactsInput = (
                CatalogClusterFactsInput.model_validate(facts.model_dump(mode="python"))
            )
        else:
            snapshot = CatalogBlockedFactsInput.model_validate(
                {
                    "tenant_id": facts.tenant_id,
                    "cluster_id": facts.cluster_id,
                    "facts_hash": facts.facts_hash,
                }
            )
    except (AttributeError, PydanticValidationError, TypeError, ValueError):
        raise ValidationError("目录评估事实定位符无效") from None
    if snapshot.tenant_id != tenant_id:
        raise ValidationError("目录评估事实租户不匹配")
    return snapshot


def _evaluation_locator(
    tenant_id: TenantId, facts: CatalogClusterFactsInput
) -> CatalogBlockedFactsInput:
    try:
        locator = CatalogBlockedFactsInput.model_validate(
            {
                "tenant_id": facts.tenant_id,
                "cluster_id": facts.cluster_id,
                "facts_hash": facts.facts_hash,
            }
        )
    except (AttributeError, PydanticValidationError, TypeError, ValueError):
        raise ValidationError("目录评估事实定位符无效") from None
    if locator.tenant_id != tenant_id:
        raise TenantIsolationViolation("跨租户目录评估事实访问被拒绝")
    return locator


def _current_facts(
    tenant_id: TenantId, facts: CatalogClusterFactsInput
) -> CatalogClusterFactsInput:
    try:
        checked = CatalogClusterFactsInput.model_validate(
            facts.model_dump(mode="python")
        )
    except (AttributeError, PydanticValidationError, TypeError, ValueError):
        raise ValidationError(_FACTS_INVALID) from None
    if checked.tenant_id != tenant_id:
        raise TenantIsolationViolation("跨租户目录评估事实访问被拒绝")
    return checked


def _same_hash_covered_facts(
    current: CatalogClusterFactsInput,
    historical: CatalogClusterFactsInput,
) -> bool:
    fields = (
        "cluster_category",
        "member_need_ids",
        "distinct_account_ids",
        "member_count",
        "distinct_account_count",
        "known_country_codes",
        "unknown_country_account_count",
        "recurring_true_account_count",
        "recurring_false_account_count",
        "recurring_unknown_account_count",
        "quantity_unit_covered_account_count",
        "unified_unit",
        "safe_total_quantity",
        "evidence_summaries",
    )
    return all(
        getattr(current, field) == getattr(historical, field) for field in fields
    )


def _proposal_matches_passed_evaluation(
    proposal: CatalogProductProposal,
    evaluation: CatalogProposalEvaluation,
) -> bool:
    return (
        evaluation.overall_passed
        and evaluation.blocked_reason is None
        and type(evaluation.facts) is CatalogClusterFactsInput
        and proposal.evaluation_id == evaluation.evaluation_id
        and proposal.cluster_id == evaluation.cluster_id
        and proposal.policy_version_id == evaluation.policy_version_id
        and proposal.facts_hash == evaluation.facts_hash
        and proposal.proposed_by_run == evaluation.proposed_by_run
    )


def _same_evaluation(
    existing: CatalogProposalEvaluation,
    snapshot: CatalogClusterFactsInput | CatalogBlockedFactsInput,
    result: CatalogProposalEvaluationResult,
    proposed_by_run: RunId,
) -> bool:
    return (
        existing.facts == snapshot
        and existing.rule_results == result.rule_results
        and existing.overall_passed == result.overall_passed
        and existing.blocked_reason == result.blocked_reason
        and existing.proposed_by_run == proposed_by_run
    )


def _same_proposal(
    proposal: CatalogProductProposal,
    evaluation: CatalogProposalEvaluation,
    owner: EmployeeId,
) -> bool:
    return (
        proposal.evaluation_id == evaluation.evaluation_id
        and proposal.cluster_id == evaluation.cluster_id
        and proposal.policy_version_id == evaluation.policy_version_id
        and proposal.facts_hash == evaluation.facts_hash
        and proposal.owner_employee == owner
        and proposal.proposed_by_run == evaluation.proposed_by_run
    )


def _cultivation_evidence_refs(
    facts: CatalogClusterFactsInput,
) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.source_id for item in facts.evidence_summaries))


def _content(value: CatalogProposalPolicyContent) -> CatalogProposalPolicyContent:
    if not isinstance(value, CatalogProposalPolicyContent):
        raise ValidationError("目录提案策略内容无效")
    try:
        return CatalogProposalPolicyContent.model_validate(
            value.model_dump(mode="python")
        )
    except (AttributeError, PydanticValidationError, TypeError, ValueError):
        raise ValidationError("目录提案策略内容无效") from None


def _decision(value: CatalogApprovalDecisionInput) -> CatalogApprovalDecisionInput:
    if not isinstance(value, CatalogApprovalDecisionInput):
        raise CatalogPolicyDecisionInvalidError(_DECISION_INVALID)
    try:
        return CatalogApprovalDecisionInput.model_validate(
            value.model_dump(mode="python")
        )
    except (AttributeError, PydanticValidationError, TypeError, ValueError):
        raise CatalogPolicyDecisionInvalidError(_DECISION_INVALID) from None


def _view(policy: CatalogProposalPolicyVersion) -> CatalogProposalPolicyView:
    return CatalogProposalPolicyView(
        policy_version_id=policy.policy_version_id,
        content=policy.content,
        content_hash=policy.content_hash,
        base_active_version_id=policy.base_active_version_id,
        proposed_by=policy.proposed_by,
        approval_id=policy.approval_id,
        state=policy.state.value,
        created_at=policy.created_at,
        activated_at=policy.activated_at,
        terminal_at=policy.terminal_at,
    )


def _policy_fact(
    value: CatalogProposalPolicyVersion,
    tenant_id: TenantId,
    *,
    expected_id: CatalogProposalPolicyVersionId | None = None,
) -> CatalogProposalPolicyVersion:
    if (
        not isinstance(value, CatalogProposalPolicyVersion)
        or value.tenant_id != tenant_id
        or (expected_id is not None and value.policy_version_id != expected_id)
    ):
        raise TransientError(_POLICY_UNAVAILABLE)
    try:
        value.__post_init__()
    except Exception:  # noqa: BLE001 -- 损坏持久事实不得泄漏底层异常
        raise TransientError(_POLICY_UNAVAILABLE) from None
    return value


def _active_fact(
    value: CatalogProposalPolicyVersion | None, tenant_id: TenantId
) -> CatalogProposalPolicyVersion | None:
    if value is None:
        return None
    policy = _policy_fact(value, tenant_id)
    if policy.state is not CatalogProposalPolicyState.ACTIVE:
        raise TransientError(_POLICY_UNAVAILABLE)
    return policy


def _same_creation_request(
    existing: CatalogProposalPolicyVersion,
    content: CatalogProposalPolicyContent,
    proposed_by: EmployeeId,
) -> bool:
    return (
        existing.proposed_by == proposed_by
        and existing.content == content
        and existing.content_hash == catalog_policy_content_hash(content)
        and existing.creation_request_hash
        == catalog_policy_creation_request_hash(
            content, proposed_by, existing.base_active_version_id
        )
    )


def _base_matches(
    candidate: CatalogProposalPolicyVersion,
    current: CatalogProposalPolicyVersion | None,
) -> bool:
    if candidate.base_active_version_id is None:
        return current is None
    return (
        current is not None
        and current.policy_version_id == candidate.base_active_version_id
    )


def _require_exact_decision(
    candidate: CatalogProposalPolicyVersion,
    decision: CatalogApprovalDecisionInput,
    now: datetime,
) -> None:
    if (
        decision.approval_type != "catalog_proposal_policy_change"
        or decision.contract_namespace != "catalog-policy-v1"
        or decision.change_set_ref
        != f"catalog-policy:{candidate.policy_version_id}:{candidate.content_hash}"
        or decision.request_hash != candidate.creation_request_hash
        or decision.proposed_by_run is not None
        or decision.proposed_by_employee != candidate.proposed_by
        or decision.owner_employee != candidate.proposed_by
        or decision.approval_id != candidate.approval_id
    ):
        raise CatalogPolicyDecisionInvalidError(_DECISION_INVALID)
    if decision.state in {"approved", "rejected"}:
        if decision.decided_at is None or decision.decided_at > now:
            raise CatalogPolicyDecisionInvalidError(_DECISION_INVALID)
    elif decision.expires_at > now:
        raise CatalogPolicyDecisionInvalidError(_DECISION_INVALID)


def _cultivation_change_set(proposal: CatalogProductProposal) -> str:
    return catalog_cultivation_change_set_ref(
        proposal.proposal_id,
        proposal.policy_version_id,
        proposal.facts_hash,
    )


def _require_exact_cultivation_decision(
    proposal: CatalogProductProposal,
    decision: CatalogApprovalDecisionInput,
    now: datetime,
) -> None:
    if (
        decision.approval_type != "catalog_product_cultivation"
        or decision.contract_namespace != "catalog-cultivation-v1"
        or decision.change_set_ref != _cultivation_change_set(proposal)
        or decision.request_hash != proposal.approval_request_hash
        or decision.proposed_by_run != proposal.proposed_by_run
        or decision.proposed_by_employee is not None
        or decision.owner_employee != proposal.owner_employee
        or decision.approval_id != proposal.approval_id
        or decision.decided_by_employee == proposal.owner_employee
    ):
        raise CatalogProposalDecisionInvalidError(_PROPOSAL_DECISION_INVALID)
    if decision.state in {"approved", "rejected"}:
        if decision.decided_at is None or decision.decided_at > now:
            raise CatalogProposalDecisionInvalidError(_PROPOSAL_DECISION_INVALID)
    elif decision.expires_at > now:
        raise CatalogProposalDecisionInvalidError(_PROPOSAL_DECISION_INVALID)


def _exact_cultivation_case(
    value: CatalogCultivationCase,
    proposal: CatalogProductProposal,
    evidence_refs: tuple[str, ...],
) -> bool:
    return (
        value.proposal_id == proposal.proposal_id
        and value.approval_id == proposal.approval_id
        and value.cluster_id == proposal.cluster_id
        and value.policy_version_id == proposal.policy_version_id
        and value.facts_hash == proposal.facts_hash
        and value.evidence_refs == evidence_refs
        and value.state == "queued"
    )


def _raise_storage_error(error: Exception) -> NoReturn:
    if isinstance(
        error,
        (
            CatalogPolicyApprovalConflictError,
            CatalogPolicyDecisionInvalidError,
            CatalogPolicyIdempotencyConflictError,
            CatalogPolicyNotFoundError,
            CatalogPolicyStateTransitionError,
        ),
    ):
        raise error
    if isinstance(error, IdempotencyConflict):
        raise CatalogPolicyApprovalConflictError(_APPROVAL_CONFLICT) from None
    if isinstance(error, InvalidStateTransition):
        raise CatalogPolicyStateTransitionError("目录提案策略状态转换冲突") from None
    if isinstance(error, (ValidationError, TransientError)):
        raise error
    if isinstance(error, TradeOSError):
        raise TransientError(_POLICY_UNAVAILABLE) from None
    raise TransientError(_POLICY_UNAVAILABLE) from None


class CatalogProposalServiceImpl:
    """策略创建、读取与决定应用；所有写入及事件共享一个 Products UoW。"""

    def __init__(
        self,
        uow_factory: Callable[[TenantId], CatalogProductsUnitOfWork],
        authorizer: ProductAuthorizer,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise ValidationError("目录提案策略事务依赖无效")
        if not isinstance(authorizer, ProductAuthorizer):
            raise ValidationError("目录提案策略授权依赖无效")
        self._uow_factory = uow_factory
        self._authorizer = authorizer
        self._now = now or (lambda: datetime.now(UTC))

    def _require(
        self, tenant_id: TenantId, actor: ProductActor, action: ProductAction
    ) -> None:
        self._authorizer.require(actor, action, tenant_id)

    def _clock(self) -> datetime:
        return _clock(self._now())

    async def create_policy_candidate(
        self,
        tenant_id: TenantId,
        content: CatalogProposalPolicyContent,
        *,
        idempotency_key: str,
        actor: ProductActor,
    ) -> CatalogProposalPolicyVersionId:
        self._require(tenant_id, actor, ProductAction.CATALOG_POLICY_PROPOSE)
        checked_content = _content(content)
        checked_key = _creation_key(idempotency_key)
        proposed_by = EmployeeId(actor.actor_id)
        try:
            async with self._uow_factory(tenant_id) as uow:
                await uow.policies.lock_policy_namespace(tenant_id)
                existing = await uow.policies.get_by_creation_key(
                    tenant_id, proposed_by, checked_key
                )
                if existing is not None:
                    existing = _policy_fact(existing, tenant_id)
                    if not _same_creation_request(
                        existing, checked_content, proposed_by
                    ):
                        raise CatalogPolicyIdempotencyConflictError(_CREATION_CONFLICT)
                    return existing.policy_version_id

                current = _active_fact(
                    await uow.policies.get_active(tenant_id, for_update=True),
                    tenant_id,
                )
                existing = await uow.policies.get_by_creation_key(
                    tenant_id, proposed_by, checked_key
                )
                if existing is not None:
                    existing = _policy_fact(existing, tenant_id)
                    if not _same_creation_request(
                        existing, checked_content, proposed_by
                    ):
                        raise CatalogPolicyIdempotencyConflictError(_CREATION_CONFLICT)
                    return existing.policy_version_id

                base_id = None if current is None else current.policy_version_id
                candidate = CatalogProposalPolicyVersion(
                    tenant_id=tenant_id,
                    policy_version_id=CatalogProposalPolicyVersionId(new_id("cpv")),
                    content=checked_content,
                    content_hash=catalog_policy_content_hash(checked_content),
                    base_active_version_id=base_id,
                    proposed_by=proposed_by,
                    creation_key=checked_key,
                    creation_request_hash=catalog_policy_creation_request_hash(
                        checked_content, proposed_by, base_id
                    ),
                    approval_id=None,
                    state=CatalogProposalPolicyState.PENDING_APPROVAL,
                    created_at=self._clock(),
                )
                stored = _policy_fact(
                    await uow.policies.add(tenant_id, candidate), tenant_id
                )
                if not _same_creation_request(stored, checked_content, proposed_by):
                    raise CatalogPolicyIdempotencyConflictError(_CREATION_CONFLICT)
                return stored.policy_version_id
        except Exception as error:
            if isinstance(error, CatalogPolicyIdempotencyConflictError):
                raise
            if isinstance(error, IdempotencyConflict):
                raise CatalogPolicyIdempotencyConflictError(
                    _CREATION_CONFLICT
                ) from None
            _raise_storage_error(error)

    async def get_active_policy(
        self, tenant_id: TenantId, *, actor: ProductActor
    ) -> CatalogProposalPolicyView | None:
        self._require(tenant_id, actor, ProductAction.CATALOG_POLICY_READ)
        try:
            async with self._uow_factory(tenant_id) as uow:
                active = _active_fact(
                    await uow.policies.get_active(tenant_id), tenant_id
                )
                return None if active is None else _view(active)
        except Exception as error:  # noqa: BLE001 -- 仓储错误统一脱敏
            _raise_storage_error(error)

    async def list_policy_versions(
        self, tenant_id: TenantId, *, actor: ProductActor, limit: int
    ) -> tuple[CatalogProposalPolicyView, ...]:
        self._require(tenant_id, actor, ProductAction.CATALOG_POLICY_READ)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValidationError("目录提案策略历史 limit 必须为 1..200")
        try:
            async with self._uow_factory(tenant_id) as uow:
                page = await uow.policies.list_versions(tenant_id, limit=limit)
                if len(page.items) > limit:
                    raise TransientError(_POLICY_UNAVAILABLE)
                return tuple(
                    _view(_policy_fact(item, tenant_id)) for item in page.items
                )
        except Exception as error:  # noqa: BLE001 -- 仓储错误统一脱敏
            _raise_storage_error(error)

    async def get_policy_change_snapshot(
        self,
        tenant_id: TenantId,
        policy_version_id: CatalogProposalPolicyVersionId,
        *,
        actor: ProductActor,
    ) -> CatalogPolicyChangeSnapshot:
        self._require(tenant_id, actor, ProductAction.CATALOG_SYSTEM_APPLY)
        _policy_id(policy_version_id)
        try:
            async with self._uow_factory(tenant_id) as uow:
                candidate = await uow.policies.get(tenant_id, policy_version_id)
                if candidate is None:
                    raise CatalogPolicyNotFoundError(_POLICY_NOT_FOUND)
                candidate = _policy_fact(
                    candidate, tenant_id, expected_id=policy_version_id
                )
                base = None
                if candidate.base_active_version_id is not None:
                    base = await uow.policies.get(
                        tenant_id, candidate.base_active_version_id
                    )
                    if base is None:
                        raise TransientError(_POLICY_UNAVAILABLE)
                    base = _policy_fact(
                        base,
                        tenant_id,
                        expected_id=candidate.base_active_version_id,
                    )
                current = _active_fact(
                    await uow.policies.get_active(tenant_id), tenant_id
                )
                return CatalogPolicyChangeSnapshot(
                    base=None if base is None else _view(base),
                    current=None if current is None else _view(current),
                    candidate=_view(candidate),
                    base_is_current=_base_matches(candidate, current),
                )
        except Exception as error:  # noqa: BLE001 -- 仓储错误统一脱敏
            _raise_storage_error(error)

    async def bind_policy_approval(
        self,
        tenant_id: TenantId,
        policy_version_id: CatalogProposalPolicyVersionId,
        approval_id: ApprovalId,
        request_hash: str,
        *,
        actor: ProductActor,
    ) -> CatalogProposalPolicyView:
        self._require(tenant_id, actor, ProductAction.CATALOG_SYSTEM_APPLY)
        _policy_id(policy_version_id)
        _approval_id(approval_id)
        _request_hash(request_hash)
        try:
            async with self._uow_factory(tenant_id) as uow:
                candidate = await uow.policies.bind_approval(
                    tenant_id,
                    policy_version_id,
                    approval_id,
                    request_hash,
                )
                if candidate is None:
                    raise CatalogPolicyNotFoundError(_POLICY_NOT_FOUND)
                candidate = _policy_fact(
                    candidate, tenant_id, expected_id=policy_version_id
                )
                if (
                    candidate.approval_id != approval_id
                    or candidate.creation_request_hash != request_hash
                ):
                    raise CatalogPolicyApprovalConflictError(_APPROVAL_CONFLICT)
                return _view(candidate)
        except Exception as error:  # noqa: BLE001 -- 仓储错误统一脱敏
            _raise_storage_error(error)

    async def apply_policy_decision(
        self,
        tenant_id: TenantId,
        policy_version_id: CatalogProposalPolicyVersionId,
        decision: CatalogApprovalDecisionInput,
        *,
        actor: ProductActor,
    ) -> CatalogProposalPolicyView:
        self._require(tenant_id, actor, ProductAction.CATALOG_SYSTEM_APPLY)
        _policy_id(policy_version_id)
        checked_decision = _decision(decision)
        applied_at = self._clock()
        try:
            async with self._uow_factory(tenant_id) as uow:
                await uow.policies.lock_policy_namespace(tenant_id)
                candidate = await uow.policies.get_for_update(
                    tenant_id, policy_version_id
                )
                if candidate is None:
                    raise CatalogPolicyNotFoundError(_POLICY_NOT_FOUND)
                candidate = _policy_fact(
                    candidate, tenant_id, expected_id=policy_version_id
                )
                _require_exact_decision(candidate, checked_decision, applied_at)
                matching_terminal = {
                    "approved": {
                        CatalogProposalPolicyState.ACTIVE,
                        CatalogProposalPolicyState.SUPERSEDED,
                        CatalogProposalPolicyState.STALE,
                    },
                    "rejected": {CatalogProposalPolicyState.REJECTED},
                    "expired": {CatalogProposalPolicyState.EXPIRED},
                }[checked_decision.state]
                if candidate.state in matching_terminal:
                    return _view(candidate)
                if candidate.state is not CatalogProposalPolicyState.PENDING_APPROVAL:
                    raise CatalogPolicyStateTransitionError(
                        "目录提案策略终态不允许应用另一决定"
                    )

                if checked_decision.state == "rejected":
                    return _view(
                        await uow.policies.update(
                            tenant_id,
                            replace(
                                candidate,
                                state=CatalogProposalPolicyState.REJECTED,
                                terminal_at=applied_at,
                            ),
                        )
                    )
                if checked_decision.state == "expired":
                    return _view(
                        await uow.policies.update(
                            tenant_id,
                            replace(
                                candidate,
                                state=CatalogProposalPolicyState.EXPIRED,
                                terminal_at=applied_at,
                            ),
                        )
                    )

                current = _active_fact(
                    await uow.policies.get_active(tenant_id, for_update=True),
                    tenant_id,
                )
                if not _base_matches(candidate, current):
                    stale = await uow.policies.update(
                        tenant_id,
                        replace(
                            candidate,
                            state=CatalogProposalPolicyState.STALE,
                            terminal_at=applied_at,
                        ),
                    )
                    return _view(stale)
                if current is not None:
                    await uow.policies.update(
                        tenant_id,
                        replace(
                            current,
                            state=CatalogProposalPolicyState.SUPERSEDED,
                            terminal_at=applied_at,
                        ),
                    )
                active = await uow.policies.update(
                    tenant_id,
                    replace(
                        candidate,
                        state=CatalogProposalPolicyState.ACTIVE,
                        activated_at=applied_at,
                    ),
                )
                await uow.bus.publish(
                    CatalogProposalPolicyActivated(
                        tenant_id=tenant_id,
                        occurred_at=applied_at,
                        policy_version_id=active.policy_version_id,
                        content_hash=active.content_hash,
                    )
                )
                return _view(active)
        except Exception as error:  # noqa: BLE001 -- 仓储错误统一脱敏
            _raise_storage_error(error)

    async def evaluate_cluster(
        self,
        tenant_id: TenantId,
        facts: CatalogClusterFactsInput,
        *,
        proposed_by_run: RunId,
        actor: ProductActor,
    ) -> CatalogProposalEvaluationView:
        self._require(tenant_id, actor, ProductAction.CATALOG_EVALUATE)
        locator = _evaluation_locator(tenant_id, facts)
        _run_id(proposed_by_run)
        try:
            async with self._uow_factory(tenant_id) as uow:
                await uow.evaluations.require_trusted_catalog_evaluation_run(
                    tenant_id, proposed_by_run, locator.cluster_id
                )
                await uow.policies.lock_policy_namespace(tenant_id)
                policy = _active_fact(
                    await uow.policies.get_active(tenant_id, for_update=True),
                    tenant_id,
                )
                if policy is None:
                    raise TransientError("没有活动目录提案策略")
                result = evaluate_catalog_facts(policy.content, facts)
                snapshot = _evaluation_snapshot(tenant_id, facts, result)
                existing = await uow.evaluations.get_by_subject(
                    tenant_id,
                    snapshot.cluster_id,
                    policy.policy_version_id,
                    snapshot.facts_hash,
                )
                if existing is not None:
                    existing = _evaluation_fact(existing, tenant_id)
                    if not _same_evaluation(
                        existing, snapshot, result, proposed_by_run
                    ):
                        raise CatalogEvaluationConflictError(_EVALUATION_CONFLICT)
                    if existing.overall_passed:
                        proposal = await uow.proposals.get_by_evaluation(
                            tenant_id, existing.evaluation_id
                        )
                        if proposal is None:
                            raise TransientError(_PROPOSAL_UNAVAILABLE)
                        proposal = _proposal_fact(proposal, tenant_id)
                        if not _same_proposal(proposal, existing, policy.proposed_by):
                            raise CatalogEvaluationConflictError(_EVALUATION_CONFLICT)
                    return _evaluation_view(existing)

                created_at = self._clock()
                evaluation = CatalogProposalEvaluation(
                    tenant_id=tenant_id,
                    evaluation_id=CatalogProposalEvaluationId(new_id("cpe")),
                    cluster_id=snapshot.cluster_id,
                    policy_version_id=policy.policy_version_id,
                    facts_hash=snapshot.facts_hash,
                    facts=snapshot,
                    rule_results=result.rule_results,
                    overall_passed=result.overall_passed,
                    blocked_reason=result.blocked_reason,
                    proposed_by_run=proposed_by_run,
                    created_at=created_at,
                )
                stored = _evaluation_fact(
                    await uow.evaluations.add(tenant_id, evaluation), tenant_id
                )
                if not _same_evaluation(stored, snapshot, result, proposed_by_run):
                    raise CatalogEvaluationConflictError(_EVALUATION_CONFLICT)
                if not stored.overall_passed:
                    return _evaluation_view(stored)

                proposal = CatalogProductProposal(
                    tenant_id=tenant_id,
                    proposal_id=CatalogProductProposalId(new_id("cpr")),
                    evaluation_id=stored.evaluation_id,
                    cluster_id=stored.cluster_id,
                    policy_version_id=stored.policy_version_id,
                    facts_hash=stored.facts_hash,
                    owner_employee=policy.proposed_by,
                    proposed_by_run=stored.proposed_by_run,
                    approval_id=None,
                    approval_request_hash=None,
                    state=CatalogProductProposalState.AWAITING_APPROVAL_SUBMISSION,
                    created_at=created_at,
                    updated_at=created_at,
                )
                stored_proposal = _proposal_fact(
                    await uow.proposals.add(tenant_id, proposal), tenant_id
                )
                if not _same_proposal(stored_proposal, stored, policy.proposed_by):
                    raise CatalogEvaluationConflictError(_EVALUATION_CONFLICT)
                await uow.bus.publish(
                    CatalogProductProposalCreated(
                        tenant_id=tenant_id,
                        occurred_at=created_at,
                        run_id=proposed_by_run,
                        proposal_id=stored_proposal.proposal_id,
                        evaluation_id=stored.evaluation_id,
                        cluster_id=stored.cluster_id,
                        policy_version_id=stored.policy_version_id,
                        facts_hash=stored.facts_hash,
                    )
                )
                return _evaluation_view(stored)
        except Exception as error:
            if isinstance(error, CatalogEvaluationConflictError):
                raise
            if isinstance(error, IdempotencyConflict):
                raise CatalogEvaluationConflictError(_EVALUATION_CONFLICT) from None
            _raise_storage_error(error)

    async def get_evaluation(
        self,
        tenant_id: TenantId,
        evaluation_id: CatalogProposalEvaluationId,
        *,
        actor: ProductActor,
    ) -> CatalogProposalEvaluationView:
        self._require(tenant_id, actor, ProductAction.CATALOG_PROPOSAL_READ)
        _catalog_id(evaluation_id, "cpe", "目录评估 ID")
        try:
            async with self._uow_factory(tenant_id) as uow:
                value = await uow.evaluations.get(tenant_id, evaluation_id)
                if value is None:
                    raise CatalogEvaluationNotFoundError(_EVALUATION_NOT_FOUND)
                return _evaluation_view(
                    _evaluation_fact(value, tenant_id, expected_id=evaluation_id)
                )
        except Exception as error:
            if isinstance(error, CatalogEvaluationNotFoundError):
                raise
            _raise_storage_error(error)

    async def list_evaluations(
        self, tenant_id: TenantId, *, actor: ProductActor, limit: int
    ) -> tuple[CatalogProposalEvaluationView, ...]:
        self._require(tenant_id, actor, ProductAction.CATALOG_PROPOSAL_READ)
        _limit(limit, "目录评估历史")
        try:
            async with self._uow_factory(tenant_id) as uow:
                page = await uow.evaluations.list_evaluations(tenant_id, limit=limit)
                if len(page.items) > limit:
                    raise TransientError(_EVALUATION_UNAVAILABLE)
                return tuple(
                    _evaluation_view(_evaluation_fact(item, tenant_id))
                    for item in page.items
                )
        except Exception as error:  # noqa: BLE001 -- 仓储错误统一脱敏
            _raise_storage_error(error)

    async def get_proposal(
        self,
        tenant_id: TenantId,
        proposal_id: CatalogProductProposalId,
        *,
        actor: ProductActor,
    ) -> CatalogProductProposalView:
        self._require(tenant_id, actor, ProductAction.CATALOG_PROPOSAL_READ)
        _catalog_id(proposal_id, "cpr", "目录产品提案 ID")
        try:
            async with self._uow_factory(tenant_id) as uow:
                value = await uow.proposals.get(tenant_id, proposal_id)
                if value is None:
                    raise CatalogProposalNotFoundError(_PROPOSAL_NOT_FOUND)
                return _proposal_view(
                    _proposal_fact(value, tenant_id, expected_id=proposal_id)
                )
        except Exception as error:
            if isinstance(error, CatalogProposalNotFoundError):
                raise
            _raise_storage_error(error)

    async def list_proposals(
        self, tenant_id: TenantId, *, actor: ProductActor, limit: int
    ) -> tuple[CatalogProductProposalView, ...]:
        self._require(tenant_id, actor, ProductAction.CATALOG_PROPOSAL_READ)
        _limit(limit, "目录产品提案历史")
        try:
            async with self._uow_factory(tenant_id) as uow:
                page = await uow.proposals.list_proposals(tenant_id, limit=limit)
                if len(page.items) > limit:
                    raise TransientError(_PROPOSAL_UNAVAILABLE)
                return tuple(
                    _proposal_view(_proposal_fact(item, tenant_id))
                    for item in page.items
                )
        except Exception as error:  # noqa: BLE001 -- 仓储错误统一脱敏
            _raise_storage_error(error)

    async def bind_proposal_approval(
        self,
        tenant_id: TenantId,
        proposal_id: CatalogProductProposalId,
        approval_id: ApprovalId,
        request_hash: str,
        *,
        actor: ProductActor,
    ) -> CatalogProductProposalView:
        self._require(tenant_id, actor, ProductAction.CATALOG_SYSTEM_APPLY)
        _catalog_id(proposal_id, "cpr", "目录产品提案 ID")
        _approval_id(approval_id)
        _request_hash(request_hash)
        try:
            async with self._uow_factory(tenant_id) as uow:
                proposal = await uow.proposals.bind_approval(
                    tenant_id,
                    proposal_id,
                    approval_id,
                    request_hash,
                    self._clock(),
                )
                if proposal is None:
                    raise CatalogProposalNotFoundError(_PROPOSAL_NOT_FOUND)
                proposal = _proposal_fact(proposal, tenant_id, expected_id=proposal_id)
                if (
                    proposal.approval_id != approval_id
                    or proposal.approval_request_hash != request_hash
                ):
                    raise CatalogProposalApprovalConflictError(
                        _PROPOSAL_APPROVAL_CONFLICT
                    )
                return _proposal_view(proposal)
        except Exception as error:
            if isinstance(
                error,
                (
                    CatalogProposalApprovalConflictError,
                    CatalogProposalNotFoundError,
                ),
            ):
                raise
            if isinstance(error, IdempotencyConflict):
                raise CatalogProposalApprovalConflictError(
                    _PROPOSAL_APPROVAL_CONFLICT
                ) from None
            if isinstance(error, InvalidStateTransition):
                raise CatalogProposalStateTransitionError(
                    "目录产品提案状态不允许绑定审批"
                ) from None
            _raise_storage_error(error)

    async def apply_cultivation_decision(
        self,
        tenant_id: TenantId,
        proposal_id: CatalogProductProposalId,
        decision: CatalogApprovalDecisionInput,
        current_facts: CatalogClusterFactsInput,
        *,
        actor: ProductActor,
    ) -> CatalogProductProposalView:
        self._require(tenant_id, actor, ProductAction.CATALOG_SYSTEM_APPLY)
        checked_current = _current_facts(tenant_id, current_facts)
        _catalog_id(proposal_id, "cpr", "目录产品提案 ID")
        try:
            checked_decision = _decision(decision)
        except CatalogPolicyDecisionInvalidError:
            raise CatalogProposalDecisionInvalidError(
                _PROPOSAL_DECISION_INVALID
            ) from None
        applied_at = self._clock()
        try:
            async with self._uow_factory(tenant_id) as uow:
                await uow.policies.lock_policy_namespace(tenant_id)
                proposal = await uow.proposals.get_for_update(tenant_id, proposal_id)
                if proposal is None:
                    raise CatalogProposalNotFoundError(_PROPOSAL_NOT_FOUND)
                proposal = _proposal_fact(proposal, tenant_id, expected_id=proposal_id)
                if checked_current.cluster_id != proposal.cluster_id:
                    raise ValidationError(_FACTS_INVALID)
                active = _active_fact(
                    await uow.policies.get_active(tenant_id, for_update=True),
                    tenant_id,
                )
                _require_exact_cultivation_decision(
                    proposal, checked_decision, applied_at
                )
                try:
                    evaluation = await uow.evaluations.get(
                        tenant_id, proposal.evaluation_id
                    )
                    if evaluation is None:
                        raise ValueError("evaluation missing")
                    evaluation = _evaluation_fact(evaluation, tenant_id)
                except (
                    PydanticValidationError,
                    TransientError,
                    ValidationError,
                    TypeError,
                    ValueError,
                ):
                    raise CatalogCultivationConflictError(
                        _CULTIVATION_CONFLICT
                    ) from None
                if not _proposal_matches_passed_evaluation(proposal, evaluation):
                    raise CatalogCultivationConflictError(_CULTIVATION_CONFLICT)
                historical_facts = cast(CatalogClusterFactsInput, evaluation.facts)
                matching_terminal = {
                    "approved": {
                        CatalogProductProposalState.CULTIVATION_QUEUED,
                        CatalogProductProposalState.STALE,
                    },
                    "rejected": {CatalogProductProposalState.REJECTED},
                    "expired": {CatalogProductProposalState.EXPIRED},
                }[checked_decision.state]
                if proposal.state in matching_terminal:
                    if proposal.state is CatalogProductProposalState.CULTIVATION_QUEUED:
                        evidence_refs = _cultivation_evidence_refs(historical_facts)
                        cultivation = await uow.cultivation_cases.get_by_proposal(
                            tenant_id, proposal.proposal_id
                        )
                        if cultivation is None:
                            raise TransientError(_PROPOSAL_UNAVAILABLE)
                        cultivation = _cultivation_fact(cultivation, tenant_id)
                        if not _exact_cultivation_case(
                            cultivation, proposal, evidence_refs
                        ):
                            raise CatalogCultivationConflictError(_CULTIVATION_CONFLICT)
                    return _proposal_view(proposal)
                if proposal.state is not CatalogProductProposalState.PENDING_REVIEW:
                    raise CatalogProposalStateTransitionError(
                        "目录产品提案终态不允许应用另一决定"
                    )
                if checked_decision.state == "rejected":
                    return _proposal_view(
                        await uow.proposals.update(
                            tenant_id,
                            replace(
                                proposal,
                                state=CatalogProductProposalState.REJECTED,
                                updated_at=applied_at,
                            ),
                        )
                    )
                if checked_decision.state == "expired":
                    return _proposal_view(
                        await uow.proposals.update(
                            tenant_id,
                            replace(
                                proposal,
                                state=CatalogProductProposalState.EXPIRED,
                                updated_at=applied_at,
                            ),
                        )
                    )

                if (
                    active is None
                    or active.policy_version_id != proposal.policy_version_id
                    or checked_current.facts_hash != proposal.facts_hash
                    or not _same_hash_covered_facts(checked_current, historical_facts)
                ):
                    stale = await uow.proposals.update(
                        tenant_id,
                        replace(
                            proposal,
                            state=CatalogProductProposalState.STALE,
                            updated_at=applied_at,
                        ),
                    )
                    return _proposal_view(stale)

                if not _same_proposal(proposal, evaluation, active.proposed_by):
                    raise CatalogCultivationConflictError(_CULTIVATION_CONFLICT)
                evidence_refs = _cultivation_evidence_refs(historical_facts)
                if not evidence_refs:
                    raise TransientError(_EVALUATION_UNAVAILABLE)
                queued = _proposal_fact(
                    await uow.proposals.update(
                        tenant_id,
                        replace(
                            proposal,
                            state=CatalogProductProposalState.CULTIVATION_QUEUED,
                            updated_at=applied_at,
                        ),
                    ),
                    tenant_id,
                )
                if queued.approval_id is None:
                    raise TransientError(_PROPOSAL_UNAVAILABLE)
                cultivation = CatalogCultivationCase(
                    tenant_id=tenant_id,
                    cultivation_case_id=CatalogCultivationCaseId(new_id("ccc")),
                    proposal_id=queued.proposal_id,
                    approval_id=queued.approval_id,
                    cluster_id=queued.cluster_id,
                    policy_version_id=queued.policy_version_id,
                    facts_hash=queued.facts_hash,
                    evidence_refs=evidence_refs,
                    state="queued",
                    queued_at=applied_at,
                )
                stored_case = _cultivation_fact(
                    await uow.cultivation_cases.add(tenant_id, cultivation),
                    tenant_id,
                )
                if not _exact_cultivation_case(stored_case, queued, evidence_refs):
                    raise CatalogCultivationConflictError(_CULTIVATION_CONFLICT)
                await uow.bus.publish(
                    CatalogCultivationQueued(
                        tenant_id=tenant_id,
                        occurred_at=applied_at,
                        cultivation_case_id=stored_case.cultivation_case_id,
                        proposal_id=queued.proposal_id,
                    )
                )
                return _proposal_view(queued)
        except Exception as error:
            if isinstance(
                error,
                (
                    CatalogCultivationConflictError,
                    CatalogProposalDecisionInvalidError,
                    CatalogProposalNotFoundError,
                    CatalogProposalStateTransitionError,
                ),
            ):
                raise
            if isinstance(error, IdempotencyConflict):
                raise CatalogCultivationConflictError(_CULTIVATION_CONFLICT) from None
            if isinstance(error, InvalidStateTransition):
                raise CatalogProposalStateTransitionError(
                    "目录产品提案状态转换冲突"
                ) from None
            _raise_storage_error(error)

    async def get_cultivation_case(
        self,
        tenant_id: TenantId,
        cultivation_case_id: CatalogCultivationCaseId,
        *,
        actor: ProductActor,
    ) -> CatalogCultivationCaseView:
        self._require(tenant_id, actor, ProductAction.CATALOG_CULTIVATION_READ)
        _catalog_id(cultivation_case_id, "ccc", "目录产品培养 Case ID")
        try:
            async with self._uow_factory(tenant_id) as uow:
                value = await uow.cultivation_cases.get(tenant_id, cultivation_case_id)
                if value is None:
                    raise CatalogCultivationCaseNotFoundError(_CULTIVATION_NOT_FOUND)
                return _cultivation_view(
                    _cultivation_fact(value, tenant_id, expected_id=cultivation_case_id)
                )
        except Exception as error:
            if isinstance(error, CatalogCultivationCaseNotFoundError):
                raise
            _raise_storage_error(error)

    async def list_cultivation_cases(
        self, tenant_id: TenantId, *, actor: ProductActor, limit: int
    ) -> tuple[CatalogCultivationCaseView, ...]:
        self._require(tenant_id, actor, ProductAction.CATALOG_CULTIVATION_READ)
        _limit(limit, "目录产品培养队列")
        try:
            async with self._uow_factory(tenant_id) as uow:
                page = await uow.cultivation_cases.list_cases(tenant_id, limit=limit)
                if len(page.items) > limit:
                    raise TransientError(_PROPOSAL_UNAVAILABLE)
                return tuple(
                    _cultivation_view(_cultivation_fact(item, tenant_id))
                    for item in page.items
                )
        except Exception as error:  # noqa: BLE001 -- 仓储错误统一脱敏
            _raise_storage_error(error)


__all__ = ("CatalogProposalServiceImpl",)
