# Tool Gateway

**所有对外部世界的动作只能经过这里。** 模型和 Agent 永远拿不到数据库密码、邮箱
Token、浏览器 Cookie 或其他凭证（硬边界 1）。Gateway 不是一个转发函数，而是业务域与
Connector 之间最后一道独立、可恢复、可审计的闸门。

本页只描述当前已经实现并由测试锁定的 Phase 1 Gmail 客户单封发送、内部员工固定模板
事务通知、typed DSN 反馈读取，以及 Hunter 联系人补全/邮箱验证插件。Hunter 部分只有
无真实 Key、无真实网络的协议与边界测试，尚未接入生产 composition。自动对账扫描器、
人工对账 UI、回复正文/投诉 worker 与其他工具仍是后续能力，不能按已实现能力对外承诺。

---

## 一、已实现调用路径

```text
API / Worker
      │ 只传 typed ID 与幂等键；正文只在本次进程内存中
      ▼
Outreach 当前事实快照
      │ Campaign → Enrollment → Attempt 锁序
      ▼
Tool Gateway email.send 六阶段
      │ tenant → permission → suppression → approval
      │        → idempotency → rate_limit
      ▼
提交 canonical tool_call = EXECUTING + append-only event
      ▼
GmailConnector.send_once
      │ 先按确定性 header 搜索，未命中才发送
      ▼
Outreach 完成 MessageAttempt
      ▼
完成 canonical tool_call = SUCCEEDED
```

任一检查拒绝即停止，并返回固定 typed category。Connector 只接收检查完成后的
`DeliveryMaterial`，凭证在应用 composition 内通过密钥解析器配置；不进入模型上下文、
数据库、日志或异常消息。

`email.send` 的精确六阶段为：

1. `tenant`：调用、Actor、Campaign、Attempt 与全部 Provider 结果同租户。
2. `permission`：API 与网关分别判权，不信任客户端 role/scope header。
3. `suppression`：重新检查 contact/account 全局抑制与当前回复。
4. `approval`：Campaign 精确版本审批仍有效，商业承诺仍需逐次审批。
5. `idempotency`：HMAC 指纹、canonical claim、重复/冲突判定。
6. `rate_limit`：使用 Sending Identity 真实额度做原子 reservation。

通用 manifest 仍保留
`tenant → permission → playbook → country_policy → suppression → approval → idempotency → rate_limit`
全序；具体工具只能按该顺序选子集。Phase 1 的 `email.send` 没有启用 playbook 与
country-policy stage，文档不得把未执行的检查写成已经执行。

### HIGH stage profile

发送邮件一律记为 `RiskLevel.HIGH`。HIGH manifest 未显式指定 profile 时归一化为
`customer_outbound`，继续要求 `email.send` 的六个强制阶段；这保持既有 manifest 行为。

`internal_transactional` 是显式、窄化的 HIGH profile，只允许 notification worker 给内部
员工发送固定模板事务通知。它精确要求：

```text
tenant → permission → idempotency → rate_limit
```

并且必须使用 `IdempotencyRequirement.REQUIRED`。这类收件人不是客户，内容也不属于
Campaign，因此不运行客户 suppression 或 Campaign approval；审计风险仍是 HIGH。该
profile 不接受缺段、多段或乱序，LOW/MEDIUM manifest 也不能声明。它不得承载任意正文、
客户可见内容、Campaign 触达或商业承诺，不能成为普通邮件旁路；Gateway pipeline 仍只按
manifest stages 通用编排，不得增加 tool-id 特判。

生产 notification worker 必须完整配置专用 TRANSACTIONAL Sending Identity、员工 recipient
directory 与 connector/fingerprint secret references。配置全缺或部分缺失均在启动期固定
失败；direct config 的 `email=None` 也必须在 runtime context body 和首次 job claim 前拒绝，
不能隐式退化成站内-only 后把邮件渠道持久化为终态 rejected。

---

## 二、两个持久状态机

### Tool Gateway canonical ledger（迁移 0010）

`tool_calls` 是 tenant-scoped 的 canonical 调用行，`tool_call_events` 是 append-only
阶段证据：

```text
received → rejected
received → claimed → executing → succeeded
                         │
                         ├→ failed_transient
                         └→ failed_permanent
同一已完成请求的新调用 → duplicate
```

精确状态词表：

```text
received / claimed / executing / succeeded / rejected / duplicate /
failed_transient / failed_permanent
```

`claimed`、`executing` 与 `failed_transient` 都带有 lease owner/expiry；只有同租户、同
tool、同幂等键、同 HMAC 请求指纹才能复用 canonical 行。相同键但指纹不同是
`idempotency_conflict`，绝不覆盖旧请求。

### Outreach MessageAttempt（迁移 0011）

```text
reserved → sending → sent
    │          │
    └──────────┴→ failed_transient / failed_permanent
```

`send_claimed_at` 证明 Outreach 已把 Attempt 线性化为 `sending`。它不是可清理的临时位：
Connector 一旦可能开始，不能把它回退成 `reserved` 来重发。`sent` 保存 provider ref；
失败只保存固定 typed category。

### 锁顺序

为避免并发发送与死锁，锁顺序固定：

```text
Campaign → Enrollment（批量按 ID 排序）→ MessageAttempt
→ Sending Domain → Sending Identity → Daily Reservation
```

Tool ledger 使用独立 tenant-scoped UoW；进入 Connector 前，canonical `EXECUTING` 与
append-only event 必须已经提交。Outreach 与 ledger 无法成为一个跨域数据库事务，因此
恢复协议必须能处理其中任一步提交后进程崩溃。

---

## 三、安全持久化边界

Gateway **不记录完整入参或结果**。`tool_calls` 只允许：

- tenant/tool/call/run/user/campaign/message-attempt 等安全 ID；
- tool version、risk/cost class、固定状态与错误分类；
- idempotency key、64 位小写十六进制 HMAC 请求指纹及 key version；
- lease、attempt count、时间、provider ref、Retry-After；
- duplicate 指向的 canonical call ID。

append-only event 只允许 stage、outcome、rule、category、actor ID、安全关联 ID、时间、
耗时与固定成本说明。

以下内容不得进入两张表、日志、审计 extra 或异常文本：

```text
邮箱地址、主题、正文、退订 URL、客户原话、OAuth token、Authorization header、
完整 HTTP 请求/响应、DSN、凭证引用解析结果、原始 provider 异常
```

数据库 CHECK 约束同时限制安全 label、provider ref、状态字段组合与错误词表；
`tool_call_events` 由触发器保证 append-only。应用层验证不是数据库约束的替代品。

HMAC key 只在运行时由 Secret Resolver 解析。ledger 只存 `fingerprint_version`。轮换时，
旧 key version 在 ledger 保留期内必须仍可核验；如果运行时无法核验旧版本，应固定报告
幂等冲突并停止，不能用新 key 重算后假装是同一请求。

---

## 四、Gmail 确定性发送

`GmailConnector.send_once` 在发送前先搜索两个确定性 header：

```text
Message-ID: <HMAC 派生的确定性 RFC 5322 ID>
X-TradeOS-Idempotency-V1: <确定性幂等摘要>
```

邮件同时带：

```text
List-Unsubscribe: <安全退订 URL>
List-Unsubscribe-Post: List-Unsubscribe=One-Click
```

搜索命中时直接返回现有 provider ref，未命中才调用 Gmail send。Connector 将失败分为
固定交付确定性：

| 情况 | category | 自动再次 send |
|---|---|---|
| 401/403 | `provider_auth_required` | 否，人工恢复授权 |
| 429 | `rate_limited` | 仅按 Retry-After 重新跑当前事实 |
| 参数/永久 provider 错误 | `provider_permanent` | 否 |
| 传输失败且明确未写入 | `provider_transient` | 可在 lease 后重新跑当前事实 |
| 超时/错误且可能已写入 | `reconciliation_required` | **绝不允许** |

provider 原始异常不向外传播；Gateway/API 只暴露固定 category 和有界 Retry-After。

### Gmail typed 反馈读取

`email.feedback.fetch` 是 LOW/FREE read，只运行 `tenant → permission`，不具备调用者幂等
语义。Gateway 先提交 technical claim 的 `EXECUTING` 证据；Gmail Connector 只解析严格
RFC 3464 DSN，随后把 typed page 放入容量一的进程内 slot，并在 ledger 只保存一次性
`fpg_` handle。worker `take()` 后 handle 立即失效；失败或取消必须清空。

typed page、原始 MIME/header/address、OAuth token 和 provider cursor 都不得进入 ledger、
日志或模型上下文。page 交给 `workflows/email_feedback` 后，在 tenant＋mailbox advisory
transaction lock 内做整页 fingerprint preflight、Outreach/Sending Identity 业务效果、
receipt/quarantine/action/outbox 与 cursor 提交；任一步失败整页回滚。

### Hunter 联系人补全与邮箱验证

`contact.enrich` 运行
`tenant → permission → playbook → country_policy → suppression → rate_limit`；
`contact.verify` 运行 `tenant → permission → suppression → rate_limit`。两者都是
`idempotency=NONE` 的付费读取，每次调用仍由 technical claim 留下 durable 状态证据。

PII 只存在于 repr-disabled typed DTO、prepared payload 和 async-task-local 容量一 slot；
成功 ledger 只保存领取一次即失效的 `ceb_` / `veb_` handle。Hunter `score`、`confidence`
与原始 JSON 在 connector 边界丢弃。所有邮箱验证结果（verified / invalid / risky /
unverified）都带 UTC 检查时间和固定成本备注，并在严格小于 30 天时复用；缓存命中不创建
connector、不解析 Key、不调用 Hunter，也不占 Provider 配额。

不确定的付费结果映射为 `reconciliation_required`，不得自动重试。451 隐私声明保留为
typed `privacy_claimed` 事实，由后续账户发现 workflow 决定持久化或删除；Gateway handler
不直接写业务域。当前测试全部使用受控 transport，没有真实 Hunter Key/网络。

---

## 五、崩溃与人工对账

最重要的操作规则是：

> **一次 Gmail 搜索未命中不等于邮件确定未发送；只要 Connector 可能已开始，系统不得自动重发。**

恢复决策树：

```text
canonical 仍为 CLAIMED，且没有已提交 EXECUTING？
├─ 是：lease 过期后重新读取当前事实并跑六阶段；安全时可执行首次 send
└─ 否：已经 EXECUTING 或 error=reconciliation_required
       └─ 只调用 reconcile_once（只搜索，永不 send）
          ├─ 命中确定性 Message-ID/header
          │  ├─ provider ref 与既有证据一致：完成 Attempt，再完成 canonical ledger
          │  └─ provider ref 不一致：固定冲突，保留原证据，人工处理
          └─ 未命中：继续 reconciliation_required，人工处理，禁止自动重发
```

如果 Connector 明确证明 `definitely_not_sent`，canonical 可以进入普通
`provider_transient`。lease 到期后的重试仍要重新读取 Campaign、Enrollment、回复、
抑制、审批、身份与额度；新增抑制或回复必须能阻止发送。

关键故障语义：

- `EXECUTING`/event 提交失败：Gmail 调用次数必须为 0；lease 后可安全重试。
- Gmail 可能写入后进程崩溃：状态进入人工对账，Gmail 不再 send。
- Gmail 成功但 Outreach Attempt completion 失败：搜索找到原信后补 Attempt，不能重发。
- Attempt 已完成但 canonical completion 失败：搜索并核对 provider ref 后补 ledger。
- append event 或任一 completion 失败：状态与错误必须诚实，禁止输出成功。
- provider ref mismatch：不覆盖已持久证据，不拼接原始响应，不自动选择赢家。

Phase 1 只有被动恢复协议与测试工具，没有后台自动扫描器和对账 UI。运维人员必须根据
ledger 与 Gmail 受限搜索结果显式裁决。

---

## 六、可观测性与告警

至少监控以下 tenant-scoped 指标：

- `claimed` / `executing` / `reconciliation_required` 数量与最老 age；
- attempt count、lease age、Retry-After；
- Gmail 搜索命中/未命中；
- provider auth/rate/transient/permanent 分类计数；
- `ledger.executing`、Outreach completion、canonical completion 的提交失败计数；
- duplicate 与 idempotency conflict 数量。

建议告警：

- `executing` 超过正常发送时长；
- 任一 `reconciliation_required` 超过人工响应 SLA；
- 同一 tenant/provider auth 连续失败；
- provider ref mismatch；
- ledger 或 Attempt completion commit 失败。

日志只写固定中文消息和安全 ID/category/stage/tenant/count/age，不写正文、地址、URL、
异常字符串或 traceback。审计写入失败必须阻断 Connector；不能为了“可用性”绕过证据。

---

## 七、插件与后续范围

新增工具仍遵循“manifest + handler + composition 注册”，不把业务规则写进 Connector。
检查 stage 属于 Gateway 编排，业务事实属于域服务，外部 SDK 只在 Connector。

当前实现范围：

- `email.send` 六阶段；
- Postgres canonical ledger 与 append-only event；
- Outreach Attempt claim/completion；
- Gmail 单封发送、确定性 header、只读恢复搜索；
- Gmail RFC 3464 typed 反馈读取、一次性 page handle 与真实 feedback worker；
- Hunter 单 Provider connector、联系人补全/邮箱验证 handler 与一次性 typed handle；
- API 手工发送入口、离线 controlled-transport 演示与真实 PostgreSQL 恢复测试。

当前明确不做：

- 自动对账扫描器与对账 UI；
- Gmail 回复正文、投诉 FBL、标签、DNS worker；
- 多渠道 Outreach；
- Browser Agent 发送邮件；
- 接受任意旧 approval 或绕过 Campaign current-facts；
- 自动重发任何交付结果不确定的邮件；
- 生产注册 `contact.enrich`（真实国家政策包与 Playbook composition 尚未配置）；
- account-discovery 持久化/workflow、Campaign 接线、联系人 UI 与多 Provider 路由；
- Phase 3 成本钱包。

浏览器工具未来仍必须遵守“官方 API → 公开 HTTP → 确定性 Playwright Adapter → 受限
Browser Agent → 人工接管”的降级顺序，并受同一凭证、tenant、审计与幂等边界约束。
