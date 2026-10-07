# Task 0 报告：基线与 Web 能力清单

日期：2026-09-05

## 状态

Task 0 已完成。新增 Web 核心能力矩阵，并且只把实施计划中 Task 0 的五个进度框从未完成改为完成。未修改 README、HANDBOOK、业务代码、配置或其他计划内容；未启动进程、连接数据库、访问网络或读取 `.env`/凭证。

提交：`eb147df`（`docs: inventory web core capabilities`）。

## 基线证据

- 隔离工作树：`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion`
- 分支：`codex/web-core-completion`
- 盘点时 HEAD：`ec801a87270503b92d3c70389cbcd4b92807dbb9`
- 功能基线：`1b760b2`
- Task 0 开始时 tracked、staged、untracked 路径为空；分别以 `git diff --name-only`、`git diff --cached --name-only`、`git ls-files --others --exclude-standard` 核对。
- 本地迁移源码单一链尾：`0058`，`down_revision = "0057"`；没有读取数据库 revision。
- `.venv/bin/python --version`：Python 3.12.14。
- `node --version`：v24.15.0；`apps/web/package.json` 要求 Node 24.x。
- 控制者已在同一基线运行 `python3 scripts/check_boundaries.py`，7 项 PASS，并运行 `test_agent_worker`、`test_guardrail_checker`、`test_api_runtime`、`test_inbox_api`，共 60 passed in 4.98s；按任务指令没有重复运行。
- Git 安全读取持续报告共享对象库 `._pack-*.idx` 的既有 `non-monotonic index`。本任务没有删除、改写、repack 或修复共享 `.git/objects`。

## 源码核对结论

1. API 的 `create_runtime_app()` 是可装配入口，会严格读取显式配置并在 lifespan 核对 schema；`create_app()` 的零配置形态只会失败关闭。
2. Scheduler 有完整 `SchedulerRuntimeFactory`，但 `main()` 没有零参数生产 bootstrap；typed 业务依赖必须由部署层注入。它是 W2 的主要启动缺口。
3. Agent Worker 与 Browser Worker 只有 Protocol、循环和可注入 factory，没有生产 job repository/零参数 factory，也没有持久任务来源；两者应保持 disabled。
4. Email Feedback Worker 有真实入口，但只注册 `email.feedback.fetch`，处理 DSN/ARF 退信和投诉；客户回复正文读取、RawArtifact 归档和可信关联是 W3 必须新实现的能力。
5. Reply Qualification 已有定义、动作组合和 `InboundMessageStored` 事件接线 seam，但 scheduler 的组合是可选依赖，生产入口没有提供真实模型与正文 reader；因此现有 Inbox 数据查询不能证明入站闭环已运行。
6. Web 的 `authenticated` 身份标记只会写 `X-Tenant-Id`/`X-Employee-Id`。API 在 `dev_mode=false` 时无条件 403；dev 模式仍从固定租户 Employee public DTO 重读角色和 scope。本机测试必须 loopback + 隔离数据，多人使用必须另做真实后端认证。
7. 寻源、成本报价、审批、Run 和 Settings/Catalog 已有可组合业务链及历史受控验收；它们仍需 W2 的同版本受控启动与 W5/W6 的跨页面回归。Catalog 培养下游明确暂缓，终点只能显示 `queued`。

## 文件

- 新增：`docs/operations/web-core-capability-matrix.md`
- 更新：`docs/superpowers/plans/2026-09-05-web-first-completion.md`，仅 Task 0 五个 checkbox。
- 本报告：`.superpowers/sdd/2026-09-05-web-first-completion/task-0-report.md`。该路径受 `.superpowers/sdd/.gitignore` 管理，不纳入提交。

## 检查

- `git diff --check`：PASS；只出现既有共享 Git pack warning，没有 whitespace 错误。
- 所有能力矩阵中的本地源码、验收、规格和计划链接逐一以 `test -e` 核对：PASS，无缺失路径。
- 计划 diff 人工核对：只改 Task 0 的五个 `[ ]` 为 `[x]`。
- 必需能力项人工核对：指令、发现、Campaign、Inbox、Need、接管、寻源、成本报价、审批、Run、Settings 共 11 项均存在；每项均写明状态、API、流程执行者、域服务、外部能力、开启条件/缺口和验收归属。
- 进程矩阵人工核对：API、Scheduler、Agent、Browser、Email Feedback、Notification、Web 均有当前入口事实和 Web 核心结论。
- 没有运行全量/新增测试：本任务只有文档，且控制者已给出本批要求的结构和聚焦基线。

## 自审

- 区分了“已实现业务事实”“可注入组合”“正式进程可启动”和“真实 Provider/运营已验收”，没有把历史受控验收推断为生产 ready。
- 每个状态变化动作都归给 API 即时调用或持锁 scheduler；Agent/Browser Worker 不获得虚构队列。
- 没有放宽九条硬边界：金额/报价、Provenance、事实与推断、可达性、租户、凭证和审批边界均在相关行明确保留。
- 身份结论直接来自前端 header 行为和 API `dev_mode` 拒绝逻辑，没有把前端 `authenticated` 名称误称为真实认证。
- 矩阵最后明确没有读取 `.env`、凭证、数据库和网络，也没有任何外部效果。

## 关注项

- 共享 `.git/objects/pack/._pack-*.idx` warning 会污染 Git 命令输出，但当前未阻止读取/提交；属于工作树外的既有文件系统问题，应由控制者决定是否单独治理。
- “已有可组合”不等于已纳入统一启动。矩阵已给出严格定义，后续 W2 不应仅以文件存在或历史测试通过把进程标为 ready。
- API 目前要求若干邮件/对象存储相关显式配置，同时正式工厂并未注入手工发送组合；W2 配置分层时应保持缺能力原因可见，避免为了启动只读页面而伪造可发送状态。

## Review fix round 1

- 审查文件：`.superpowers/sdd/2026-09-05-web-first-completion/task-0-review.md`。
- 唯一 Important 已修复：能力矩阵成本报价行现明确 `quoted` 可直接进入客户可见报价；只有 `indicative` 时，必须由人工明确接受风险并完整留痕，并在审批包/客户报价链路继续显著保留风险标记，否则拒绝进入。
- 修复只改该行文档措辞，没有改变业务代码、审批规则或其他能力状态。
- 修复提交：`0b58bd9`（`docs: restore indicative quote exception`）。
- 验证：`git diff --check` PASS；提交只含能力矩阵一行替换。未重跑测试或全仓审计。
