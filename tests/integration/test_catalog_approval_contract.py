"""Catalog Proposal 两类中央审批的严格、租户安全契约。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from domains.approvals.service_impl import ApprovalServiceImpl
from infra.db.approval_uow import SqlAlchemyApprovalUnitOfWork
from infra.db.tables import ApprovalPackageRow
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414 -- 真实隔离 PostgreSQL 夹具
)

NOW = datetime(2026, 9, 5, 8, tzinfo=UTC)
TENANT = "tn_catalog_approval"
POLICY = "cpv_01K00000000000000000000000"
PROPOSAL = "cpr_01K00000000000000000000000"
CLUSTER = "ncl_01K00000000000000000000000"
RUN = "run_01K00000000000000000000000"
OWNER = "emp_01K00000000000000000000010"
HASH_A = "a" * 64
HASH_B = "b" * 64
BOSS = "emp_01K00000000000000000000011"


def test_catalog_policy_hash_matches_the_existing_products_creation_commitment() -> (
    None
):
    """若Approvals重写算法漂移，Task 7 的原子绑定会永久拒绝合法审批。"""
    from domains.approvals.catalog_contract import (
        CatalogPolicyContentFact,
        catalog_policy_content_hash,
        catalog_policy_request_hash,
    )
    from domains.products.catalog_rules import (
        catalog_policy_content_hash as products_policy_content_hash,
    )
    from domains.products.schemas import CatalogProposalPolicyContent
    from domains.products.service import catalog_policy_creation_request_hash
    from shared.schemas.identifiers import EmployeeId

    content = CatalogPolicyContentFact(
        minimum_distinct_accounts=3,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=None,
        minimum_quantity_unit_accounts=None,
        require_unified_unit=False,
    )
    products_content = CatalogProposalPolicyContent.model_validate(
        content.model_dump(mode="python")
    )
    proposer = EmployeeId("emp_01K00000000000000000000010")

    assert catalog_policy_content_hash(content) == catalog_policy_content_hash(
        CatalogPolicyContentFact.model_validate(content.model_dump(mode="python"))
    )
    assert catalog_policy_content_hash(content) == products_policy_content_hash(
        products_content
    )
    assert catalog_policy_request_hash(content, proposer, None) == (
        catalog_policy_creation_request_hash(products_content, proposer, None)
    )


def _policy_content(**changes):
    from domains.approvals.catalog_contract import CatalogPolicyContentFact

    values = {
        "minimum_distinct_accounts": 3,
        "minimum_recurring_accounts": None,
        "minimum_distinct_countries": None,
        "minimum_quantity_unit_accounts": None,
        "require_unified_unit": False,
    }
    return CatalogPolicyContentFact.model_validate(values | changes)


def _rules():
    from domains.approvals.catalog_contract import CatalogRuleResultFact

    return tuple(
        CatalogRuleResultFact.model_validate(item)
        for item in (
            {
                "rule": "membership_integrity",
                "status": "passed",
                "actual_value": True,
                "required_value": True,
                "explanation_code": "成员关系与品类完整一致",
            },
            {
                "rule": "distinct_accounts",
                "status": "passed",
                "actual_value": 3,
                "required_value": 3,
                "explanation_code": "去重客户数达到策略门槛",
            },
            {
                "rule": "recurring_accounts",
                "status": "not_required",
                "actual_value": 1,
                "required_value": None,
                "explanation_code": "策略不要求复购客户数",
            },
            {
                "rule": "distinct_countries",
                "status": "not_required",
                "actual_value": 2,
                "required_value": None,
                "explanation_code": "策略不要求已知国家数",
            },
            {
                "rule": "quantity_unit_coverage",
                "status": "not_required",
                "actual_value": 3,
                "required_value": None,
                "explanation_code": "策略不要求数量单位覆盖",
            },
            {
                "rule": "unified_unit",
                "status": "not_required",
                "actual_value": "pcs",
                "required_value": None,
                "explanation_code": "策略不要求统一单位",
            },
        )
    )


def _cultivation_hash(**changes):
    from domains.approvals.catalog_contract import (
        CATALOG_CULTIVATION_WARNING,
        catalog_cultivation_request_hash,
    )

    values = {
        "tenant_id": TENANT,
        "proposal_id": PROPOSAL,
        "cluster_id": CLUSTER,
        "policy_version_id": POLICY,
        "policy_content_hash": HASH_A,
        "facts_hash": HASH_B,
        "rule_results": _rules(),
        "evidence_refs": ("msg_01K00000000000000000000000",),
        "proposed_by_run": RUN,
        "owner_employee": OWNER,
        "change_set_ref": f"catalog-cultivation:{PROPOSAL}:{POLICY}:{HASH_B}",
        "expires_at_limit": NOW + timedelta(days=3),
        "warning": CATALOG_CULTIVATION_WARNING,
    }
    return catalog_cultivation_request_hash(**(values | changes))


def _policy_command():
    from domains.approvals.catalog_contract import (
        CatalogPolicyApprovalCommand,
        catalog_policy_content_hash,
        catalog_policy_request_hash,
    )

    content = _policy_content()
    content_hash = catalog_policy_content_hash(content)
    return CatalogPolicyApprovalCommand(
        tenant_id=TENANT,
        policy_version_id=POLICY,
        content=content,
        content_hash=content_hash,
        base_active_version=None,
        proposed_by_employee=OWNER,
        owner_employee=OWNER,
        change_set_ref=f"catalog-policy:{POLICY}:{content_hash}",
        request_hash=catalog_policy_request_hash(content, OWNER, None),
        expires_at_limit=NOW + timedelta(days=7),
    )


def _cultivation_command():
    from domains.approvals.catalog_contract import (
        CATALOG_CULTIVATION_WARNING,
        CatalogCultivationApprovalCommand,
    )

    return CatalogCultivationApprovalCommand(
        tenant_id=TENANT,
        proposal_id=PROPOSAL,
        cluster_id=CLUSTER,
        policy_version_id=POLICY,
        policy_content_hash=HASH_A,
        facts_hash=HASH_B,
        rule_results=_rules(),
        evidence_refs=("msg_01K00000000000000000000000",),
        proposed_by_run=RUN,
        owner_employee=OWNER,
        change_set_ref=f"catalog-cultivation:{PROPOSAL}:{POLICY}:{HASH_B}",
        request_hash=_cultivation_hash(),
        expires_at_limit=NOW + timedelta(days=3),
        warning=CATALOG_CULTIVATION_WARNING,
    )


class _BossReader:
    async def read_actor(self, tenant_id, employee_id):
        from domains.approvals.catalog_contract import CatalogApprovalActorFact

        return CatalogApprovalActorFact(
            tenant_id=tenant_id,
            employee_id=employee_id,
            current_role="boss",
            active=True,
            eligible=True,
        )


def test_catalog_cultivation_hash_is_deterministic_lower_hex_and_binds_every_subject() -> (
    None
):
    digest = _cultivation_hash()
    assert digest == _cultivation_hash()
    assert len(digest) == 64 and digest == digest.lower()
    mutations = (
        {"tenant_id": "tn_other"},
        {"proposal_id": "cpr_other"},
        {"cluster_id": "ncl_other"},
        {"policy_version_id": "cpv_other"},
        {"policy_content_hash": "c" * 64},
        {"facts_hash": "d" * 64},
        {"rule_results": tuple(reversed(_rules()))},
        {"evidence_refs": ("msg_other",)},
        {"proposed_by_run": "run_other"},
        {"owner_employee": "emp_other"},
        {"change_set_ref": "catalog-cultivation:other"},
        {"expires_at_limit": NOW + timedelta(days=2)},
        {"warning": "不是完整的固定风险提示"},
    )
    assert all(_cultivation_hash(**mutation) != digest for mutation in mutations)


def test_catalog_commands_are_strict_and_cannot_carry_raw_urls_prices_or_probabilities() -> (
    None
):
    from pydantic import ValidationError as SchemaError

    from domains.approvals.catalog_contract import (
        CATALOG_CULTIVATION_WARNING,
        CatalogCultivationApprovalCommand,
        CatalogPolicyApprovalCommand,
        catalog_policy_content_hash,
        catalog_policy_request_hash,
    )

    content = _policy_content()
    policy_values = {
        "tenant_id": TENANT,
        "policy_version_id": POLICY,
        "content": content,
        "content_hash": catalog_policy_content_hash(content),
        "base_active_version": None,
        "proposed_by_employee": OWNER,
        "owner_employee": OWNER,
        "change_set_ref": (
            f"catalog-policy:{POLICY}:{catalog_policy_content_hash(content)}"
        ),
        "request_hash": catalog_policy_request_hash(content, OWNER, None),
        "expires_at_limit": NOW + timedelta(days=7),
    }
    policy = CatalogPolicyApprovalCommand.model_validate(policy_values)
    cultivation_values = {
        "tenant_id": TENANT,
        "proposal_id": PROPOSAL,
        "cluster_id": CLUSTER,
        "policy_version_id": POLICY,
        "policy_content_hash": HASH_A,
        "facts_hash": HASH_B,
        "rule_results": _rules(),
        "evidence_refs": ("msg_01K00000000000000000000000",),
        "proposed_by_run": RUN,
        "owner_employee": OWNER,
        "change_set_ref": f"catalog-cultivation:{PROPOSAL}:{POLICY}:{HASH_B}",
        "request_hash": _cultivation_hash(),
        "expires_at_limit": NOW + timedelta(days=3),
        "warning": CATALOG_CULTIVATION_WARNING,
    }
    cultivation = CatalogCultivationApprovalCommand.model_validate(cultivation_values)

    assert policy.model_dump()["content"] == content.model_dump()
    assert cultivation.warning == CATALOG_CULTIVATION_WARNING
    for field, value in (
        ("source_url", "https://example.invalid/raw"),
        ("price", "9.99"),
        ("probability", 0.9),
        ("raw_text", "customer words"),
    ):
        with pytest.raises(SchemaError):
            CatalogCultivationApprovalCommand.model_validate(
                cultivation_values | {field: value}
            )
    with pytest.raises(SchemaError):
        CatalogCultivationApprovalCommand.model_validate(
            cultivation_values
            | {"evidence_refs": ("https://example.invalid/original",)}
        )


@pytest.mark.parametrize(
    "payload",
    (
        {
            "rule": "distinct_accounts",
            "status": "passed",
            "actual_value": "customer said three",
            "required_value": 3,
            "explanation_code": "去重客户数达到策略门槛",
        },
        {
            "rule": "distinct_accounts",
            "status": "failed",
            "actual_value": 3,
            "required_value": 3,
            "explanation_code": "去重客户数未达到策略门槛",
        },
        {
            "rule": "membership_integrity",
            "status": "passed",
            "actual_value": 1,
            "required_value": True,
            "explanation_code": "成员关系与品类完整一致",
        },
        {
            "rule": "recurring_accounts",
            "status": "passed",
            "actual_value": 3,
            "required_value": 2,
            "explanation_code": "复购客户数未达到策略门槛",
        },
        {
            "rule": "unified_unit",
            "status": "failed",
            "actual_value": "pcs",
            "required_value": True,
            "explanation_code": "有效数量单位不统一",
        },
        {
            "rule": "quantity_unit_coverage",
            "status": "passed",
            "actual_value": None,
            "required_value": 2,
            "explanation_code": "目录事实损坏，评估已阻断",
        },
    ),
)
def test_catalog_rule_facts_reject_raw_text_and_semantic_contradictions(
    payload: dict[str, object],
) -> None:
    from pydantic import ValidationError as SchemaError

    from domains.approvals.catalog_contract import CatalogRuleResultFact

    with pytest.raises(SchemaError):
        CatalogRuleResultFact.model_validate(payload)


@pytest.mark.asyncio
async def test_catalog_postgres_concurrent_replay_converges_across_all_states(
    unit_engine,
) -> None:
    """advisory xact lock 必须让同 ref 并发收敛，决定后也不得创建第二包。"""
    from domains.approvals.catalog_contract import CatalogApprovalContractError

    sessions = async_sessionmaker(unit_engine, expire_on_commit=False)
    clock = [NOW]
    service = ApprovalServiceImpl(
        lambda tenant: SqlAlchemyApprovalUnitOfWork(
            sessions, tenant, now=lambda: clock[0]
        ),
        catalog_actor_reader=_BossReader(),
        now=lambda: clock[0],
    )
    policy = _policy_command()
    cultivation = _cultivation_command()

    policy_ids = await asyncio.gather(
        service.submit_catalog(policy), service.submit_catalog(policy)
    )
    cultivation_ids = await asyncio.gather(
        service.submit_catalog(cultivation), service.submit_catalog(cultivation)
    )
    assert policy_ids[0] == policy_ids[1]
    assert cultivation_ids[0] == cultivation_ids[1]
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ApprovalPackageRow)
                .where(ApprovalPackageRow.tenant_id == TENANT)
            )
            == 2
        )

    for approval_id in (policy_ids[0], cultivation_ids[0]):
        await service.decide(TENANT, approval_id, True, BOSS)
        await service.mark_applied(TENANT, approval_id, f"apply:{approval_id}")
    clock[0] += timedelta(days=30)
    assert await service.submit_catalog(policy) == policy_ids[0]
    assert await service.submit_catalog(cultivation) == cultivation_ids[0]

    mutations = (
        policy.model_copy(update={"owner_employee": BOSS}),
        cultivation.model_copy(update={"expires_at_limit": NOW + timedelta(days=2)}),
    )
    for mutated in mutations:
        with pytest.raises(CatalogApprovalContractError) as conflict:
            await service.submit_catalog(mutated)
        assert conflict.value.code == "catalog_request_conflict"
    assert (
        await service.find_catalog_fact("tn_other", cultivation.change_set_ref) is None
    )
