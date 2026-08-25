# Task 6D：精确 HEAD 全量验收与证据报告

日期：2026-08-26

任务基线：`7d098fc48f000a55586870c1dd855454bce1af98`

最终受控验收 HEAD：`3e4d3ad72641bf772fef26f15092828cea933f9e`

## 结论

最终 HEAD 的本地代码、真实 PostgreSQL、真实 Uvicorn/Vite/Chromium 受控验收全部通过。
本任务发现并按 RED→GREEN 修复一个生产并发死锁和一个 E2E 测试隔离缺陷；每次提交后均从
新的 HEAD 重启完整验收矩阵。受控仓库框架可以进入最终整分支审查，但这不是 Phase 1 运营完成。

真实 Hunter、Web/provider、模型、Gmail/邮件、客户数据、送达率、域名信誉和人工接管时效均未
运行或观察，状态为 **`not_run`**。本任务没有安装依赖、访问外部 provider、发送邮件、部署、
push 或使用真实凭证。

## 环境与工作树

- macOS 26.5.1（25F80），arm64；Python 3.12.13；Alembic 1.19.0。
- Node v24.15.0；npm 11.12.1；Docker 29.5.3；Docker Compose 5.1.4。
- PostgreSQL：disposable `pgvector/pgvector:pg16` 容器；Alembic 单一 head：`0038`。
- Playwright Chromium / Google Chrome for Testing 151.0.7922.34。
- 开始与结束均保留既有未跟踪符号链接 `apps/web/node_modules`，目标为主工作区依赖目录；
  未纳入暂存或提交。验收截图与命令日志全部在仓库外。

## 最终精确 HEAD 门禁

以下均在 `3e4d3ad72641bf772fef26f15092828cea933f9e` 上重新执行；退出码均为 0。

| 门禁 | 精确命令 | 结果 | 时间 |
|---|---|---|---|
| 真实 PG 迁移 | `pytest tests/integration/test_migrations.py -q` | 59 passed；空库升级、历史 downgrade/upgrade、0038 roundtrip 与单 head | 69.47s；wall 70.57s |
| API 类型生成 | `npm run gen:api` | 成功 | wall 1.50s |
| 生成契约零差异 | `git diff --exit-code -- src/api/api.d.ts` | 零差异 | wall 0.03s |
| 前端类型 | `npm run typecheck` | 通过 | wall 3.58s |
| 前端 lint | `npm run lint` | 0 errors；133 个既有 baseline warnings，无新增 | wall 2.73s |
| 前端单测 | `npm run test` | 19 files / 151 tests passed | 9.95s；wall 10.62s |
| 前端构建 | `npm run build` | 96 modules transformed | Vite 726ms；wall 4.38s |
| Ruff | `ruff check .` | All checks passed | wall 0.15s |
| mypy | `mypy domains shared tool_gateway connectors apps workflows notification_gateway infra` | 394 source files，0 issues | wall 1.04s |
| 架构边界 | `python scripts/check_boundaries.py` | 七类检查全部通过 | wall 2.84s |
| 敏感扫描 | `python scripts/scan_sensitive.py` | 无发现 | wall 1.67s |
| 全量非 E2E | `pytest -q -m "not e2e"` | 3988 passed，6 deselected | 308.46s；wall 310.77s |
| 强制 E2E | `TRADEOS_REQUIRE_E2E=1 pytest tests/e2e -q -W error` | 6 passed；warning-as-error | 47.22s；wall 49.27s |

前端矩阵另外用 `set -euo pipefail` 的干净串行 shell 重跑并单独留证，避免把一次缺少 Python
环境 PATH 的命令包装错误误记为产品结果。

## Reply eval：仅受控关键词 smoke

命令：`python -m tests.evals.reply_evals_runner`；退出码 0；wall 0.13s。

- 总用例 160，错误 0，总体准确率 0.762；
- 退订召回 1.000；自动回复误停率 0.000；投诉召回 0.700；提取召回 0.000；
- 动作契约准确率 0.775；退订范围准确率 1.000。

这些是 `keyword-baseline-v1` 的本地 smoke 指标，不是生产模型准确率，也不是 provider 验收。
真实 provider/model eval 明确为 **`not_run`**；其中提取召回 0.000 是已知 smoke 基线风险，
不得用总体 0.762 掩盖。

## 本任务发现、分类与 TDD 修复

### 1. 手工发送 20 并发出现 500：生产缺陷

初始全量非 E2E 为 `3 failed, 3983 passed, 6 deselected`。并发用例连续两次稳定失败，
每次约 37 秒，状态中含 5 个 500。安全 stack 与 PostgreSQL `pg_stat_activity` 证明：

`API manual-send → OutreachService current-fact validation → campaign/enrollment/attempt row locks
→ service-backed Campaign approval provider → ApprovalService → 第二个 DB connection`。

其余并发请求占满 SQLAlchemy QueuePool 的 5 个基础连接与 10 个 overflow，并等待同一 Outreach
行锁；持锁者再申请第 16 个连接，最终得到 `sqlalchemy.exc.TimeoutError`，被中间件映射为 500。
诊断只记录错误类型、调用边界和锁类别，未把凭证、PII、正文或原始异常敏感内容写入报告。

RED：新增两个最小锁序测试，首次为 `2 failed`，分别证明 prepare 与 preflight 的 approval
读取晚于 Campaign lock。

修复：在获取 Outreach 行锁前读取精确 Campaign/version 的不可变审批快照；锁内仍重读并校验
Campaign、Enrollment、Attempt、精确版本及当前 Campaign approval binding，不放松 current-fact
门禁。

GREEN：锁序 `2 passed`；原 20 并发集成 `1 passed in 7.30s`，无 500；受影响组 `82 passed`。
提交：`945db2423a697132d25a296aa50434bb2acb0677`。

同一初始矩阵的另外两个失败是测试契约落后于 0036–0038 schema：公开 Enrollment key set 缺
`source_hypothesis_id`，repository parity 缺新增列、索引、FK/CHECK。测试最小更新后目标
`2 passed`；未修改生产 schema 来迁就旧断言。

### 2. 全 E2E 两项顺序相关失败：测试隔离缺陷

首次完整 E2E 为 `2 failed, 4 passed`：Phase 1 用例预期自身两条 handoff，却看到前序
opportunity-board 写入后的三条；随后 Playbook 用例预期未配置，却看到 Phase 1 已写入的 active
Playbook。两个失败用例各自单独 pytest session 均通过，确认不是产品或环境缺陷。

共享 `e2e_stack` 的契约已明确：写 append-only 事实的测试必须使用独立 lifecycle。Phase 1 闭环
违反了该契约。RED 即上述完整 E2E 两项失败；最小修复仅把该测试改为 function-scoped 独占真实栈；
GREEN 为完整 E2E `6 passed in 46.47s`。提交：
`3e4d3ad72641bf772fef26f15092828cea933f9e`。随后又在该提交从头重跑本报告全部门禁。

### 3. 非产品命令错误

- 一次 zsh wrapper 使用只读变量 `status`；实际边界命令立即独立重跑通过。
- 一次 E2E wrapper 把环境变量放在 zsh `time` 的非法位置；改用 `/usr/bin/time -p env ...` 后执行。
- 最终 HEAD 首次并行启动前端时漏设项目 Python PATH，`gen:api` 找不到 FastAPI；设定 Python 3.12
  PATH 后重跑，最终又用 `set -euo pipefail` 干净串行重跑完整前端矩阵。

这些均发生在测试进程进入业务断言前，分类为命令/环境包装错误，不是绿灯，也没有通过修改代码、
跳过测试或安装依赖来消除。

## Mutation / security 审计

审计基线为 Task 6D 起点 `7d098fc...`，对比最终 HEAD 共 5 个文件、108 insertions、21 deletions：
一个 Outreach 锁序生产修复，两个锁序回归，两个 schema/API parity 更新，以及一个 E2E 隔离修复。
`git diff --check 7d098fc..3e4d3ad` 通过。没有新增外部出口、凭证路径、金额 float、数值置信度、
跨域 import 或未带 tenant 的查询。

现有 adversarial 定向矩阵为 `166 passed in 11.14s`（wall 12.41s）；crash/replay 专项为
`3 passed in 5.35s`（wall 6.32s）。映射如下：

| 高风险 mutation | 杀死它的定向证据 |
|---|---|
| 移除/错用 tenant filter | reply evidence cross-tenant、message reader cross-tenant、workflow tenant/type/subject、outbox tenant mismatch、Phase 1 tenant-bound replay |
| 接受 stale Campaign/version/current fact | 16 类 preflight current-fact mutation、stale enrollment version、manual-send current-facts、20 并发 atomic send |
| 未验证联系人入组 | `test_only_verified_contact_point_may_enter_sequence`、untrusted contact snapshot 参数化门禁 |
| 模型输出数值置信度 | demand agent numeric-confidence variants；全库 boundary confidence scan |
| Money 使用 float/int/bool 或 JSON number | 完整 `tests/unit/test_money.py` 的 Decimal、非有限值与 string-only schema 矩阵 |
| 无客户证据产生 Validated Need | durable customer evidence verifier adversarial、Need verified binding、完整 PostgreSQL Phase 1 闭环 |
| 原始消息无界或泄漏 | subject/body 注入上限、raw bytes over-limit、workflow fingerprint 不存 raw、Need/outbox/log marker 扫描、浏览器闭环四类 forbidden marker |
| outbox/workflow/log 泄漏 | event explicit whitelist、unsafe payload rejection、unknown event rejection、metadata-only/digest、sanitized failure、E2E console/process/durable-state 扫描 |
| 崩溃重放导致重复或丢失 | reply need→opportunity crash replay、proposal commit→outbox recovery、email executing→reconciliation without Gmail、Phase 1 replay-safe、manual-send idempotency |
| approval/error/event 任意字符串穿透 | approval public error allowlist、显式 event registry、unsafe Outreach event serialization |
| 迁移约束缺失/漂移 | 59 项真实 PG migration file tests，包括 0038 JSONB object CHECK、FK/index/schema parity 与 roundtrip |
| 浏览器 chain ID 被 seed/错连 | final Phase 1 E2E 逐 ID 断言 proposal/run/signal/hypothesis/account/enrollment/outbound/inbound/need/opportunity/handoff |

未执行随机 mutation engine，因此没有声称 mutation score；本审计证明的是列明的既有对抗 mutation
均被命名测试杀死。未发现新的未覆盖安全缺口。剩余的主要不可判定区域是外部 provider、真实模型、
真实邮件和运营数据，它们按授权边界保持 `not_run`。

## Browser / 截图与控制台证据

最终 Phase 1 七张截图（仓库外）：

```text
/var/folders/t2/6_w0ct0s04l68z693_h2fzkw0000gn/T/
  tradeos-task-6c-run_01M0X545AZ3FQ8V6ED6CF91DXY/
    01-command-confirmed.png
    02-demand-evidence.png
    03-discovery-waiting.png
    04-campaign-enrolled.png
    05-need-mobile.png
    06-opportunity.png
    07-handoff-mobile.png
```

设置、国家政策、manual send、通知、发件身份的 11 张 desktop/mobile 截图：

```text
/tmp/tradeos-6d-evidence.YkqYjD/final-e2e-screenshots-3e4d3ad/
```

已逐张检查 Phase 1 闭环并抽查额外 11 张：页面非空、无 Vite overlay、无明显横向溢出或断链；
desktop/mobile 均显示预期真实状态、typed IDs、焦点环、审批/安全提示和人工接管动作。E2E 对各
旅程适用的 console/page error、关键 HTTP 响应、标题/heading、交互、overflow、durable chain IDs
与 forbidden raw markers 做了明确断言；最终 6 项全部通过。未复用旧 HEAD 的截图。

## 运营真相（HANDBOOK 四项标准）

| 运营标准 | 状态 | 原因 |
|---|---|---|
| 从零创建真实 Campaign 并得到一批真实已验证需求 | `not_run` | 只使用合成数据与受控 transport |
| 每个真实 Validated Need 均可追溯到客户原话/证据 | `not_run` | 代码/E2E 证据链通过，但没有真实客户事实 |
| 真实发件域名信誉保持健康 | `not_run` | 未发送真实邮件，未观察送达/退信/投诉/信誉 |
| 真实人工接管等待时间可测且无超时丢失 | `not_run` | 只验证本地工作流和 UI，未观察真实运营 SLA |

`phase1_operating_acceptance = not_run`

## 剩余风险与下一步

1. 关键词 smoke 的提取召回为 0.000；上线前必须用授权的真实/代表性 provider-model eval 单独设阈值，
   不能把受控总体准确率当生产质量。
2. 真实 Hunter/Gmail/provider 配置、认证、配额、成本、SLA、邮件 threading、送达与域名信誉未验证。
3. 本地真实 PostgreSQL 和 Chromium 证明代码闭环，不证明目标环境数据迁移、容量、网络和可观测性；
   部署前仍需备份、迁移演练和受控运营验收。
4. 下一步应做最终 whole-branch 独立 code review，再由用户选择 finishing-a-development-branch 的
   合并/PR 路径；本任务未 push 或 deploy。
