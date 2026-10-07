# Prospecting Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 按 `docs/superpowers/specs/2026-08-20-prospecting-persistence-design.md`
交付企业、联系人、联系方式、法律依据、验证门禁和删除抑制的真实 PostgreSQL 闭环，
为后续 account discovery 与 Demand Radar 提供可信公共接口。

**Architecture:** `domains/prospecting` 保存纯域规则、公共 DTO、Protocol 与浅域服务；
`infra/db` 保存 SQLAlchemy Row、tenant-bound repository 与 UoW；迁移 0023 建五张表；
既有 `PostgresEventBus` 与业务 session 同事务写 `ContactPointVerified`。本计划不接外部
Provider、不自动入组 Campaign、不改 UI。

**Tech Stack:** Python 3.12、SQLAlchemy 2.x async、PostgreSQL、Alembic、pytest；
全部命令使用 `/Users/xueziheng/miniconda3/envs/tradeos-py312/bin`，本地不使用 Docker。

## 全局执行规则

- 每项严格 RED → 最小 GREEN → 定向回归 → boundary/ruff/mypy → commit → push →
  exact-HEAD GitHub CI success；失败即停在当前项修复，禁止带红进入下一项。
- 编辑只用 `apply_patch`；提交前删除 AppleDouble，新增文件 mode 必须 `100644`。
- 每个查询显式 tenant predicate；跨租户调用抛 `TenantIsolationViolation`。
- 联系方式原值不得进入日志、异常、outbox、哈希抑制表或测试快照。
- outbox 只含既有 `ContactPointVerified` metadata；不得新增原值、法律依据或成本字段。
- 金额规则不适用于 `enrichment_cost_note`：它只是来源提供的非结构化成本备注，不能
  被当作金额或用于计算；未来真实成本必须使用 `Money/Decimal`。
- 每项提交后执行：

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
git push origin HEAD
head_sha=$(git rev-parse HEAD)
test "$head_sha" = "$(git rev-parse origin/codex/handbook-phase1-slice4-execution)"
run_id=""
for i in $(seq 1 12); do
  run_id=$(gh run list --commit "$head_sha" --workflow ci --limit 1 --json databaseId --jq '.[0].databaseId // empty')
  if [ -n "$run_id" ]; then break; fi
  sleep 5
done
test -n "$run_id"
gh run watch "$run_id" --exit-status
gh run view "$run_id" --json headSha,status,conclusion --jq 'if (.headSha == "'"$head_sha"'" and .status == "completed" and .conclusion == "success") then "OK" else error("exact-HEAD CI mismatch") end'
```

### Task 1：纯域门禁、公共 DTO 与服务契约

**Files:**

- Modify: `domains/prospecting/models.py`
- Modify: `domains/prospecting/schemas.py`
- Modify: `domains/prospecting/service.py`
- Modify: `domains/prospecting/repository.py`
- Modify: `domains/prospecting/errors.py`
- Create: `tests/unit/test_prospecting_models.py`
- Create: `tests/unit/test_prospecting_contracts.py`

- [ ] 先写单元测试：`may_enter_sequence` 仅 VERIFIED 为真；LI 缺 assessment、非 UTC
  collected_at、空白字段、非法 kind/result、内部 model 从 service 公共签名泄漏均失败。
- [ ] 运行 RED：
  `pytest -q tests/unit/test_prospecting_models.py tests/unit/test_prospecting_contracts.py`，
  失败必须来自现有 `NotImplementedError`/缺 DTO/旧签名。
- [ ] 实现 dataclass DTO、枚举复出口、固定安全错误、`ContactValueHasher` Protocol、
  `ProspectingUnitOfWork` Protocol；服务方法采用规格 §3 权威签名。
- [ ] 实现模型不变量和 `may_enter_sequence`；不在本项创建数据库或 service_impl。
- [ ] GREEN + 门禁：上述 pytest、`ruff check` 相关文件、`mypy domains/prospecting`、
  `python scripts/check_boundaries.py`。
- [ ] Commit：`feat(prospecting): define compliant domain contracts`，push 并等精确 CI。

### Task 2：迁移 0023 与 ORM 表契约

**Files:**

- Modify: `infra/db/tables.py`
- Create: `migrations/versions/0023_prospecting.py`
- Modify: `tests/integration/test_migrations.py`

- [ ] 写迁移 RED：head 必须 0023；五张新表存在；列、composite PK/FK、unique、
  partial index、枚举/nonblank/pair/hash CHECK、erasure update/delete trigger 与 ORM parity。
- [ ] 增加 upgrade → downgrade 0022 → upgrade roundtrip，验证 cascade 只从 contact point
  删除 legal basis，不越租户删除 contact/account。
- [ ] 运行 RED，仅允许因缺 0023/Row 失败。
- [ ] 实现五个 Row 与 0023；JSONB 使用 `none_as_null=True` 仅在可空 JSON 列需要时设置；
  所有 timestamptz 为 timezone aware；所有 composite FK 含 tenant。
- [ ] GREEN：定向 migration tests；再跑 `ruff check infra/db/tables.py
  migrations/versions/0023_prospecting.py tests/integration/test_migrations.py`、mypy、boundary。
- [ ] Commit：`feat(prospecting): add persistence schema`，push 并等精确 CI。

### Task 3：tenant-bound repositories 与 UoW

**Files:**

- Create: `infra/db/repositories/prospecting.py`
- Create: `infra/db/prospecting_uow.py`
- Create: `tests/integration/test_prospecting_repositories.py`

- [ ] 写 RED：account/contact/legal-basis roundtrip；按域名、名称、ID、账户查询；同值
  去重；跨租户不可见与 repo 参数越界；固定排序；UoW commit/rollback。
- [ ] repository 显式 `_require_tenant`；account `add -> bool` 用 partial-index conflict；
  contact point + legal basis 同 session 原子插入；row mapper 完整保留枚举与 UTC 时间。
- [ ] verification repository 用 `SELECT ... FOR UPDATE` 返回转换前快照；erasure/hash 方法
  在本项只实现 repository 原语，不做 service 业务编排。
- [ ] GREEN：定向集成测试、ruff、mypy、boundary。
- [ ] Commit：`feat(prospecting): add tenant repositories`，push 并等精确 CI。

### Task 4：企业消歧、联系人创建和联系方式录入服务

**Files:**

- Create: `domains/prospecting/service_impl.py`
- Modify: `infra/db/outbox.py`（只注册 `ContactPointVerified`，事件在 Task 5 使用）
- Create: `tests/unit/test_prospecting_service.py`
- Modify: `tests/integration/test_prospecting_repositories.py`

- [ ] 写 RED：纯 host/IDNA canonicalization；无域名不自动合并；同域名 20 路并发只一行；
  contact 必须属于当前 tenant；legal basis 全规则；邮箱/电话 canonicalization；相同语义
  幂等、不同 contact/依据/备注冲突；已删除 hash 拒绝。
- [ ] `ContactValueHasher` 只接收 canonical value，输出严格 64-lower-hex；服务验证输出
  shape，禁止把 key 或原值写入状态。测试使用固定 fake hasher，不提交真实 secret。
- [ ] resolve conflict 后重读 winner；同域名新增 source refs 做稳定去重并集，不覆盖首条
  企业事实。没有域名时每次创建新 ID。
- [ ] GREEN：unit + integration、ruff、mypy、boundary；敏感 marker 断言日志/异常/outbox
  均不含地址。
- [ ] Commit：`feat(prospecting): resolve accounts and contacts`，push 并等精确 CI。

### Task 5：验证状态机与原子事件

**Files:**

- Modify: `domains/prospecting/service_impl.py`
- Modify: `tests/unit/test_prospecting_service.py`
- Modify: `tests/integration/test_prospecting_repositories.py`
- Modify: `tests/integration/test_outbox_transaction.py`

- [ ] 写 RED：四种结果；verified_at pair；仅非 VERIFIED → VERIFIED 发布一次；重复与
  20 路并发只有一个 `ContactPointVerified`；事件不含 value/legal basis/cost；未知 ID
  和跨租户 fail closed。
- [ ] 注入 UTC `now` 并拒绝 naive/non-UTC；provider 非空；repository 行锁串行化转换。
- [ ] bus 故障测试：状态、verified_at、provider、outbox 全回滚；重试后恰一次成功。
- [ ] `list_verified_contact_points` 只通过 contact→account tenant-bound join 返回 VERIFIED，
  RISKY/UNVERIFIED/INVALID 永不出现。
- [ ] GREEN + ruff/mypy/boundary。
- [ ] Commit：`feat(prospecting): enforce verification gate`，push 并等精确 CI。

### Task 6：隐私删除与不可恢复抑制

**Files:**

- Modify: `domains/prospecting/service_impl.py`
- Modify: `tests/unit/test_prospecting_service.py`
- Modify: `tests/integration/test_prospecting_repositories.py`
- Modify: `tests/integration/test_migrations.py`

- [ ] 写 RED：存在/未知值删除、重复删除、并发删除、跨租户隔离、事务回滚、trigger
  拒绝 suppression UPDATE/DELETE、删除后重新添加拒绝。
- [ ] 删除同 hash 的 legal basis/contact point；仅当 contact 无剩余 point 时删除 contact；
  account 与既有 outreach suppression 保留；未知值返回 0 但创建 hash suppression。
- [ ] marker 测试扫描业务表、erasure 行、outbox、caplog 和异常文本，确认原值零泄漏。
- [ ] GREEN + ruff/mypy/boundary。
- [ ] Commit：`feat(prospecting): honor contact erasure`，push 并等精确 CI。

### Task 7：切片完整门禁与文档同步

**Files:**

- Modify: `docs/architecture/10-database.md`（只补已实现的 0023 事实）
- Modify: `docs/architecture/08-compliance.md`（说明 hash suppression 与 outreach
  typed suppression 的职责分离）
- Modify: `HANDBOOK.md`（仅勾/注已完成 prospecting 持久化子切片，不宣称切片 7 完成）

- [ ] 运行 prospecting 定向 unit/integration/migration/outbox 测试。
- [ ] mutation/characterization：临时破坏 VERIFIED 门禁、tenant predicate、LI assessment、
  erasure 重采集阻断、事件幂等各一次，确认对应测试变红；恢复后重跑变绿。
- [ ] 使用本地临时 PostgreSQL（UTF-8 initdb，不用 Docker）运行全量：
  `pytest -q`、`ruff check .`、全量 mypy、`python scripts/check_boundaries.py`、敏感扫描。
- [ ] 运行 API schema 生成零漂移、web typecheck/lint/tests/build；本切片无 UI 变更，浏览器
  E2E 以 exact-HEAD GitHub CI 为准。
- [ ] 更新文档，不夸大进度：account discovery workflow、connector、Campaign 接线和 UI
  仍未完成。
- [ ] Commit：`docs(prospecting): record persistence completion`，push 并等精确 CI；确认
  clean worktree 与本地 HEAD == origin HEAD。

## 完成后下一切片

按依赖顺序继续：单 Provider connector/tool-gateway → account discovery workflow →
需求发现预算上限与工作流 → Campaign 入组接线 → Demand Radar API/UI → Phase 1 端到端。
