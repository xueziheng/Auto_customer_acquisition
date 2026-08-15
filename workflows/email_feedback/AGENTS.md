# workflows/email_feedback/ —— 邮件反馈整页事务

本目录继承根目录与 `workflows/AGENTS.md`，并进一步收紧：

- Gmail 网络读取必须在业务事务外完成；本流程只接收 provider-neutral typed page。
- receipt、quarantine、两域 effect/outbox 与 cursor 必须由同一个外层 UoW 一次提交。
- 外层 UoW 必须按 tenant 持有事务级反馈锁；不同 mailbox 也不得形成 receipt 外键锁与域资源锁的逆序死锁。
- 反馈匹配只允许 route-scoped 精确关联键；禁止地址、主题、时间或正文模糊匹配。
- receipt 必须持久化安全的整项 fingerprint；相同 provider event 但 payload 不同必须在调用域服务前整页失败。历史无指纹 receipt 使用保留标记并 fail closed。
- hard bounce 与 complaint 自动形成精确 ContactPoint suppression；soft bounce 只记录 receipt。
- 日志、审计和异常不得携带邮箱地址、Message-ID、provider cursor/event 原文、token 或凭证。
- duplicate receipt 不得再次调用域服务、写 action/outbox 或写 allow audit。
- deny audit 立即写安全审计；allow audit 只在外层提交成功后刷新。跨路由 CRITICAL 只在隔离提交成功后发出。
