"""Sourcing Center —— Phase 1 人工寻源操作界面。

GET  /sourcing/queue             待寻源需求（Phase 1 按时间排）
POST /sourcing/cases             开案例（完整度门槛校验）
POST /sourcing/cases/{id}/ladder-check    记录梯子检查
POST /sourcing/cases/{id}/candidates      提交候选（证据快照必填）
POST /sourcing/cases/{id}/complete
POST /sourcing/cases/{id}/fail            必须带原因
"""

from __future__ import annotations

from .module_status import build_status_router

router = build_status_router(
    module="sourcing",
    mode="manual",
    reason_code="SOURCING_PERSISTENCE_NOT_CONFIGURED",
)
