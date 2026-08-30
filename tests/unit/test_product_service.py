"""产品服务的候选卡、视图与成本门禁。"""

from __future__ import annotations

import importlib
import json
from dataclasses import fields
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Self

import pytest
from pydantic import ValidationError as PydanticValidationError

from domains.products.service import CandidateStatus, Product, ProductPool
from shared.errors import PermissionDenied
from shared.schemas.identifiers import (
    ArtifactId,
    ProductId,
    SourcingCaseId,
    SupplierCandidateId,
    SupplierId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, Money

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
TENANT = TenantId("tn_test")
OTHER_TENANT = TenantId("tn_other")


def _symbol(module: str, name: str) -> Any:
    try:
        return getattr(importlib.import_module(module), name)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{module}.{name} 尚未实现（{exc}）")


def _price_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "minimum_quantity": 100,
        "unit_amount": Decimal("0.123456789012"),
        "currency": "USD",
        "unit": "piece",
        "evidence_ref": ArtifactId("art_price"),
    }
    payload.update(changes)
    return payload


def _command(**changes: object) -> Any:
    CandidateProductCreate = _symbol(
        "domains.products.schemas", "CandidateProductCreate"
    )
    payload: dict[str, object] = {
        "sourcing_case_id": SourcingCaseId("src_case"),
        "supplier_candidate_id": SupplierCandidateId("spc_candidate"),
        "name_zh": "不锈钢铰链",
        "name_en": "Stainless Steel Hinge",
        "category": "Hinges",
        "spec_summary": "304 stainless, 75 mm",
        "moq": 100,
        "evidence_refs": (ArtifactId("art_page"), ArtifactId("art_price")),
        "indicative_prices": (_price_payload(),),
    }
    payload.update(changes)
    return CandidateProductCreate.model_validate(payload)


class _Authorizer:
    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.calls: list[tuple[object, object, TenantId]] = []

    def require(self, actor: object, action: object, tenant_id: TenantId) -> str:
        self.calls.append((actor, action, tenant_id))
        if self.deny:
            raise PermissionDenied("denied")
        return "test-rule"


class _Products:
    def __init__(self, rows: list[Product] | None = None) -> None:
        self.rows = rows or []
        self.add_calls: list[Product] = []

    async def add(self, tenant_id: TenantId, product: Product) -> None:
        self.add_calls.append(product)
        self.rows.append(product)

    async def get(self, tenant_id: TenantId, product_id: ProductId) -> Product | None:
        return next(
            (
                row
                for row in self.rows
                if row.tenant_id == tenant_id and row.product_id == product_id
            ),
            None,
        )

    async def search(
        self,
        tenant_id: TenantId,
        pools: list[ProductPool],
        category: str | None,
        keywords: list[str],
        limit: int,
    ) -> list[Product]:
        return [row for row in self.rows if row.tenant_id == tenant_id]


class _Sources:
    def __init__(self, *, canonical: Any | None = None) -> None:
        self.by_origin: dict[tuple[TenantId, object, object], Any] = {}
        self.canonical = canonical

    async def get_by_origin(
        self, tenant_id: TenantId, case_id: object, candidate_id: object
    ) -> Any | None:
        return self.by_origin.get((tenant_id, case_id, candidate_id))

    async def get_by_product(
        self, tenant_id: TenantId, product_id: ProductId
    ) -> Any | None:
        return next(
            (
                item
                for item in self.by_origin.values()
                if item.tenant_id == tenant_id and item.product_id == product_id
            ),
            None,
        )

    async def add(self, tenant_id: TenantId, source: Any) -> Any:
        if self.canonical is not None:
            return self.canonical
        key = (
            tenant_id,
            source.sourcing_case_id,
            source.supplier_candidate_id,
        )
        self.by_origin.setdefault(key, source)
        return self.by_origin[key]


class _Uow:
    def __init__(
        self, products: _Products | None = None, sources: _Sources | None = None
    ) -> None:
        self.products = products or _Products()
        self.candidate_sources = sources or _Sources()
        self.entered = 0

    async def __aenter__(self) -> Self:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _Factory:
    def __init__(self, uow: _Uow) -> None:
        self.uow = uow
        self.calls: list[TenantId] = []

    def __call__(self, tenant_id: TenantId) -> _Uow:
        self.calls.append(tenant_id)
        return self.uow


def _actor(role: str = "system") -> Any:
    ProductActor = _symbol("domains.products.permissions", "ProductActor")
    ProductRole = _symbol("domains.products.permissions", "ProductRole")
    return ProductActor(actor_id="emp_test", role=ProductRole(role), tenant_id=TENANT)


def _service(
    *,
    products: _Products | None = None,
    sources: _Sources | None = None,
    authorizer: _Authorizer | None = None,
) -> tuple[Any, _Factory, _Authorizer]:
    Service = _symbol("domains.products.service_impl", "ProductServiceImpl")
    uow = _Uow(products, sources)
    factory = _Factory(uow)
    auth = authorizer or _Authorizer()
    return Service(factory, auth, now=lambda: NOW), factory, auth


def _formal_product(product_id: str, *, complete_cost: bool = True) -> Product:
    product = Product(
        product_id=ProductId(product_id),
        tenant_id=TENANT,
        pool=ProductPool.FORMAL,
        name_zh="铰链",
        name_en="Hinge",
        category=" Hinges ",
        created_at=NOW,
        supplier_id=SupplierId("sup_one"),
        internal_cost=Money(Decimal("1.25"), CurrencyCode("USD")),
        internal_cost_basis="EXW supplier quote",
        internal_cost_unit="piece",
        internal_cost_source_ref=ArtifactId("art_cost"),
    )
    if not complete_cost:
        product.internal_cost_source_ref = None
    return product


def test_candidate_command_is_strict_exact_and_evidence_bound() -> None:
    """放宽 Decimal、MOQ、单位或 Evidence 会让不可核验网页价格进产品池。"""

    CandidateProductCreate = _symbol(
        "domains.products.schemas", "CandidateProductCreate"
    )
    command = _command()
    assert command.indicative_prices[0].unit_amount == Decimal(
        "0.123456789012"
    )
    assert (
        command.model_dump(mode="json")["indicative_prices"][0]["unit_amount"]
        == "0.123456789012"
    )

    for change in (
        {"moq": 0},
        {"evidence_refs": ()},
        {"indicative_prices": ()},
        {"name_en": " "},
        {"extra": "forbidden"},
        {"indicative_prices": (_price_payload(unit=""),)},
        {"indicative_prices": (_price_payload(unit_amount=Decimal(0)),)},
        {
            "indicative_prices": (
                _price_payload(evidence_ref=ArtifactId("art_missing")),
            )
        },
    ):
        with pytest.raises(PydanticValidationError):
            CandidateProductCreate.model_validate(
                {
                    **_command().model_dump(mode="python"),
                    **change,
                }
            )

    with pytest.raises(PydanticValidationError):
        CandidateProductCreate.model_validate_json(
            '{"sourcing_case_id":"src_case","supplier_candidate_id":"spc_candidate",'
            '"name_zh":"铰链","name_en":"Hinge","category":"hinges",'
            '"spec_summary":"304","moq":1,"evidence_refs":["art_price"],'
            '"indicative_prices":[{"minimum_quantity":1,"unit_amount":0.1,'
            '"currency":"USD","unit":"piece","evidence_ref":"art_price"}]}'
        )


@pytest.mark.parametrize(
    "amount",
    (
        Decimal("1.0000000000000"),
        Decimal("10000000000000000.000000000000"),
    ),
)
def test_candidate_price_rejects_decimal_outside_numeric_28_12(amount: Decimal) -> None:
    """超过 NUMERIC(28,12) 的值必须在事务前拒绝，不能量化或等 commit 报错。"""

    CandidateProductCreate = _symbol(
        "domains.products.schemas", "CandidateProductCreate"
    )
    with pytest.raises(PydanticValidationError, match="NUMERIC"):
        CandidateProductCreate.model_validate(
            {
                **_command().model_dump(mode="python"),
                "indicative_prices": (_price_payload(unit_amount=amount),),
            }
        )


@pytest.mark.parametrize(
    "amount",
    ("1.0000000000000", "10000000000000000.000000000000"),
)
def test_candidate_json_price_rejects_decimal_outside_numeric_28_12(
    amount: str,
) -> None:
    """JSON 十进制字符串同样必须按原始 scale/整数位门禁。"""

    CandidateProductCreate = _symbol(
        "domains.products.schemas", "CandidateProductCreate"
    )
    payload = _command().model_dump(mode="json")
    payload["indicative_prices"][0]["unit_amount"] = amount
    with pytest.raises(PydanticValidationError, match="NUMERIC"):
        CandidateProductCreate.model_validate_json(json.dumps(payload))


def test_candidate_price_accepts_numeric_28_12_boundary_without_rounding() -> None:
    """16 位整数加 12 位小数是边界合法值，必须保持原 Decimal。"""

    amount = Decimal("9999999999999999.999999999999")
    command = _command(indicative_prices=(_price_payload(unit_amount=amount),))
    assert command.indicative_prices[0].unit_amount == amount


@pytest.mark.asyncio
async def test_authorizer_runs_before_uow_and_tenant_mismatch_fails_closed() -> None:
    """把 UoW 放在判权前会让未授权请求触碰租户数据。"""

    denied = _Authorizer(deny=True)
    service, factory, _ = _service(authorizer=denied)
    with pytest.raises(PermissionDenied):
        await service.create_candidate_from_sourcing(TENANT, _command(), actor=_actor())
    assert factory.calls == []

    Authorizer = _symbol("domains.products.permissions", "Phase2ProductAuthorizer")
    service, factory, _ = _service(authorizer=Authorizer(TENANT))
    with pytest.raises(PermissionDenied):
        await service.create_candidate_from_sourcing(
            OTHER_TENANT, _command(), actor=_actor()
        )
    assert factory.calls == []


@pytest.mark.asyncio
async def test_candidate_product_is_source_only_and_idempotent() -> None:
    """重复消费同一 Case+Candidate 不能创建第二张产品卡。"""

    service, factory, _ = _service()
    first = await service.create_candidate_from_sourcing(
        TENANT, _command(), actor=_actor()
    )
    second = await service.create_candidate_from_sourcing(
        TENANT, _command(), actor=_actor()
    )
    assert first == second
    assert len(factory.uow.products.rows) == 1
    created = factory.uow.products.rows[0]
    assert created.pool is ProductPool.CANDIDATE
    assert created.candidate_status is CandidateStatus.SOURCE_ONLY


@pytest.mark.asyncio
async def test_three_views_make_internal_fields_structurally_unrepresentable() -> None:
    """销售/客户 DTO 新增供应商、成本或原始价格字段时必须失败。"""

    service, _, _ = _service()
    product_id = await service.create_candidate_from_sourcing(
        TENANT, _command(), actor=_actor()
    )
    internal = await service.get_internal_view(
        TENANT, product_id, actor=_actor("sourcing")
    )
    sales = await service.get_sales_view(TENANT, product_id, actor=_actor("sales"))
    customer = await service.get_customer_view(
        TENANT, product_id, actor=_actor("sales")
    )

    internal_names = {item.name for item in fields(type(internal))}
    assert {
        "supplier_id",
        "internal_cost",
        "internal_cost_basis",
        "internal_cost_unit",
        "internal_cost_source_ref",
        "candidate_source",
    } <= internal_names
    forbidden = {
        "supplier_id",
        "supplier_name",
        "internal_cost",
        "internal_cost_basis",
        "internal_cost_unit",
        "internal_cost_source_ref",
        "candidate_source",
        "indicative_prices",
        "evidence_refs",
    }
    assert forbidden.isdisjoint({item.name for item in fields(type(sales))})
    assert forbidden.isdisjoint({item.name for item in fields(type(customer))})
    assert customer.inquiry_enabled is False
    assert customer.sample_request_enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing",
    [
        "internal_cost",
        "internal_cost_currency",
        "internal_cost_basis",
        "internal_cost_unit",
        "internal_cost_source_ref",
    ],
)
async def test_matching_requires_complete_internal_cost_five_tuple(
    missing: str,
) -> None:
    """缺任一成本维度必须排除自动交接，并返回结构化 finding。"""

    complete = _formal_product("prd_complete")
    incomplete = _formal_product(f"prd_missing_{missing}")
    if missing == "internal_cost":
        incomplete.internal_cost = None
    elif missing == "internal_cost_currency":
        incomplete.internal_cost = object.__new__(Money)
        object.__setattr__(incomplete.internal_cost, "amount", Decimal("1.25"))
        object.__setattr__(incomplete.internal_cost, "currency", "")
    else:
        setattr(incomplete, missing, None)
    service, _, _ = _service(products=_Products([incomplete, complete]))
    result = await service.search_for_matching(
        TENANT, " HINGES ", [" hinge "], actor=_actor()
    )
    assert [item.product_id for item in result.qualified_matches] == [
        complete.product_id
    ]
    finding = next(
        item for item in result.findings if item.product_id == incomplete.product_id
    )
    assert finding.code == "internal_cost_incomplete"
    assert missing in finding.missing_fields
