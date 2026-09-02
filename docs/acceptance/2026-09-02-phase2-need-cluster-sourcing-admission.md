# Phase2 NeedCluster Sourcing Admission 验收记录

## 结论与范围

本记录确认 **NeedCluster Sourcing Admission** 已完成受控真实核心与 Browser 验收；只表示“老板确认
策略后，按需求簇优先级有界启动各自独立的 Sourcing Case V2”这一子项目完成，**不表示整个 Phase2
完成，也不表示生产已启用**。

- 实施基线：`b0869bc`。
- 受控核心使用真实 PostgreSQL migrations/repositories、领域服务、事务 Outbox、scheduler advisory
  lock cycle、Workflow engine、FastAPI 与 Vite；只替换已有 E2E 栈允许的 Tavily/page/model 外部端口，
  且本验收在调用它们之前停止。
- 一个 `Validated Need` 始终对应一个 Case、一条 Admission 和自己的不可变 Need/priority snapshot。
  需求簇只提供排序事实，不合并数量、规格、Provenance、Case、Run 或订单。
- 本轮没有 push、merge、deploy、生产激活或真实外部调用。

仍未完成的 Phase2 项目为：Catalog Product Proposal、联系人多源瀑布、70/30 allocator、自动
backpressure、真实 direct supplier quote、商业来源。它们仍需独立规格、实现、验收和相应授权。

## RED → GREEN 与根因证据

### 既有 Need → Case E2E 超时

既有 `test_controlled_need_to_estimated_cost_uses_real_core_only` 可稳定超时。诊断时直接读取真实
PostgreSQL：Need 已达到完整度 3，canonical V2 Case 已创建，但 Admission 停在 waiting、Workflow Run
精确为 0；Outbox 的 `NeedValidated` 与 `SourcingCaseOpened` 已 delivered。基线 `1cf4fe5` 的测试同样
直接等待 Case 推进，没有建立新的老板确认准入前提。

根因是旧测试仍假设 readiness 可以直接启动 Workflow，而 0055 后正确产品语义要求当前
boss-confirmed `sourcing_admission` policy。测试改为通过真实 proposal/confirm HTTP 链，并明确断言确认前
Run 为 0；没有直接写 active Directive，也没有放宽 fail-closed。

### `DirectiveActivated` composition 缺口

修正旧前提后出现独立 RED：真实 confirm 发布的 `DirectiveActivated` 因 scheduler sourcing
composition 没有注册 handler，被严格 Outbox 标为 dead，错误为 `no registered handler`。单元注册测试先
覆盖该事件；生产只增加具名、tenant-bound 的 `sourcing_case.directive_activated_audit` lifecycle
acknowledgement。它不领取 Admission、不启动 Run、不触发任何外部端口，也没有引入全局 no-handler
宽容。组合根测试进一步通过真实 `PostgresEventBus` 发布并核验该事件 delivered。

### 新受控 E2E 的测试装配边界

首轮新 E2E 让 fixture 后台 scheduler 与造数并发，因而读到 7/2/1 的中间快照。这是测试装配竞态，
不是生产排序错误。验收改为先正常停止 fixture worker：12 条前置 `Validated Need` 由测试拥有者在隔离
PostgreSQL 中通过 SQLAlchemy session 播种，并在同一事务发布真实 readiness Outbox；cluster 与 membership
事实再经真实 tenant-bound Demand repository/UoW 提交，最后用真实 advisory lock 执行一个完整 cycle。

这里没有发布 `NeedClusterFormed`：该既有事件只表示第二成员首次形成多成员簇，设计明确本准入路径不
消费；验收所需的排序变更事实是每条 `NeedClusterMembershipChanged`。前置 Need 行是明确限定在 fixture
内的 direct seed；cluster 事实经真实 repository/UoW 持久化，readiness/membership 事件经真实事务 Outbox
发布，后续 Case、snapshot、Admission、claim、Workflow start 与 replay 全部走生产组合。没有直接写入
Admission/Run，也没有跳过准入门禁。

### 全量门禁暴露的既有测试前提

0055 会正确拒绝两类旧测试装配：共享集成库中已有 boss-confirmed Directive 时做跨 0052 的有损
downgrade，以及不经 Admission 直接创建正常 Sourcing Case V2 Run。调查没有修改 0052/0055 生产约束：

- provider-readiness、search-quota 与 sourcing migration 的 roundtrip 改用每用例独立 PostgreSQL 数据库；
  migration head 断言更新为 0055，并补充 ORM/数据库 partial unique index parity。历史 migration/guard 专项
  仍保留刻意构造与拒绝语义。
- 公开计划确认与候选产品投影测试改为 canonical Need snapshot，并经真实
  `enqueue → claim → STARTING → engine.start → complete admission` 建立唯一 Run；重复 Run 继续由数据库
  拒绝，exact event replay 只返回 canonical Run。

Linux evidence parser 全链路还暴露 Docker Desktop 对子进程 `RLIMIT_CPU`/`RUSAGE_CHILDREN` 的计账抖动：
同一受控镜像、1 GiB、2 CPU、`network=none` 下，正常 PDF→EMAIL parse 在 4 秒预算的重复观测最大为
5.249 CPU 秒。只把**测试正常 parse profile** 的 CPU 上限设为 8 秒，与既有 8 秒 wall timeout 对齐；
CPU fault 仍显式为 2 秒，生产 parser、阈值和重试均未修改。精确用例和完整 Linux resource chain 随后
通过。

## 受控真实核心

`tests/e2e/test_sourcing_admission_controlled.py` 创建 12 条独立 Validated Need：一个 8-member 簇、一个
3-member 簇和一个 unclustered Need。实际断言如下：

1. readiness/membership 投递后等待队列精确为 `8 × 8、3 × 3、1 × 1`，稳定顺序为 8-member →
   3-member → unclustered；未配置策略时 Run 为 0。
2. 通过真实 HTTP 创建并确认 `mode=cluster_ranked`、`batch_limit=2` 的提案；确认 API 返回后 Run 仍为
   0，证明 `DirectiveActivated` acknowledgement 与实际 admission cycle 顺序分离。
3. 第一个真实锁定 cycle 精确启动 2 个 Run，两个都属于 8-member 簇，没有第三个 start。
4. 重放所有 readiness 后重建 scheduler runtime；下一轮只继续启动 2 个，累计 Case 仍为 12、Run 为
   4、Run subject 也为 4，没有重复 Case/Run。
5. 对 unclustered Admission 以同一个 `Idempotency-Key` 人工准入两次，只产生 1 个 Run；两次结果的
   `admitted_at` 相同。
6. 12 个 Case 的 Need snapshot、12 个当前 priority snapshot、Case/Need 绑定都分别唯一；人工操作前后
   snapshot ID、cluster facts、ready/facts observed 时间、ranking version 和解释文本不变。
7. tenant 内 dead Outbox 精确为 0，durable ToolCall 精确为 0。

新 E2E 单独复跑结果为 `1 passed in 7.35s`；旧 E2E、新 E2E、composition 与 scheduler runtime 目标组
结果为 `52 passed in 22.51s`。

## 外部调用边界

本验收止于 Workflow start，以下计数均在受控栈中显式断言：

| 能力 | 状态 | 调用数 |
| --- | --- | ---: |
| Tavily usage/search | `not_run` | 0 / 0 |
| public page validate/fetch | `not_run` | 0 / 0 |
| extraction model | `not_run` | 0 |
| 真实网络 | `not_run` | 0 |
| contact discovery/verification | 未装配且 `not_run` | 0 |
| email/send | 未装配且 `not_run` | 0 |
| procurement / supplier contact | 未装配且 `not_run` | 0 |
| direct supplier quote / customer Quote | 未装配且 `not_run` | 0 |

没有读取或打印任何 secret value，也没有把“配置存在”当成真实联网授权。

## Browser 受控验收

Browser 使用真实受控 FastAPI/Vite/PostgreSQL/scheduler 栈，先从 `/commands` 通过 UI 创建、检查并确认
batch 2 提案，再由受控 harness 执行一个真实锁定 scheduler cycle。桌面和 390px 均基于非空真实数据，
没有 mocked frontend response。

| 场景 | 实际证据 |
| --- | --- |
| 提案与确认 | 提案卡展示老板原话、系统解释、预期行为和确认按钮；确认后才在下一 cycle 启动。 |
| 等待 / 处理中 | `/sourcing` 同时展示 waiting 与 in-progress；首轮处理中精确为 2 条且都是 8-member 簇。 |
| 排序解释 | 队列和详情显示“需求簇含 8 条已验证需求”等解释，不宣称合并订单或已有供应。 |
| 不可变详情 | 详情显示独立 Case/Need、snapshot、事实观测时间、ranking/Directive version 与成员数。 |
| 人工重放 | 同 key 两次 HTTP replay 返回同一 Run、同一 `admitted_at`、同一 actor；页面只有一个目标记录。 |
| 响应式 | 桌面 `innerWidth=scrollWidth=1280`；移动 `innerWidth=scrollWidth=390`。 |
| 浏览器错误 | 所有批准截图所在的新 context console error 为 0。 |

第一次页面 transition 曾显示一次局部“无法连接准入服务”，但同一时刻四个 API 均为 HTTP 200，server
log 无异常。随后用三个 fresh context 重复真实 `/commands` → 点击 `/sourcing`，三次均能看到 waiting 与
in-progress、`loadError=false`、console error 0。因此它只记录为一次性观察，不把未复现现象冒充产品
缺陷；批准截图全部取自 network/console clean 的新 context。

批准证据仅有下列非 AppleDouble 文件：

- `output/playwright/t11-need-cluster-admission/01-policy-proposal-before-confirm.jpg`
- `output/playwright/t11-need-cluster-admission/02-desktop-queue-exact-two.jpg`
- `output/playwright/t11-need-cluster-admission/03-immutable-snapshot-detail.jpg`
- `output/playwright/t11-need-cluster-admission/04-manual-admission-replay.jpg`
- `output/playwright/t11-need-cluster-admission/05-mobile-390-queue.jpg`

## 完整门禁

| 命令 | 实际结果 |
| --- | --- |
| `ruff check .` | PASS，`All checks passed!` |
| `mypy domains shared tool_gateway apps workflows notification_gateway infra` | PASS，`Success: no issues found in 496 source files` |
| `python3 scripts/check_boundaries.py` | PASS，7 项结构检查全部通过 |
| `python3 scripts/scan_sensitive.py` | PASS，退出码 0 |
| `pytest tests/integration -x -q -rs` | PASS，`1578 passed in 1252.42s`，零 failure/error/skip |
| `pytest -q -rs` | PASS，`8274 passed in 1546.44s`，零 failure/error/skip |
| `npm run gen:api` + OpenAPI byte diff | PASS，生成成功；`git diff --exit-code -- apps/web/src/api/api.d.ts` 为 0 |
| `npm test` | PASS，25 files、308 tests |
| `npm run typecheck` | PASS，退出码 0 |
| `npm run lint` | PASS，退出码 0；仓库既有 172 warnings，0 errors |
| `npm run build` | PASS，142 modules transformed |

## 部署、保留物与限制

```yaml
push: not_run
merge: not_run
deploy: not_run
production_activation: not_run
real_external_calls: 0
```

- 本结论只在本地 feature worktree 成立；后续合并到本地 `main` 仍需用户明确确认，push/deploy 需要
  另行授权。
- 既有未跟踪 `output/playwright/t10-99ac25716c954ee8b489bffcb72e9436/` 原样保留且不提交。
- 挂载盘会生成 ignored `._*.png` AppleDouble 副产物；它们不是批准证据，不暂存。
- Git 读取仍会报告既有 `._pack-*.idx` non-monotonic warning；本任务没有删除、修改、repack 或修复
  `.git/objects`。
- lint warning 只能按仓库既有规则如实记录，不能降低 lint/scanner/mypy/boundary 门禁或隐藏 skip。
