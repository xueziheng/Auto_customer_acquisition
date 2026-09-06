### Task 11：Run、接管和成本输入

## 执行约定（精确BASE由controller在Task10通过后提供）

Task10最终源码0890ae9/report HEADa5a59d9已独立Approved。QuoteVersions仅从submit结果显示精确run_id链接，RunCenter仅从后端RunApprovalView.approval_id导航原审批query；Run仍boss-only。OpportunityDetail现有Need和机会成本链接，CostingQuotes接受opportunity_id/cost_sheet_id query；保留本工作台显式目标的同scope意图和所有读取失败清确认规则，不重做Task10。Mac主owner没有完整quotation配置且解析器要求Linux；原完整报价在另一隔离Linux owner验证，不能把这两组Need/Run当同一链，统计与演示须明确scope/来源。

唯一生产实施者，不派子代理；controller另派独立review。先本brief、task-11-review-context.md中的原文Global Constraints、就近AGENTS/HANDBOOK，按真实接口形成正式中文子规格再TDD。不要重读总计划或重做0–10；Task9的Run精确run query、失效generation与错误/未知状态保留，Task10最终具名链接交接以controller消息为准。

完整report为同目录task-11-report.md，列精确命令/实际输出/版本、RED→GREEN、来源口径/未知项、截图路径与owned清理。复杂行为验证使用真实PG/Gateway/域，外部仅合成；现有Python Playwright用于1440与390实际有内容页面并实看，Browser plugin/skill不可用。不读.env/既有DSN或private配置值；自有runtime可用原loader，秘密仅在确定性内存不输出。自建owned资源并精确清理，不复用已清旧owner。不push/merge/deploy/真实发送。

必要API变更独立export/generate，前端不手写重复DTO；聚焦作用组、类型/必要lint/build/结构、显式改动路径敏感扫描/diff-check。全仓门禁属12，不重复同版本已通过测试。Git AppleDouble stderr只捕获计数不修共享.git。源码提交冻结，报告单独提交；仅短回status/commits/testsummary/concerns。缺口具名反馈controller裁定，其他独立工作继续。

## 已有审计事实与口径

- Task10原Linux报价夹具使用固定NOW（2026/8/29），但部分DB开始时间用真实时钟（2026/9/6），截图存在结束早于开始。该合成证据不能用作真实耗时或业绩；任何指标遇时序不一致应明确未知/缺项，不能生成负耗时或用abs掩盖。不要因此在本任务重写整个旧夹具时钟；对指标处理实际异常输入即可。

- 先读 workflows/engine/audit.py 与 infra/db/run_audit.py。现有RunDetail只有steps/tool_calls/artifacts/approvals，研究阶段数来自安全context白名单，Tavily credits来自独立reservation状态计数；工具cost_class不是费用。不要把多Run投影简单求和当唯一实体数或真实成本。
- Typed投影带时间窗口/统计范围/数据完整性；唯一数量用canonical实体ID去重，不能用重试次数或事件次数替代；无可信Run关联时明确只提供tenant/window汇总或unknown，不猜source attribution。
- `GLOSSARY.md:14`明确合格贸易机会必须同时满足可接触客户、真实需求、可供应、可接受利润和执行团队。Opportunity记录数或接管数不能直接标成合格贸易机会数；若供应/利润等当前证据不齐，只展示对应阶段实体数及资格缺项。无可信逐项资格判定时合格数量应未知，不能拿所有Opportunity作北极星指标分母；不得让模型补概率/利润或伪造报价来使指标可算。
- 接管等待时长不等于人工实际工作耗时；ContextBuilder UTF-8预算估算不等于实际模型token usage。现StructuredJsonModelClient只返回文本，不提供计量；没有可信usage/费率/人工耗时来源时显示unknown及缺项，不能默认0或根据cost_class/free推算金额。
- 若增加输入端口，先列最小来源/权限/幂等契约再实现，不能为此造订阅/积分钱包、薪资系统或修改历史账本。新金额依旧Money/Decimal，缺费率不输出总额或“单位机会成本”。
- Run审计当前boss-only，不借新增deep link向员工开放全租户审计；对象链接仍经各对象当前权限。固定失败类别/安全引用，不含context全文、subject/body、地址、locator、hash或凭证。

**Files:**
- 复用：`infra/db/run_audit.py`、机会域接管统计、Gateway 调用记录和各外部预算记录。
- 更新：`apps/api/routers/runs.py`、`apps/web/src/views/runs/RunCenter.vue`、`crm/HandoffQueue.vue`。
- 新增：`tests/integration/test_web_core_observability.py`、`docs/operations/web-core-metrics.md`。

**Interfaces:** 以 tenant + Run/Need/Opportunity 安全 ID 归因；业务金额复用 Money/Decimal，统计结果带时间窗口和数据完整性，不额外做计费钱包。

- [ ] 记录各阶段唯一实体数、停滞步骤、接管深度/等待时长及模型 token/来源调用/人工耗时输入。
- [ ] 写重放不重复计数、跨租户隔离、未知费用不当作零、缺费率不输出金额的测试。
- [ ] 已有来源不足以计算的指标显示未知及缺项；不通过修改历史账本补造成本。
- [ ] 验证 UI 能从失败 Run 定位有权限的对象，日志/通知只含安全分类和引用；提交。

**Exit gate:** 老板能知道任务停在哪里、谁应处理、有哪些已知成本和缺项；不把测试统计当真实获客成绩。

## 8. W6：集中验收与交付
