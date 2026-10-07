# Task 3a 审查

## Spec Compliance

- ❌ Issues found：API typed 注入、SDK 归属与 worker 健康观察符合本阶段范围；原物理锁连接的确定性释放未完成，且 `released is not True` 路径漏掉规格要求的丢弃。证据：`apps/scheduler_worker/main.py:385`、`:389`、`:564`；对应正式子规格 `docs/superpowers/specs/2026-09-05-web-core-runtime-contract.md:27`。
- ⚠️ 本审查不判定总体 Task 3 完成。完整业务 readers/composition/bootstrap 明确属于 Task 3b；本 diff 没有这些变更不构成 3a 缺项。

## Strengths

- `apps/api/runtime.py:99` 保留生产 wrapper 并提供显式 typed 构造入口；`apps/api/composition/runtime.py:1286` 与 `:1626` 将默认模型设为 factory-owned、注入模型设为 caller-owned。`tests/integration/test_web_core_runtime.py:218` 实际经过原 Trade Manager 输出校验，并断言注入对象不被关闭。
- `apps/api/runtime.py:141` 的清理链依次尝试各 owned resource 与 engine；`:170` 在没有主异常时将普通清理失败转为固定错误。`tests/integration/test_web_core_runtime.py:238` 覆盖继续清理和主异常对象保留，`:401` 覆盖真正惰性创建的受控 SDK 被关闭。
- `apps/scheduler_worker/main.py:525` 在原锁检查与 activation 之后发布 running；`apps/scheduler_worker/runtime.py:385` 直接读取原 stop flag，使优雅停止不必取消在途 cycle。`tests/integration/test_web_core_runtime.py:295` 覆盖 observer 故障不能阻止解锁，`:90` 先查 `pg_locks` 再尝试取得锁，避免单纯同连接重入造成假阳性。

## Issues

### Critical

- 无。

### Important

1. **[P1] detach 后 invalidate 丢失了关闭原物理连接的路径。** `apps/scheduler_worker/main.py:385` 先 `sync_connection.detach()`，`:389` 再 `await connection.invalidate()`。当前安装的 SQLAlchemy 中，`_ConnectionFairy.detach()` 将 `_connection_record` 设为 `None`；随后 `_ConnectionFairy.invalidate()` 只有在 record 存在时才调用 record 的实际关闭逻辑，之后直接将 `dbapi_connection` 设为 `None`。因此 `_checkin()` / `_finalize_fairy()` 及外层 `connection.close()` 已没有底层连接可关闭。这个 helper 不能证明 session advisory lock 已释放；它被获取结果未知、获取后首次 commit 取消、解锁失败/取消和失锁路径共用，问题落在本任务的核心可靠性要求上。

   精确依赖证据：本解释器加载的 `/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-catalog-product-proposal/.venv/lib/python3.12/site-packages/sqlalchemy/pool/base.py:1499`（`detach`）、`:1476`（`invalidate`）、`:1393`（`_checkin`）、`:917`（`_finalize_fairy`），以及同环境 `sqlalchemy/engine/base.py:693`（`Connection.invalidate`）。这是检查当前共享虚拟环境的已安装库源码，没有改动该工作树。

   零网络、零文件数据库的聚焦验证实际执行通过：用 `create_engine('sqlite:///:memory:')` 建立 SQLAlchemy connection，保留 `physical = connection.connection.driver_connection`，依次 `detach()`、`invalidate()`、`close()` 后，原 `physical.execute('SELECT 1')` 仍成功；输出 `original_physical_connection_still_open=True`，最后手工关闭 physical 和 engine。这证明当前库的包装器清理序列不等于物理关闭；没有宣称 SQLite 验证了 PostgreSQL 锁语义。现有 PG 通过的可能解释是 asyncpg 对象引用被回收后关闭连接，但这属于推测，不能作为确定性释放契约。`:612`、`:652` 的新 PG 用例未刻意保留原 driver 强引用，尚未排除这一解释。

   修复应在丢弃包装器引用之前保留原物理 handle，并显式完成该 handle 的 close/terminate（遵循实际驱动与 SQLAlchemy 异步上下文要求）；若需要 detach 防回池，应在 detach 后先完成原 handle 的物理关闭，再清理 wrapper。普通 close 失败或被取消时，应对原 driver 执行可靠的 terminate 兜底；绝不通过可能重新取 backend 的 `execute()` 或重连取得另一 handle 来冒充释放。无法确认关闭时保留主异常；没有主异常则固定失败退出，不能仅日志后返回成功。补一个保留原 driver 强引用直到另一个 backend 检查 `pg_locks` 为零的聚焦回归，覆盖 acquisition 未知与 unlock 取消即可，不需全 factory 回归。

2. **[P2] 解锁返回非 True 时只记录日志，没有进入要求的原连接丢弃路径。** `apps/scheduler_worker/main.py:564` 的分支在日志后结束，连接仍沿普通 context-manager close 返回池，先前准备的 `STARTED` result 也可正常返回。这与子规格中“失锁或解锁未确认时……丢弃原物理连接”的约定不符；实现报告对该路径已丢弃的陈述也不准确。应在该分支调用修复后的原物理连接关闭逻辑，并补针对 `released=False` 的断言。不能仅为了返回 false 就新增第二次取锁或在其他 backend 上解锁。

   退出语义要区分清楚：普通 unlock SQL 异常后，如果已经确定关闭/终止了原物理连接，session lock 随之消失，属于已恢复的清理，不必仅因最初 SQL 异常强制非零；如果原连接释放仍不能确认，就不能成功返回。当前 helper 的实际关闭缺陷及其吞掉所有关闭错误的行为使后一种情况被掩盖，修复时一并让结果可判定。

### Minor

- 无。

## Checks / 审查边界

- 一次顺序读完 `review-2fb1de8..7c64049.diff` 的所有变更；随后仅程序化提取匹配位置的目标行号。没有重跑 git、没有重复读取完整 changed files。
- 为判断被 hunk 截断的锁检查与最终退出链，仅补读 `apps/scheduler_worker/main.py:359` 的 `_same_lock_backend` 和 `apps/scheduler_worker/runtime.py:1674` 的 factory finally。原 driver 顺序无 diff 变更，未拓宽爬取业务实现。
- 具名外部风险仅为 SQLAlchemy pool/session lock 所有权与未知获取结果清理；检查了上述已安装库的 detach/invalidate/finalize/reconnect/commit 与 asyncpg terminate 实现，并执行一次内存 SQLite 聚焦验证。未运行任何 PG/provider/network 操作，未读取凭证、环境或连接配置。
- SDK caller-owned/factory-owned、observer error/cancellation 与原 driver 顺序均依据完整 diff 核对；没有运行测试套件。实施报告的 151 项相关测试、末次类型 guard 后 26 项 PG 测试以及 ruff/mypy/boundaries 通过属于已有报告证据，本审查未重新生成；AppleDouble git stderr 为已知环境问题，不记为本任务回归。

## Assessment

**Task quality：Needs fixes。** API 资源归属与健康观察接口可保留；必须先把原物理锁连接的确定性关闭修正，并覆盖未确认 unlock 分支，才能通过 Task 3a。不得因此标记总体 Task 3 完成。

## Fix 1 定向复审（2026-09-05）

- **Spec Compliance：✅ Spec compliant。** 本次范围仅前次 P1/P2 及直接引入的风险，审查 base `7c640494ab0d12c3883c8beb451bde2d97f35109` → head `89d74bd2b4a1ebf4583dbc40ec08bf86f4b2e432`。本节取代上述两项未修复状态，不改变 Task 3b 与总体 Task 3 的边界。
- **原 P1：ADDRESSED。** `apps/scheduler_worker/main.py:507` 在首次锁查询前保存原 driver 强引用；`:405` 对该 handle 执行有界 close，`:412` 必要时 terminate 同一 handle，`:418` 才 invalidate 包装器。没有 detach→invalidate 的引用丢失序列，也没有从清理 helper 重新获取 backend。`:423` 最终核验原 driver，无法确认且没有主异常时 `:426` 固定失败。`tests/integration/test_web_core_runtime.py:687` 的 acquisition result unknown / unlock cancel / unlock false / unlock error 参数保留原 driver 强引用，断言其 closed 后再检查 `pg_locks`；补上了前次证据缺口。
- **原 P2：ADDRESSED。** `apps/scheduler_worker/main.py:613` 的非 True 解锁结果进入同一确定性清理路径。普通 unlock 错误在 `:595` 调用清理后，仅在原 driver 已确认关闭时允许恢复为原结果；未知关闭结果由 helper 失败退出。`tests/integration/test_web_core_runtime.py:687` 覆盖 false/error，`:791` 覆盖关闭、终止、invalidate 全失败时不能成功返回，以及已有 activation 主异常对象不被替换。
- **直接引入的取消/异常风险：未发现新增问题。** `apps/scheduler_worker/main.py:428` 在关闭已确认、无既有主异常时传播清理取消；`:598` 将已有 activation 异常或原 unlock 取消作为 primary 传入，避免关闭阶段的异常覆盖它。`tests/integration/test_web_core_runtime.py:747` 在 close 错误/取消后验证 terminate 的正是原 driver，取消分支仍抛 `CancelledError`。普通 SQL 解锁错误经确定物理关闭后恢复，与释放未知时失败的语义已分开。
- **Strengths：** `task-3a-report.md:131` 明确纠正旧 detach/invalidate 结论；新增强引用 PG 测试验证资源行为而非只验证 helper 被调用。原 observer、driver 次序及 API 资源链均不在本次修复 diff 内。
- **Critical / Important / Minor：** 本次定向复审无新增问题，前次两项均已关闭。
- **检查：** 顺序完整读一次 `review-7c64049..89d74bd.diff`，仅程序化提取目标行号；读取实施报告 Fix 1 的更正、3 项强引用 PG RED 和最终 93 项相关测试通过证据。没有读取 changed files、重跑 git/测试套件、拓宽代码搜索或访问网络/数据库/环境/凭证；已有测试已回答本次具体风险，无需追加运行。
- **Task quality：Approved。** Task 3a 可通过；此结论不是总体 Task 3 完成证明。
