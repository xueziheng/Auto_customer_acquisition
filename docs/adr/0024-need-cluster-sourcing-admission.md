# ADR 0024：需求簇成员变更与寻源准入契约

## 状态

已接受。

## 背景

需求簇形成后仍会持续接纳已验证需求。仅有 `NeedClusterFormed` 无法表达某一条
Validated Need 何时归入既有簇，也无法让后续寻源准入逻辑在不重写历史事件的前提下读取
变更后的累计成员事实。

## 决策

- 新增过去式事件 `NeedClusterMembershipChanged`，携带 tenant、发生时间、可选 Run、
  `NeedClusterId`、变更的 `ValidatedNeedId` 与变更后的正整数成员数。
- 新增 `SourcingAdmissionId` 与 `SourcingPrioritySnapshotId` 强类型标识，为后续准入记录和
  优先级快照保留独立命名空间。
- 事件在 Outbox 白名单中以其既有类名序列化；空簇 ID、空 Need ID 或非正成员数在写入与
  反序列化边界均拒绝，不能被下游当作有用事实。
- `NeedClusterFormed`、`NeedValidated`、`NeedBecameSourcingReady` 的字段及其序列化名称
  保持不变；历史 Run 不补发、不改写。
- `NeedBecameSourcingReady` 无条件创建持久化、待评估的 admission（入队），不直接启动
  Workflow；未配置或不可读取的政策也不得丢弃该待评估项。
- Directives 仍是自动寻源准入政策的唯一来源。可选的 `sourcing_admission` 段必须完整包含
  `mode: cluster_ranked`、显式的 `automatic_admission_enabled` 与 `batch_limit`；`batch_limit`
  必须为 `1..50` 的整数，关闭自动准入时也不得省略。只有当前 active Directive 的该段完整且
  `automatic_admission_enabled=true` 时，准入 driver 才能 claim admission 并启动 Workflow。
  缺少该段（包括历史 Directive）是 `policy_not_configured`，关闭是
  `automatic_admission_disabled`，读取状态未知是 `policy_status_unknown`；三种状态均启动
  零个 Workflow，且保留 waiting admission 供后续评估、政策启用或人工准入。
- 本决策不包含 catalog proposal。

## 后果

后续准入消费者必须按 readiness 创建 tenant-bound 的审计 admission；当前 Directive 只在
自动 admission、claim 与 Workflow start 时生效。成员数、入队或事件投递都不能解释为已经
开始寻源、询价或报价。
