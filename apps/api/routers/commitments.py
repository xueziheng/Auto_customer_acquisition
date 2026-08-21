"""Commitment Center。

GET  /commitments                按人/状态/到期过滤
POST /commitments/{id}/confirm   确认提取结果（可修正到期时间）
POST /commitments/{id}/fulfill
GET  /commitments/overdue        逾期看板（老板问"谁的承诺明天到期"）
"""

from __future__ import annotations

from .module_status import build_status_router

router = build_status_router(
    module="commitments",
    mode="manual",
    reason_code="COMMITMENT_PERSISTENCE_NOT_CONFIGURED",
)
