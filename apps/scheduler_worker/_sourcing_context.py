"""Sourcing Case V2 Workflow 的最小、确定性上下文构造。"""

from __future__ import annotations

import unicodedata

from domains.sourcing.schemas import SourcingNeedSnapshot
from shared.errors import ValidationError


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _safe_context(
    snapshot: SourcingNeedSnapshot, case_id: str
) -> dict[str, object]:
    """只从可信 Need snapshot 生成既有 V2 context，禁止补造业务事实。"""

    if not isinstance(snapshot.product_category.value, str):
        raise ValidationError("可信寻源需求品类必须是文本")
    category = _normalize(snapshot.product_category.value)
    if not category:
        raise ValidationError("可信寻源需求品类不能为空")
    keywords = sorted(
        {
            normalized
            for fact in (snapshot.application, snapshot.material, snapshot.size_spec)
            if fact is not None and isinstance(fact.value, str)
            if (normalized := _normalize(fact.value))
        }
    )
    return {
        "case_id": case_id,
        "need_id": str(snapshot.need_id),
        "need_snapshot_hash": snapshot.snapshot_hash,
        "product_category": category,
        "keywords": keywords,
    }


__all__ = ()
