### Spec Compliance

- ❌ Issues found: 能力矩阵覆盖了 Task 0 要求的 11 项能力、状态、执行者、域服务、外部能力和开启条件，也同步了 Task 0 的五个状态框；但成本报价行把 `quoted` 写成无例外的绝对门槛，遗漏了总纲硬边界 7 明确保留、且现有领域代码已经实现的“人工明确接受 `indicative` 风险并留痕”路径（`docs/operations/web-core-capability-matrix.md:36`）。
- ⚠️ Cannot verify from diff: Task 开始时 tracked/staged/untracked 均为空、Python/Node 精确版本、迁移链尾、控制者先前的 60 个测试结果、`git diff --check` 和链接存在性属于命令/过程证据，diff 只记录结论（`docs/operations/web-core-capability-matrix.md:9-14`）。本次评审按控制器约束没有重跑 Git、测试或环境检查；控制器应以留存的原始基线输出确认这些 checkbox 的过程证据。

### Strengths

- 文档先定义四种状态，并明确“有页面/Protocol/历史验收”不能等同于当前进程可启动，避免把可组合源码夸大为 production ready（`docs/operations/web-core-capability-matrix.md:3,16-23`）。
- 11 项能力均给出页面/API、唯一执行者、域服务、外部能力、开启门槛和后续任务归属；Agent/Browser Worker 在缺少真实持久任务源时明确保持 disabled（`docs/operations/web-core-capability-matrix.md:25-39,47-48`）。
- 本机身份段准确区分前端 `authenticated` 标签和服务端认证，并保留 loopback、隔离数据及非 dev 403 边界（`docs/operations/web-core-capability-matrix.md:53-57`）。
- 计划变更严格限于 Task 0 的五个 checkbox，没有夹带业务代码或范围外配置改动（`docs/superpowers/plans/2026-09-05-web-first-completion.md:62-66`）。

### Issues

#### Critical (Must Fix)

- 无。

#### Important (Should Fix)

- `docs/operations/web-core-capability-matrix.md:36`： “只有 quoted 价格可进正式客户报价”遗漏了允许 `indicative` 成本经人工风险接受并留痕后支撑客户报价的例外。现有门禁明确在存在 indicative 项且 `risk_acceptance is None` 时才阻断，并把“取得供应商实报价或完成人工风险接受”列为二选一（`domains/costing/service.py:178-195`）；模型注释也说明 indicative 且无风险接受时才不能支撑客户可见报价（`domains/costing/models.py:302-306`），审批类型包含 `INDICATIVE_RISK_ACCEPTANCE`（`domains/approvals/service_impl.py:70`）。这份矩阵是后续 W5/W6 的能力与验收基线，绝对化表述会让实现和验收错误地删除一个已批准的业务路径。应改为“只有 quoted 可直接进入；indicative 仅在人工明确接受风险并完整留痕后可进入，并在审批包/客户报价链路中显著保留风险标记”。

#### Minor (Nice to Have)

- 无。

### Checks

- 命名风险“Scheduler 是否真的缺少生产 bootstrap”：抽查 `apps/scheduler_worker/main.py:472-485` 与 `apps/scheduler_worker/runtime.py:608-650,932-940`，确认零参数入口失败关闭，业务 composition 由 typed dependencies 注入。
- 命名风险“本机身份是否被文档误称为真实认证”：抽查 `apps/api/identity.py:117-133` 与 `apps/web/src/api/client.ts:71-128`，确认非 dev 直接 403，前端两种模式都只绑定身份 header，服务端使用固定 tenant。
- 命名风险“反馈 Worker 是否已经具备客户回复正文入口”：抽查 `connectors/gmail/AGENTS.md:5-9,78-82` 与 `apps/email_feedback_worker/AGENTS.md:5-17`，确认当前能力仅是 DSN/ARF typed feedback，回复正文 worker 明确不在现有范围。
- 命名风险“Agent/Browser Worker 是否存在未被矩阵发现的生产 repository”：全库非测试 Python 定义检索只命中 `apps/agent_worker/main.py:53` 和 `apps/browser_worker/main.py:85` 的 Protocol；两个入口在无 factory 时都失败关闭（`apps/agent_worker/main.py:268-278`，`apps/browser_worker/main.py:257-267`）。
- 没有运行测试、Git、网络、数据库或读取环境凭证。

### Assessment

**Task quality:** Needs fixes

**Reasoning:** 文档结构、任务覆盖和执行边界整体清楚，绝大多数高风险事实经聚焦源码抽查成立；但成本报价这一核心硬边界的例外被漏写，修正前不能把该矩阵作为后续实现和验收的可信基线。

---

### Fix 1 定向复审（0b58bd9）

#### Spec Compliance

- ✅ Spec compliant：`docs/operations/web-core-capability-matrix.md:36` 现已明确区分 `quoted` 可直接进入客户可见报价，以及仅有 `indicative` 时必须人工明确接受风险、完整留痕并持续显著展示风险标记的例外路径；此前 Important 已关闭。
- ⚠️ 原评审关于命令/过程证据无法从 diff 独立验证的说明不变，不构成本次措辞修复的缺陷。

#### Issues

- Critical：无。
- Important：无。
- Minor：无。

#### Assessment

**Task quality:** Approved

**Reasoning:** 定向 diff 只修正此前指出的报价门槛表述，与总纲硬边界及既有 `INDICATIVE_RISK_ACCEPTANCE` 机制一致，没有引入额外范围或新的歧义。

#### Checks

- 仅阅读 `.superpowers/sdd/2026-09-05-web-first-completion/task-0-fix1.diff`；没有重读完整评审包、运行测试/Git、访问网络/数据库或派发代理。
