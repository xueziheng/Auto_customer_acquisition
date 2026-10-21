# ADR 0102：信息不足的客户回复进入现有负责人核对队列

状态：隔离实现，尚未部署。2026-10-16。

## 背景

原 14 类业务分类没有中性的人工核对结果。输入只有当前单条回复的 subject/body 投影；不能据此声称已理解完整历史。缺失上下文或互相矛盾的表达不应被猜成兴趣或拒绝。

## 决定

保留原 14 类，增加 `needs_review`，确定性动作只有 `stop_sequence` 与 `review_reply`。新动作和 `reply_review` 队列复用原 `conversation_reply_work`、当前客户归属、员工 Inbox 权限、状态处理、租户过滤与 `reply:{action}:{message_id}` 幂等键。模型输出仍只有 category、candidate_fields 和退订专用 suppress_scope。核对结果不得携带需求字段，不创建 Need、Opportunity、Handoff 或 suppression。

仅当完整原件先通过既有安全护栏、预算，但无法分离当前表达时，reader 抛出专用 `ReplyContentNeedsReview`；workflow 以 `system:reply-content-review-v1` 留痕，重核当前授权，再持原分类事务锁写入中性结果。这不伪装成模型判断。权限、原件完整性、凭证护栏、超限、provider/模型结构错误仍失败关闭，不宽泛吞异常。

私有测试也复用同一核对工作项，不能借测试用途创建正式业务成果。人工通过原会话查看证据并纠正分类；标记工作完成不自动重放历史动作、不自动生成商机或外发消息。

WhatsApp 请求仍为 willing_to_continue，显示在原会话及原需求确认待办；没有 WhatsApp 自动联系或新模块。老板通知仍仅六种原正向 customer_reply 类别；核对结果不增加通知。通知 UI 只补固定中文类型名，不读取客户全文、生成模型总结或监看全员聊天。

## 兼容与验收

迁移 0078 只扩展原分类/纠正/action/queue CHECK，并限制核对字段为空。旧值保持兼容；存在新值时拒绝 downgrade，不清理客户数据。必须在独立数据库验证 upgrade → downgrade → upgrade、ORM 一致性及回退保护。

验证原分类契约、明确退订优先、模糊路径、安全/权限错误不被转成核对、工作当前归属与转交撤权、跨租户不可见、并发幂等及不增加老板通知。冻结语料只追加十条人工核对样本。离线测试、关键字基线与真实模型语义测试分别报告，不把 stub 测试声称为模型效果。

生产发布须单独获准并重核最新 release；本隔离实现不部署、不迁移生产库、不重跑真实邮件。
