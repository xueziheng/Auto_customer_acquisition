### Spec Compliance

- ❌ Issues found：`infra/controlled/resources.py:63` 在进程 leader 已退出时仅清理此前记录过的 children，随后返回成功。leader 在首次 children 快照前退出的真实启动窗口会留下同组存活子进程，却仍可得到成功清理结果，违反本批“只清理可核实的 owned 资源、未知清理非零”的停止契约。
- ✅ 已核对本批具名文件：启动脚本、操作说明、集成测试、Makefile、README 均有对应变更；未发现业务种子、临时审批接口、apps 进程互导或 infra 复制业务编排。`apps/api/controlled.py:84` 仅初始化持久员工；`apps/scheduler_worker/controlled.py:30` 使用 CanonicalSchedulerBootstrap。
- ⚠️ 原 API 工厂和 canonical scheduler 的完整内部行为不在此 diff；本审查核对消费方式，不重新审查 Task3b。Task5/6 正文与回复、Task8 sender Web 配置、Task12 全链与旧扫描命中裁定属于已明确的后续范围，未作为本批缺失，也不将 ready 当业务验收通过。

### Strengths

- `infra/controlled/config.py:43`、`:51`：配置独占创建、权限/UID/owner 目录校验；凭证字段使用 SecretStr，受信解析与模型接口分离。`apps/api/controlled.py:30` 的模型只接受明确中文演练输入，仍消费原生产提案入口。
- `infra/controlled/resources.py:137`：容器使用固定本机 Docker socket、随机 owner label 和完整 ID；创建后核对 loopback 发布地址，清理结果不确定时保留固定错误。
- `infra/controlled/providers.py:35`、`:53`：Provider 调用单独持久化，重复 send 不因消息去重而丢失调用记录；消息与调用查询都有 tenant 过滤，外部场景不读取业务 PG。
- `apps/web/src/api/client.ts:107`：受控身份 setter 限定显式 DEV 配置内员工并保持 fixed-dev；`apps/web/tests/controlled-identity.test.ts:6` 覆盖 generation、订阅、未知员工与 PROD 拒绝。`apps/web/src/App.vue:9` 仅在受控模式订阅页面重建，避免改变普通身份流程。
- `apps/web/vite.controlled.config.ts:8`：状态接口只返回安全投影且按四秒时效判定 unknown；`apps/web/src/components/ControlledModeBar.vue:41` 清楚说明未启用能力。测试使用真实独立进程、HTTP 审批和原调度激活，不用批准结果种子替代。

### Issues

#### Critical (Must Fix)

- 无。

#### Important (Should Fix)

- **I1 — leader 在首次快照前退出可遗留子进程并误报清理成功。** `infra/controlled/resources.py:63` 在 `poll() is not None` 时直接调用 `_close_children()`；`:82` 的 `_record_children()` 仅能在 leader 仍活着时按父子关系登记。新建子进程若在状态快照前退出，其子孙会脱离父子树，`children` 为空，停止函数成功返回而同 owner 进程组仍有活进程。启动期间的 Vite/helper 异常就是此类窗口，现有“已记录 children 后杀 leader”的测试不回答它。应让生命周期拥有独立于已退出 leader 的可验证进程组/会话清理机制，或使用保持存活的监督包装确保子孙能被登记；无法确认组已清空时必须返回非零，不能默认为已清理。增加“leader 产生孩子后立即退出、此前从未调用 public”的真实进程用例，并核验没有 owner 存活残留。

#### Minor (Nice to Have)

- 已知验证噪声仍存在：实现报告记录的 172 条 ESLint warnings、VueRouter R0004，以及四处旧敏感扫描命中。按控制器给定基线证据不列为本批新增缺陷，但结果不能描述为全仓门禁无告警；Task12 仍需裁定。参见 `.superpowers/sdd/2026-09-05-web-first-completion/task-4-report.md` 的“基线告警”段。

### Assessment

**Task quality:** Needs fixes。

**Reasoning:** 装配与受控边界总体符合本批范围，已有验证覆盖正常启动、审批和多种失败路径；I1 是实际复现的生命周期缺陷，会使成功退出与真实资源状态不一致，应在接受 Task4 前修复。

### Checks and scope

- 只读审查 base `68fe023fbe8c838ca4cd264c25f3baac0eec2672` → head `b818263e283c496385d0d55629b3cd05359937ee` 的指定 diff，按块单遍阅读；未重跑 git，未单独重读 changed 源文件，未派子代理。首次合并输出发生截断，随后仅补读未显示的元数据及 AGENTS 内容；实现报告/子规格按任务要求单独读取。
- **具名疑问：首次快照前死亡是否漏清理。** 仅运行一次真实父子进程聚焦验证：调用现有 OwnedProcess.start，父进程生成 sleep 子进程后立即退出；等待父退出后直接 stop，不调用 public。实际结果为 `stop_returned_success=True`、`recorded_children=0`、`child_still_running=True`、`child_in_owner_group=True`。验证结束已按出生时间和 PGID 核对并清理该测试孩子；未启动 Docker 或业务服务。
- **具名疑问：Node 版本探测继承 NODE_OPTIONS 是否执行 preload。** 一次临时脚本验证当前 node `--version`：退出 0，受控 preload 标记未执行；未据此提出缺陷，未读取既有用户环境配置。
- **具名证据核对：全新声明依赖环境的停止结果。** 仅读取报告给出的 fresh-final `status.json` 安全状态字段，文件存在，实际为 `status=stopped`、`reason=requested_stop`、`cleanup_errors=[]`。此项支持该次停止记录，不能替代 I1 的未覆盖窗口。
- 未重跑已报告的 23 后端/337 Web/typecheck/lint/build/边界检查；没有把报告中的执行结果冒称本审查重新运行。无其他 diff 外代码检查，无源码/index/HEAD 修改；唯一持久写入为本审查报告。

## Fix1 定向复审（2026-09-05）

### Finding Verdicts

- **I1：首次 public 快照前 leader 退出，漏清理孩子并误报成功 — ADDRESSED。** `infra/controlled/process_anchor.py:14` 与 `:32` 在业务 exec 前建立并握手确认同组 anchor；`infra/controlled/resources.py:65` 记录 anchor 出生身份并核验，`:153` 在存活 anchor 核验前后枚举实际组成员，因而 leader 已退出时不再依赖已有 children 快照。`:110` 停止前补录组成员，`:122` 确认无业务成员后才要求 anchor 退出；强停、未知归属与残留均不能走正常成功结论。`tests/integration/test_web_core_launcher.py:551` 新用例精确覆盖“从未 public、leader 已退出、孩子未登记”，报告含对应 RED 与 GREEN。

### New Breakage in the Fix Diff

- **Critical / Important：无。** `infra/controlled/process_anchor.py:34` 的 anchor 子进程只显式继承生命周期 FD 与最小环境；原业务 FD 留给原 PID 的 exec。`scripts/controlled_web_supervisor.py:267` 在忘记成功的一次性进程前释放 anchor，避免修复伴生的 bootstrap 残留。fixdiff 中同时提供握手失败不 exec、监听 FD/管道 EOF、出生身份不匹配和 TERM 升级清理行为测试。
- **Minor：无新增发现。**

### Out-of-Scope Observations

- 无新增域外观察；首轮记录的旧 lint/Router/敏感扫描门禁及后续任务范围不变，不延长本轮修复范围。

### Verdict

**Fix round:** All findings addressed, no new Critical/Important breakage。

**Task quality:** Approved（Task4 本批范围；不代表后续全链业务验收通过）。

### Checks

- 按定向复审模板核对 I1 与唯一修复 diff：base `b818263e283c496385d0d55629b3cd05359937ee` → head `ed1d81601582bb663e30f64ddc62c15c25034f30`，源码提交 `f1632658bfe8f2a4377c171aaba171ddc512179a`；以 diff 元数据与实现报告为提交标识依据。
- 单次分块读取 811 行 fixdiff；先读取实现报告末尾 Fix1，再逐项核对实现与新增测试。未重新读取旧源码、未运行 git、未派子代理、未重跑任何测试或静态套件。
- 报告明确区分完整 22 项 launcher 回归与随后追加的 1 项强停单跑，并提供原缺陷与短 bootstrap 伴生缺陷的 RED/GREEN。覆盖测试与 fixdiff 对应；本复审未把报告结果表述为自行重新执行。
- 只追加本审查报告，未修改源码、index、HEAD 或其他持久文件。
