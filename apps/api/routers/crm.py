"""CRM & Opportunities。

GET  /opportunities              机会列表（ABAC；含打分解释）
GET  /opportunities/{id}         详情：需求、供应、成本、承诺、时间线
POST /opportunities/{id}/transition   状态推进（非法转换 409）
POST /opportunities/{id}/mark-lost    必须带 loss_reason
GET  /handoffs                   待接管队列（按等待时长排序）
POST /handoffs/{id}/accept
GET  /analytics/loss-reasons     (reason × died_at_state) 交叉统计
"""

from __future__ import annotations

from fastapi import APIRouter

# S3-12 只提供路由挂载点；业务 endpoint 在 S3-14/S3-15 逐项加入。
router = APIRouter()
