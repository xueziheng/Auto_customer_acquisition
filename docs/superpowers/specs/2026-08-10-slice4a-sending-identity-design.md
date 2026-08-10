# Slice 4A 发件身份与域名信誉设计

> 状态：已确认
> 日期：2026-08-10
> 对应路线：`HANDBOOK.md` 切片 4 的第一个独立子项目

## 一、目的

本设计把发件身份、独立域名、认证门禁、预热额度和信誉熔断实现为一个可独立验收的领域纵切面。它回答三个问题：

1. 这个身份现在能不能用于发送？
2. 今天还能安全占用多少发送额度？
3. 当前信誉是否要求自动限流或停用？

本任务不发送邮件。它为后续 Tool Gateway 与 Gmail Connector 提供强制、原子、默认拒绝的领域门禁，避免外部连接器拥有业务放行权。

## 二、已确认的切片 4 分解

切片 4 分成三个分别设计、分别审查、分别推送的子项目：

1. **4A 发件身份与域名信誉**：本设计的范围。
2. **4B Tool Gateway 与 Gmail/DNS Connector**：固定检查管线、持久化审计与幂等、协议级 Gmail 适配。
3. **4C 单封手动发送与真实通知出口**：API/worker、站内通知、邮件通知和进程级 E2E。

4B、4C 必须在 4A 交付后分别经过新的 brainstorming/spec 门禁。本轮不接真实 Gmail 凭证、不发送真实邮件。

## 三、范围

### 3.1 本轮实现

- `domains/sending_identity` 的实体、状态机、DTO、typed permissions、service 与实现。
- 发件域名角色的数据库唯一约束。
- 身份、认证历史、预热计划、信誉事件、发送额度预留、人工恢复记录的 PostgreSQL 持久化。
- request-scoped UoW、tenant-bound repositories 与 durable outbox。
- SPF/DKIM/DMARC typed 结果的记录入口；结果由上层提供，领域层不做 DNS IO。
- 原子发送名额预留与幂等 reservation key。
- 身份级和域名级滚动窗口信誉评估与自动熔断。
- 默认拒绝的 Phase 1 权限矩阵与固定安全审计。
- 真实 PostgreSQL 演示脚本及数据库回读测试，不访问外网。

### 3.2 明确不做

- Gmail OAuth、Gmail API、DNS 查询或任何真实网络调用。
- Tool Gateway、抑制名单、联系人可达性、Campaign 配额和审批。
- 发送正文、邮件模板、回复拉取、退信解析。
- 发件身份管理 UI。
- 多域名自动轮换、信誉预测模型、自动购买域名。
- Redis、Temporal、第二家邮件服务商。

## 四、全局硬边界

### 4.1 凭证

`connector_ref` 只是服务端密钥引用名，不是 Token。它必须通过严格格式校验，只允许短的标识符；拒绝空白、URL、Bearer/token 形态、换行和超长文本。领域实体、DTO、事件、异常、日志和审计都不得包含凭证值。

### 4.2 租户隔离

所有表都有 `tenant_id`。所有主键、唯一键和外键都包含 `tenant_id`；所有 repository 方法显式接收或在构造时绑定租户。任何 tenant mismatch 固定拒绝，写一条不含业务 payload 的 CRITICAL 安全告警。

### 4.3 精确比率

退信率、投诉率和投递率是确定性业务指标，使用 `Decimal` 计算与比较。数据库使用定点 `NUMERIC`，API wire 未来使用十进制字符串，不接受 JSON number/float。模型不产生概率或阈值。

### 4.4 依赖方向

`domains/sending_identity` 只依赖 `shared.*`。它不导入 connector、其他 domain、SQLAlchemy 或 app。DNS/Gmail 调用只允许存在于后续 connector/app composition。

## 五、领域模型

### 5.1 域名角色

`DomainRole` 保持三个值：

- `COLD_OUTREACH`：唯一允许用于冷开发。
- `PRIMARY_BUSINESS`：主业务域，任何冷开发请求永久拒绝。
- `TRANSACTIONAL`：系统通知域，不占冷开发额度。

数据库表 `sending_domains` 以 `(tenant_id, normalized_domain)` 为主键或唯一键，角色是该行不可变属性。同一租户同一域名无法登记为两个角色。域名必须小写、去末尾点、IDNA 规范化；不接受邮箱地址、scheme、path、端口、空标签或 localhost。

### 5.2 身份状态

```text
CREATED → AUTH_PENDING → WARMING → ACTIVE
               │            │         │
               └────────────┴─────────┼→ THROTTLED
                                      ├→ SUSPENDED
任意非 RETIRED 状态 ──────────────────┴→ RETIRED
THROTTLED → WARMING 或 ACTIVE
SUSPENDED → WARMING 或 ACTIVE
RETIRED → 无后继
```

规则：

- `register` 只创建 `CREATED`。
- `begin_authentication` 把 `CREATED` 变为 `AUTH_PENDING`。
- `record_authentication_result` 只记录 typed 结果；部分通过仍为 `AUTH_PENDING`。
- 三项认证全过后，仍需显式 `start_warmup` 才进入 `WARMING`。
- `advance_warmup` 由 SYSTEM 定期调用；第 29 个自然日及以后转为 `ACTIVE`，只发布一次 `SendingIdentityActivated`。
- 任一可发送状态发生认证退化，立即 `SUSPENDED`，原因固定为 authentication regression。
- `THROTTLED` 在 Phase 1 阻断所有新冷开发发送。
- 进入 `THROTTLED/SUSPENDED` 时持久化 `sendable_state_before_restriction`，值只能是 `WARMING/ACTIVE`；从 THROTTLED 再升级到 SUSPENDED 时不得覆盖它。
- `THROTTLED` 恢复和 boss 人工恢复 `SUSPENDED` 都回到 `sendable_state_before_restriction`，因此预热中的身份绝不能借一次熔断恢复跳到 `ACTIVE`。
- `RETIRED` 不可恢复。

### 5.3 认证结果

`AuthenticationResult` 包含：

- `checked_at`（UTC aware）
- SPF/DKIM/DMARC 三个 typed 布尔结果
- 每个未通过项的固定类别和可执行修复说明
- connector 生成的安全 `check_ref`

领域层不接收 DNS 原始响应、HTTP headers 或异常文本。认证历史只增不改，最新结果通过有序查询获得。

### 5.4 预热计划

Phase 1 的 `target_daily_volume` 必须是整数 `5..100`。计划固定覆盖 28 天，不能由调用方提交任意曲线：

| 天数 | 当日上限 |
|---|---:|
| 1–3 | `min(5, target)` |
| 4–7 | `min(15, target)` |
| 8–14 | `min(30, target)` |
| 15–21 | `min(50, target)` |
| 22–28 | 从 `min(50, target)` 到 target 的七天单调整数插值 |
| 29 起 | target |

令 `base = min(50, target)`，第 `d` 天（`22 <= d <= 28`）的上限精确为
`base + ceil((target - base) * (d - 21) / 7)`，全程只用整数运算实现
`ceil`，不得经 float。开始日期之前上限为 0。计划生成函数必须证明：第一天不超过 10、全程单调不减、不超过 target、第 28 天达到 target。即使 target 为 5，身份仍经历完整 28 天的 `WARMING`；禁止 `skip_warmup`。

### 5.5 信誉事件

`DeliveryEventType` 为 typed enum：

- `DELIVERED`
- `HARD_BOUNCED`
- `SOFT_BOUNCED`
- `COMPLAINT`
- `UNSUBSCRIBED`
- `SPAM_TRAP`
- `BLOCKLISTED`

`DeliveryEventRecord` 必须带 `tenant_id`、`identity_id`、`occurred_at`、`dedup_key` 和安全 `source_ref`。不接收字符串自由枚举，不保存原始邮件正文或 webhook payload。

滚动窗口默认 7 天，边界为 `[computed_at - 7 days, computed_at]`。比率分母使用 `sent_attempts`；分母为 0 时返回 `Decimal("0")`。

### 5.6 默认阈值

使用当前领域骨架中更保守的 Phase 1 默认值：

| 指标 | THROTTLED | SUSPENDED |
|---|---:|---:|
| 硬退信率 | 3% | 5% |
| 投诉率 | 0.1% | 0.3% |

- 普通比率只有 `sent_attempts >= 50` 时参与状态判断。
- 触发比较使用 `metric >= threshold`；等于阈值即采取更保守的限制动作。
- `SPAM_TRAP` 或 `BLOCKLISTED` 命中一次立即停用，不受样本量限制。
- 身份窗口与域名聚合窗口分别评估；域名达到阈值时，同域所有可发送身份一起限流或停用。
- 自动从 `THROTTLED` 恢复时，身份和域名窗口都必须严格低于对应 throttle 阈值的 80%，且没有 spam trap/blocklist 命中。此检查由 SYSTEM 的显式 `resume_from_throttle` 执行并留痕，避免读取操作隐式改状态。

## 六、公共服务契约

所有方法都必须显式携带 domain-local `Actor`，先 typed authorize，再访问 tenant-bound UoW。

### 6.1 管理写操作

- `register(tenant_id, request, *, actor) -> SendingIdentityId`
- `begin_authentication(tenant_id, identity_id, *, actor) -> None`
- `record_authentication_result(tenant_id, identity_id, result, *, actor) -> None`
- `start_warmup(tenant_id, identity_id, target_daily_volume, *, actor) -> None`
- `advance_warmup(tenant_id, identity_id, *, actor) -> None`
- `resume_from_throttle(tenant_id, identity_id, *, actor) -> None`
- `resume_from_suspension(tenant_id, identity_id, investigation_note, *, actor) -> None`
- `retire(tenant_id, identity_id, reason, *, actor) -> None`

### 6.2 发送门禁

- `check_send_permission(..., for_cold_outreach: bool, *, actor) -> SendPermission` 是只读视图，供 UI/诊断使用，不能作为外部发送的最终依据。
- `reserve_send_slot(..., reservation_key: IdempotencyKey, for_cold_outreach: bool, *, actor) -> SendReservation` 是真实发送前唯一权威入口。

上述方法的当前时间和当前 UTC 日期只来自 service 注入时钟。调用方不能提交
`started_on/on_day/computed_at` 来回拨预热进度或切换到另一天额度；测试通过可控注入时钟推进时间。

`reserve_send_slot` 在一个事务中：

1. 锁定 tenant-bound identity/domain/counter 行。
2. 重新校验角色、状态、最新认证、预热日期和域名级状态。
3. 检查 `(tenant_id, identity_id, reservation_key)`；已存在则返回原 reservation，不重复计数。
4. 验证当日计数小于上限。
5. 插入 reservation 并把 `sent_attempts` 原子加一。
6. 提交后写一条 allow audit。

所有会同时修改同域多个身份的事务固定先锁 domain row，再按 `identity_id` 升序锁 identity rows，
最后锁 counter/reservation；不同代码路径不得改变锁序。状态转换与 outbox 使用稳定 dedup key，
重复评估不会重复发布熔断事件。

发送预留不退款。网络超时时无法证明 Gmail 未接受请求；退款会允许重试突破日限额。后续 Tool Gateway 幂等命中不会重复调用本方法。

### 6.3 信誉写操作

- `record_delivery_event(tenant_id, identity_id, event, *, actor) -> bool`
- `evaluate_reputation(tenant_id, identity_id, *, actor) -> ReputationView`

重复 `dedup_key` 返回 `False`，不重复计数、不重复发事件。外部事件的
`occurred_at` 必须是 UTC aware、不得早于身份创建时间、不得晚于 service 当前时钟五分钟以上。
新事件与可能的状态变化、outbox 通知在同一事务提交。

### 6.4 查询

- `get`
- `list_available_for_campaign`
- `get_domain_reputation`
- `get_warmup_progress`
- `check_send_permission`

`list_available_for_campaign` 必须在 SQL 中同时过滤 tenant、`COLD_OUTREACH`、最新认证全过、状态为 `WARMING/ACTIVE`；服务层再逐行防御性复核。不能先按 tenant LIMIT 再在 Python 过滤。

## 七、权限与审计

### 7.1 Typed action

`SendingIdentityAction` 至少包含：

- `IDENTITY_REGISTER`
- `AUTH_CHECK_BEGIN`
- `AUTH_RESULT_RECORD`
- `WARMUP_START`
- `WARMUP_ADVANCE`
- `IDENTITY_READ`
- `IDENTITY_LIST`
- `REPUTATION_READ`
- `SEND_PERMISSION_READ`
- `SEND_SLOT_RESERVE`
- `DELIVERY_EVENT_RECORD`
- `REPUTATION_EVALUATE`
- `THROTTLE_RESUME`
- `SUSPENSION_RESUME`
- `IDENTITY_RETIRE`

### 7.2 Phase 1 矩阵

`SendingIdentityScope` 是 domain-local frozen DTO，包含 `level`、
`allowed_identity_ids` 和 `allowed_domains`。`None` 表示该维度不限制，空集合表示全拒；
MANAGER 必须至少显式收窄一个维度。SYSTEM 对具体资源执行写操作时必须携带包含目标
identity 的单例 `allowed_identity_ids`，不能用无限制 SYSTEM 身份写任意记录。

| actor | scope | 允许操作 |
|---|---|---|
| boss | TENANT | register、begin auth、start warmup、read、人工恢复 suspension、retire |
| manager | MANAGER（显式 identity/domain 集合） | identity/reputation/permission read |
| system | SYSTEM（具体 identity 单例） | record auth result、advance warmup、reserve、record delivery、evaluate、resume throttle、read 目标资源 |
| sales | SELF | 无发件身份管理权限 |

未列角色、scope、action、错误 tenant、空 actor 或 `scope != actor.scope` 一律 `PermissionDenied`。SYSTEM 不获得 register、人工恢复或 retire 权限。

### 7.3 审计顺序

所有写操作固定为：

```text
typed require
→ tenant-filtered load
→ resource/domain ABAC
→ state/input validation
→ atomic DB write + outbox
→ UoW commit
→ exactly one allow audit
```

拒绝、验证失败、状态失败、并发失败、commit 失败均没有 allow audit。deny audit 只记录 `actor/action/tenant_id/scope/rule`；不记录 address、domain、connector_ref、DNS details、调查文本或事件 source_ref。

## 八、PostgreSQL 数据模型

### 8.1 `sending_domains`

- `tenant_id`
- `domain`
- `role`
- `created_at`
- 主键/唯一：`(tenant_id, domain)`

角色不可原地修改。需要变更时先退役全部身份，再登记新域名；Phase 1 不提供转换接口。

### 8.2 `sending_identities`

- composite identity：`(tenant_id, identity_id)`
- domain composite FK
- normalized address、display name、state、connector ref
- started/activated/suspended/retired timestamps
- `suspended_from_state`、固定 suspension category
- target volume 与各 Decimal 阈值
- `sendable_state_before_restriction` 与 optimistic version 或 row lock 所需列

同一 tenant 下 address 唯一。邮箱域必须与关联 `sending_domains.domain` 精确一致。

### 8.3 `sending_auth_checks`

append-only；包含 check ID、tenant/identity composite FK、checked_at、三个结果、安全修复说明和 check_ref。禁止 UPDATE/DELETE trigger 与现有 append-only 表模式一致。

### 8.4 `sending_reputation_events`

append-only；唯一 `(tenant_id, dedup_key)`；带 identity composite FK、typed event、occurred_at、source_ref。所有窗口查询同时过滤 tenant、identity/domain、时间边界。

### 8.5 `sending_daily_counters`

主键 `(tenant_id, identity_id, on_day)`，`sent_attempts >= 0`。只允许原子 increment。

### 8.6 `sending_send_reservations`

唯一 `(tenant_id, identity_id, reservation_key)`，记录 on_day、序号和 created_at。reservation 与 counter 在同一事务写入。

### 8.7 `sending_identity_actions`

append-only；记录状态变化、actor ID、action、before/after、occurred_at 和必要的人工调查 note。note 只在授权查询中返回，不进入日志/事件。

### 8.8 ORM 与迁移一致性

迁移、SQLAlchemy Row、repository 映射必须逐列一致。复合 FK、CHECK、唯一键、append-only trigger 都必须用真实 PostgreSQL 测试；SQLite/fake 不能证明这些约束。

## 九、事件与事务

继续复用现有 durable outbox。4A 发布：

- `SendingIdentityActivated`
- `SendingIdentityThrottled`
- `SendingIdentitySuspended`
- `ReputationThresholdBreached`

事件不包含邮箱、域名、DNS details、connector ref 或调查 note，只带 tenant、identity ID、typed threshold/category、时间和安全 dedup key。

身份状态必须先在同一事务落库，再发布 outbox。业务事务回滚时状态、action history 与 outbox 全部不存在。通知投递失败不能回滚已经生效的熔断。

## 十、错误处理

- 角色冲突、格式和阈值错误：`ValidationError` 子类。
- 非法状态转换：`InvalidStateTransition`。
- 主业务/通知域用于冷开发：`ColdOutreachDomainViolation`。
- 认证未通过：`AuthenticationNotVerifiedError`。
- 预热/日额度不足：`WarmupLimitExceededError`。
- suspended/retired：固定 `PolicyViolation` 子类。
- tenant mismatch：`TenantIsolationViolation` + 固定 CRITICAL 安全日志。
- 数据库、驱动和未知异常不转换为包含原文的领域错误；上层统一脱敏。

错误消息可包含安全的 state、阈值和剩余额度，不得包含 address、domain、DNS 内容、connector ref 或原始事件。

## 十一、测试设计

### 11.1 单元测试

- 完整状态转换表与每条非法转换。
- 域名和邮箱规范化、connector ref 凭证形态拒绝。
- SPF/DKIM/DMARC 组合门禁。
- 注入时钟下预热日 0、1、3、4、7、8、14、15、21、22、28、29 的精确边界；public API 不接受回拨日期。
- target 5/50/100 的单调、封顶与完整 28 天性质。
- `Decimal` 比率、零样本、边界等于/高于阈值。
- minimum sample、硬退信、投诉、spam trap、blocklist。
- 身份级与域名级熔断、throttle 恢复的 80% 条件。
- Phase 1 authorizer 全 allow/deny 矩阵。
- 所有拒绝路径零 allow audit。

### 11.2 真实 PostgreSQL 集成测试

- 迁移与 ORM parity、复合 FK、CHECK、unique、append-only trigger。
- repository 每个读写方法 tenant isolation。
- 同域不同角色、同租户重复地址、跨租户 FK 全部拒绝。
- auth/reputation/action history 只增不改。
- delivery dedup 不重复计数和熔断。
- 并发 N 个 reservation 在 cap 边界上成功数恰好等于剩余额度。
- 同 reservation key 并发重试只占一个名额。
- 状态、action、outbox 原子提交；人为 commit failure 全回滚且零 allow。
- `list_available_for_campaign` 在 WHERE/LIMIT 前完成所有过滤。
- 认证退化与域名聚合熔断在竞争下不漏停、不重复事件。

### 11.3 演示测试

新增只连接测试 PostgreSQL 的 `scripts/demo_sending_identity.py`：

1. 登记冷开发域和身份。
2. 记录三项认证通过。
3. 启动预热并并发占用有限名额。
4. 注入去重后的 hard bounce/complaint 事件。
5. 触发域名级熔断。
6. 从数据库回读身份状态、计数、history 和 outbox。

脚本不读取 Gmail/DNS 凭证、不访问网络、不打印 address/domain/connector ref。失败只输出固定中文消息。

### 11.4 门禁

每个实现任务都遵循：

1. genuine RED，排除 fixture/import/PATH/Docker 假失败。
2. 最小 GREEN。
3. affected focused tests。
4. `make check`。
5. `pytest tests/integration -q -W error`。
6. `python3 scripts/check_boundaries.py`。
7. sensitive scan 与 `git diff --check`。
8. 独立实现审查与安全审查；所有 Critical/Important 先 RED 后修。
9. 普通 commit，不 amend；push 后等待 GitHub CI success。

Python 命令固定使用 `tradeos-py312` conda 环境。

## 十二、实施分批

4A 后续 implementation plan 应拆为可独立推送的小任务：

1. 纯领域模型、typed contracts、权限矩阵与单元测试。
2. PostgreSQL migration、ORM、repositories、UoW 与真实约束测试。
3. service implementation、outbox、并发 reservation、信誉熔断与真实集成测试。
4. 无网络演示脚本、完整审查、全门禁与文档同步。

任何一批若发现必须引入 Gmail、Tool Gateway、outreach、approvals 或 UI，立即停止并回到 4B/4C 的独立设计，不在 4A 中越界。

## 十三、验收标准

4A 完成必须同时满足：

1. 主业务域和 transactional 域无法用于冷开发，且数据库和服务双层证明。
2. 未完成认证或预热的身份不能占用发送名额。
3. 并发预留永不突破当日上限，幂等重试不重复占用。
4. 信誉指标用 Decimal、滚动窗口和最小样本确定性计算。
5. 身份级与域名级阈值自动熔断；spam trap/blocklist 立即停用。
6. suspended 只能由 boss 带调查记录恢复；retired 永不可恢复。
7. 所有数据 tenant-filtered，所有状态变化与 outbox 原子，审计在 commit 后。
8. 模型、日志、异常、事件和测试输出都未接触凭证或客户内容。
9. demo 通过真实 PostgreSQL 证明完整行为，且无外网访问。
10. 所有本地门禁、独立 review 和远端 CI 全绿。
