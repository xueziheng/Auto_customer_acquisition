# Task 6D：精确 HEAD 全量验收与证据报告

日期：2026-08-26

任务基线：`7d098fc48f000a55586870c1dd855454bce1af98`

最终审查修复基线：`1d8eb3cb10be2264d043e5828a62ba6294704aa7`

最终受控验收代码 HEAD：`184fc15bc4f69b29ff82bb22a5b8868558322bb5`

## 结论

`184fc15bc4f69b29ff82bb22a5b8868558322bb5` 上的本地代码、真实 PostgreSQL、真实
Uvicorn/Vite/Chromium 受控验收全部通过。本轮最终审查指出的两项 Important 均通过严格
RED→GREEN 修复：模型候选逐字证据现在由 Agent 和持久化公共契约共同执行 500 Unicode
code-point 上限并拒绝超长值；多消息 Need/Opportunity intake 现在使用每个字段各自已持久化、
客户已验证的来源，同时正确恢复既有 Opportunity 的分配和 handoff。

在首次全量重跑中又发现并修复一个同租户多 workflow engine 抢占未注册 workflow type 的真实
竞态；修复后从新 HEAD 重启全部矩阵。受控框架可以进入最终独立审查，但这不是 Phase 1 运营完成。

真实 Hunter、Web/provider、模型、Gmail/邮件、客户数据、送达率、域名信誉和人工接管时效均未
运行或观察，状态为 **`not_run`**。本任务没有安装依赖、访问外部 provider、发送邮件、部署、
push 或使用真实凭证。

## 环境与工作树

- macOS 26.5.1（25F80），arm64；Python 3.12.13；Alembic 1.19.0。
- Node v24.15.0；npm 11.12.1；Docker 29.5.3；Docker Compose 5.1.4。
- PostgreSQL：disposable `pgvector/pgvector:pg16` 容器；Alembic 单一 head：`0038`。
- Playwright Chromium / Google Chrome for Testing 151.0.7922.34。
- 开始与结束均只保留既有未跟踪符号链接 `apps/web/node_modules`，目标为主工作区依赖目录；
  未纳入暂存或提交。验收截图与命令日志全部在仓库外。

## 最终精确 HEAD 门禁

以下均在 `184fc15bc4f69b29ff82bb22a5b8868558322bb5` 上重新执行；退出码均为 0。

| 门禁 | 精确命令 | 结果 | 时间 |
|---|---|---|---|
| 真实 PG 迁移 | `pytest tests/integration/test_migrations.py -q` | 59 passed；空库升级、历史 downgrade/upgrade、0038 roundtrip 与单 head | 54.44s；wall 55.49s |
| API 类型生成 | `npm run gen:api` | 成功 | wall 1.61s |
| 生成契约零差异 | `git diff --exit-code -- src/api/api.d.ts` | 零差异 | — |
| 前端类型 | `npm run typecheck` | 通过 | wall 3.45s |
| 前端 lint | `npm run lint` | 0 errors；133 个既有 baseline warnings，无新增 | wall 2.75s |
| 前端单测 | `npm run test` | 19 files / 151 tests passed | 10.01s；wall 10.72s |
| 前端构建 | `npm run build` | 96 modules transformed | Vite 635ms；wall 4.16s |
| Ruff | `ruff check .` | All checks passed | wall 0.18s |
| mypy | `mypy domains shared tool_gateway apps workflows notification_gateway infra connectors` | 394 source files，0 issues | wall 1.18s |
| 架构边界 | `python3 scripts/check_boundaries.py` | 七类检查全部通过 | wall 3.04s |
| 敏感扫描 | `python3 scripts/scan_sensitive.py` | 无发现 | wall 1.75s |
| 全量非 E2E | `pytest -q -m 'not e2e'` | 3999 passed，6 deselected | 303.63s；wall 306.15s |
| 强制 E2E | `TRADEOS_REQUIRE_E2E=1 pytest tests/e2e -q -W error` | 6 passed；warning-as-error | 46.90s；wall 49.17s |
| 受影响 mutation/security 矩阵 | 15 个列明的 unit/integration 文件 | 323 passed | 37.33s；wall 38.74s |
| crash/replay/claim 专项 | 5 个显式 node IDs | 5 passed | 7.66s；wall 8.97s |

前端矩阵使用项目 Python 3.12 环境。首次命令曾误用系统 Python 3.9，因缺少 FastAPI 在生成前
退出；没有文件变化。设置正确 PATH 后从头串行重跑完整前端矩阵，以上才是验收结果。

## Reply eval：仅受控关键词 smoke

命令：`python3 -m tests.evals.reply_evals_runner`；退出码 0；wall 0.49s。

- 总用例 160，错误 0，总体准确率 0.762；
- 退订召回 1.000；自动回复误停率 0.000；投诉召回 0.700；提取召回 0.000；
- 动作契约准确率 0.775；退订范围准确率 1.000。

这些是 `keyword-baseline-v1` 的本地 smoke 指标，不是生产模型准确率，也不是 provider 验收。
真实 provider/model eval 明确为 **`not_run`**；其中提取召回 0.000 是已知 smoke 基线风险，
不得用总体 0.762 掩盖。

## 本任务发现、分类与 TDD 修复

### 1. 手工发送 20 并发出现 500：生产缺陷

初始全量非 E2E 为 `3 failed, 3983 passed, 6 deselected`。并发用例连续两次稳定失败，
每次约 37 秒，状态中含 5 个 500。安全 stack 与 PostgreSQL `pg_stat_activity` 证明边界链为：

`API manual-send → OutreachService current-fact validation → campaign/enrollment/attempt row locks
→ service-backed Campaign approval provider → ApprovalService → 第二个 DB connection`。

其余请求占满连接池并等待相同 Outreach 行锁；持锁者再申请额外连接，最终发生
`sqlalchemy.exc.TimeoutError`。诊断未记录凭证、PII、正文或原始异常敏感内容。

RED：两个最小锁序测试首次均失败。修复是在获取 Outreach 行锁前读取精确 Campaign/version 的
不可变审批快照；锁内仍重读并校验 Campaign、Enrollment、Attempt、精确版本及当前审批绑定。
GREEN：锁序 2 passed；原 20 并发 1 passed，无 500；受影响组 82 passed。提交：
`945db2423a697132d25a296aa50434bb2acb0677`。

同一初始矩阵另外两个失败是测试契约落后于 0036–0038 schema；只更新公开 Enrollment key set 与
repository parity 断言，未修改生产 schema 迁就旧断言。

### 2. 全 E2E 两项顺序相关失败：测试隔离缺陷

首次完整 E2E 为 `2 failed, 4 passed`；两个失败用例各自单独执行均通过。根因是写 append-only
事实的 Phase 1 闭环误用了 session-scoped 共享栈。最小修复仅将其改为 function-scoped 独占真实栈；
完整 E2E GREEN 为 6 passed。提交：`3e4d3ad72641bf772fef26f15092828cea933f9e`。

### 3. 超长模型候选逐字证据：生产完整性与泄漏风险

独立复现证明 QualificationAgent 与 `ReplyFieldEvidence` 均会接受超过 1,200 Unicode code points
的整段回复候选，导致原始正文可能进入 durable classification 与 Need Provenance。

六个最小回归先 RED，覆盖 Agent 边界、公共 domain contract、真实 PostgreSQL workflow、跨消息
provenance、既有 Opportunity 和 crash/replay。修复后：

- QualificationAgent 在任何 candidate 持久化前拒绝超过 500 Unicode code points 的 quote；
- `ReplyFieldEvidence`、durable snapshot 与 handoff contract 同样拒绝超长值，绝不静默截断；
- exact-substring 校验保持不变；prompt 与 ADR 0012 已同步公共边界；
- 对抗模型返回包含 email/credential/tail 标记的完整 1,230+ 字符正文时，真实 PG 流程产生零 durable
  classification、零 Validated Need/Opportunity、零 reply action，且标记不进入 workflow/log；
- enrollment 保持原状态，系统 fail closed。

提交：`1ddd2d8d9b10af76f1d5265589cb946bdf2834c2`。

### 4. 多消息 Need/Opportunity intake：生产语义缺陷

独立复现证明：Need 已由前一条已验证消息建立、Opportunity 已存在时，第二条正常回复仍被错误要求
把所有历史字段伪装成当前消息来源，最终抛出来源不匹配并卡住 handoff。

修复后的语义为：

- 已有 Opportunity：不重新要求历史 Need 字段来自当前消息；幂等恢复/确保 owner 与
  `QUALIFIED → ASSIGNED` 状态，再完成当前回复 handoff；
- 尚无 Opportunity：逐字段读取 tenant-bound、same-account 的 durable `provides_specification`
  classification，并保留该字段真实的 message source ID、quote、classifier 和 classified_at；
- 缺失、未验证、跨租户、错 account、错 quote 或字段不匹配一律 fail closed；
- 第二消息 stop→update→handoff 的 crash/replay 明确测试通过，没有重复副作用或 partial dead-end。

实现与上一项共同提交于 `1ddd2d8d9b10af76f1d5265589cb946bdf2834c2`；公共 contract 与 ADR 0012
均已更新。

### 5. 同租户 workflow engine 抢占未注册类型：生产竞态

在 `f6f449c...` 的首次强制 E2E 中出现 1 failed / 5 passed；失败用例单独执行通过。完整捕获证明：
后台 scheduler engine 与测试 account engine 共用 tenant/table，但 `poll_due` 未按本 engine 已注册的
`(workflow_type, workflow_version)` 过滤，因此会竞态 claim 对方 workflow，并以“definition not
registered”标为失败。

最小 RED 用两个同租户、注册互斥定义的 engine 稳定证明错误 claim；单一修复使 `poll_due` 只 claim
本 engine 注册的类型/版本，空注册集合返回 0。GREEN：并发目标 3 passed、workflow engine integration
46 passed、原 browser E2E 单测通过，随后从新 HEAD 完整矩阵 6/6 通过。提交：
`184fc15bc4f69b29ff82bb22a5b8868558322bb5`。

### 6. 非产品命令错误

- 早期一次 zsh wrapper 使用只读变量；边界命令立即独立重跑通过；
- 早期一次 E2E wrapper 的环境变量位置无效；修正包装后执行；
- 本轮一次前端 API 生成误用系统 Python；设置项目 Python PATH 后完整矩阵重跑通过；
- 最后一组额外 5 项专项首次用裸 `pytest`，新 shell 找不到命令；改用项目 Python 的
  `python -m pytest` 后 5/5 通过。

这些都在业务断言前退出，分类为命令/环境错误；没有被记为绿灯，也没有通过改代码、跳过测试或
安装依赖消除。

## Mutation / security 审计

最终审查修复基线 `1d8eb3c..184fc15` 为 18 个文件、675 insertions、103 deletions；Task 6D 原始
基线 `7d098fc..184fc15` 为 23 个文件、972 insertions、125 deletions。两段 `git diff --check`
均通过。没有新增外部出口、凭证路径、金额 float、数值置信度、跨域 import 或无 tenant 查询。

受影响矩阵 323 passed；另有显式 crash/replay/claim 5 passed。覆盖的高风险 mutation 包括：

| 高风险 mutation | 杀死它的定向证据 |
|---|---|
| 超过 500 code points 或整段正文进入 durable evidence | Agent/domain/workflow 三层 overlong rejection；真实 PG 零 durable classification/Need/action |
| quote 不是 exact substring | Agent 与 intake forged/wrong quote 对抗测试 |
| 跨租户或错 account 证据拼接 | reply evidence cross-tenant/same-account 校验 |
| 多消息字段被伪造为当前消息来源 | cross-message per-field source ID/quote/classifier 断言 |
| 已有 Opportunity 的第二回复卡死 | existing-opportunity idempotent assignment/handoff 与 replay 测试 |
| 崩溃导致 Need 有而 Opportunity/接管缺失 | second-message crash/replay stop→update→handoff 与完整 PG replay-safe 闭环 |
| engine claim 未注册 workflow | 两个同租户互斥 engine 的并发隔离测试 |
| 未验证联系人、stale Campaign/current fact | contact verification、preflight/current-fact、并发 atomic send 矩阵 |
| Money float 或 JSON number | `tests/unit/test_money.py` Decimal/string-only schema 矩阵 |
| outbox/workflow/log 泄漏 | explicit event whitelist、unsafe payload rejection、digest/sanitized failure、E2E forbidden-marker 扫描 |
| 迁移约束漂移 | 59 项真实 PG migration 测试，包括 0038 CHECK/FK/index/schema parity/roundtrip |

未执行随机 mutation engine，因此没有声称 mutation score。外部 provider、真实模型、真实邮件和运营
数据仍不可判定，按授权边界保持 `not_run`。

## Browser / 截图与控制台证据

最终精确 HEAD 的 Phase 1 七张截图位于仓库外：

```text
/var/folders/t2/6_w0ct0s04l68z693_h2fzkw0000gn/T/
  tradeos-task-6c-run_01M0X8EV8ET15J03XPXGS5TF1H/
    01-command-confirmed.png
    02-demand-evidence.png
    03-discovery-waiting.png
    04-campaign-enrolled.png
    05-need-mobile.png
    06-opportunity.png
    07-handoff-mobile.png
```

七张均已逐张人工查看：页面非空、无 Vite overlay，预期状态、ID、provenance、安全提示和 handoff
动作可见。E2E 同时断言 console error/warning、HTTP >=400、标题/heading、横向 overflow、完整 durable
chain IDs 与 forbidden raw markers。窄屏 handoff 页的长内部触发标识存在紧凑换行，但未遮挡安全
状态或操作；记为低风险视觉问题，不影响本次闭环真实性。

## 运营真相（HANDBOOK 四项标准）

| 运营标准 | 状态 | 原因 |
|---|---|---|
| 从零创建真实 Campaign 并得到一批真实已验证需求 | `not_run` | 只使用合成数据与受控 transport |
| 每个真实 Validated Need 均可追溯到客户原话/证据 | `not_run` | 代码/E2E 证据链通过，但没有真实客户事实 |
| 真实发件域名信誉保持健康 | `not_run` | 未发送真实邮件，未观察送达/退信/投诉/信誉 |
| 真实人工接管等待时间可测且无超时丢失 | `not_run` | 只验证本地工作流和 UI，未观察真实运营 SLA |

`phase1_operating_acceptance = not_run`

## 剩余风险与下一步

1. 关键词 smoke 的提取召回为 0.000；上线前必须用授权的代表性 provider/model eval 单独设阈值，
   不能把受控总体准确率当生产质量。
2. 真实 Hunter/Gmail/provider 配置、认证、配额、成本、SLA、邮件 threading、送达与域名信誉未验证。
3. 本地真实 PostgreSQL 和 Chromium 证明代码闭环，不证明目标环境迁移、容量、网络和可观测性；
   部署前仍需备份、迁移演练和受控运营验收。
4. 建议先完成最终 whole-branch 独立复审，再由用户选择分支合并/PR 路径；本任务未 push 或 deploy。
