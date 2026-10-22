# ADR 0103：固定 QQ 回信试验的证据、起点与持久门禁

状态：隔离离线实现；未部署、未启用、未做真实邮箱或模型验收。2026-10-16。

## 背景与授权边界

生产参考 release 为 `20261016-reply-review-e2dcc968` / schema `0078`。既有人工确认恢复不能证明原 QQ 自动入站入口已成功。QQ 可能改写 Message-ID，客户端 APPEND 的发件副本和本地 provider_ref 都不能单独证明实际编号。

本轮只获准三项离线最小改进。不得部署、接入真实邮箱、刷新 OAuth、调用真实模型、发信、重放 Q01/Q02、重置历史游标或改动原 Gateway 核心/Workflow 引擎。不能把会话中的同意伪写成 jslt 的页面审批。

## 决定

1. 新增 `shared.schemas.qq_trial` 明确单次试验契约。固定租户、路由、邮箱身份、owner、分类员工/用户、模型版本、双方邮箱和唯一 SENT attempt。`authorization_source=explicit_user_request` 和引用只记录实际授权来源，不能自授生产启用权。
2. 别名证据必须同时满足：唯一非 canonical 的 Message-ID、原 attempt 的精确 X-TradeOS-Idempotency-V1、双方地址、原件哈希/长度、同租户成功 Gateway 调用及其 artifact 引用。排除客户端 canonical APPEND 副本；缺失、多值、歧义、错租户、错 attempt 一律拒绝。原 MIME 不改写，仅向原关联流程投影获准 canonical 编号。
3. **别名来源尚未解决。** `email.qq.sent_alias.read` 是证据契约要求的具名工具，本补丁没有注册该工具或提供采集器。测试用合成成功账本和固定头样本，不能冒充真实 QQ 证据。在原 Connector/Gateway 内取得可靠来源之前，保持启用不可用，不搜索整个邮箱，不从主题/正文推断，也不扩建另一套收信平台。
4. 新试验不能使用 `qq-project-reply` 或已存在的原生游标。首次只读快照记录 UIDVALIDITY 和 UIDNEXT−1，不 SEARCH、不读历史正文；计入会话额度。后续只查高水位之后的 UID。UIDVALIDITY 变化、回退、未完成扫描、配置/归属改变均停止。`after_epoch` 不作为 QQ 过滤能力。
5. 0079 新增三张技术表：`qq_sent_aliases`、`qq_reply_trials`、`qq_reply_trial_events`。预算预留先独立提交，外部 IO 后不退款；原页事务统一提交 Message/receipt/cursor 与试验 message 绑定。证据/事件 append-only，scope/初始水位固定，计数只能增加，同 attempt 不能另建预算。所有查询显式 tenant，表强制 RLS。
6. 上限为 600 秒、间隔至少 30 秒、20 会话、20 SEARCH、20 候选头、1 正文（256 KiB）、1 外层分类、4 底层请求、累计模型输入 32 KiB、累计输出 512 token、模型总超时 180 秒。原全局更严格限制优先。纯文本或 HTML 单体 MIME 才准入；附件和 Bcc 拒绝。multipart 暂因读取前缺少子部分结构证据而拒绝（mime_structure_unverified），不把无附件的 multipart/alternative 错称为附件。
7. 原 `AgentTransport` 在实际协议参数完成转换后、SDK 请求前计量，兼容 Responses 和 Chat 格式。原 broker 的每一轮都独立预留；模型结果未知或缺失用量时停止。包装器自己构造原 model.generate Gateway，并在调用 Provider 前验证同一个具名 AgentProvider 逐轮门禁，拒绝任意未接线端口。不增加重试或模型切换。
8. 拉取、正文、归档、分类和业务提交前均重核当前范围。`QQTrialCurrentAccess` 复用原邮箱发送事实和原模型配置公开授权；最终模型调用仍由原 ReplyModelAuthority 和 Gateway 配额判权。原 UoW 的可选提交回调保留原事务。关闭新门禁时维持旧行为。

## 兼容、范围和剩余事项

没有修改原 14 类、needs_review、原 owner 工作项、老板六种正向通知、纠正机制、页面或发送能力。没有新增进程、定时器、全局启用设置、部署脚本或真实试验命令。提供的是门禁与显式组合接口，尚未把它挂入生产启动入口；实际自动收信仍是未验收状态。

预算保护适用于受信装配；不是对拥有数据库超级用户或任意 Python 修改权的操作者做沙箱承诺。外部系统在请求已经开始后的执行不可撤销；取消/未知结果保留消费，不自动补调用。原对象归档可能先于页提交完成，失败后可以留有原件，但不得伪造业务消息已提交。

升级只建空技术表、约束和触发器，无历史回填。空表支持 0078→0079→0078→0079；任一证据存在则拒绝 downgrade。回退应用时保留三表和已消费预算，不能删数据后强行降级。`row_security=off` 只让无绕过权限的降级查询报错，防止把被 RLS 过滤的表误当空表。

离线测试使用外网隔离 namespace、Unix socket PostgreSQL 与固定 fake 邮箱/SDK/对象存储。数据库行锁、原 Gateway、事务、模型额度、分类和工作项代码保持真实。离线通过不能证明真实 QQ 编号改写、SEARCH 兼容性、客户端 MIME 形式或真实模型输出效果。后续须先解决证据来源与受信生产组合，再另行批准一个全新 attempt 的真实单封验收。

第二版只补齐原真实 IMAP transport 的受控 alias 查询、MIME/Bcc 头字段及准确的持久停止原因，并增加按真实 HEADER.FIELDS 裁剪的 socketpair 测试。可靠编号证据采集和生产启动受信装配仍未完成；APPEND/SMTP/UIDPLUS 不能提供投递编号保证。完整环境 36 项回归仍受未运行 Docker 与外网隔离拓扑阻断。具体来源论证、multipart 最小方案和待单独授权的最小证据实验见 [离线运维说明](../operations/qq-bounded-reply-offline.md)。本补丁仍仅具备部分实现审阅条件，不具备部署验收条件。


## v4 离线补充（2026-10-16）

仅在原 connector 的 trial 头请求加入有界 BODYSTRUCTURE。允许已验证结构的根 alternative 恰含纯文本与 HTML 两个叶子；其余未准入结构继续拒绝。读取后核对原 MIME 分段、编码、字节和行数，不改变原次数/总字节/授权门禁。精确关联但头无法安全解释的候选持久停止，不静默推进。实际地址容错因缺原始语法证据仍未实现。详见 `../operations/qq-v4-mime-admission.md`。本 ADR 补充不授权真实采集、生产迁移或上线。
