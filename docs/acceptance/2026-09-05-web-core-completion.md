# Web 核心同版本受控验收

实际验收日期：2026-09-06。Task12，待独立审查；不是 Task13 总交付声明。
BASE：`6cdb40d8019d560d1490925df72a58d14f4881d6`。本批24个源/测试文件的冻结内容摘要：`fa21469003d1391487e0f093c33c1adf046bca1d44d4c3037ff0e3f7421b055d`，逐文件 SHA256 见 `output/acceptance/task12/source-snapshot.json`。

## 验收边界与实际入口

Mac 完整链只从原 `scripts/run_web_core_controlled.py` 启动 API、scheduler、notification、Web 四进程。新声明环境实际安装 `.[dev]` 并启动该入口，Python3.12.14、Node24.15.0；隔离探针确认没有旧 Catalog 工作树依赖。PostgreSQL、MinIO、核心域、Gateway、Workflow、Outbox、审批、权限及预算均真实。合成端口只提供具名 DNS、邮件、公开研究页面/搜索、联系人与模型外部响应，网络边界拒绝未知地址，无真实供应商联系、真实邮箱/模型/联系人 Provider 或付费来源调用。

本批发现并修正原受控入口遗漏的研究与联系人装配。研究使用原 ResearchRuntimePorts、真实持久 quota/Artifact/Gateway；联系人按 [ADR0065](../adr/0065-late-bound-contact-runtime-ports.md) 在本 runtime 的 canonical Outreach 形成后借用同一 core/session，注册原 manifest/check/handler。外部合成联系人的验证结果由原 VerifyContactsStep 经 Prospecting 落库；没有直接写已验证结果。真实 Hunter 就绪判断保持不变，合成账户不是现实供应商账户。

## A1–A10 证据

| 项 | 已执行入口与断言 | 安全证据 |
| --- | --- | --- |
| A1 | 新声明 Python 环境安装并启动原四进程；原缺 Node/端口占用/迁移故障/未持锁不 ready 回归 | `install.json`、`installation-isolation.json`；原 `test_web_core_launcher.py`、`test_web_core_runtime.py` |
| A2 | 浏览器创建 Playbook 与 KE 政策，各由另一老板真实审批；确认老板研究指令产生3 Signal/3 Hypothesis，研究阶段无 Campaign、无联系人调用、无发送 | 第九轮主链 owner `335a86cc07fc4d7092270b1d8767d809` 下 `research-confirmation.json`、`proof.json`、研究截图 |
| A3 | 另一个明确人工输入的企业/需求假设和独立 Campaign 审批；未验证联系人被拒；原 account_discovery v2 经单 Provider enrich→verify→持久验证→归属→精确版本入组→原 HTTP 发送，一次发送 | `test_web_core_controlled.py` 反向消费原入口，`web_core_contacts.py` 只调用公开域/Workflow；最终全量新 proof 记录 Run、联系点、来源及逐次调用 |
| A4 | 合成 MIME 经原 Gateway、Artifact、ingest、Outbox、回复识别形成真实 Need/Opportunity/Handoff；原字段来源/邮件下载可读；真人接受交接 | 主链 `proof.json`、`message.eml`、Need/Handoff 1440/390截图；`test_reply_completion.py` |
| A5 | 同消息重放、原四进程 HUP 后一份交接且一次发送；Provider 动作后 ack 前未知响应按每次真实调用计数；candidate commit→Run start故障及同actor/key/payload恢复；DB暂停后原端点恢复 | `test_reply_completion.py::test_unknown_provider_send_is_reconciled_without_a_second_send`；`test_web_core_settings_recovery.py`；原审批/Campaign workflow 回归 |
| A6 | Need、Handoff、Run、原件均按当前 actor 检查；真实停用 actor 后旧内容清除且入口拒绝。boss/manager/sales 的列表、附件及纠正矩阵由原真实PG测试覆盖 | 主链撤销断言；`test_inbox_access.py`、`test_email_inbound_access.py`、`test_web_core_observability.py` |
| A7 | 独立原 Linux 公开回复→来源/单位→Decimal成本→独立审批→PDF链；原 Need→Sourcing→estimated cost适用链分开验收；无quoted且无风险接受时拒绝正式报价 | `a7-final-source.log`：3 passed/131.60s；Linux browser owner `a79870b4f3714cd79d6211d061c5ab64`、integration owner `8ad147dd94c84955aa1ba9de772dff88` |
| A8 | Web全量411项验证等待/失败/未知/暂停/stale/queued；实际看过主链Need1440、Handoff390和Linux报价390/精确成本1440，关键控件可读，无横溢 | `web-test-gate.log`，主链与Linux截图；机会页截图仅证明看板展示，精确Need绑定来自真实API |
| A9 | 原owner PID+出生时间、容器label+ID核验；正常/HUP/子进程故障停止后精确删除mail与reply-model SQLite及侧文件；停止不确定保留文件 | 主链 `cleanup.json`；原launcher正常/故障回归与 `test_web_core_private_cleanup.py` |
| A10 | 未知查询/页面/模型输入/联系点拒绝；网络未知地址在连接前拒绝；实际Provider动作独立记数，业务核心不可替代 | 原allowlist测试、新research/contact端口反例、Gateway账本和调用证据；真实外部能力仍为 `not_run` |

本表路径省略的统一前缀为 `output/acceptance/task12/`；Linux产物位于 `output/playwright/t10-<owner>/`。第九轮是一次中间完整主链通过，最终全仓结果不与该数相加。截图名 `need-sales-denied-390` 实际页面是 Handoff；它只证明当前页面撤销后清除，Need拒绝另由真实HTTP断言证明。OpportunityList 不消费 `?opportunity_id`，没有将无效参数声称为精确深链。

## 完整门禁

静态边界与敏感扫描 exit0，mypy 562文件通过；全仓ruff首次两处本批格式提示已修，最终exit0。Web：411 passed，typecheck/lint/build/gen:api均exit0，生成API无diff。lint实际112 warnings/0errors，逐行git blame证实均早于本计划branch base：App.vue25、OutreachWorkbench.vue34、BillingUnavailable.vue10、ManualOperations.vue12、ProductSupplyCenter.vue31。归属见 `lint-attribution.json`，不沿用历史120或172数字，也不声称零告警。

冻结版本完整后端实际为 **98 failed、9217 passed、exit1**，pytest耗时2076.35s、wrapper耗时2083.9s；本检查点门禁未通过，不能作为最终验收通过。完整98项安全索引见 `backend-failure-index.json`，后续修复与作用组结果另行登记，不倒写本轮结论。最终独立新owner与告警分类待核。所有pytest均清除TEST_DATABASE_URL，设置PYTHON_DOTENV_DISABLED=1、TRADEOS_REQUIRE_E2E=1，数据库测试串行。命令/耗时以 `static-gates.json`、`web-gates.json`、`backend-full.json`、`diff-gates.json` 为准。

## 限制与失败历史

Mac 原入口的 quotation 与自动寻源准入配置仍未具备完整运行条件；Mac成本页的明确503不能替代Linux报价证明。Linux与Mac的owner、Need、Opportunity不同。Linux固定业务时钟不作为真实耗时证据。本批没有新增统一launcher、跨网络桥或绕过Linux parser资源probe。

DB独立stop/start会因原随机HostPort分配改变公开端口，原配置不自动更新。内部pg_isready成功而原端点持续ConnectionRefused的失败已保留，不能归因为API连接池问题；应用HUP只重启四应用，不承诺自动恢复改变的DB端点。该情况下必须回到原owner入口受控重建配置/依赖，不能把新空库冒充原业务恢复。A5的短暂不可用用同容器pause/unpause保持端点，并以独立限时恢复任务保证取消请求能完成。

首次pause试验的driver取消也等待暂停PG，需精确owner手动unpause才结束；不计作有界恢复通过。期间误启动的E2E已立即中断并清理，之后恢复数据库串行纪律。所有中间TDD、旧fixture错误、审批选错旧记录及上述故障轮保留在Task12报告与独立日志；没有倒推为同一失败原因。

受控输入是合成演练证据，不代表真实买家、法律政策、供应商报价、邮箱可达性或生产账户就绪。受控exclusive研究账户只描述当前owner的合成端口。本批不部署、push、merge或实际外发。

另在只复制声明锁文件、公开src与构建配置的新目录中完成Node依赖安装与构建：`node-isolated-final.json` 记录 `npm ci` exit0/28.71s、`npm run build` exit0/5.05s。用户与全局npm配置均指定各自独立空文件，未借用用户凭证配置。首次两配置都指向/dev/null被npm以double-loading拒绝，原日志保留；这不属于应用运行失败。

合成模型没有真实计费token数据，真人处理没有可核实的实际处理时长；这些值保持未知，不填0或编造效率改善。已观察的Provider调用次数与Handoff等待状态分别按持久账本和真实当前时钟解释，Linux固定业务时钟不用于耗时比较。
