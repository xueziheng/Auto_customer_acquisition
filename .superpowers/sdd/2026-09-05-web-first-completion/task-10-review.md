# Task10 独立审查

## Spec Compliance

- ❌ Issues found：新增精确成本入口与原成本编辑流程组合后存在回归，见 I1。其余本批新增对象链接、输入失效和具名实施裁定符合规格；不因已经接受的 Mac/Linux 分栏限制否决本批。
- 审查范围：BASE `eec91eaf4c4356c3ff7a8b24f137056e3de5c28a` → HEAD `023db808303ba24f7ef6649ae076fc97fb2e96d9`；源码 `8cc0419c04334681ca6918a2fa9e662872d3dd6b`。按提供的 review package 顺序读取完整差异；首轮输出截断部分已补读。未重新生成 diff、未操作 Git、未重跑已通过套件，唯一写入是本报告。

## Strengths

- `apps/web/src/views/crm/OpportunityDetail.vue:181`、`:190`、`:208` 使用真实 Need/Opportunity ID，并把有 Provenance 改为“关键字段 / 来源记录”，没有把来源存在当成客户确认。
- `apps/web/src/views/costing-quotes/CostingQuotes.vue:90`、`:184`、`:328` 将 query 纳入原请求门，校验成本所属机会，对父页面成本/准备/依据的 401/403 撤销并行读取；`:406` 同步处理 query 变化。没有增加第二套请求门或自动来源确认。
- `apps/web/src/views/costing-quotes/QuoteVersions.vue:124`、`:151`、`:179` 及 `apps/web/src/views/runs/RunCenter.vue:443` 用返回的成本、Run、Approval 引用导航，仍明确审批提交不等于批准或发送。
- `tests/e2e/test_costing_quote_browser.py:224` 起真实验证员工 Run 拒绝、老板进入精确审批、独立 decider 审批以及当前 PDF 下载；`:263` 起保留旧报价深链、两档宽度、精确成本刷新和 Run 审批链接检查。
- `task-10-report.md:56`、`:76`、`:96` 把 Mac 实际回复链但报价 503、独立 Linux 完整报价链、受控 HTTP 前端证据分开；没有把不同 owner/Need 合并，也没有抹掉早轮未知根因及 cleanup_unknown。

## Issues

### Critical

- 无。

### Important

**I1 — 精确成本 query 在每次重读时覆盖用户的新版本选择。**

- 位置：`apps/web/src/views/costing-quotes/CostingQuotes.vue:198`。关联原流程：`:246` 创建成功后选中新 ID 并调用 `loadVersions()`；`:289` 保存成本项后也调用它；`:564` 允许用户显式选择其他成本版本。
- 触发：从报价点击“核对此报价的成本表与需求依据”，进入带 `cost_sheet_id=A` 的工作台；创建成本 B 成功，或从列表明确选择 B 后保存一个成本项。
- 实际：`loadVersions()` 无条件执行 `selectedSheetId = routeSheetId`，将当前选择重新设为 A。创建成功时 UI 同时显示“成本表版本已创建”和旧 A 的详情；报价引用的 A 通常已冻结，于是用户看到旧版禁用操作。手动选择 B 后，每次保存又跳回 A，并触发原选择变化清理 coverage/scope/calculation 的逻辑。新链接因此破坏原连续成本维护行为。
- 修复：区分初次/路由变化的精确选择与当前工作台的显式选择；新建或明确切换版本时同步路由，或在同一作用域的刷新中保留仍在授权列表中的显式选择。不得恢复“精确对象不存在就退首项”。补一条从真实 `cost_sheet_id=A` 入口创建 B、向 B 保存成本项且后续仍显示 B 的回归。
- 定向验证：在 `apps/web` 用 `node` stdin 读取当前 SFC 的原 `loadVersions`/`createSheet` 函数，通过已安装 TypeScript 的 `transpileModule` 原样转译；仅提供确定性 ref、请求门及 POST 201/GET 两成本响应桩，无文件写入、无外部请求。输出为 `created=cost_created, selectedAfterSuccessfulCreate=cost_original, notice=成本表版本已创建`；随后显式选择 created 并重读，输出 `selectedAfterRefresh=cost_original`。这是函数级定向复现，不冒充完整 Vue/HTTP 浏览器验收。
- 现有覆盖缺口：`apps/web/tests/quotation-flow.test.ts:844` 的新增用例只验证初始精确选择；Linux E2E 在创建两版并完成审批后才于 `tests/e2e/test_costing_quote_browser.py:278` 进入精确成本链接，没有再执行创建/编辑，因此原通过证据不能否定此回归。

### Minor

- 无独立新增项。Mac 503、既有 AppleDouble stderr 和合成时钟不作为此代码回归；但交付时仍须保留平台限制与诊断噪声的事实，不能宣称全入口可用或所有输出无错误。

## 具名域外核对与结果

每项只为具体风险检查所需契约，没有泛查其他域。

| 风险 | 定向文件 / 行 | 核对结果 |
| --- | --- | --- |
| query 变化只是取消网络而旧结果仍可应用 | `apps/web/src/views/costing-quotes/quote-request-scope.ts:13` | 原 helper 同步递增 generation，并核对 identity snapshot、channel controller 与 disposed；新增 query 实际参与该门。 |
| 成本编辑与 query 冲突 | `CostingQuotes.vue:209`、`:256`、`:550` | diff 在 create/save 函数中间截断，补读完整函数及成本版本选择区；确认 I1。另补读原 `clearBusiness`，确认撤销会清除父页受限对象。 |
| Quote 内部成本无权后误关闭合法 PDF 入口 | `QuoteVersions.vue:37` 至 `:105`，`apps/api/routers/quotation_actions.py:530`、`:628` | diff 未含完整 load/refresh，补读这些函数。内部报价与文件分别请求，文件继续交给独立 files 服务授权；新增导航没有合并权限。 |
| 新路由名/query 猜错，或 Run 链接放宽角色 | `apps/web/src/router.ts:18`、`:37`、`:89`、`:116`；`ApprovalCenter.vue:155`；`apps/api/routers/runs.py:42`、`:89` | 实际路由契约相符；审批 query 有 watcher；Run API 明确 boss-only，链接本身不授予权限。 |
| 新前端链接把 tenant/actor 当客户端业务参数 | `apps/api/routers/costing_quotes.py:105`；`quotation_actions.py:455`、`:588`；`approvals.py:98`、`:115` | 相关 API 从 RequestIdentity 取 tenant/employee，并调用原域服务；提交真实返回 QuoteApprovalStartResult；决定仍经审批服务，前端只传具名资源 ID。 |
| 旧 HistoricalEligibility 修复退回模糊 inbox 扫描 | `apps/composition_support/outreach_fact_readers.py:136` | 当前 reader 精确核对 tenant/account/contact、EMAIL 和返回事实归属；unknown 抛 TransientError。新测试适配委托此契约，非伪造 InboxActor。 |
| 新失败诊断输出异常消息或 private 原始日志 | `tests/e2e/costing_quote_stack.py:53`；diff 内 `costing_quote_server.py:115` | server 仅格式化仓库 tests 相对 frame；stack 仍经固定 `safe_output`，不输出异常 message/locals。新增用例覆盖 private 文本排除与 module frame。 |
| 独立 Linux 证据由结果 seed 绕过回复/审批 | `tests/integration/costing_quote_case.py:144` | 补读 diff 截断的 initialize_public_case：仅员工等前置 fixture 入库；Campaign 公开服务提交及独立审批、受控历史回执、入站/classification、客户证据门、promote_to_validated、create_from_need 均走原服务。其“公开前置”是确定性测试直接调用公开服务，不能宣传成 Mac Gmail/UI 同链。 |
| Linux 白名单扩为泛目录或改变 A-only | `tests/integration/quote_evidence_linux_support.py:192` 与对应新增构包断言 | 仅在 quotation 分支增加具名五个文件；原 A-only 不包含它们。未改预算、parser/probe 或网络隔离。 |

## 证据核验与 Cannot verify

- 独立读取 `/tmp/task10-browser-evidence/core-qa.json`：8 页，pageerrors 为空，14 条 console error 都是 503；所列 Mac Opportunity/Need 与报告 A 栏一致。读取 main-cleanup 及 linux-final-cleanup：Mac stopped、无遗留记录；9 个具名 Linux owner 的核验均无存活匹配进程/容器/网络/开放端口，早 7 轮 historical_cleanup_verified=false 仍保留。没有自行再操作运行环境。
- 独立读取 Linux 第 8 轮 `result.json` 与 `lifecycle.json`：Need/Opportunity/V1/V2/Run/Approval 与报告 B 栏逐一相符，code=0、cleanup_verified=true、206 个观测进程。当前 PDF 42047 bytes，重新计算 SHA-256 为 `162e6ac3db534a67dbd06fcab03c03b801f8bb6ee72cbca202c2eaa9976b3a0d`，与 result 一致；不由此推断现实供应商报价。
- 实看 `actual-opportunity-390.png`、Linux `exact-cost-390.png`、`run-approval-390.png`：新 Need/成本/审批链接与长 ID 可见，来源标签已修；Run 截图确实存在 fixture 结束时间早于 DB 开始时间，不能用于耗时证明。
- ⚠️ Cannot verify from diff：完整寻源准入/canonical/Catalog queued/stale 安全矩阵和全仓 A1–A10。本批对这些没有源码修改，报告 77 项回归不等于重新完成真实填充寻源链；保持 Task12 集成核验边界。
- ⚠️ Cannot verify：Mac 主入口完整报价能力仍未具备；Linux 成功不能补成“同一个 Mac Need 已完成报价”。第 1/2/6 轮根因依旧未证实，不能由第 8 轮成功倒推。截图与存档 cleanup 状态不等于持续运行监控。
- 原报告 122、77、80、1 是各自的通过组，80 实际包含原 Linux integration；没有相加宣称独立总覆盖，也没有重复执行这些命令。

## Assessment

**Task quality：Needs fixes。**

精确 ID 链接、请求作用域与权限边界总体沿用原契约，证据平台分栏诚实。I1 是新增深链与既有创建/保存动作组合后可确定复现的选择回归，应先修复并补聚焦回归，再作本任务批准。

## Fix round 1 限定复审

### 双结论

- **I1：NOT ADDRESSED（成功路径已修复，错误恢复路径仍可复现同一问题）。** Spec Compliance 仍为 Issues found；Task quality 仍为 Needs fixes。
- 范围：FIX_BASE `023db808303ba24f7ef6649ae076fc97fb2e96d9` → HEAD `40f751c4951fad160839a61f082af5d8f99d5a7e`，最终源码 `54a01265029015e51771af05bf2b5d26492102bb`。顺序完整读取 `review-023db80..40f751c.diff` 和 report 的 Fix round 1 节；不重审旧差异、不重跑已通过的 66 项或旧 Linux/浏览器验证。

### 已修复部分

- `apps/web/src/views/costing-quotes/CostingQuotes.vue:198` 以当前选择优先于 URL 的进入目标，因此原创建 B 后和保存 B 成本项后的成功重读不再强制回 A。
- `apps/web/src/views/costing-quotes/CostingQuotes.vue:200` 在成功返回的列表中找不到 B 时保留 B 的目标意图，并显式清除 coverage/scope/calculation。后续同为 200 的列表重读不会退 A，B 同 hash 回来时旧确认也已清除。
- `apps/web/tests/quotation-flow.test.ts:930`、`:977`、`:1000` 的新增用例挂载真实 Vue/Router/client，分别断言创建和保存的具体请求目标、显式选择后重读、连续缺失及同 hash 回来后不能复用旧确认。响应仍是受控 fetch 桩；报告没有把它冒充真实 API/PG/浏览器验收。

### Important — I1 剩余错误恢复分支

- 位置：`apps/web/src/views/costing-quotes/CostingQuotes.vue:189`、`:210`，与新增目标选择 `:198` 组合。
- 场景：以 `cost_sheet_id=A` 进入，创建或明确选择 B；一次“读取成本版本”返回 503 或发生网络异常，再次在同一页面读取成功。非 200 和 catch 分支仍清空 `selectedSheetId`，下一次读取因没有当前目标而消费 URL 的 A，继续发生 I1 的错误版本跳转。也可发生在已报告 B 缺失后的一次短暂失败，使“后续重读仍核 B”的保证失效。
- 这不是要求失败时保留旧成本数据。失败时应继续清空数据及确认；需要保留的是尚未变更的目标意图。401/403、身份/机会/route 变更的原撤销边界继续保持。建议补“选 B → 503/网络失败 → 同 scope 重读成功仍 B”的参数化回归；可以沿用本轮成功但缺失分支的目标意图与确认清理方法处理非授权故障。
- 新 Critical：无。除 I1 的上述残余外，fixdiff 未发现其他新 Important；无另列 Minor。

### 定向验证与限制

- 具名风险：本轮只在成功列表分支保存选择，错误分支是否会丢失同 scope 意图。diff 的 `loadVersions` 开头被 hunk 截断，因此仅补读完整函数及检索这两个成本测试文件中的失败覆盖。没有域外扩查、Git 操作或测试文件写入。
- 只运行一次当前原 `loadVersions` 的函数级定向复现：在 `apps/web` 通过 `node` stdin 读取当前 SFC，用已安装 TypeScript `transpileModule` 原样转译该函数，注入固定 refs 与依次 503、200（返回 A/B 两对象）的 GET 桩。初始 URL 为 A、当前选择为 B；exit 0，输出 `after503="", error="status_503"`，随后 `afterSameScopeRetry="cost_A", expected="cost_B"`。这是原函数的确定性行为复现，不是完整组件或真实后端验收。
- 报告的最终源码 66 项及 build/type/lint/结构检查属于本修复版本；原 Linux、Mac、80 项 Python 证据仍属于 `8cc0419`。不合并不同 owner/Need，不扩大 Mac 报价能力结论，不倒推混合时钟或早轮失败根因；原 Cannot verify 项继续保留。

## Fix round 2 限定复审

### 双结论

- **I1：ADDRESSED。Spec Compliance：✅ compliant；Task quality：Approved。** 此结论关闭上面两轮记录的 I1，原平台和证据限制继续有效。
- 范围：FIX_BASE `40f751c4951fad160839a61f082af5d8f99d5a7e` → HEAD `a5a59d9d44d208081a53c912b22612ad78d180f9`，源码 `0890ae99dd1fe340247024991d200eecf72efb91`。完整读取 `review-40f751c..a5a59d9.diff`（包含 report Fix round 2），只核 I1 的残余和本次差异，不重新审查旧分支。

### 修复核对

- `apps/web/src/views/costing-quotes/CostingQuotes.vue:189`、`:210` 在非 200 或网络异常时清空成本数据并调用已核验的 `clearCostConfirmation()`，不再删除同 scope 的目标 ID；后续 `:198` 会继续以 B 为目标，并检查它在重新授权读取的当前列表中。由此关闭 Fix1 的 503/网络失败后回 A 的残余。
- 同函数 `:190` 的 401/403 `revokeBusiness()` 分支仍保留；真实身份/机会/route 变化的重置未修改。保留的 ID 仅是选择意图，失败期间 `sheets` 为空且 coverage/scope/calculation 均被清除，没有保留可操作的旧成本事实。
- `apps/web/tests/quotation-flow.test.ts:1000` 的 missing/503/network 参数化测试覆盖完整 Vue 组件：先选择 B、取得并使用费用及 scope 确认、完成受控计算；连续两次失败均无成本详情、旧确认、计算或报价按钮；恢复同 hash 的 A/B 列表后无需再次点选，仍直接显示 B，报价因缺确认而禁用。此次删除恢复后额外点击 B 的动作，使断言直接验证恢复选择，未掩盖回退。

### 问题与证据范围

- 新 Critical：无。新 Important：无。无独立 Minor。
- 未发现需要新增域外核对或运行测试的具体疑点；沿用前轮已核验的 scope、清理和授权契约，没有重复测试、Git 操作、stack 启动或产物改写。
- Report 的 RED 为 2 failed / 1 passed / 64 skipped；最终 68 项及 build/type/lint/结构检查对应 `0890ae9`。前轮 66 项对应 `54a0126`，原 Mac/Linux/Python 证据对应 `8cc0419`，不累计或冒充最新源码的全链重跑。
- **Cannot verify 保持原范围**：本修复未提供新的真实浏览器/Linux 链或全仓 A1–A10 证据；Mac 主入口仍受报价配置/平台限制，不把不同 owner/Need 合并。原混合时钟和早轮未知失败根因仍不能倒推。本任务审查批准不等于完整 Mac 报价能力已交付，也不取消 Task12/13 的核验和说明责任。
