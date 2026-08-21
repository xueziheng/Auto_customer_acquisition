# tool_gateway/ —— 工具网关

## 职责

**所有对外部世界的动作的唯一出口**（硬边界 1）。Agent 和工作流提出工具调用，本模块跑完检查管线后才执行 handler。模型永远拿不到凭证——凭证只在 handler 内部，运行期从密钥服务取。

设计文档：`docs/architecture/04-tool-gateway.md`。

## 结构

```text
tool_gateway/
├── manifest.py        工具 manifest schema + 注册表
├── errors.py          固定安全错误、交付确定性与调用状态
├── repository.py      tenant-scoped ledger / UoW Protocol
├── fingerprint.py     HMAC 请求指纹（只持久化摘要）
├── pipeline.py        检查、claim、执行与恢复编排
├── checks/            每个 stage 一个文件
│   ├── tenant.py        租户一致性
│   ├── permission.py    RBAC + ABAC
│   ├── playbook.py      公司规则（排除品类/国家）
│   ├── country_policy.py 国家政策包
│   ├── suppression.py   抑制名单
│   ├── approval.py      审批状态
│   ├── idempotency.py   幂等
│   └── rate_limit.py    频率与配额
└── handlers/             工具执行器注册处（实现调 connectors/）
    ├── email_send.py     Gmail 单封发送参数组装与恢复搜索
    ├── email_feedback.py Gmail typed 反馈页的一次性进程内交接
    ├── contact_enrichment.py  联系人候选 typed 补全
    └── contact_verification.py 邮箱可达性缓存与 typed 验证
```

## 加新工具 = manifest + handler，不改管线

这是四个插件点之一。任何「加个工具要改 pipeline.py」的做法都是设计违规。
`idempotency=NONE` 的通用 technical-claim 生命周期属于既有管线能力，不得在其中按
具体 tool_id 分支；新只读工具仍只能增加 manifest + handler。

## Stage 顺序不可随意调换

便宜且否决率高的在前，贵的在后；幂等必须在执行前、记账后：

通用 manifest 的全序是：

```text
tenant → permission → playbook → country_policy → suppression
→ approval → idempotency → rate_limit
```

Phase 1 的 `email.send` 只启用六个与发送相关的 stage，精确顺序是：

```text
tenant → permission → suppression → approval → idempotency → rate_limit
→ 提交 EXECUTING 证据 → Gmail → Outreach 完成 Attempt → 完成 canonical tool_call
```

所有邮件发送仍是 `RiskLevel.HIGH`。HIGH manifest 未显式声明 stage profile 时按
`customer_outbound` 处理，并继续强制上述六阶段。唯一已批准的另一 profile 是
`internal_transactional`，只供 notification worker 向内部员工发送固定模板事务通知：

```text
tenant → permission → idempotency → rate_limit
```

该 profile 必须显式声明、强制 `REQUIRED` 幂等，且 checks 必须与四阶段精确相等；
LOW/MEDIUM 工具不得声明它。它不适用客户 suppression 与 Campaign approval，是因为收件人
不是客户且内容来自固定内部模板，不代表降低风险级别。不得把它用于任意正文、客户邮件、
Campaign 触达或绕过审批；新增 profile 也不得在 `pipeline.py` 按 tool_id 分支。

顺序错了会出现「重复扣费但没发送」或更糟的「重复发送」。

## 拒绝必须结构化

任一 stage 拒绝即终止，返回**结构化原因**（哪个 stage、什么规则、能否补救）。Agent 需要知道「为什么不行」才能换办法，而不是重试同一个调用。裸异常会被当成临时故障重试——那正是最不该发生的。

## 依赖白名单

```text
允许   shared.*、domains 的 service.py 显式接口（判权与查名单）、connectors.*
禁止   domains 的 models.py / repository.py
禁止   在本模块写业务规则——「该不该发」在域里判断，这里只查「能不能发」
```

## 持久 ledger 与审计

迁移 `0010` 提供 tenant-scoped `tool_calls` 与 append-only
`tool_call_events`。ledger 只允许安全 ID、固定分类、时间、耗时、成本说明、
provider reference 与 HMAC 请求指纹；**不得持久化邮箱地址、主题、正文、退订链接、
OAuth token、完整请求或异常文本**。请求 payload/preflight 只活在单次进程内。

`email.feedback.fetch` 是 LOW/FREE read 工具，只启用 `tenant → permission`，且
`idempotency=NONE`。Gateway 仍用每次调用唯一的内部 technical claim 驱动 durable
`RECEIVED → EXECUTING → SUCCEEDED/FAILED` 状态机；这个 claim 不构成调用者幂等语义，
不能用于跨调用复用结果。判权通过后 trusted reader 才构造并配置 Gmail Connector。
typed page 只进入容量一的进程内 `FeedbackPageSlot`，ledger 只能保存一次性 `fpg_`
handle；`take()` 后立即删除，失败或 cancellation 必须清空。
这个 handle 不是业务数据引用：不能从数据库恢复 page，也不能让 Agent/模型读取 page。
原始 MIME、header、地址、provider cursor 与 typed page 都不进入 ledger；Connector 返回的
page 只能在同一调用栈内由受信 worker 消费。

`contact.enrich` 与 `contact.verify` 使用各自容量一、async-task-local、领取即删除的 typed
结果槽；ledger 只保存不可恢复的 `ceb_` / `veb_` handle。邮箱、姓名、职位、source URI、
Hunter 原始响应和 API Key 不得进入 ledger、outbox、日志或模型上下文。Provider 的
`score` / `confidence` 在 connector 边界直接丢弃，绝不能当作业务置信度。

`contact.verify` 对四种结果都保存检查时间和固定成本备注，并仅在
`now < checked_at + 30 days` 时命中缓存；缓存命中不解析凭证、不调用 Hunter、也不预留
Provider 配额。付费调用出现结果不确定时固定进入 `reconciliation_required`，禁止自动重试。
隐私声明只作为 typed 事实交给后续 workflow，handler 不写 Prospecting，也不自行执行删除。

每次调用（含拒绝与重复）都追加结构化 event。进入 Connector 前必须先提交
`EXECUTING` 和对应事件；这笔写入失败时不得调用 Gmail。完成 Attempt 或 canonical
ledger 失败时也不得伪造成功。**审计写入失败必须阻断动作本身**——审计不完整时
继续发客户邮件是合规裸奔。

HMAC 指纹 key 由运行时密钥解析器提供，只记录 key version。key 轮换时，旧版本在
ledger 保留期内必须仍可核验；否则相同幂等键只能固定报冲突，不能猜测或重算成新请求。

## Gmail 不确定结果恢复

`CLAIMED` 的 lease 过期且尚未提交 `EXECUTING` 时，可以重新跑**当前事实**与六个
stage。只要已提交 `EXECUTING`，或 Connector 返回“可能已经写入”，canonical 调用
就进入 `failed_transient / reconciliation_required`：后续只允许按确定性
`Message-ID` 与 `X-TradeOS-Idempotency-V1` 搜索，禁止再次调用 send。

**一次 Gmail 搜索未命中不等于邮件确定未发送；只要 Connector 可能已开始，系统不得自动重发。**

搜索命中时，先按原 provider reference 完成 Outreach Attempt，再完成 canonical
ledger；未命中继续保持人工对账；provider reference 不一致固定冲突，绝不覆盖原值。
只有 Connector 明确给出 `definitely_not_sent` 的临时失败，才可在 lease 到期后重新跑
当前事实；此时新增抑制、回复、身份或额度变化都可以停止重试。

## Phase 1 范围

manifest 注册表、两种显式 HIGH stage profile、固定 stage 编排、Postgres canonical
ledger、append-only event、`email.send` 客户邮件 handler、`notification.email.send` 内部
固定模板事务通知 handler、`email.feedback.fetch` typed 只读 handler、Hunter 联系人插件的
离线实现、租约恢复与人工对账边界。Hunter 测试不使用真实 Key 或网络；生产
`contact.enrich` 尚未注册，必须先配置真实国家政策包与 Playbook composition。账户发现
持久化/workflow、Campaign 接线和 UI 仍未完成。成本钱包仍是 Phase 3 挂载点；不在本阶段
实现自动对账扫描器、对账 UI、回复正文 worker、自动重发或多 Provider 路由。
