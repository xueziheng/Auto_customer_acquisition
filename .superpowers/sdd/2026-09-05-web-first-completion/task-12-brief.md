### Task 12：同版本受控端到端验收

Task8交接更新：源码96c0c01初始验收全Web358后有增量33及build，后端179/1测试断言修复后单项1passed，并非一轮完整全过；本任务仍需最终同版本完整门禁，不能累计这些历史数。Task8 review M1指出全仓lint现为120warnings（此前Task4为172），具体归属仍须本批实际核对，不能未经比较将每条称旧问题。共享.git stderr规则不变。Task8已补真实网页登记/认证/预热/绑定及原Task6生成填充通知→handoff→Need/原件下载/accept204，相关QA脚本/截图在/tmp/task8-browser-evidence及task-8-report末节；本批统一A1–A10仍必做。最终以8修复审查版本为准，不回退旧跨通道403竞态。

敏感扫描预检补充：scripts/scan_sensitive.py原有精确占位符许可（如dummy/placeholder），没有通用测试路径豁免。对Task4报告列出的四处旧unit fixture命中，先核实其是否仅作为CredentialMarkerGuard的合成输入；如等价可改为明确占位符且保留同一敏感marker/相同真实护栏行为，优先这种最小fixture修正。不可通过拆词/拼字符串隐藏真实形态，不新增宽泛路径豁免或放松scanner；若占位符会改变被测语义则保留并提出具体验证/裁定。只报告路径/行/类别，不输出匹配值。四处属于“Task4 base未改”，不据此宣称都在本分支起点之前已存在。

## Task6审查保留的具体Minor

M1（aab51d0的connectors/gmail/inbound_mime.py:233）：HTML引用栈void集合不完整，source/track/area/embed等标准无闭合元素会被压栈，随后blockquote结束被误判未闭合，使合法当前表达整封拒绝。最终整体验收前按实际完整HTML void语义最小修复（必要核权威标准，不泛化HTML重写器），新增引用内媒体元素、结束后真实当前字段仍可用且不跨引用拼接的反例。原最小输入为<p>We need hinges.</p><blockquote><hr><source src="x">old reply</blockquote><p>5000 units.</p>。这是保守误拒，不是已知事实污染；6 Fix1只处理Important Outlook后缀隔离，M1不混入该修复loop。执行时核6最终代码是否自然已覆盖，不重复改；最终whole-branch review必须核此项结论。

M2为同批已披露Git AppleDouble stderr；沿既定规则如实记录，不在本任务维修/重打包共享.git。

## 证据与运行裁定

- 已知前置：Task4全仓scan_sensitive曾报4处其base未改测试fixture形态命中，详见Task4最终report安全路径/规则/行号（禁止回显疑似值）。开始本批先核当时证据与当前代码，区分真泄露和有意测试样例并做最小正确修复/明确裁定，不能忽略非零门禁或静默放宽扫描器。Task4 lint172项样式warning也按当前源码核新增/旧来源，勿把捕获输出称零告警。

- 执行前读正式设计A1–A10，逐行建立“真实入口→持久事实→UI动作→断言→证据文件”的表。仅受控外部responses可替换，核心域/Gateway/Outbox/审批/额度/状态机必须真实。research_only与完整触达是两个独立场景，测试不能把前者结果直接提升为Campaign。
- Task4入口是唯一新增完整链生命周期来源，测试反向消费，不复制旧1100行conftest或直插Message/Need/Opportunity/已审批结果。种子仅身份/职责基础资料；业务提案、审批、验证、来源确认都经真实服务/API。受控供应商证据必须显式标合成，不冒称真实quoted/外部验证。
- 全量与数据库敏感测试串行，不与另一个任务共享容器/数据库；Docker不可用/迁移失败在required E2E必须fail，新增测试skip不能通过门禁。所有pytest显式清除TEST_DATABASE_URL、禁dotenv；连接与生成密钥不得出现traceback/output。Docker/MinIO只操作owner标签+ID一致资源。
- 后端解释器固定本工作树.venv/bin/python3或其实际python；前端gen:api子进程调用python3，显式PATH指向该venv，避免用系统解释器生成漂移。当前私有venv用只读.pth复用旧依赖只是开发便利；A1必须验证声明依赖的新环境安装/启动，不能依赖旧Catalog工作树。不得删除旧env或改共享依赖。
- 证据登记源码commit/工作树修改摘要、实际日期、每条命令、exit/test counts/warnings/skips、截图完整路径、cleanup状态；历史8741/335不能当当前结果，也不能把多次局部测试累计为全量。测试失败修复后更新同版本证据，未改代码且无新疑点不重复全量。
- 真实客户端390px与桌面截图必须实际查看；测overflow/主要操作可达/旧身份内容清除/源证据下载，jsdom不等于浏览器验收。下载PDF检查内容与当前授权；禁止使用已缓存公共下载URL绕过撤权。
- A10零真实外部行为要有受控transport allowlist/未知操作拒绝/provider构造边界的证据；初始化计数器为0不构成证明。公网真实模型、邮箱发送、联系人provider、供应商联系仍not_run，不读真实凭证。
- 发送重放/结果未知的外部证据须计每次实际Provider调用，不只计去重后的邮件行；不能靠受控邮箱自动合并重复send让核心重复执行的测试假绿。明确“外部已发生、响应未知”故障发生位置，验证原Gateway/耐久attempt没有再次发送，保留后续核对语义。
- 本批若发现跨模块缺陷，可修复最小实际缺口并聚焦回归，不降低A1–A10或把未实现标通过；新增业务算法不在此随意发明。最终报告明确受控场景限制，多人部署与桌面实现不作为通过项。

**Files:**
- 新增：`tests/e2e/test_web_core_controlled.py`、`docs/acceptance/2026-09-05-web-core-completion.md`（文件名沿计划日期，正文填写实际验收日期）。
- 复用：既有 pytest、Vitest、PostgreSQL/Browser 生命周期支持；截图使用本次独占目录。

**Interfaces:** 覆盖设计 A1–A10；测试只替换外部端口。受控完整链与 research_only 链分别验证，不能把后者自动提升为 Campaign。

- [ ] 使用 Task 4 入口启动；从浏览器发起任务，按正常 API 生成审批与业务状态。
- [ ] 完成受控发现、单 Provider 验证、已批 Campaign 发送、邮件回复入站、需求验证和员工接管。
- [ ] 完成现有寻源/成本/报价审批/PDF 的适用场景；没有 quoted 证据且没有符合既有规则的人工风险接受记录时，验证正式报价被拦。
- [ ] 注入 worker 重启、数据库短暂不可用、重复消息、旧审批、权限撤销和响应未知；核对幂等和恢复。
- [ ] 停止数据库敏感的并发验收后，按同一源码版本跑完整后端与 Web 门禁；发生修复才重跑受影响范围，最终证据精确标明源码与命令。
- [ ] 人工检查关键桌面/390px 截图、原始证据深链、日志脱敏和自有资源清理结果；记录真实外部调用为未运行。

```bash
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 TRADEOS_REQUIRE_E2E=1 python3 -m pytest tests/e2e/test_web_core_controlled.py -q -rs
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
python3 -m ruff check .
python3 -m mypy domains shared tool_gateway apps workflows notification_gateway infra
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 TRADEOS_REQUIRE_E2E=1 python3 -m pytest -q -rs
npm --prefix apps/web test
npm --prefix apps/web run typecheck
npm --prefix apps/web run lint
npm --prefix apps/web run build
npm --prefix apps/web run gen:api
git diff --exit-code -- apps/web/src/api/api.d.ts
git diff --check
```

测试命令在已核验 Python 3.12+/Node 24 环境内运行；使用项目自建隔离数据库，禁止读取生产连接。新增 E2E 必须执行，不能以 skip 的绿色退出当作通过。已有 lint warning 如仍存在，记录实际数和来源，不宣称零告警。
## Task9 验证范围交接

Task10平台裁定：Mac原ControlledConfig未装配quotation，且LinuxEvidenceTextParser在Mac平台不可用；Task10以主owner真实reply生成的Need/Opportunity/精确链接/成本503诚实状态，加原独立internal-network Linux报价E2E的公开reply→Need/Opportunity→来源/单位/成本/独立审批/PDF完整链分栏验证。不把独立owner/不同Need说成同一入口；A7必须有实际Linux完整链，不能以503页面替代。本任务按最终相同源码核验两套环境与准确命令；不得假称Mac统一入口报价可用，不为通过测试绕parser/probe/网络隔离。统一入口的环境限制由13交付说明明确。

Task9独立review Minor M1：`apps/web/tests/web-core-state-recovery.test.ts:40` 测试标题声称“拒绝后旧研究响应不能复活”，实际仅503读取失败文案断言。根据实际测试语义收窄标题；如本任务需要该延迟场景则写有意义断言，不能将旧标题本身当已有覆盖证明。此项不混入Task9 I1/I2修复。

Task9真实Settings浏览器恢复是在原服务器202之后丢失响应并替换为503，原key/payload再次202；不等于服务内部候选持久化后启动Run失败。本任务在原已授权故障验证中核查该内部中间状态与重启恢复是否已有精确证据；缺失则补最小故障注入，沿原公开端口和同key/payload，不按同内容或最新历史判定自己的提交成功。Task9 Sourcing已在真实PG覆盖canonical保存后ack失败、ack后event失败及原命令恢复。当前每批分组不累加为最终全仓通过。

## Task11 观测口径交接

Task11清理发现并已补删本owner的reply-model.sqlite。controller具名核apps/scheduler_worker/controlled.py:52在owned配置同目录创建该文件，而scripts/controlled_web_supervisor.py:442–447的close仅清config.json/mail.sqlite及其journal/wal/shm，遗漏reply-model.sqlite及其侧文件。本批按实际owned生命周期最小修复，先验证停止进程后删除，覆盖正常停止/故障清理，不扫目录或删除其他owner，不读取SQLite内容/配置值；Task11初次清理缺项和后续精确补删历史保持。

Task11已通过独立审查（源码4be8753e5b46442244e9dfe110bd4c308a367c25，报告a3390b5144c32fa72fd1e7f2a9c8ed882343ef43），读取最终report/review。正式spec为docs/design/2026-09-06-web-core-observability.md。各阶段采用各自时间字段与当前状态快照，不是同批cohort；接管队列为独立当前范围。ToolCall窗口内创建记录的当前累计attempt_count及quota当前状态不能当成历史窗口实际调用/消费。无可信usage、费率、人工工时及合格机会五条件合取证据时保留typed unknown，不把Task12合成样本当成生产成本数据。Task11人工缺字段Handoff Run故障注入仅验证原engine/scheduler失败路径；它不代表正常链业务失败率。精确对象来自持久Run.subject_ref绑定与tenant校验，页面导航仍须重新授权。

Task11 review Minor M1：新sourcing绑定分支缺真实PG聚焦覆盖，本批补成功绑定、workflow_version不一致和Opportunity.need_id不一致三种用例，当前未发现实现错误，不改变既有精确关联谓词。Need/Handoff目标页面在7/8已有权限作用组，本批最终当前身份变更/拒绝覆盖须保留；boss成功链接不是全角色权限证明。
