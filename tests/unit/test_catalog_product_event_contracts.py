"""Catalog Product Proposal 事件只传租户绑定定位元数据的契约。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from infra.db.outbox import deserialize, serialize
from shared.errors import ValidationError
from shared.events.catalog import (
    AccountCountryFactsChanged,
    CatalogCultivationQueued,
    CatalogProductProposalCreated,
    CatalogProposalPolicyActivated,
    NeedCatalogFactsChanged,
)
from shared.schemas.identifiers import (
    CatalogCultivationCaseId,
    CatalogProductProposalId,
    CatalogProposalEvaluationId,
    CatalogProposalPolicyVersionId,
    NeedClusterId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
TENANT_ID = TenantId("tn_01K4BZX1YQ2FJX1PKA3M8C7V9D")
NEED_ID = ValidatedNeedId("vnd_01K4BZX1YQ2FJX1PKA3M8C7V9D")
ACCOUNT_ID = ProspectAccountId("acc_01K4BZX1YQ2FJX1PKA3M8C7V9D")
POLICY_VERSION_ID = CatalogProposalPolicyVersionId("cpv_01K4BZX1YQ2FJX1PKA3M8C7V9D")
EVALUATION_ID = CatalogProposalEvaluationId("cpe_01K4BZX1YQ2FJX1PKA3M8C7V9D")
PROPOSAL_ID = CatalogProductProposalId("cpr_01K4BZX1YQ2FJX1PKA3M8C7V9D")
CULTIVATION_CASE_ID = CatalogCultivationCaseId("ccc_01K4BZX1YQ2FJX1PKA3M8C7V9D")
CLUSTER_ID = NeedClusterId("ncl_01K4BZX1YQ2FJX1PKA3M8C7V9D")


@pytest.mark.parametrize(
    ("event", "expected_payload"),
    [
        (
            NeedCatalogFactsChanged(
                tenant_id=TENANT_ID,
                occurred_at=NOW,
                need_id=NEED_ID,
                cluster_id=CLUSTER_ID,
                change_kind="quantity",
            ),
            {
                "tenant_id": str(TENANT_ID),
                "occurred_at": NOW.isoformat(),
                "run_id": None,
                "need_id": str(NEED_ID),
                "cluster_id": str(CLUSTER_ID),
                "change_kind": "quantity",
            },
        ),
        (
            AccountCountryFactsChanged(
                tenant_id=TENANT_ID,
                occurred_at=NOW,
                account_id=ACCOUNT_ID,
            ),
            {
                "tenant_id": str(TENANT_ID),
                "occurred_at": NOW.isoformat(),
                "run_id": None,
                "account_id": str(ACCOUNT_ID),
            },
        ),
        (
            CatalogProposalPolicyActivated(
                tenant_id=TENANT_ID,
                occurred_at=NOW,
                policy_version_id=POLICY_VERSION_ID,
                content_hash="a" * 64,
            ),
            {
                "tenant_id": str(TENANT_ID),
                "occurred_at": NOW.isoformat(),
                "run_id": None,
                "policy_version_id": str(POLICY_VERSION_ID),
                "content_hash": "a" * 64,
            },
        ),
        (
            CatalogProductProposalCreated(
                tenant_id=TENANT_ID,
                occurred_at=NOW,
                proposal_id=PROPOSAL_ID,
                evaluation_id=EVALUATION_ID,
                cluster_id=CLUSTER_ID,
                policy_version_id=POLICY_VERSION_ID,
                facts_hash="b" * 64,
            ),
            {
                "tenant_id": str(TENANT_ID),
                "occurred_at": NOW.isoformat(),
                "run_id": None,
                "proposal_id": str(PROPOSAL_ID),
                "evaluation_id": str(EVALUATION_ID),
                "cluster_id": str(CLUSTER_ID),
                "policy_version_id": str(POLICY_VERSION_ID),
                "facts_hash": "b" * 64,
            },
        ),
        (
            CatalogCultivationQueued(
                tenant_id=TENANT_ID,
                occurred_at=NOW,
                cultivation_case_id=CULTIVATION_CASE_ID,
                proposal_id=PROPOSAL_ID,
            ),
            {
                "tenant_id": str(TENANT_ID),
                "occurred_at": NOW.isoformat(),
                "run_id": None,
                "cultivation_case_id": str(CULTIVATION_CASE_ID),
                "proposal_id": str(PROPOSAL_ID),
            },
        ),
    ],
)
def test_catalog_product_events_roundtrip_only_locator_metadata(
    event: object, expected_payload: dict[str, object]
) -> None:
    """缺字段、值或敏感商业事实的事件均不能代替受控事实读取。"""
    assert serialize(event) == expected_payload
    assert deserialize(type(event), serialize(event)) == event
    rendered = json.dumps(serialize(event))
    assert json.loads(rendered) == expected_payload
    for forbidden_key in (
        "quantity",
        "country",
        "customer_quote",
        "provenance",
        "price",
        "credential",
    ):
        assert forbidden_key not in json.loads(rendered)


def test_unclustered_need_catalog_change_roundtrips_without_fabricated_cluster() -> None:
    """把 optional locator 改成必填会迫使 producer 伪造单成员 Catalog 簇。"""
    event = NeedCatalogFactsChanged(
        tenant_id=TENANT_ID,
        occurred_at=NOW,
        need_id=NEED_ID,
        cluster_id=None,
        change_kind="quantity",
    )

    assert deserialize(NeedCatalogFactsChanged, serialize(event)) == event
    assert serialize(event) == {
        "tenant_id": str(TENANT_ID),
        "occurred_at": NOW.isoformat(),
        "run_id": None,
        "need_id": str(NEED_ID),
        "cluster_id": None,
        "change_kind": "quantity",
    }


@pytest.mark.parametrize(
    "event",
    [
        NeedCatalogFactsChanged(
            tenant_id=TENANT_ID,
            occurred_at=NOW,
            need_id=ValidatedNeedId(""),
            cluster_id=CLUSTER_ID,
            change_kind="quantity",
        ),
        NeedCatalogFactsChanged(
            tenant_id=TENANT_ID,
            occurred_at=NOW,
            need_id=NEED_ID,
            cluster_id=CLUSTER_ID,
            change_kind="country",
        ),
        AccountCountryFactsChanged(
            tenant_id=TENANT_ID,
            occurred_at=NOW,
            account_id=ProspectAccountId(""),
        ),
        CatalogProposalPolicyActivated(
            tenant_id=TENANT_ID,
            occurred_at=NOW,
            policy_version_id=POLICY_VERSION_ID,
            content_hash="A" * 64,
        ),
        CatalogProductProposalCreated(
            tenant_id=TENANT_ID,
            occurred_at=NOW,
            proposal_id=PROPOSAL_ID,
            evaluation_id=EVALUATION_ID,
            cluster_id=CLUSTER_ID,
            policy_version_id=POLICY_VERSION_ID,
            facts_hash="short",
        ),
        CatalogCultivationQueued(
            tenant_id=TENANT_ID,
            occurred_at=NOW,
            cultivation_case_id=CULTIVATION_CASE_ID,
            proposal_id=CatalogProductProposalId(""),
        ),
        NeedCatalogFactsChanged(
            tenant_id=TENANT_ID,
            occurred_at=NOW.replace(tzinfo=None),
            need_id=NEED_ID,
            cluster_id=CLUSTER_ID,
            change_kind="unit",
        ),
        NeedCatalogFactsChanged(
            tenant_id=TENANT_ID,
            occurred_at=datetime(2026, 9, 4, 20, 0, tzinfo=timezone(timedelta(hours=8))),
            need_id=NEED_ID,
            cluster_id=CLUSTER_ID,
            change_kind="recurring_requirement",
        ),
    ],
)
def test_catalog_product_events_reject_invalid_locator_shapes(event: object) -> None:
    """改成空定位、非 UTC 时间、非白名单事实种类或非摘要哈希时必须拒绝。"""
    with pytest.raises(ValidationError, match="目录产品提案事件载荷无效"):
        serialize(event)


@pytest.mark.parametrize(
    "event",
    [
        CatalogProposalPolicyActivated(
            tenant_id=TENANT_ID,
            occurred_at=NOW,
            policy_version_id=CatalogProposalPolicyVersionId(str(EVALUATION_ID)),
            content_hash="a" * 64,
        ),
        CatalogProductProposalCreated(
            tenant_id=TENANT_ID,
            occurred_at=NOW,
            proposal_id=CatalogProductProposalId(str(EVALUATION_ID)),
            evaluation_id=EVALUATION_ID,
            cluster_id=CLUSTER_ID,
            policy_version_id=POLICY_VERSION_ID,
            facts_hash="b" * 64,
        ),
        CatalogProductProposalCreated(
            tenant_id=TENANT_ID,
            occurred_at=NOW,
            proposal_id=PROPOSAL_ID,
            evaluation_id=CatalogProposalEvaluationId(str(PROPOSAL_ID)),
            cluster_id=CLUSTER_ID,
            policy_version_id=POLICY_VERSION_ID,
            facts_hash="b" * 64,
        ),
        CatalogProductProposalCreated(
            tenant_id=TENANT_ID,
            occurred_at=NOW,
            proposal_id=PROPOSAL_ID,
            evaluation_id=EVALUATION_ID,
            cluster_id=CLUSTER_ID,
            policy_version_id=CatalogProposalPolicyVersionId(str(CULTIVATION_CASE_ID)),
            facts_hash="b" * 64,
        ),
        CatalogCultivationQueued(
            tenant_id=TENANT_ID,
            occurred_at=NOW,
            cultivation_case_id=CatalogCultivationCaseId(str(PROPOSAL_ID)),
            proposal_id=PROPOSAL_ID,
        ),
        CatalogCultivationQueued(
            tenant_id=TENANT_ID,
            occurred_at=NOW,
            cultivation_case_id=CULTIVATION_CASE_ID,
            proposal_id=CatalogProductProposalId(str(CULTIVATION_CASE_ID)),
        ),
    ],
)
def test_catalog_product_events_reject_wrong_or_cross_contract_id_prefixes(
    event: object,
) -> None:
    """各目录提案定位 ID 必须使用自己的前缀，不能借用另一契约的 ID。"""
    with pytest.raises(ValidationError, match="目录产品提案事件载荷无效"):
        serialize(event)
