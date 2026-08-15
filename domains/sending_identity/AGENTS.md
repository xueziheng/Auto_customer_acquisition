# domains/sending_identity/ —— 发件身份域

## 职责与边界

本域回答三个问题：发件身份当前是否可发送、今天还能原子占用多少额度、
信誉恶化时身份或同域身份是否必须立即受限。冷开发域必须与主业务域物理隔离：

> **冷开发邮件永远不与主业务邮件共用域名。**

本域只管理策略、状态与持久化事实，不接触凭证，不查询 DNS，不调用 Gmail，
也不发送邮件。外部发送方不能自行解释状态；真实发送前唯一权威入口是
`reserve_send_slot`，`check_send_permission` 只供诊断。

依赖白名单：

```text
允许   shared.events、shared.schemas、shared.errors
禁止   任何其他 domains/*
禁止   任何外部 SDK
```

## 固定领域语义

域角色的 wire value 只有：

```text
cold_outreach       冷开发专用域
primary_business    主业务域，禁止冷开发
transactional       系统事务通知域
```

同一租户内，规范化域名的角色不可变；需要换角色时必须退役旧身份后另行登记，
不得原地修改。地址、域名和 connector reference 都是敏感业务数据，不得进入
授权日志、异常或熔断事件。

状态转换为：

```text
created → auth_pending/retired
auth_pending → warming/retired
warming → active/throttled/suspended/retired
active → throttled/suspended/retired
throttled → warming/active/suspended/retired
suspended → warming/active/retired
retired → 无后继
```

从 `throttled` 或 `suspended` 恢复时，只能回到持久化的
`sendable_state_before_restriction`（`warming` 或 `active`），禁止借恢复跳过预热。
`throttled` 在 Phase 1 阻断所有新冷开发发送。`suspended` 只能由 boss/TENANT
带 1–1000 字符调查记录恢复；恢复前最新 SPF、DKIM、DMARC 仍须全部通过。

## 认证、预热与发送名额

- SPF、DKIM、DMARC 必须全部通过；认证事实是 typed、只增记录，不保存原始 DNS。
- 认证通过不会自动预热，必须显式 `start_warmup`；目标日量只能是 5–100 的整数。
- 固定 28 天曲线：第 1–3 天 5，第 4–7 天 15，第 8–14 天 30，
  第 15–21 天 50（均不超过 target）；第 22–28 天从 `min(50,target)` 确定性爬升到 target，
  第 29 个自然日才完成并可显式推进为 `active`。调用方不能传自定义 schedule 或日期。
- `reserve_send_slot` 在同一事务中重查角色、状态、最新认证、7 天信誉窗口和
  当日额度，再写 counter 与 immutable reservation。reservation key 重试返回原记录，
  不重复占额；成功占用不退款。

## 信誉与自动熔断

身份级和规范化域名级都按 `[computed_at-7d, computed_at]` 计算。分母来自该窗口
内 immutable reservations，不得使用生命周期总行数或 daily counter 代替；比率和
阈值只用 `Decimal`，数据库只用定点 `NUMERIC`。

普通比率在样本数至少 50 时按 `>=` 触发：

| 指标 | throttled | suspended |
|---|---:|---:|
| hard bounce rate | `.03` | `.05` |
| complaint rate | `.001` | `.003` |

任一 spam trap 或 blocklist 命中不等样本数，立即 `suspended`。域名窗口达到阈值时，
同域所有 `warming`/`active` 身份一起进入相同 restriction；`throttled` 只能进一步
升级为 `suspended`。每个状态变化必须与 action history、状态 outbox 同事务提交，
触发源另发一条 `ReputationThresholdBreached`。

`resume_from_throttle` 是 SYSTEM singleton-scope 的显式动作：身份和域名窗口的
hard bounce 都必须严格 `< .024`，complaint 都必须严格 `< .0008`，且无 spam trap
或 blocklist。读取操作不得隐式恢复状态。

## 权限、租户与审计

- 每个公共方法都要求 typed `Actor`；未列出的 role/scope/action 一律拒绝。
- boss/TENANT 管登记、认证启动、预热启动、读取、人工恢复 suspension 和退役。
- SYSTEM 必须是目标 identity 的单例 scope，负责认证结果、预热推进、reservation、
  投递事实、信誉评估和 throttle 恢复。
- 所有 repository 查询和写入必须显式绑定 `tenant_id`；域锁之后按 identity ID 升序锁。
- allow audit 只能在事务提交成功后写一条，字段固定为
  `actor/action/tenant_id/scope/rule`；deny 同样不得回显敏感输入。

发布事件：`SendingIdentityActivated`、`SendingIdentityThrottled`、
`SendingIdentitySuspended`、`ReputationThresholdBreached`。

## Slice 4A 明确不做

本 Slice 不含 suppression、联系人可达性、Campaign 配额或内容、Gmail/DNS Connector、
Tool Gateway、真实发送、退信 webhook 解析、API/UI 和通知渠道。不要在本域直接补这些
能力；它们必须先经过后续 Slice 的独立设计门禁。

## Slice 4 演示与验收

- 进程级演示与容器验收：`scripts/demo_slice4_manual_send.py`、
  `tests/integration/test_demo_slice4_manual_send.py`（同一 migrated PG 跑两次：
  不同租户、恰一次冷发、认证事实、熔断与第二次发送被真实门禁阻断、通知投递）。
- 演示只直插 employee 与受控连接器配置前置，不直插本域业务行；演示通过后必须
  由验收测试从数据库读回复核（不变量、租户隔离、无敏感字段持久化）。
- 运维手册：`docs/operations/slice4-email-operations.md`。演示与文档不构成能力
  声明；真实域名验收需独立外部前提，未完成前不得宣称 Slice 4 完成。
