"""寻源准入的公开纯领域模型、状态和固定排序规则。"""

from __future__ import annotations

from datetime import datetime

from domains.sourcing._admission_facts import (
    PriorityExplanationFacts,
    canonical_priority_facts_hash,
    priority_facts_payload,
)
from domains.sourcing.models import (
    ADMISSION_RANKING_VERSION,
    AdmissionBlockedReason,
    AdmissionState,
    SourcingAdmission,
    SourcingPrioritySnapshot,
)


def priority_sort_key(snapshot: SourcingPrioritySnapshot) -> tuple[int, datetime, str]:
    """按簇累计已验证 Need 数、等待时间和 Need ID 生成唯一的 v1 排序键。"""

    return (-snapshot.cluster_member_count, snapshot.ready_at, str(snapshot.need_id))


def priority_explanation(snapshot: PriorityExplanationFacts) -> str:
    """返回固定中文解释，不使用模型或隐含权重。"""

    if snapshot.cluster_id is None:
        return "该需求尚未归入多成员需求簇；按等待时间排序。"
    return f"该需求簇当前有 {snapshot.cluster_member_count} 条已验证需求；同规模需求按等待时间排序。"


__all__ = (
    "ADMISSION_RANKING_VERSION",
    "AdmissionBlockedReason",
    "AdmissionState",
    "PriorityExplanationFacts",
    "SourcingAdmission",
    "SourcingPrioritySnapshot",
    "canonical_priority_facts_hash",
    "priority_explanation",
    "priority_facts_payload",
    "priority_sort_key",
)
