# Task 2 实施报告

状态：DONE_WITH_CONCERNS（已完成本批受控组件范围；生产缺口见下，等待独立审查）。

- 工作树：`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion`
- 分支：`codex/web-core-completion`
- Base：`d6dee3fcd54dae6a050aaa53633ab5374e801fe0`
- Head / 本任务唯一提交：`6eb0e14a29890092a0a49f81cad3cfe372c5a75c`
- 提交：`feat(context): 实现受信员工与运行权限约束的上下文`
- 子规格：`docs/superpowers/specs/2026-09-05-context-builder-contract.md`
- 只修改 Task 2 的源代码、测试、子规格及主计划 Task 2 checklist；未改 ledger/其他批次 scratch。报告本身写在指定 scratch 路径。

## 需求裁定与落实

1. 保留原六参数 `ContextBuilder.build` 与既有 BuiltContext；WorkerContextAdapter 实现原 worker 的 build(task) 结构型接口。main 仅更新窄装配契约注释，不启用默认 runtime，不增加队列或改九个 Agent。
2. runtime 内部三个 provider：ContextIdentityReader / ContextPolicyReader / ContextFactReader。所有内部 DTO 使用严格 frozen Pydantic、tuple、extra=forbid、hide_input_in_errors、revalidate_instances=always；事实与推断分字段、来源与确认时间成对，未知实体/字段拒绝。
3. EmployeeContextIdentityReader 显式绑定 tenant 和注入的 system/SYSTEM 员工只读 actor，经 EmployeeService.list_active 公共 View 按 user_id 唯一解析。无 EmployeeId 同串回退；名字无授权意义；停用/重复/未知角色/错租户拒绝。sales 只自己；manager 只自己和活跃直属；boss 也仅枚举的明确活跃 owners。完整名单永不进模型。
4. OpportunityContextFactReader 使用合法员工 Actor + OpportunityScope 调用公开 OpportunityService.get 精确读取，再复核 ID/owner。只投影有唯一对应 Provenance 的 quantity/spec_summary/destination/required_by；裸 summary、缺失/重复来源、agent_inference 伪事实拒绝，网页来源还必须原始 URL/hash 都存在。价格、成本、企业名称、地址头、原件 URL/locator 不投影。
5. **租户信任边界**：OpportunityView 本身没有 tenant_id，不能声称 View 提供了租户证据。tenant 正确性来自显式 tenant-bound 公开 get 的过滤契约；adapter 在验证 ID/owner 后以请求租户标记 section，builder 再检查 provider section 的 tenant/owner/ref。真实服务在 repo IO 前执行角色判权，实体 owner ABAC 在读到行之后、构造 View/读取来源之前执行；本测试没有声称数据库 get 自身带 owner SQL 条件。
6. Playbook 员工规则读取仍未实现。本批只接受受信 typed policy provider 的完整 rules、policy_ref/playbook_ref、16 项全局逐次审批动作、显式排除集和必需 directives；空白规则、缺项/未配置拒绝。不存在伪造 boss/system 读取组织规则或默认商业阈值。
7. descriptor reader 以 task tenant/run/user 取锁定快照，含来源引用、精确 skill_id/version/prompt_ref/prompt、entity refs，以及本 Run 必填工具允许/禁用集。adapter 精确调用 router.get(id, version)，核 task.skill_ids、版本、prompt_ref，一次任务建独立 frozen policy wrapper/builder。可信 reader 对 prompt 快照与获批来源一致性负责，未实现生产文件 prompt loader。
8. 自审补齐同员工不同 Run 的授权限制：descriptor 的 run allow 与 policy provider 的运行基线取交集，Run/skill blocks 并入 policy。所有集合在相交前先校验，避免 wildcard 被相交消掉而静默容忍。最终 `(skill ∩ user ∩ run ∩ registered) - explicit_blocks`；blocked 含全部禁止与请求未获权项，稳定排序去重，未知工具不启用。task.inputs 的权限/实体/预算字段全部忽略。
9. 所有模型候选字符串（规则、prompt、事实、来源、目标及会被裁掉的背景）先 CredentialMarkerGuard、再窄隐私拒绝，随后裁剪。拒绝邮箱、URL/URI、绝对或相对 locator、明显内部成本标记；来源 ID 和审计 section ID 用有界安全引用。该文本规则保守，可能拒绝普通 token/cookie 讨论或带斜线的合法规格，不是通用语义 PII 分类器。
10. constructor 显式预算最大值与历史条数/字节限额；预算严格正整数拒 bool/float/字符串/负数/零/超上限。估算是完整模型可见 JSON 的 canonical UTF-8 字节数（ensure_ascii=False、sort_keys=True、紧凑 separators），字段名继续叫 token_estimate，但不是实测 token usage。任务、规则、技能约束、工具列表、data_scope 必需且不截断；超预算拒绝。历史/供应/实体背景按稳定优先级整段移除，Provenance 不拆，日志只记安全 section ID + 固定原因。模型消息封装及输出预算归调用层。
11. data_scope 仅安全审计；与 provider identity 不共享。测试修改模型 scope 后，真实 PermissionCheck 仍以独立授权 callback 拒绝工具；不将该 dict 转回 actor。BuiltContext 保持历史 list/dict 类型，每次重新构造，跨任务无共享结果容器。
12. 原 worker guard/gate/identity check/lifecycle 复用。受控 Consumer 实际读取 BuiltContext facts，并把读取的规格写入 ChangeSet summary；高风险变更传递到既有 gate 端口；不安全价格正文仍由原 guard 改成结构化拒绝。失败不触发 runner/gate/complete，取消传播并清理 readiness/signals/runtime 资源，tenant/run 不匹配在 gate 前拒绝。

## RED / GREEN 实录

采用 test-driven-development 技能；先写上述子规格，再新增测试，随后实现。

- 初始 RED：`.venv/bin/python -m pytest tests/unit/test_context_builder.py -q` → **exit 2，1 collection error**：`ModuleNotFoundError: agent_runtime.context_builder.contracts`，组件不存在，符合预期。
- 首轮实现验证：三文件聚焦测试 → **2 failed, 76 passed**。两项都是夹具技能 ID `fixture.review` 与 manifest domain `qualification` 不一致；按真实 FileSkillRouter 契约改为 `qualification.review`，未改 router 行为。随后 **78 passed**。
- 自审 RED：新增相对 locator/成本文本、空白必需规则、缺 URL/hash 的网页来源 → **5 failed, 88 passed**，五项全部为预期 `DID NOT RAISE`。补投影校验和必需文本/网页来源校验后 → **93 passed**。
- Run 绑定 RED：`-k same_user_concurrent_runs` → **1 failed, 54 deselected**，descriptor 尚不接受必填 run allow/block。新增受信 Run 约束并逐任务相交后 → **1 passed, 54 deselected**。
- 原始策略通配校验 RED：`-k policy_wildcard_cannot` → **1 failed, 60 deselected**，`DID NOT RAISE`。把 provider 原始配置检查移到 Run 交集之前，后续聚焦 GREEN 验证通过。
- 精确 SemVer 元数据 RED：`-k locked_semver_build_metadata` → **1 failed, 61 deselected**，合法 `1.0.0+approved` 在本地安全引用正则被误拒。版本已由 router.get 精确校验，因此版本只做模型候选安全扫描，保留 `+metadata` 合法形状；最终 GREEN 包含此例。

初轮检查另发现 3 个局部 mypy 类型问题（Literal 构造、局部循环变量重复推导、dict key 类型）和 ruff 的格式/字典写法问题，均在本批文件内修正。结构检查首次拒绝新增测试直接导入域 models；改为复用既有员工/机会单测的造数 helper，生产 adapter 一直只依赖公开 service/permissions。未修改结构脚本或放宽任何全局规则。

## 最终聚焦证据（对应本提交代码）

```text
.venv/bin/python -m pytest tests/unit/test_context_builder.py tests/unit/test_agent_worker.py tests/unit/test_guardrail_checker.py -q
101 passed in 0.36s

.venv/bin/python -m ruff check agent_runtime/context_builder apps/agent_worker tests/unit/test_context_builder.py tests/unit/test_agent_worker.py
All checks passed!

.venv/bin/python -m mypy agent_runtime/context_builder apps/agent_worker/context_adapter.py apps/agent_worker/context_providers.py apps/agent_worker/main.py --follow-imports=silent
Success: no issues found in 8 source files

.venv/bin/python scripts/check_boundaries.py
分层与依赖方向 / 金额 float / 置信度数值 / 事件注册 / 租户过滤 / AGENTS.md 覆盖 / 域结构完整：全部通过。

git diff --cached --check
exit 0，无 whitespace finding；有已知 AppleDouble stderr（下述）。
```

测试组合不是全库测试，不含 DB/integration/evals/真实模型或外部调用。模型/prompt 资产没有改动，测试使用临时技能文件和受控 prompt。`test_guardrail_checker.py` 复用原有用例未修改；包含原金额/概率/证据/承诺等护栏回归。真实 EmployeeServiceImpl 与 OpportunityServiceImpl 运行时，只有仓储/审计边界替换为无 IO 的受控实现；这证明服务的判权顺序和参数约束，不证明 PostgreSQL 实际隔离。

## 自审结论与明确限制

- 实际交付为通用 ContextBuilderService、真实公开域服务窄 adapter、不可变逐任务 WorkerContextAdapter、可执行受控 Consumer 验收。没有伪造生产 AgentJob、没有新队列表、没有 scheduler 改写。
- W2 必须继续显示“通用 Agent Worker 未启用；ContextBuilder 组件通过受控验证，尚无生产模型消费者”。scheduler 的模板发送/窄回复模型链不受本批影响。
- 缺生产获批 durable descriptor/policy provider 和员工只读 Playbook 投影，因此 production generic worker **disabled**。组织域授权未扩大。
- `Gate` 是记录端口：只证明 worker 原 gate 获得保留 high risk 标记的变更或护栏拒绝。**真实 approvals 提交、批准、应用与落库均 not_run**，不能据此称审批集成已经完成。Task 2 checklist 已加限定说明。
- 当前真实事实 adapter 只提供精确机会及四个有来源字段；历史与供应背景仅经 controlled typed provider 验证，未接实际检索源；来源存在多条历史时保守拒绝，后续若需要选择当前证据应扩展明确的公共版本契约，不能猜最新一条。
- 员工 identity reader 用 list_active 公共接口，没有新增高规模按 user_id 查询；生产大规模需求应由另批评审公开服务改动。认证仍由获批任务生产者负责，本 reader 只做可信主体映射。
- 各新 provider/adapter 的公开异常边界固定转换为不可重试 ValidationError（取消继续传播）；当前泛用 Worker 未启用。将来接真实 DB/provider 前，应分别制定临时依赖故障的脱敏重试分类，不把本批受控错误分类当生产故障策略。
- 未读网络、DB、环境变量、.env、凭证或 DSN；未操作 Catalog 环境和共享 .git 配置/清理；没有子代理。

## Git 已知噪声与提交后状态

Git status/diff/add/commit 反复输出既有 stderr：

```text
error: non-monotonic index /Volumes/T7/Company/Auto_customer_acquisition/.git/objects/pack/._pack-71922f8ae42513044763e2e33c0d31feea40abf1.idx
```

未修复或删除该工作树之外的 AppleDouble 文件。提交成功，包含 11 个任务文件。Git 另提示自动推导 committer name/email；没有修改 git config 或 amend 身份。提交后 `git status --short` 无变更项（仍有上述 stderr），`git rev-parse HEAD` 为本报告所列 head。本报告待 controller 独立审查，不自动推进 ledger 或下一批。
