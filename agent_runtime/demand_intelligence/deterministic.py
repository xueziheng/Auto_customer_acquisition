"""无模型的保守公开页面分析；仅生成可回指原文的候选研究信号。"""

from __future__ import annotations

import json
import re

_POWER = re.compile(r"(?i)\b(?:solar|electric|battery[ -]?powered)\b")
_VEHICLE = re.compile(
    r"(?i)\b(?:e-?tuks?|tuk[ -]?tuks?|three[ -]?wheel(?:ers?)?|"
    r"tricycl(?:e|es)|e-?rickshaw)\b"
)
_CHANNEL = re.compile(
    r"(?i)\b(?:import(?:er|ing|s)?|distribut(?:or|ion|ing|e)|dealer(?:s|ship)?|"
    r"fleet|operator(?:s)?)\b"
)
_MAKER = re.compile(
    r"(?i)\b(?:we manufacture|we produce|our factory|our manufacturing|"
    r"manufacturer of|manufacturing plant|we build (?:electric|solar))\b"
)
_FUEL = re.compile(r"(?i)\b(?:fuel|petrol|diesel|gasoline)\b")
_USED = re.compile(r"(?i)\b(?:used|second[ -]?hand|pre[ -]?owned|refurbished)\b")
_INSTRUCTION = re.compile(
    r"(?i)\b(?:ignore (?:previous|all) instructions|system prompt|"
    r"assistant:|developer:|you must (?:output|report|say))\b"
)
_BREAK = re.compile(r"(?<=[.!?。！？])\s+|[\r\n]+")


def _excerpt(text: str) -> str | None:
    """仅取同一短句或相邻短句里的车型与渠道事实，不拼接远处片段。"""
    if _MAKER.search(text):
        return None
    boundaries = [0, *(match.end() for match in _BREAK.finditer(text)), len(text)]
    spans = [
        (boundaries[index], boundaries[index + 1])
        for index in range(len(boundaries) - 1)
        if text[boundaries[index] : boundaries[index + 1]].strip()
    ]
    for index, (start, end) in enumerate(spans):
        for candidate_end in (end, spans[index + 1][1] if index + 1 < len(spans) else end):
            candidate = text[start:candidate_end].strip()
            if (
                len(candidate) <= 1000
                and _POWER.search(candidate)
                and _VEHICLE.search(candidate)
                and _CHANNEL.search(candidate)
                and not _FUEL.search(candidate)
                and not _USED.search(candidate)
                and not _INSTRUCTION.search(candidate)
            ):
                return candidate
    return None


class DeterministicResearchModelPort:
    """实现现有模型端口，但只为 research_only 提供有界事实摘录。"""

    async def analyze_pages(
        self, *, system_prompt: str, discovery: dict[str, object]
    ) -> str:
        del system_prompt
        if discovery.get("execution_mode") != "research_only":
            return '{"signals":[],"hypotheses":[]}'
        raw_pages = discovery.get("pages")
        max_signals = discovery.get("max_signals")
        if (
            not isinstance(raw_pages, (tuple, list))
            or type(max_signals) is not int
            or max_signals < 1
        ):
            return '{"signals":[],"hypotheses":[]}'
        signals: list[dict[str, object]] = []
        for index, page in enumerate(raw_pages):
            if len(signals) >= max_signals:
                break
            if not isinstance(page, dict) or not isinstance(page.get("text"), str):
                continue
            excerpt = _excerpt(page["text"])
            if excerpt is None:
                continue
            signals.append(
                {
                    "signal_type": "public_vehicle_channel_activity",
                    "source_page_index": index,
                    "source_excerpt": excerpt,
                    "possible_need": "可能存在目标车型的渠道或车队合作场景，值得验证",
                    "evidence_level": "agent_industry_inference",
                }
            )
        return json.dumps(
            {"signals": signals, "hypotheses": []},
            ensure_ascii=False,
            separators=(",", ":"),
        )
