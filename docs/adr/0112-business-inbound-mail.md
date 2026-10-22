# ADR 0112：普通业务来信先可见，再独立归类

- 日期：2026-10-21
- 状态：已实现，待隔离部署和真实邮件验收
- 来源：用户明确批准已连接QQ邮箱正常接收陌生首信；先在TradeOS可见，再使用现有Codex CLI与DeepSeek归类。

## 决策与边界

新增普通业务来信契约与四张持久表，独立于私人回信测试grant和本人Gmail镜像。只接收当前active owner明确启用、精确绑定当前邮箱连接operation_id的QQ INBOX。未知发件人恒为待确认联系人，不创建正式客户、需求、贸易机会，不自动发信。

邮件事实与分类推断分别保存：原始字节经新的`email.business.fetch`只读Gateway插件归档、实际有界读回核验后，受信worker在整页事务写入消息与UID收据并推进游标。每个消息有稳定run_id；UIDVALIDITY+UID、实际来源字节摘要、含Message-ID的确定性内容摘要去重。每次传输收据保留自己的实际artifact引用。接收时间来自IMAP INTERNALDATE，发件人Date不作为接收事实。

超过完整MIME安全上限的邮件仅抓取有界邮件头，保存`headers_only`和`oversize`待确认记录，再处理后续UID。`source_sha256`是实际归档字节摘要；只有完整MIME才填写`mime_sha256`。不能把部分邮件头冒充完整原件。正文仅纯文本，附件不进入模型。

同一邮箱使用Postgres session advisory lock；每页抓取前后检查锁与当前绑定。每轮固定首个UIDNEXT快照尾，先保存该有界历史，再按真实接收时间从新到旧归类。模型前消息已提交，失败、额度不足和进程重启不会让邮件消失或要求用户重发。

模型能力名为`business_mail_triage`，持久身份来自消息run与当前owner；不冒用`reply_qualification`或私人grant。仍由原`model.generate`、配置服务、公司模型目录、Codex隔离runner、原计量账本和原限额完成调用，不初始化、更改或重置额度。runner使用现有受限分类profile，只开放读取本任务上下文，禁用shell、浏览、任意工具和额外网络。只提交必要主题与正文，先过凭证标记护栏。模型只返回固定归类、理由与可逐字定位的原文引句，不返回概率、价格或权限字段。

worker在claim前只读共享模型账本，避免已知并发拥堵或已耗尽窗口把其他邮件变成失败。最终原子额度判断仍由原Gateway执行。窗口次数耗尽才持久暂停到原窗口末尾；并发槽满仅暂缓。已经派发或执行结果未知的消息不自动重放。

读、纠正、模型前后均重核当前租户、active员工、精确owner与连接操作版本。老板没有跨邮箱特权。人员失活或邮箱连接撤销、轮换立即关闭访问和模型交付。人工纠正使用expected_revision CAS并追加历史；模型前已纠正则停止派发；模型在途完成时保留原判历史，但人工当前归类优先。

## API与进程

`GET /inbox/business-mail`、`GET /inbox/business-mail/{id}`与`POST /inbox/business-mail/{id}/correct-classification`只消费会话身份。纠正请求只有expected_revision、category、note。所有详情为纯文本并禁止缓存。

新增`python -m apps.email_feedback_worker.business_mail --profile ... --model ... --scope ...`独立模式，不在API启动后台线程、不改变原feedback、镜像和私人回复worker。scope必须显式enabled、当前owner与user、地址、当前连接operation_id、私有邮箱目录和有界抓取参数；不含密码和模型密钥。数据库须预先迁移到0085，不在启动时自动升级。

## 数据与回退

0085创建四张带tenant_id、强制RLS的表，并向已有受限租户数据库角色授权。DDL冻结，不从运行时ORM生成历史迁移。原始消息事实、UID收据和分类历史有数据库不可变约束；分类状态更新不会重写原始来源。空库验证upgrade→downgrade→upgrade；有业务来信或历史时拒绝有损downgrade。

## 已知成本与未验证项

初始回填是显式有限UID窗口，不能声称扫描完整邮箱。凭证标记护栏保守拦截会使部分正常邮件需要人工确认。原模型配置或规范API/scheduler心跳不可用时普通收件仍能持久可见，但分类关闭。真实QQ、Codex和DeepSeek调用及sales首信UI验收须在隔离部署后完成，不能用受控Provider测试代替业务验收。

## 审查修复：预览与最终原子准入之间的额度竞态

预览ready不能证明随后仍有额度。若最终Gateway在rate_limit阶段明确拒绝，当前精确身份的
canonical为rate_limited/failed_transient且有对应失败事件、无provider_ref，并且该消息整个
run不存在任何ModelInvocationRow，worker恢复pending；窗口次数耗尽仍等待原窗口，并发满
仅暂缓。不为该零派发拒绝写入终态分类历史。

消息run_id保持稳定。每次新的准入序号由同run完整canonical拒绝链确定性推导，逐项核对
当前tenant/owner/user、business_mail_triage/config身份键、原事件、状态与尝试次数。原模型
与配置仍锁定，不能通过后续准入换模型。新序号形成新的原Gateway幂等键，旧canonical和事件
保持只读，旧序号不再通过当前授权。只要存在任何reserved、dispatched、unknown、rejected
或其他模型计量记录，即禁止此恢复分支；它不构成模型调用重试、退款或账本重置。
