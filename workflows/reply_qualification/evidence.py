"""生产分类与验收共用的逐字原件证据校验，不持久化客户正文。"""

from agent_runtime.qualification_agent.agent import ReplyClassificationResult
from shared.errors import ValidationError

from .ports import ReplyMessageContent


def require_reply_evidence(
    content: ReplyMessageContent, result: ReplyClassificationResult
) -> None:
    """候选必须来自同一原件的当前证据片段，投影占位符不能作为事实。"""
    if result.rejected_candidates:
        raise ValidationError("回复含不可验证候选，需人工核对")
    reliable = content.evidence_segments if content.projected else (content.body,)
    for candidate in result.candidate_fields:
        if (
            any(
                marker in candidate.quote
                for marker in (
                    "[private reference omitted]",
                    "[current expression boundary]",
                )
            )
            or reliable is None
            or not any(
                source is not None
                and candidate.quote in source
                and (
                    not content.projected
                    or candidate.quote in (content.original_body or "")
                )
                for source in reliable
            )
        ):
            raise ValidationError("回复字段缺少逐字原件证据")
