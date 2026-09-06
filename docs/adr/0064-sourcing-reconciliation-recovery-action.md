# ADR 0064：寻源核对事实保存后的安全恢复动作

- 状态：已接受（Task 9 controller 2026-09-06 裁定）

## 背景

`SourcingCaseApplication.reconcile_uncertain` 依次保存 canonical 核对事实、收紧原额度预留、投递精确恢复事件；后两步可能失败。原列表 `can_current_user_reconcile` 只允许无核对事实的执行，不能表达事实已保存后合法的同命令恢复。HTTP Idempotency-Key 只在路由校验存在，耐久去重依据仍是原 execution 的 canonical reconciliation。

## 决定

现有 `SourcingUncertainExecutionReadView` 增加固定 `recovery_action`：`unavailable`（默认）、`record_reconciliation`、`resume_reconciliation`、`event_delivered`。保持原 boolean 的首次核对语义，不将其 false 在前端翻为 true。无需新表、迁移或新命令。

应用层经原域授权读取，核对 tenant/case/run/execution 的绑定及原 quota 的 tenant/run/request/status。首次核对仅原 active run/public_search 且 quota uncertain；恢复仅当前 boss 与 canonical.reconciled_by 相同、canonical.execution_id 精确、confirmed_consumed、quota uncertain/consumed，且仍为精确 active run/public_search。已投递的原 reconciliation_id/execution_id 事件指纹只返回 event_delivered，不代表业务完成。任何读取失败、绑定不一致或未知值均 unavailable。投影不替代 POST 中已有域、额度、Run 与原命令一致性重验。

Web 未知请求冻结 reconciliation_id、run/request、reason、artifact、原 HTTP key；先读精确投影。resume 只在原不可变命令与 canonical 所有公开事实、当前 actor 一致时使用原命令；冲突、不可见或权限拒绝不提交。已投递只刷新 Case/Run。身份/对象变化撤销本页意图和在途响应。

## 验证与代价

真实 PostgreSQL 原作用组覆盖 canonical 提交后 ack 失败、ack 后 event 失败、事件已投递及同命令重放；错 actor、旧 run、读失败不开放。API DTO 从 OpenAPI 独立导出生成。投影增加每项安全状态读取成本，沿原 limit 50 有界；不扩成通用命令账本或全站恢复框架。

## HTTP key 兼容裁定

Task 9 新操作使用 `sourcing-reconcile-${reconciliation_id}`，首次/同页/刷新后相同。历史随机HTTP header未持久化且原路由已丢弃，不是权威；不建立浏览器账本或猜回其值。legacy只在后端明确resume、当前同actor且canonical字段完整时，从既有reconciliation_id派生稳定header，续交付同一记录；原reconciliation_id、payload、reconciled_by不可变化，原header校验仍适用。耐久幂等继续由原域、quota与engine保证。
