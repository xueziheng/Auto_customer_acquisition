# ADR0068：持久本机内测仅投递站内通知

状态：已接受。日期：2026-09-07。

## 背景

持久 Web 内测使用真实账号、数据库和对象资料，但本轮未配置邮箱与外部 Provider。
默认生产通知组合要求邮件，不能通过假邮件客户端把任务标成成功，也不能把真实内测称为合成演练。

## 决策

增加显式 `NotificationRuntimeMode.LOCAL_IN_APP`，仅 `apps/notification_worker/pilot.py`
选择此模式。复用原站内通道、持久 job、模板、优先级、受众和 dedup 策略；完成只承诺站内。
健康端点返回 `mode=local_in_app`、`email=disabled`。API 与 worker 仍为独立进程，
外部动作仍受 Gateway 和本 profile loopback 网络边界约束。通知健康仅监听 loopback。

默认 PRODUCTION 缺少邮件仍拒绝；CONTROLLED_IN_APP 保留原语义。此决策不增加通知渠道，
不放宽根九条硬边界，不允许 Agent 作价格、交期、合同或外发承诺。

## 代价与验收

操作者必须进入 Web 查看通知，不能依赖邮件提醒；没有邮件回执或多渠道送达保证。
回归验证真实 PostgreSQL 站内记录与 job 完成、健康披露、默认模式拒绝及资源对称关闭。
未来启用邮件须独立配置与验收，不复用本站内完成状态推断历史邮件送达。
