"""Run Center —— Agent 做了什么。

GET /runs                        Run 列表（按类型/状态/发起人）
GET /runs/{id}                   全景：目标、技能、模型调用、搜索、
                                 证据、变更集、审批、成本、错误
GET /runs/{id}/changes           变更集详情（应用前/后）
GET /tool-calls                  工具调用流水（含被拒的，带拒绝原因）
"""

from __future__ import annotations

from .module_status import build_status_router

router = build_status_router(
    module="runs",
    mode="audit",
    reason_code="TRADE_RUN_READ_MODEL_NOT_CONFIGURED",
)
