"""Deal Cost & Quote —— Phase 1 人工录入成本与报价。

POST /cost-sheets                          创建（版本类型/数量/币种）
POST /cost-sheets/{id}/items               加成本项（模型建议进待确认区）
POST /cost-sheets/{id}/lock                锁定（INDICATIVE 门禁执行点）
POST /cost-sheets/{id}/accept-indicative-risk   人工风险接受（留痕）
GET  /cost-sheets/{id}/breakdown           确定性计算结果
POST /quotes                               创建报价草稿
POST /quotes/{id}/submit                   提交审批
GET  /quotes/{id}                          含版本历史
"""

from __future__ import annotations

from .module_status import build_status_router

router = build_status_router(
    module="costing_quotes",
    mode="manual",
    reason_code="COSTING_QUOTATION_PERSISTENCE_NOT_CONFIGURED",
)
