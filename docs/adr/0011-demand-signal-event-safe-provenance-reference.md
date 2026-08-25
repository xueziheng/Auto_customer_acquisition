# ADR 0011：需求信号事件只携带安全回查引用

- 状态：已接受
- 日期：2026-08-25

## 背景

`DemandSignalCaptured` 原先把完整 `source_url` 写入共享事件。公开页面 URL 的 path 和
query 可能包含联系人姓名、搜索参数或其他任意来源文本；事件会被持久化到 outbox，因而
把仅应存在于租户隔离 provenance 的内容复制到了更广的异步分发面。

需求信号表已经在同一事务保存完整 URL、页面 hash 与不可变快照引用。事件订阅方只需要
知道哪条信号已创建，可以用 tenant_id 与强类型 signal_id 通过 Demand 公共服务读取获授权
的事实和 provenance，不需要在事件中复制来源 URL。审计未发现依赖 `source_url` 的生产
消费者。

## 决策

从 `DemandSignalCaptured` 公共契约删除 `source_url`。事件只携带 tenant/run/time、
signal_id、entity_name 与 signal_type；其中 signal_id 是回查 tenant-bound Demand
provenance 的唯一安全引用。完整 URL 继续保存在 Demand 信号行，并通过 Demand 的租户过滤
读取接口提供给内部 UI。

## 理由

保留 signal_id 能维持事件的可关联性和证据追踪，同时避免把任意 URL path/query 复制进
durable outbox。相比仅让当前 producer 把 `source_url` 写成 `None`，删除字段能让类型契约
本身禁止后续 producer 再次泄漏，而不是依赖调用方自觉。

## 放弃的选项

- **事件继续携带完整 URL。** 消费方便，但扩大 PII/任意文本持久化面，不接受。
- **保留字段但固定写 `None`。** 对旧构造方兼容，但契约仍允许错误 producer 写入 URL，
  无法形成结构性边界。
- **改为只携带 artifact ID 或 page hash。** 两者同样是安全引用，但 signal_id 已能在租户
  边界内同时关联领域事实和全部 provenance；增加第二个引用会制造冗余一致性问题。

## 后果

旧的事件构造方和订阅方必须删除 `source_url` 依赖；部署中的历史 outbox 行仍按其原始
event_payload 保留，不做破坏性重写。新事件不会再携带 URL path/query。需要查看原始证据的
消费者必须以 tenant_id + signal_id 调用 Demand 公共读取接口并接受其授权与不存在语义。

## 何时重新审视

只有在出现不允许同步回查 Demand 服务、但又必须验证不可变网页证据的独立消费者时重新
评估；届时优先增加 artifact ID 或 hash 这类 metadata-safe 引用，不恢复完整 URL。
