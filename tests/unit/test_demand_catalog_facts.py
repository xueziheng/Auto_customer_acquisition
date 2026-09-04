"""Catalog-ready Need Cluster facts stay conservative, stable, and provenance-backed."""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from domains.demand.schemas import (
    CatalogEvidenceSummary,
    DemandCatalogAccountFact,
    catalog_evidence_summary,
)
from domains.demand.service_impl import DemandServiceImpl
from domains.demand.unit_facts import quantity_fact_hash
from infra.db.repositories.need_hypotheses import _need_to_row, _row_to_need
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    EmployeeId,
    MessageId,
    NeedClusterId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import FactualField, Provenance, SourceType

_models = importlib.import_module("domains.demand.models")
NeedCluster = _models.NeedCluster
NeedStatus = _models.NeedStatus
ValidatedNeed = _models.ValidatedNeed

NOW = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)
TENANT = TenantId("tn_catalog_facts")
OTHER_TENANT = TenantId("tn_catalog_other")
CLUSTER_ID = NeedClusterId("ncl_catalog_facts")
ACTOR = EmployeeId("emp_catalog")


def _provenance(source_id: str, *, observed_at: datetime = NOW) -> Provenance:
    return Provenance(
        source_type=SourceType.CONVERSATION,
        source_id=source_id,
        extracted_by="human",
        extracted_at=observed_at,
        confirmed_by=ACTOR,
        confirmed_at=observed_at,
        source_quote="redacted by the catalog projection",
    )


def test_account_country_fact_rejects_forged_content_hash() -> None:
    """Accepting a well-shaped arbitrary digest would sever the country evidence commitment."""
    account_id = ProspectAccountId("acc_country")
    forged = CatalogEvidenceSummary(
        source_type=SourceType.CONVERSATION,
        source_id="account_country_source",
        extracted_by="human",
        confirmed_by=ACTOR,
        confirmed_at=NOW,
        observed_at=NOW,
        content_hash="a" * 64,
    )

    with pytest.raises(ValueError, match="目录账户事实无效"):
        DemandCatalogAccountFact(
            tenant_id=TENANT,
            account_id=account_id,
            country_code="US",
            country_evidence=forged,
        )


def test_country_commitment_changes_with_identity_value_and_safe_provenance() -> None:
    """Omitting an identity, country, or safe source field would let commitments be replayed."""
    account_id = ProspectAccountId("acc_country")

    def committed(
        tenant_id: TenantId,
        subject_id: ProspectAccountId,
        country_code: str,
        provenance: Provenance,
    ) -> str:
        return catalog_evidence_summary(
            tenant_id=tenant_id,
            subject_id=str(subject_id),
            field_name="country",
            value=country_code,
            provenance=provenance,
        ).content_hash

    provenance = _provenance("account_country_source")
    hashes = {
        committed(TENANT, account_id, "US", provenance),
        committed(OTHER_TENANT, account_id, "US", provenance),
        committed(TENANT, ProspectAccountId("acc_other"), "US", provenance),
        committed(TENANT, account_id, "DE", provenance),
        committed(
            TENANT,
            account_id,
            "US",
            replace(provenance, extracted_by="employee"),
        ),
    }

    assert len(hashes) == 5


def _need(
    suffix: str,
    account: str,
    *,
    quantity: int | None,
    unit: str | None,
    recurring: bool | None,
    tenant_id: TenantId = TENANT,
    category: str = "hinges",
    cluster_id: NeedClusterId | None = CLUSTER_ID,
    stale_unit: bool = False,
) -> ValidatedNeed:
    need_id = ValidatedNeedId(f"vnd_{suffix}")
    quantity_fact = (
        FactualField(quantity, _provenance(f"msg_quantity_{suffix}"))
        if quantity is not None
        else None
    )
    unit_fact = (
        FactualField(unit, _provenance(f"msg_unit_{suffix}"))
        if unit is not None
        else None
    )
    binding = None
    confirmation_id = None
    if quantity_fact is not None and unit_fact is not None:
        binding = quantity_fact_hash(tenant_id, need_id, quantity_fact)
        if stale_unit:
            binding = "0" * 64
        confirmation_id = f"nuc_{suffix}"
    return ValidatedNeed(
        need_id=need_id,
        tenant_id=tenant_id,
        account_id=ProspectAccountId(account),
        product_category=FactualField(category, _provenance(f"msg_category_{suffix}")),
        source_message_id=MessageId(f"msg_source_{suffix}"),
        created_at=NOW - timedelta(days=1),
        status=NeedStatus.VALIDATED,
        quantity=quantity_fact,
        unit=unit_fact,
        unit_quantity_fact_hash=binding,
        unit_confirmation_id=confirmation_id,
        recurring_requirement=(
            FactualField(recurring, _provenance(f"msg_recurrence_{suffix}"))
            if recurring is not None
            else None
        ),
        cluster_id=cluster_id,
    )


@pytest.mark.parametrize(
    ("field_name", "malformed_value"),
    [
        ("quantity", True),
        ("quantity", 1.5),
        ("quantity", "10"),
        ("quantity", 0),
        ("quantity", -1),
        ("unit", 10),
    ],
)
def test_catalog_snapshot_decoder_rejects_malformed_quantity_and_unit(
    field_name: str,
    malformed_value: object,
) -> None:
    """Coercing persisted JSON would turn corruption into catalog decision facts."""
    row = _need_to_row(
        _need("decode", "acc_decode", quantity=10, unit="pcs", recurring=None)
    )
    payload = dict(getattr(row, field_name))
    payload["value"] = malformed_value
    setattr(row, field_name, payload)

    with pytest.raises(ValidationError, match="^字段快照损坏$") as caught:
        _row_to_need(row)

    assert caught.value.__cause__ is None


@dataclass(frozen=True)
class _Snapshot:
    cluster: NeedCluster
    needs: tuple[ValidatedNeed, ...]


class _Clusters:
    def __init__(self, snapshot: _Snapshot) -> None:
        self.snapshot = snapshot

    async def get_catalog_snapshot(
        self, tenant_id: TenantId, cluster_id: NeedClusterId
    ) -> _Snapshot | None:
        return self.snapshot

    async def list_catalog_cluster_ids(
        self, tenant_id: TenantId, *, limit: int
    ) -> tuple[NeedClusterId, ...]:
        return (CLUSTER_ID,)

    async def list_catalog_cluster_ids_for_account(
        self, tenant_id: TenantId, account_id: ProspectAccountId, *, limit: int
    ) -> tuple[NeedClusterId, ...]:
        return (CLUSTER_ID,)


@dataclass
class _Uow:
    clusters: _Clusters

    @asynccontextmanager
    async def context(self) -> AsyncIterator[_Uow]:
        yield self


class _Accounts:
    def __init__(self, countries: dict[str, str | None]) -> None:
        self.countries = countries

    async def get_account_catalog_fact(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
    ) -> DemandCatalogAccountFact:
        country = self.countries[str(account_id)]
        provenance = _provenance(f"account_country_{account_id}")
        return DemandCatalogAccountFact(
            tenant_id=tenant_id,
            account_id=account_id,
            country_code=country,
            country_evidence=(
                catalog_evidence_summary(
                    tenant_id=tenant_id,
                    subject_id=str(account_id),
                    field_name="country",
                    value=country,
                    provenance=provenance,
                )
                if country is not None
                else None
            ),
        )


def _cluster(needs: tuple[ValidatedNeed, ...]) -> NeedCluster:
    return NeedCluster(
        cluster_id=CLUSTER_ID,
        tenant_id=TENANT,
        category="hinges",
        member_need_ids=[need.need_id for need in reversed(needs)],
        created_at=NOW - timedelta(hours=2),
        updated_at=NOW + timedelta(hours=1),
    )


def _service(
    needs: tuple[ValidatedNeed, ...],
    countries: dict[str, str | None],
    *,
    cluster: NeedCluster | None = None,
) -> DemandServiceImpl:
    snapshot = _Snapshot(cluster or _cluster(needs), tuple(reversed(needs)))
    uow = _Uow(_Clusters(snapshot))
    return DemandServiceImpl(
        lambda tenant_id: uow.context(),
        now=lambda: NOW + timedelta(days=30),
        catalog_accounts=_Accounts(countries),
    )


async def test_matching_current_units_total_once_per_distinct_account() -> None:
    """A regression to Need counting or exact-unit comparison overstates demand."""
    needs = (
        _need("a", "acc_a", quantity=10, unit="PCS", recurring=True),
        _need("b", "acc_b", quantity=20, unit="pcs", recurring=False),
        _need("c", "acc_c", quantity=30, unit="Pcs", recurring=None),
    )
    service = _service(needs, {"acc_a": "US", "acc_b": "DE", "acc_c": None})

    facts = await service.get_cluster_catalog_facts(TENANT, CLUSTER_ID)
    repeated = await service.get_cluster_catalog_facts(TENANT, CLUSTER_ID)

    assert facts.member_need_ids == (ValidatedNeedId("vnd_a"), ValidatedNeedId("vnd_b"), ValidatedNeedId("vnd_c"))
    assert facts.distinct_account_ids == (ProspectAccountId("acc_a"), ProspectAccountId("acc_b"), ProspectAccountId("acc_c"))
    assert (facts.member_count, facts.distinct_account_count) == (3, 3)
    assert facts.known_country_codes == ("DE", "US")
    assert facts.unknown_country_account_count == 1
    assert (
        facts.recurring_true_account_count,
        facts.recurring_false_account_count,
        facts.recurring_unknown_account_count,
    ) == (1, 1, 1)
    assert facts.quantity_unit_covered_account_count == 3
    assert facts.unified_unit == "pcs"
    assert facts.safe_total_quantity == 60
    assert facts.facts_observed_at == NOW + timedelta(hours=1)
    assert len(facts.facts_hash) == 64
    assert facts == repeated
    assert facts.evidence_summaries
    assert not hasattr(facts.evidence_summaries[0], "value")
    assert not hasattr(facts.evidence_summaries[0], "source_quote")
    assert not hasattr(facts.evidence_summaries[0], "source_url")


async def test_multiple_needs_for_one_account_force_quantity_unknown_and_mix_recurrence() -> None:
    """Counting Needs instead of accounts would invent quantity and recurring prevalence."""
    needs = (
        _need("a1", "acc_a", quantity=10, unit="pcs", recurring=False),
        _need("a2", "acc_a", quantity=20, unit="pcs", recurring=True),
    )
    facts = await _service(needs, {"acc_a": "US"}).get_cluster_catalog_facts(
        TENANT, CLUSTER_ID
    )

    assert facts.distinct_account_count == 1
    assert facts.quantity_unit_covered_account_count == 0
    assert facts.safe_total_quantity is None
    assert facts.recurring_true_account_count == 1
    assert facts.recurring_false_account_count == 0
    assert facts.recurring_unknown_account_count == 0
    assert facts.display_codes == ("recurring_mixed_same_account",)


async def test_mixed_units_and_stale_bindings_never_total() -> None:
    """Unit conversion or accepting an old quantity binding would create a false total."""
    mixed = (
        _need("pcs", "acc_pcs", quantity=10, unit="pcs", recurring=None),
        _need("kg", "acc_kg", quantity=20, unit="kg", recurring=None),
    )
    mixed_facts = await _service(
        mixed, {"acc_pcs": "US", "acc_kg": "CA"}
    ).get_cluster_catalog_facts(TENANT, CLUSTER_ID)
    stale = (_need("stale", "acc_stale", quantity=10, unit="pcs", recurring=None, stale_unit=True),)
    stale_facts = await _service(stale, {"acc_stale": "US"}).get_cluster_catalog_facts(
        TENANT, CLUSTER_ID
    )

    assert mixed_facts.quantity_unit_covered_account_count == 2
    assert mixed_facts.unified_unit is None
    assert mixed_facts.safe_total_quantity is None
    assert stale_facts.quantity_unit_covered_account_count == 0
    assert stale_facts.safe_total_quantity is None


@pytest.mark.parametrize("corruption", ["missing", "reversed", "tenant", "category"])
async def test_invalid_bidirectional_membership_fails_before_emitting_hash(
    corruption: str,
) -> None:
    """Any partial membership chain must fail closed instead of yielding plausible facts."""
    first = _need("a", "acc_a", quantity=10, unit="pcs", recurring=None)
    second = _need("b", "acc_b", quantity=20, unit="pcs", recurring=None)
    needs = (first, second)
    cluster = _cluster(needs)
    if corruption == "missing":
        needs = (first,)
    elif corruption == "reversed":
        needs = (first, replace(second, cluster_id=NeedClusterId("ncl_other")))
    elif corruption == "tenant":
        needs = (first, replace(second, tenant_id=OTHER_TENANT))
    elif corruption == "category":
        needs = (first, replace(second, product_category=FactualField("bolts", second.product_category.provenance)))

    with pytest.raises(ValidationError):
        await _service(
            needs,
            {"acc_a": "US", "acc_b": "DE"},
            cluster=cluster,
        ).get_cluster_catalog_facts(TENANT, CLUSTER_ID)


async def test_catalog_cluster_id_lists_exclude_unclustered_account_needs() -> None:
    """Catalog enumeration returns persisted clusters, never fabricated singleton Needs."""
    need = _need("a", "acc_a", quantity=10, unit="pcs", recurring=None)
    service = _service((need,), {"acc_a": "US"})

    assert await service.list_catalog_cluster_ids(TENANT, limit=25) == (CLUSTER_ID,)
    assert await service.list_catalog_cluster_ids_for_account(
        TENANT, ProspectAccountId("acc_a"), limit=25
    ) == (CLUSTER_ID,)


@dataclass(frozen=True)
class _AccountView:
    tenant_id: TenantId
    account_id: ProspectAccountId
    country: str
    field_provenance: dict[str, Provenance]


class _Prospecting:
    def __init__(self, account: _AccountView | Exception) -> None:
        self.account = account

    async def get_account(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> _AccountView:
        if isinstance(self.account, Exception):
            raise self.account
        return self.account


@pytest.mark.parametrize("country", ["Germany", "us", "XX"])
async def test_account_adapter_treats_non_assigned_exact_iso2_as_unknown(
    country: str,
) -> None:
    """Aliases, casing, and unassigned codes must not become geographic facts."""
    from workflows.catalog_product_proposal.account_facts import (
        ProspectingDemandCatalogAccountFactsReader,
    )

    account_id = ProspectAccountId("acc_country")
    reader = ProspectingDemandCatalogAccountFactsReader(
        _Prospecting(
            _AccountView(
                tenant_id=TENANT,
                account_id=account_id,
                country=country,
                field_provenance={
                    "country": _provenance("account_country_source")
                },
            )
        )
    )

    fact = await reader.get_account_catalog_fact(TENANT, account_id)

    assert fact.country_code is None
    assert fact.country_evidence is None


async def test_account_adapter_maps_exact_country_to_redacted_evidence() -> None:
    """The adapter must expose only the country code and safe evidence metadata."""
    from workflows.catalog_product_proposal.account_facts import (
        ProspectingDemandCatalogAccountFactsReader,
    )

    account_id = ProspectAccountId("acc_country")
    reader = ProspectingDemandCatalogAccountFactsReader(
        _Prospecting(
            _AccountView(
                tenant_id=TENANT,
                account_id=account_id,
                country="US",
                field_provenance={
                    "country": _provenance("account_country_source")
                },
            )
        )
    )

    fact = await reader.get_account_catalog_fact(TENANT, account_id)

    assert fact.country_code == "US"
    assert fact.country_evidence is not None
    assert fact.country_evidence.source_id == "account_country_source"
    assert not hasattr(fact.country_evidence, "source_quote")


async def test_account_adapter_treats_incomplete_country_provenance_as_unknown() -> None:
    """A valid-looking country without a qualified observation time is not a fact."""
    from workflows.catalog_product_proposal.account_facts import (
        ProspectingDemandCatalogAccountFactsReader,
    )

    account_id = ProspectAccountId("acc_country")
    incomplete = Provenance(
        source_type=SourceType.EMPLOYEE_INPUT,
        source_id="account_country_source",
        extracted_by="human",
        extracted_at=NOW.replace(tzinfo=None),
    )
    reader = ProspectingDemandCatalogAccountFactsReader(
        _Prospecting(
            _AccountView(
                tenant_id=TENANT,
                account_id=account_id,
                country="US",
                field_provenance={"country": incomplete},
            )
        )
    )

    fact = await reader.get_account_catalog_fact(TENANT, account_id)

    assert fact.country_code is None
    assert fact.country_evidence is None


async def test_account_adapter_classifies_dependency_errors_without_raw_text() -> None:
    """Dependency failures need fixed retry semantics and must not retain exception text."""
    from workflows.catalog_product_proposal.account_facts import (
        ProspectingDemandCatalogAccountFactsReader,
    )

    account_id = ProspectAccountId("acc_country")
    transient = ProspectingDemandCatalogAccountFactsReader(
        _Prospecting(TransientError("raw provider detail"))
    )
    malformed = ProspectingDemandCatalogAccountFactsReader(
        _Prospecting(object())  # type: ignore[arg-type]
    )

    with pytest.raises(TransientError) as retryable:
        await transient.get_account_catalog_fact(TENANT, account_id)
    with pytest.raises(ValidationError) as permanent:
        await malformed.get_account_catalog_fact(TENANT, account_id)

    assert str(retryable.value) == "目录账户事实依赖暂不可用"
    assert str(permanent.value) == "目录账户事实依赖返回无效"
    assert "raw provider detail" not in str(retryable.value)
