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
EVIDENCE_CONTENT_HASH = "c" * 64
EVIDENCE_REF = (
    "catalog-evidence-v1:conversation:msg_01K00000000000000000000000:"
    + EVIDENCE_CONTENT_HASH
)


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
        "evidence_refs": (EVIDENCE_REF,),
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
        evidence_refs=(EVIDENCE_REF,),
        proposed_by_run=RUN,
        owner_employee=OWNER,
        change_set_ref=f"catalog-cultivation:{PROPOSAL}:{POLICY}:{HASH_B}",
        request_hash=_cultivation_hash(),
        expires_at_limit=NOW + timedelta(days=3),
        warning=CATALOG_CULTIVATION_WARNING,
    )


class _BossReader:
    def __init__(self) -> None:
        self.role = "boss"
        self.active = True
        self.eligible = True
        self.tenant_id = TENANT
        self.employee_id = BOSS

    async def read_actor(self, tenant_id, employee_id):
        from domains.approvals.catalog_contract import CatalogApprovalActorFact

        return CatalogApprovalActorFact(
            tenant_id=self.tenant_id,
            employee_id=self.employee_id,
            current_role=self.role,
            active=self.active,
            eligible=self.eligible,
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
        "evidence_refs": (EVIDENCE_REF,),
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
    "unsafe_ref",
    ("0.42", "9.99", "www.example.com/raw", "customer_verbatim"),
)
def test_catalog_cultivation_rejects_unbranded_evidence_even_with_matching_hash(
    unsafe_ref: str,
) -> None:
    """任意 source_id/原文形字符串即使参与 request hash 也不是安全原件 locator。"""
    from pydantic import ValidationError as SchemaError

    from domains.approvals.catalog_contract import CatalogCultivationApprovalCommand

    values = _cultivation_command().model_dump(mode="python")
    values["evidence_refs"] = (unsafe_ref,)
    values["request_hash"] = _cultivation_hash(evidence_refs=(unsafe_ref,))

    with pytest.raises(SchemaError):
        CatalogCultivationApprovalCommand.model_validate(values)


@pytest.mark.parametrize(
    ("source_type", "source_id"),
    (
        ("conversation", "msg_01K00000000000000000000000"),
        ("web_page", "d" * 64),
        ("upload", "upl_01K00000000000000000000000"),
    ),
)
def test_catalog_evidence_locator_round_trips_only_routable_sources(
    source_type: str, source_id: str
) -> None:
    """Task11 显式组装后仍可解析出原域授权路由所需的完整 locator。"""
    from domains.approvals.catalog_contract import (
        CatalogEvidenceLocator,
        catalog_evidence_locator,
        parse_catalog_evidence_locator,
    )

    encoded = catalog_evidence_locator(
        source_type=source_type,
        source_id=source_id,
        content_hash=EVIDENCE_CONTENT_HASH,
    )

    assert parse_catalog_evidence_locator(encoded) == CatalogEvidenceLocator(
        source_type=source_type,
        source_id=source_id,
        content_hash=EVIDENCE_CONTENT_HASH,
    )
    assert encoded == (
        f"catalog-evidence-v1:{source_type}:{source_id}:{EVIDENCE_CONTENT_HASH}"
    )


@pytest.mark.parametrize("source_type", ("employee_input", "external_api"))
def test_catalog_evidence_locator_fails_closed_for_sources_without_a_route(
    source_type: str,
) -> None:
    from pydantic import ValidationError as SchemaError

    from domains.approvals.catalog_contract import CatalogEvidenceLocator

    with pytest.raises(SchemaError):
        CatalogEvidenceLocator(
            source_type=source_type,
            source_id="wcf_01K00000000000000000000000",
            content_hash=EVIDENCE_CONTENT_HASH,
        )


@pytest.mark.parametrize(
    ("source_type", "source_id"),
    (
        ("conversation", "0.42"),
        ("conversation", "customer_verbatim"),
        ("web_page", "www.example.com/raw"),
        ("upload", "9.99"),
    ),
)
def test_catalog_evidence_locator_rejects_wrong_source_identity_shape(
    source_type: str, source_id: str
) -> None:
    from pydantic import ValidationError as SchemaError

    from domains.approvals.catalog_contract import CatalogEvidenceLocator

    with pytest.raises(SchemaError):
        CatalogEvidenceLocator(
            source_type=source_type,
            source_id=source_id,
            content_hash=EVIDENCE_CONTENT_HASH,
        )


def _replace_rules(
    *replacements: tuple[int, dict[str, object]],
):
    from domains.approvals.catalog_contract import CatalogRuleResultFact

    rules = list(_rules())
    for position, changes in replacements:
        rules[position] = CatalogRuleResultFact.model_validate(
            rules[position].model_dump(mode="python") | changes
        )
    return tuple(rules)


@pytest.mark.parametrize(
    "rules",
    (
        _replace_rules(
            (
                2,
                {
                    "status": "passed",
                    "actual_value": 4,
                    "required_value": 2,
                    "explanation_code": "复购客户数达到策略门槛",
                },
            )
        ),
        _replace_rules((3, {"actual_value": 4})),
        _replace_rules((4, {"actual_value": 4})),
        _replace_rules(
            (1, {"actual_value": 5}),
            (
                2,
                {
                    "status": "passed",
                    "actual_value": 4,
                    "required_value": 4,
                    "explanation_code": "复购客户数达到策略门槛",
                },
            ),
        ),
        _replace_rules(
            (1, {"actual_value": 5}),
            (
                4,
                {
                    "status": "passed",
                    "actual_value": 4,
                    "required_value": 4,
                    "explanation_code": "数量单位覆盖达到策略门槛",
                },
            ),
        ),
        _replace_rules(
            (
                5,
                {
                    "status": "passed",
                    "actual_value": "pcs",
                    "required_value": True,
                    "explanation_code": "有效数量单位已经统一",
                },
            )
        ),
        _replace_rules(
            (
                2,
                {
                    "status": "unknown",
                    "actual_value": None,
                    "required_value": None,
                    "explanation_code": "目录事实损坏，评估已阻断",
                },
            )
        ),
        _replace_rules(
            (
                4,
                {
                    "status": "passed",
                    "actual_value": 2,
                    "required_value": 2,
                    "explanation_code": "数量单位覆盖达到策略门槛",
                },
            ),
            (
                5,
                {
                    "status": "failed",
                    "actual_value": None,
                    "required_value": True,
                    "explanation_code": "有效数量单位不统一",
                },
            ),
        ),
    ),
)
def test_catalog_cultivation_rejects_six_rule_cross_field_contradictions(
    rules,
) -> None:
    """六条各自合法仍不能伪造不可能的同一评估结果。"""
    from pydantic import ValidationError as SchemaError

    from domains.approvals.catalog_contract import CatalogCultivationApprovalCommand

    values = _cultivation_command().model_dump(mode="python")
    values["rule_results"] = rules
    values["request_hash"] = _cultivation_hash(rule_results=rules)

    with pytest.raises(SchemaError):
        CatalogCultivationApprovalCommand.model_validate(values)


def test_catalog_cultivation_accepts_only_the_complete_fixed_blocked_shape() -> None:
    from domains.approvals.catalog_contract import (
        CatalogCultivationApprovalCommand,
        CatalogRuleResultFact,
    )

    required_values = (True, 3, None, None, None, None)
    blocked = tuple(
        CatalogRuleResultFact(
            rule=rule.rule,
            status="unknown",
            actual_value=None,
            required_value=required,
            explanation_code="目录事实损坏，评估已阻断",
        )
        for rule, required in zip(_rules(), required_values, strict=True)
    )
    values = _cultivation_command().model_dump(mode="python")
    values["rule_results"] = blocked
    values["request_hash"] = _cultivation_hash(rule_results=blocked)

    assert (
        CatalogCultivationApprovalCommand.model_validate(values).rule_results == blocked
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
    from domains.approvals.schemas import ApprovalReaderIdentity

    pending = await service.list_for_reader(
        TENANT,
        reader=ApprovalReaderIdentity(employee_id=BOSS, role="boss"),
        limit=2,
    )
    assert [item.approval_id for item in pending] == [
        str(cultivation_ids[0]),
        str(policy_ids[0]),
    ]
    assert all(item.can_current_user_decide for item in pending)
    assert (
        await service.list_for_reader(
            "tn_other",
            reader=ApprovalReaderIdentity(employee_id=BOSS, role="boss"),
            limit=2,
        )
        == []
    )
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


@pytest.mark.asyncio
async def test_catalog_postgres_link_state_rechecks_reader_without_expanding_package_read(
    unit_engine,
) -> None:
    from domains.approvals.schemas import ApprovalReaderIdentity
    from shared.errors import PermissionDenied

    sessions = async_sessionmaker(unit_engine, expire_on_commit=False)
    actors = _BossReader()
    service = ApprovalServiceImpl(
        lambda tenant: SqlAlchemyApprovalUnitOfWork(
            sessions, tenant, now=lambda: NOW
        ),
        catalog_actor_reader=actors,
        now=lambda: NOW,
    )
    approval_id = await service.submit_catalog(_policy_command())

    for role in ("boss", "product", "sourcing", "finance"):
        actors.role = role
        linked = await service.get_catalog_link_state_for_reader(
            TENANT,
            approval_id,
            reader=ApprovalReaderIdentity(employee_id=BOSS, role=role),
        )
        assert linked.model_dump(mode="json") == {
            "approval_id": str(approval_id),
            "approval_type": "catalog_proposal_policy_change",
            "state": "pending",
        }

    actors.role = "product"
    product = ApprovalReaderIdentity(employee_id=BOSS, role="product")
    with pytest.raises(PermissionDenied):
        await service.get_for_reader(TENANT, approval_id, reader=product)

    actors.role = "finance"
    with pytest.raises(PermissionDenied):
        await service.get_catalog_link_state_for_reader(
            TENANT,
            approval_id,
            reader=ApprovalReaderIdentity(employee_id=BOSS, role="product"),
        )

    actors.role = "product"
    actors.active = False
    with pytest.raises(PermissionDenied):
        await service.get_catalog_link_state_for_reader(
            TENANT, approval_id, reader=product
        )
