"""供应商服务的能力匹配与价格证据门禁。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Self

import pytest

from domains.suppliers.service import Supplier, SupplierPriceRecord
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import ArtifactId, SupplierId, TenantId
from shared.schemas.money import CurrencyCode, Money

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
TENANT = TenantId("tn_test")


def _symbol(module: str, name: str) -> Any:
    try:
        return getattr(importlib.import_module(module), name)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{module}.{name} 尚未实现（{exc}）")


class _Authorizer:
    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.calls = 0

    def require(self, actor: object, action: object, tenant_id: TenantId) -> str:
        self.calls += 1
        if self.deny:
            raise PermissionDenied("denied")
        return "test-rule"


class _EvidenceReader:
    def __init__(self, evidence: object | None = None, *, fail: bool = False) -> None:
        self.evidence = evidence
        self.fail = fail
        self.calls: list[tuple[TenantId, ArtifactId]] = []

    async def read_verified(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> object:
        self.calls.append((tenant_id, artifact_id))
        if self.fail:
            raise RuntimeError("evidence unavailable")
        assert self.evidence is not None
        return self.evidence


class _Suppliers:
    def __init__(self, rows: list[Supplier]) -> None:
        self.rows = rows
        self.requested_tags: list[str] = []

    async def add(self, tenant_id: TenantId, supplier: Supplier) -> None:
        self.rows.append(supplier)

    async def get(
        self, tenant_id: TenantId, supplier_id: SupplierId
    ) -> Supplier | None:
        return next((row for row in self.rows if row.supplier_id == supplier_id), None)

    async def search_by_tags(
        self, tenant_id: TenantId, tags: list[str]
    ) -> list[Supplier]:
        self.requested_tags = tags
        return list(reversed(self.rows))


class _Prices:
    def __init__(self, *, fail: bool = False) -> None:
        self.rows: list[SupplierPriceRecord] = []
        self.fail = fail

    async def add(self, tenant_id: TenantId, record: SupplierPriceRecord) -> None:
        self.rows.append(record)
        if self.fail:
            raise RuntimeError("price write failed")


class _Uow:
    def __init__(self, suppliers: _Suppliers, prices: _Prices) -> None:
        self.suppliers = suppliers
        self.prices = prices
        self.entered = 0

    async def __aenter__(self) -> Self:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is not None:
            self.prices.rows.clear()


class _Factory:
    def __init__(self, uow: _Uow) -> None:
        self.uow = uow
        self.calls: list[TenantId] = []

    def __call__(self, tenant_id: TenantId) -> _Uow:
        self.calls.append(tenant_id)
        return self.uow


def _actor(role: str = "system") -> Any:
    Actor = _symbol("domains.suppliers.service", "SupplierActor")
    Role = _symbol("domains.suppliers.service", "SupplierRole")
    return Actor(actor_id="emp_test", role=Role(role), tenant_id=TENANT)


def _service(
    rows: list[Supplier] | None = None,
    *,
    authorizer: _Authorizer | None = None,
    price_fail: bool = False,
    evidence_reader: _EvidenceReader | None = None,
) -> tuple[Any, _Factory, _Prices, _Suppliers]:
    Service = _symbol("domains.suppliers.service_impl", "SupplierServiceImpl")
    suppliers = _Suppliers(rows or [])
    prices = _Prices(fail=price_fail)
    factory = _Factory(_Uow(suppliers, prices))
    return (
        Service(
            factory,
            authorizer or _Authorizer(),
            evidence_reader or _EvidenceReader(),
            now=lambda: NOW,
        ),
        factory,
        prices,
        suppliers,
    )


def _supplier(index: int, tags: list[str] | None = None) -> Supplier:
    return Supplier(
        supplier_id=SupplierId(f"sup_{index:03d}"),
        tenant_id=TENANT,
        name=f"Factory {index}",
        created_at=NOW,
        capability_tags=tags or [" Metal  Fabrication "],
    )


def _record(**changes: object) -> SupplierPriceRecord:
    values: dict[str, object] = {
        "supplier_id": SupplierId("sup_001"),
        "tenant_id": TENANT,
        "product_desc": "304 stainless hinge",
        "quantity_tier": 100,
        "price": Money(Decimal("1.234567890123"), CurrencyCode("USD")),
        "basis": "indicative",
        "observed_at": NOW,
        "evidence_ref": ArtifactId("art_price"),
        "valid_until": None,
    }
    values.update(changes)
    return SupplierPriceRecord(**values)  # type: ignore[arg-type]


def _quote_evidence(record: SupplierPriceRecord, **changes: object) -> object:
    Evidence = _symbol("domains.suppliers.service", "SupplierQuoteEvidence")
    SourceKind = _symbol("domains.suppliers.service", "SupplierQuoteSourceKind")
    values: dict[str, object] = {
        "tenant_id": record.tenant_id,
        "artifact_id": record.evidence_ref,
        "supplier_id": record.supplier_id,
        "product_desc": record.product_desc,
        "quantity_tier": record.quantity_tier,
        "price": record.price,
        "observed_at": record.observed_at,
        "valid_until": record.valid_until,
        "source_kind": SourceKind.DIRECT_SUPPLIER_QUOTE,
    }
    values.update(changes)
    return Evidence(**values)


@pytest.mark.asyncio
async def test_supplier_authorizer_runs_before_uow() -> None:
    """拒绝请求不得开启供应商事务。"""

    service, factory, _, _ = _service(authorizer=_Authorizer(deny=True))
    with pytest.raises(PermissionDenied):
        await service.search_by_capability(TENANT, ["OEM"], actor=_actor("sourcing"))
    assert factory.calls == []


@pytest.mark.asyncio
async def test_unauthorized_quoted_price_does_not_read_evidence_or_open_uow() -> None:
    """authorizer 必须先于金额验证、Evidence reader 和 UoW。"""

    record = _record(
        basis="quoted", valid_until=NOW + timedelta(days=1)
    )
    reader = _EvidenceReader(_quote_evidence(record))
    service, factory, _, _ = _service(
        authorizer=_Authorizer(deny=True), evidence_reader=reader
    )
    with pytest.raises(PermissionDenied):
        await service.record_price(TENANT, record, actor=_actor())
    assert reader.calls == []
    assert factory.calls == []


@pytest.mark.asyncio
async def test_supplier_capability_search_normalizes_orders_and_caps_50() -> None:
    """仓储返回乱序/过量时，服务仍须输出稳定的最多 50 条 tag 交集。"""

    rows = [_supplier(index) for index in range(52)]
    service, _, _, suppliers = _service(rows)
    result = await service.search_by_capability(
        TENANT, ["  METAL fabrication  "], actor=_actor("sourcing")
    )
    assert suppliers.requested_tags == ["metal fabrication"]
    assert [item.supplier_id for item in result] == [
        SupplierId(f"sup_{index:03d}") for index in range(50)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    [
        _record(
            basis="quoted",
            evidence_ref=ArtifactId(""),
            valid_until=NOW + timedelta(days=1),
        ),
        _record(basis="quoted", valid_until=None),
        _record(basis="quoted", valid_until=NOW),
        _record(basis="quoted", valid_until=NOW - timedelta(seconds=1)),
        _record(price=Money(Decimal(0), CurrencyCode("USD"))),
        _record(price=Money(Decimal(-1), CurrencyCode("USD"))),
    ],
)
async def test_quoted_price_requires_current_evidence_and_positive_decimal(
    record: SupplierPriceRecord,
) -> None:
    """无有效报价证据或非正金额不得进入只增价格历史。"""

    service, factory, prices, _ = _service()
    with pytest.raises(ValidationError):
        await service.record_price(TENANT, record, actor=_actor())
    assert factory.calls == []
    assert prices.rows == []


@pytest.mark.asyncio
async def test_indicative_price_stays_indicative_and_price_failure_rolls_back() -> None:
    """服务不得把公开参考价投影为 quoted，写失败必须回滚。"""

    reader = _EvidenceReader(fail=True)
    service, _, prices, _ = _service(evidence_reader=reader)
    await service.record_price(TENANT, _record(), actor=_actor())
    assert len(prices.rows) == 1
    assert prices.rows[0].basis == "indicative"
    assert prices.rows[0].valid_until is None
    assert reader.calls == []

    failing, _, failed_prices, _ = _service(price_fail=True)
    with pytest.raises(RuntimeError, match="price write failed"):
        await failing.record_price(TENANT, _record(), actor=_actor())
    assert failed_prices.rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "amount",
    (
        Decimal("1.0000000000000"),
        Decimal("10000000000000000.000000000000"),
    ),
)
async def test_supplier_price_rejects_unrepresentable_decimal_before_evidence_and_uow(
    amount: Decimal,
) -> None:
    """授权后先做 NUMERIC 可表示性门禁，再读取报价证据或打开事务。"""

    record = _record(
        price=Money(amount, CurrencyCode("USD")),
        basis="quoted",
        valid_until=NOW + timedelta(days=1),
    )
    reader = _EvidenceReader(_quote_evidence(record))
    service, factory, _, _ = _service(evidence_reader=reader)
    with pytest.raises(ValidationError, match="NUMERIC"):
        await service.record_price(TENANT, record, actor=_actor())
    assert reader.calls == []
    assert factory.calls == []


@pytest.mark.asyncio
async def test_direct_supplier_quote_evidence_must_match_every_commercial_field() -> None:
    """quoted 只有逐项匹配的直接供应商报价投影才能通过。"""

    valid_until = NOW + timedelta(days=1)
    record = _record(basis="quoted", valid_until=valid_until)
    SourceKind = _symbol("domains.suppliers.service", "SupplierQuoteSourceKind")
    mismatches = (
        {"tenant_id": TenantId("tn_other")},
        {"artifact_id": ArtifactId("art_other")},
        {"supplier_id": SupplierId("sup_other")},
        {"product_desc": "different spec"},
        {"quantity_tier": 101},
        {"price": Money(Decimal("1.234567890124"), CurrencyCode("USD"))},
        {"price": Money(Decimal("1.234567890123"), CurrencyCode("CNY"))},
        {"observed_at": NOW - timedelta(seconds=1)},
        {"valid_until": valid_until + timedelta(seconds=1)},
        {"source_kind": SourceKind.PUBLIC_WEB_SNAPSHOT},
        {"source_kind": SourceKind.INDICATIVE_RECORD},
    )
    for mismatch in mismatches:
        reader = _EvidenceReader(_quote_evidence(record, **mismatch))
        service, factory, _, _ = _service(evidence_reader=reader)
        with pytest.raises(ValidationError, match="quoted Evidence"):
            await service.record_price(TENANT, record, actor=_actor())
        assert reader.calls == [(TENANT, record.evidence_ref)]
        assert factory.calls == []


@pytest.mark.asyncio
async def test_direct_supplier_quote_evidence_allows_exact_boundary_and_reader_failure_closes() -> None:
    """完全匹配的直接报价可写；reader 故障不能降级为信任调用方。"""

    amount = Decimal("9999999999999999.999999999999")
    record = _record(
        price=Money(amount, CurrencyCode("USD")),
        basis="quoted",
        valid_until=NOW + timedelta(days=1),
    )
    reader = _EvidenceReader(_quote_evidence(record))
    service, factory, prices, _ = _service(evidence_reader=reader)
    await service.record_price(TENANT, record, actor=_actor())
    assert prices.rows == [record]
    assert factory.calls == [TENANT]

    unavailable = _EvidenceReader(fail=True)
    service, factory, prices, _ = _service(evidence_reader=unavailable)
    with pytest.raises(RuntimeError, match="evidence unavailable"):
        await service.record_price(TENANT, record, actor=_actor())
    assert factory.calls == []
    assert prices.rows == []


@pytest.mark.asyncio
async def test_supplier_record_tenant_mismatch_is_rejected_before_uow() -> None:
    """记录自带租户不能与服务租户错配。"""

    service, factory, _, _ = _service()
    with pytest.raises(ValidationError, match="租户"):
        await service.record_price(
            TENANT,
            _record(tenant_id=TenantId("tn_other")),
            actor=_actor(),
        )
    assert factory.calls == []
