"""Products 与 Approvals 公共契约间的显式 Catalog 映射。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast

from domains.approvals.service import (
    CATALOG_CULTIVATION_NAMESPACE,
    CATALOG_CULTIVATION_WARNING,
    CATALOG_POLICY_NAMESPACE,
    CatalogApprovalFact,
    CatalogCultivationApprovalCommand,
    CatalogPolicyApprovalChange,
    CatalogPolicyApprovalCommand,
    CatalogPolicyContentFact,
    CatalogPolicyVersionFact,
    CatalogRuleResultFact,
    catalog_cultivation_request_hash,
    catalog_evidence_locator,
    catalog_package_fields,
    catalog_policy_content_hash,
    catalog_policy_request_hash,
)
from domains.demand.service import NeedClusterCatalogFacts
from domains.products.service import (
    CatalogClusterFactsInput,
    CatalogEvidenceSummaryInput,
    CatalogPolicyChangeSnapshot,
    CatalogProductProposalView,
    CatalogProposalEvaluationView,
    CatalogProposalPolicyContent,
    CatalogProposalPolicyView,
    CatalogProposalRuleResult,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId


def map_catalog_facts(value: NeedClusterCatalogFacts) -> CatalogClusterFactsInput:
    """逐字段把 Demand 快照映射成 Products 自有严格 DTO。"""
    try:
        evidence = tuple(
            CatalogEvidenceSummaryInput(
                source_type=cast(
                    Literal[
                        "conversation",
                        "web_page",
                        "upload",
                        "employee_input",
                        "external_api",
                    ],
                    item.source_type.value,
                ),
                source_id=item.source_id,
                extracted_by=item.extracted_by,
                confirmed_by=item.confirmed_by,
                confirmed_at=item.confirmed_at,
                observed_at=item.observed_at,
                content_hash=item.content_hash,
            )
            for item in value.evidence_summaries
        )
        return CatalogClusterFactsInput(
            tenant_id=value.tenant_id,
            cluster_id=value.cluster_id,
            cluster_category=value.cluster_category,
            member_need_ids=value.member_need_ids,
            distinct_account_ids=value.distinct_account_ids,
            member_count=value.member_count,
            distinct_account_count=value.distinct_account_count,
            known_country_codes=value.known_country_codes,
            unknown_country_account_count=value.unknown_country_account_count,
            recurring_true_account_count=value.recurring_true_account_count,
            recurring_false_account_count=value.recurring_false_account_count,
            recurring_unknown_account_count=value.recurring_unknown_account_count,
            quantity_unit_covered_account_count=(
                value.quantity_unit_covered_account_count
            ),
            unified_unit=value.unified_unit,
            safe_total_quantity=value.safe_total_quantity,
            evidence_summaries=evidence,
            display_codes=value.display_codes,
            facts_observed_at=value.facts_observed_at,
            facts_hash=value.facts_hash,
        )
    except (AttributeError, TypeError, ValueError):
        raise ValidationError("目录产品工作流 Demand 事实无效") from None


def evaluation_workflow_context(
    tenant_id: TenantId,
    policy: CatalogProposalPolicyView,
    facts: NeedClusterCatalogFacts,
) -> dict[str, object]:
    """仅把评估定位符和摘要放入 durable context。"""
    mapped = map_catalog_facts(facts)
    if mapped.tenant_id != tenant_id:
        raise ValidationError("目录产品工作流 Demand 事实租户无效")
    if policy.state != "active":
        raise ValidationError("目录产品工作流活动策略无效")
    return {
        "cluster_id": str(mapped.cluster_id),
        "policy_version_id": str(policy.policy_version_id),
        "policy_content_hash": policy.content_hash,
        "facts_hash": mapped.facts_hash,
    }


def cultivation_workflow_context(
    tenant_id: TenantId,
    proposal: CatalogProductProposalView,
    evaluation: CatalogProposalEvaluationView,
    policy: CatalogProposalPolicyView,
) -> dict[str, object]:
    """构造不含规则、证据或事实正文的培养 workflow context。"""
    try:
        if (
            policy.state != "active"
            or proposal.proposal_id is None
            or proposal.evaluation_id != evaluation.evaluation_id
            or proposal.cluster_id != evaluation.cluster_id
            or proposal.policy_version_id != evaluation.policy_version_id
            or proposal.policy_version_id != policy.policy_version_id
            or proposal.facts_hash != evaluation.facts_hash
            or proposal.owner_employee != policy.proposed_by
            or proposal.proposed_by_run != evaluation.proposed_by_run
            or not evaluation.overall_passed
            or evaluation.blocked_reason is not None
        ):
            raise ValueError
        return {
            "proposal_id": str(proposal.proposal_id),
            "evaluation_id": str(evaluation.evaluation_id),
            "cluster_id": str(proposal.cluster_id),
            "policy_version_id": str(proposal.policy_version_id),
            "policy_content_hash": policy.content_hash,
            "facts_hash": proposal.facts_hash,
            "owner_employee": str(proposal.owner_employee),
            "proposed_by_run": str(proposal.proposed_by_run),
            "change_set_ref": (
                f"catalog-cultivation:{proposal.proposal_id}:"
                f"{proposal.policy_version_id}:{proposal.facts_hash}"
            ),
        }
    except (AttributeError, TypeError, ValueError):
        raise ValidationError("目录产品提案事件事实无效") from None


def _rule(value: CatalogProposalRuleResult) -> CatalogRuleResultFact:
    try:
        return CatalogRuleResultFact(
            rule=value.rule,
            status=value.status,
            actual_value=value.actual_value,
            required_value=value.required_value,
            explanation_code=value.explanation_code,
        )
    except (AttributeError, TypeError, ValueError):
        raise ValidationError("目录产品培养审批规则事实无效") from None


def build_cultivation_approval_command(
    tenant_id: TenantId,
    proposal: CatalogProductProposalView,
    evaluation: CatalogProposalEvaluationView,
    policy: CatalogProposalPolicyView,
    expires_at_limit: datetime,
) -> CatalogCultivationApprovalCommand:
    """从 canonical Products 快照构造 Approvals 自有严格命令。"""
    _utc(expires_at_limit, "expires_at_limit")
    cultivation_workflow_context(tenant_id, proposal, evaluation, policy)
    return build_cultivation_approval_command_from_records(
        tenant_id,
        proposal,
        evaluation,
        policy_content_hash=policy.content_hash,
        expires_at_limit=expires_at_limit,
    )


def build_cultivation_approval_command_from_records(
    tenant_id: TenantId,
    proposal: CatalogProductProposalView,
    evaluation: CatalogProposalEvaluationView,
    *,
    policy_content_hash: str,
    expires_at_limit: datetime,
) -> CatalogCultivationApprovalCommand:
    """用已存提案/评估与原策略摘要重建精确审批命令。"""
    _utc(expires_at_limit, "expires_at_limit")
    try:
        if (
            proposal.evaluation_id != evaluation.evaluation_id
            or proposal.cluster_id != evaluation.cluster_id
            or proposal.policy_version_id != evaluation.policy_version_id
            or proposal.facts_hash != evaluation.facts_hash
            or proposal.proposed_by_run != evaluation.proposed_by_run
            or not evaluation.overall_passed
            or evaluation.blocked_reason is not None
            or len(policy_content_hash) != 64
            or any(
                character not in "0123456789abcdef"
                for character in policy_content_hash
            )
        ):
            raise ValueError
        facts = evaluation.facts
        if not isinstance(facts, CatalogClusterFactsInput):
            raise TypeError
        rules = tuple(_rule(item) for item in evaluation.rule_results)
        evidence_refs_list: list[str] = []
        for item in facts.evidence_summaries:
            if item.source_type not in {"conversation", "web_page", "upload"}:
                raise ValidationError(
                    "目录产品培养审批 Evidence 来源尚无可路由原件"
                )
            evidence_refs_list.append(
                catalog_evidence_locator(
                    source_type=cast(
                        Literal["conversation", "web_page", "upload"],
                        item.source_type,
                    ),
                    source_id=item.source_id,
                    content_hash=item.content_hash,
                )
            )
        evidence_refs = tuple(evidence_refs_list)
        change_set_ref = (
            f"catalog-cultivation:{proposal.proposal_id}:"
            f"{proposal.policy_version_id}:{proposal.facts_hash}"
        )
        request_hash = catalog_cultivation_request_hash(
            tenant_id=tenant_id,
            proposal_id=proposal.proposal_id,
            cluster_id=proposal.cluster_id,
            policy_version_id=proposal.policy_version_id,
            policy_content_hash=policy_content_hash,
            facts_hash=proposal.facts_hash,
            rule_results=rules,
            evidence_refs=evidence_refs,
            proposed_by_run=proposal.proposed_by_run,
            owner_employee=proposal.owner_employee,
            change_set_ref=change_set_ref,
            expires_at_limit=expires_at_limit,
            warning=CATALOG_CULTIVATION_WARNING,
        )
        return CatalogCultivationApprovalCommand(
            tenant_id=tenant_id,
            proposal_id=proposal.proposal_id,
            cluster_id=proposal.cluster_id,
            policy_version_id=proposal.policy_version_id,
            policy_content_hash=policy_content_hash,
            facts_hash=proposal.facts_hash,
            rule_results=rules,
            evidence_refs=evidence_refs,
            proposed_by_run=proposal.proposed_by_run,
            owner_employee=proposal.owner_employee,
            change_set_ref=change_set_ref,
            request_hash=request_hash,
            expires_at_limit=expires_at_limit,
            warning=CATALOG_CULTIVATION_WARNING,
        )
    except ValidationError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise ValidationError("目录产品培养审批 Evidence 事实无效") from None


def require_exact_cultivation_approval(
    command: CatalogCultivationApprovalCommand,
    fact: CatalogApprovalFact,
) -> CatalogApprovalFact:
    """逐字段比较 canonical 培养审批的全部不可变请求。"""
    try:
        title, _, reason, blast, run, employee, evidence = catalog_package_fields(
            command
        )
        change = fact.proposed_change
        exact_change = (
            change.schema_version == CATALOG_CULTIVATION_NAMESPACE
            and change.tenant_id == command.tenant_id
            and change.approval_type == "catalog_product_cultivation"
            and change.proposal_id == command.proposal_id
            and change.cluster_id == command.cluster_id
            and change.policy_version_id == command.policy_version_id
            and change.policy_content_hash == command.policy_content_hash
            and change.facts_hash == command.facts_hash
            and change.rule_results == command.rule_results
            and change.evidence_refs == command.evidence_refs
            and change.warning == command.warning
            and change.request_hash == command.request_hash
        )
        exact = (
            fact.tenant_id == command.tenant_id
            and fact.approval_type == "catalog_product_cultivation"
            and fact.contract_namespace == CATALOG_CULTIVATION_NAMESPACE
            and fact.title == title
            and exact_change
            and fact.reason == reason
            and fact.blast_radius == blast
            and fact.proposed_by_run == run
            and fact.proposed_by_employee == employee
            and fact.owner_employee == command.owner_employee
            and fact.evidence_refs == tuple(evidence)
            and fact.change_set_ref == command.change_set_ref
            and fact.expires_at == command.expires_at_limit
            and fact.expires_at_limit == command.expires_at_limit
            and fact.request_hash == command.request_hash
        )
    except (AttributeError, TypeError, ValueError):
        exact = False
    if not exact:
        raise ValidationError("目录产品培养审批事实与提案不匹配")
    return fact


def _utc(value: datetime, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValidationError(f"目录策略工作流 {field} 无效")
    return value


def _policy_content(value: CatalogProposalPolicyContent) -> CatalogPolicyContentFact:
    """逐字段复制策略；禁止跨域递归序列化。"""
    try:
        return CatalogPolicyContentFact(
            minimum_distinct_accounts=value.minimum_distinct_accounts,
            minimum_recurring_accounts=value.minimum_recurring_accounts,
            minimum_distinct_countries=value.minimum_distinct_countries,
            minimum_quantity_unit_accounts=value.minimum_quantity_unit_accounts,
            require_unified_unit=value.require_unified_unit,
        )
    except (AttributeError, TypeError, ValueError):
        raise ValidationError("目录策略工作流策略内容无效") from None


def _policy_version(value: CatalogProposalPolicyView) -> CatalogPolicyVersionFact:
    content = _policy_content(value.content)
    try:
        fact = CatalogPolicyVersionFact(
            policy_version_id=value.policy_version_id,
            content=content,
            content_hash=value.content_hash,
        )
    except (AttributeError, TypeError, ValueError):
        raise ValidationError("目录策略工作流基准策略无效") from None
    if fact.content_hash != catalog_policy_content_hash(content):
        raise ValidationError("目录策略工作流基准策略摘要无效")
    return fact


def _checked_snapshot(
    snapshot: CatalogPolicyChangeSnapshot,
) -> CatalogPolicyChangeSnapshot:
    if not isinstance(snapshot, CatalogPolicyChangeSnapshot):
        raise ValidationError("目录策略工作流候选快照无效")
    try:
        candidate = snapshot.candidate
        if (
            (candidate.base_active_version_id is None) != (snapshot.base is None)
            or snapshot.base is not None
            and snapshot.base.policy_version_id != candidate.base_active_version_id
        ):
            raise ValidationError("目录策略工作流候选基准不匹配")
        _policy_content(candidate.content)
        if candidate.content_hash != catalog_policy_content_hash(
            _policy_content(candidate.content)
        ):
            raise ValidationError("目录策略工作流候选内容摘要无效")
    except (AttributeError, TypeError, ValueError):
        raise ValidationError("目录策略工作流候选快照无效") from None
    return snapshot


def policy_workflow_context(
    tenant_id: TenantId,
    snapshot: CatalogPolicyChangeSnapshot,
) -> dict[str, object]:
    """构造只含定位元数据的初始 Run context。"""
    if (
        not isinstance(tenant_id, str)
        or not tenant_id
        or tenant_id != tenant_id.strip()
    ):
        raise ValidationError("目录策略工作流租户无效")
    checked = _checked_snapshot(snapshot)
    candidate = checked.candidate
    change_set_ref = (
        f"catalog-policy:{candidate.policy_version_id}:{candidate.content_hash}"
    )
    return {
        "policy_version_id": str(candidate.policy_version_id),
        "content_hash": candidate.content_hash,
        "change_set_ref": change_set_ref,
        "proposed_by": str(candidate.proposed_by),
    }


def build_policy_approval_command(
    tenant_id: TenantId,
    snapshot: CatalogPolicyChangeSnapshot,
    expires_at_limit: datetime,
) -> CatalogPolicyApprovalCommand:
    """从 Products 快照逐字段构造 Approvals 的严格命令。"""
    _utc(expires_at_limit, "expires_at_limit")
    checked = _checked_snapshot(snapshot)
    candidate = checked.candidate
    content = _policy_content(candidate.content)
    base = None if checked.base is None else _policy_version(checked.base)
    change_set_ref = (
        f"catalog-policy:{candidate.policy_version_id}:{candidate.content_hash}"
    )
    request_hash = catalog_policy_request_hash(
        content,
        candidate.proposed_by,
        None if base is None else base.policy_version_id,
    )
    try:
        return CatalogPolicyApprovalCommand(
            tenant_id=tenant_id,
            policy_version_id=candidate.policy_version_id,
            content=content,
            content_hash=candidate.content_hash,
            base_active_version=base,
            proposed_by_employee=candidate.proposed_by,
            owner_employee=candidate.proposed_by,
            change_set_ref=change_set_ref,
            request_hash=request_hash,
            expires_at_limit=expires_at_limit,
        )
    except (TypeError, ValueError):
        raise ValidationError("目录策略审批命令无效") from None


def require_exact_policy_approval(
    command: CatalogPolicyApprovalCommand,
    fact: CatalogApprovalFact,
) -> CatalogApprovalFact:
    """比较 canonical Approval 的全部不可变请求事实。"""
    try:
        title, _, reason, blast, proposed_run, proposed_employee, evidence = (
            catalog_package_fields(command)
        )
        change = fact.proposed_change
        expected_base_id = (
            None
            if command.base_active_version is None
            else command.base_active_version.policy_version_id
        )
        exact_change = (
            isinstance(change, CatalogPolicyApprovalChange)
            and change.schema_version == CATALOG_POLICY_NAMESPACE
            and change.tenant_id == command.tenant_id
            and change.approval_type == "catalog_proposal_policy_change"
            and change.policy_version_id == command.policy_version_id
            and change.content == command.content
            and change.content_hash == command.content_hash
            and change.base_active_version_id == expected_base_id
            and change.before_policy == command.base_active_version
            and change.request_hash == command.request_hash
            and change.configuration_when_missing == "未配置即关闭"
            and change.external_action == "无外部动作"
        )
        exact = (
            isinstance(fact, CatalogApprovalFact)
            and fact.tenant_id == command.tenant_id
            and fact.approval_type == "catalog_proposal_policy_change"
            and fact.contract_namespace == CATALOG_POLICY_NAMESPACE
            and fact.title == title
            and exact_change
            and fact.reason == reason
            and fact.blast_radius == blast
            and fact.proposed_by_run == proposed_run
            and fact.proposed_by_employee == proposed_employee
            and fact.owner_employee == command.owner_employee
            and fact.evidence_refs == tuple(evidence)
            and fact.change_set_ref == command.change_set_ref
            and fact.expires_at == command.expires_at_limit
            and fact.expires_at_limit == command.expires_at_limit
            and fact.request_hash == command.request_hash
        )
    except (AttributeError, TypeError, ValueError):
        exact = False
    if not exact:
        raise ValidationError("目录策略审批事实与候选版本不匹配")
    return fact


__all__ = (
    "build_cultivation_approval_command",
    "build_cultivation_approval_command_from_records",
    "build_policy_approval_command",
    "cultivation_workflow_context",
    "evaluation_workflow_context",
    "map_catalog_facts",
    "policy_workflow_context",
    "require_exact_cultivation_approval",
    "require_exact_policy_approval",
)
