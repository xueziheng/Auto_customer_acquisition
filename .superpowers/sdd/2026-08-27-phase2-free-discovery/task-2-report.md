# Task 2 实现报告

## 范围与结果

已实现受信部署绑定的 Tavily 免费账户额度、Gateway run-bound 注入和 scheduler 显式供应商组合。
遵循 TDD 技能先写失败测试；使用 verification-before-completion 技能重新执行验收后再提交。
未修改 `pipeline.py`、Gateway 核心错误枚举、业务 workflow 或联系人依赖，未派发子代理。
根代理负责独立审查；本报告不表示整批 Phase 2 已完成。

## 实现与公共接口

- `PostgresSearchQuotaRepository(factory, tenant_id, now=...)` 使用 tenant-bound 基座；账户行锁序列化跨 Run 预留。
- 单数据库唯一 `provider=tavily` 账户槽；全局唯一约束拒绝跨租户重复绑定，所有业务查询仍带 tenant 过滤，不读取其他 owner。
- 不接受请求中的账户别名、key ref 或账户 ID。API 没有 Provider account ID，因此没有伪造它。
- 本地记录 `reserved → uncertain → consumed`。`uncertain` 在 dispatch 前提交，取消、timeout、429、崩溃或完成写入失败没有退款/自动重试路径。
- 相同 Run/请求 HMAC 不重发；同 Run 存在任何 reserved/uncertain 时其他查询也阻止；其他 Run 可使用剩余额度。
- `SearchQuotaRepository.snapshot()` 提供安全账户摘要；`run_state(run_id)` 提供固定停止原因并优先从遗留 reserved/uncertain 推导 `request_uncertain`。
- `FreeSearchStopReason` 包含 quota_exhausted、usage_unknown、paid_enabled、request_uncertain、unsupported；`FreeSearchError.reason` 和 `is_retryable=False` 是下游入口。
- `RunBoundWebSearcherFactory.for_run(tenant_id, run_id, request_key)` 在 prepare 只创建不可变绑定；凭证、usage 和预留只发生在全部 Gateway 检查后的 execute 中。未引入全局 mutable current_run。
- 免费组合复用现有结果槽、页面来源检查、每 Run Tool ledger 预算与全部原检查。只对免费搜索注册 `web.search/v1.free`、cost_class=free；旧 Brave 保持原路径和成本类别。
- 生产 runtime 既有 `DemandDiscoveryComposition.web_tools` 已消费 `WebDiscoveryToolComposition`；后者新增显式 `provider="tavily"` 和 `exclusive_account_confirmed=True`，无需改 runtime 流程。配置不完整直接拒绝、不回退 Brave。

## 安全余量算法及明确限制

只有精确 Researcher、cost_status=free、paygo_enabled=False 且 limit/used 完整才可预留。
有效快照余量为 `max(0, limit-used)`；账户 ceiling 取历次有效余量的最小值；剩余安全下界为 `max(0, ceiling-累计本地预留)`。
由于 usage 没有账期与一致性水位，本地已消费可能已计入 Provider used，保守公式可能重复扣除；这是安全下界而不是精确余额。
不因本机月份、Provider used 下降、旧快照或进程重启增加额度。

单库单账户并不支持多个免费账户。`exclusive_account_confirmed` 是受信部署者声明，不是 API 验证事实。
不同数据库/外部程序共用真实账户无法由本库约束阻止，账户设置也可能在 usage 后被外部修改；必须独占账户。
同一在途 Run 的 HMAC key 配置应保持稳定，不能通过 key 轮换重放已成功查询。
本任务没有人工对账/释放预留/安全补额入口，也没有自动月度补充。不得以清表、重建绑定或另建数据库代替对账。
无真实密钥、无真实 Tavily/联系人/邮件调用，无 push/deploy，无生产数据库迁移；真实 Provider 验证仍为 not_run。

## RED 记录

命令均使用工作树：`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery`，
Python 为 `/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3`，显式设置同路径 PYTHONPATH。

1. `python3 -m pytest tests/integration/test_search_quota.py -q --tb=short`
   - 初始结果：11 setup errors，固定失败信息「免费账户持久预留及 Gateway reader 尚未实现」，缺少模块而非数据库/网络错误。
   - 后续有真正行为 RED：并发绑定的一侧仅得 reconciliation_required、没有持久 quota_exhausted；定位为 PostgreSQL 同时约束 PK 和 provider unique 时只指定单一 ON CONFLICT 目标不能覆盖竞争。改为无目标 DO NOTHING 后，tenant scoped SELECT FOR UPDATE 继续强制 owner 和原子预算。
2. `python3 -m pytest tests/unit/test_free_search_quota.py -q --tb=line`
   - 修正测试 HMAC 构造参数后：8 failed，7 个「免费成本策略尚未实现」、1 个「handler 尚未提供 run-bound reader 注入」。
3. `python3 -m pytest tests/integration/test_search_quota.py -k 'composition or gateway_rejection or per_run_budget or unconfirmed' -q --tb=line`
   - 8 failed，固定失败「生产组合尚未提供显式 Tavily 免费选择」。
4. `python3 -m pytest tests/integration/test_search_quota.py::test_tavily_composition_passes_gateway_and_returns_typed_quota_reason -q --tb=short`
   - 1 failed：ledger cost_class 实际 medium、期待 free；增加免费供应商 manifest 后修复。
5. `python3 -m pytest tests/unit/test_free_search_quota.py -q --tb=short`
   - 1 failed, 9 passed：Gateway reconciliation_required 且无可读 Run 状态时，原 adapter 仍抛通用错误。已修复为稳定非自动重试的 FreeSearchError(request_uncertain)。

上述迭代还纠正了测试数据：HMAC 构造顺序、结果槽 ULID、政策版本前缀 cpp，以及既有缺预算抛 ValidationError 的真实契约。没有更改既有门禁来迁就测试。

## GREEN 与迁移验收

以下命令在工作树执行。`PYTHONPATH` 均显式为 `/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery`，
`python3` 均指 `/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3`。

```text
env -u TEST_DATABASE_URL PYTHONPATH=<上述工作树> <上述Python> -m pytest tests/unit tests/integration -k 'quota or gateway or web_search or scheduler or migrations or alembic or migration_head' -m 'not e2e' -q --tb=short
474 passed, 3569 deselected in 169.63s (0:02:49)

env -u TEST_DATABASE_URL PYTHONPATH=<上述工作树> <上述Python> -m pytest tests/unit/test_free_search_quota.py tests/integration/test_search_quota.py -q --tb=short
35 passed in 16.46s

PYTHONPATH=<上述工作树> <上述Python> -m ruff check .
All checks passed!

PYTHONPATH=<上述工作树> <上述Python> -m mypy tool_gateway connectors infra/db/search_quota.py apps/scheduler_worker
Success: no issues found in 95 source files

PYTHONPATH=<上述工作树> <上述Python> scripts/check_boundaries.py
✓ 分层与依赖方向
✓ 金额 float
✓ 置信度数值
✓ 事件注册
✓ 租户过滤
✓ AGENTS.md 覆盖
✓ 域结构完整
结构自检通过。
```

相关回归进程启动后只补充了独立的 key 引用轮换专项测试；它已包含在最终 35 项专项通过结果中。
未运行全库/前端/e2e：按分工由最终 Task 统一运行。

迁移使用新建 testcontainers Postgres，未读取真实 `.env`、未输出 DSN/secret。
`scripts/run_alembic.py upgrade head` 从空库建立 0039；`test_0039_roundtrip_schema_matches_orm` 实际执行
0039 → 0038 → head(0039)，确认三表降级消失、再升级恢复，并核对列集合、nullable、CHECK、unique 与 FK 和 ORM 一致。
最终相关回归额外包含现有 `test_migrations.py` 的完整迁移往返检查。

## 文件清单

- 新增 `tool_gateway/free_search_contracts.py`
- 新增 `tool_gateway/handlers/free_search.py`
- 修改 `tool_gateway/handlers/web_search.py`：唯一原 handler 修改为类型明确的 run-bound 插件入口
- 新增 `infra/db/search_quota.py`
- 修改 `infra/db/tables.py`
- 新增 `migrations/versions/0039_search_quota.py`
- 新增 `apps/scheduler_worker/free_web_discovery.py`
- 修改 `apps/scheduler_worker/web_discovery.py`
- 新增 `tests/unit/test_free_search_quota.py`
- 新增 `tests/integration/test_search_quota.py`
- 修改 `tests/integration/test_migrations.py`、`tests/unit/test_alembic_appledouble.py`、`tests/unit/test_work_intake_migration_head.py`：head=0039
- 新增 `docs/operations/free-search-quota.md`
- 本报告 `.superpowers/sdd/2026-08-27-phase2-free-discovery/task-2-report.md`

## 自审

核对：不改核心管线；无凭证持久化；无 request/query/Provider payload 落额度表；固定 provider 全球唯一但读取不越租户；无月度自动释放；检查失败零账户预留；无 default query/page 预算；原每 Run 预算继续执行；旧 Brave 不使用免费账户槽；page 仍按搜索结果来源受控；无新商业工具注册。
Doc 操作说明依据根规则撰写（仓库未存在 docs/AGENTS.md）；未增加平行 Agent 规则文件。

## Fix round 1：HMAC 轮换后的 consumed 恢复去重

FIX_BASE：`8a3905544b187e53b273a3a73ab5168241ab0b4f`。
独立审查的 P2 已复现：Provider 成功并提交 consumed 后、Gateway 完成账本/交付前退出，
重启时新 HMAC 版本产生不同 request_key，原版可以再次 dispatch。此前「运营保持 HMAC 不变」的说明不能作为保护，本轮由持久门禁取代。

按 receiving-code-review 与 TDD 技能，先验证并新增真实 Postgres + 完整 Gateway 回归，再修改实现。

### 修改

- `search_quota_runs.fingerprint_version` 保存首次 Run 的非秘密版本；不存密钥或密钥哈希。
- handler 将当前 fingerprint version 显式传给 run-bound factory；reader 在凭证/usage 前的 `check_available` 与账户锁内 `reserve` 两次核验。
- 首次创建使用 insert-on-conflict-do-nothing；已有版本不可被新版本覆盖。旧 NULL 和版本不匹配固定持久化 request_uncertain，零额外预留、零 usage/search。
- 不仅相同请求，同一旧 Run 的不同请求也会拒绝。新 Run 可采用新版本并继续使用账户剩余额度。
- 修复此路径暴露的插件/ledger 映射不一致：业务异常的 `is_retryable=False` 与技术分类 reconciliation_required 不能直接组合为 ledger 永久失败。
  reader 插件出口先还原通用 ToolGatewayError，让 Gateway 按既有合法技术状态完成 `failed_transient/reconciliation_required`；外层 adapter 再恢复 `FreeSearchError(request_uncertain, is_retryable=False)`。
  持久去重门禁继续拦截再次调用，技术状态不表示授权重新搜索；未修改 Gateway core 或核心错误枚举。
- 因 0039 仅在此隔离分支、从未部署，根代理明确允许本轮在原迁移补列；不是对已部署迁移的原地修改。
- 本轮文件：上述 gateway/infra 实现文件、tables.py、0039、两个专项测试文件、操作说明与本报告；未扩展到 Task 3/4。

### RED

环境与 Python 路径同上，全部 DB 测试显式 `env -u TEST_DATABASE_URL` 使用新 testcontainers 库。

```text
env -u TEST_DATABASE_URL PYTHONPATH=<上述工作树> <上述Python> -m pytest tests/integration/test_search_quota.py -k consumed_before_delivery_crash -q --tb=short
2 failed, 25 deselected in 6.04s
两个参数（factory / different query）均：Failed: DID NOT RAISE FreeSearchError
```

故障注入仅包裹真实 repository.consume：先执行并提交真实 consumed，再抛 CancelledError，阻断 Gateway 完成/交付。
初始请求已 dispatch，后续新 worker 使用 HMAC v2；回归断言不能再次 dispatch，不能再次 usage。

新增版本门禁后第一次 GREEN 尝试仍为 RED：

```text
env -u TEST_DATABASE_URL PYTHONPATH=<上述工作树> <上述Python> -m pytest tests/unit/test_free_search_quota.py tests/integration/test_search_quota.py -q --tb=short
2 failed, 35 passed in 10.13s
shared.errors.ValidationError: 永久失败分类无效
```

按根代理确认，在插件边界修正合法 Gateway 技术状态映射；增加 ledger 终态、持久停止原因、非自动重试标志、再次调用零 dispatch、旧版本不变、新 Run 可使用 v2、NULL 失败关闭，以及 reserve 锁内二次核验断言。

### GREEN

初步专项：`39 passed in 9.86s`。最终含所有新增断言及迁移/handler 回归：

```text
env -u TEST_DATABASE_URL PYTHONPATH=<上述工作树> <上述Python> -m pytest tests/unit/test_free_search_quota.py tests/integration/test_search_quota.py tests/integration/test_migrations.py tests/unit/test_country_policy_web_gateway.py tests/unit/test_tool_gateway_pipeline.py tests/integration/test_tool_gateway_pipeline.py tests/unit/test_work_intake_migration_head.py tests/unit/test_alembic_appledouble.py tests/unit/test_web_search_discovery.py -q --tb=short
151 passed in 62.35s (0:01:02)

PYTHONPATH=<上述工作树> <上述Python> -m ruff check .
All checks passed!

PYTHONPATH=<上述工作树> <上述Python> -m mypy tool_gateway connectors infra/db/search_quota.py apps/scheduler_worker
Success: no issues found in 95 source files

PYTHONPATH=<上述工作树> <上述Python> scripts/check_boundaries.py
✓ 分层与依赖方向
✓ 金额 float
✓ 置信度数值
✓ 事件注册
✓ 租户过滤
✓ AGENTS.md 覆盖
✓ 域结构完整
结构自检通过。

git diff --check
exit 0（无输出）
```

实际迁移：新建隔离 Postgres 从空库 upgrade head(0039)，0039→0038→0039 往返及 ORM schema 对比均通过，包含新增 nullable fingerprint_version 列；没有升级生产库。
自审结论：旧 Run 不会因 HMAC 版本变化得到新绑定，consumer/ledger 缝隙不再产生二次 dispatch；只保留原任务的单账户、保守额度及无对账恢复入口限制，无新增扩大范围。此轮未启动子代理。
