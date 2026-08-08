"""shared.schemas.provenance 的契约单测（行为断言，datetime.UTC，无 Any/type-ignore/noqa）。

拦截的变异：
1. WEB_PAGE 网页留痕校验缺失 / None 与空串、纯空白混为一谈
2. 确认人/确认时间不同有同无 / 单边缺失放行
3. source_id、extracted_by 空白放行
4. is_human_confirmed 判定错
5. FactualField 放行 AGENT_INFERENCE（事实字段混入推断）
6. InferredField 空 based_on 放行（无依据推断）
7. ExtractionChain 编辑三元组部分赋值放行
8. 三个泛型类的类型参数可实例化（公共泛型表面）
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from shared.errors import ValidationError
from shared.schemas.evidence import EvidenceItem, EvidenceLevel
from shared.schemas.identifiers import EmployeeId
from shared.schemas.provenance import (
    ExtractionChain,
    FactualField,
    InferredField,
    Provenance,
    SourceType,
)

_NOW = datetime(2026, 8, 8, tzinfo=UTC)


def _provenance(source_type: SourceType = SourceType.CONVERSATION) -> Provenance:
    if source_type == SourceType.WEB_PAGE:
        return Provenance(
            source_type=source_type,
            source_id="sid1",
            extracted_by="model_v3",
            extracted_at=_NOW,
            source_url="https://example.com",
            page_hash="sha256:abc",
        )
    return Provenance(
        source_type=source_type,
        source_id="sid1",
        extracted_by="model_v3",
        extracted_at=_NOW,
    )


# --- Provenance：WEB_PAGE 网页留痕 -------------------------------------------------


@pytest.mark.parametrize(
    ("source_url", "page_hash"),
    [
        (None, "sha256:abc"),  # 缺 URL
        ("https://example.com", None),  # 缺哈希
        ("", "sha256:abc"),  # URL 空串视为未填
        ("https://example.com", ""),  # 哈希空串视为未填
        ("   ", "sha256:abc"),  # URL 纯空白视为未填
        ("https://example.com", "   "),  # 哈希纯空白视为未填
    ],
)
def test_web_page_requires_url_and_hash(source_url: str | None, page_hash: str | None) -> None:
    with pytest.raises(ValidationError):
        Provenance(
            source_type=SourceType.WEB_PAGE,
            source_id="sid1",
            extracted_by="model_v3",
            extracted_at=_NOW,
            source_url=source_url,
            page_hash=page_hash,
        )


def test_web_page_valid_when_url_and_hash_present() -> None:
    p = _provenance(SourceType.WEB_PAGE)
    assert p.source_url == "https://example.com"
    assert p.page_hash == "sha256:abc"


@pytest.mark.parametrize(
    "source_type",
    [
        SourceType.CONVERSATION,
        SourceType.UPLOAD,
        SourceType.EMPLOYEE_INPUT,
        SourceType.AGENT_INFERENCE,
        SourceType.EXTERNAL_API,
    ],
)
def test_non_web_sources_may_omit_url_and_hash(source_type: SourceType) -> None:
    p = _provenance(source_type)
    assert p.source_url is None
    assert p.page_hash is None


# --- Provenance：确认人/确认时间同有同无 -------------------------------------------


@pytest.mark.parametrize(
    ("confirmed_by", "confirmed_at"),
    [
        (EmployeeId("e1"), None),  # 有确认人无确认时间
        (None, _NOW),  # 有确认时间无确认人
    ],
)
def test_confirmation_single_sided_raises(
    confirmed_by: EmployeeId | None, confirmed_at: datetime | None
) -> None:
    with pytest.raises(ValidationError):
        Provenance(
            source_type=SourceType.CONVERSATION,
            source_id="sid1",
            extracted_by="model_v3",
            extracted_at=_NOW,
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
        )


def test_confirmation_neither_valid() -> None:
    p = _provenance()
    assert p.confirmed_by is None
    assert p.confirmed_at is None


def test_confirmation_both_valid() -> None:
    p = Provenance(
        source_type=SourceType.CONVERSATION,
        source_id="sid1",
        extracted_by="model_v3",
        extracted_at=_NOW,
        confirmed_by=EmployeeId("e1"),
        confirmed_at=_NOW,
    )
    assert p.confirmed_by == EmployeeId("e1")
    assert p.confirmed_at == _NOW


# --- Provenance：source_id / extracted_by 非空 --------------------------------------


@pytest.mark.parametrize("blank", ["", "   "])
def test_source_id_blank_raises(blank: str) -> None:
    with pytest.raises(ValidationError):
        Provenance(SourceType.CONVERSATION, blank, "model_v3", _NOW)


@pytest.mark.parametrize("blank", ["", "   "])
def test_extracted_by_blank_raises(blank: str) -> None:
    with pytest.raises(ValidationError):
        Provenance(SourceType.CONVERSATION, "sid1", blank, _NOW)


# --- Provenance：is_human_confirmed -------------------------------------------------


def test_is_human_confirmed_false_when_unconfirmed() -> None:
    assert _provenance().is_human_confirmed is False


def test_is_human_confirmed_true_when_confirmed() -> None:
    p = Provenance(
        source_type=SourceType.CONVERSATION,
        source_id="sid1",
        extracted_by="model_v3",
        extracted_at=_NOW,
        confirmed_by=EmployeeId("e1"),
        confirmed_at=_NOW,
    )
    assert p.is_human_confirmed is True


# --- FactualField：拒绝 AGENT_INFERENCE，接受其余 -----------------------------------


def test_factual_field_rejects_agent_inference() -> None:
    with pytest.raises(ValidationError):
        FactualField[int](value=5000, provenance=_provenance(SourceType.AGENT_INFERENCE))


@pytest.mark.parametrize(
    "source_type",
    [
        SourceType.CONVERSATION,
        SourceType.WEB_PAGE,
        SourceType.UPLOAD,
        SourceType.EMPLOYEE_INPUT,
        SourceType.EXTERNAL_API,
    ],
)
def test_factual_field_accepts_other_sources(source_type: SourceType) -> None:
    f = FactualField[int](value=5000, provenance=_provenance(source_type))
    assert f.value == 5000


# --- InferredField：based_on 非空 ----------------------------------------------------


def _evidence_item() -> EvidenceItem:
    return EvidenceItem(
        level=EvidenceLevel.PUBLIC_COMPANY_EVENT,
        source_type="web_page",
        source_id="e1",
        observed_at=_NOW,
        summary="公开的工厂扩建公告",
    )


def test_inferred_field_empty_based_on_raises() -> None:
    with pytest.raises(ValidationError):
        InferredField[str](
            value="可能需要五金件",
            based_on=[],
            inferred_by="model_v3",
            inferred_at=_NOW,
        )


def test_inferred_field_with_evidence_valid() -> None:
    f = InferredField[str](
        value="可能需要五金件",
        based_on=[_evidence_item()],
        inferred_by="model_v3",
        inferred_at=_NOW,
    )
    assert f.value == "可能需要五金件"
    assert len(f.based_on) == 1


# --- ExtractionChain：编辑三元组同有同无 ----------------------------------------------


def test_extraction_chain_all_absent_valid() -> None:
    c = ExtractionChain[str](
        raw_artifact_id="art1",
        agent_extracted="原始提取",
        final_value="最终值",
    )
    assert c.employee_edited is None
    assert c.edited_by is None
    assert c.edited_at is None


def test_extraction_chain_all_present_valid() -> None:
    c = ExtractionChain[str](
        raw_artifact_id="art1",
        agent_extracted="原始提取",
        final_value="员工改后",
        employee_edited="员工改后",
        edited_by=EmployeeId("e1"),
        edited_at=_NOW,
    )
    assert c.employee_edited == "员工改后"


@pytest.mark.parametrize(
    ("employee_edited", "edited_by", "edited_at"),
    [
        ("edited", None, None),  # 只填编辑值
        (None, EmployeeId("e1"), None),  # 只填修改人
        (None, None, _NOW),  # 只填修改时间
        ("edited", EmployeeId("e1"), None),  # 缺修改时间
        ("edited", None, _NOW),  # 缺修改人
        (None, EmployeeId("e1"), _NOW),  # 缺编辑值
    ],
)
def test_extraction_chain_partial_edit_raises(
    employee_edited: str | None,
    edited_by: EmployeeId | None,
    edited_at: datetime | None,
) -> None:
    with pytest.raises(ValidationError):
        ExtractionChain[str](
            raw_artifact_id="art1",
            agent_extracted="原始提取",
            final_value="最终值",
            employee_edited=employee_edited,
            edited_by=edited_by,
            edited_at=edited_at,
        )
