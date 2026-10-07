> 历史附录：Task0 / 2026-09-05 / ec801a8 / 0058，仅为当时缺口，当前状态见[主能力矩阵](../../operations/web-core-capability-matrix.md)。相对链接已随归档位置调整。

# Web 核心能力与执行边界清单

日期：2026-09-05。本文只描述提交 `1b760b2` 的业务源码加计划提交 `ec801a8` 在当前隔离工作树中的事实，不代表生产部署、真实外部能力或运营效果已经验收。后续任务应以本清单定位组合缺口，不能把“有页面”“有 Protocol”或“历史受控验收通过”单独解释为当前进程可启动。

## 一、基线与判定口径

| 项目 | 当前事实 | 依据 |
| --- | --- | --- |
| 工作树 | 分支 `codex/web-core-completion`；当前 HEAD `ec801a87270503b92d3c70389cbcd4b92807dbb9`，其父功能基线为 `1b760b2` | `git branch --show-current`、`git rev-parse HEAD`、`git log -3 --oneline` |
| 改动基线 | Task 0 开始时 tracked、staged、untracked 路径均为空 | `git diff --name-only`、`git diff --cached --name-only`、`git ls-files --others --exclude-standard` |
| 迁移源码 head | 单一链尾为 `0058`，`down_revision = "0057"`；这里只核对迁移源码，没有连接数据库 | [`0058_catalog_reconciliation_checkpoints.py`](../../../migrations/versions/0058_catalog_reconciliation_checkpoints.py)、[`test_migrations.py`](../../../tests/integration/test_migrations.py) |
| 运行时 | `.venv/bin/python` 为 Python 3.12.14；Node 为 v24.15.0 | 版本命令；[`package.json`](../../../apps/web/package.json) 也约束 Node 24.x |
| 结构与聚焦回归 | 控制者在同一基线运行结构检查，7 项 PASS；`test_agent_worker`、`test_guardrail_checker`、`test_api_runtime`、`test_inbox_api` 合计 60 passed（4.98s） | 本任务接收的控制者基线证据；Task 0 未重复运行 |
| Git 元数据噪声 | 读取 Git 时会报告共享对象库 `._pack-*.idx` 的既有 `non-monotonic index`；本任务不修改或修复共享 `.git/objects` | 安全 Git 元数据命令的 stderr |

状态只按“当前源码能否在 Web 核心受控运行中形成真实链路”判定：

- **已有可组合**：业务、API/流程和安全端口已存在，受显式配置或数据门禁控制；W2 仍需把必要进程放入同一受控启动入口。
- **需补组合**：主要业务实现已存在，但正式进程入口、typed 依赖或事件/步骤接线尚未闭合。
- **需新实现**：缺少完成链路必需的安全能力，不是简单接线。
- **本轮暂缓**：设计明确不进入 Web 核心里程碑，页面必须如实显示终点或不可用状态。

“执行者”指改变该项状态的唯一进程。API 只执行即时命令；需要恢复、等待或重放的动作只由持有单副本锁的 scheduler 推进。当前 Agent 能力是 API 或 scheduler 中确定步骤的依赖，Agent Worker 不与它们重复消费；Browser Worker 也不替代 scheduler 的公开页面 Gateway 链。

## 二、页面到执行链路

| 能力与状态 | 页面动作 → API | 工作流与唯一执行者 | 域服务 | 外部能力 | 当前开启条件、缺口与验收归属 |
| --- | --- | --- | --- | --- | --- |
| **指令：需补组合** | boss-only 的 `/commands` 创建、读取、确认、拒绝发现提案：`POST/GET /commands/discovery-proposals...`；另有寻源准入提案 | 创建提案由 **API 即时调用** `TradeManagerAgent.propose_discovery`；确认后 API 只幂等 `start(demand_discovery)`；四个持久步骤只能由 **scheduler** 推进 | `DirectiveService`；研究状态读取服务；`WorkflowEngine` | 提案解析使用 OpenAI JSON 模型端口；后续研究外部能力归“发现”链 | API 工厂已构造专用 Trade Manager，但通用技能注册、上下文裁剪与权限工具交集尚未实现；scheduler 的 `DemandDiscoveryComposition` 只能注入，零参数入口不会装配。W1/W2 覆盖。依据：[`command_center.py`](../../../apps/api/routers/command_center.py)、[`runtime.py`](../../../apps/api/composition/runtime.py)、[`demand_discovery/flow.py`](../../../workflows/demand_discovery/flow.py) |
| **发现：需补组合** | boss-only 的 `/demand` 读取 Signal/Hypothesis/Validated Need/Need Cluster；`POST /prospects/discoveries` 发起账户发现，`/prospects/accounts` 读取企业/联系人 | `demand_discovery` 的 `plan_search → execute_search → generate_hypotheses → score_and_queue` 与 `account_discovery` 的企业解析、联系人补全/验证、归属、入组均由 **scheduler**；页面读取与 Run 创建由 API | `DemandService`、`ProspectingService`、`EmployeeService`、`OutreachService` | Tavily 搜索、公开页读取、模型提取；联系人可选 Hunter `contact.enrich` + `contact.verify`，全经 Tool Gateway | 研究要求确认提案、有效 Playbook/国家政策、显式 Tavily 组合与额度；触达准备还要求 Campaign 发送组合。Hunter 必须配置声明、真人验证、匹配配置并由锁 owner 写 `runtime_composed`。现有源码和受控验收不等于真实 Provider 已启用。W2 闭合启动组合。依据：[`scheduler runtime`](../../../apps/scheduler_worker/runtime.py)、[`account_discovery/flow.py`](../../../workflows/account_discovery/flow.py)、[`2026-08-27-phase2-free-discovery.md`](../2026-08-27-phase2-free-discovery.md) |
| **Campaign：需补组合** | `/campaigns` 创建/修订/提交审批/激活/暂停/取消并查看 Enrollment；另有手工邮件 API | 边界和状态命令由 **API 即时调用**；每个 Enrollment 的 `draft_content → prepare_send → send → record_sent → wait_for_reply` 只由 **scheduler** 扫描和推进 | `OutreachService`、`SendingIdentityService`、`ApprovalService` | Gmail `email.send`、DNS 认证检查、退订链接，均经 Tool Gateway | 激活需独立审批；发送还需已验证联系人、可用发件身份、抑制/额度/回复现状和 scheduler `CampaignMessagingComposition`。正式 API 工厂当前传入 `manual_send=None`，因此手工发送 Gateway 明确不可用；scheduler `main()` 也没有生产 bootstrap。W2 闭合。依据：[`campaigns.py`](../../../apps/api/routers/campaigns.py)、[`outreach_campaign/flow.py`](../../../workflows/outreach_campaign/flow.py)、[`API runtime`](../../../apps/api/runtime.py) |
| **Inbox：需新实现** | `/inbox` 列表、详情、人工纠正分类：`GET /inbox/conversations...`、`POST /inbox/messages/{id}/correct-classification` | 现有数据读取/纠正由 **API 即时调用**；理想入站链应由 **scheduler** 消费 `InboundMessageStored` 并运行 `classify → apply_actions` | `ConversationService`、`OutreachService`；后续动作接 `DemandService`、`OpportunityService` | 当前 Gmail 工具只有 `email.feedback.fetch`，处理 DSN/ARF 退信投诉；没有客户回复正文读取、RawArtifact 归档和可信出站关联入口 | Inbox 当前只允许 boss，缺负责人范围；`ReplyQualificationComposition` 是可选注入且生产入口未提供模型/正文 reader；最前面的 Gmail 正文入口完全缺失。W3 新增安全入站插件，W4 补服务层范围。依据：[`inbox.py`](../../../apps/api/routers/inbox.py)、[`reply_qualification/flow.py`](../../../workflows/reply_qualification/flow.py)、[`connectors/gmail/AGENTS.md`](../../../connectors/gmail/AGENTS.md)、[`email_feedback_worker/AGENTS.md`](../../../apps/email_feedback_worker/AGENTS.md) |
| **Need：需补组合** | `/demand` 查看事实/推断/需求；`/demand/needs/{id}` 查看 Provenance 并跳转寻源 | 查询由 **API 即时调用**；只有 **scheduler** 的回复流程动作可沿可信客户回复把 Hypothesis 晋升为 Validated Need，并继续创建 Opportunity | `DemandService`；回复组合还使用 `ConversationService`、`ProspectingService`、`OutreachService` | 无独立外部动作；依赖 Inbox 的原始邮件 artifact 与分类模型结果 | 读取既有 Need 已可用；新 Need 的完整链被 Inbox 正文入口和 reply composition 阻断。客户原话、逐字段 quote、Enrollment/出站关联验证缺一不可，不能手工直插冒充。W3 验收。依据：[`demand_radar.py`](../../../apps/api/routers/demand_radar.py)、[`reply_actions.py`](../../../apps/scheduler_worker/reply_actions.py)、[`reply_customer_evidence.py`](../../../apps/scheduler_worker/adapters/reply_customer_evidence.py) |
| **接管：需补组合** | `/crm/handoffs` 查看队列/包并 `POST /crm/handoffs/{id}/accept`；机会列表/详情/状态也已有 API | 接受与 CRM 命令由 **API 即时调用**；接管通知、T1/T2 等待与升级只由 **scheduler** 的 `human_handoff` 工作流推进 | `OpportunityService`、`EmployeeService`，通知 job/adapter | 站内/邮件通知由 Notification Worker 投递；无客户外部承诺 | 已有队列与原子接受；自动创建接管包依赖 reply action 与真实 owner 分配，升级依赖 scheduler bootstrap。W2/W3/W4 覆盖，队列必须按等待时长。依据：[`crm.py`](../../../apps/api/routers/crm.py)、[`human_handoff/flow.py`](../../../workflows/human_handoff/flow.py)、[`notification_worker/runtime.py`](../../../apps/notification_worker/runtime.py) |
| **寻源：已有可组合** | `/sourcing` 查看 Admission/Case、人工准入；详情可建/确认/运行公开搜索计划、评审和不确定结果核对 | 人工命令由 **API 即时调用**；自动准入扫描和 `sourcing_case.v2` 持久步骤只由持锁 **scheduler** | `SourcingService`、`DirectiveService`、`OpportunityService`；额度 repository | Tavily、公开页、安全解析/模型，经 Tool Gateway；不接联系人、邮件、采购或客户 Quote | 仅在显式 `TRADEOS_SOURCING_SETTINGS_JSON` enabled 且 typed 研究端口齐全时组合；未配置策略不自动准入。现有受控链已验收，W2 只需把真实必要工厂接入受控启动，W5 做跨页面回归。依据：[`sourcing.py`](../../../apps/api/routers/sourcing.py)、[`scheduler config`](../../../apps/scheduler_worker/config.py)、[`2026-08-30-phase2-sourcing-case-product-cards.md`](../2026-08-30-phase2-sourcing-case-product-cards.md)、[`2026-09-02-phase2-need-cluster-sourcing-admission.md`](../2026-09-02-phase2-need-cluster-sourcing-admission.md) |
| **成本报价：已有可组合** | `/costing-quotes` 建 Cost Sheet/成本项、评估完整度、确认政策/汇率/价格证据/范围/单位、确定性计算、建修订 Quote、提交审批、生成/下载 PDF | 成本和报价草稿命令由 **API 即时调用**；`quote_approval` 与到期扫描只由 **scheduler** | `CostingService`、报价域服务、`ApprovalService`、Artifact Store | 对象存储、受限证据解析、PDF 生成；没有自动供应商谈判或 direct supplier quote | 金额由 Decimal 代码计算；`quoted` 价格可直接进入客户可见报价，只有 `indicative` 价格时必须由人工明确接受风险并完整留痕，且审批包和客户报价链路继续显著保留风险标记，否则不得进入。API 仅在报价配置和对象存储端口整组齐全时暴露真实组合，文件组可独立禁用；scheduler 必须使用同一类显式设置注册审批步骤。W5 回归。依据：[`costing_quotes.py`](../../../apps/api/routers/costing_quotes.py)、[`quotation_actions.py`](../../../apps/api/routers/quotation_actions.py)、[`2026-08-28-phase2-costing-quotation.md`](../2026-08-28-phase2-costing-quotation.md) |
| **审批：已有可组合** | `/approvals` 列表/详情；boss/manager 通过 `POST /approvals/{id}/decide` 决定 | 决定由 **API 即时调用**并发布事实；等待中的 Playbook、国家政策、报价、Catalog 等流程只由 **scheduler** 接收 `ApprovalDecided` 后应用 | `ApprovalService` 加各业务域服务 | 审批本身无外部调用；后续通知由 Notification Worker，报价发送仍另过 Gateway | 每类审批必须读取 canonical 变更包并防自批/过期/重放；“决定成功”不等于下游已应用。scheduler 未运行时应显示 pending/apply 状态，不能伪装完成。依据：[`approvals.py`](../../../apps/api/routers/approvals.py)、[`quote_approval/flow.py`](../../../workflows/quote_approval/flow.py)、[`catalog policy flow`](../../../workflows/catalog_product_proposal/policy_flow.py) |
| **Run：已有可组合** | boss-only 的 `/runs` 和 `/runs/{id}` 只读列表、步骤、计数、等待/失败/成本安全投影 | 读取由 **API 即时调用**；所有状态变化归创建该 Run 的 **scheduler 工作流**，页面没有通用“强制重试”执行权 | `RunAuditService`、`PostgresWorkflowEngine` | 无直接外部能力；只展示已由步骤安全记录的调用/成本元数据 | API 需显式数据库与当前 schema；运行状态是否变化取决于 scheduler 是否真实持锁并 ready。W2 提供健康/恢复，W4/W5补状态呈现。依据：[`runs.py`](../../../apps/api/routers/runs.py)、[`engine/audit.py`](../../../workflows/engine/audit.py)、[`scheduler main`](../../../apps/scheduler_worker/main.py) |
| **Settings：已有可组合；Catalog 消费本轮暂缓** | boss-only 的 `/settings` 读取研究、Playbook、国家政策、Hunter readiness并提交 Playbook/国家政策提案；`/products` 管理 Catalog 策略/提案并显示培养 Case | 提案创建和只读状态由 **API 即时调用**；Playbook、国家政策与 Catalog 审批/恢复/评估由持锁 **scheduler** | `OrganizationService`、`ComplianceService`、`ProviderReadinessService`、`CatalogProposalService`、`ApprovalService` | Hunter 只显示 durable readiness；验证需授权真人走专用 Gateway。Settings 不应解析或回显凭证 | Playbook/国家政策批准后才激活；Hunter 当前配置需独立声明、真人验证、重启并匹配 `runtime_composed`。Catalog 审批后只创建 `queued` 培养 Case，下游消费者明确暂缓，不能显示为已培养。依据：[`settings.py`](../../../apps/api/routers/settings.py)、[`catalog_product_runtime.py`](../../../apps/scheduler_worker/catalog_product_runtime.py)、[`2026-09-04-phase2-catalog-product-proposal.md`](../2026-09-04-phase2-catalog-product-proposal.md) |

## 三、进程启动事实与唯一职责

| 进程 | 当前正式入口事实 | Web 核心结论 |
| --- | --- | --- |
| API | [`create_runtime_app()`](../../../apps/api/runtime.py) 读取显式配置，装配 Postgres/域/API，并在 lifespan 核对 schema；对象存储设置也由正式工厂读取。`create_app()` 的零配置形态只供 OpenAPI/失败关闭 | W2 需提供隔离配置与生命周期；不能用 `create_app()` 的未配置 503 形态冒充可用 API |
| Scheduler Worker | [`SchedulerRuntimeFactory`](../../../apps/scheduler_worker/runtime.py) 能装配下层组件，但 [`main()`](../../../apps/scheduler_worker/main.py) 未收到 factory 时固定非零退出；typed 业务依赖仍须部署层提供 | W2 的主要启动缺口；必须单副本锁，不能从 API 进程导入或复制第二套流程 |
| Agent Worker | [`main.py`](../../../apps/agent_worker/main.py) 只有队列/runner/context/gate Protocol 和可注入循环；仓库中未找到生产 `AgentJobRepository` 或零参数 factory | 保持 disabled。当前 Web 核心 Agent 调用留在确定 API/scheduler 步骤；除非未来出现真实持久 Agent Job 来源，不创建填空队列 |
| Browser Worker | [`main.py`](../../../apps/browser_worker/main.py) 只有 Gateway 授权 Browser Job 的可注入循环；仓库中未找到生产 `BrowserJobRepository` 或零参数 factory | 保持 disabled。现有公开页读取使用 sourcing/demand 的 Tool Gateway 组合；不为“启动全部进程”复制页面任务 |
| Email Feedback Worker | 有真实零参数 `EmailFeedbackRuntimeFactory(os.environ)`，可显式 enabled/disabled，只注册 `email.feedback.fetch` | 只负责 DSN/ARF 退信和投诉，不能承担客户回复正文入站；W3 新能力若需要独立进程，须另立规格/ADR |
| Notification Worker | 有显式环境配置的真实 runtime，固定站内+邮件渠道，缺邮件配置失败关闭 | 只消费持久通知 job；W2 仅在核心场景需要投递时启动，不由 API 同步冒充投递完成 |
| Web | `npm run dev` 启动 Vite；当前 [`vite.config.ts`](../../../apps/web/vite.config.ts) 没有项目级受控 launcher，Makefile `dev` 只启动 PostgreSQL/Redis/MinIO | W2 新增统一受控入口，显式绑定 loopback、使用独立测试数据，并只停止自己拥有的进程/资源 |

## 四、本机身份边界

本机角色切换不是认证。Web 的 [`client.ts`](../../../apps/web/src/api/client.ts) 会把 `fixed-dev` 或名为 `authenticated` 的内存身份转换成 `X-Tenant-Id`、`X-Employee-Id`；这个名称不构成服务器验证。API 的 [`resolve_request_identity`](../../../apps/api/identity.py) 在 `dev_mode=false` 时无条件返回 403，在 dev 模式也只接受一个员工 ID 头，再从固定租户的员工域 public DTO 重读角色、停用状态和 manager 直属范围。客户端不能自报 role 或 scope。

因此 W2 本机入口必须同时满足：只监听 loopback；只连任务自己创建或明确指定的隔离测试数据库/对象资源；显著标注测试身份；角色切换后清理旧请求和缓存；不允许回退到生产数据库。多人共享使用必须另接后端验证的真实认证主体、会话撤销、员工映射、CSRF/来源约束，并继续由服务端推导 tenant、role、owner scope。在这些门禁完成前，不得把 `authenticated` 前端标记或开发头部署成多人登录。

## 五、启动缺口与后续验证场景

| 缺口 | 归属 | 完成时必须证明 |
| --- | --- | --- |
| 技能注册、上下文裁剪、权限工具交集及 Agent Worker 窄适配缺失 | W1（Task 1–2） | 受控任务只加载精确版本和授权事实；必需护栏不被裁掉；ChangeSet 仍经 guardrail/审批 |
| API、scheduler 与必要通知进程没有统一的受控启动/停止入口；scheduler 无生产 bootstrap | W2（Task 3–4） | 缺配置/旧 schema/未注册步骤不 ready；单副本锁与重启恢复真实；只绑定 loopback、只清理自有资源 |
| Gmail 客户回复正文读取、RawArtifact 归档、可信关联和 cursor/重放语义缺失 | W3（Task 5） | 从 Gateway 到 Artifact、`ingest_inbound`、Outbox 全链；未知/跨租户关联不晋升；零真实网络 |
| Reply flow 的模型、正文 reader、护栏和业务动作尚未由生产 scheduler 提供 | W3（Task 6） | `InboundMessageStored` 触发且幂等；逐字段 Provenance；合法门槛才产生 Need/Opportunity/Handoff |
| Inbox 目前 boss-only，列表/详情/纠正没有统一员工 owner scope | W4（Task 7–9） | boss/manager/sales 的列表、深链、附件和纠正均在服务层一致判权；身份改变不回显旧数据 |
| 已有寻源、成本、报价、审批、Run 的跨页面启动与恢复尚未在同一版本集中验证 | W5/W6（Task 10–13） | A1–A10 同版本通过；未知、paused、stale、queued 如实展示；真实发送、供应商联系和付费来源调用均为 0 |
| Catalog 培养 Case 下游消费者 | 本轮暂缓 | 页面终点保持 `queued`，不得注册无业务策略的消费者，也不得称已培养或已创建正式 Product |

本文未读取 `.env`、凭证、数据库或网络，没有启动业务进程或产生外部效果。完整交付范围与 A1–A10 见 [`Web 核心收口验收设计`](../../superpowers/specs/2026-09-05-web-first-completion-design.md)，执行顺序见 [`实施计划`](../../superpowers/plans/2026-09-05-web-first-completion.md)。
