# Slice 4B2：Tool Gateway 与 Gmail 单封手动发送设计

> 日期：2026-08-11
> 状态：对话设计已分节批准，待书面规格复核
> 适用阶段：Phase 1 需求验证闭环
> 前置基线：Slice 4B1 已完成 Campaign、Enrollment、MessageAttempt 与全局抑制

## 一、目标与完成定义

本切片把 Slice 4B1 产生的 `MessageAttempt` 接到真实的外部发送边界。员工通过受保护的 API 对一条已准备的 Attempt 发起单封邮件发送；请求依次通过 Tool Gateway 的当前事实检查、持久化幂等账本、发件身份原子额度预留和 Gmail Connector，最后由 Outreach 公共服务记录发送结果。

本切片完成时必须同时满足：

1. 配置完整的应用可以调用 Gmail 发送一封真实邮件；默认未配置应用仍然 fail closed。
2. `MessageAttempt` 只是发送准备事实，不是发送授权。实际外发前必须重新检查 Campaign、精确版本审批、Enrollment、回复、抑制、联系方式资格、发件身份和额度。
3. 同一业务动作在并发、网络超时、进程崩溃和数据库完结失败后都不会被系统自动重复发送。Gmail 结果不确定时宁可停在待对账，也不以一次搜索未命中作为重发依据。
4. Gmail 已经成功但本地状态未完成时，系统只能进入对账恢复，不能谎称邮件未发送。
5. 含价格、折扣、交期、库存、付款条件、合同、认证、独家代理或质量保证等承诺的内容不能借 Campaign 审批绕过逐次审批。
6. 数据库、审计、日志和异常中都不得保存或回显收件地址、主题、正文、OAuth Token、完整请求头或 DSN。
7. 所有数据库查询 tenant-scoped，所有跨域调用只经过对方 `service.py` 的公共契约。

### 1.1 本切片不做

- Campaign 自动扫描、定时序列推进和批量发送；这些属于后续 Workflow 切片。
- 发送确认页、状态页或其他 UI。后续 UI 进入独立 creative-production 设计流程。
- 自动接受逐次审批事项。现有 Approvals 实现不足时一律拒绝，不以“员工点击发送”冒充审批。
- Gmail 回复拉取、退信与投诉回流的完整 Worker。Connector 保留公共能力，回流消费在后续切片实现。
- Playbook、国家政策包和 Phase 3 成本钱包的完整实现。
- Gmail 之外的邮件 Provider。

## 二、方案选择

采用**同步 Tool Gateway + 持久化调用账本**。

调用方同步得到成功、结构化拒绝、限流、临时失败或待对账结果；外部网络调用前后均有 durable 状态。该方案适合 Phase 1 的单封人工发送，能在不提前引入完整消息队列和扫描器的情况下关闭最危险的重复发送窗口。

未采用的方案：

- **全异步 Outbox/Worker**：长期更适合批量发送，但会把本切片扩成调度、租约扫描、队列监控和 UI 状态订阅，超出单封人工发送目标。
- **数据库事务跨 Gmail 网络调用持锁**：表面顺序简单，但长事务、连接占用、死锁和不确定网络时延会扩大故障面；外部副作用仍然无法随数据库回滚。

## 三、模块边界

### 3.1 `domains/outreach`

继续拥有 Campaign、精确版本审批绑定、Enrollment、回复状态、联系方式资格、全局抑制和 MessageAttempt 生命周期。

本域新增两项公共契约（实施计划固定精确签名）：

- 只读 `preflight_message_send(...) -> MessageSendPreflight`，用于 Gateway 各 stage 读取安全资源上下文；
- 写侧 `claim_message_send(...) -> MessageAttemptView`，在短事务内重新验证全部当前事实，并把 Attempt 从 `RESERVED/FAILED_TRANSIENT` 原子推进为 `SENDING`。

`MessageSendPreflight` 只含安全 ID、版本、步骤和幂等键，不含地址或正文。Gateway 不得自行复制这些业务判断。

`SENDING` 是本切片新增的持久化状态，表示业务已批准这次确定的发送动作进入外部执行边界，不等于 Gmail 已成功。Attempt 保存 `send_claimed_at`，但不保存 Tool Gateway 上层类型或外部请求内容。相同 Attempt 的重复 claim 幂等，不同资源绑定固定冲突。

Attempt 的发送边固定为：

```text
RESERVED / FAILED_TRANSIENT → SENDING
SENDING → SENT / FAILED_TRANSIENT / FAILED_PERMANENT
SENT / FAILED_PERMANENT → 无后继
```

`reconciliation_required` 不推进 Attempt，保持 `SENDING`；事实尚不确定时不能写成失败。

Outreach 的 Campaign/Enrollment/Attempt 写操作和 suppression 写入必须使用一致的 Campaign → Enrollment（按 ID 升序）→ Attempt 锁顺序。这样 suppression 与最终 claim 能在 PostgreSQL 内串行。Outreach 不得导入 Gmail、Tool Gateway 或 Sending Identity 内部实现。Outreach 的 `claim_message_send`、`record_sent` 和 `record_send_failure` 是 Attempt 发送状态的唯一写入口。

### 3.2 `domains/sending_identity`

`reserve_send_slot(...)` 是发送额度的唯一权威写入口。Gateway 使用 MessageAttempt 的同一 `IdempotencyKey` 预留额度；重复调用返回原快照，不重复计数。

发件地址通过公共 `IdentityView` 读取。Gateway 不读取 Sending Identity 的 models、repository 或 ORM row。

### 3.3 `tool_gateway`

拥有工具注册表、固定检查顺序、结构化拒绝、持久化 invocation/claim、租约、执行审计和错误分类。

Gateway 可以导入域的 `service.py` 公共接口，不得导入域内部模型或仓储。业务事实仍由域服务判断；Gateway 只编排“当前是否可以执行”和“如何安全执行一次”。

### 3.4 `connectors/gmail`

拥有 Gmail OAuth、协议转换、稳定幂等 Header、发送前查询和 HTTP/网络错误分类。Connector 不决定业务权限、不查询业务表、不修改 Outreach 状态。

OAuth 只以 `GMAIL_OAUTH_TOKEN_REF` 形式进入 Connector 的 secret resolver。Token 不进入构造参数之外的业务上下文，不落日志、不进返回值、不进入异常文本。

### 3.5 `apps/api`

只负责依赖装配、身份断言、HTTP DTO 和错误映射。新增受现有 tenant/identity middleware 保护的应用入口：

```text
POST /crm/message-attempts/{attempt_id}/send
```

客户端请求只包含 `subject` 与 `body`。以下字段由服务端从当前事实与配置解析，客户端不得提供或覆盖：

- tenant、employee actor；
- Campaign、Enrollment、Contact Point、Account；
- 发件身份与发件地址；
- 收件地址；
- Tool ID、Tool version、幂等键；
- 退订 URL；
- approval 状态与 provider reference。

运行时必须显式注入：

- Outreach 公共服务；
- Sending Identity 公共服务；
- `DeliveryMaterialProvider`：按 tenant + Contact Point 解析当前收件材料，失败时固定拒绝；
- `UnsubscribeLinkProvider`：生成与当前 tenant/contact/account 绑定的退订链接；
- Tool Gateway、Gmail Connector、secret resolver 和时钟。

上述任一依赖未配置时发送端点返回固定 503，不构造空 registry、不使用环境变量默认收件人、不允许请求体补齐缺失依赖。

`DeliveryMaterialProvider` 与 `UnsubscribeLinkProvider` 是应用装配协议，不向 Agent 暴露。Gateway 审计只看到安全引用和指纹；原始地址与 URL 只在当前执行内存中传给 Gmail handler。

同一 Attempt 的退订链接必须稳定且可重复解析，不能每次调用生成随机 token 或把当前时间写入 URL；否则同一业务动作会得到不同指纹。签名 key 轮换时须保留仍有效 token 的旧验证 key。Contact Point 的实际地址若发生变化，必须形成新的已验证 Contact Point/Attempt，不允许旧 Attempt 静默改发到新地址。

### 3.6 `workflows`

本切片不扫描或自动创建发送任务。后续 Outreach Campaign Workflow 只能消费已经准备的 MessageAttempt，并复用本切片同一 Gateway 入口，不能新增旁路发送器。

## 四、工具契约

### 4.1 Gmail 发送工具

Tool ID 固定为 `email.send`，风险为 `HIGH`，幂等要求为 `REQUIRED`。Phase 1 manifest 使用以下六段：

```text
tenant → permission → suppression → approval → idempotency → rate_limit
```

`playbook`、`country_policy` 仍保留在全局 `STAGE_ORDER` 和插件点中，但不加入本工具的 Phase 1 manifest：

- Campaign 精确版本边界承担当前 playbook 约束；
- Contact Eligibility 的合法依据、国家与主体事实承担当前发送资格约束；
- 当组织级 playbook 与国家政策包有正式实现后，再通过 manifest 显式加入，仍无需修改 pipeline。

`cost` 只保留安全的 cost class，不在 Phase 1 扣积分。

### 4.2 内存请求与持久化投影

`EmailSendRequest` 在当前进程内携带：

- `attempt_id`；
- 员工提交的 subject/body；
- MessageAttempt 的 `IdempotencyKey`。

为避免在额度预留后才发现材料无效，`ToolHandler` 的通用插件契约分为两个方法：

```text
prepare(context) -> PreparedToolCall
execute(prepared) -> safe output
```

`prepare` 只读且不得调用产生外部副作用的 Connector。Gmail handler 在这里通过服务端 providers 解析发件地址、收件地址和 unsubscribe URL，验证资源绑定，并返回：

- 仅存活于当前进程的 ephemeral payload；
- 安全 audit projection；
- 稳定 request fingerprint。

Gateway 在 approval 之后、idempotency 与 rate-limit 之前调用 `prepare`。任何材料错误或 provider 不可用都在 claim/额度预留前失败。

数据库与审计只保存：

- Attempt、Campaign、Employee、Run 等安全 typed ID；
- Tool ID/version；
- 幂等键；
- 规范化请求的 HMAC-SHA-256 指纹及 fingerprint key version；
- subject/body/address 的长度或存在性等安全元数据，不保存原值；
- 安全 provider reference、错误分类和时间戳。

请求指纹使用无歧义的长度前缀编码，覆盖所有会改变外部副作用的字段：规范化地址、subject 的原始 UTF-8、body 的原始 UTF-8、稳定 unsubscribe URL、Attempt、Tool version 和幂等 Header version。不能用简单字符串拼接，也不能在指纹时丢弃正文空白而发送时保留。相同幂等键但任一外发字段不同即为 `idempotency_conflict`。

指纹使用应用 secret resolver 提供的稳定 key，不使用普通地址哈希，避免离线枚举收件地址。Key 只以 `TOOL_CALL_FINGERPRINT_KEY_REF` 引用，绝不进入模型、上下文、日志或数据库。轮换时必须保留仍在 ledger retention 内的旧版本用于比较；不能因换 key 让历史重试失去幂等性。

指纹只能用于一致性比较，不能作为地址或内容的展示、查找接口。

### 4.3 内容审批边界

Approval stage 调用 `domains.quotations.service.contains_forbidden_commitment` 的确定性公共函数。该函数以规则优先、默认拒绝的方式识别必须逐次审批的商业承诺。

规则至少覆盖货币/价格/折扣/比例、库存、交付日期或时长、认证、付款条件、合同、独家和保证表达；无法明确归类的商业数字或承诺句式按需审批处理。模型判断只能增加命中项，不能清除确定性规则的命中。测试必须使用中英文、大小写、空白、标点与常见改写的独立词表，不能只复制实现正则。

本切片不接受 approval reference 来自动放行这些内容。命中任一禁止承诺时返回固定 `approval_required`，不调用 Gmail、不预留发送额度。后续 Approvals 服务完成后，可以在不修改 Gateway 顺序的前提下扩展为验证具体 approval package。

安全的 Campaign discovery 邮件仍需当前 Campaign 精确版本已获批，并必须包含有效退订链接。

## 五、持久化模型

在当前 Alembic head 后增加下一条迁移，创建 `tool_calls` 与 `tool_call_events`。迁移编号由实施计划在读取当时 head 后确定，不在设计中硬编码。

### 5.1 `tool_calls`

一行表示一次 invocation；成功取得幂等 claim 的行同时是该业务动作的 canonical ledger。

核心字段：

- `tenant_id` + `tool_call_id` 复合主键；
- `tool_id`、`tool_version`、`risk_level`、`cost_class`；
- `idempotency_key`，只有 canonical claim 行非空；
- `request_fingerprint`；
- `fingerprint_version`；
- `status`；
- `duplicate_of`，重复 invocation 指向 canonical call；
- `lease_owner`、`lease_expires_at`、`attempt_count`；
- `run_id`、`user_id`、`campaign_id`、`message_attempt_id`；
- `provider_ref`；
- `error_category`、`retry_after_at`；
- `created_at`、`updated_at`、`completed_at`。

约束：

- 所有业务索引以 `tenant_id` 为首列；
- canonical 行唯一键为 `(tenant_id, tool_id, idempotency_key)`，仅对非空 key 生效；
- `RECEIVED` 或在 prepare 前被拒的 invocation 可以没有 fingerprint；任何 canonical claim 必须同时拥有 key、fingerprint 与 fingerprint version；
- duplicate invocation 不持有 canonical key，只以 tenant-scoped `duplicate_of` 指向 winner；
- provider ref 使用现有安全词表、长度和控制字符约束；
- 状态相关字段由 CHECK 保证一致；
- 表中不存在 params、recipient、subject、body、headers、token、DSN 或异常原文列。

### 5.2 `tool_call_events`

追加式记录 invocation 的阶段与结果：

- tenant、event ID、tool call ID；
- stage、outcome、固定 rule/category；
- actor/run/campaign/attempt 等安全引用；
- occurred_at、整数毫秒 duration 与安全 cost note。

通过数据库 guard 禁止 UPDATE/DELETE。事件表使用 tenant + tool_call 复合外键，禁止跨租户关联。

### 5.3 状态机

为兼顾“所有调用都审计”和“被抑制后可在事实合法变化时重新尝试”，invocation 先进入 `RECEIVED`，只有通过 idempotency stage 的行才写 canonical key：

```text
RECEIVED
  ├─ REJECTED
  ├─ DUPLICATE
  └─ CLAIMED
       ├─ FAILED_TRANSIENT → 重新领取 → CLAIMED
       ├─ FAILED_PERMANENT
       └─ EXECUTING
            ├─ FAILED_TRANSIENT → 重新领取 → EXECUTING
            ├─ FAILED_PERMANENT
            └─ SUCCEEDED
```

`RECONCILIATION_REQUIRED` 是 `FAILED_TRANSIENT` 的固定错误类别，不另造模糊状态。

在 idempotency stage 之前被拒的 invocation 不占用 canonical key；修复联系方式、审批或抑制事实后可以使用同一业务键重新检查。已经 CLAIMED 的动作由 canonical ledger 决定重复、冲突和恢复。

## 六、发送数据流

1. API middleware 从受信 header 与配置得到 tenant、employee identity；请求体只解析 subject/body。
2. 应用层读取 Attempt 安全视图，生成 ToolCallContext；未知、跨租户或非 prepared/retryable Attempt 固定拒绝。
3. Gateway 校验 registry、manifest 与输入类型，创建 `RECEIVED` 审计行并提交。审计创建失败时终止。
4. Tenant 和 permission stage 验证 actor、tenant 及 Attempt/Campaign/Enrollment scope。
5. Outreach `preflight_message_send` 重新读取当前事实：
   - Campaign 为 active；
   - Enrollment 绑定版本等于 Attempt 版本，且该精确版本当前审批有效；
   - Enrollment 仍活跃、步骤匹配、尚未回复；
   - Contact Point 与 Account 都未抑制；
   - 联系方式仍 verified、具备合法依据并属于目标 Account；
   - Attempt 绑定的发件身份仍属于 Campaign 允许集合。
6. Approval stage 检查 subject/body；命中商业承诺固定拒绝。
7. Gmail handler 的只读 `prepare` 解析当前收件地址、发件地址和退订链接，生成 ephemeral payload、安全 projection 与 HMAC 指纹。任何资源、tenant 或绑定不一致均 fail closed；该阶段不得解析 OAuth 或调用 Gmail。
8. Idempotency stage 原子写 canonical key：
   - 已完成且指纹相同：当前 invocation 记 `DUPLICATE`，返回原安全结果；
   - 运行中且租约未过期：返回 `in_progress`；
   - key 相同而指纹不同：固定 `idempotency_conflict`；
   - 无记录或可恢复：取得 claim/lease。
9. 调用 Outreach `claim_message_send`。该短事务锁定 Campaign、Enrollment 与 Attempt，重新验证步骤 5 的全部当前事实，并与 suppression 写入按同一资源锁序串行。claim 失败时不调用 Gmail，也不预留 Sending Identity slot。
10. Rate-limit stage 使用同一 MessageAttempt 幂等键调用 `reserve_send_slot(..., for_cold_outreach=True)`。Campaign quota 已在 prepare Attempt 阶段预留，这里不得重复扣 Campaign quota。额度竞态或暂满时 Attempt 记为 `FAILED_TRANSIENT/RATE_LIMITED`，canonical call 记为 `FAILED_TRANSIENT/rate_limited`；下一窗口重试必须从 Outreach claim 重新检查全部当前事实。
11. 额度成功后，在 Gmail 前提交 ledger `EXECUTING` 状态和 `execution_accepted` 事件。该提交失败时 connector 零调用；恢复时 ledger 仍为 `CLAIMED`，因此能证明 Connector 未开始。已经预留的 slot 由相同幂等键复用，不重复计数。
12. Gmail 只在 `execute` 中解析 OAuth，并先按稳定 Header 查询：
    - 找到既有邮件，返回其安全 message ref；
    - 未找到时发送一次。
13. 调用 Outreach `record_sent`，把 `SENDING` 推进为 `SENT`，然后完结 canonical call 为 `SUCCEEDED`。重复 `record_sent` 必须接受相同 provider ref，拒绝不同 ref。
14. Outreach 增加并固定失败类别：
    - `RATE_LIMITED`、`PROVIDER_TRANSIENT`、`PROVIDER_AUTH_REQUIRED` → `FAILED_TRANSIENT`；
    - `PROVIDER_PERMANENT`、`IDENTITY_UNAVAILABLE` → `FAILED_PERMANENT`；
    - 只有 `IDENTITY_UNAVAILABLE` 会停止 Enrollment；
    - `reconciliation_required` 不调用 `record_send_failure`，Attempt 保持 `SENDING`。

    Connector 或额度失败后，Gateway 使用上述 typed 类别回写 Attempt，再完结或保留 canonical ledger；不得把不同根因压成一个普通失败。

### 6.1 线性化点与抑制竞态

suppression 与当前业务事实的线性化点是：Outreach `claim_message_send` 在当前事实通过后提交 `SENDING`。Sending Identity 额度仍在其后独立原子预留；额度失败不产生外部副作用，并把 Attempt 转为可重试状态。额度成功后 Gateway 提交 `EXECUTING` 与发送前审计，再调用 Gmail。系统不在 Gmail 网络调用期间持有 PostgreSQL 行锁。

- suppression 先取得资源锁并提交：claim 随后看到 suppression，本次发送必须被拒绝。
- claim 先取得资源锁并提交：后来的 suppression 仍立即停止 Enrollment 的未来邮件，但无法召回已进入 `SENDING` 的这一次；系统必须如实完成发送或对账。
- 重试时先使用稳定 Message-ID/key 对 Gmail 做只读 reconciliation：
  - Gmail 已存在该邮件：补记本地 sent，不因后来 suppression 伪装成未发送；
  - canonical ledger 能证明 Connector 从未开始，或 Connector 返回 typed `definitely_not_sent`：重新运行全部当前检查，新 suppression 必须阻止 send；
  - 已进入 Connector 但 Gmail 查询未命中：仍是结果不确定，保持 `reconciliation_required`，**不得自动重发**。

## 七、Gmail Connector

### 7.1 幂等协议

每封邮件同时写入确定性的 RFC 5322 `Message-ID` 和固定名称、版本化格式的自定义幂等 Header。二者由幂等键经带域分离标签的 HMAC 生成，只含安全的不可逆标识，不暴露 tenant、Campaign、Attempt、地址或正文。`send` 在 Gmail create/send 前先按确定性 Message-ID 查询，并用自定义 Header 做附加核对；找到时返回原 message ref 和 `already_existed=True`。

Gmail 不提供本系统可以依赖的原生幂等 API，搜索也不被视为强一致。因此“查询未命中”不是“肯定没发”的证明。只要此前请求可能已经到达 Gmail，Connector 就返回 `reconciliation_required`，Gateway 不自动再次调用 send。

稳定 Header 规范一经发布不能静默更名，否则历史重试会失去防重能力。变更必须增加版本兼容查询并留 ADR。

### 7.2 退订

`unsubscribe_url` 必填，同时写入正文可见退订入口与 `List-Unsubscribe` Header。URL 由服务端 provider 生成，客户端不能提交任意 URL。缺失、非 HTTPS、tenant/contact 绑定不一致或 provider 不可用均拒绝发送。

### 7.3 错误映射

```text
请求发送前已确定的 400          → provider_permanent
请求发送前已确定的 401 / 403    → provider_auth_required，需要人工恢复
请求发送前已确定的 429          → rate_limited，保留安全 retry_after
明确发生在 HTTP send 前的网络错 → provider_transient
send 的 5xx / 网络 / 超时        → reconciliation_required
响应丢失或进程中断              → reconciliation_required
```

分类依据 typed HTTP status/transport exception，不解析异常字符串。`CancelledError` 原样传播，但连接、lease 和 session 必须在 finally 中释放。

## 八、结构化错误与 API 映射

Gateway 固定类别：

| 类别 | 是否可重试 | HTTP 映射 |
|---|---:|---:|
| `validation` | 否 | 400 |
| `permission_denied` | 否 | 403 |
| `suppressed` | 当前事实不变时否 | 409 |
| `approval_required` | 否 | 409 |
| `idempotency_conflict` | 否 | 409 |
| `in_progress` | 是 | 503 |
| `rate_limited` | 是 | 429 |
| `provider_auth_required` | 配置修复前否 | 503 |
| `provider_permanent` | 否 | 409 |
| `provider_transient` | 是 | 503 |
| `reconciliation_required` | 是 | 503 |
| `unexpected` | 保守处理 | 500 |

成功与同指纹 duplicate 均返回 200；响应只含固定状态、tool_call_id、安全 provider ref、duplicate 标志和可选安全 retry_after，不返回 Gmail 响应正文。

内部日志使用固定中文消息，只允许 tool、tenant、call、stage、category、safe ref 和计数等字段。任何 secondary log/send/cleanup failure 都不得把原始异常链带到服务器日志或 HTTP 响应。

## 九、崩溃恢复

### 9.1 Gmail 成功，本地完结失败

canonical call 保持 `FAILED_TRANSIENT`，错误类别为 `reconciliation_required`。下一调用取得过期租约后：

1. 用相同 Header 查询 Gmail；
2. 找到既有 message ref；
3. 幂等调用 Outreach `record_sent`；
4. 完结 ledger 为 `SUCCEEDED`。

不得再次扣额度，不得再次发送。

### 9.2 Connector 前后进程退出

如果 Outreach claim 已提交但 ledger 仍是 `CLAIMED`，代码顺序证明 Connector 尚未开始。租约到期后可重新领取，幂等确认 Attempt 的 `SENDING` claim，再提交 `EXECUTING` 后执行。

一旦 ledger 已进入 `EXECUTING`，进程退出就视为结果不确定。恢复过程查询 Gmail：找到则补记；未找到仍保持待对账，不自动重发。

### 9.3 结果未知

网络超时不直接视为未发送。进入 reconciliation；查询命中时补记，查询未命中时仍保持结果不确定。只有 Connector 提供 typed、可证明请求未进入 Gmail send 的 `definitely_not_sent` 结果时，才允许重新检查后再次执行。

## 十、测试设计

### 10.1 单元测试

- manifest 注册、重复工具、未知工具和 HIGH/REQUIRED 约束；
- manifest checks 只能按全局 `STAGE_ORDER` 的相对顺序执行；
- 内容安全词表、全部禁止承诺类别和退订要求；
- 每种固定错误类别与 HTTP 映射；
- 请求指纹稳定、内容变化必变、持久化投影无原始数据；
- Gmail 400、401/403、429、send 前网络错、send 中 5xx/网络/超时和 cancellation 映射；
- 非法 ledger 状态转换；
- provider ref、retry_after、Header 与 URL 的安全校验。

### 10.2 真实 PostgreSQL 集成测试

- migration `upgrade → downgrade → upgrade` 和 ORM/schema parity；
- 同 tenant 同 key 并发只产生一个 canonical claim；
- 同 key 异指纹冲突；
- 不同 tenant 相同 key 完全隔离；
- 租约未过期拒绝、到期 reclaim；
- 审计创建/accepted event/claim/commit failure 时 connector 零调用；
- 同 key quota reservation 不重复计数；
- suppression 与 claim 的真实锁序、Campaign pause/revision、reply、contact eligibility 和 sender state 的 current-fact 竞态；
- provider success 后 Outreach commit failure、ledger commit failure、重启 reconciliation；
- send timeout + 查询未命中保持 reconciliation，自动 send 调用次数仍为一；
- provider 已存在 + 后增 suppression 时只补记；provider 不存在 + 后增 suppression 时不发送；
- 所有 DB 查询 tenant-scoped，跨租户资源固定拒绝。

### 10.3 Connector 合同测试

CI 使用受控 HTTP transport，不使用真实账号：

- 稳定 Header 与 search-before-send；
- 找到既有邮件时不调用 create/send；
- `List-Unsubscribe` 与正文退订入口；
- 安全 message ref；
- OAuth、请求头、地址、正文和 provider 响应不出现在日志、异常或返回值。

### 10.4 变异强度

测试必须能杀死以下变异：

- 删除 tenant、permission、suppression、approval、idempotency 或 rate-limit 任一阶段；
- 把审计提交移动到 Gmail 调用之后；
- 让 prepared Attempt 直接绕过 current-fact 检查或 `SENDING` claim；
- 从请求体接受 recipient、sender、tenant 或 unsubscribe URL；
- 删除 Gmail 发送前查询；
- 把网络超时或一次查询未命中当作确定未发送；
- 对 duplicate 再次预留额度；
- 在 provider 已发送而本地失败后根据新 suppression 把 Attempt 记成未发送。

### 10.5 数据泄漏探针

测试使用明显的 address/body/subject/token/DSN marker，扫描：

- `tool_calls`、`tool_call_events` 与 Outreach 相关表；
- 应用、Gateway 与 Connector 日志；
- HTTP 响应；
- 异常 `str`、`repr`、`__context__` 与 `__cause__`；
- 序列化结果和测试报告。

全部位置都不得出现原值。

### 10.6 门禁

所有 Python 命令使用 conda `tradeos-py312`：

```bash
ruff check .
mypy domains shared tool_gateway connectors apps infra
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
make check
pytest tests/integration -q -W error
git diff --check
```

每个任务还要运行其 focused RED/GREEN、迁移往返、staged sensitive scan 和 cached diff check。

真实 Gmail smoke 不是 CI 门禁。只有操作者显式设置 live 开关、配置专用测试发件身份和专用测试收件箱时才运行；不能使用真实客户地址，也不能把凭证或地址写入命令、日志、报告或 Git。

## 十一、实施拆分

### Task 1：工具契约与安全类型

完成 manifest、registry、typed context/result、错误分类、请求指纹、持久化安全投影和内容承诺检查的公共契约。不建表、不调用 Gmail。

### Task 2：持久化调用账本

增加迁移、ORM、tenant-scoped repository/UoW、invocation audit、canonical claim、租约和状态转换。只做真实 PostgreSQL，不接 Connector。

### Task 3：Phase 1 检查管线

实现六阶段管线、Outreach current-fact/`SENDING` claim、Sending Identity 额度 adapter、approval deny-only 边界及 audit-before-side-effect 规则。

### Task 4：Gmail Connector

实现 secret resolver 边界、稳定 Header、search-before-send、发送、退订 Header、安全结果和 typed 错误映射。使用受控 transport 完成合同测试。

### Task 5：发送编排与 API 装配

实现 `POST /crm/message-attempts/{attempt_id}/send`、服务端 material resolution、Gateway handler、Outreach sent/failure 回写和 configured runtime factory。默认 runtime fail closed。

### Task 6：恢复、端到端证明与文档

实现并验证崩溃恢复、reconciliation、suppression race、并发重试、离线 PostgreSQL 演示、架构文档和 AGENTS 同步。可选 live Gmail smoke 只做人为显式验证。

依赖顺序固定：

```text
安全契约
  → 持久化账本
  → 检查管线
  → Gmail Connector
  → 发送装配
  → 崩溃恢复与最终验收
```

每个任务均执行：genuine RED → focused GREEN → scoped/full gates → staged 自审 → 普通 commit → push → 精确 HEAD 远端 CI success。不得 amend、force push 或在 CI 未完成时开始下一任务。

## 十二、验收矩阵

| 验收项 | 权威证据 |
|---|---|
| 正式配置可发送 | configured runtime + 受控 transport E2E；可选 live smoke |
| 默认安全失败 | zero-arg/unconfigured runtime connector 零调用 |
| 无重复发送 | 真实 PG 并发、崩溃、timeout、reconciliation 测试 |
| Current facts 生效 | pause/revision/reply/suppression/contact/sender 竞态测试 |
| 商业承诺不可旁路 | approval stage 全类别和变异测试 |
| 额度不超限 | Sending Identity 原子 reservation + 同键重试测试 |
| 审计先于外部动作 | commit failure/顺序 trace mutation 测试 |
| Tenant 隔离 | 两 tenant 同 key/同资源 ref 的真实 PG 测试 |
| 无敏感数据持久化 | marker DB/log/exception/response 扫描 |
| 边界方向正确 | boundary checker + import review |
| 交付完整 | 每任务 push，精确 HEAD CI success |

任何一项只有间接证据、只读代码推测或单元 fake，而要求本身涉及真实 PostgreSQL、网络时序或崩溃恢复时，都不得标记为完成。
