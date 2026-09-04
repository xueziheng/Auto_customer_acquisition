"""Products 与 Approvals 公共契约间的显式 Catalog 映射。"""

from __future__ import annotations

from datetime import UTC, datetime

from domains.approvals.service import (
    CATALOG_POLICY_NAMESPACE,
    CatalogApprovalFact,
    CatalogPolicyApprovalChange,
    CatalogPolicyApprovalCommand,
    CatalogPolicyContentFact,
    CatalogPolicyVersionFact,
    catalog_package_fields,
    catalog_policy_content_hash,
    catalog_policy_request_hash,
)
from domains.products.service import (
    CatalogPolicyChangeSnapshot,
    CatalogProposalPolicyContent,
    CatalogProposalPolicyView,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId


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
    "build_policy_approval_command",
    "policy_workflow_context",
    "require_exact_policy_approval",
)
