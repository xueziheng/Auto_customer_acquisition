"""回复评估集完整性验收：先建评估集（HANDBOOK 切片 6），再写 prompt。

样本 = 输入 + 期望行为（分类值 / 必须拦截 / 必须提取的字段）。机器可检的
部分在此锁定：目录与 14 类枚举一致、每类数量下限（退订/自动回复各 20）、
JSON 形状、客户内容全英文、提取字段 quote 必须指向原消息（provenance）、
拦截期望与确定性动作映射（REPLY_ACTIONS）一致。评估集只增不改——本测试
是球门，不是样例生成器。

TDD RED：评估集尚为空，以下断言全部失败。
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
REPLIES_DIR = REPO / "tests" / "evals" / "replies"

_CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
_CASE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")

MIN_SAMPLES_PER_CATEGORY = 10
MIN_SAMPLES_UNSUBSCRIBE = 20
MIN_SAMPLES_AUTO_REPLY = 20

#: 字段提取的目标词表：domains/demand ValidatedNeed 的 FactualField 业务字段。
NEED_FIELD_NAMES = frozenset(
    {
        "product_category",
        "application",
        "material",
        "size_spec",
        "quantity",
        "packaging",
        "destination",
        "required_by",
        "target_price",
        "current_supply_issue",
        "certification_required",
    }
)


def _conversations_models() -> object:
    try:
        return importlib.import_module("domains.conversations.models")
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED：domains.conversations.models 尚未创建（{exc.name}）")


def _reply_categories() -> tuple[str, ...]:
    return tuple(item.value for item in _conversations_models().ReplyCategory)


def _category_dirs() -> list[Path]:
    if not REPLIES_DIR.is_dir():
        return []
    return sorted(p for p in REPLIES_DIR.iterdir() if p.is_dir())


def _case_dirs() -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    for category_dir in _category_dirs():
        for case_dir in sorted(p for p in category_dir.iterdir() if p.is_dir()):
            result.append((category_dir.name, case_dir))
    return result


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        pytest.fail(f"JSON 解析失败：{path.relative_to(REPO)}（{exc}）")
    assert isinstance(payload, dict), f"JSON 顶层必须是对象：{path.relative_to(REPO)}"
    return payload


def _sample_pairs() -> list[tuple[str, Path, dict[str, object], dict[str, object]]]:
    pairs = []
    for category, case_dir in _case_dirs():
        input_path = case_dir / "input.json"
        expected_path = case_dir / "expected.json"
        assert input_path.is_file(), f"缺少 input.json：{case_dir.relative_to(REPO)}"
        assert expected_path.is_file(), f"缺少 expected.json：{case_dir.relative_to(REPO)}"
        pairs.append(
            (category, case_dir, _read_json(input_path), _read_json(expected_path))
        )
    return pairs


def test_every_reply_category_has_minimum_samples() -> None:
    """14 类每类至少 10 个样本；退订/自动回复至少 20 个（HANDBOOK 切片 6）。"""
    categories = _reply_categories()
    assert len(categories) == 14, "ReplyCategory 必须是设计稿第十二节的 14 类"
    counts = {category: 0 for category in categories}
    for category, _case_dir in _case_dirs():
        assert category in counts, f"未知类别目录：{category}"
        counts[category] += 1
    for category, count in counts.items():
        assert count >= MIN_SAMPLES_PER_CATEGORY, (
            f"类别 {category} 样本不足：{count} < {MIN_SAMPLES_PER_CATEGORY}"
        )
    assert counts["unsubscribe"] >= MIN_SAMPLES_UNSUBSCRIBE
    assert counts["auto_reply"] >= MIN_SAMPLES_AUTO_REPLY


def test_expected_category_matches_directory_and_enum() -> None:
    """expected.category 必须是 14 类枚举值且与所在目录一致。"""
    categories = set(_reply_categories())
    for category, case_dir, _input, expected in _sample_pairs():
        raw = expected.get("category")
        assert isinstance(raw, str), f"{case_dir.name}: category 缺失"
        assert raw in categories, f"{case_dir.name}: 未知类别 {raw}"
        assert raw == category, (
            f"{case_dir.name}: expected.category={raw} 与目录 {category} 不一致"
        )


def test_case_names_unique_and_well_formed() -> None:
    seen: set[str] = set()
    for category, case_dir in _case_dirs():
        name = case_dir.name
        assert _CASE_NAME.fullmatch(name) is not None, f"用例名非法：{name}"
        key = f"{category}/{name}"
        assert key not in seen, f"用例路径重复：{key}"
        seen.add(key)


def test_input_is_english_customer_content() -> None:
    """客户消息原文全英文、非空（客户内容英文规则）。"""
    for _category, case_dir, input_payload, _expected in _sample_pairs():
        message_id = input_payload.get("message_id")
        subject = input_payload.get("subject")
        body = input_payload.get("body")
        assert isinstance(message_id, str) and message_id, f"{case_dir.name}: message_id 缺失"
        assert isinstance(subject, str) and subject.strip(), f"{case_dir.name}: subject 缺失"
        assert isinstance(body, str) and body.strip(), f"{case_dir.name}: body 缺失"
        assert _CJK.search(subject) is None, f"{case_dir.name}: subject 含中文"
        assert _CJK.search(body) is None, f"{case_dir.name}: body 含中文"


def test_message_ids_are_unique_and_match_case() -> None:
    """message_id 必须按 msg_evals_<类别>_<用例名> 唯一生成（防脚本写串文件）。"""
    seen: set[str] = set()
    for category, case_dir, input_payload, _expected in _sample_pairs():
        message_id = input_payload.get("message_id")
        expected_id = f"msg_evals_{category}_{case_dir.name}"
        assert message_id == expected_id, (
            f"{case_dir.name}: message_id {message_id!r} 与用例位置不一致（应为 {expected_id}）"
        )
        assert message_id not in seen, f"message_id 重复：{message_id}"
        seen.add(message_id)


def test_must_intercept_is_subset_of_deterministic_actions() -> None:
    """必须拦截的动作必须是该类别 REPLY_ACTIONS 的子集（确定性动作映射）。"""
    actions_by_category = _conversations_models().REPLY_ACTIONS
    for category, case_dir, _input, expected in _sample_pairs():
        raw = expected.get("must_intercept")
        assert isinstance(raw, list), f"{case_dir.name}: must_intercept 必须是列表"
        allowed = set(actions_by_category[category])
        assert set(raw) <= allowed, (
            f"{case_dir.name}: must_intercept {raw} 超出 {category} 的动作 {allowed}"
        )


def test_must_not_intercept_never_overlaps_category_actions() -> None:
    """绝不拦截的动作与该类别动作映射不相交（如 auto_reply 不得 stop_sequence）。"""
    actions_by_category = _conversations_models().REPLY_ACTIONS
    for category, case_dir, _input, expected in _sample_pairs():
        raw = expected.get("must_not_intercept", [])
        assert isinstance(raw, list), f"{case_dir.name}: must_not_intercept 必须是列表"
        allowed = set(actions_by_category[category])
        assert not (set(raw) & allowed), (
            f"{case_dir.name}: must_not_intercept {raw} 与 {category} 动作冲突"
        )


def test_critical_categories_lock_handbook_acceptance() -> None:
    """退订零漏判、自动回复零误停序列、投诉必抑制必通知（HANDBOOK 验收）。"""
    for category, case_dir, _input, expected in _sample_pairs():
        intercepted = set(expected.get("must_intercept", []))
        not_intercepted = set(expected.get("must_not_intercept", []))
        if category == "unsubscribe":
            assert "suppress" in intercepted, (
                f"{case_dir.name}: 退订样本必须要求抑制（零漏判）"
            )
        if category == "auto_reply":
            assert "stop_sequence" in not_intercepted, (
                f"{case_dir.name}: 自动回复样本必须明确不停序列（零误停）"
            )
        if category == "complaint":
            assert {"suppress", "record_complaint"} <= intercepted, (
                f"{case_dir.name}: 投诉样本必须要求抑制并记录信誉"
            )


def test_unsubscribe_suppress_scope_is_contact_or_account() -> None:
    """退订样本的抑制范围必须是 contact 或 account（公司级退订不得降级）。"""
    for category, case_dir, _input, expected in _sample_pairs():
        if category != "unsubscribe":
            continue
        scope = expected.get("suppress_scope")
        assert scope in {"contact", "account"}, (
            f"{case_dir.name}: suppress_scope 必须是 contact/account（实际 {scope!r}）"
        )


def test_extract_fields_use_demand_vocabulary_with_verbatim_quotes() -> None:
    """提取字段用 demand 词表；quote 必须逐字指向原消息（provenance）。"""
    for _category, case_dir, input_payload, expected in _sample_pairs():
        extract = expected.get("extract", [])
        assert isinstance(extract, list), f"{case_dir.name}: extract 必须是列表"
        for item in extract:
            assert isinstance(item, dict), f"{case_dir.name}: extract 条目必须是对象"
            field = item.get("field")
            value = item.get("value")
            quote = item.get("quote")
            assert isinstance(field, str) and field in NEED_FIELD_NAMES, (
                f"{case_dir.name}: 未知需求字段 {field!r}"
            )
            assert isinstance(value, str) and value, f"{case_dir.name}: 提取值缺失"
            assert isinstance(quote, str) and quote, f"{case_dir.name}: quote 缺失"
            haystacks = [
                str(input_payload.get("subject", "")),
                str(input_payload.get("body", "")),
            ]
            assert any(quote in haystack for haystack in haystacks), (
                f"{case_dir.name}: quote {quote!r} 不在输入消息中（provenance 断裂）"
            )
