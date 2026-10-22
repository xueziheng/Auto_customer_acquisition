# ADR 0117：普通邮件持续对话的独立授权与精确关联

日期：2026-10-22。状态：已实施并通过Task5独立门禁；Task6装配与最终分支审查见验收记录；不代表生产发送获批。

## 决策

持续普通问答复用精确获批 Campaign 的独立 ordinary_dialogue_policy，缺省关闭。
首次普通询盘可由真人选择同一精确范围准入，无需冷 Enrollment；已回复冷 Enrollment
保持终态。Task4 的 prepare_and_enroll 与内部 confirmed_refs 不授予对话或披露权限。

普通对话范围只允许当前确认产品的客户视图，显式负责人、处理依据、有效期、有限轮次、
模型调用次数、输入输出上限及无进展上限。服务记录真实确认人/时间，非空范围参与原审批包，
空字段不进入旧 canonical 包。范围改变沿原 Campaign 新版本重新审批，不修改旧批准记录。

实现通过独立 durable binding 连接首次准入或冷发送祖先与 canonical Message、
account/contact/hypothesis/Need；入站只接受精确 SENT 对话头，不模糊匹配、不伪造 Enrollment。
执行按 tenant+source message 唯一，独立 lease/fencing，不冒充人工 ReplyWork handle。
计划存 artifact，账本只元数据；外调前持久调用身份，未知只核对。

所有发送经过新增 HIGH/customer_outbound 插件和原六阶段。模型、凭证、外部连接保持现有
平台与 Gateway 边界。无 Opportunity 的商业问题暂停并进入 owner REVIEW_REPLY；已有机会
复用接管，具体价格/交期/认证等承诺逐次走原审批。分类/需求证据只来自客户原文。

## 影响与验收

修改 Campaign 公共契约与版本持久化，不改九条硬边界。旧包兼容和新范围改版必须回归；
首次准入、新 Message-ID 同 Need、重复/重启/未知/撤权/隔离、真实 scheduler 与协议替身
全链验收通过才可报告 Task5 完成。该决策不构成真实 Campaign 或邮件许可。

## 2026-10-22 发件身份裁定与已实施基础

`ordinary_dialogue_policy.reply_sender_identity_ids` 独立参与精确版本批准，可包含当前合法
`cold_outreach` 或 `primary_business` 身份，拒绝 `transactional`。原冷 Campaign 的
`sender_identity_ids`、冷入组与冷发送校验保持只允许 `cold_outreach`；主业务邮箱回复不能
借用冷发送列表隐式授权。首次询盘须匹配实际收件邮箱、当前连接版本及独立回复身份列表。
普通回复插件使用原 `reserve_send_slot(for_cold_outreach=False)`，不改变冷发送预约语义。

0094–0096 提供 metadata-only execution、精确来源与 Message 映射，并允许 ReplyWork 通过
显式 binding 表达无冷 Enrollment 的首次询盘。来源读取被事件、分类、业务事实、客户证据
及模型 Run 关联共用；同源执行唯一、租约过期外调变 unknown，精确已知结果才能核对 SENT。
原件读取权限仍需与当前客户 owner、邮箱权限取交集；来源映射本身不授予原件访问权。

当前产品模型没有版本字段。普通对话单独通过 Products 公共读端口重核 FORMAL、无候选状态，
只返回客户视图，再比较服务端计算的客户视图摘要；摘要不是确认或披露许可。前端只能选择
产品、邮箱、期限与业务轮数，不要求企业用户填写摘要、模型版本、Run 或 token 参数。

无 Opportunity 的商业接管请求进入原 REVIEW_REPLY，绑定暂停和工作项同事务；单纯规格
缺字段的既有 Need 补全仍可进入普通追问，之后仍受商业内容与当前权限门禁约束。

## 普通对话受托 owner 的权限交集

新增独立普通对话分类/规划入口，仅允许精确已批准 policy 内的当前实际 owner。该 owner
必须同时拥有当前邮箱原件访问权与当前客户业务权限；每轮、原件读取、模型与发送前重读
角色、归属、邮箱连接及批准版本。客户端和模型不得声明 owner 或 approved。
原 boss-only 冷回复 qualify、tenant Inbox 与 BusinessMail.detail 不放宽；老板若不是邮箱
owner，同样不能读取该首询原件。不复制原件，不用 SYSTEM 代替真人原件资格。新增入口不
扩大 manager/sales 的主动搜索或 Campaign 管理权。

### 正式邮箱与全局开关（2026-10-22 补充）

普通对话由已有本人 QQ 邮箱目录与平台 reply_enabled 独立装配，不依赖 qq_reply 私人单目标试验。真实 automatic_send_enabled 在普通 HIGH 工具的当前检查、额度、执行转换和凭证解析前关闭实际发送；收信、历史原件和人工处理仍可用。缺明确冷分类员工的询盘模式不能分类旧冷回复。可选部署 reply_classifier_employee_id 只标识真实受托员工，每次仍执行旧 active boss 规则；不得覆盖不一致的 Gmail/私人 QQ 员工绑定。

正式首封可走现有 API 的本人 QQ connector 与原 email.send；BusinessMail 全 MIME 收取通过同企业/本人邮箱/原发送双方/精确 SENT attempt 到 canonical InboundMessageStored，再走原冷分类与停止序列。新的普通 Message-ID 则精确关联独立 SENT execution 和原 Need。私人 QQ 单目标 reader 不作为正式多轮入口。没有建立第二冷发送流程；自动冷首封 scheduler 对同一正式 QQ connector 的接线由 Task6 完成，不能把 API 首封能力写成已完成全自动首封。

REQUESTS_MATERIALS 在精确独立批准范围中进入受限公开产品段落 planner；无范围保留旧人工移交。首次冷回复须在原分类流程完成后凭真实关联准入，不能通过预置binding绕开顺序。Task6按[ADR0118](0118-agent-upgrade-final-integration.md)补充严格完成门禁与正常耗尽COMPLETED接续；仍不改变Enrollment终态。文件不支持/缺资料仍人工核对；报价、样品与其他具体商业承诺不因此取得授权。

无进展计数只依据 Need 业务值的规范化摘要，新来源/原文/确认人不算需求进展；原 Provenance 完整保留。发送前仍独立重查原件hash、最新客户消息和当前产品视图/许可，不能用语义摘要代替权限或来源验证。确定性 DSN/ARF 只接受唯一原始发送标识、幂等头和原始双方/报告recipient；缺证据留人工核对，不从主题或MAILER-DAEMON字符串推测。

### 失败可处理性与技术反馈

普通模型已开始但结果未知时保留原 UNKNOWN execution、调用身份与预算，不自动重新规划；同一 owner 的 REVIEW_REPLY 显示固定原因。重启后补扫无 attempt 的 UNKNOWN，幂等创建人工工作项。明确无效的计划进入 REVIEW，不伪称模型或邮件已成功。发送未知仍只允许原 attempt 的只读核对；必须等待原 Gateway 租约，不以换 key 或执行行实现重发。

已归档的确定性 DSN/ARF 使用精确 SENT、原始幂等头、发件身份、收件 Contact 和原邮箱连接版本的元数据关联。技术抑制与信誉事实不因 owner 停用而撤销；该路径不读取原件字节、不调用模型、不以 SYSTEM 获取个人邮箱原件权限。客户普通正文路径继续要求当前 owner/客户归属/邮箱交集。报告仍保存不可变原件引用；无法确定关联的材料不得作确定性投诉或退信。

额度成本明确分层：入普通绑定后，分类、需求提取使用的模型轮与规划所有 provider 轮共用独立上限；准入之前的既有冷回复分类沿原平台调用账本。每封普通发送占原 Campaign 每日总量与身份额度，再叠独立普通轮数；未知不退款。UI只显示服务端绑定的业务范围和模型总调用上限。销售员工到达原机会创建权限节点时，需要有权限人员增加一次人工交接。

### 复审修复：模型输入与未桥接入站（2026-10-22）

产品的 FORMAL/公开/hash 校验不代替凭证护栏。planner 改用最小显式产品文字投影，
包括模型尚未选择的名称、类别、规格、描述、变体及产品标识，全部在 generate 前检查；
图片引用和 UI 开关不传模型。输出 render 原护栏保留，不能用模型返回 review 绕过输入检查。

独立业务收件进程可能在一次 bridge 扫描后、模型或计划处理中提交下一封来信。
owner 当前投影新增 `pending_inbound`：只查询当前企业、当前邮箱、原件 hash、双方地址、
精确 SENT 普通发送或冷祖先及尚无桥接收据的完整客户邮件；不使用邮件 Date、主题或
分类结果推断。自动权限在每次原复核点拒绝 pending；人工原件阅读和 bridge 消费仍可进行。
已开始但未派发的旧执行取消，预算/身份保留，不把整条 binding 暂停，后续新消息沿原事件链
继续。SENDING/UNKNOWN 不借取消重发，仍只核对原 attempt。无新增迁移或后台循环。

首次 inquiry 的实际验收从未准入 BusinessMail POST 开始，使用真实销售 owner、原 outbox/
singleton factory cycle、产品说明与同 Need 下一封，不手工入执行队列或预分类；全程无冷
Enrollment。这与已有 cold 首封转换场景分别保存证据。
