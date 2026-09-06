# Task12 同版本受控验收子规格

日期：2026-09-06。实施基线：6cdb40d8019d560d1490925df72a58d14f4881d6。
这是执行前的接口与断言计划，不是通过结论。Task0–11 不重做；本批只补具名缺口和最终证据。
使用 superpowers:test-driven-development 与 verification-before-completion；真实业务入口不被 fixture 替换。

| 场景 | 真实入口 | 持久事实 | Web 操作及断言 | 持久证据 |
| --- | --- | --- | --- | --- |
| A1 | scripts/run_web_core_controlled.py → Supervisor → 各 controlled 入口 | owned PostgreSQL migration、四进程、能力投影 | 新声明依赖环境启动 Settings；缺配置/迁移准确失败 | 本批验收报告命令表、status 安全投影、桌面/390 截图 |
| A2 | Command Center → discovery proposal/confirm → scheduler research workflow | Signal、Hypothesis、quota、Artifact、Run | 显式三线路 research_only 提案确认；无 Campaign/联系人/发信 | test_web_core_controlled 研究场景与安全计数 |
| A3 | 独立 outreach proposal/获批 Campaign → 原 prepare_sent 公开服务及 Gateway | 联系人验证、Campaign/Enrollment、发送 attempt | 发件身份登记/认证/预热/绑定；预算与暂停/退订仍拒绝 | 原触达测试及本批 sender/发送计数证据 |
| A4 | ControlledGmailTransport.receive_inbound → Gmail reader → Gateway → ingest → Outbox → reply → handoff | Message、Artifact、字段 Provenance、Validated Need、Opportunity、Handoff | Inbox 原文下载；精确 Need/机会/接管链接；接受接管 | 本批安全对象 ID 与桌面/390 截图 |
| A5 | 同一 Gateway command、同一邮件、原 workflow restart | 持久 attempt、消息唯一约束、Run/审批幂等 | 外部已发生但响应丢失后保留核对；每次 send 调用只一次 | 聚焦故障测试，原始计数而非去重邮件数 |
| A6 | 原 HTTP identity → 当前员工服务 → inbox/CRM/Run authorizer | tenant 与当前职责范围 | boss/manager/sales 列表、深链、原件、纠正拒绝；切换清旧内容 | 真实 PG 权限用例与浏览器身份切换 |
| A7 | Mac 原 launcher Need/Opportunity；独立原 Linux costing_quote_stack | Linux 公开回复→来源/单位/Decimal 成本/独立 Approval/PDF | Mac 诚实503；Linux indicative 拒绝、精确审批、下载与撤权 | 分 owner 记录；test_real_costing_quote_browser 完整链 |
| A8 | Run/Settings/Campaign/Sourcing/Catalog API 投影 | waiting/failed/unknown/paused/stale/queued | 不伪装空或成功；390 无横溢且按钮可达 | 前端用例与真实浏览器截图 |
| A9 | Supervisor restart/close、owner resource protocol | owner PID+birth、容器 label+ID | 正常/故障停止后才精确清私有文件；不动别的 owner | 生命周期回归和 cleanup 安全投影 |
| A10 | controlled allowlist、未知操作拒绝、固定外部端口装配 | 受控 provider 调用记录 | 仅 loopback，未知域/动作失败；无真实 provider 构造或凭证 | allowlist/构造测试与受控输入声明 |

## 最小修改合同

1. HTML 引用栈完整识别标准 void 元素；不改正文解析策略，不拼接引用两侧证据。新增合法媒体引用后当前字段仍可提取及跨段不可提取回归。先 RED，再最小集合修改。
2. Supervisor.close 在 owner 应用停止后，精确删除 reply-model.sqlite 及 journal/wal/shm；停止结果不确定时保留正在使用的私有文件并记录原停止错误。正常停止、启动故障、停止失败分别证明清理边界，不扫目录。
3. 四处 scanner 命中仅在验证合成护栏输入语义后换明确占位符，不改 scanner、不拼字隐藏形态。输出限路径/行/类别。
4. Task9 标题收窄到实际断言。新增 Settings 真实 PG 候选 commit 后 Run start 故障与原 actor/key/payload 恢复，不能用浏览器丢202替代。
5. Task11 sourcing Run 绑定新增真实 PG：成功、版本不匹配、机会 need 不匹配。既有查询谓词不放宽。

## 同版本与证据纪律

新增测试只反向消费 Task4 launcher。禁止 seed Message/Need/Opportunity/审批结果。仓储读模型单测的合成数据明确分层，不冒称完整链。
所有 pytest 使用 `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 TRADEOS_REQUIRE_E2E=1`，固定本工作树 Python3.12；数据库敏感命令串行。
只允许本次 ControlledConfig 确定性 loader 内存读取本 owner 配置；不输出连接或密钥、不读 SQLite 内容。安全报告和截图持久保存在本工作树。
完整门禁按 brief 实际运行并保留 exit、passed/failed/skips/warnings。旧失败不倒推成同因，局部通过不累加为全量。
真实邮箱/模型/联系人/供应商/付费来源仍 not_run；Mac quotation 依赖未配置的限制保留。Task13 负责总交付文档。

## A2 本批发现的原入口装配缺口（已获controller裁定）

新环境原launcher启动成功并完成真实Playbook/KE政策独立审批后，research_only提案确认按钮仍disabled。
代码核对：ControlledConfig未提供研究组；scheduler controlled入口未向既有CanonicalSchedulerBootstrap传ResearchRuntimePorts。
Task4历史仅证明提案生成/依赖拒绝，不证明研究可执行。此处为本批有意义RED，不能翻可用布尔伪装完成。

最小接线：本owner新合成研究引用→原API ResearchAccessService技术配置；原scheduler使用ResearchRuntimePorts
与research_enabled=True，复用原领域、Gateway、持久quota、Artifact与Outbox。新增infra/controlled/research.py只提供
三个精确查询/页面及对应合成模型响应的外部端口；未知country/query/limit/url/payload/model一律拒绝，不联网。
研究账户exclusive只表示本owner合成Provider场景，不是现实Tavily账户。对象存储使用原DeferredS3ObjectBlobTransport，
由原scheduler runtime外层async context在正常/启动故障/取消时close。无新增engine、队列、网络桥或业务结果seed。
A5另覆盖DB短暂不可用、旧/重复审批、unknown发生在Provider写入后/ack前的每次实际send计数。

## A3 同runtime联系人接线（ADR0065）

静态ContactRuntimePorts先于canonical Outreach形成，新增可选typed contacts_factory，与静态ports互斥，必须显式启用contacts与campaign。factory只借用本runtime core/session/Outreach/fingerprint/tenant/tool actor资源；当前core.prospecting身份必须相同，未绑定或返回无效端口拒绝。默认静态组合保留。见docs/adr/0065-late-bound-contact-runtime-ports.md。

受控factory在apps/scheduler_worker/controlled_contacts.py注册原contact.enrich/verify manifest、handler、真实tenant/permission/政策/suppression/rate_limit检查与Postgres ToolCall账本。infra/controlled/contacts.py只给具名公司/邮箱的合成外部响应；每次调用写同owner mail.sqlite独立调用表，原close统一清理。Hunter生产readiness完全不变。

A3从独立人工输入企业与待验证假设及独立Campaign批准开始，原account_discovery v2执行模型证据判断、Provider补全、VerifyContactsStep持久验证、归属、精确Campaign版本入组、原发送入口。另一个未验证联系点入组必须拒绝。研究场景不自动升级，且启用联系人后research_only仍零联系人调用/入组/发送。

A5数据库瞬态使用exact-owner pause/unpause并bounded取消原start，保持同端点。独立stop/start已实证随机公开端口可能改变，原配置不自动更新；不能将其解释为连接池故障，亦不属于应用HUP恢复契约。本批不改端口分配。

HTML void 集合依据 [WHATWG HTML syntax：Void elements](https://html.spec.whatwg.org/multipage/syntax.html#void-elements)：area、base、br、col、embed、hr、img、input、link、meta、source、track、wbr。它们没有结束标签，尾斜杠不改变语义。本次只补原保守引用栈的集合，不扩写通用HTML容错器。
