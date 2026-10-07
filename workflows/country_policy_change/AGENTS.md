# 国家政策包变更工作流规则

本目录继承根目录与 `workflows/AGENTS.md`，并追加以下约束：

1. 只依赖合规域与审批域的公共服务、公共 DTO 和公共错误；不得导入域模型、仓储或 UoW。
2. 工作流 context 与 outbox payload 只保存强类型 ID 的字符串形式、规范化国家键、内容哈希、
   安全 change-set reference 和固定状态码；不得保存政策正文、网页正文、凭证或 PII。
3. 国家政策激活必须先通过合规域的持久化幂等机制提交，再调用审批服务
   `mark_applied`；不得先标记审批已应用。
4. proposal 事件和 API 直接启动必须共享 tenant＋version 派生的确定性幂等键；proposal
   事件负责修复候选事务提交后、API 启动前崩溃的窗口。
5. 可确定的基线、审批事实和激活冲突必须转为固定 `apply_failed` 原因码；基础设施异常继续
   抛出，交给工作流引擎重试。
