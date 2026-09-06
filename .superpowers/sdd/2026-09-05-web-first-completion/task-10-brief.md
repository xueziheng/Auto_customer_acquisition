### Task 10：寻源、成本、审批、报价跨页面连接

## 执行上下文

本任务承接已独立Approved的Task9最终源码1810acd/report HEAD27098ce；controller给出计划同步后的精确BASE。任务0–9不得重做。唯一生产实施者，禁止派子代理；独立review由controller在最终报告之后安排。先读本brief，再读同目录task-10-review-context.md中的Global Constraints与项目就近AGENTS/HANDBOOK、所需旧验收文档；不要求读取整个总计划。产出正式中文子规格，明确现有真实API/路由和有限补口，再按TDD实现。

Task9现在已有精确Run深链、Sourcing父子同canonical恢复、身份/route generation清理；SendingIdentity的status/bind/retry共用inbound channel。Sourcing公开recovery_action为unavailable/record_reconciliation/resume_reconciliation/event_delivered；最后一种仅事件送达，不是寻源完成。`apps/api/validation_route.py`仅为显式ApiErrorResponse 422路由转换RequestValidationError，其他业务/运行时400含义不变；跨页不得绕过这些恢复语义。

实际浏览器使用原Python Playwright（Browser plugin/skill不可用），1440与390、有填充证据、长ID及主要按钮均需实看；核心domains/Gateway/workflows/outbox/审批/PG真实，外部provider/模型合成。拥有的测试环境自行新建并精确清理；旧Task8/9 owned已清空，不能复用或读取旧private配置。Task8真实填充链的原helper/临时harness使用方案见task-8-report，按需读取精确相关节；只从已有公开前置端口经真实链生成Need/Opportunity，不直接seed结果。若桥接需要本owner配置，凭证仅在原确定性loader/runtime内存，不输出/模型读取。

完整report写同目录task-10-report.md，含源SHA、准确命令/输出/版本、RED→GREEN、浏览器真实与受控边界、截图路径、资源清理及concerns。API变更才独立export/generate；必做覆盖改动的聚焦测试、类型/必要lint/build/结构/显式改动路径敏感扫描/diff-check；不得读取.env/既有DSN或用全仓扫描输出敏感值。全仓A1–A10留12，同版本测试无新疑点不重复。Git AppleDouble stderr仅捕获计数，不修共享.git，不push/merge/deploy/真实发送。提交本地源码并冻结，报告单独提交；短回复status/commits/一行testsummary/concerns。

**Files:**
- 核对/修复：`apps/web/src/views/sourcing/`、`costing-quotes/`、`products/`、`approvals/`、`runs/` 和对应 API 路由。
- 复用：`docs/acceptance/2026-08-28-phase2-costing-quotation.md`、`2026-08-30-phase2-sourcing-case-product-cards.md`、`2026-09-02-phase2-need-cluster-sourcing-admission.md`、`2026-09-04-phase2-catalog-product-proposal.md` 中已有验收入口。

**Interfaces:** 保留现有 Need、Case、Opportunity、Cost Sheet、Quote 与 Approval ID 和状态；不得为了联调新建简化业务对象。

## 控制器具名预检（实施时先核Task8/9最终差异）

- 当前 `OpportunityDetail.vue` 仅以文本显示 `need_id`，未提供进入当前机会成本报价的直接入口；`CostingQuotes.vue` 的 `opportunityId` 起始为空，只读 `quoteId` 路由参数，用户需手工输入机会ID。补精确对象链接和可刷新/直开的输入消费，不把query参数当已授权对象或已验证事实；API读取失败不能回退到列表首项或留下上一个机会的报价。
- `CostingQuotes.vue` 已有 `useQuoteRequestScope`、输入变更失效、scope/evidence/hash/有效期确认关系和原幂等确认组件，应扩展现成scope覆盖新增路由输入，不另建第二套请求门。无证据的供应商价格不能通过跳转自动确认。
- `SourcingCaseDetail.vue` 已有真实Need链接；`ApprovalCenter.vue` 已按 `approval_id` query读取，`RunCenter.vue` 按 `run` query读取。沿用这些实际路由契约，先查后补，不重造近义query或直接字符串拼接猜关联ID。
- Task8会修OpportunityDetail现有“有Provenance就显示已验证事实”的语义错误；本任务从其最终代码继续，不重复实现或把该旧文案带回。

- [ ] 从真实受控回复生成的 Need/Opportunity 开始验证既有链；价格证据走已实现的来源确认端口，不能生成虚构供应商报价。
- [ ] 校验寻源准入、独立审批、indicative/quoted 边界、单位/数量变更、成本冻结和 PDF 当前授权。
- [ ] 修复实际发现的断链与页面缺口；Catalog queued/stale 如实展示，未有下游消费者不显示“培养成功”。
- [ ] 运行受影响子项目的现有聚焦回归，提交。本任务不重写已验收域服务。
