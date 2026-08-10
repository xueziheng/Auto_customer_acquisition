# Slice 4B-1 触达编排与全局抑制设计

> 状态：已确认
> 日期：2026-08-11
> 对应路线：`HANDBOOK.md` 切片 4 的第二个独立子项目

## 一、目的

本设计实现冷开发触达的领域基础：Campaign 不可变版本、Enrollment、全局抑制、Campaign 配额、发送尝试和持久化审计。它回答五个问题：

1. 哪个已验证联系人可以进入哪个已批准 Campaign？
2. 同一客户账户是否已经处于另一条活跃触达序列？
3. 当前联系人或账户是否已被全局抑制？
4. Campaign 当前还能否准备一条发送尝试？
5. 退订、投诉或硬退信如何在所有 Campaign 中立即停止后续触达？

本任务不调用 Gmail，也不向外部世界发送消息。它把发送前必须成立的业务事实和原子状态变更做成独立领域纵切面，为后续 Tool Gateway 提供默认拒绝的输入。

## 二、切片 4B 的分解

切片 4B 分成四个分别设计、审查、提交和推送的子项目：

1. **4B-1 触达编排与全局抑制**：本设计的范围。
2. **4B-2 Tool Gateway 发送门禁与持久化审计**：在真实外部动作前重新检查抑制、回复、Campaign 与发件身份，并占用发件身份名额。
3. **4B-3 Gmail Connector**：OAuth 凭证隔离、协议适配、发送幂等、退信与投诉归一化。
4. **4B-4 单封手动发送 API/UI 与通知**：真人确认、浏览器流程和真实通知出口。

4B-2 至 4B-4 必须分别经过新的设计门禁。本轮不创建 Gmail 客户端、不读取凭证、不接外网、不增加发送 UI。

## 三、范围

### 3.1 本轮实现

- `domains/outreach` 的 Campaign、不可变版本、Sequence Step、Enrollment、Suppression、Campaign Quota、Message Attempt 与 Action History。
- domain-local typed actor、scope、action、permission matrix、两阶段授权和安全审计。
- 联系人资格、发件身份资格、Campaign 审批和回复状态的窄 Protocol。
- PostgreSQL 表、约束、tenant-bound repositories、request-scoped UoW 与 durable outbox。
- Campaign 创建、送审、修订、激活、暂停和读取。
- Enrollment 创建、准备发送、记录发送结果和人工停止。
- 联系人级、账户级全局抑制；抑制与停止所有匹配活跃 Enrollment 在同一事务完成。
- Campaign 新联系人额度和总发送额度的原子占用。
- 真实 PostgreSQL 并发、回滚、幂等、租户隔离和离线演示。

### 3.2 明确不做

- Tool Gateway 实际发送检查管线。
- Gmail OAuth、Gmail API、DNS、HTTP 或任何真实网络调用。
- 邮件正文生成、模板渲染、附件、线程同步或回复分类。
- Notification Gateway、scheduler、worker、API 或 Web UI。
- Prospecting、Demand、Approvals、Conversations 或 Sending Identity 域的生产实现修改。
- 自动解除抑制、自动重新 Enrollment、批量导入联系人。
- Campaign 智能排序、模型评分、A/B 测试或最优发送时间预测。
- 多渠道触达、WhatsApp、短信或社交平台私信。

## 四、架构边界

### 4.1 责任归属

```text
apps composition
  ├─ ContactEligibilityProvider
  ├─ SendingIdentityEligibilityProvider
  ├─ CampaignApprovalProvider
  └─ ReplyStatusProvider
                 ↓
domains/outreach service
  ├─ Campaign / Version / Step
  ├─ Enrollment / Suppression
  ├─ Quota / MessageAttempt / Action
  └─ typed permission + audit
                 ↓
infra/db repositories + UoW + outbox
```

`domains/outreach` 只依赖 `shared.*`，不得直接导入 `domains/prospecting`、`domains/sending_identity`、`domains/approvals`、`domains/conversations` 或任何 connector。跨域读取只能由 app composition 实现本设计定义的窄 Protocol。

### 4.2 默认拒绝

任何 provider 不可用、返回租户不一致、返回缺字段或无法证明当前事实时，服务抛固定 typed retryable/policy error；不得把查询失败解释成“未抑制”“未回复”“已验证”或“可发送”。失败路径不进入写 UoW，不产生 allow audit。

### 4.3 4B-1 与真实发送的边界

`prepare_message_attempt` 只创建一条可追踪的待发送事实，不调用 connector。即使准备完成后抑制或回复状态发生变化，4B-2 Tool Gateway 仍必须在 Gmail 调用前重新读取当前抑制、回复、Campaign、Enrollment 和发件身份状态。4B-1 的准备结果不是发送授权。

### 4.4 现有骨架的明确替换

当前骨架中的几个宽松接口不能原样进入实现：

- `enroll(..., verified: bool)` 改为由服务调用 `ContactEligibilityProvider` 取得不可伪造的当前资格快照。
- `prepare_send() -> SendAuthorization` 改为 `prepare_message_attempt() -> MessageAttemptView`；名称和返回值明确它不是外发授权。
- `suppress(scope: str, target_id: str, ...)` 改为 typed `SuppressionTarget` 和显式 idempotency key。
- 公开 `list_due_enrollments` 不在 4B-1 作为 worker 租约接口实现；4B-2 必须另行设计可并发领取、可恢复的扫描/lease 契约。4B-1 的 `list_enrollments` 只是受权限约束的管理查询。

这些是对未实现骨架的收紧，不提供兼容旁路。

## 五、领域模型

### 5.1 Campaign 与不可变版本

Campaign 当前状态：

```text
DRAFT → PENDING_APPROVAL → ACTIVE → PAUSED → ACTIVE
  │             │             │         │
  ├─────────────┴─────────────┴─────────┼→ CANCELLED
  └──────────────────────────────────────┴→ COMPLETED

ACTIVE / PAUSED 修订 → 新版本 PENDING_APPROVAL
```

规则：

- 新建 Campaign 产生 `version=1` 的不可变边界快照和对应 Sequence Steps。
- `submit_campaign` 只允许初始 `DRAFT` 版本进入 `PENDING_APPROVAL`。
- `activate` 必须取得当前版本的精确审批快照；旧版本审批不能激活新版本。
- 已批准边界不得原地修改。修订创建 `version+1`，更新 current version、清除 current activation approval reference，并直接进入 `PENDING_APPROVAL` 等待新审批。
- `pause` 不改变版本；恢复时仍使用原已批准版本，并重新确认审批仍指向当前版本。
- `activate_campaign` 同时承担 `PENDING_APPROVAL → ACTIVE` 和 `PAUSED → ACTIVE`；两个分支都必须重新取得当前版本的精确审批快照。
- `COMPLETED/CANCELLED` 是终态。
- Campaign 当前行只保存状态、当前版本、当前激活审批引用、持久化 sender round-robin cursor 和必要汇总，不复制不可变步骤内容。

### 5.2 Campaign 边界

每个版本包含以下边界：

- 非空 Campaign 名称。
- 非空目标市场集合。
- 目标企业类型集合；空集合表示拒绝所有，不能解释为不限。
- 允许品类集合；空集合表示拒绝所有，不能解释为不限。
- 非空允许发件身份 ID 集合。
- `daily_new_contact_limit > 0`。
- `daily_total_message_limit > 0`。
- `daily_new_contact_limit <= daily_total_message_limit`。
- 1 至 5 个 Sequence Steps。
- 第一条 Step 的 intent 必须是 `DISCOVERY`。
- Step number 从 1 连续递增；第一步 `wait_days=0`，后续步骤 `wait_days > 0`。
- handoff triggers 使用固定公共词表；空集合表示没有自动转人工触发条件，不能解释为不限。
- `stop_on_reply` 在 Phase 1 固定为 `True`，调用方不能关闭。

Campaign 版本只保存边界与安全引用，不保存邮件正文、联系人地址或 connector payload。

### 5.3 Enrollment

Enrollment 状态：

- 活跃：`ENROLLED`、`IN_SEQUENCE`。
- 终态：`REPLIED`、`COMPLETED`、`STOPPED_SUPPRESSED`、`STOPPED_BOUNCED`、`STOPPED_MANUAL`、`STOPPED_IDENTITY_UNAVAILABLE`。

规则：

- 同一租户同一 `ProspectAccountId` 在所有 Campaign 中最多有一条活跃 Enrollment，由 PostgreSQL partial unique index 保证。
- Enrollment 固定绑定创建时的 Campaign version；Campaign 修订不静默改变既有 Enrollment 的步骤语义。
- Enrollment 保存安全的 contact/account typed ID，不保存邮箱地址、原始客户文本或凭证。
- 任何终态都不能自动复活；重新触达必须经过新的显式 Enrollment，并再次通过全部当前门禁。
- 当前 step、下次 due time 和选中发件身份都是持久化状态，不能只存在于 worker 内存。

### 5.4 全局抑制

`SuppressionTarget` 是 typed sum type，必须且只能包含以下一个目标：

- `ContactPointId`，scope 为 `CONTACT`。
- `ProspectAccountId`，scope 为 `ACCOUNT`。

调用方不能提交自由字符串 scope 后再附任意 target。target ID 的命名空间必须与 scope 一致。

`SuppressionReason` 为 typed enum：

- `UNSUBSCRIBE`
- `COMPLAINT`
- `HARD_BOUNCE`
- `MANUAL_BLOCK`
- `COMPETITOR`
- `EXISTING_CUSTOMER_CONFLICT`

每条抑制事实包含 tenant、target、reason、UTC occurred time、严格安全的 `source_ref` 和显式 `IdempotencyKey`。唯一键为 `(tenant_id, idempotency_key)`：

- 同一 key 且 canonical target/reason/source ref/occurred time 完全一致时返回原结果，不新增事实、不重复停止 Enrollment、不重复发 outbox。
- 同一 key 携带不同业务内容时固定幂等冲突，不能用旧结果掩盖调用方错误。
- 不同 key 即使 target/reason 相同也保留为独立事实。
- 任何一条有效事实都表示当前被抑制。
- 不提供 delete、unsuppress 或 update 公共方法。
- 不对联系人/账户业务表建强外键；即使隐私删除原业务记录，最小抑制事实仍必须保留。

增加抑制必须在同一个 UoW 内：追加抑制事实、锁定并停止全部匹配活跃 Enrollment、追加 Action、发布 `SuppressionAdded` outbox。任一步失败都整体回滚。

### 5.5 Campaign 配额

Campaign 每个 UTC 自然日维护两类单调计数：

- `new_contacts_reserved`
- `messages_reserved`

计数只允许原子增加，不允许 update 减少或 delete。repository 使用条件 UPDATE、row lock 或精确 `INSERT ... ON CONFLICT`，禁止 Python read-modify-write。

- Enrollment 创建成功时占用一次新联系人额度。
- Message Attempt 首次创建时占用一次总发送额度。
- 相同幂等 key 重试返回原记录，不重复占用。
- 配额一旦占用不退款。网络结果未知时退款会让重试突破上限。

Enrollment request 携带显式 `IdempotencyKey`。同一 key 与完全相同的 Campaign/contact/account 重试返回原 Enrollment；同 key 异内容固定冲突；不同 key 命中同账户活跃 partial unique 时固定报“账户已有活跃触达”，不能返回另一 Campaign 的记录。

### 5.6 发件身份选择

Campaign current row 保存持久化 round-robin cursor。创建 Enrollment 时，在 Campaign row lock 内：

1. 从当前版本已批准的 sender ID 集合按 canonical ID 排序。
2. 从 cursor 后一项开始循环扫描。
3. 对每个候选调用 `SendingIdentityEligibilityProvider` 读取安全快照。
4. 选择第一个 tenant 匹配、role 为 `COLD_OUTREACH`、认证通过、状态可发送且剩余额度大于 0 的身份。
5. 在同一事务更新 cursor 并把身份 ID 写入 Enrollment。

没有候选时固定拒绝，不创建 Enrollment、不占 Campaign 配额。选择结果不授权真实发送；4B-2 仍需原子占用发件身份发送名额。

### 5.7 Message Attempt

Message Attempt 状态：

- `RESERVED`
- `SENT`
- `FAILED_TRANSIENT`
- `FAILED_PERMANENT`

稳定幂等 key 为：

```text
{tenant_id}:{campaign_id}:{enrollment_id}:v{version}:step{step_number}
```

表中只保存 typed IDs、step/version、状态、安全 provider message reference 和固定失败类别；不保存正文、收件地址、HTTP 响应或异常文本。

每条 Attempt 在创建时生成内部 typed `MessageId`；`MessageSent` 使用该 ID。外部 provider ref 只是发送结果的安全幂等证据，不能替代内部 MessageId。

- `prepare_message_attempt` 创建或返回同 key 记录，并仅在首次创建时占 Campaign message quota。
- `record_sent` 只接受匹配的 `RESERVED/FAILED_TRANSIENT` attempt。相同安全 provider ref 重试为 no-op；不同 ref 固定拒绝。成功后推进 Enrollment，并发布 `MessageSent`。
- `SendFailureCategory` 在本切片固定为 `PROVIDER_TRANSIENT` 和 `IDENTITY_UNAVAILABLE`。前者把 Attempt 设为 `FAILED_TRANSIENT` 并允许用同一 key 重试；后者把 Attempt 设为 `FAILED_PERMANENT`，并把 Enrollment 设为 `STOPPED_IDENTITY_UNAVAILABLE`。
- 硬退信、投诉和退订不通过 `record_send_failure` 表示；4B-3 必须把它们转换为对应的全局 suppression 请求。
- 任何方法都不得接收或记录原始 provider exception、headers 或 response body。

## 六、跨域窄接口

这些 Protocol 定义在 `domains/outreach` 的公共契约中，由 `apps` composition 实现。返回值均为 frozen、typed、安全快照。

### 6.1 ContactEligibilityProvider

输入 tenant、contact point ID、account ID，返回：

- 相同 tenant 的 typed IDs。
- 当前可达性已验证状态与验证时间。
- 当前 legal basis typed value。
- contact point 是否仍属于该 account。
- 当前规范化 country、entity type 和 qualified category 集合，供服务逐项对照 Campaign 已批准边界。
- snapshot observed time。

不能只传调用方可伪造的 `verified=True`。

### 6.2 SendingIdentityEligibilityProvider

输入 tenant 与 sending identity ID，返回：

- 相同 tenant 与相同 identity ID。
- `DomainRole` 的公共安全表示。
- 当前认证是否全部通过。
- 当前状态是否可发送。
- 当前剩余名额。

不返回 connector ref、Token、邮箱凭证或底层错误。

### 6.3 CampaignApprovalProvider

输入 tenant、Campaign ID 和 version，返回精确审批快照：

- tenant、Campaign ID、version。
- approval ID。
- approved by。
- approved at。
- typed approval state。

不得用裸布尔值代替版本绑定审批。

### 6.4 ReplyStatusProvider

输入 tenant 与 Enrollment 的 contact/account IDs，返回当前 typed reply 状态和发生时间。任何已回复状态都会阻止新 Message Attempt，并把活跃 Enrollment 转为 `REPLIED`。

## 七、公共服务契约

所有方法都显式接收 tenant 与 domain-local actor；当前时间只来自注入的 UTC clock。

### 7.1 Campaign 管理

- `create_campaign(tenant_id, request, *, actor) -> CampaignView`
- `submit_campaign(tenant_id, campaign_id, *, actor) -> CampaignView`
- `revise_campaign(tenant_id, campaign_id, request, *, actor) -> CampaignView`
- `activate_campaign(tenant_id, campaign_id, *, actor) -> CampaignView`
- `pause_campaign(tenant_id, campaign_id, reason, *, actor) -> CampaignView`
- `cancel_campaign(tenant_id, campaign_id, *, actor) -> CampaignView`
- `get_campaign(tenant_id, campaign_id, *, actor) -> CampaignView`
- `list_campaigns(tenant_id, scope, *, limit, actor) -> list[CampaignView]`

### 7.2 Enrollment 与发送准备

- `enroll(tenant_id, campaign_id, request, *, actor) -> EnrollmentView`
- `prepare_message_attempt(tenant_id, enrollment_id, *, actor) -> MessageAttemptView`
- `record_sent(tenant_id, attempt_id, provider_ref, *, actor) -> MessageAttemptView`
- `record_send_failure(tenant_id, attempt_id, category, *, actor) -> MessageAttemptView`
- `stop_enrollment(tenant_id, enrollment_id, reason: EnrollmentStopReason, *, actor) -> EnrollmentView`
- `get_enrollment(tenant_id, enrollment_id, *, actor) -> EnrollmentView`
- `list_enrollments(tenant_id, scope, *, limit, actor) -> list[EnrollmentView]`

### 7.3 抑制

- `add_suppression(tenant_id, request, *, actor) -> SuppressionResult`
- `is_suppressed(tenant_id, target, *, actor) -> SuppressionView | None`
- `list_suppressions(tenant_id, scope, *, limit, actor) -> list[SuppressionView]`

没有 remove/clear/unsuppress 方法。

## 八、权限与审计

### 8.1 Typed scope

`OutreachScope` 是 frozen DTO，包含：

- `level`: `TENANT | MANAGER | SYSTEM | SELF`
- `allowed_campaign_ids`
- `allowed_account_ids`
- `allowed_enrollment_ids`
- `allowed_suppression_targets`

`None` 表示该维度不限制，空集合表示该维度全拒。MANAGER 必须至少收窄 campaign 或 account；SYSTEM 写操作必须携带精确单一 enrollment 或 suppression target。构造后容器必须转换为 `frozenset`，不能通过外部可变 set 扩权。

### 8.2 Typed action

`OutreachAction` 在 Phase 1 固定包含：

- `CAMPAIGN_CREATE`
- `CAMPAIGN_SUBMIT`
- `CAMPAIGN_REVISE`
- `CAMPAIGN_ACTIVATE`
- `CAMPAIGN_PAUSE`
- `CAMPAIGN_CANCEL`
- `CAMPAIGN_READ`
- `CAMPAIGN_LIST`
- `ENROLLMENT_CREATE`
- `ENROLLMENT_PREPARE_SEND`
- `ENROLLMENT_RECORD_SENT`
- `ENROLLMENT_RECORD_FAILURE`
- `ENROLLMENT_STOP`
- `ENROLLMENT_READ`
- `ENROLLMENT_LIST`
- `SUPPRESSION_ADD`
- `SUPPRESSION_READ`
- `SUPPRESSION_LIST`

### 8.3 Phase 1 权限矩阵

| actor | scope | 允许操作 |
|---|---|---|
| boss | TENANT | 全部 Campaign 生命周期、激活、Enrollment create/stop/read/list、六类抑制、全租户读取；不能伪造 prepare/record sent/failure |
| manager | MANAGER | 对显式范围内既有 Campaign 执行 submit/revise/pause/cancel；创建 Enrollment、人工停止；范围内读取；不能 create/activate/add suppression |
| system | SYSTEM | 对精确单一 Enrollment prepare/record sent/failure/stop；对精确单一 target 增加 `UNSUBSCRIBE/COMPLAINT/HARD_BOUNCE` 自动抑制；读取精确资源 |
| sales | SELF | 只读由 app 根据 ownership 计算后显式列入 scope 的 Campaign/Enrollment；领域层仍逐资源核 scope；不能写 Campaign、Enrollment 或 Suppression |

Campaign 创建和激活仅 boss/TENANT；manager 不能通过“自己创建再自己送审”绕过审批。SYSTEM 只能提交 `UNSUBSCRIBE/COMPLAINT/HARD_BOUNCE`，不能提交 `MANUAL_BLOCK/COMPETITOR/EXISTING_CUSTOMER_CONFLICT`，也不能使用无限制 target scope。未知角色、空 actor、空 level、actor/scope 不一致、错误 tenant 或超出资源 scope 全部默认拒绝。

### 8.4 两阶段授权与审计顺序

每个公共入口遵循：

1. 在读取 clock、验证客户输入、调用 provider 或打开 UoW 前做 action/tenant/actor 的 preauthorization。
2. 加载 tenant-bound trusted row 或 provider 快照。
3. 使用真实 campaign/account/enrollment/target 做 resource authorization。
4. 执行业务校验和写入。
5. UoW 成功退出并提交。
6. 写恰一条 allow audit。

授权拒绝写恰一条固定 `deny:authorization`，零 allow。tenant isolation violation 还写一条固定中文 CRITICAL 安全日志。commit、provider、验证、并发或 outbox 失败均为零 allow。

审计只允许 actor ID、action、tenant、scope label、固定 rule、Campaign/Enrollment 的安全 typed ID 和固定 count；不包含 contact address、target 原值、Campaign 名称、模板、正文、provider ref、异常文本或凭证。

## 九、PostgreSQL 持久化

### 9.1 八张表

1. `outreach_campaigns`：当前状态、当前版本、round-robin cursor。
2. `outreach_campaign_versions`：不可变 Campaign 边界快照。
3. `outreach_sequence_steps`：不可变版本步骤。
4. `outreach_enrollments`：账户/联系人、固定版本、发送身份、状态与 due time。
5. `outreach_suppressions`：append-only 抑制事实。
6. `outreach_daily_quotas`：Campaign UTC 日计数。
7. `outreach_message_attempts`：待发送与结果状态。
8. `outreach_actions`：append-only 状态变更历史。

所有表都有 `tenant_id`。所有业务外键使用 tenant 复合键；所有 repository 查询显式 tenant filter。

### 9.2 关键约束

- Campaign `(tenant_id, campaign_id)` 唯一；version `(tenant_id, campaign_id, version)` 唯一。
- Sequence Step `(tenant_id, campaign_id, version, step_number)` 唯一且 step number 连续性由 service 验证。
- Campaign Version 与 Sequence Step 通过 trigger 禁止 UPDATE/DELETE；修订只能追加新版本。
- 活跃 Enrollment 对 `(tenant_id, account_id)` 使用 partial unique index。
- Enrollment `(tenant_id, idempotency_key)` 唯一；相同 key 的 payload 一致性由 repository outcome 与 service 共同验证。
- Suppression `(tenant_id, idempotency_key)` 唯一并通过 trigger 禁止 UPDATE/DELETE。
- Quota `(tenant_id, campaign_id, quota_date)` 唯一；两个计数分别 CHECK 非负，条件写入分别对照 Campaign 的对应上限；trigger 禁止减少和 DELETE。不能要求 `new_contacts_reserved <= messages_reserved`，因为联系人可以先加入而尚未准备消息。
- Message Attempt 的稳定业务 key 在 tenant 内唯一。
- Action 通过 trigger 禁止 UPDATE/DELETE。
- 时间列为 timezone-aware `TIMESTAMPTZ`；日期额度为 UTC date。
- DB CHECK 使用 `IS NOT NULL` 或显式双分支，不能依赖 SQL 三值逻辑让非法 NULL 通过。

### 9.3 UoW 与失败语义

repositories、outbox 和 action history 共用一个 AsyncSession。UoW body/commit 失败必须 rollback；存在 primary exception 时，rollback/close 的 `BaseException` 不能覆盖 primary；无 primary 的 cancellation 原样传播。所有失败都不得留下部分 quota、suppression、Enrollment、Action 或 outbox。

## 十、核心流程

### 10.1 创建 Campaign

1. preauthorize boss create。
2. 验证版本边界和步骤。
3. 对每个 sender ID 读取安全 eligibility snapshot，验证 tenant、role、ID 对齐且当前认证全部通过。
4. 在一个 UoW 写 Campaign、version、steps、Action。
5. commit 后 allow audit。

创建时不要求 sender 当前仍有剩余额度，但必须是已登记且认证通过的 `COLD_OUTREACH` 身份；Enrollment 时再检查动态可用性。

### 10.2 激活 Campaign

1. preauthorize boss activate。
2. 锁定 tenant-bound Campaign 当前行和当前 version。
3. 取得 exact-version approval snapshot。
4. 检查 tenant、Campaign、version、approved state 和审批身份。
5. resource authorize 后转 `ACTIVE`，追加 Action。
6. commit 后 allow audit。

### 10.3 创建 Enrollment

1. preauthorize。
2. 读取联系人资格快照；验证 tenant、contact/account 关系、当前 verified reachability、legal basis，并对照当前 Campaign version：`country ∈ markets`、`entity_type ∈ target_entity_types`、`qualified_categories ∩ allowed_categories` 非空；任一越界或空事实固定拒绝。
3. 锁 Campaign，确认 ACTIVE 与当前不可变版本。
4. resource authorize。
5. 查询 contact 与 account 当前抑制；任何命中固定拒绝。
6. 原子占用当日 new-contact quota。
7. 在 Campaign lock 下按持久化 cursor 选择发件身份。
8. 插入 Enrollment；partial unique 竞争者固定失败或返回既有幂等结果。
9. 追加 Action，commit 后 allow audit。

### 10.4 准备发送

1. preauthorize SYSTEM 精确 Enrollment。
2. 锁 Campaign 后锁 Enrollment；确认 Campaign 为 ACTIVE、Enrollment 绑定版本仍存在且有精确审批、due time 和 step 合法。不得要求 Enrollment 版本等于 Campaign current version；修订不能静默改变既有 Enrollment。
3. 读取当前 reply；已回复则转 `REPLIED` 并不创建 attempt。
4. 读取 contact/account suppression；命中则转 `STOPPED_SUPPRESSED` 并不创建 attempt。
5. 重新检查联系人资格和发件身份安全快照。
6. 原子创建稳定 key Message Attempt，并按 Campaign 当前已激活版本的 `daily_total_message_limit` 占用 quota；Enrollment 的旧版本只决定步骤内容，不能保留更高的旧额度。
7. commit 后 allow audit。

该方法的返回值只允许交给 4B-2 Tool Gateway；不能直接调用 Gmail Connector。

### 10.5 增加抑制

1. preauthorize actor/reason；SYSTEM 只允许三个自动原因。
2. 验证 typed target、UTC aware occurred time、安全 source ref 和 idempotency key。occurred time 不得晚于 service 当前时钟五分钟以上，事实的数据库 created time 只取 service 时钟。
3. 尝试插入抑制事实；已存在相同 key 时返回原结果。
4. 新事实则按 Enrollment ID canonical 顺序锁定全部匹配活跃 Enrollment。
5. 全部转 `STOPPED_SUPPRESSED`，追加对应 Action。
6. 追加一条 suppression Action 和一条 `SuppressionAdded` outbox。
7. commit 后 allow audit。

### 10.6 记录发送结果

`record_sent` 按统一顺序锁 Campaign current、Enrollment、再锁 Attempt，验证稳定 key、状态和 provider ref 幂等；同一事务把 Attempt 设为 `SENT`、推进 step/终结 Enrollment、追加 Action 和 `MessageSent` outbox。`record_send_failure` 使用同一锁序和第 5.7 节的固定类别，不保存底层异常文本。

## 十一、并发与锁序

固定锁序：

```text
Campaign current
  → Enrollment（按 enrollment_id 升序）
  → Daily quota
  → Message attempt
```

抑制路径不反向锁 Campaign：先声明/插入 suppression winner，再按 enrollment ID 升序锁匹配 Enrollment，停止后不再取得 Campaign lock。这样不会与 prepare 的 Campaign → Enrollment 顺序形成环。

必须用真实 PostgreSQL 证明：

- 20 个并发 Enrollment 同一 account，最终恰一条活跃记录。
- 20 个并发 quota reservation 永不超过 new-contact/message cap。
- 同一 suppression key 并发只产生一条事实、一组停止动作和一条 outbox。
- 同一 Message Attempt key 并发只占用一次 quota。
- 不同 Campaign 的 account partial unique 仍是全租户约束。
- 抑制与 prepare 竞争后，不会产生可直接外发的授权；若已有 RESERVED attempt，4B-2 必须因当前抑制拒绝。
- commit/unique/deadlock/serialization error 原样回滚并传播，不解析数据库异常字符串。
- 相同业务 ID 在不同 tenant 互不影响。

## 十二、事件与安全 payload

使用共享 typed 事件：

- `SuppressionAdded`
- `MessageSent`

payload 只包含 tenant、typed entity IDs、固定 reason/category、UTC time 和安全 dedup key；不得包含联系人地址、邮件正文、模板内容、客户原话、provider 响应、异常文本、connector ref 或凭证。

事件必须登记在显式 whitelist。序列化层对 ID、枚举、时间和自由字符串做值级验证；恶意 `Bearer`、token/secret/password、控制字符、URL/DSN 和超长文本固定拒绝。outbox 与业务变更在同一 UoW。

## 十三、错误处理

- 用户输入错误：固定 typed `ValidationError` 子类，不回显原输入。
- 状态错误：固定 `InvalidStateTransition` 或领域 typed error。
- 权限错误：`PermissionDenied` + 恰一 deny audit。
- 租户错配：`TenantIsolationViolation` + 固定 CRITICAL 安全日志。
- provider 不可用：固定 retryable error，零写入、零 allow。
- 已抑制/已回复/额度已满/无发件身份：固定 policy error。
- 并发唯一冲突：只通过精确 conflict target/typed repository outcome 处理；不捕获宽泛 IntegrityError 后解析字符串。
- 未知数据库、commit、cancellation 或 outbox 错误原样传播，清理错误不覆盖 primary。

内部日志用中文固定文案，只记录 error type/category 与安全 IDs；不使用 `logger.exception` 输出客户数据或连接串。

## 十四、测试与验收

### 14.1 TDD 原则

每个实现任务先取得 genuine RED。缺失行为、约束或契约必须是唯一失败原因；import、PATH、Docker、fixture、migration、warning 和 AppleDouble 假失败先排除，不能冒充 RED。

### 14.2 单元与契约测试

- Campaign/Enrollment/Suppression/Attempt 状态机和非法构造。
- Campaign version 不可变、修订重新审批、步骤上限和 discovery-first。
- typed permission 全矩阵、空 actor、可变 set 扩权、targetless SYSTEM、scope mismatch。
- 每个公共方法 preauthorize-first、resource require、commit 后恰一 allow、所有失败零 allow。
- provider mismatch/unavailable、联系资格和审批快照 fail-closed。
- round-robin cursor、无 sender、quota、幂等 key、回复/抑制停止。
- malicious event/audit/error payload 不泄漏。
- property/mutation tests 不复制生产算法或私有 priority map。

### 14.3 PostgreSQL 测试

- migration `0009 → 0008 → 0009` roundtrip、Alembic exact head、ORM parity。
- 八表 tenant composite FK/unique/check/append-only/monotonic guards。
- repository scope fail-closed、wrong tenant typed violation。
- UoW rollback、commit/cancellation cleanup、outbox atomicity。
- 本设计第十一节全部真实并发场景。
- suppression 停止跨 Campaign Enrollment，tenant isolation 和 durable outbox readback。
- Campaign approval exact-version、quota 和 Message Attempt 幂等。

### 14.4 离线演示

提供只读取 `DATABASE_URL` 的离线 PostgreSQL demo：随机 tenant，真实 service/UoW/authorizer，并用显式 demo-only、默认拒绝的 provider 返回安全联系人/发件身份/精确审批/回复快照；创建 Campaign、激活、Enrollment、准备 attempt、增加 suppression，再从独立 session 回读八表与 outbox。不得把 demo provider 当作 production composition，不得访问外网、连接 Gmail、直插业务表或输出地址、正文、DSN、凭证。相同数据库连续运行两次必须 tenant 隔离。

### 14.5 最终门禁

```bash
pytest tests/unit/test_outreach_*.py -q -W error
pytest tests/integration/test_outreach_*.py tests/integration/test_demo_outreach.py -q -W error
make check
pytest tests/integration -q -W error
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
```

每个小任务独立 review；所有 Critical/Important 必须通过新的 RED→GREEN 关闭。每个小任务普通 commit 并 push，等待精确 HEAD CI 成功后再进入下一任务。

## 十五、交付分层

本规格对应一个实现计划，但按小任务分批交付：

1. 领域模型、DTO、permissions 与 Protocol。
2. PostgreSQL migration、repositories、UoW 与 outbox。
3. Campaign 版本、审批和生命周期 service。
4. Enrollment、round-robin、Campaign quota 与 Message Attempt。
5. 全局 suppression、跨 Campaign stop 与并发闭环。
6. 离线真实 PostgreSQL demo、文档和最终广审。

任务边界允许在写实现计划时按真实接口依赖微调，但不得把 Tool Gateway、Gmail、API、UI 或通知偷偷并入 4B-1。

## 十六、验收结论

Slice 4B-1 只有同时满足以下条件才算完成：

1. Campaign 的已批准边界由不可变版本保护，任何修订都重新审批。
2. 联系人资格、回复、抑制、Campaign、quota 和 sender eligibility 全部当前读取且失败默认拒绝。
3. 同租户同账户最多一条活跃 Enrollment，配额和幂等在真实并发下不超限。
4. 联系人或账户抑制能原子停止所有匹配活跃 Enrollment，且抑制事实不可删除。
5. Message Attempt 可追踪、可幂等，但不能绕过后续 Tool Gateway 成为发送授权。
6. 所有写入 tenant-safe、transactional、audited，并与 durable outbox 原子提交。
7. 任何事件、审计、异常、日志和 demo 输出都不包含凭证、联系人地址或邮件正文。
8. 全部门禁、独立 review 和精确 HEAD CI 通过。
