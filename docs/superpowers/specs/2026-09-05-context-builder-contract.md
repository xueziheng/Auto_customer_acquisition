# 权限约束上下文与 Worker 适配契约

日期：2026-09-05。Web 核心收口 Task 2；本契约先于实现与 RED 测试固定。

## 身份与只读事实

保留 `ContextBuilder.build(tenant_id, acting_user, task_objective, entity_refs, skill_tool_requirements, token_budget)` 六参数和 `BuiltContext`。新增严格、冻结、拒绝未知字段的 Pydantic 内部 DTO；只经三个 typed provider 解析身份、读取策略和加载事实。DTO 不是模型可反序列化的授权书。

`ContextIdentityReader.resolve` 通过 `EmployeeService.list_active` 公共 DTO 唯一匹配 `user_id`，不靠姓名或同串 EmployeeId。显式注入且绑定租户的只读 system actor 仅用于员工解析；其身份名单不进模型。停用、重复、未知角色、跨租户拒绝。sales 只自身；manager 只自身和活跃直属员工；boss 明确租户范围，但本批仍限有明确活跃 owner 的机会。国家/品类范围标为 unavailable，不授予相应范围的搜索能力。

`ContextFactReader.load(identity, refs, *, history_limit)` 仅接收精确 opportunity 引用。apps 的真实窄 adapter 用当前员工 actor 与显式 owner ABAC 调用 `OpportunityService.get`，绝不导入 API 或域 repository/model。公共 OpportunityView 没有 tenant 字段，adapter 依赖服务的 tenant-filtered get，复核返回 ID/owner 后绑定请求租户；runtime 再核返回段 tenant/owner/ref。只投影有完整来源的 quantity/spec_summary/destination/required_by 字段；缺来源、重复来源或把推断当事实拒绝。不暴露价格、成本、邮箱、文件路径、对象 locator。公共来源摘要仅保留来源类别、逻辑 ID、提取者/时间、确认人/时间；来源 URL/页面 hash 不进入模型。其他事实 provider 必须返回相同的窄 DTO；事实和推断分字段，推断必须指向该次加载的事实来源。

## 策略和 Worker

`ContextPolicyReader.read(identity)` 必须返回 tenant/employee/version 绑定的全部规则：显式 Playbook 版本、完整审批动作集、排除项（允许明确空集合）、必需规则，以及 user/run 工具允许集和所有显式禁用。未配置拒绝，不假装默认常量来自 Playbook。Gateway 始终独立判权；`data_scope` 仅是最小安全审计投影。

`WorkerContextAdapter.build(task)` 通过受信 descriptor reader，以 task tenant/run/user 读取不可变的选择快照，内含来源引用、该 Run 显式工具允许/禁用集、精确锁定 skill_id/version/prompt_ref/prompt 文本和精确 entity refs。descriptor reader 对锁定 prompt 内容的正确性负责（本批不实现 durable producer 或文件 prompt reader）。adapter 用 FileSkillRouter 的公共 get 精确查询该版本、核 skill_ids 与 prompt_ref；版本未锁、空或未知技能、绑定不符拒绝。每任务建立新 frozen policy wrapper 和 builder，把 Run/技能禁用并入该次 policy，把 Run 允许集与 provider 的运行允许基线取交集，把 prompt 与 evidence_required 作为必需技能约束。不得从 task.inputs 取得任何授权、实体选择、版本或预算，不改变共享 builder 的权限。

工具结果稳定去重排序：`(skill_allowed ∩ user_allowed ∩ run_allowed ∩ registered_tools) - explicit_blocks`；blocked 包含显式禁止和所有请求但未授权工具。所有允许/禁用集拒绝 wildcard；未知工具没有 registry 授权，不启用。

## 输入和预算

constructor 显式配置最大预算、历史条数和历史字节数；预算严格正整数，bool/浮点/字符串/超上限拒绝。全部候选字符串（含未来丢弃的历史/技能/规则/来源引用）在裁剪前经过 CredentialMarkerGuard 与窄隐私拒绝检查；不以先截断或脱敏掩盖凭证。隐私采用拒绝邮箱、URL/URI、路径、对象 locator 的保守文本投影，原始字段白名单保证成本不进入候选。错误只固定中文，不回显内容或异常链。

必需 sections 为任务目标、完整规则、技能约束；工具黑白名单和审计范围也属必需。完整模型可见 JSON 采用 ensure_ascii=False、sort_keys=True、紧凑 separators 的 UTF-8 字节数，写入兼容字段 token_estimate；这是保守预算单位，不是真实 token usage。调用层单独承担模型消息封装和输出预算。估算不含审计用截断日志/估算自身。必需内容超预算直接拒绝。可选当前实体、历史证据、供应背景按稳定 ID/优先级排序，从尾部整段移除；来源与事实不可拆。历史数量/字节限额同样先扫描全部候选再整段裁剪并留固定原因与安全 section ID。

## 验收与当前能力状态

先 RED 再 GREEN：两租户、两员工、manager 直属范围；身份伪造/停用/重复；真实服务授权前置与返回 owner/ref 复核；工具交集/禁用/未知/wildcard；规则缺失；预算边界及中文 UTF-8；凭证/隐私在所有候选与被裁背景中拒绝；锁版本与并发隔离；受控 runner 真正读取 BuiltContext 并产出 ChangeSet。复用 worker 原护栏/gate，检验失败/取消/资源清理和错误脱敏。受控 gate 只证明分流调用，不等同真实审批落库或批准执行。

通用 Agent Worker 继续 disabled：没有 durable AgentJob producer、完整生产 policy/descriptor provider 或生产 BuiltContext 模型消费者。scheduler 原有窄回复模型与模板发送不改写。Task 5/6 负责其正文投影缺口；真实审批/模型/数据库/外部调用验收本批 not_run。
