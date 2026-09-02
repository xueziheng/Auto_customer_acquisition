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
- Directives 仍是寻源政策的唯一来源；就绪事件仅负责排队，不代表启动寻源。
- 本决策不包含 catalog proposal。

## 后果

后续准入消费者可以按新增事件构建 tenant-bound 的审计事实，但必须自行依据当前
Directive 决定是否入队；不能把成员数或事件投递解释为已开始寻源、询价或报价。
