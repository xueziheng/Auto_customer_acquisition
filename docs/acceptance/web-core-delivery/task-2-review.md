# Task 2 独立审查

- **规格合规：✅ 通过。** 按 controller 的受控组件范围判定；未发现缺失、额外范围或误解。
- **代码质量：Approved。** 无 Critical / Important；一项 Minor 测试覆盖建议不阻断本任务。
- 审查对象：`d6dee3fcd54dae6a050aaa53633ab5374e801fe0..6eb0e14a29890092a0a49f81cad3cfe372c5a75c`；完整 review package 分段读完一次，未重跑 git 或已通过的测试集。

## 已落实的关键要求

- `apps/agent_worker/context_providers.py:50` 用公开员工记录的 `user_id` 唯一映射；经理范围只含自己和活跃直属员工。`agent_runtime/context_builder/service.py:102` 在事实加载前复核身份与策略绑定，拒绝错租户或员工策略。
- `apps/agent_worker/context_adapter.py:99` 校验 descriptor 的 tenant/run/user 和技能集合；`:126` 精确读取锁定版本并复核 prompt 引用。`:30` 的冻结任务策略包装器把 Run 允许集取交集、禁止集取并集，原策略先校验后相交；没有从 `task.inputs` 接收授权、引用或预算。
- `apps/agent_worker/context_providers.py:115` 带合法 Actor/ABAC 调用公开服务并复核返回 ID/owner；`:124` 只投影四个关键字段，要求唯一来源，拒绝 agent_inference 作为事实。`agent_runtime/context_builder/contracts.py:138` 保留安全 Provenance，`:185` 分开事实和有依据的推断；身份名单、价格成本、原件 locator 不进入模型投影。
- `agent_runtime/context_builder/service.py:166` 对所有返回候选先做凭证与隐私扫描，之后才按历史限额和预算移除完整段；`:199` 的预算裁剪保留已单独核算的必需规则。`tests/unit/test_context_builder.py:198` 核对完整 JSON 的 UTF-8 字节预算、边界和完整规则；`:228` 覆盖被裁背景中的凭证仍被拒绝。
- `tests/unit/test_context_builder.py:815` 经过真实 EmployeeServiceImpl / OpportunityServiceImpl 和真实 authorizer，验证两个租户、不同员工以及来源读取前的拒绝；`:971` 的消费者实际读取 BuiltContext 并生成 ChangeSet，验证高风险标记交给原 gate、价格正文被原 guard 拦截。`:754` 与 `:1045` 覆盖跨租户结果容器隔离及同员工不同 Run 的独立工具权限。

## Issues

### Critical

- 无。

### Important

- 无。

### Minor

- `tests/unit/test_agent_worker.py:249` 的取消测试使用 `CancellingContexts` 替身，证明 Worker 的 readiness/signals/外层资源清理，但没有穿过新增 WorkerContextAdapter → ContextBuilderService 的实际 await 链。可补一个受信 descriptor 或事实 provider 挂起后取消的定向用例，断言 CancelledError 原样传播且 consumer/gate 未调用。当前新增适配器只捕获 Exception，而 CancelledError 会继续传播；这是覆盖补强，未发现实际取消缺陷。

## 具名外部风险与核查

- **机会 View 无 tenant 字段，标记请求租户是否掩盖越权：** 核查 `domains/opportunities/service_impl.py:1125` 的 get：角色授权在仓储 IO 前、带 tenant 调用 get、资源 ABAC 在视图构造前；`:473` 的 owner 检查拒绝归属缺失或不在范围；`:1149` 来源读取同样带 tenant。`domains/opportunities/permissions.py:59`、`:104` 验证 SELF actor 与 owner 单例匹配。未将 adapter 重标 tenant 当成独立数据库隔离证据。
- **员工名单读取是否扩大员工权限：** 核查 `domains/employees/service_impl.py:482` 和 `domains/employees/permissions.py:118`、`:137`，名单读取先鉴权，显式 system actor 对应员工只读矩阵；未用它读取组织规则或机会。
- **精确锁版本是否仍会取最新版本：** 核查 `agent_runtime/skill_router/service.py:468`，带 version 时使用完整二元键，并返回深拷贝；不走默认 latest 分支。
- **Worker 的原 guard/gate 与取消清理是否仍有效：** diff 的 main hunk 仅含协议注释，未包含需判断的消费循环，因此仅补读 `apps/agent_worker/main.py:184` 的既有循环及 `:268` 的资源装配尾部。`:226` 构建后消费上下文，`:228` 核对 ChangeSet tenant/run，`:234` 原 guard 在 gate 前，`:241` 传播取消，`:259` 清理 signals/readiness。未重读其他已包含完整 hunk 的改动文件。

## 验证边界

- ⚠️ 101 项聚焦测试、ruff、mypy 与结构检查结果来自实施报告；本审查未重复执行，也未作独立实跑声明。源码未留下需要新增执行才能判断的阻断疑点。
- ⚠️ PostgreSQL 实际隔离、真实 approvals 提交/批准/落库、生产 descriptor/policy/模型消费者均未验证。`docs/superpowers/specs/2026-09-05-context-builder-contract.md:31` 与计划 Task 2 限定说明如实保留 generic Worker disabled；按本批裁定，这些是后续集成门槛，不是 Task 2 缺项。
- 未访问网络、环境变量、凭证、DSN 或数据库；未修复已记录的 AppleDouble `.git` 环境噪声。唯一写入是本审查报告。
