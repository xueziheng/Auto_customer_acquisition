# ADR 0084：网页 Gmail 连接与本人邮箱诊断

状态：接受。用户要求通过云端 TradeOS 的 jslt 网页连接本人 Gmail，并向本人指定 QQ 邮箱发送测试邮件。

## 问题
既有全邮箱镜像和固定邮件诊断只有本机 CLI 配置；网页只提供邮箱列表和企业发件域名管理，登记地址不等于 Google 授权。

## 决策
- 为独立服务器增加显式 gmail_web_settings_file，包含 Web OAuth 客户端路径、私有状态目录和唯一 HTTPS 网站来源。保留原本机授权模式，不能共享全局 profile.gmail 覆盖员工绑定。
- 企业、员工、用户与原登录会话共同绑定随机 state、PKCE 和十分钟期限。持久原子 claim 防止回放；新授权替代旧待办。只接受 Google 官方客户端、端点、精确注册回调及完整 read+send scope，核对实际 Gmail profile 后才保存绑定。
- 回调只落到同源静态页面，立即清除 URL 查询，再使用原会话、CSRF 进入 API。API 不新增匿名许可，不降低 Cookie/来源验证。代理不记录回调查询，响应 no-store/no-referrer。
- 新工具 email.gmail.connect 是受限 MEDIUM 授权交换，不发客户内容、不改 Sending Identity。经 tenant、permission、idempotency、rate_limit 记录已领取的一次授权；未知交换不可自动重试；每员工用户十分钟最多十个授权请求，私有持久预算经 Gateway 重新核验。
- 本人镜像依旧经 email.mailbox.fetch、租户数据库角色、员工与用户映射检查和 mailbox advisory lock。独立 web_mailbox worker 发现显式配置的本租户连接，暂停失败绑定并遵守退避。原只读客户端仍只接受 readonly scope；网页专用客户端显式接受 readonly+send，但镜像端口仅 GET。
- 网页诊断复用 ADR0079 的 HIGH 六阶段和固定英文模板。仅在职企业管理员、已验证本人 Gmail、允许状态的已有发件身份、未抑制的本人收件地址。网页逐次确认固定收件地址与内容；持久 request_id、授权和 Gateway 幂等键一致，未知结果不换键重发；每员工每天最多五个诊断授权。诊断成功不代表营销认证通过。
- 连接凭证与授权码不进入模型、审计、资料库、Git 或日志。权限使用当前事实重查；连接metadata虽在私有文件中仍明确包含 tenant_id、employee_id、user_id。

## 代价与边界
Google 测试项目及测试用户限制仍存在；公开推广前另行完成 Google 所要求的应用审核。
本切片为本人 Gmail 同步与固定邮件诊断，不新增任意正文发送、Campaign 旁路或读取其他员工邮箱。
