"""寻源准入的纯排序规则。

这里刻意不读取 Need、Directive 或 Workflow。排序输入已经由跨域编排层核验并
冻结为 ``SourcingPrioritySnapshot``，本模块只执行可复现的 v1 排序与解释。
"""

from __future__ import annotations

from datetime import datetime

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


def priority_explanation(snapshot: SourcingPrioritySnapshot) -> str:
    """返回固定中文解释，不使用模型或隐含权重。"""

    if snapshot.cluster_id is None:
        return "该需求尚未归入多成员需求簇；按等待时间排序。"
    return f"该需求簇当前有 {snapshot.cluster_member_count} 条已验证需求；同规模需求按等待时间排序。"


__all__ = (
    "ADMISSION_RANKING_VERSION",
    "AdmissionBlockedReason",
    "AdmissionState",
    "SourcingAdmission",
    "SourcingPrioritySnapshot",
    "priority_explanation",
    "priority_sort_key",
)
