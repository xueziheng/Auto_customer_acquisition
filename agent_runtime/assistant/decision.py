"""只允许可验证的四类结果；模型没有确认、发送或报价动作。"""

from __future__ import annotations

import re

from pydantic import TypeAdapter

from agent_runtime.assistant.context import AssistantContext
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from domains.assistant.schemas import (
    AssistantDecision,
    Explanation,
    ReadRequest,
    ResearchDraft,
)
from shared.errors import PermissionDenied, ValidationError

_ADAPTER: TypeAdapter[AssistantDecision] = TypeAdapter(AssistantDecision)


def parse_decision(text: str) -> AssistantDecision:
    if len(text.encode()) > 65536:
        raise ValidationError("模型结果超限")
    return _ADAPTER.validate_json(text)


def validate_decision(
    decision: AssistantDecision, context: AssistantContext
) -> AssistantDecision:
    """引用必须来自本次读集；对业务事实采用原文展示，防止引用存在但断言虚构。"""
    raw = decision.model_dump_json()
    CredentialMarkerGuard().check(subject=None, body=raw)
    if re.search(
        r"(?:概率|置信度|probability|confidence)\s*[:：=]?\s*(?:0\.\d+|\d+\s*%)",
        raw,
        re.IGNORECASE,
    ):
        raise ValidationError("模型不得给出概率")
    if re.search(
        r"https?://|\$\s*\d|(?:USD|CNY|EUR)\s*\d|保证交货|正式报价|承诺库存",
        raw,
        re.IGNORECASE,
    ):
        raise ValidationError("模型结果含未验证链接或商业承诺")
    if (
        isinstance(decision, ResearchDraft)
        and "research_proposal" not in context.capabilities
    ):
        raise PermissionDenied("当前角色不能准备研究提案")
    if isinstance(decision, ReadRequest):
        if "business_read" not in context.capabilities:
            raise PermissionDenied("当前角色不能读取业务资料")
        # 模型只能点选上下文中的对象；新资料只能通过范围查询取得。
        known = {r for f in context.fragments for r in f.dependencies}
        known.update(r for t in context.turns for r in t.object_refs)
        if any(r not in known for r in decision.refs):
            raise ValidationError("模型引用缺少本次来源")
    if isinstance(decision, Explanation):
        visible = {r for f in context.fragments for r in f.dependencies}
        turn_ids = {t.turn_id for t in context.turns}
        for fragment in decision.fragments:
            if (
                not fragment.dependencies
                or not set(fragment.dependencies).issubset(visible)
                or not set(fragment.source_turn_ids).issubset(turn_ids)
            ):
                raise ValidationError("模型解释缺少本次来源")
            sources = [
                f
                for f in context.fragments
                if set(f.dependencies).intersection(fragment.dependencies)
            ]
            if not any(fragment.text in f.text for f in sources):
                raise ValidationError("模型解释不是可核验来源摘录")
        # 所有已读输入均视为派生依赖，不能靠模型漏报来源减小撤权范围。
        deps = tuple(
            sorted(visible, key=lambda r: (r.kind, r.object_id, r.version or ""))
        )
        return decision.model_copy(
            update={
                "fragments": tuple(
                    f.model_copy(
                        update={
                            "dependencies": deps,
                            "source_turn_ids": tuple(t.turn_id for t in context.turns),
                        }
                    )
                    for f in decision.fragments
                )
            }
        )
    return decision
