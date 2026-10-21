# ADR 0098：QQ 私有回信测试复用原消息与负责人流程

- 日期：2026-10-15
- 状态：隔离实现与受控验收；未部署、未启用真实收发
- 授权来源：用户在协调聊天“自动获客”明确授权隔离修改和 fixture 测试，限定两个本人控制邮箱。部署和真实邮件验收仍需另外授权。

## 流程与边界

原单封手动发送 Attempt → QQ 精确回复头 → 原 email.inbound.fetch Gateway →
RawArtifact/整页事务/InboundMessageStored Outbox → 原 reply_qualification →
model.generate Gateway → 原 conversation_reply_work → 原消息页当前负责人处理。
正向分类经原通知投影、durable jobs、notification worker、站内通道通知当前老板。
明确购买兴趣与愿意通过 WhatsApp 交流保留不同分类；没有 WhatsApp 外部接口或自动联系。

QQ 连接器固定 imap.qq.com:993/TLS，EXAMINE 和 BODY.PEEK。正文前校验 Message-ID、
唯一 In-Reply-To、From/To、无抄送、租户、精确发件身份、SENT Attempt及provider receipt、
私有测试用途、当前员工/用户/客户归属与本人邮箱绑定。缺失、歧义、无关、未发送、未知发送拒绝。
正文读取后重验头、当前授权及 UIDVALIDITY，失败不交付归档或模型。qic1游标绑定完整route、
UIDVALIDITY、last UID与固定窗口；旧游标可重放，持久游标前进后不重复读正文。
新的机械 provider seam 只参数化 reader/initial/validate，不改 Gateway 阶段、原件或事务语义。

## 用户明确授权的窄测试例外（全局硬边界6）

普通业务“未验证联系人不得进入发送序列”的规则不变。本次独立用途只允许
1249583590@qq.com → 986647123@qq.com；收件人为本人控制，UI明确确认同意测试。
新增 private_reply_tests 仅绑定现有 account/contact/sender/owner，不新增会话或待办状态机。
联系人保持 unverified、verified_at为空，无公开公司证据、需求假设或正式商机。
此标记不对客户端开放直接写入；只在显式QQ配置的本人入口生成。
0077表启用并强制RLS，复用固定租户角色的permissive/restrictive双策略及既有角色授权。
验收覆盖新库upgrade→downgrade→upgrade、真实受限角色跨租户读取/更新拒绝和已使用用途拒绝降级。

原 Outreach 域只允许受信权限快照对应的当前owner、manual、无hypothesis、
精确单一sender和单步已批准Campaign。允许技术发送actor仍须经过原精确资源权限、
当前联系人、归属、抑制、审批、发件身份、额度与Gateway重新检查。
不允许普通unverified联系人、自动模式、多步、多sender、其他owner/企业/地址复用此例外。
稳定入组键限制同一个私有测试对象只能复用同一单封流程，不通过换键新增邮件。
发送未知保持原账本核对语义，不自动重发。此例外不应推广为未验证业务客户的触达机制。

私有测试的正向动作只写原START_QUALIFICATION负责人工作项，不创建Need、Hypothesis、
Opportunity或Handoff。退订/投诉/退信继续原安全动作。正式业务的Handoff语义保持。

## 收回复与自动发送分别启用

automatic_send_enabled默认false。QQ配置要求reply_enabled=true且automatic_send_enabled=false。
回复分类员工单独绑定当前真实boss；邮箱owner继续原sales角色，不提升qikai权限。
reply-only runtime构造原Outreach只读事实依赖，不注册campaign发送handlers、流程或driver。
历史Gmail自动模式需要显式开关和完整组合；升级不能因为已有旧发送队列就自动恢复发送。

## 负责人界面、通知与授权

原消息页面显示pending/in_progress工作项，服务先以当前ownership过滤再LIMIT；
处理通过当前员工与owner锁、expected_status CAS，重复目标状态幂等，转派撤权即时拒绝。
私有测试准备入口置于原撰写客户邮件页。换身份、撤权或迟到响应不能恢复旧资料。
可选“下一问”拒绝时重查原消息权限，保留仍获授权的处理入口；复查失败清空资料。

ReplyReceived的conversation_id从同事务原消息取得，不相信调用者自报编号。
老板通知只含安全消息/会话编号与分类，LOW仅站内，原job/dedup去重。
旧正向事件若已人工纠正不再生成正向通知。负责人处理不要求建立正式商机。
模型真实供应商、真实QQ邮箱与公网费用均未在本次测试中调用。


## 2026-10-20：已批准的新私有单封轮次

用户在服务器验收接手聊天中明确批准两个原测试邮箱之间新增一封邮件，用于购买意向与首次自动处理验收。
原稳定键已按已批准 Campaign 区分，但账户级历史回复门禁仍阻止新轮次。

显式私有测试部署现可为新的已批准单步 manual Campaign 读取有界原发送关联事实。
只有当前私有用途、本人同意、当前 owner、唯一 sender、已批准版本全部有效时，
领域规则才可忽略精确关联其他私有 SENT Attempt 且已完成分类的旧轮次回信。
不按邮件日期推断轮次；未关联、关联歧义、非私有/非SENT来源、未知分类、needs_review、
退订/投诉/退信/拒绝，以及本轮非自动回复仍阻止发送。原账户回信、分类、工作项与通知保留不变。

新门禁在入组、准备草稿、Gateway 发送前的原事实核验均生效；
普通业务、自动序列、未装配私有读取器的部署均保持原账户级回复规则。
抑制、权限、精确审批、单封额度、SENT幂等和首次自动回复验收标准不变。
不迁移数据库，不部署正式生产实例，不改变后台收信持续运行能力。
