"""客户可见邮件的确定性禁止承诺门禁。"""

from __future__ import annotations

import importlib

import pytest

from domains.quotations.service import contains_forbidden_commitment
from shared.errors import ValidationError


def _commitments() -> object:
    return importlib.import_module("domains.quotations.models").ForbiddenAutoCommitment


@pytest.mark.parametrize(
    ("text", "expected_name"),
    [
        ("Our price is USD 12.50 per unit.", "FIRST_CONCRETE_PRICE"),
        ("Price: 12.50 per unit.", "FIRST_CONCRETE_PRICE"),
        ("Unit price=12.50.", "FIRST_CONCRETE_PRICE"),
        ("Price：１２．５０ per unit.", "FIRST_CONCRETE_PRICE"),
        ("正式报价：每件人民币 88 元。", "FORMAL_QUOTATION"),
        ("给您 8％ 折扣。", "DISCOUNT"),
        ("We have 500 units in stock.", "STOCK_COMMITMENT"),
        ("Delivery within 14 days is guaranteed.", "DELIVERY_DATE_COMMITMENT"),
        ("We can ship tomorrow.", "DELIVERY_DATE_COMMITMENT"),
        ("We will deliver today.", "DELIVERY_DATE_COMMITMENT"),
        ("Shipment next week.", "DELIVERY_DATE_COMMITMENT"),
        ("We can dispatch next Monday.", "DELIVERY_DATE_COMMITMENT"),
        ("Delivery by Friday.", "DELIVERY_DATE_COMMITMENT"),
        ("We will ship on 2026-10-12.", "DELIVERY_DATE_COMMITMENT"),
        ("明天发货。", "DELIVERY_DATE_COMMITMENT"),
        ("This product is CE certified.", "CERTIFICATION_COMMITMENT"),
        ("PAYMENT TERMS: Net 30.", "PAYMENT_TERMS"),
        ("签署正式合同后发货。", "CONTRACT_TERMS"),
        ("You will be our exclusive distributor.", "EXCLUSIVE_DISTRIBUTION"),
        ("We guarantee the quality for five years.", "QUALITY_GUARANTEE"),
        ("目录外型号参考价约 $3.20。", "OFF_CATALOG_REFERENCE_PRICE"),
    ],
)
def test_forbidden_commitment_variants_return_typed_category(
    text: str, expected_name: str
) -> None:
    """删掉任一独立类别规则都会让具体客户承诺穿过 Campaign 审批。"""
    commitment = _commitments()
    matches = contains_forbidden_commitment(text)
    assert getattr(commitment, expected_name) in matches
    assert all(isinstance(item, commitment) for item in matches)


@pytest.mark.parametrize(
    "text",
    [
        "Could you share the specification?",
        "方便确认贵司当前采购需求吗？",
        "We can discuss commercial terms after reviewing your requirements.",
        "Please tell us the expected quantity and destination.",
        "Our team can arrange a call next week to understand the project.",
        "Could you share the delivery destination tomorrow?",
        " Could you share the specification? ",
        "\n\tCould you share the specification?\r\n",
    ],
)
def test_safe_discovery_copy_has_no_commitment(text: str) -> None:
    """把普通需求探索一律拒绝会让单封人工开发信功能不可用。"""
    assert contains_forbidden_commitment(text) == []


def test_returns_all_matches_once_in_enum_declaration_order() -> None:
    """首命中短路会让人工一次只看到一个问题并反复修改邮件。"""
    commitment = _commitments()
    matches = contains_forbidden_commitment(
        "Formal quotation: USD 12.50, 8% discount, Net 30, delivery in 14 days."
    )
    assert matches == [
        commitment.FIRST_CONCRETE_PRICE,
        commitment.FORMAL_QUOTATION,
        commitment.DISCOUNT,
        commitment.DELIVERY_DATE_COMMITMENT,
        commitment.PAYMENT_TERMS,
    ]


@pytest.mark.parametrize(
    "text",
    [
        "We can commit 250 for this order.",
        "我们承诺 250。",
        "Total will be €1,200.",
        "MOQ is 100 and we guarantee availability.",
    ],
)
def test_ambiguous_commercial_numbers_fail_closed(text: str) -> None:
    """无法归类的商业数字若默认放行会形成未审批承诺。"""
    commitment = _commitments()
    assert commitment.FIRST_CONCRETE_PRICE in contains_forbidden_commitment(text)


@pytest.mark.parametrize(
    "text", [None, 1, b"text", "", " \t\r\n ", "bad\x00text", "bad\x7ftext"]
)
def test_guard_rejects_malformed_runtime_input(text: object) -> None:
    """纯空白或非法输入不能当作不含承诺的安全文本。"""
    with pytest.raises(ValidationError):
        contains_forbidden_commitment(text)  # type: ignore[arg-type]


def test_guard_rejects_unbounded_content() -> None:
    """无界正文检查会让单次 API 请求放大 CPU/内存消耗。"""
    with pytest.raises(ValidationError):
        contains_forbidden_commitment("x" * 100_001)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_guard_accepts_paragraphs_and_checks_wrapped_commitments(newline: str) -> None:
    """合法换行既不能拒绝普通开发信，也不能让承诺跨行后漏检。"""
    assert contains_forbidden_commitment(
        f"Hello,{newline}{newline}Could you share your current sourcing needs?"
    ) == []
    commitment = _commitments()
    assert commitment.DELIVERY_DATE_COMMITMENT in contains_forbidden_commitment(
        f"We can ship{newline}tomorrow."
    )
    assert commitment.FIRST_CONCRETE_PRICE in contains_forbidden_commitment(
        f"We can commit{newline}250 for this order."
    )


@pytest.mark.parametrize("control", ["\r", "\x00", "\x0b", "\x1b", "\x7f", "\x85"])
def test_guard_still_rejects_unsafe_controls(control: str) -> None:
    """放行正文换行不能同时放行孤立CR、终端控制或C1控制字符。"""
    with pytest.raises(ValidationError):
        contains_forbidden_commitment(f"Hello,{control}Could you share your needs?")
