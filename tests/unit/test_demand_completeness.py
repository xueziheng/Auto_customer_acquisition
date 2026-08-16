"""ValidatedNeed 完整度确定性推导单元测试（plan 2026-08-16-demand-completeness-derivation）。

RED 预期：``completeness`` / ``is_sourcing_ready`` / ``missing_fields_for_sourcing``
当前均为 ``raise NotImplementedError`` 骨架——失败原因必须是骨架未实现，
不是 fixture/import/类型/环境错误。

契约（15 项，均真实 Provenance + FactualField、零 mock、动态 import 域内
models 以通过 check_boundaries）：
- 累积阶梯 0–5（最高连续满足级别）：0 product_category 无；1 仅 product_category；
  2 application 或 size_spec 有但 quantity 无；3 quantity 有但 destination 或
  required_by 至少一个无（寻源门槛）；4 destination+required_by 有但 material
  或 size_spec 至少一个无；5 累积全部满足（application 不单独强制）
- presence 只判 ``FactualField is None``，不查 value 真值（value=0/"" 算存在）
- is_sourcing_ready 精确 ``completeness >= 3``
- missing_fields_for_sourcing 只返回当前阻塞 level-3 的最低层，固定顺序：
  ["product_category"] → ["application","size_spec"]（替代成对）→ ["quantity"] → []
- 属性调用不改输入对象/字段；返回新 list
"""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime
from typing import Any

from shared.schemas.identifiers import (
    MessageId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.provenance import FactualField, Provenance, SourceType

_models = importlib.import_module("domains.demand.models")
ValidatedNeed = _models.ValidatedNeed

NOW = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)

#: 确认需求的那条消息（_need.source_message_id 与全部 FactualField 的
#: Provenance.source_id 共享同一来源，保证「真实 Provenance」语义成立）
SOURCE_MESSAGE_ID = MessageId("msg_demand_test_001")


def _provenance() -> Provenance:
    return Provenance(
        source_type=SourceType.CONVERSATION,
        source_id=str(SOURCE_MESSAGE_ID),
        extracted_by="model-v1",
        extracted_at=NOW,
    )


def _field(value: object) -> FactualField:
    return FactualField(value=value, provenance=_provenance())


def _need(**overrides: object) -> Any:
    """level-5 全字段基准；overrides 把指定字段置 None 得到各级。"""
    fields: dict[str, object] = {
        "need_id": ValidatedNeedId(new_id("nd")),
        "tenant_id": TenantId(new_id("tn")),
        "account_id": ProspectAccountId(new_id("acct")),
        "product_category": _field("hinges"),
        "source_message_id": SOURCE_MESSAGE_ID,
        "created_at": NOW,
        "application": _field("marine"),
        "material": _field("stainless steel"),
        "size_spec": _field("M8"),
        "quantity": _field(5000),
        "destination": _field("Rotterdam"),
        "required_by": _field(date(2026, 12, 31)),
    }
    fields.update(overrides)
    return ValidatedNeed(**fields)


def test_factual_fields_provenance_points_to_source_message() -> None:
    """FactualField 的 provenance.source_id 指向 need 的确认消息（硬边界 4
    语义：每个事实字段能点到客户说这句话的那条消息）。"""
    need = _need()
    assert need.quantity.provenance.source_id == str(need.source_message_id)
    assert need.product_category.provenance.source_id == str(need.source_message_id)


def test_completeness_level_0_product_category_missing() -> None:
    """product_category 无 → 0（即使 quantity/material 等后级字段存在——累积不跳级）。"""
    need = _need(product_category=None, quantity=_field(5000), material=_field("steel"))
    assert need.completeness == 0


def test_completeness_level_1_only_product_category() -> None:
    """仅 product_category → 1。"""
    need = _need(application=None, size_spec=None, quantity=None)
    assert need.completeness == 1


def test_completeness_level_2_or_semantics() -> None:
    """application 或 size_spec 至少一个有、quantity 无 → 2（OR 双侧）。"""
    via_application = _need(application=_field("marine"), size_spec=None, quantity=None)
    assert via_application.completeness == 2
    via_size_spec = _need(application=None, size_spec=_field("M8"), quantity=None)
    assert via_size_spec.completeness == 2


def test_completeness_level_3_quantity_without_destination_or_required_by() -> None:
    """quantity 有但 destination 或 required_by 至少一个无 → 3。"""
    no_destination = _need(destination=None)
    assert no_destination.completeness == 3
    no_required_by = _need(required_by=None)
    assert no_required_by.completeness == 3


def test_completeness_level_4_destination_required_by_but_material_or_size_spec_missing() -> None:
    """destination+required_by 有但 material 或 size_spec 至少一个无 → 4。"""
    no_material = _need(material=None)
    assert no_material.completeness == 4
    no_size_spec = _need(size_spec=None)
    assert no_size_spec.completeness == 4


def test_completeness_level_5_all_cumulative_present() -> None:
    """累积全部满足 → 5；application 为 None 但 size_spec 有 → 仍 5。"""
    assert _need().completeness == 5
    no_application = _need(application=None)
    assert no_application.completeness == 5


def test_completeness_is_cumulative_no_skip() -> None:
    """后级字段存在不能跳级：quantity 有但 product_category 无 → 0；
    material/size_spec 都有但 required_by 无 → 3。"""
    assert _need(product_category=None, quantity=_field(5000)).completeness == 0
    assert _need(required_by=None).completeness == 3


def test_presence_is_none_based_not_truthiness() -> None:
    """value 假值仍算字段存在：quantity=0 → 5；application="" 且 size_spec/material
    缺 → 4（属性不偷做字段值业务验证）。"""
    zero_quantity = _need(quantity=_field(0))
    assert zero_quantity.completeness == 5
    assert zero_quantity.quantity.value == 0  # 字段值原样保留
    blank_application = _need(application=_field(""), size_spec=None, material=None)
    assert blank_application.completeness == 4


def test_is_sourcing_ready_threshold_2_vs_3() -> None:
    """level 2 → False；level 3 → True（精确 >= 3）。"""
    level2 = _need(quantity=None)
    assert level2.completeness == 2
    assert level2.is_sourcing_ready() is False
    level3 = _need(destination=None)
    assert level3.completeness == 3
    assert level3.is_sourcing_ready() is True


def test_missing_fields_product_category_first() -> None:
    """product_category 缺（其余随意）→ ["product_category"]。"""
    need = _need(product_category=None)
    assert need.missing_fields_for_sourcing() == ["product_category"]


def test_missing_fields_alternative_pair() -> None:
    """product_category 有、application 与 size_spec 都缺 → ["application","size_spec"]。"""
    need = _need(application=None, size_spec=None)
    assert need.missing_fields_for_sourcing() == ["application", "size_spec"]


def test_missing_fields_quantity_when_level2_satisfied() -> None:
    """OR 已满足、quantity 缺 → 只列 ["quantity"]；destination/required_by/material
    即使也缺也不列出（只返回当前阻塞层）。"""
    need = _need(
        application=_field("marine"),
        quantity=None,
        destination=None,
        required_by=None,
        material=None,
    )
    assert need.missing_fields_for_sourcing() == ["quantity"]


def test_missing_fields_empty_when_sourcing_ready() -> None:
    """completeness >= 3（即使 destination/required_by/material 仍缺）→ []。"""
    level3 = _need(destination=None, required_by=None, material=None, size_spec=None)
    assert level3.completeness == 3
    assert level3.missing_fields_for_sourcing() == []
    level5 = _need()
    assert level5.missing_fields_for_sourcing() == []


def test_properties_do_not_mutate() -> None:
    """调用三个属性不改输入对象/字段；missing_fields 返回新 list。"""
    need = _need(destination=None)  # level 3
    before = tuple(
        (
            f is None,
            f.value if f is not None else None,
        )
        for f in (
            need.product_category,
            need.application,
            need.material,
            need.size_spec,
            need.quantity,
            need.destination,
            need.required_by,
        )
    )
    assert need.completeness == 3
    assert need.is_sourcing_ready() is True
    assert need.missing_fields_for_sourcing() == []
    after = tuple(
        (
            f is None,
            f.value if f is not None else None,
        )
        for f in (
            need.product_category,
            need.application,
            need.material,
            need.size_spec,
            need.quantity,
            need.destination,
            need.required_by,
        )
    )
    assert after == before  # 字段未被修改
    # 同一对象连续调用返回新 list：内容相同、identity 不同；修改 first
    # 后再调用同一对象仍返回 ["quantity"]（改返回值不影响对象后续行为）
    missing_need = _need(quantity=None)
    first = missing_need.missing_fields_for_sourcing()
    second = missing_need.missing_fields_for_sourcing()
    assert first == second == ["quantity"]
    assert first is not second
    first.append("quantity")
    assert missing_need.missing_fields_for_sourcing() == ["quantity"]
