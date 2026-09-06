### Spec Compliance

- ❌ Issues found：Task3b 的真实事实读取基本符合要求，但 canonical Demand 缺少账户发现必需的公开事实端口，contacts 在实际不可执行时仍标记 enabled；研究与寻源同时启用还会构造两个 DirectiveServiceImpl，未满足本任务的同进程单一服务拓扑。详见 Important 1–2。
- ✅ 公开联系人读取在 SQL 精确绑定 tenant/account/contact，类别只取当前 inferred/contacting、有证据假设且第 201 类明确失败；没有从 Campaign 允许类别反填。证据：`infra/db/repositories/prospecting.py:402`、`infra/db/repositories/need_hypotheses.py:331`、`domains/demand/service_impl.py:283`。
- ✅ 回复采用已写入 ADR 的 account 级保守暂停；单 SQL 聚合当前有效分类，unknown 失败关闭，后来的 AUTO_REPLY 不覆盖另一条真人消息。证据：`infra/db/repositories/conversations.py:574`、`apps/composition_support/outreach_fact_readers.py:139`、`docs/adr/0025-current-outreach-facts-and-runtime-composition.md:8`。
- ✅ 四个共享 reader、API typed 外部 transport 注入及安全能力 DTO/生成类型均有对应改动；未增加 Agent/Browser、正文入站或完整回复的虚假实现。证据：`apps/composition_support/AGENTS.md:10`、`apps/api/composition/runtime.py:1061`、`shared/schemas/runtime_capabilities.py:29`、`apps/web/src/api/api.d.ts:6656`、`apps/scheduler_worker/runtime.py:1822`。
- ⚠️ Task3a 的完整 owned/borrowed 关闭及锁健康结果属于前批能力，本 diff 只显示其局部上下文和 main 的两个字段级改动。控制器应将本报告与已通过的 3a 审查、实现报告第 2 组 161 passed 记录合并判断；不要把本次静态阅读当作重新执行生命周期验收。证据：`apps/api/runtime.py:100`、`apps/scheduler_worker/main.py:126`、`.superpowers/sdd/2026-09-05-web-first-completion/task-3b-report.md:44`。
- ⚠️ Task4 的真实多进程启动/停止、对象资源归属与 disabled 组构造计数，Task5 正文来源、Task6 完整 reply action 的 canonical 绑定仍需各自验收。控制器应核真实消费者执行及独立实例，不能仅沿用本批构造/身份比较测试判全链通过；本批明确 disabled 是正确边界。证据：`tests/integration/test_current_outreach_facts.py:759`、`.superpowers/sdd/2026-09-05-web-first-completion/task-3b-report.md:55`。

### Strengths

- `CurrentCampaignApprovalReader` 使用精确 `campaign:{id}:v{version}` 与审批类型/状态/决定人；缺版本返回无事实，无批准 fixture 回退。`apps/composition_support/campaign_approval_reader.py:26`。
- 发件身份仅读取原 get/check_send_permission，未复制预热或额度算法，真实持久测试证明重复读取不预占额度及耗尽拒绝。`apps/composition_support/outreach_fact_readers.py:114`、`tests/integration/test_current_outreach_facts.py:179`。
- UserId 与 EmployeeId 的转换改为持久映射，HTTP Run 保留真实 UserId，指令确认保持原决定人 EmployeeId；旧同串 fallback 被拒绝。`apps/api/routers/customer_discovery.py:178`、`apps/api/routers/command_center.py:344`、`apps/scheduler_worker/account_discovery.py:104`、`apps/composition_support/employee_readers.py:154`。
- 材料 reader 的地址不进入资格 DTO；原 SuppressionCheck 先取 canonical preflight，EmailSendHandler 比对材料全部五项绑定并核验耐久 attempt，随后原 RateLimitCheck claim/reserve 保持。`apps/composition_support/delivery_material_reader.py:27`、`tool_gateway/checks/suppression.py:44`、`tool_gateway/handlers/email_send.py:143`、`tool_gateway/checks/rate_limit.py:66`。
- 测试使用真实 PostgreSQL 与公开写入口覆盖类别撤销、未知回复、自动回复历史、审批版本、额度与发送前事实；报告如实保留共享容器迁移失败及独立验证范围，没有把早期通过数冒充最终总数。`tests/integration/test_current_outreach_facts.py:88`、`.superpowers/sdd/2026-09-05-web-first-completion/task-3b-report.md:47`。

### Issues

#### Critical (Must Fix)

- 无。

#### Important (Should Fix)

1. **canonical Demand 缺少 account_names，实际账户发现必然失败。** `apps/scheduler_worker/runtime.py:1313` 将 `catalog_products.demand` 作为唯一 core Demand；原工厂 `apps/scheduler_worker/catalog_product_runtime.py:237` 的构造只注入 `catalog_accounts`（`:244`），没有 `account_names`。新增 `apps/scheduler_worker/bootstrap.py:330` 把该服务传给 DemandAccountDiscoveryTaskReader；消费者 `apps/scheduler_worker/account_discovery.py:69` 调 `get_hypothesis_for_discovery`，有效假设加载完后在 `domains/demand/service_impl.py:1118` 读取账户名，并必经 `:1855` 的“需求账户展示名依赖未配置”异常。因此 contacts capability 在 `apps/scheduler_worker/runtime.py:1786` 报 enabled，却不能完成任何真实账户发现。现有 `tests/integration/test_current_outreach_facts.py:759` 仅创建运行时，`:831` 起比较共享实例，未调用该 reader；HTTP 身份测试也仅验证 Run 身份与 resolver。**修复：**在唯一 Demand 构造时接入基于 canonical Prospecting 的既有公开账户名/国家/域名 reader，Catalog 与 contacts/research 共用这一完整实例，不另造 Demand。新增一次真实公开 account/signal/hypothesis → bootstrap 产出的 task_reader.load 验证，并保留 enabled 状态断言。

2. **研究与寻源同时启用会创建两套 DirectiveServiceImpl。** `apps/scheduler_worker/bootstrap.py:356` 为 research 创建指令服务并在 `:383` 绑定 task reader；`apps/scheduler_worker/runtime.py:1563` 随后又为 sourcing 创建另一实例。两者使用不同的员工适配器，虽访问同一数据库，也违背 brief“不能……第二套池或域服务”的明确要求，后续增加权限或领域依赖时容易只接入其中一套，单一 canonical 拓扑目前只在部分域成立。**修复：**把当前指令服务在本进程构造一次，通过具体窄 typed 上下文/依赖传给两个消费者；保持直接 SchedulerDomainDependencies 入口兼容。对 research+sourcing 同时启用的组合核对两个消费者持有同一服务，不扩成可变全系统容器。

#### Minor (Nice to Have)

- `.superpowers/sdd/2026-09-05-web-first-completion/task-3b-report.md:51` 记录 Git AppleDouble stderr 被捕获，说明原始验证输出并非完全干净。这属于环境噪声，不使功能测试失效；控制器应在保留的验收记录中明确标注已知噪声与安全过滤方式，不把“捕获 stderr”等同于底层命令零告警，也无需为此改动 `.git`。

### 审查范围与聚焦检查

- 审查基线 `a3fcd9fa775e9d57848eb5fa49353516c81c0da4`，HEAD `f654fc6711858ce37cc59ca3045c1c5ba14cbdce`；读取指定 diff 包的全部连续区间。首段工具输出截断处仅补读相应区间；没有重新执行 git diff。
- 风险“新材料 reader 信任的上游是否仍取得/维持 canonical 绑定”：聚焦检查 `tool_gateway/handlers/email_send.py`、`tool_gateway/checks/suppression.py`、`tool_gateway/checks/rate_limit.py` 及 `tool_gateway/pipeline.py` 调用顺序定位；未发现本批引入旁路。
- 风险“enabled 组是否仅靠 dataclass 外壳绕过必需端口”：聚焦检查 `agent_runtime/account_discovery/model_port.py`、`agent_runtime/demand_intelligence/model_port.py`、`apps/scheduler_worker/web_discovery.py` 的构造校验，以及 diff 未包含完整定义的 `runtime.py` composition dataclass。已有模型/transport 缺项校验成立；Important 1 是更深层的领域依赖缺口。
- 风险“新 core Demand 从 Catalog 复用后是否满足实际消费者”：聚焦检查 `apps/scheduler_worker/catalog_product_runtime.py` 的唯一 Demand 构造与 `domains/demand/service_impl.py` 的 discovery/account fact 方法，确认 Important 1。该领域文件原 diff 只含另一方法，相关消费者函数不在 diff 上下文中。
- 风险“研究与寻源同时启用是否重复域服务”：核对 diff 两个 DirectiveServiceImpl 构造；只为判断 core 来源补读 `runtime.py` 被截断的 core 前置组装上下文，没有扩大为全仓搜索。联系人 snapshot 的 positional bool 另对照 `domains/outreach/schemas.py`，确认为已由精确 SQL 证明的 contact_belongs_to_account；法律依据 fallback 对照 `domains/prospecting/models.py` 的原始约束，未发现正当利益评估绕过。
- 未运行测试、未重跑已报告 PASS、未访问 `.env`/已有 DSN/凭证、未进行真实外部调用；除本报告外未改工作树、索引或 HEAD。实现报告的测试结果按声明引用，本次确定缺陷以静态调用链为证据。

### Assessment

**Task quality:** Needs fixes

**Reasoning:** 当前事实读取和身份转换的边界清楚，但 canonical 服务“同实例”尚未保证“实例具备实际消费者所需的完整依赖”。先修复上述两个装配问题并补真实消费者验证，再进入 Task3b 定向复审；本结论不代表全分支最终审查。

## Fix1 定向复审

### Finding Verdicts

- **Important 1：唯一 Demand 缺少 account_names，contacts enabled 后实际读取失败 — ADDRESSED。** `apps/scheduler_worker/catalog_product_runtime.py:246` 在原唯一 Demand 构造中注入基于同一 Prospecting 的 `ProspectingDemandAccountNames`，原 Catalog facts 端口仍保留。`apps/composition_support/outreach_fact_readers.py:185` 是原 API 适配器的机械迁移，`apps/api/composition/demand_radar.py:5` 重导出同一个类，没有另造 Demand 或改 API 契约。新增 `tests/integration/test_current_outreach_facts.py:905` 实际调用 bootstrap 产出的 task_reader.load，随后断言企业名、国家、官网、类别、信号引用；`:860` 另核 Catalog driver 与 core Demand 是同一个对象。原缺失依赖及仅凭构造宣称可执行的测试缺口均已修复。
- **Important 2：research+sourcing 重复构造 DirectiveServiceImpl — ADDRESSED。** `apps/scheduler_worker/runtime.py:1326` 统一构造一次 Directive，具体 typed `SchedulerCoreServices.directives` 传递该实例；research 在 `apps/scheduler_worker/bootstrap.py:373` 使用 `core.directives`，sourcing 在 `apps/scheduler_worker/runtime.py:1581` 使用同一实例，原两处重复构造已删除。`:1315` 的适配器选择保留旧直接依赖+sourcing 的原员工端口，bootstrap 使用 canonical scope。`tests/integration/test_current_outreach_facts.py:873` 在 research+sourcing 同时启用时比较两个真实消费者的 Directive identity，覆盖原重复装配条件。

### New Breakage in the Fix Diff

- 无新 Critical、Important 或 Minor。账户事实 reader 的方法与验证语义保持原样；Directive 提前构造只做本进程接线，未引入新池、运行副作用、可变服务字典或 app 进程互导。证据：`apps/composition_support/outreach_fact_readers.py:185`、`apps/scheduler_worker/runtime.py:1326`。

### Out-of-Scope Observations

- 无新增观察。原 Task3a 与 Task4/5/6/12 跨批验证项由控制器继续跟踪，不扩大本轮复审；既有 AppleDouble 噪声已在 `.superpowers/sdd/2026-09-05-web-first-completion/task-3b-report.md:123` 明确区分于零告警，不要求修改 `.git`。

### Checks

- 读取 fix-only 包 `review-f654fc6..da41c45.diff` 的两个连续区间；基线 `f654fc6711858ce37cc59ca3045c1c5ba14cbdce`，HEAD `da41c45d8912d335a076391308858bd3d1e8893d`。未重读原整批 diff、未执行 git 命令、未重跑测试或扩大源码检查。
- 核对追加报告列明两项针对性 RED、最终两项通过、75 项聚焦回归及两项旧 direct 入口通过，并提供本轮静态命令与退出状态；代码中的测试确实执行了原失败消费者并比较真实共享服务。证据：`.superpowers/sdd/2026-09-05-web-first-completion/task-3b-report.md:137`、`:151`、`:159`、`tests/integration/test_current_outreach_facts.py:860`、`:905`。这些测试结果为已记录的实现者执行证据，本复审没有重新执行。

### Verdict

**Fix round:** All findings addressed, no new Critical/Important breakage。

**Task quality（本轮更新）:** Approved。Task3b 两项原 Important 均关闭；该结论仅为本任务定向复审通过，不代表完整 Web 链路或全分支最终通过。
