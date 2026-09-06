# Task 10 实施报告

状态：**DONE_WITH_CONCERNS**。待 controller 独立审查；未自行 review、未派子代理。

- BASE：`eec91eaf4c4356c3ff7a8b24f137056e3de5c28a`
- 源码冻结提交：`8cc0419c04334681ca6918a2fa9e662872d3dd6b`，17 文件（含正式子规格）。本文单独提交。
- 工作目录：`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion`；分支 `codex/web-core-completion`。
- 未 push、merge、deploy、真实对外发送；未读取旧 private 配置或 `.env`；未改变 domains、审批、parser、probe、预算或 launcher 模式。

## 实现结果与边界

1. OpportunityDetail 使用 API 具名 `need_id` 链接到需求详情，使用当前 `opportunity_id` 链接成本工作台。来源区域从“已验证事实”改为“来源记录 / 关键字段”，保留原 ProvenancePopover 等级、原文、确认者和时间。基线实际仍有错误文案，Task8 修 Handoff 不能推成这里已修，已获 controller 具名裁定。
2. CostingQuotes 消费单一 `opportunity_id` 和可选 `cost_sheet_id` query，直开与刷新可读；query 只是输入，仍经当前 API 授权。成本列表及 quote-context 核对同机会，指定成本不在列表时拒绝，不退首项。重复/无效/孤立成本 query 拒绝。新增 query 纳入原 request scope 和写入确认；路由/身份变化清旧状态，401/403 撤销组合读取、并行迟到结果不能复活。已有手输机会→报价详情保持可用，同对象刷新仍保留原 hash 失效检查。
3. QuoteVersions 从真实返回的 opportunity/cost_sheet ID 链回精确成本；提交仅记录返回 run_id，仍明确等待审批、不表示已发送。RunCenter 从已返回 approval_id 链到现有审批 query。
4. 当前 Run 是 boss-only：员工从提交结果实际点击 Run 后保持403；老板读取同一 Run 后可点精确审批。未开放员工审计权限。
5. Sourcing→Need、Product→Case、Catalog→Approval 已有链接继续复用，未机械触碰后端。ApprovalView 无具名 quote_id，不能从 affected_entities/proposed_change_display 猜反向路由。无 API/schema 变化，故没有 export/generate。
6. 真实390页面发现 OpportunityList 全局 body min-width 1080 导致当前页及后续成本页裁切。移除此全局下限，只收紧机会看板的移动布局、Detail长链接换行；成本长ID与标题移动端分行。未重做全站。

测试支撑兼容修复均经 controller 裁定：quotation=True tar 精确增加当前装配依赖的五个 composition_support 文件（email_inbound、campaign_approval_reader、delivery_material_reader、employee_readers、outreach_fact_readers），A-only 白名单不变；旧 HistoricalEligibility 委托现成 CurrentReplyStatusReader，保留固定 NOW、tenant/contact/account 与 unknown 拒绝，不假造 InboxActor。准备失败定位只输出 tests 相对文件/正整数行号/固定函数标签，经过原 safe_output；不输出异常正文、locals、配置、请求体或 raw logs。

## 环境与准确验证命令

宿主 Node v24.15.0、Python3.12.14、Vitest4.1.10、Vite8.2.1、pytest9.1.1、Playwright1.62.0、pypdf6.16.2、ruff0.16.6。Browser plugin 不可用，按 brief 使用原 Python Playwright。所有 Python 测试采用 `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1`，不加载旧 DSN。以下命令 cwd 均为工作目录；ESLint 的 cwd 为 `apps/web`。

| 最终命令 | 结果 |
| --- | --- |
| `npm --prefix apps/web test -- tests/quotation-flow.test.ts tests/costing-quotes.test.ts tests/opportunity-list.test.ts tests/run-center.test.ts` | exit0，4文件122 passed，2.70s |
| `npm --prefix apps/web test -- tests/sourcing-center.test.ts tests/sourcing-case-detail.test.ts tests/product-supply-center.test.ts tests/catalog-product-proposal.test.ts` | exit0，4文件77 passed |
| `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/unit/test_costing_quote_transport.py tests/unit/test_quotation_linux_support.py tests/integration/test_costing_quote_closed_loop.py -k 'not real_costing_quote_closed_loop' -q --tb=short` | exit0，80 passed，35.71s；该排除表达式未匹配真实集成测试名称，因此**实际包含原 Linux 完整集成** `test_approved_pdf_does_not_send_or_create_a_won_deal`，不能说仅单元测试 |
| `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 TRADEOS_REQUIRE_E2E=1 .venv/bin/python -m pytest tests/e2e/test_costing_quote_browser.py::test_real_costing_quote_browser -q --tb=short -s` | 最终 exit0，1 passed，62.99s；worker23，forbidden gateway calls0，child_exit/cleanup verified |
| `npm --prefix apps/web run build` | exit0；vue-tsc --noEmit + Vite157 modules，build506ms |
| `./node_modules/.bin/eslint src/views/costing-quotes/CostingQuotes.vue src/views/costing-quotes/QuoteVersions.vue src/views/crm/OpportunityDetail.vue src/views/crm/OpportunityList.vue src/views/runs/RunCenter.vue tests/opportunity-list.test.ts tests/quotation-flow.test.ts tests/run-center.test.ts` | exit0，无输出 |
| `.venv/bin/python -m ruff check tests/e2e/costing_quote_server.py tests/e2e/costing_quote_stack.py tests/e2e/test_costing_quote_browser.py tests/integration/costing_quote_case.py tests/integration/quote_evidence_linux_support.py tests/integration/test_costing_quote_closed_loop.py tests/unit/test_costing_quote_transport.py tests/unit/test_quotation_linux_support.py`（同 Python env 前缀） | exit0，All checks passed |
| `.venv/bin/python scripts/check_boundaries.py`（同 env 前缀） | exit0，七组结构自检通过 |
| `.venv/bin/python scripts/scan_sensitive.py` + `/tmp/task10-source-files.json` 所列17个显式源路径 | exit0；包含spec，不全仓扫描，不输出匹配内容 |
| `git diff --check --` + 同17路径 | exit0 |

上述77项在后续仅成本watch初始化窄修前完成；该窄修已由最终122项覆盖。没有为了累计次数重复通过的 Linux 链。全仓 A1–A10 留 Task12。

## RED → GREEN 与失败历史

- 初始三文件 `-t Task10`：8 failed /111 skipped（exit1）。修实现后8 passed/111 skipped。中间测试首次错误断言未展示的 sheet hash、fake quote-missing 返回数组导致渲染错误，分别改为实际成本ID和404；保留该轮3 failed/5 passed，不把测试错误当产品缺陷。
- 原四文件回归曾6 failed/114 passed：初版读取时清空旧成本/context破坏原同对象hash变更确认，修为同对象刷新保留、作用域变化清除后120 passed。
- 来源文案新增测试先1 failed/37 skipped，改文案后1 passed/37 skipped；四文件121 passed。
- 手输机会跨 quote 路由新增测试先1 failed/61 skipped，`watch(routeScope)` 返回新数组时仍误清空；改用两个 query 的独立 watch source 后1 passed。四文件再测121 passed/1 failed，暴露 immediate 多源watch旧值为 `[]` 导致默认USD被重置；用 previous.length 只在真实变化重置，最终122 passed。未跳过原成本保存用例。
- 安全诊断：新增 server frame 用例先1 failed/41 deselected→42 passed；stack安全日志汇聚先1 failed/42 deselected→43 passed；module frame先1 failed/43 deselected→相关3 passed/41 deselected。为遵循ruff避免测试动态exec，改用固定函数code filename/name，最终全支撑测试覆盖。
- quotation tar 原构包测试扩充5文件断言：RED1 failed/1 passed/22 deselected→精确白名单修复后相关合并68 passed。
- HistoricalEligibility适配实际调用no_reply/replied/unknown：RED3 failed/7 deselected→3 passed；再补错tenant/point拒绝，共5 passed/7 deselected。最终80项含所有这些与原集成。
- 390真实布局：`/tmp/task10-layout-proof.py` 首轮 width390/scroll1080/body1080 exit1；修复后 opportunity与cost均width390/scroll390 exit0。初始截图曾被后续同名GREEN覆盖；另以精确BASE的OpportunityList临时复现同一真实API页面，捕获 `actual-opportunity-390-red-reproduced.png`（390/1080），finally恢复最终文件字节，再跑最终真实浏览器8页。该图是可重复基线复现，不冒充最初截图。受控长costID标题几何从30.92px→126px，截图与最终build通过。

原 Linux 完整浏览器始终保留360s执行/45s清理预算、internal network、真实 Linux parser/runtime及资源probe。每轮失败未提高时限或替换解析器：

| 轮次/owner | 原始结果 | 可证明定位 |
| --- | --- | --- |
| 1 `d027f849c3ab434aabe309c1cee2be9b` | 1 failed，53.34s | startup AssertionError；只有安全摘要，根因未知 |
| 2 `1ed953494aae4272a3a0f8f6cf5cbf0f` | 1 failed，26.15s | startup AssertionError；server诊断未从stack汇聚，根因未知 |
| 3 `e656c4b9be2a4feb9234416818378c60` | 1 failed，5.86s | frame只到server，出现ModuleNotFoundError，module frame被过滤，具体导入尚未知 |
| 4 `b77e8e3803f54b61af179ddb91b0bc44` | 1 failed，27.67s | case:22导入apps.api.runtime，确认quotation tar缺上述5依赖。不能倒推所有早轮同根因 |
| 5 `8986c91064b341eea6c8c62c537d0373` | 1 failed，38.28s | 已过parser，initialize→HistoricalEligibility:136旧list_inbox缺actor TypeError；窄修测试adapter |
| 6 `df735fc8ec1b4365bbd1e5b537a97501` | 1 failed，77.55s | 首次create_sheet_quote请求等待TimeoutError；worker33/forbidden0，无该轮故障截图。未证明与后续watch缺陷同因 |
| 7 `319eee31431345a783e8383d9606df58` | 1 failed，31.31s | 两版报价提交并实际点Run后，员工真实403；新增断言同时匹配两处拒绝文案，改.first。保留failure-product.png |
| 8 `e157fae06cbd40a787bf5ebedeb2ebcd` | **1 passed，62.99s** | 原完整业务步骤及新增精确链接通过，cleanup verified |

早7轮 lifecycle cleanup_verified=false（非协作失败退出）原样保留；后续精确owner核验无残留，不能改写历史为 verified。

## 浏览器证据：三栏严格区分

### A．当前 Mac 原入口与本 owner 真实回复链

Owner `4867dcc1d5ab4866bea2285bf1ff5f7e`，Web `http://127.0.0.1:61329`、API61328。用原 launcher 新建，不复用旧环境。仅确定性原 ControlledConfig loader 在脚本内存装配自身连接，从不向模型/日志输出配置。

原公开发件身份注册/授权/人工确认预热目标15封/日（当日预热容量按原固定曲线，并非已运行15天）/绑定→已发布playbook提案→独立老板审批→原受控发送历史→Gmail入站→分类与四个消费者，实际生成：

- sender `sid_01M1TR25DH6DE6CASBWBY5MQZJ`
- message `msg_01M1TR6GZKDTG1192K2X5T36FN`，conversation `con_01M1TR6GZDA4QDH2VW4NMP5QH5`
- Need `need_01M1TR6J5HQTAY8MJXSFRN976R`
- Opportunity `opp_01M1TR6J6YGANYFW2SQS1W5PHG`
- Handoff `hand_01M1TR6JBCMQ7BCWF1JDW8EZDQ`，notification `not_01M1TR6JKNN3QQ5G43SXPSP9C0`
- 合成模型调用1。外部provider/模型合成，核心PG/domain/Gateway/outbox真实；未seed Need/Opportunity/quote结果。准备脚本一次曾错用email字段KeyError，改为实际address DTO后重走成功，不算业务链失败。

`/tmp/task10-core-browser.py` 最终 exit0，1440×1000及390×844各机会、成本、Need、寻源四页。实际点精确Need→原文、机会→成本，并刷新保持当前opp。成本 GET cost-sheets200空；quote-context/evidence/quotes/customer-quote-versions实际503 dependency_unavailable。寻源显示“自动准入未配置”和空Case，未声称已完成寻源。最终 `core-qa.json` 8页、无pageerror、无overlay、无页面横溢；console14条都是实际503，不能说无控制台错误。

截图根 `/tmp/task10-browser-evidence/`：`actual-opportunity-{1440,390}.png`、`actual-cost-{1440,390}.png`、`actual-need-{1440,390}.png`、`actual-sourcing-{1440,390}.png`，另有身份就绪、预热确认截图。实看390和桌面主要入口、长ID、来源标签；controller也独立实看机会/成本390。

### B．原独立 internal-network Linux 完整报价工作台

这是**另一 owner、另一 Need/Opportunity**，不是Mac入口临时注入quotation配置。成本/报价runtime、parser平台/probe与网络隔离原样；controlled SDK和上传的供应文字是明确测试fixture，`quoted` 分类只经原公开人工来源确认，不能写成现实供应商已经报价。

最终产物根：`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion/output/playwright/t10-e157fae06cbd40a787bf5ebedeb2ebcd/`。

- Need `need_01M1TSJC96VX22095T9FTJB1YV`，Opportunity `opp_01M1TSJC9DKE248NKMYDXA9WGV`
- V1 `quo_01M1TSJPAMPN42GQXZ5200ASEV`，V2 `quo_01M1TSJS0N87RN6S8G4HEVVHFH`
- V2 cost `cost_01M1TSJPTGCGC782ZBZD69PERC`
- Run `run_01M1TSJSN19ANASR2DZEY9JCDR`，Approval `apr_01M1TSJTW1GM58ZKR0H80JDMDG`
- 原公开入站生成需求→价格原文定位/来源确认→客户单位确认→成本22项适用清单→scope确认→创建两版报价→提交→员工Run拒绝→老板精确Run链接点审批→员工审批403→独立decider审批→老板当前授权生成/下载PDF。
- 已实点V2→返回的精确cost，刷新仍同cost；1440/390均检验V1旧深链不误读V2、V2已批准、长ID换行。新增boss Run→Approval通过真实数据点击，无HTTP mock。
- `quote-{1440,390}.png`、`exact-cost-{1440,390}.png`、`run-approval-{1440,390}.png`、`approval-1280.png`。已实看桌面quote和390 quote/cost/run；没有把DOM快照当截图。
- `approved-quote.pdf` 实际1页，42047 bytes，sha256 `162e6ac3db534a67dbd06fcab03c03b801f8bb6ee72cbca202c2eaa9976b3a0d` 与后端file.content_hash一致；原断言客户总价200.00 USD、无内部供应原文。金额来自确定性业务计算，不前端重算。
- 原完整integration另owner `4c1706b0bc9043dabd89508b95a51c0a` 验证PDF批准并不发信、不won、不fulfilled及禁用gateway调用0。该验收的历史发信回执是原测试公开前置，非本次报价发送。

截图Run开始为数据库当前2026/9/6、结束为fixture固定2026/8/29；混合测试时钟不能用于真实任务耗时或指标推导。本批不扩修业务时钟，交Task11/12注意。

### C．独立受控HTTP响应的前端链接证据

`/tmp/task10-links-browser.py` 明确route受控HTTP fixtures，1440/390实际浏览器点Quote→cost和submit→Run→Approval；`links-qa.json` 8页、pageerror0、console0、overlay0、overflow false。截图 `controlled-{quote,exact-cost,run-approval,approval}-{1440,390}.png`。此栏仅验证UI链与边界，不是实际quote/审批/PDF；不得拿合成quoted证据冒充现实报价。当前员工权限的真实结果以B栏403为准。

## 资源清理

- Mac先核status owner与supervisor PID18424/出生时间，才SIGTERM。原supervisor exit0，status stopped、reason requested_stop、cleanup_errors[]。9个记录PID（supervisor及4业务/4anchor）已不存在，原2容器均清，61328/61329/61330/61334关闭；config.json及4种mail.sqlite文件均不存在。证据 `/tmp/task10-browser-evidence/main-pre-cleanup.json`、`main-cleanup.json`。未输出私密文件内容。
- Linux最终browser记录206个观测进程、2进程组，协作退出及cleanup verified；integration也verified。对上表7失败+2成功共9个精确owner，检查原lifecycle记录、owner标签容器/网络、ports：匹配存活进程0、容器0、网络0、开放端口0。`linux-final-cleanup.json` 留存于上述临时证据根；早期unknown不抹掉。
- 测试截图/PDF/安全状态保留未跟踪输出供审查，未提交fixture产物。没有清理别的owner或修共享.git。Git AppleDouble stderr定向捕获，最终status捕获计数4；未保存全程累计计数，不宣称整个任务无该噪声。

## Concerns / 后续范围

1. 当前Mac主入口没有quotation runtime配置，且Linux parser平台要求不能在Mac伪装。B栏证明既有Linux完整工作台可回归，**不证明同一Mac入口可完整报价**。controller已接受DONE_WITH_CONCERNS，Task12同版本核验、Task13明确可运行说明/能力矩阵；本批不加launcher模式、容器通用化或绕probe。
2. 无具名Approval→Quote反向契约，保留精确前向链。Run老板权限不为导航放宽。
3. 当前Mac寻源自动准入未配置；准入/canonical/Catalog queued/stale等本批以既有77项聚焦回归为证，未把空Case截图算完整寻源成功。
4. 原Linux固定时钟截图不提供真实耗时证据；第1/2及第6轮失败没有可证明根因，不用后来成功倒推。
5. 报价的indicative/quoted、数量/单位、来源、冻结、审批及PDF授权仍用原组件/服务。聚焦原用例与完整业务链通过，不宣称本批已重新运行全仓所有安全/业务矩阵。

报告提交前后均只对本文执行显式敏感扫描，exit0；17个源码路径与冻结HEAD的diff exit0，报告diff-check exit0。controller自有4个计划文件修改与未跟踪测试产物未纳入提交。

## Fix round 1/5：I1 精确成本入口与当前选择冲突

- FIX_BASE：`023db808303ba24f7ef6649ae076fc97fb2e96d9`。
- 本轮中间源码提交：`2e8c31c5c1922f8ea90ed440c0411153cf05ff4f`；最终冻结源码：`54a01265029015e51771af05bf2b5d26492102bb`。本节单独报告提交，保留中间历史，不amend源码。
- 仅修改 `apps/web/src/views/costing-quotes/CostingQuotes.vue` 和 `apps/web/tests/quotation-flow.test.ts`。未改其他页、后端、fixture、CSS、API或schema；未派子代理、未自行review。

I1确由完整Vue组件复现：A深链进入后，真实组件通过受控fetch发送POST并收201创建B，但首次重读把详情切回A。明确选B再重读也复现相同错误。本轮测试的“真实组件POST”指挂载原App/Vue/Router并走原client请求；响应仍是单元测试桩，不冒充真实API/浏览器/PG验收。

最终语义：同scope重读先核对当前已创建/明确选择的目标ID；没有当前选择时才消费route中的精确ID。route/机会/身份真实变化仍走原scope重置。当前目标B不在已授权列表时清空成本数据、coverage/scope/calculation确认并明确报缺失，**仅保留B的目标意图**，后续重读仍核B，不静默退原URL的A。B以相同hash重新出现后不会复用旧确认。原同ID内容hash变化、授权拒绝与迟到结果门保留。URL保留进入目标A，本工作台内显式选择B不改URL；完整页面重新进入/刷新仍以精确URL目标重新授权读取。

新增三个行为测试：A（已锁定）深链→POST201新建B→对B POST204保存7.25 USD及来源→自动/手动重读仍B；明确选B→同对象重读→route改missing不退首项；已选择的B有可用scope确认→B消失→再次重读仍missing→B以相同hash回来仍须重新确认。

所有命令cwd同本报告工作目录；版本仍Node24.15.0、Python3.12.14、Vitest4.1.10、Vite8.2.1。

| 命令与阶段 | 准确输出 / exit |
| --- | --- |
| `npm --prefix apps/web test -- tests/quotation-flow.test.ts -t 'Task10 I1'`，生产修复前两项 | 2 failed /62 skipped（64），exit1；收到创建成功提示但item-panel仍A；明确选B重读仍A |
| 同命令，补B消失确认测试，生产未改 | 3 failed /62 skipped（65），exit1；第三项未报B缺失而退A |
| 同命令，中间实现 | 3 passed /62 skipped（65），exit0 |
| `npm --prefix apps/web test -- tests/quotation-flow.test.ts tests/costing-quotes.test.ts`，中间实现 | 2文件66 passed，1.92s，exit0 |
| `npm --prefix apps/web test -- tests/quotation-flow.test.ts -t 'Task10 I1 已选B消失'`，追加连续刷新反例 | 1 failed /64 skipped（65），exit1；第一次B缺失已清确认，下一次刷新却重新显示A，保留该明确失败，不把前述66项当最终证明 |
| `npm --prefix apps/web test -- tests/quotation-flow.test.ts tests/costing-quotes.test.ts`，最终实现 | **2文件66 passed，1.85s，exit0**；包含三个新增完整Vue测试及原授权/hash/请求scope/金额确认相关用例 |
| `npm --prefix apps/web run build`，最终实现 | exit0，vue-tsc --noEmit + Vite157 modules，built461ms（中间480ms亦通过，后续因新增缺失意图修复才重跑） |
| `./node_modules/.bin/eslint src/views/costing-quotes/CostingQuotes.vue tests/quotation-flow.test.ts`，cwd apps/web | 最终exit0，无输出 |
| `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python scripts/check_boundaries.py` | 最终exit0，七组结构自检通过 |
| `.venv/bin/python scripts/scan_sensitive.py apps/web/src/views/costing-quotes/CostingQuotes.vue apps/web/tests/quotation-flow.test.ts` | 最终exit0；仅两个显式源路径 |
| `git diff --check -- apps/web/src/views/costing-quotes/CostingQuotes.vue apps/web/tests/quotation-flow.test.ts` | 最终exit0 |

按controller指定范围，本轮不重启stack、不重跑Linux、不新增浏览器截图；原Linux62.99s、Mac截图、Python80项等均是先前源码8cc0419的证据，不宣称已在54a0126重跑。当前修复版本新增证据严格限上表66项及静态/build验证。Mac报价配置/平台限制、混合时钟和原早轮未知根因仍保留，状态仍DONE_WITH_CONCERNS；same reviewer只复审FIX_BASE到本轮最终源码的fixdiff。没有新资源创建或清理操作。

## Fix round 2/5：I1 非授权读取失败后的意图保留

- FIX_BASE：`40f751c4951fad160839a61f082af5d8f99d5a7e`；最终冻结源码：`0890ae99dd1fe340247024991d200eecf72efb91`。本节报告独立提交。
- Fix1独立复审I1仍为NOT ADDRESSED：此前成功列表及200缺失路径已修，但非200/catch仍清selectedSheetId，503/网络失败后恢复会回到URL的A。本轮完整Vue组件复现了这个残余，不用Fix1的66项通过否定它。
- 仅两文件：`CostingQuotes.vue` 的非200与catch两处，将“清数据并清目标ID”改为“清数据并显式清coverage/scope/calculation”；没有改401/403的revokeBusiness、身份/机会/route重置，也没有改其他页/后端/CSS/API/fixture。目标ID是同scope用户意图，失败期间没有旧成本详情可用；它不是已授权成本事实。成功恢复仍须重新读取列表确认目标归属。401/403继续撤销目标和并行受限读取。
- 原完整组件缺失测试参数化为missing/503/network。先选择B并读取费用与scope确认、完成实际报价计算，随后连续两次失败；断言错误、无成本详情、无报价按钮、无旧scope或计算；恢复返回同hash的A/B后不额外点选，直接断言仍B，报价按钮因确认失效禁用，旧scope/计算均不回来。响应为受控fetch，非真实API/PG/浏览器。

| 命令（cwd仍工作目录） | 本轮准确输出 / exit |
| --- | --- |
| `npm --prefix apps/web test -- tests/quotation-flow.test.ts -t 'Task10 I1 .*失败后'`，源码未改的RED | **2 failed /1 passed /64 skipped（67），exit1**；missing通过，503和network均恢复成A而不是cost_selected。875ms总时长 |
| `npm --prefix apps/web test -- tests/quotation-flow.test.ts tests/costing-quotes.test.ts`，最终源码GREEN | **2文件68 passed，1.90s，exit0**；包含上述三分支与原授权、同ID hash、scope、请求失效和成本保存用例 |
| `npm --prefix apps/web run build` | exit0；vue-tsc --noEmit，Vite157 modules，built459ms |
| `./node_modules/.bin/eslint src/views/costing-quotes/CostingQuotes.vue tests/quotation-flow.test.ts`（cwd apps/web） | exit0，无输出 |
| `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python scripts/check_boundaries.py` | exit0，七组通过 |
| `.venv/bin/python scripts/scan_sensitive.py apps/web/src/views/costing-quotes/CostingQuotes.vue apps/web/tests/quotation-flow.test.ts` | exit0，仅两显式源路径 |
| `git diff --check -- apps/web/src/views/costing-quotes/CostingQuotes.vue apps/web/tests/quotation-flow.test.ts` | exit0 |

版本范围：Node24.15.0、Python3.12.14、Vitest4.1.10、Vite8.2.1未变。本轮68项及build/static对应0890ae9；Fix1的66项对应54a0126，原实际Mac/Linux与Python80项仍属于8cc0419，不混算最新版本覆盖。按限定范围没有stack/Linux重跑、没有新资源或截图、没有代理。Mac主入口配置/平台concerns以及原证据分栏继续保留，状态DONE_WITH_CONCERNS，待同一reviewer限定fixdiff复审。
