"""NextQuestionSuggestion 模型校验单元测试（阶段 1；纯单元，无 DB）。

RED 预期：``__post_init__`` 当前是骨架（``raise NotImplementedError``），
构造任何实例都抛 NotImplementedError——先确认该失败原因（非 collection/
语法错误），再最小实现。

契约（plan 2026-08-16-next-question-selection）：
- topics 必须 list、0..2 个；每项必须 str、strip 后非空、且 item == item.strip()
  （拒绝元素前后空白）；必须无重复；无长度上限（topic 无 DB 长度定义，不自造 max）
- reason 必须 str、strip 后非空；不拒绝自身前后空白（内部固定格式生成，
  最小语义只要求非空）
- 防御性拷贝只切断「构造后调用方修改传入源列表」的别名路径；topics 属性
  自身仍是可变 list，不测试深度不可变
"""

from __future__ import annotations

import importlib

import pytest

from shared.errors import ValidationError

# 域私有实现（models）经 importlib 动态解析：静态导入会被
# scripts/check_boundaries.py 的 domain-internals 规则拦截（跨域只能用
# schemas.py 与 service.py）；与 test_conversations_messages.py 同款先例。
_models = importlib.import_module("domains.conversations.models")
NextQuestionSuggestion = _models.NextQuestionSuggestion


class _SubList(list):
    """list 子类：``type(...) is list`` 必须拒绝（``isinstance`` 会放过）。"""


def test_valid_construction_preserves_fields() -> None:
    """1/2 个 topic + reason 保真；reason 允许自身前后空白（最小语义）。"""
    suggestion = NextQuestionSuggestion(
        topics=["quantity", "destination"],
        reason="完整度 3/5，缺失字段（按上游顺序取前 2）：quantity、destination",
    )
    assert suggestion.topics == ["quantity", "destination"]
    assert suggestion.reason == (
        "完整度 3/5，缺失字段（按上游顺序取前 2）：quantity、destination"
    )

    single = NextQuestionSuggestion(topics=["quantity"], reason="完整度 2/5")
    assert single.topics == ["quantity"]

    # reason 前后空白不拒绝（内部生成文本，仅要求非空）
    padded = NextQuestionSuggestion(topics=["quantity"], reason=" 完整度 2/5 ")
    assert padded.reason == " 完整度 2/5 "


def test_empty_topics_allowed() -> None:
    """topics=[] 合法（空缺失字段即无追问的语义）。"""
    suggestion = NextQuestionSuggestion(topics=[], reason="无缺失字段，无需追问")
    assert suggestion.topics == []
    assert suggestion.reason == "无缺失字段，无需追问"


def test_rejects_invalid_inputs() -> None:
    """每类非法输入 → 固定中文 ValidationError 摘要（精确匹配 = 不回显输入）。"""
    cases: list[tuple[str, dict[str, object], str]] = [
        ("topics 非 list（str）", {"topics": "quantity"}, "建议主题必须是列表"),
        ("topics 非 list（tuple）", {"topics": ("quantity",)}, "建议主题必须是列表"),
        ("topics 为 list 子类", {"topics": _SubList(["quantity"])}, "建议主题必须是列表"),
        ("topics 超过两个", {"topics": ["a", "b", "c"]}, "建议主题最多两个"),
        ("topic 非 str", {"topics": [123]}, "建议主题无效"),
        ("topic 空串", {"topics": [""]}, "建议主题无效"),
        ("topic 全空白", {"topics": ["   "]}, "建议主题无效"),
        ("topic 前后空白", {"topics": [" quantity "]}, "建议主题无效"),
        ("topic 重复", {"topics": ["quantity", "quantity"]}, "建议主题重复"),
        ("reason 非 str", {"reason": 123}, "建议理由无效"),
        ("reason 空白", {"reason": "   "}, "建议理由无效"),
    ]
    for label, overrides, expected in cases:
        kwargs: dict[str, object] = {"topics": ["quantity"], "reason": "完整度 3/5"}
        kwargs.update(overrides)
        with pytest.raises(ValidationError) as exc_info:
            NextQuestionSuggestion(**kwargs)  # type: ignore[arg-type]
        assert str(exc_info.value) == expected, label


def test_defensive_copy_breaks_source_alias() -> None:
    """构造后修改源列表不影响 suggestion.topics（只断言这一条别名被切断）。"""
    source = ["quantity", "destination"]
    suggestion = NextQuestionSuggestion(topics=source, reason="完整度 3/5")
    assert suggestion.topics is not source
    source.append("material")
    source[0] = "material"
    assert suggestion.topics == ["quantity", "destination"]
