# 普通邮件持续沟通运行边界

本能力消费企业已有的获批范围与当前本人邮箱，保持冷开发 Enrollment 的 REPLIED 终态。工程验收使用合成资料、真实 Postgres/Gateway 和 SMTP 协议替身；没有真实客户邮件、真实模型供应商或部署验收。

## 已支持组合

- 企业在原 Campaign 页明确选择普通沟通负责人、已确认可对外使用产品、普通回复邮箱、期限、轮数与依据；保存后仍走原精确版本审批。旧版本无普通 policy 时默认关闭。Task4 客户准备资料确认不等于普通对话或外用授权。
- 已有获批 QQ 首封可从原 API email.send 入口发送。BusinessMail 完整 MIME 收取按同企业、原邮箱、严格 SENT Message-ID 和双方地址进入原冷回复分类，停止冷序列后建立独立普通绑定。新的普通 Message-ID 回信继续同一 Need。
- 原本人业务来信页可显式选择当前已验证 Contact 与既有获批普通 scope，引用逐字客户原话准入；不建冷 Enrollment。主业务身份仅可列在独立普通回复身份范围中，不能用于冷入组。
- 普通发送仅支持现有 QQ 本人连接及真实线程 adapter。其他 provider 当前不提供普通自动回复；不得用无线程的冷发送作替代。私人 QQ 单目标测试 reader 不用于正式多轮收件。

## 部署组合与 Task6 交接

`StandaloneModelSettings.reply_enabled` 加既有 `qq_mail_directory` 装配普通链，`qq_reply` 私人试验保持独立。模型和密钥仍由平台配置，企业不能选择连接或填写技术参数。普通链的 `automatic_send_enabled=False` 阻断规划/发送执行，收取、原分类、人工查看保留；HIGH Gateway 在凭证解析前再查全局开关。

可选 `reply_classifier_employee_id` 只绑定原冷分类的真实、当前在职老板；无此字段且无原 Gmail 分类身份时，正式模式仅支持已准入询盘，不擅自创建冷分类人。若与 Gmail/私人 QQ 分类绑定冲突，配置拒绝。普通 scope 中的销售负责人不冒充该冷分类老板。

Task6 仍须把已有 `EmployeeMailConnectors` 正式 QQ connector 接到原 Campaign scheduler 冷首封组合。当前 Task5 的同 provider 证据从正式获批 API 首封开始，不能据此宣称自动冷首封已完成。可复用公开 `AliyunOutboundConnectors` / EmployeeMailConnectors 协议及既有邮箱存储组合，但 scheduler technical tool_user 不是本人邮箱 owner；必须从当前 attempt、所选身份与当前本人连接求交，不得借用 cold classifier 身份解析凭证。保持原六阶段与全局停发。

实际普通调度入口是 `SchedulerRuntimeFactory` / `CanonicalSchedulerBootstrap.reply_factory`；无旧冷 inbound 时用 `DialogueArtifactPorts` 提供原件端口，原事件处理器仍注册。`run_scheduler_worker` 在取得单副本锁后扫描，前后核对锁连接；没有第二个后台任务。

## 暂停、额度与人工处理

普通内容只引用当前获准英文产品段落，最多问两个需求缺项。价格、交期、库存、认证、付款、合同、保证和样品承诺交原人工/逐次审批。无 Opportunity 时暂停到 owner REVIEW_REPLY；sales 已完成 Need 验证但无原 intake 权限时，等待有权限人员建立或接管机会。

模型结果未知保留调用身份与已用额度，显示人工工作项；无效计划不发信。发送未知保留原 attempt 与额度，只核对已发送记录，原 Gateway 租约到期前不抢占。重复收件、工作项、重启不得重发。新来信、转交、撤权、资料变化、scope 撤回、退订或投诉使旧计划失效。无进展按实际业务字段值计数，换邮件原文或 Provenance 不算进展。

独立模型上限覆盖绑定后的分类/提取/规划 provider 轮，失败和未知不退款；准入前原冷分类仍计入原平台账本。普通邮件同时消耗原 Campaign 每日总发送量、身份额度及普通轮数。空队列没有模型 I/O。

确定性 DSN/ARF 使用新 SENT 来源关联真实 Contact、身份和 binding；owner 停用不撤销已归档确定性技术反馈。缺少精确发送标识/recipient 的材料只保留人工核对，不能由主题或 MAILER-DAEMON 字样推断成功、退信或投诉。

迁移为0093至0101，包含来源、执行/CAS、预算、不可变计划种类、日额度预占及业务原件收据；启动不自动迁移。先按部署流程迁移并备份，再启用获批范围。工程报告记录对应 roundtrip 与隔离证据。
