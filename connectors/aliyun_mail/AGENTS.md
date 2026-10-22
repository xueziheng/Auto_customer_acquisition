# connectors/aliyun_mail/ —— 阿里企业邮箱连接器

仅供 Gateway 内受信装配调用，提供账号认证、单封客户邮件发送及只读对账。
复用现有 GmailSendRequest/GmailSendResult 数据契约，不把阿里邮箱标记成 Gmail。
不写业务表、不决定审批、Campaign、可达性或租户权限；遵守根硬边界 1、6、8、9。

固定官方 SMTP `smtp.qiye.aliyun.com:465` 和 IMAP `imap.qiye.aliyun.com:993`，
校验证书及主机名，不提供自定义服务器、非 TLS 或自动重试选项。客户端安全密码仅由
受信装配注入 SecretStr，不进入 repr、错误、日志或返回数据。认证须检查两个协议且不发信。
官方配置：https://help.aliyun.com/zh/document_detail/36576.html
客户端安全密码：https://help.aliyun.com/zh/document_detail/444269.html

发件地址须与绑定地址精确相同。发信前只检索已发送文件夹的 Message-ID 与
X-TradeOS-Idempotency-V1，有限读取候选头并复核两头、From、To、Subject；不读取无关收件箱。
未知或不可选择已发送文件夹必须阻断发送。只有 SMTP 明确接受后才可补写 Sent 副本；
补写失败不得撤销已确认发送事实。SMTP DATA 开始后若结果不确定，返回 reconciliation_required。
恢复只搜索；未命中不证明未发送，禁止自动重发。

SMTP 接受表示服务商已接收邮件，不等于客户已收到或打开。Sent 副本缺失时未知发送须人工核对。
