"""金额与汇率。

硬边界 2：**所有金额用 ``Decimal``，禁止 ``float``。**

为什么这条必须是硬边界：``float`` 的二进制表示无法精确表达 0.1、0.01
这类十进制小数。单笔误差极小，但成本表有二十多个成本项，逐项累加后
误差会显现；跨币种换算再乘一次汇率，误差进一步放大。

结果是报价单上的数字和成本表算出来的不一致——客户看到的价格与内部
利润核算对不上，这是会直接损失信任和金钱的 bug，而且极难排查。

**模型永远不产生最终金额。** 大模型负责识别可能遗漏的成本项、解释
计算结果、提醒低利润风险、生成客户可读说明；数字本身由本模块的
确定性代码算。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import (
    ROUND_05UP,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Decimal,
    InvalidOperation,
)
from typing import Annotated, NewType

from pydantic import BeforeValidator, ValidationInfo, WithJsonSchema

from shared.errors import CurrencyMismatchError, ValidationError

CurrencyCode = NewType("CurrencyCode", str)
"""ISO 4217 三字母币种代码，例如 ``USD``、``CNY``、``EUR``。

金额与币种必须成对出现。裸数字在跨境贸易场景里没有意义——
"5000" 是美元还是人民币，差了七倍。
"""


def _parse_wire_decimal(value: object, info: ValidationInfo) -> Decimal:
    """区分 Python 与 JSON 边界，拒绝会在解析前丢失精度的数字输入。"""
    if info.mode == "json":
        if not isinstance(value, str):
            raise ValueError("JSON 金额必须为十进制字符串")
        try:
            decimal_value = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("JSON 金额必须是有效十进制字符串") from exc
    elif isinstance(value, Decimal):
        decimal_value = value
    else:
        raise ValueError("Python 金额必须是 Decimal")
    if not decimal_value.is_finite():
        raise ValueError("金额必须是有限 Decimal")
    return decimal_value


WireDecimal = Annotated[
    Decimal,
    BeforeValidator(_parse_wire_decimal),
    WithJsonSchema({"type": "string"}),
]
"""Pydantic 边界金额：Python 只收 Decimal，JSON 只收十进制字符串。"""

# 合法的 decimal 舍入策略（stdlib，无外部依赖）。
_VALID_ROUNDING = {
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    ROUND_05UP,
}


def _validate_currency(currency: CurrencyCode) -> None:
    """币种必须是 3 位大写 ASCII 字母（对「ISO 4217 三字母」的最小约束）。"""
    if (
        not isinstance(currency, str)
        or len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or not currency.isupper()
    ):
        raise ValidationError("币种必须是 3 位大写 ASCII 字母")


@dataclass(frozen=True)
class Money:
    """金额。不可变。

    字段：
        amount:   金额，必须是 ``Decimal``
        currency: 币种

    实现要求：
    - ``__post_init__`` 校验 ``amount`` 是 ``Decimal``，传入 ``float``
      直接抛错。不要"友好地"自动转换——静默转换会让错误留到生产。
    - 算术运算（加减）要求币种一致，不一致抛错，**不自动换算**。
      自动换算会隐藏"用了哪个时点的汇率"这个关键信息。
    - 乘除只允许乘 ``Decimal`` 或 ``int``（数量、比例），不允许
      两个 Money 相乘。
    - 舍入必须显式指定策略和精度，不依赖默认值。
    """

    amount: WireDecimal
    currency: CurrencyCode

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise ValidationError("amount 必须是 Decimal（硬边界 2，拒绝 float/int/bool）")
        if not self.amount.is_finite():
            raise ValidationError("amount 必须是有限 Decimal（拒绝 NaN/Infinity）")
        _validate_currency(self.currency)

    def add(self, other: Money) -> Money:
        """同币种相加。币种不一致抛 ``CurrencyMismatchError``。"""
        if self.currency != other.currency:
            raise CurrencyMismatchError(
                f"币种不匹配：{self.currency} 与 {other.currency} 不可直接相加",
                context={"self_currency": self.currency, "other_currency": other.currency},
            )
        return Money(self.amount + other.amount, self.currency)

    def multiply(self, factor: Decimal | int) -> Money:
        """乘以数量或比例（Decimal 或 int，含负/零）；拒绝 float/bool/Money/非有限。"""
        if isinstance(factor, bool) or not isinstance(factor, (Decimal, int)):
            raise ValidationError("乘数只允许 Decimal 或 int")
        if isinstance(factor, Decimal) and not factor.is_finite():
            raise ValidationError("乘数必须是有限 Decimal")
        return Money(self.amount * factor, self.currency)

    def round_to(self, places: int, strategy: str = "ROUND_HALF_UP") -> Money:
        """按指定精度和策略舍入。策略必须显式传入或使用本项目统一默认值。"""
        if isinstance(places, bool) or not isinstance(places, int):
            raise ValidationError("places 必须是 int")
        if places < 0:
            raise ValidationError("places 不能为负")
        if strategy not in _VALID_ROUNDING:
            raise ValidationError("未知的舍入策略")
        quantum = Decimal(1).scaleb(-places)
        return Money(self.amount.quantize(quantum, rounding=strategy), self.currency)


@dataclass(frozen=True)
class FxRate:
    """汇率快照。

    字段：
        base:        源币种
        quote:       目标币种
        rate:        汇率（1 单位 base 兑多少 quote）
        observed_at: 取得时间
        source:      来源（哪个数据源）

    **报价时必须锁定汇率快照并与报价版本绑定。** 汇率天天变，
    历史报价不能因为今天汇率变了而显示不同的金额——那会让
    "我们上周报的是多少"变成无法回答的问题。
    """

    base: CurrencyCode
    quote: CurrencyCode
    rate: WireDecimal
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.rate, Decimal):
            raise ValidationError("rate 必须是 Decimal")
        if not self.rate.is_finite():
            raise ValidationError("rate 必须是有限 Decimal")
        if self.rate <= 0:
            raise ValidationError("rate 必须为正")
        _validate_currency(self.base)
        _validate_currency(self.quote)
        if self.base == self.quote and self.rate != Decimal(1):
            raise ValidationError("同币种汇率必须为 1")
        if not self.source or not self.source.strip():
            raise ValidationError("source 不能为空")


def convert(amount: Money, to: CurrencyCode, rate: FxRate) -> Money:
    """按给定汇率快照换算币种。

    实现要求：
    - 校验 ``rate.base == amount.currency`` 且 ``rate.quote == to``，
      不匹配抛错。不要自动取倒数——那容易在方向上出错。
    - 换算结果必须能追溯到用的是哪个 ``FxSnapshotId``，
      调用方负责把快照 ID 记进成本表或报价版本。
    - 不在这里做舍入，由调用方按业务场景决定精度。
    """
    if rate.base != amount.currency:
        raise CurrencyMismatchError(
            f"汇率基准币种 {rate.base} 与金额币种 {amount.currency} 不匹配",
            context={"rate_base": rate.base, "amount_currency": amount.currency, "to": to},
        )
    if rate.quote != to:
        raise CurrencyMismatchError(
            f"汇率报价币种 {rate.quote} 与目标币种 {to} 不匹配",
            context={"rate_quote": rate.quote, "to": to, "amount_currency": amount.currency},
        )
    return Money(amount.amount * rate.rate, to)


class PriceBasis:
    """价格基准。硬边界 7 的落地枚举。

    ``INDICATIVE`` —— 抓取的参考价（网页、目录、公开列表）。
        网页价格在没和供应商对话之前，对报价几乎没用：真实价格取决于
        数量档、材质等级、定制要求和谈判。
        **只能用于内部判断"这个方向值不值得做"。**

    ``QUOTED`` —— 供应商针对具体规格和数量给出、带有效期的报价。
        **只有这个能进客户可见报价。**

    门禁规则：只有 ``INDICATIVE`` 价格时，系统不允许生成客户可见报价，
    除非人工明确接受风险并留痕。这道门槛直接防止亏本报价。
    """

    INDICATIVE = "indicative"
    QUOTED = "quoted"
