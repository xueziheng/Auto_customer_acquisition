# Task 3a 实施报告

日期：2026-09-05。已提交，等待独立审查；没有勾选总体 Task 3，也未实施 Task 3b。

- 工作树：`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion`
- 分支：`codex/web-core-completion`
- base：`2fb1de8e7b237c1c8d8575a5363ca6eb8e9f210a`
- head：`7c640494ab0d12c3883c8beb451bde2d97f35109`
- 提交：`fix(runtime): 注入模型端口并绑定资源与单例锁健康生命周期`
- 正式子规格：`docs/superpowers/specs/2026-09-05-web-core-runtime-contract.md`

## 实现与范围

1. 保留零参数生产 API wrapper，新增显式 typed settings/端口入口
   `create_runtime_app_from_settings`；原 `build_phase1_dependencies` 支持显式
   StructuredJsonModelClient，继续通过原 StructuredTradeManagerModelPort 调用。
   未注入保持生产 OpenAI 默认，显式注入不构造该 Provider；不存在受控模式自动回退。
2. ConfiguredApiDependencies 提供明确 `model_lifecycle` 与 `object_store_lifecycle`。
   默认模型/旧对象传输是 factory-owned；注入模型 caller-owned，API 不关闭共享对象。
   直接调用 build_phase1_dependencies 的调用者负责关闭返回的 owned lifecycle 和 quotation，
   再关闭自己的 engine。同步构造无 DB 连接、SDK client、parser 子进程；构造失败的零 IO
   测试使用真实依赖装配后故障，不为未连接 engine 创建额外事件循环。
3. 生命周期按 quotation → owned model → owned object → engine 逐一清理；失败不跳过后续
   清理。有主异常保留原对象；无主异常的普通释放失败抛固定 RuntimeCleanupError；取消
   保留。补 OpenAI、旧 S3 和 deferred S3 的最小 aclose：未使用不初始化，成功幂等，
   失败保留引用可重试。S3 复用既有线程取消收口，未增加 Provider 行为。
4. 原 SchedulerRuntime 增加窄 lifecycle observer，原 run_scheduler_worker 驱动状态。
   config/schema/database/registry 完成仍 not_ready；真正获单例锁、activation 完成且再次
   确认原 backend 后、首周期前才 running-ready。stop_event 一旦设置立即 not_ready，
   当前周期继续完成；未获锁/失锁/取消/退出均撤销 ready。保留 live 和原退出码/driver顺序。
   observer 错误固定脱敏，并不能阻止解锁或 factory 清理。health server 保留两参数兼容，
   支持显式 `host="127.0.0.1"`；测试真实监听 loopback。
5. 自审发现并通过真实 PG 复现旧缺陷：获取查询已执行但结果未知，或取得锁后首次 commit
   被取消，原连接回池仍保有 session lock。现将该原物理连接 detach 后 invalidate；失锁与
   未确认解锁也丢弃原连接，不重连后冒称解锁。解锁取消不覆盖已存在 activation 主异常。
   锁释放测试先查 pg_locks 为零，避免同一 pooled backend 重入得到假阳性。

没有 service dict、第二连接池、锁轮询器、进程互导或新后台服务；可选业务组继续 disabled。
完整 readers、业务依赖拓扑、requested-enabled 完整性、bootstrap 属于 Task 3b。

## RED 与过程中校正

所有 pytest 命令在本工作树以 `.venv/bin/python` 执行，前缀统一为：
`env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1`。使用本次独占 testcontainers
`pgvector/pgvector:pg16` 和现有迁移 fixture，未读取 .env 或现有数据库连接。

1. `-m pytest tests/integration/test_web_core_runtime.py -q --tb=short`
   首轮：**8 failed**。预期 RED：旧 composed health 返回 200，缺 typed factory 和 lifecycle。
2. `-m pytest tests/unit/test_api_owned_clients.py -q --tb=short`
   首轮：**3 failed**。预期 RED：OpenAI/deferred S3 没有 aclose。
3. 初步 GREEN 检查中 S3 测试传入非 canonical object key 被既有验证拒绝，修正为合法测试 key；
   后续 API/scheduler 相关回归 **121 passed**。新增资源和故障覆盖后聚焦 **28 passed**。
4. 扩大相关集曾为 **1 failed, 147 passed**；新增 registry 用例的 tenant 不是合法 ULID，
   属于测试夹具错误。未改生产配置校验，改成明确合法测试 tenant 后 registry 用例通过。
5. 获取锁取消窗口首次单例 RED：**1 failed, 1 passed**（后者是 registry 用例）；扩成
   `commit/query_result` 参数后 **2 failed**，均直接观察 pg_locks 残留 1。
6. 丢弃原连接后，聚焦集 **25 passed**。随后
   `test_unlock_cancellation_discards_original_connection_and_preserves_primary` RED **1 failed**：
   CancelledError 覆盖 activation RuntimeError。修复后纳入最终 GREEN。

## 最终 GREEN 与检查

相关完整测试命令：

```bash
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest \
  tests/unit/test_api_owned_clients.py \
  tests/unit/test_api_runtime.py \
  tests/unit/test_scheduler_worker_config.py \
  tests/unit/test_scheduler_quotation_activation.py \
  tests/integration/test_api_runtime.py \
  tests/integration/test_scheduler_worker.py \
  tests/integration/test_web_core_runtime.py \
  tests/integration/test_quote_runtime.py -q --tb=short
```

结果：**151 passed in 29.99s，exit 0，无 skip**。

类型检查随后指出 SQLAlchemy sync_connection 在声明中允许 None。补显式判空（不改正常
运行行为）后重跑真实 PG 新聚焦集：

```bash
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest \
  tests/integration/test_web_core_runtime.py -q --tb=short
```

最终结果：**26 passed in 6.98s，exit 0**。

最终静态检查（均 exit 0）：

```bash
.venv/bin/python -m ruff check \
  apps/api/runtime.py apps/api/dependencies.py apps/api/composition/runtime.py \
  apps/scheduler_worker/main.py apps/scheduler_worker/runtime.py \
  connectors/openai/client.py connectors/object_store/deferred.py connectors/object_store/s3.py \
  tests/integration/test_web_core_runtime.py tests/integration/test_scheduler_worker.py \
  tests/unit/test_api_owned_clients.py --output-format concise

.venv/bin/python -m mypy \
  apps/api/runtime.py apps/api/dependencies.py apps/api/composition/runtime.py \
  apps/scheduler_worker/main.py apps/scheduler_worker/runtime.py \
  connectors/openai/client.py connectors/object_store/deferred.py connectors/object_store/s3.py \
  --follow-imports=silent

.venv/bin/python scripts/check_boundaries.py
git diff --check
git diff --cached --check
```

Ruff：All checks passed。Mypy：8 个源文件无问题（只声明本次聚焦门，不声称全库类型通过）。
结构自检七项通过。git 均以 Python subprocess capture，仅显示 stdout/exit，未修复共享 .git、
未删除 AppleDouble 文件。提交后 `git status --short` 为空。

## 有界自审与限制

- 核对 typed 入口无环境读取；注入模型仍走真实 Trade Manager JSON 合约，非法受控输出被原
  校验拒绝，调用一次且 API 不关借用 client。默认模型与 S3 SDK 使用受控边界，真实工厂与
  PostgreSQL 都保留；对象/模型释放顺序可观察。
- 实际 schema downgrade/未知 head/多 head 拒绝在本次 API 集成回归通过；缺 worker handler
  的真实 factory 测试在健康服务启动前拒绝，并 dispose 已打开 engine。
- 覆盖 activation 前后、第二 singleton 零 cycle、stop flag、同 backend 被终止、取消、
  observer 三阶段故障、获取结果未知、原锁连接丢弃、主异常与清理取消优先级。
- 这里只修运行契约与资源归属，未做业务状态整链或 Browser 验收。受控 wrapper 后续必须
  显式注入模型并选择 loopback；关闭自有 client 前应由宿主结束/排空请求，未设计并发关闭
  正在运行请求的新业务语义。
- 没有真实模型、客户发送、供应商联系、Tavily 或生产数据库操作。未部署、推送、合并；
  测试成功不是生产运行或总体 Task 3/整个 Web 核心完成证据。

## Fix 1：独立审查 P1 / P2（2026-09-05）

本节更正前文关于 detach + invalidate 能确定关闭原物理连接的结论；该旧结论不成立。
原来的 PG 测试没有保留 driver 强引用，不能排除对象回收导致的关闭。审查文件保持原样。

- fix base：`7c640494ab0d12c3883c8beb451bde2d97f35109`
- fix head：`89d74bd2b4a1ebf4583dbc40ec08bf86f4b2e432`
- 仅修改 `apps/scheduler_worker/main.py`、新运行契约集成测试、正式子规格三个文件。
- 提交：`fix(scheduler): 确定性关闭原锁连接并处理未确认解锁`

### 修复语义

在原连接完成 checkout 后、任何锁查询之前保留原 asyncpg driver 强引用；获取结果未知、
取消、失锁、解锁错误及 `released=False` 都传递该同一 handle 给清理路径。先直接执行
原 driver 的 `close(timeout=2)`；仍未关闭则对同一 driver 执行 `terminate()`；然后才
invalidate SQLAlchemy 包装器，最后仍按保留的原 driver 核实 `is_closed()`。不再 detach，
不通过 cleanup 查询或重新获取 connection 来认定原锁已释放。

原连接释放仍不能确认时，无主异常抛固定 `SchedulerLockCleanupError`，有主异常保留原
异常对象；普通 unlock SQL 错误在已确认物理关闭后允许清理恢复。无主异常时 close/invalidate
发生的取消在完成物理关闭后仍向上传播，不能变成 STARTED 成功；已有主异常时不覆盖它。

### RED / GREEN

全部使用本工作树 `.venv/bin/python` 与本次自有 testcontainers PostgreSQL；统一前缀：
`env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1`。没有读取 .env / 现有数据库配置，
没有 Provider 或生产数据库操作。

1. 新增 `test_unknown_lock_cleanup_closes_retained_original_driver`，在 acquisition result
   unknown、unlock cancellation、unlock false 三条路径保留原 driver，直至不同连接查询
   pg_locks。旧代码 **3 failed in 5.44s**，三者均明确 `original.is_closed() == False`。
   测试 finally 使用保留的原 handle 终止本次测试连接，防止 RED 遗留锁污染后续测试。
2. 实现修复后，强引用与既有获取/解锁取消组合 **6 passed, 23 deselected in 4.85s**。
3. 再增加原 driver close 普通错误/取消后 terminate 同一 driver，以及 close/terminate/
   invalidate 全部故障时的固定失败与主异常保留；聚焦 **7 passed, 26 deselected in 4.81s**。
4. 补普通 unlock SQL 错误经确认关闭后恢复的行为，最终仅跑约定的锁/runtime 回归：

```bash
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest \
  tests/integration/test_web_core_runtime.py \
  tests/integration/test_scheduler_worker.py \
  tests/unit/test_scheduler_quotation_activation.py -q --tb=short
```

最终：**93 passed in 7.97s，exit 0，无 skip**。没有重复运行此前完整 151 项套件。

取消语义的具名证据：
`test_original_driver_close_failure_terminates_same_retained_driver[cancel]` 在原 driver close
取消后，确认同一个 retained driver 已 closed、pg_locks 为零，并仍观察到 CancelledError；
`test_unknown_original_driver_cleanup_cannot_return_success_or_replace_primary[True]` 保留原
activation 异常，False 参数断言固定失败；普通 unlock SQL 错误由强引用用例 `unlock_error`
参数证明物理关闭后仍可返回原 STARTED 结果。

### 最终检查与自审

```bash
.venv/bin/python -m ruff check apps/scheduler_worker/main.py \
  tests/integration/test_web_core_runtime.py --output-format concise
.venv/bin/python -m mypy apps/scheduler_worker/main.py --follow-imports=silent
.venv/bin/python scripts/check_boundaries.py
git diff --check
git diff --cached --check
```

均 exit 0：Ruff 全通过；Mypy 1 个变更源文件无问题；结构七项通过；diff 无空白错误。
Git 继续由 Python subprocess capture，仅显示 stdout/exit，未修复共享 .git。提交后工作树干净。

有界自审核对：锁查询前保留 handle，关闭/终止始终使用同一 handle；没有 detach、cleanup
取新连接或第二次解锁查询；unlock false 进入同一确定清理路径；未确认清理不返回成功；
已确认关闭不吞掉外部取消；既有主异常不被清理错误替换。仅处理 P1/P2 及直接相关取消语义，
未改 observer/driver 次序、API 资源、业务组或 Task 3b。等待 scoped 独立复审。
