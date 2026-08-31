"""产品服务的 tenant-bound、authorizer-first 实现。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from domains.products.models import (
    CandidateIndicativePriceRef as StoredPriceRef,
)
from domains.products.models import (
    CandidateStatus,
    Product,
    ProductCandidateSource,
    ProductCustomerView,
    ProductInternalView,
    ProductMatchFinding,
    ProductMatchResult,
    ProductPool,
    ProductSalesView,
    ProductSpecComparison,
    ProductSpecFact,
    ProductSpecMatchLevel,
    ProductSpecRequirement,
    QualifiedProductMatch,
)
from domains.products.permissions import ProductAction, ProductActor, ProductAuthorizer
from domains.products.repository import ProductsUnitOfWork
from domains.products.schemas import CandidateProductCreate
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import ProductId, TenantId, new_id

_SPACE_RE = re.compile(r"\s+")


class _CanonicalCandidateSourceRace(Exception):
    """触发当前 UoW 回滚后再读取 canonical winner 的内部控制信号。"""


def _normalize(value: str) -> str:
    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", value).strip()).casefold()


def _clock(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("产品服务时钟必须含时区")
    return value


def _lead_time(product: Product) -> str | None:
    if product.lead_time_days_min is None or product.lead_time_days_max is None:
        return None
    if product.lead_time_days_min == product.lead_time_days_max:
        return f"{product.lead_time_days_min} days"
    return f"{product.lead_time_days_min}-{product.lead_time_days_max} days"


def _missing_cost_fields(product: Product) -> tuple[str, ...]:
    missing: list[str] = []
    cost = product.internal_cost
    if cost is None:
        missing.extend(("internal_cost", "internal_cost_currency"))
    else:
        if not isinstance(cost.amount, Decimal) or not cost.amount.is_finite():
            missing.append("internal_cost")
        currency = cost.currency
        if (
            not isinstance(currency, str)
            or len(currency) != 3
            or not currency.isascii()
            or not currency.isalpha()
            or not currency.isupper()
        ):
            missing.append("internal_cost_currency")
    if (
        not isinstance(product.internal_cost_basis, str)
        or not product.internal_cost_basis.strip()
    ):
        missing.append("internal_cost_basis")
    if (
        not isinstance(product.internal_cost_unit, str)
        or not product.internal_cost_unit.strip()
    ):
        missing.append("internal_cost_unit")
    if (
        product.internal_cost_source_ref is None
        or not str(product.internal_cost_source_ref).strip()
    ):
        missing.append("internal_cost_source_ref")
    return tuple(dict.fromkeys(missing))


def _normalized_requirements(
    required_specs: tuple[ProductSpecRequirement, ...],
) -> tuple[ProductSpecRequirement, ...]:
    normalized: list[ProductSpecRequirement] = []
    names: set[str] = set()
    for item in required_specs:
        if not isinstance(item, ProductSpecRequirement):
            raise ValidationError("产品匹配规格要求类型无效")
        name = _normalize(item.spec_name)
        required = _normalize(item.required)
        if not name or not required or name in names:
            raise ValidationError("产品匹配规格要求必须非空且规范化后不重复")
        names.add(name)
        normalized.append(ProductSpecRequirement(name, required))
    if not normalized:
        raise ValidationError("产品匹配至少需要一项逐项规格要求")
    return tuple(sorted(normalized, key=lambda item: item.spec_name))


def _compare_specs(
    product: Product,
    requirements: tuple[ProductSpecRequirement, ...],
) -> tuple[tuple[ProductSpecComparison, ...], bool]:
    normalized_facts: dict[str, ProductSpecFact] = {}
    duplicate = False
    for raw_name, fact in sorted(product.match_specs.items()):
        name = _normalize(raw_name)
        if (
            not name
            or not isinstance(fact, ProductSpecFact)
            or name in normalized_facts
        ):
            duplicate = True
            continue
        normalized_facts[name] = fact
    comparisons: list[ProductSpecComparison] = []
    for requirement in requirements:
        matched_fact = normalized_facts.get(requirement.spec_name)
        offered = (
            _normalize(matched_fact.value)
            if matched_fact is not None
            and isinstance(matched_fact.value, str)
            and _normalize(matched_fact.value)
            else None
        )
        evidence_ref = matched_fact.evidence_ref if matched_fact is not None else None
        if offered is None or evidence_ref is None or not str(evidence_ref).strip():
            level = ProductSpecMatchLevel.UNKNOWN
        elif requirement.spec_name == "moq":
            try:
                required_quantity = int(requirement.required)
                offered_moq = int(offered)
            except ValueError:
                level = ProductSpecMatchLevel.UNKNOWN
            else:
                if (
                    isinstance(product.moq, bool)
                    or not isinstance(product.moq, int)
                    or product.moq < 1
                    or required_quantity < 1
                    or offered_moq != product.moq
                ):
                    level = ProductSpecMatchLevel.UNKNOWN
                elif required_quantity >= offered_moq:
                    level = ProductSpecMatchLevel.EXACT
                else:
                    level = ProductSpecMatchLevel.DIFFERENT
        elif requirement.spec_name == "unit" and (
            not isinstance(product.internal_cost_unit, str)
            or offered != _normalize(product.internal_cost_unit)
        ):
            level = ProductSpecMatchLevel.UNKNOWN
        elif offered == requirement.required:
            level = ProductSpecMatchLevel.EXACT
        else:
            level = ProductSpecMatchLevel.DIFFERENT
        comparisons.append(
            ProductSpecComparison(
                spec_name=requirement.spec_name,
                required=requirement.required,
                offered=offered,
                level=level,
                evidence_ref=evidence_ref,
            )
        )
    return tuple(comparisons), duplicate


class ProductServiceImpl:
    """公开读写先授权，随后仅进入由 tenant_id 绑定的 Products UoW。"""

    def __init__(
        self,
        uow_factory: Callable[[TenantId], ProductsUnitOfWork],
        authorizer: ProductAuthorizer,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise ValidationError("产品事务依赖无效")
        if not isinstance(authorizer, ProductAuthorizer):
            raise ValidationError("产品授权依赖无效")
        self._uow_factory = uow_factory
        self._authorizer = authorizer
        self._now = now or (lambda: datetime.now(UTC))

    def _require(
        self, tenant_id: TenantId, actor: ProductActor, action: ProductAction
    ) -> None:
        self._authorizer.require(actor, action, tenant_id)

    async def create_candidate_from_sourcing(
        self,
        tenant_id: TenantId,
        command: CandidateProductCreate,
        *,
        actor: ProductActor,
    ) -> ProductId:
        self._require(tenant_id, actor, ProductAction.CANDIDATE_CREATE)
        if not isinstance(command, CandidateProductCreate):
            raise ValidationError("候选产品创建命令无效")
        try:
            async with self._uow_factory(tenant_id) as uow:
                existing = await uow.candidate_sources.get_by_origin(
                    tenant_id, command.sourcing_case_id, command.supplier_candidate_id
                )
                if existing is not None:
                    return existing.product_id
                product_id = ProductId(new_id("prd"))
                product = Product(
                    product_id=product_id,
                    tenant_id=tenant_id,
                    pool=ProductPool.CANDIDATE,
                    candidate_status=CandidateStatus.SOURCE_ONLY,
                    name_zh=command.name_zh,
                    name_en=command.name_en,
                    category=command.category,
                    spec_summary=command.spec_summary,
                    moq=command.moq,
                    created_at=_clock(self._now()),
                )
                source = ProductCandidateSource(
                    tenant_id=tenant_id,
                    product_id=product_id,
                    sourcing_case_id=command.sourcing_case_id,
                    supplier_candidate_id=command.supplier_candidate_id,
                    created_at=product.created_at,
                    indicative_prices=tuple(
                        StoredPriceRef(
                            minimum_quantity=item.minimum_quantity,
                            unit_amount=item.unit_amount,
                            currency=item.currency,
                            unit=item.unit,
                            evidence_ref=item.evidence_ref,
                        )
                        for item in command.indicative_prices
                    ),
                )
                await uow.products.add(tenant_id, product)
                inserted = await uow.candidate_sources.add(tenant_id, source)
                if inserted.product_id != product_id:
                    raise _CanonicalCandidateSourceRace
                return product_id
        except _CanonicalCandidateSourceRace:
            pass

        winner = None
        failed = False
        try:
            async with self._uow_factory(tenant_id) as uow:
                winner = await uow.candidate_sources.get_by_origin(
                    tenant_id,
                    command.sourcing_case_id,
                    command.supplier_candidate_id,
                )
        except Exception:  # noqa: BLE001 -- fresh read 自由错误不得跨域
            failed = True
        if failed or winner is None:
            raise TransientError("候选产品 canonical 来源暂不可见")
        return winner.product_id

    async def search_for_matching(
        self,
        tenant_id: TenantId,
        category: str,
        keywords: list[str],
        required_specs: tuple[ProductSpecRequirement, ...],
        *,
        actor: ProductActor,
    ) -> ProductMatchResult:
        self._require(tenant_id, actor, ProductAction.MATCH_SEARCH)
        normalized_category = _normalize(category)
        if not normalized_category:
            raise ValidationError("产品匹配品类不能为空")
        normalized_keywords = sorted(
            {_normalize(keyword) for keyword in keywords if _normalize(keyword)}
        )
        normalized_requirements = _normalized_requirements(required_specs)
        async with self._uow_factory(tenant_id) as uow:
            matches = await uow.products.search(
                tenant_id,
                [ProductPool.FORMAL, ProductPool.CANDIDATE],
                normalized_category,
                normalized_keywords,
                50,
            )
        qualified: list[QualifiedProductMatch] = []
        findings: list[ProductMatchFinding] = []
        for product in sorted(matches, key=lambda item: str(item.product_id))[:50]:
            missing = _missing_cost_fields(product)
            comparisons, duplicate_specs = _compare_specs(
                product, normalized_requirements
            )
            if missing:
                findings.append(
                    ProductMatchFinding(
                        product_id=product.product_id,
                        code="internal_cost_incomplete",
                        missing_fields=missing,
                        spec_comparisons=comparisons,
                    )
                )
            elif duplicate_specs:
                findings.append(
                    ProductMatchFinding(
                        product_id=product.product_id,
                        code="product_spec_duplicate",
                        missing_fields=(),
                        spec_comparisons=comparisons,
                    )
                )
            elif any(
                item.level is ProductSpecMatchLevel.UNKNOWN for item in comparisons
            ):
                findings.append(
                    ProductMatchFinding(
                        product_id=product.product_id,
                        code="product_spec_unknown",
                        missing_fields=tuple(
                            item.spec_name
                            for item in comparisons
                            if item.level is ProductSpecMatchLevel.UNKNOWN
                        ),
                        spec_comparisons=comparisons,
                    )
                )
            elif any(
                item.level is ProductSpecMatchLevel.DIFFERENT for item in comparisons
            ):
                findings.append(
                    ProductMatchFinding(
                        product_id=product.product_id,
                        code="product_spec_different",
                        missing_fields=(),
                        spec_comparisons=comparisons,
                    )
                )
            else:
                qualified.append(QualifiedProductMatch(product, comparisons))
        return ProductMatchResult(tuple(qualified), tuple(findings))

    async def _get(self, tenant_id: TenantId, product_id: ProductId) -> Product:
        async with self._uow_factory(tenant_id) as uow:
            product = await uow.products.get(tenant_id, product_id)
        if product is None:
            raise ValidationError("产品不存在或租户不匹配")
        return product

    async def get_internal_view(
        self, tenant_id: TenantId, product_id: ProductId, *, actor: ProductActor
    ) -> ProductInternalView:
        self._require(tenant_id, actor, ProductAction.INTERNAL_VIEW)
        async with self._uow_factory(tenant_id) as uow:
            product = await uow.products.get(tenant_id, product_id)
            if product is None:
                raise ValidationError("产品不存在或租户不匹配")
            source = await uow.candidate_sources.get_by_product(tenant_id, product_id)
        return ProductInternalView(
            product_id=str(product.product_id),
            pool=product.pool.value,
            name_zh=product.name_zh,
            name_en=product.name_en,
            category=product.category,
            supplier_id=product.supplier_id,
            internal_cost=product.internal_cost,
            internal_cost_basis=product.internal_cost_basis,
            internal_cost_unit=product.internal_cost_unit,
            internal_cost_source_ref=product.internal_cost_source_ref,
            margin_note=None,
            known_issues=list(product.known_issues),
            moq=product.moq,
            lead_time_display=_lead_time(product),
            candidate_status=product.candidate_status.value
            if product.candidate_status is not None
            else None,
            candidate_source=source,
        )

    async def get_sales_view(
        self, tenant_id: TenantId, product_id: ProductId, *, actor: ProductActor
    ) -> ProductSalesView:
        self._require(tenant_id, actor, ProductAction.SALES_VIEW)
        product = await self._get(tenant_id, product_id)
        return ProductSalesView(
            product_id=str(product.product_id),
            name_zh=product.name_zh,
            name_en=product.name_en,
            category=product.category,
            selling_points=list(product.selling_points),
            allowed_price_min=product.allowed_price_min,
            allowed_price_max=product.allowed_price_max,
            moq=product.moq,
            lead_time_display=_lead_time(product),
        )

    async def get_customer_view(
        self, tenant_id: TenantId, product_id: ProductId, *, actor: ProductActor
    ) -> ProductCustomerView:
        self._require(tenant_id, actor, ProductAction.CUSTOMER_VIEW)
        product = await self._get(tenant_id, product_id)
        source_only = product.candidate_status is CandidateStatus.SOURCE_ONLY
        return ProductCustomerView(
            product_id=str(product.product_id),
            name_en=product.name_en,
            category=product.category,
            spec_display=product.spec_summary,
            inquiry_enabled=not source_only,
            sample_request_enabled=not source_only,
        )


__all__ = ("ProductServiceImpl",)
