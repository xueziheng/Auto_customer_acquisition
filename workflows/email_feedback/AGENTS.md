# workflows/email_feedback/ —— 邮件反馈整页事务

本目录继承根目录与 `workflows/AGENTS.md`，并进一步收紧：

- Gmail 网络读取必须在业务事务外完成；本流程只接收 provider-neutral typed page。
- receipt、quarantine、两域 effect/outbox 与 cursor 必须由同一个外层 UoW 一次提交。
- 反馈匹配只允许 route-scoped 精确关联键；禁止地址、主题、时间或正文模糊匹配。
- hard bounce 才能自动形成 ContactPoint suppression；soft bounce 只记录 receipt。
- 日志、审计和异常不得携带邮箱地址、Message-ID、provider cursor/event 原文、token 或凭证。
- duplicate receipt 不得再次调用域服务、写 action/outbox 或写 allow audit。
