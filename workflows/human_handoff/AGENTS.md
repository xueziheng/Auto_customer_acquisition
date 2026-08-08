# workflows/human_handoff/ —— 人工接管流程（Phase 1 浅）

## 触发

`HandoffRequested` 事件。

## 主要状态

```text
notify_owner（notification URGENT）
→ wait_acceptance（WAITING_EVENT(HandoffAccepted)，超时阈值 T1）
   ├── 接受 → complete
   ├── 超时 T1 → escalate_manager（通知经理）→ wait_acceptance(T2)
   └── 超时 T2 → escalate_boss → wait_acceptance（不再升级，持续提醒）
```

## 关键约束

超时升级不换负责人（换人是经理的决定，流程只通知）；每次升级记录进 handoff（SLA 违约审计）；队列积压统计由 opportunities 域的 get_queue_stats 负责，本流程只管单条。
