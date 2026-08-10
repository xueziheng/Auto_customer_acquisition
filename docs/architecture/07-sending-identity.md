# 发件身份与域名信誉

发件身份不是 Connector 配置项，而是保护公司邮件基础设施的业务域。主业务域一旦被
冷开发的退信或投诉拖累，报价、合同和客户往来也会失去可达性。因此架构硬规则是：

> **冷开发域与主业务域物理隔离，主业务域永不用于冷开发。**

当前实现位于 `domains/sending_identity/`。Slice 4A 只交付无网络的领域纵切面：
生命周期、认证事实、预热额度、原子发送名额、滚动信誉、域聚合熔断、权限审计和
PostgreSQL 持久化。它不持有邮箱或 DNS 凭证，也不发送邮件。

## 一、域名与身份

域角色使用固定 wire value：

| role | 用途 | 冷开发 |
|---|---|---|
| `cold_outreach` | 独立冷开发域 | 允许 |
| `primary_business` | 人工往来、报价、合同 | 永不允许 |
| `transactional` | 系统事务通知 | 不允许 |

`sending_domains` 以 `(tenant_id, normalized_domain)` 唯一标识域；域名小写、去末尾点
并做 IDNA 规范化。同一租户同一域的角色不可变，避免两个身份对域用途作出冲突解释。

一个 sending identity 记录地址、域、角色、状态、预热计划、固定信誉阈值与安全的
connector reference。reference 只是未来装配层的定位符，不是凭证；地址、域名、
reference 和原始 DNS/provider payload 不得进入授权日志或错误边界。

## 二、状态机与认证门禁

```text
created → auth_pending/retired
auth_pending → warming/retired
warming → active/throttled/suspended/retired
active → throttled/suspended/retired
throttled → warming/active/suspended/retired
suspended → warming/active/retired（其中恢复到 warming/active 仅 boss 带调查记录）
retired → 无后继
```

SPF、DKIM、DMARC 检查结果使用 typed DTO，只保存固定检查状态、失败类别、修复代码和
安全 reference；不接收原始 DNS 记录或异常文本。三项全部通过后仍须由 boss 显式开始
预热。可发送状态的最新认证若退化，身份立即 `suspended`，类别固定为
`authentication_regression`。

限制前状态持久化在 `sendable_state_before_restriction`。恢复只能回到原来的
`warming` 或 `active`，预热身份不能借恢复跳到 active。`retired` 不可逆。

## 三、固定 28 天预热

调用方只提供 5–100 的整数 target，不得注入自定义日期或 schedule；日期来自服务的
UTC 注入时钟。

| 自然日 | 当日上限 |
|---|---:|
| 开始日前 | 0 |
| 1–3 | `min(5, target)` |
| 4–7 | `min(15, target)` |
| 8–14 | `min(30, target)` |
| 15–21 | `min(50, target)` |
| 22–28 | 从 `min(50,target)` 用整数上取整确定性爬升到 target |
| 29 起 | target；第 29 个自然日才完成预热 |

例如 target=100 时，第 1 天只能占用 5 个发送名额；第 28 天额度已到 100，但身份仍是
`warming`，第 29 天显式 `advance_warmup` 后才进入 `active`。

## 四、发送前的唯一权威门禁

`check_send_permission` 是 UI/诊断快照，不能缓存为发送授权。真实发送前必须调用
`reserve_send_slot`，它在一个事务中：

1. 以 tenant 绑定读取并锁定 domain、identity；
2. 重查域角色、身份状态、最新认证、当前 7 天信誉窗口和当日额度；
3. 按 `(tenant_id, identity_id, reservation_key)` 查幂等命中；
4. 原子递增 daily counter，并写 immutable reservation。

同 key 重试返回原 reservation 和 sequence，不重复计数。不同 key 到 cap 后固定拒绝。
发送预留不退款：外部超时时不能证明 provider 未接受请求，退款会让重试突破上限。

Phase 1 的 `throttled` 会阻断所有新冷开发，而不是“只发给已回复联系人”。后者依赖
联系人、Campaign 和 suppression 数据，均不在 4A。

## 五、Decimal 滚动信誉与域聚合

身份级和域名级窗口均为 `[computed_at-7d, computed_at]`。发送分母从窗口内 immutable
reservations 的 `created_at` 计数，不能从生命周期总量或 daily counter 推测。所有比率
用 `Decimal` 计算和比较，数据库阈值为 `NUMERIC(9,6)`；未来 wire 只能用十进制字符串，
不能用 JSON number/float。

普通比率至少需要 50 个发送样本，比较使用 `>=`：

| 指标 | `throttled` | `suspended` |
|---|---:|---:|
| hard bounce rate | `.03` | `.05` |
| complaint rate | `.001` | `.003` |

spam trap 或 blocklist 任一命中是 immediate hazard，不等 50 个样本，直接停用。
不存在持久化的 `healthy` 或 `watch` identity 状态，也没有“单日退信数”阈值。

域名窗口汇总同域 identities 的 reservations 与 delivery facts，并采用成员中最保守的
确定性阈值。域窗口命中时，所有同域 `warming`/`active` 身份同时受限；已有
`throttled` 身份只可升级为 `suspended`。域聚合的 Phase 1 价值是合并各身份不足 50 的
样本，而不是声称同一阈值下的加权比率能高于每个成员比率。

每条 delivery event 以 tenant 级 dedup key 只增写入，并在同一 UoW 立即评估。每个实际
状态变化写一条 action 和一条对应 outbox，触发源再写一条
`ReputationThresholdBreached`；事务失败时三者一起回滚，重复事件或重复评估不重复发布。

## 六、恢复规则

- `resume_from_throttle` 只允许目标 identity 的 SYSTEM 单例 scope 显式调用。身份与域窗口
  的 hard bounce 都须严格 `< .024`，complaint 都须严格 `< .0008`，且没有 spam trap 或
  blocklist；等于 80% 线仍拒绝。最新认证也必须全部通过。
- `resume_from_suspension` 只允许 boss/TENANT，要求最新认证全过和 strip 后 1–1000 字符
  调查记录。调查记录只进 action history，不进 audit/event/error。
- 两者都恢复到限制前保存的 `warming` 或 `active`。读取方法从不隐式恢复。

## 七、权限、事务与持久化

Phase 1 权限默认拒绝：boss/TENANT 管登记、认证启动、预热启动、读取、suspension 人工
恢复和退役；manager 只有收窄 scope 的读取；SYSTEM 必须绑定目标 identity 单例，负责
认证结果、预热推进、reservation、delivery fact、信誉评估和 throttle 恢复；sales/SELF
无发件身份管理权限。

每个 repository 都绑定 tenant，跨租户复合外键防止错误关联。七张业务表是：

```text
sending_domains                 域角色
sending_identities              身份与当前状态
sending_auth_checks             认证历史（只增）
sending_reputation_events       投递事实（只增、dedup）
sending_daily_counters          单调日计数
sending_send_reservations       不可退款预留（只增）
sending_identity_actions        状态动作与人工记录（只增）
```

状态、history 和 durable `outbox_events` 共用一个 AsyncSession/UoW。只有提交成功后才写一条
allow audit，固定安全字段为 `actor/action/tenant_id/scope/rule`。

发布事件：`SendingIdentityActivated`、`SendingIdentityThrottled`、
`SendingIdentitySuspended`、`ReputationThresholdBreached`。

## 八、无网络 PostgreSQL 演示

`scripts/demo_sending_identity.py` 只读取 `DATABASE_URL`，不访问 Gmail、DNS 或网络服务。
测试先把真实 testcontainers PostgreSQL 迁移到 Alembic head，再以仅含该变量的环境运行
子进程。演示使用真实 service、authorizer、UoW 和注入时钟，不直接写七张业务表：

1. 随机 tenant 下登记同一 cold-outreach domain 的两个 identities，完成 typed auth 和预热；
2. 第 1 天对第一身份以 20 个不同 key 并发预留，恰 5 个成功；同 key 重试不增量；
3. 第 29 天两身份各预留 25 个：全生命周期共 55 行，7 天窗口只有这 50 行；
4. 三个唯一 hard bounce 令每身份 `25 < minimum_sample`，但域聚合为
   `Decimal('3') / Decimal('50') == Decimal('.06')`，两身份一起 suspended；重复 delivery
   dedup 不增量。

成功 stdout 只有一行安全 JSON，只含随机 tenant/identity IDs、最终状态和计数，不含
address/domain/reference；失败只输出固定中文边界，不回显 DSN、driver 或 traceback。
集成测试使用独立 engine/session 按 tenant 回读七张业务表与 outbox，并在同一数据库连跑
两次验证隔离。

## 九、Slice 边界

Slice 4A 明确不含 suppression、联系人可达性、Campaign 配额/内容/审批、Gmail/DNS
Connector、Tool Gateway、真实发送、退信 webhook 解析、API/UI 和通知渠道。后续能力须经
各自 brainstorming/spec 门禁；4A 的完成不能提前代表 HANDBOOK Slice 4 整体完成。
