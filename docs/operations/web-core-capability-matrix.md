# Web 核心能力与执行边界清单

历史受控验收日期：2026-09-06；持久内测验收日期：2026-09-07。**本机受控 Web 与本机持久内测
分别完成限定验收；多人共享部署未验收。** 受控结论依据
[Task12正式验收](../acceptance/2026-09-05-web-core-completion.md)及
[最终修复与独立限定复审](../acceptance/web-core-delivery/README.md)；持久内测依据
[真实登录与冷恢复验收](../acceptance/2026-09-07-web-internal-pilot.md)。所有验收业务输入/外部响应均为
合成资料，不作为真实采购意愿或效率成绩。

## 版本、门禁与环境

| 项目 | 精确事实 |
| --- | --- |
| 最终权限与资源修复 | `02506e44b52392879ea7b8a5b350da435a9f827f`：当前资格/Gateway 原件读取/分类事务授权与 SQLite 关闭；受影响13文件216 passed/73.69s、原主链1 passed/35.93s，限定复审双Approved；未再跑全仓 |
| 后端全量 | `48e4465fc307212e794d6ed87501cb418f74d245`：9318 passed/0 failed/0 skipped，2132.64s；源码1630文件整轮hash差异0 |
| 最终主链修复 | `7e10383c253df4a98cd224fb7ee526d721476f9a`：仅两测试文件修复真实Campaign审批actor，完整Mac主链1 passed/34.97s；生产仍与48e4465一致，未再全量 |
| Web | 同Task12未变Web源码，411 passed（32文件），typecheck/build/gen:api通过且API生成无diff；lint112 warnings/0 errors，均历史归属 |
| 受控基线迁移 | Task12 当时单一head `0059`：[入站正文迁移](../../migrations/versions/0059_email_inbound.py)。这是历史受控基线；实际完整门禁含旧迁移往返/组合，不只源码枚举 |
| 持久内测 | 生产源 `ecfb4d2d959ade7ffa143b7b9ad1b8e29cde4242`；测试修订 `63150da376b07414088f5c0f90bcb7ea5f22e075`；增强E2E 1 passed/34.15s；当前单一head `0060`，生产静态Web、真实账号/会话、三应用、持久PG/MinIO、stop/start与冷恢复 |
| 依赖 | Python3.12.14独立环境安装`.[dev]`；Node24.15.0独立空npm配置执行ci/build，见[操作说明](web-core-local.md) |
| Mac受控入口 | `scripts/run_web_core_controlled.py`：API/scheduler/notification/Vite四应用，owned PG/MinIO，loopback；停止删除本次合成环境。最终主链owner `8d234c0956fb498e9f171342c3b72f51` |
| Mac持久内测入口 | `scripts/run_web_pilot.py`：同源API/生产Web、scheduler、notification三应用，独立owned PG/MinIO；真实账号，停止保留profile/容器/卷；只验Chromium 151.0.7922.34与loopback HTTP |
| Linux独立链 | 完整报价/PDF原测试owner `2ea6b4a4e32f429192c2d1926a2ebfc1`；与Mac不同Need/Opportunity，固定internal-network和parser probe；非Mac用户启动入口 |
| 历史失败 | 首轮98 failed/9217 passed，第二完整门禁消除；A3旧独立审批声明撤回后由7e10383真实actor修复。早期3/2 warnings类别未知，不抹去 |

不同版本、局部和完整数量不相加。[安全证据/裁定索引](../acceptance/web-core-delivery/README.md)
保存版本与限制。Task0的ec801a8/0058清单只在[历史附录](../acceptance/web-core-delivery/task-0-capability-history.md)，不能代表当前状态。

## 持久内测独立证据

| 能力 | 本机持久内测实际状态 |
| --- | --- |
| 登录与授权 | boss/sales真实密码登录、HttpOnly同源Cookie、刷新保持；每次请求重读当前员工角色，boss团队API 200、sales 403。无默认账号、注册、邮件找回或开发身份回退 |
| 会话生命周期 | logout 204无body；失败重试与普通退出均验证正常jar清Cookie及隔离重放旧token为401；B直接换号时A旧业务壳卸载，共享会话从一页退出时另一页也清空；真实服务端logout响应暂缓交付期间Web Lock可见held/pending，实际response事件先于login request；disable/reset-password撤销旧会话 |
| 持久运行 | 完整stop/start后合成团队数据、数据库canonical hash和对象SHA-256保持；生产stop只停止精确owner进程/容器，不删除卷或profile |
| 冷备份恢复 | 双存储停止后物理归档；恢复到新owner、新卷、新端口，源profile独立不变；目标保留tenant/对象/业务关联并撤销恢复会话，必须重新登录 |
| Web | 使用`apps/web/dist`生产构建和同源API，不使用Vite/开发员工头；1440与390px实测，无文档级横向溢出及Vite overlay。console collector宽泛忽略任意failed-resource 401/403及模拟网络故障窗口内全部warning/error，不能排除过滤范围内并发告警，M1延期至最终review |
| 通知与外部能力 | `local_in_app`启用、email disabled；真实模型、搜索、Hunter、Gmail、供应商、发送、TLS与共享部署not_run |

持久内测只证明本机单租户登录、授权、持久化与恢复。合成员工、Territory Assignment和对象不证明
Demand Signal、Validated Need、Trade Opportunity、客户验证或北极星指标改善。Cookie 名带端口只避免
合作profile互相覆盖，不隔离恶意本机服务；跨标签认证要求当前浏览器支持 Web Locks。

## 页面到执行链路

“受控已接通”表示真实域、Gateway、审批、Workflow、Outbox和PG组合已执行，外部端口为具名合成响应。
“未配置”表示当前入口没有所需技术端口；“disabled”没有可执行消费者；“not_run”表示真实外部或部署验证未运行。
API只做即时命令和读取；持久步骤唯一执行者是持有单副本锁的scheduler，通知由notification worker消费。

| 能力 | 页面/API与当前权限 | 真实执行链 / 状态与限制 |
| --- | --- | --- |
| 指令 | `/commands`、`/commands/discovery-proposals`；当前boss | API `TradeManagerAgent.propose_discovery`→Directive提案，确认只start Run；scheduler `demand_discovery`持久步骤执行。受控精确研究文本已支持；任意自由文本不访问真实模型 |
| 研究发现 | `/demand`的Signal/Hypothesis/Cluster；当前boss研究权限 | 原 `ResearchRuntimePorts` + canonical bootstrap、持久quota、Tool Gateway search/page/model已接通。无活跃政策confirm可受理但Run失败/外部calls0；Playbook与KE独立批准后3 Signal/3 Hypothesis。research_only零联系人/Campaign/send，不自动转触达 |
| 联系人/触达准备 | `/prospects/discoveries`、`/prospects/accounts`；独立明确触达任务/当前授权 | scheduler原`account_discovery`，typed late-binding `ContactRuntimePorts`复用本runtime canonical domains，单Provider enrich→verify→原步骤持久verified→入组。未验证拒绝。真实Hunter validation/readiness未运行，不能把controlled-single-provider或exclusive合成账户当真实就绪 |
| Campaign/发件身份 | Campaign页面、`/crm/campaigns`及`/crm/sending-identities` API；人工登记/认证/预热与独立审批 | 原DNS→Gateway→认证状态机；真实提议人自批400/pending、另一当前老板批准精确Campaign/v1再激活。scheduler发送仅本owner受控邮箱；抑制/额度/当前回复/可达性门禁真实。真实DNS/冷开发运营/Gmail not_run |
| 入站 | `/email-inbound/status/binding/reviews`及原件用途读取；绑定boss，阅读按当前actor | scheduler typed mailbox经Gateway→RawArtifact→可信出站关联→ingest/Outbox；客户正文与退信投诉分开。未知/跨租户关联不晋升，正文cursor/重放已验 |
| Inbox | `/inbox/conversations...`、`/inbox/messages/{id}/correct-classification`、`evidence`；boss/manager/sales按当前owner范围 | ConversationService服务层列表/详情/纠正/下一问/原件授权一致；原reply classify/apply_actions接通，撤权后旧页面清理且HTTP拒绝。无权/跨tenant/不存在统一PermissionDenied，已授权未分类才ValidationError |
| Need | `/demand/needs/{id}`；按当前角色/对象权限 | 合成MIME经过真实回复证据/逐字段Provenance→原Demand验证→Need，事实与推断分栏、客户原话和原件可读。不能直插结果充当闭环；Need记录不自动成为北极星合格机会 |
| 机会/接管 | `/crm`、`/crm/handoffs/{id}`及accept；当前归属/管理范围 | 原reply→Opportunity/Ownership/Handoff→human_handoff/通知；真人接受已由HTTP/proof证明。队列按等待时长；OpportunityList不消费opportunity_id查询，不能把看板截图当精确机会深链 |
| 寻源 | `/sourcing`、Case/计划/确认/评审/Admission；当前sourcing与老板权限 | 既有V2/准入完整受控链与Task12原作用组通过。Mac原入口自动寻源准入/技术组未配置；不能以已有域或合成research账户推导自动启用。Need不合并，公开价格indicative只内部判断 |
| 成本报价 | `/costing-quotes`页面及同前缀`/quotes/{id}`用途API、Quote→Run→Approval、Opportunity→Need/精确成本单 | 独立Linux公开回复→来源/单位/22项成本→Decimal→独立审批→PDF链通过。Mac完整quotation组未配置返回503。quoted或人工明确接受indicative风险并留痕才可进入报价，仍需正式审批；没有供应商联系或报价发送 |
| 审批 | `/approvals/{id}`、decide；精确类型/当前角色、独立决定者 | 原审批域与业务canonical包，Outbox/scheduler应用；决定成功不等于已应用。过期、自批、stale、重复回放拒绝/收敛。批准不授予所有后续承诺 |
| Run/观测 | `/runs`、详情/安全失败与对象链接；当前boss/对象权限 | 原RunAuditService/PG安全投影，可靠ID才深链，来源不足标unknown。成功/失败/等待/缺项可见；无通用强制重试按钮。总模型token、费率、人工工时未知，单位合格机会成本不可算 |
| Settings/Catalog | `/settings` Playbook/国家政策/Provider readiness；`/products`策略/提案/培养Case | 原候选提交→Run失败可同actor/key/payload恢复，独立审批后生效。Catalog策略/提案/reconciliation持久恢复已验，批准终点仅queued Case；`CatalogCultivationQueued`消费者disabled，无正式Product/供应商/搜索/发送/报价副作用 |

接口实现分别见[API routers](../../apps/api/routers/)、[canonical scheduler](../../apps/scheduler_worker/bootstrap.py)、
[运行装配](../../apps/scheduler_worker/runtime.py)、[受控研究](../../apps/scheduler_worker/controlled.py)、
[联系人typed装配](../../apps/scheduler_worker/controlled_contacts.py)。

## 进程与仍未交付项

| 进程/能力 | 当前事实 |
| --- | --- |
| API | 原create_runtime_app、显式配置/schema核验、typed组合；无配置fail closed，不用OpenAPI空工厂假称ready |
| Scheduler | 原CanonicalSchedulerBootstrap/RuntimeFactory，持锁后推进真实步骤。受控入口已装配研究/联系人/触达/回复；报价/寻源按技术组控制 |
| 受控 Notification | 四应用之一，原durable job/站内adapter启用；controlled_in_app，email disabled。不能把排队当送达 |
| 持久内测 Notification | 三应用之一，`local_in_app`真实站内投递启用、email disabled；未运行真实邮件渠道 |
| 受控 Web | 原Vite、显著开发身份、当前请求generation；四应用健康与业务未配置分别显示；390/1440代表历史受控页面实际查看 |
| 持久内测 Web | 生产静态构建、真实登录与同源Cookie；三个应用；Web Locks缺失时安全拒绝。当前只验Chromium 151.0.7922.34，不宣称其他浏览器兼容 |
| Agent Worker | Task1/2技能注册、上下文裁剪、权限工具交集及窄适配仅组件验证。生产AgentJobRepository/任务源、tenant policy、受信descriptor和模型消费者未装配，disabled；当前Agent调用由确定API/scheduler步骤承担 |
| Browser Worker | 无生产BrowserJobRepository/任务来源或零参数factory，disabled；不启动空队列、不称可用人工浏览器接管。当前公开页由研究/寻源Gateway组合读取 |
| Email Feedback Worker | 独立配置的DSN/ARF worker仍存在，不在四进程中启动；客户正文归scheduler入站组合，不把反馈worker当正文入口 |
| 桌面 | 无Tauri/IPC/目录监听/Keychain/原生通知/登录态接管；仅[扩展契约](../architecture/12-client-capability-boundaries.md) |
| 共享部署 | 本机loopback已验真实认证、当前员工授权、会话撤销、CSRF/来源限制；共享主机、反向代理、HTTPS/TLS、Secure Cookie及多人运维仍未验收。开发角色和authenticated前端标签不构成授权，客户端不能自报权限 |
| 真实业务 | Tavily/Hunter/Gmail/真实模型/供应商/商业数据/发送、价格承诺和真实客户市场验证均not_run；真实外部动作须另有明确授权并继续经Gateway与审批 |

## 重启、成本与可验证界限

受控入口的HUP覆盖四应用，同owner重放后一份业务结果/一次实际send已验。未知发送使用每次Provider调用账本，
同消息去重数不能替代实际调用次数。研究额度为PG持久quota；联系人秒/分钟沿原内存限流，不保证跨重启额度。
reply model_calls=1只计回复模型，研究model另计1；不能推导整链总token或计费成本。

PG单独stop/start可能更换随机HostPort；原配置不自动更新。A5仅以同owner pause/unpause证明原端点短断恢复。
受控入口的静止owned数据备份演练独立列于[受控操作说明](web-core-local.md)，完整停止删除本次合成环境，
新启动是新owner空环境。持久内测入口的`stop`保留卷，冷备份恢复到新owner，详见
[持久内测操作说明](web-internal-pilot.md)；它仍不是PITR、异地灾备或外部邮箱恢复。历史失败与额外验收
成本见正式裁定记录。
