"""Slice 1 共享契约演示（确定性、无凭证/网络、金额只用 Decimal、不输出概率）。

运行：python scripts/demo_shared_contracts.py

体现的设计要点：
- 金额一律 Decimal 确定性代码计算（硬边界 2）
- 模型只做证据分类，置信度由代码推导为离散档位（硬边界 3）
- 影响商业决策的字段带 Provenance，事实与推断分离（硬边界 4/5）
- ID 时间有序（ULID），错误带结构化上下文
"""
from datetime import UTC, datetime
from decimal import Decimal

from shared.errors import CurrencyMismatchError, TradeOSError
from shared.schemas.evidence import EvidenceItem, EvidenceLevel, derive_confidence
from shared.schemas.identifiers import EmployeeId, RunId, TenantId, new_id
from shared.schemas.money import CurrencyCode, FxRate, Money, convert
from shared.schemas.provenance import (
    FactualField,
    InferredField,
    Provenance,
    SourceType,
)

_NOW = datetime(2026, 8, 8, tzinfo=UTC)
_USD = CurrencyCode("USD")
_CNY = CurrencyCode("CNY")


def main() -> None:
    print("== 1. Money：金额只用 Decimal 确定性计算（硬边界 2） ==")
    price = Money(Decimal("5000.00"), _USD)
    surcharge = Money(Decimal("300.50"), _USD)
    total = price.add(surcharge)
    print(f"  报价 5000.00 + 附加费 300.50 = {total.amount} {total.currency}")
    print(f"  数量 3：{price.multiply(Decimal(3)).amount} {_USD}")
    rounded = Money(Decimal("1.005"), _USD).round_to(2)
    print(f"  舍入 1.005 -> {rounded.amount}（默认 ROUND_HALF_UP）")
    try:
        price.add(Money(Decimal(100), _CNY))
    except CurrencyMismatchError as exc:
        print(f"  异币种相加被拒：{exc}")
    rate = FxRate(_USD, _CNY, Decimal("7.2345"), _NOW, "demo-fx-source")
    converted = convert(Money(Decimal(3), _USD), _CNY, rate)
    print(f"  按快照 7.2345 换算 3 USD -> {converted.amount} {_CNY}（不舍入，精确值）")

    print("== 2. 置信度：模型只做证据分类，代码推导离散档位（硬边界 3） ==")
    items = [
        EvidenceItem(
            level=EvidenceLevel.PUBLIC_COMPANY_EVENT,
            source_type="web_page",
            source_id="h1",
            observed_at=_NOW,
            summary="Acme 宣布扩建第二座工厂",
        ),
        EvidenceItem(
            level=EvidenceLevel.PUBLIC_COMPANY_EVENT,
            source_type="web_page",
            source_id="h2",
            observed_at=_NOW,
            summary="Acme 新增采购经理岗位",
        ),
    ]
    result = derive_confidence(items, now=_NOW)
    print(f"  档位：{result.tier.value}（离散值，不是小数）")
    print(f"  解释：{result.explanation}")
    print(f"  命中规则：{result.applied_rules}")

    print("== 3. Provenance：关键字段来源可追溯，事实与推断分离（硬边界 4/5） ==")
    provenance = Provenance(
        source_type=SourceType.WEB_PAGE,
        source_id="h1",
        extracted_by="model_v3",
        extracted_at=_NOW,
        source_url="https://example.com/news/expansion",
        page_hash="sha256:abc",
        confirmed_by=EmployeeId("e1"),
        confirmed_at=_NOW,
    )
    fact = FactualField[int](value=5000, provenance=provenance)
    inferred = InferredField[str](
        value="可能需要耐腐蚀五金件",
        based_on=[items[0]],
        inferred_by="model_v3",
        inferred_at=_NOW,
    )
    print(
        f"  事实：quantity={fact.value}，来源={fact.provenance.source_type.value}，"
        f"人工确认={fact.provenance.is_human_confirmed}"
    )
    print(f"  推断：{inferred.value}（依据 {len(inferred.based_on)} 条证据，不含置信度数值）")

    print("== 4. ID：时间有序 ULID（便于按主键范围扫描/分页） ==")
    first = new_id("opp")
    second = new_id("opp")
    print(f"  new_id('opp') 示例：{first}")
    print(f"  两次调用不同：{first != second}；均为 opp_ 开头 + 26 字符 Crockford base32")

    print("== 5. 错误结构化上下文（TradeOSError） ==")
    err = TradeOSError("演示错误", context={"tenant_id": TenantId("t1"), "run_id": RunId("r1")})
    print(f"  消息（str 只含消息）：{err}")
    print(f"  context：{err.context}")


if __name__ == "__main__":
    main()
