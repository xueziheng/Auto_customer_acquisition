# apps/scheduler_worker/ —— 调度进程（Phase 1 核心）

## 职责

Postgres 状态机的驱动器 + 全部定时任务。它停了，所有流程都停——**必须有「停止推进」的告警**（docs/architecture/11 的告警清单）。

## 主循环

```text
每 N 秒：
  workflow_engine.poll_due()        推进到期步骤
  outbox 投递                        事件总线的 outbox 扫描
  quotations.expire_overdue()       报价过期
  approvals.expire_overdue()        审批过期
  commitments.scan_overdue()        承诺逾期与升级
  sending_identity 周期评估          信誉窗口重算
  opportunities.get_queue_stats()   接管队列积压检查
```

## 唯一的硬约束

**单副本运行**（或先拿分布式锁）。重复扫描 = 重复推进状态机 = 重复发送。幂等键是兜底，不是许可证。

Hunter 联系人工具的生产装配还必须同时满足：环境中的安全配置哈希与 durable
Provider readiness 当前配置完全一致，且状态已验证；启动装配不得解析 Hunter 凭证，
不得调用 Provider。`runtime_composed` 只能由成功取得 advisory lock、并确认仍是同一
dedicated backend connection 的 scheduler 副本写入，且必须早于第一轮 cycle；未获锁、
锁已丢失或双工具未精确注册时一律不得写。激活失败必须释放同一把锁、零 cycle 退出，
日志不得包含异常原文、凭证引用、配置哈希或凭证值。

报价使用本进程独立composition，可委托非进程composition_support的机械装配，不导入apps.api。
各进程新建独立实例与显式lease_owner，不共享运行对象。显式报价配置与对象端口齐备后才注册
同一approvals/engine的七个报价步骤；缺文件预算只禁用整个文件组。来源tenant显式绑定。
quotation startup必须在原singleton获取且backend核验后、旧activation之后、首轮cycle之前；
未获锁零probe。resources退出先关同一parser再health/DB，启动取消保留原异常并沿原流程解锁。
报价通知只入持久job，固定LOW，经原通知worker模板/router进入站内，不创建邮件或伪DomainEvent。
QuoteExpiryDriver仅使用显式core.expiry_batch_limit委托expire_overdue；持锁cycle中位于campaign
之后/workflow之前，普通失败独立记录quote_expiry并继续其他driver，取消原样传播。API不扫描，
未获singleton或已失锁零expiry；事件沿原QuotationStateEventRow，不新增DomainEvent或cron。
activation返回后及expiry进入前重新确认同一专用锁backend；确认位于普通phase异常捕获外，
失锁返回LOCK_LOST且未完成轮不计数、不继续workflow/post-outbox。无expiry的旧两参数cycle
可保留；有expiry缺显式确认回调必须失败关闭。阶段检查不承诺已开始扫描的分布式fencing。
存在Sourcing Admission driver时，准入扫描前、扫描返回后进入workflow前，以及workflow有推进后进入
post-outbox前都必须重新确认同一锁；任一确认失锁都不得执行其后的phase。

验证只能由授权真人经 `provider.hunter.validate` 逐次触发，scheduler 不得代跑或自动重试。
validation passed 后必须重启 singleton scheduler；Settings 只有在 matching
`runtime_composed` 提交后才可显示 ready。密钥轮换、认证修复和回滚均创建新的配置版本，
不得复用历史 validation/runtime；运行中的旧 adapter 必须用 live guard 在 Connector 创建和
凭证解析前关闭。Provider ready 也不替代每次国家政策、Playbook、suppression 与 quota 检查。
运维证据只能使用 `docs/operations/hunter-provider-readiness.md` 的安全 allowlist。

## 入口

`main.py`：装配依赖 → 注册流程定义 → 循环。优雅停机：收到信号后完成当前批再退出，不中断在途事务。

## 研究组合

需求探索v1/v2同时注册；研究可以没有account_discovery/Campaign发送组合，此时不创建
账户发现队列。仅Web工具显式provider=tavily可启用research_only，默认Brave不得借用。
没有联系人组合的旧触达准备不能排队，不能通过降级配置绕过授权。

## 来源验收入口

`scripts/accept_research_discovery.py` 显式 opt-in 后装配真实 Tavily、公开页 transport、
Artifact Store、已生效 Playbook/国家政策与持久免费额度。只在该入口注册
`research_source_acceptance`，不得注册进普通 scheduler；其 `pages_only` 终态不代表
Signal/Hypothesis 或业务研究完成。只接受仍在职的确认老板和当前生效的研究提案。
默认工具组合仍仅接受 `demand_discovery`，专用类型只能由受信组合显式传入。

Web预算检查用独立命名空间的 tenant+run PostgreSQL advisory transaction lock，
不在独立连接重取engine已持有的Run行锁。已commit的received/未决/成功ledger继续
保守计数，rejected/duplicate排除；禁止以去锁或增加预算修复等待。

## Sourcing V2 组合

仅当 `TRADEOS_SOURCING_SETTINGS_JSON` 严格显式 enabled 且全部研究端口已注入时，root 才装配 V2。Tavily ref 只保存引用、构造期不得解析或联网；真实运行只能经 Tool Gateway 的 tenant、permission、playbook、country policy 与 rate-limit checks。受控验收可替换 Tavily、public-page、extraction model 端口，不能替换 PostgreSQL、domain service、Workflow、Outbox 或 API。不得接触联系人、发信、采购、客户 Quote 端口。

本子项目从 `ValidatedNeed` 开始；`DemandSignalCaptured` 与 `NeedHypothesisCreated` 各自仍有
catalog 所定义的真实下游语义，禁止由本 composition 作无副作用 acknowledgement 并提前标记
delivered。仅 `SourcingCaseOpened` 与 `OpportunityQualified` 是当前阶段不推进后续业务状态的具名、
tenant-bound audit acknowledgement；它只确认已消费，不能创建联系人、采购、报价或外部调用。
不得把这条显式订阅扩展成全局无 handler 的宽容策略：其他未注册事件仍由 Outbox 标记为 dead。

需求就绪事件只能读取一次可信 Need snapshot、开一次 canonical V2 Case、读取一次 Demand 当前簇事实并
durable enqueue 一次 Sourcing Admission；事件 handler 严禁直接启动 Workflow。`NeedClusterMembershipChanged`
由精确 consumer `sourcing_case.cluster_membership` 消费，事件成员数只校验形状，排序事实必须重新从 Demand
公共服务读取。当前簇事实只刷新该簇全部 waiting/blocked admission 以及 changed Need 的无快照阻断项；
starting/admitted 不变，priority-invalid 可恢复，case-state-mismatch 只更新快照不恢复。旧
`NeedClusterFormed` 不由这条路径消费。暂态事实读取失败以固定脱敏错误交回 Outbox，不得固化为 blocked；
永久非法排序结构在 readiness 路径只能形成固定 `priority_facts_invalid`，在 membership 路径必须复用
精确 refresh targets 将同簇 waiting 固定阻断；valid 但当前 cluster 为空或不同才是 stale no-op。三类
结果不得混同，且禁止保存原异常或自由文本。

## Catalog Product Proposal 组合

Catalog 恢复扫描只能在同一 singleton scheduler 专用锁下运行，位于
Sourcing Admission 之后、workflow poll 之前。进入扫描前和返回后必须分别重新确认
同一 backend；存在 Catalog 驱动却缺少确认回调时，任何 phase 开始前失败关闭。
扫描普通失败只记录固定 `catalog_products` 阶段和类别/租户/cycle，不得记录异常原文、
业务 ID、事实、证据、hash、URL、价格、凭证或 DSN。

三流每轮依次处理：`pending_approval` 策略恢复、
`awaiting_approval_submission` 提案恢复、存在活动策略时的真实需求簇扫描。
游标 checkpoint 必须持久保存 tenant、固定 stream、`created_at`、稳定实体 ID 和 CAS
version，按升序 keyset 有界翻页；进程或 driver 每轮重建仍从持久位置继续。只有读取和
整页处理确定成功才以 version+旧位置 CAS 推进，末页耗尽写入 null 位置（不删行）后才归零。
checkpoint 是恢复进度而不是锁或 leader election；生产仍必须持有同一 singleton scheduler
专用锁。不得用首页重扫饿死历史行，不得在 checkpoint 存业务正文。

评估必须先用 canonical context 和确定幂等键创建 durable
`catalog_cluster_evaluation` Run，不得由 scheduler 直接调用 Products 评估。运行时只通过
Demand/Products/Approvals 公开服务协作，不得导入其 repository、model 或 UoW 绕过领域服务。
必须注册三个 Catalog workflow 定义及精确 metadata-only 事件 consumer；
`NeedClusterMembershipChanged` 同时保留 `sourcing_case.cluster_membership` 与
`catalog_products.need_cluster_membership_changed` 两个不同 durable consumer。`ApprovalDecided`
继续按审批类型鉴别，不得以 Catalog handler 死信其他审批。组合与恢复不得创建
Product、Sourcing、供应商/联系人、发送、报价、价格、外部 provider 或 Tool Gateway 动作，也不得创建测试策略。

## 邮件入站阶段（ADR0026）

InboundDriver只在本进程已注册完整reply_qualification及原InboundMessageStored消费者时启用。
缺消费者时健康能力inbound_body为disabled/required_ports_missing，人工绑定仅代表固定邮箱配置，
不表示自动抓取已启用；不得拉取后制造无人消费的入站事件。Task6完整组合后才自动启用。
原singleton cycle在入站扫描前、返回后及后置outbox前核验同一backend；失锁零后续推进。
driver不持有锁、不建循环，opaque游标只从tenant账本读取；永久错误等待真人带版本原位核对，
429/暂态到期才能重试，不能绕过Retry-After、重置history或换身份。

## Task6回复资源（ADR0027）

reply工厂只借用本进程显式typed session/BoundedRaw及canonical服务；不挖private或另建engine。
Demand的客户证据委托每runtime独立，仅允许绑定一次真实verifier，未绑定拒绝，ready前完成。
内容复用5a纯解析，完整subject+原HTML候选及subject+文本各先guard再预算，超限拒绝不裁剪。
