"""寻源准入排序事实的低层纯函数，避免模型与公开模块循环导入。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol

from shared.errors import ValidationError
from shared.schemas.identifiers import NeedClusterId, ValidatedNeedId


class PriorityExplanationFacts(Protocol):
    """生成固定中文排序说明所需的最小不可变事实。"""

    cluster_id: NeedClusterId | None
    cluster_member_count: int


def _utc_iso8601(value: datetime, field_name: str) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValidationError(f"{field_name} 必须是 UTC 时间")
    return value.astimezone(UTC).isoformat()


def _bounded_id(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(f"{field_name} 无效")
    return value


def priority_facts_payload(
    *,
    need_id: ValidatedNeedId,
    cluster_id: NeedClusterId | None,
    cluster_member_count: int,
    ready_at: datetime,
    facts_observed_at: datetime,
    ranking_version: str,
) -> dict[str, str | int | None]:
    """构造 v1 排序事实的唯一 canonical payload，不接收任何贸易聚合字段。"""

    if isinstance(cluster_member_count, bool) or not isinstance(
        cluster_member_count, int
    ) or cluster_member_count < 1:
        raise ValidationError("cluster_member_count 必须是正整数")
    if cluster_id is None and cluster_member_count != 1:
        raise ValidationError("未归簇需求的 cluster_member_count 必须为 1")
    return {
        "need_id": _bounded_id(need_id, "need_id"),
        "cluster_id": (
            None if cluster_id is None else _bounded_id(cluster_id, "cluster_id")
        ),
        "cluster_member_count": cluster_member_count,
        "ready_at": _utc_iso8601(ready_at, "ready_at"),
        "facts_observed_at": _utc_iso8601(facts_observed_at, "facts_observed_at"),
        "ranking_version": _bounded_id(ranking_version, "ranking_version"),
    }


def canonical_priority_facts_hash(
    *,
    need_id: ValidatedNeedId,
    cluster_id: NeedClusterId | None,
    cluster_member_count: int,
    ready_at: datetime,
    facts_observed_at: datetime,
    ranking_version: str,
) -> str:
    """计算唯一 v1 排序事实的 SHA-256；后续服务必须复用本函数。"""

    payload = priority_facts_payload(
        need_id=need_id,
        cluster_id=cluster_id,
        cluster_member_count=cluster_member_count,
        ready_at=ready_at,
        facts_observed_at=facts_observed_at,
        ranking_version=ranking_version,
    )
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
