# Task 11 独立审查

## Spec Compliance

- ✅ **Spec compliant**。本次仅审查 BASE `940db7c86249aaaa85c3b18df710a804ffd1c398` → HEAD `a3390b5144c32fa72fd1e7f2a9c8ed882343ef43`（源码 `4be8753e5b46442244e9dfe110bd4c308a367c25`），未发现本 Task 缺失、额外扩展或误解的阻断问题。`apps/api/routers/runs.py:86`、`infra/db/web_core_observability.py:33`、`apps/web/src/views/runs/RunCenter.vue:156`、`apps/web/src/views/crm/HandoffQueue.vue:550`、`tests/integration/test_web_core_observability.py:1`、`docs/operations/web-core-metrics.md:1`覆盖 brief 具名交付路径。
- ✅ `workflows/engine/audit.py:255`先执行服务授权，再校验成对、有时区、最多31天且不晚于观测时刻的窗口；`infra/db/web_core_observability.py:98`从 canonical ID 去重，所有聚合来源有 tenant 限制，各阶段使用各自时间字段。当前接管另有 `tenant_current` 口径（同文件:109），不依赖最近50条 Run。
- ✅ `workflows/engine/observability.py:37`与:74保留实际 usage、人工工时、费率、合格机会资格未知及缺项；金额沿用 Money，没有把零记录、等待、cost_class 或预留额度改造成已知费用。`docs/operations/web-core-metrics.md:24`明确工具尝试与预留状态是窗口内创建记录的当前状态，不能解释成历史期间实际消耗。

## Strengths

- `infra/db/web_core_observability.py:98`采用单条 SQL 的独立聚合，避免多 Run 投影膨胀实体数量；同文件:83以 Opportunity ID 去重多次接管，:149把调用、尝试和 duplicate 回执分列，:192把 Tavily 状态单独投影。真实 PG/Gateway 测试覆盖这些语义（`tests/integration/test_web_core_observability.py:91`、:217、:238）。
- `infra/db/run_audit.py:364`按持久化 workflow 类型、subject 和同租户实体关系核验引用；sourcing 额外匹配 workflow_version 与 Opportunity.need_id（:383）。没有从 context 或 ID 前缀猜对象。:401将反序时钟标为未知；前端明确记录跨度与工时不同。
- `apps/web/src/views/runs/RunCenter.vue:77`将授权拒绝后的列表、详情、观测清理集中处理；:156使用独立请求通道，读取失败不伪装为空或零。`apps/web/tests/run-center.test.ts:492`、:507覆盖权限失败和旧200/503响应迟到。
- `apps/web/src/components/WebCoreObservationPanel.vue:44`明确非 cohort、当前状态、受控数据及未知资格；`apps/web/src/api/api.d.ts:1849`等新增类型来自 OpenAPI，前端未新增重复业务 DTO。现有1440窗口截图与390失败 Run 截图实看内容可读、长引用换行合理。

## Issues

### Critical（Must Fix）

- 无。

### Important（Should Fix）

- 无。

### Minor（Nice to Have）

- `tests/integration/test_web_core_observability.py:153`：新增持久绑定测试覆盖 human_handoff 和 unrelated Run，但未覆盖新增的 sourcing 分支（`infra/db/run_audit.py:383`）。当前实现谓词审查未发现错误；建议以后修改该查询时加入 sourcing 成功绑定、workflow_version 不一致、Opportunity.need_id 不一致的聚焦用例，防止精确关联条件回归。此项是覆盖改进，不代表已发现生产缺陷。

## Cannot verify 与审查边界

- ⚠️ `docs/design/2026-09-06-web-core-observability.md:35`沿用的 Need/Handoff 目标页面与域服务授权属于未改代码。本次核对了请求身份解析，但未扩大为所有目标对象授权的全分支复审；Task12应保留目标页当前身份变更/拒绝覆盖。受控 boss 导航成功不能证明所有角色的对象权限。
- ⚠️ `task-11-report.md:26`后的测试、build、lint、类型、OpenAPI、边界扫描命令结果是实施报告证据；未重跑同版本作用组，也未独立复现整套 runtime。代码和新增断言已审查；全仓门禁仍属Task12。
- ⚠️ `task-11-report.md:101`的原launcher `reply-model.sqlite`生命周期缺口是 controller 已具名接受并安排Task12的旧问题，本 Task 未修复。已读取最终安全清理JSON，其记录 owner停止、进程/容器/端口与私有文件无残留；这不是对launcher自动清理能力的通过判定。共享.git AppleDouble噪声保留报告中的局部计数，未维修或发起新的git命令。
- ⚠️ `workflows/engine/observability.py:37`、:74中的真实费用、实际 token/工时与五项资格仍无可信来源，因此未知是正确输出。合成场景不证明真实获客成绩、生产费用、多人认证或另一Linux报价owner的完整链；没有据此提出补造数字或账本。

## 检查记录

- 已读根及适用 apps/api/web、workflows、infra、tests AGENTS、Task reviewer模板、brief、review-context、正式子规格和报告；完整提供的diff按顺序分块审查。初次合并输出截断了正式子规格，已定向补读；`get_run`与授权类的diff上下文在函数中途结束，按需补读了完整查询字段和授权判断。未重生成diff、未改源码/index/HEAD、未派子代理；仅写本报告。
- 具名风险「sourcing持久主体/版本是否能证明关联」：定向核对 `infra/db/tables.py:4829`、`workflows/sourcing_case/application.py:265`及就近规则；Case有tenant/need/opportunity关系，真实start使用case_id作subject，查询没有用自由文本推断。Case本身没有run_id列，不能要求它提供不存在的绑定字段。
- 具名风险「observation通道是否支持刷新与身份失效」：核对 `apps/web/src/views/costing-quotes/quote-request-scope.ts:13`；通道键隔离、旧controller替换、generation、identity snapshot与unmount失效均生效，新增通道与现有机制兼容。
- 具名风险「身份是否来自当前员工而非客户端role」：核对 `apps/api/identity.py:133`；tenant取服务配置，employee service重新读取员工并验证，客户端不提供role。服务自身仍按既有Phase1RunAuditAuthorizer契约消费这一身份。
- 具名风险「预留记录数是否可冒称多credit费用」：定向核对 `infra/db/search_quota.py:171`；当前一次成功预留增加一个reservation，观测仅投影本地状态数量且不输出金额；未读取配置或凭证。
- 已读取 `/tmp/task11-browser-evidence/browser-qa.json`、`main-cleanup.json`；实看 `run-window-1440.png`及`failed-run-binding-390.png`。未重新跑浏览器、测试或资源生命周期。

## Assessment

**Task quality: Approved。**

实现范围克制，canonical计数、窗口口径、未知费用与安全绑定相互一致，新增异步失效处理有明确回归断言。未发现需阻断Task11的具体缺陷；一个可选测试覆盖改进与Task12既有清理/全量门禁事项如上保留。
