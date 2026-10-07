# ADR 0079：操作者本人邮箱的单封投递测试

- 日期：2026-10-05
- 范围：操作者明确要求 TradeOS 向其本人指定邮箱发送一封收发测试邮件

## 背景

Campaign 的发件身份认证与预热控制用于业务触达。当前项目 Gmail 的商业认证仍未通过；不得为测试伪造 DNS 事实、客户联系人、Campaign 审批或送达记录。操作者此次明确要求的是向本人 QQ 邮箱发送测试邮件，不是给陌生客户开发信。

## 决策

新增 `email.mailbox.test` manifest、handler 及本机管理脚本。它仍是 HIGH 工具，经过原 Gateway 六阶段：tenant、permission、suppression、approval、idempotency、rate_limit；不新增 stage profile，不修改核心管线。Gmail 固定模板事务 MIME 连接器只在 Gateway 已持久化 EXECUTING 后解析发送授权。令牌校验真实 Gmail profile 必须与运行配置的发件地址一致。

测试入口只允许当前在职老板逐次声明并确认的本人收件箱，主题与正文由代码固定；没有任意正文、群发、客户序列或 scheduler 入口。私有授权文件绑定租户、员工、发件人、收件人、批准及到期时间，权限为 0600，45 分钟有效。每份授权只派生一个幂等键，真实 PostgreSQL 工具账本保证一次已确认发送。所有重试必须复用同一授权；不确定发送只搜索对账，不新建授权重发。

仍读取已有联系人/企业抑制事实，并拒绝已停用员工、变更的邮箱绑定、撤销或修改的授权以及 throttled/suspended/retired 发件身份。这个本人诊断用途无需把 auth_pending 标成可商业发送，也不修改 Sending Identity 的认证、预热和信誉状态。它不适用 `notification.email.send` 的四阶段例外。

## 结果与限制

成功仅证明 Gmail 接受一封本人诊断邮件；不保证收件箱投递、不表示 Campaign 可用。QQ 是否进入收件箱需收件人确认；回信由原只读邮箱同步链路接收。邮件测试不能作为 ADR0078 冷开发试验的授权，也不放宽任何客户发送门禁。

CLI 先 `authorize --recipient <本人地址> --confirm-my-mailbox`，返回不含地址的 grant_id；再 `send --grant-id <原ID>`。参数均需带 `--profile <现有运行目录>`。批准文件是独立私有运行数据，源代码及工具账本不保存地址、正文或凭证。
