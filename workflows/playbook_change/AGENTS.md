# Company Playbook 变更工作流规则

本目录继承根目录与 `workflows/AGENTS.md`，并追加以下约束：

1. 只依赖组织域与审批域的公共服务、公共 DTO 和公共错误；不得导入域模型、仓储或 UoW。
2. 工作流 context 与 outbox payload 只允许保存强类型 ID 的字符串形式、状态、固定原因码和内容哈希；不得保存完整 Playbook、客户资料或任何凭证形态字段。
3. Playbook 激活必须先通过组织域的持久化幂等机制提交，再调用审批服务 `mark_applied`；不得先标记审批已应用。
4. 可确定的策略冲突必须转为 `apply_failed` 固定原因码；基础设施异常必须继续抛出，交给工作流引擎重试。
