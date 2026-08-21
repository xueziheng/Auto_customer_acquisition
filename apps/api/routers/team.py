"""Team & Territory。

GET  /team/employees
GET  /team/territory             分配矩阵
POST /team/territory             新增规则（走审批）
GET  /team/progress              员工进展（可验证事实：做了什么、
                                 客户反应、商机推进、承诺完成——
                                 明确不做 AI 模糊绩效分）
POST /team/ownership/{account_id}/transfer   转移归属（留原因）
"""

from __future__ import annotations

from .module_status import build_status_router

router = build_status_router(
    module="team",
    mode="manual",
    reason_code="TEAM_QUERY_API_NOT_CONFIGURED",
)
