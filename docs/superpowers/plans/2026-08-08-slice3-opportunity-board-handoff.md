# Slice 3 · 机会看板与人工接管（第一个可演示版本）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** HANDBOOK 切片 3 验收——人工造 5 条严格 Validated Need → 建机会 → 按 Territory 分给两个员工 → 接管队列最久等待最前 → 单条 T1/T2 超时升级（结构化日志 + 机会域审计，不接外部凭证）→ 前端可操作。产出模块化单体首个可演示版本（API + Web + 最小 Postgres workflow 引擎 + 单副本 scheduler + 结构化日志通知）。

**Architecture:** `apps/api`（FastAPI 工厂 `create_app` + 租户断言 + dev-mode actor 身份 assertion + 错误转换 + DI + 独立 composition 编排，**只挂已实现的 Slice-3 router**）；`apps/web`（Vue3 + TypeScript + Vite + Ant Design Vue + openapi-typescript 生成 types + openapi-fetch 类型化 wrapper，两个页面均在 `views/crm`）；`apps/scheduler_worker`（单副本 advisory lock 驱动 workflow 与 outbox 投递）；`workflows/human_handoff`（Postgres 状态机，严格对齐 `workflows/engine/runner.py` 的 `WorkflowEngine`/`WorkflowRun`/`StepDefinition` 接口）；`notification_gateway`（结构化日志 channel + 注入 durable `NotificationDedupStore`）；`domains/employees`（Territory Matrix + 企业粒度归属锁 + 八级首次分配 + 服务层授权）；`domains/opportunities`（typed action/scope/actor 授权契约 + 注入 authorizer 全读写判权 + 手工 Validated Need 证据校验）。

**Tech Stack:** Python 3.12+（conda `tradeos-py312`）、FastAPI、Pydantic v2、SQLAlchemy 2.x async + asyncpg、Alembic、PostgreSQL 16（testcontainers）、pytest/pytest-asyncio；前端 Vue3 + Vite + TS + Ant Design Vue + openapi-typescript + openapi-fetch（npm typecheck/lint/test/build）；E2E：pytest + Playwright Chromium（dev 依赖，`pyproject.toml` 安装）。

**Global Constraints:**
- 九条硬边界（AGENTS.md §三）；D1–D10 + F1–F17 为本切片硬约束，逐条落地。
- **门禁前缀**：`env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH"`；**不硬编码任何 Codex 临时路径**。
- **统一门禁块**（每任务，除非另有标注；**顺序固定，扫描在 stage 之后**）：
  1. 目标测试：`pytest <target> -q -W error`
  2. ruff：`ruff check <files> --no-cache`
  3. mypy：`mypy <files>`
  4. `make check`（内含 `scripts/scan_sensitive.py` **默认 tracked 模式**，CI 亦同；mypy 范围含 apps/workflows/notification_gateway/infra）
  5. `python3 scripts/check_boundaries.py`
  6. `git diff --check`
  7. 精确 `git add <exact files>` → `git diff --cached --name-only`（必须与 Files 完全一致）→ `python3 scripts/scan_sensitive.py --staged`（**提交前、add 后**）→ `git diff --cached --check` → `git commit` → `git push` → CI watch
- **前端任务（S3-17 后）**另跑 `npm run typecheck`、`npm run lint`、`npm run test`、`npm run build`。
- **Step 7 的 CI watch 固定块**（每任务）：
```bash
RUN_ID=$(gh run list --branch codex/phase1-implementation --commit "$(git rev-parse HEAD)" --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN_ID" --exit-status
```

**依赖顺序**：S3-1 工具门禁 → S3-2 文档 → S3-3/4 employees → S3-5 opportunities 授权（同 commit 适配 rg 确认的调用方）→ S3-6 Validated Need 证据（同 commit 适配 create 调用方）→ S3-7/8/9/10/11 workflow/outbox/notification/human_handoff/scheduler → S3-12 API 基建（空 crm 骨架）→ S3-13 机会/接管列表扩展 → S3-14/15 crm 端点 → S3-16 设计关卡（任何 web 编码前）→ S3-17/18/19 web → S3-20 演示 → S3-21 E2E。每任务独立可验证/提交/push。

---

## 审计

### 一、事实

- Slice 2 完成（S2-1…S2-12）：opportunities 全链路——六表迁移+触发器（0002 含 outbox guard，**仅 status/delivered_at 可更新**）、ORM+五仓储、UoW+outbox 写入、scorer、service 核心+handoff/查询、demo 脚本；332 测试全绿。公共接口 = `service.py` Protocol + `schemas.py` DTO。
- employees 合同已存在（浅域）：models（Employee/TerritoryAssignment/OwnershipLock，字段见 R2/R3）、service.py、repository.py；八级优先序见 AGENTS.md。
- `workflows/engine/runner.py`：`WorkflowEngine`/`WorkflowRun`/`StepDefinition`/`StepHandler`/`StepStatus` 接口齐全。
- rg 实测：`OpportunityServiceImpl(` 直接构造仅在 `scripts/demo_opportunities.py`、`tests/unit/test_opportunities_service.py`、`tests/unit/test_opportunities_handoff.py`；`create_from_need`/`OpportunityCreateRequest(` 在 `scripts/demo_opportunities.py`、`tests/unit/test_opportunities_service.py`、`tests/unit/test_opportunities_contracts.py`。
- `apps/web/package-lock.json` 不存在（S3-17 标 Create）。

### 二、文档冲突（已裁决）

- D7：`domains/opportunities/AGENTS.md` 漏 `OpportunityWon`（S3-2）。
- apps/web package.json 占位 vs AGENTS.md → S3-17。
- 0002 outbox guard 禁 attempt 更新 → F3/R7：0005 **自管**（不修改 0002）。
- workflow engine stub vs human_handoff → R6/R9/F9。

### 三、监督裁决 D1–D10 + 复核 R1–R17 + 终轮 F1–F17（本切片硬约束）

- **D1** `POST /opportunities` = Phase 1 operator-only 人工录入已验证需求适配器；复用/扩展域 schema，API 不手写重复业务结构。
- **D2** apps/api composition 依次 `EmployeeService.resolve_owner` → `OpportunityService.assign`；router 只解析/判权/调用/返回；只 import 各域 public service/schemas/errors。
- **D3/R6-R11** 最小 Postgres workflow 引擎 + outbox 投递闭环 + human_handoff T1/T2 + 审计 + scheduler 单副本。
- **D4/R13/F13** creative-production 设计关卡在**任何 web 编码前**；产物 `docs/design/slice3/spec.md` + `board.html` + `queue.html`，渲染截图视觉复核后才放行；两页面均在 `views/crm`。
- **D5/R11** `X-Tenant-Id` 精确匹配注入 settings，缺失/不匹配 fail closed；actor 来自 dev-mode `X-Employee-Id` assertion → 查员工推导 role/scope，绝不信任 role header。
- **D6/R4/F5** RBAC+ABAC 双保险、默认拒绝；opportunities 与 employees **所有读写服务**服务层授权；**同 commit 适配 rg 确认的调用方**；worker 显式 system actor。
- **D8/R2/R3/F4** 员工 Phase 1 只做角色/Territory/归属锁/八级首次分配（第 6 级 active count + employee_id tie-break，经理池注入已验证 active，绝不随机）；历史转移留痕。
- **D9/R15/F15/F16** 最终演示验收 + E2E（真实 uvicorn + Vite + Playwright Chromium）。
- **D10/R1/F1/F17** 门禁顺序固定（扫描在 add 后）；S3-1 改 Makefile/CI（mypy 扩范围、scanner 默认 tracked）；前端 npm 四件。
- **R5/F6** Validated Need 门槛 ≥ CUSTOMER_INTEREST_REPLY + source_type 白名单 + source_id/provenance；typed `ValidatedNeedEvidence` DTO 由 service 强制；同 commit 适配 create 调用方。
- **R7/F3/F7** 0005 自管 guard（不修改 0002）；durable per-handler `outbox_deliveries`；handler registry 显式注册（独立于 EVENT_REGISTRY 事件类型白名单）；仅全部 handler delivered 才 event delivered；无 handler 标记 dead（NO_REGISTERED_HANDLER，last_error 仅 event_type 不含 payload）并测试；TransientError backoff；dead-letter 可观测态。
- **R8/F8** notification dedup 用 durable `NotificationDedupStore`（0006）；router 注入 routing policy；结构化日志为 Slice3 唯一渠道；deep link 仅清理 query/token 的相对深链，禁绝对外链/凭证。
- **R9/F9** handoff escalation 审计属机会域（0007 + opportunities 公共 `record_handoff_escalation` + infra 仓储实现）；重复 level unique violation 精确映射幂等 no-op。
- **R10** scheduler 单副本（dedicated connection pg_try_advisory_lock、每轮先 drain→poll→再 drain）。
- **R12/F11** 新增 `list_opportunities`（SQL tenant-filtered scope）+ `list_pending_handoffs`；排序 `requested_at ASC` ≡ `wait_seconds DESC`；公共 `HandoffQueueItemView`/`ProvenanceSummary`。
- **R14/F14** openapi-typescript 生成 **types** + openapi-fetch；确定性 OpenAPI export 用 create_app 工厂（import 不连 DB）。
- **F10** S3-12 建空 `crm.py` APIRouter 骨架；E2E/运行统一 `uvicorn apps.api.main:create_app --factory`。
- **F12** API request/response 经 Pydantic v2 验证、域 schema 复用/TypeAdapter；intake create 返回 None 不继续 resolve_owner/assign。

### 四、ADR 门槛分析

- 新增 employees/workflow/outbox/notification/handoff_escalations 表（additive）+ 0005 自管 guard 扩展（不修改已发布 0002）→ 无 ADR。
- opportunities/employees 授权契约 + Validated Need 证据 DTO（additive）→ 无 ADR。
- 不改九条硬边界；shared 契约无字段改动 → 无 ADR。

---

## Schema 合同附录（迁移顺序 R16）

> 通用：全表 `tenant_id` NOT NULL（硬边界 8）；PK VARCHAR(32)；时间 TIMESTAMPTZ；只增表沿用审计触发器；复合 FK 参照列需先有复合 UNIQUE。**已发布 0001/0002 不回改**（0003 尚未发布，本切片新建）；0005 自管对 0002 表的 guard 调整。

### 0003 `employees`（S3-4）
`employees`：employee_id VARCHAR(32) PK、tenant_id、name VARCHAR(200) NOT NULL、role VARCHAR(32) NOT NULL、created_at TIMESTAMPTZ NOT NULL DEFAULT now()、user_id VARCHAR(32) NULL、team_id VARCHAR(32) NULL、manager_id VARCHAR(32) NULL、languages TEXT[] NOT NULL DEFAULT '{}'、timezone VARCHAR(64) NULL、is_active BOOLEAN NOT NULL DEFAULT true、max_active_accounts INT NULL。约束：UNIQUE(tenant_id, user_id)、**UNIQUE(tenant_id, employee_id)**（供复合 FK）、INDEX(tenant_id, role)、INDEX(tenant_id, is_active)。

`territory_assignments`：assignment_id PK、tenant_id、employee_id、priority INT NOT NULL、effective_from TIMESTAMPTZ NOT NULL、countries TEXT[] NOT NULL DEFAULT '{}'、product_categories TEXT[] NOT NULL DEFAULT '{}'、need_categories TEXT[] NOT NULL DEFAULT '{}'、buyer_types TEXT[] NOT NULL DEFAULT '{}'、languages TEXT[] NOT NULL DEFAULT '{}'、manager_id VARCHAR(32) NULL、backup_employee_id VARCHAR(32) NULL、effective_until TIMESTAMPTZ NULL。约束：复合 FK (tenant_id, employee_id)→employees；**复合 FK (tenant_id, manager_id)→employees**、**复合 FK (tenant_id, backup_employee_id)→employees**；INDEX(tenant_id, priority)。

`ownership_locks`：lock_id PK、tenant_id、account_id、owner VARCHAR(32) NOT NULL、locked_at TIMESTAMPTZ NOT NULL、locked_by_rule VARCHAR(64) NOT NULL。约束：UNIQUE(tenant_id, account_id)、复合 FK (tenant_id, owner)→employees。

`ownership_transfer_history`（只增）：transfer_id PK、tenant_id、account_id、from_owner VARCHAR(32) NULL、to_owner VARCHAR(32) NOT NULL、transferred_by VARCHAR(32) NOT NULL、transferred_at TIMESTAMPTZ NOT NULL、reason TEXT NOT NULL。约束：只增触发器、INDEX(tenant_id, account_id)、**复合 FK (tenant_id, from_owner)→employees**、**复合 FK (tenant_id, to_owner)→employees**、**复合 FK (tenant_id, transferred_by)→employees**。

### 0004 `workflow_runs` / `workflow_steps`（S3-7）
`workflow_runs`：run_id PK、tenant_id、workflow_type VARCHAR(64) NOT NULL、workflow_version INT NOT NULL、subject_ref VARCHAR(64) NOT NULL、current_step VARCHAR(64) NOT NULL、status VARCHAR(32) NOT NULL DEFAULT 'running'、created_at TIMESTAMPTZ NOT NULL DEFAULT now()、next_poll_at TIMESTAMPTZ NULL、retry_count INT NOT NULL DEFAULT 0、context JSONB NOT NULL、last_error TEXT NULL、idempotency_key VARCHAR(200) NOT NULL。约束：UNIQUE(tenant_id, idempotency_key)、**UNIQUE(tenant_id, run_id)**（供复合 FK）、INDEX(tenant_id, status, next_poll_at)。

`workflow_steps`：step_id PK、run_id VARCHAR(32) NOT NULL、tenant_id、step_name VARCHAR(64) NOT NULL、status VARCHAR(32) NOT NULL DEFAULT 'pending'、data JSONB NOT NULL、attempt INT NOT NULL DEFAULT 0、error TEXT NULL、due_at TIMESTAMPTZ NOT NULL、idempotency_key VARCHAR(200) NOT NULL、created_at/updated_at TIMESTAMPTZ NOT NULL DEFAULT now()。约束：复合 FK (tenant_id, run_id)→workflow_runs(tenant_id, run_id) ON DELETE CASCADE、UNIQUE(tenant_id, idempotency_key)、INDEX(tenant_id, status, due_at)。

### 0005 `outbox_events` 扩展 + `outbox_deliveries`（S3-8，**自管 guard，不修改 0002**）
`outbox_events` **加列**：next_attempt_at TIMESTAMPTZ NULL、last_error TEXT NULL；**0005 内** DROP 原 status CHECK 后重建（白名单含 'pending'/'delivered'/'dead'）并 `CREATE OR REPLACE` guard 函数（允许 status/delivered_at/attempt/next_attempt_at/last_error 更新）；加 UNIQUE(tenant_id, event_id)。**downgrade 恢复 0002 语义**（还原 status CHECK 与 guard 行为）。测试覆盖 0002→0005 upgrade 与 downgrade 恢复。

`outbox_deliveries`（durable per-handler）：delivery_id PK、tenant_id、event_id、handler_name VARCHAR(64) NOT NULL、status VARCHAR(16) NOT NULL DEFAULT 'pending'、attempts INT NOT NULL DEFAULT 0、next_attempt_at TIMESTAMPTZ NULL、last_error TEXT NULL、delivered_at TIMESTAMPTZ NULL。约束：UNIQUE(tenant_id, event_id, handler_name)、复合 FK (tenant_id, event_id)→outbox_events。

### 0006 `notification_deliveries`（S3-9）
delivery_id PK、tenant_id、dedup_key VARCHAR(200) NOT NULL、channel_name VARCHAR(64) NOT NULL、status VARCHAR(16) NOT NULL DEFAULT 'pending'、**attempts INT NOT NULL DEFAULT 0**、**next_attempt_at TIMESTAMPTZ NULL**、**last_error TEXT NULL**、delivered_at TIMESTAMPTZ NULL。约束：UNIQUE(tenant_id, dedup_key, channel_name)（支持部分渠道失败后 durable retry）。

### 0007 `handoffs` 加 UNIQUE + `handoff_escalations`（S3-10）
`handoffs` **加 UNIQUE(tenant_id, handoff_id)**（additive，0007 内）。

`handoff_escalations`（只增，机会域审计）：escalation_id PK、tenant_id、handoff_id、level INT NOT NULL、escalated_at TIMESTAMPTZ NOT NULL、note TEXT NULL。约束：只增触发器、UNIQUE(tenant_id, handoff_id, level)（幂等）、复合 FK (tenant_id, handoff_id)→handoffs。

---

## 任务列表

### 任务 S3-1：敏感扫描脚本 + 门禁纳入（R1/F1）

**Files**
- Create: `scripts/scan_sensitive.py`、`tests/unit/test_scan_sensitive.py`
- Modify: `Makefile`、`.github/workflows/ci.yml`

- [ ] **Step 1：写失败测试** — 扫描只匹配**高置信凭证形态**（带非占位 userinfo 的 DSN、AKIA+16 位、`BEGIN [A-Z ]*PRIVATE KEY` 形密钥、token 前缀、`password=非占位`；**不写完整凭证样例**，matcher 只描述形态）；**允许**文档占位符与 scheme-only 字面量（含 conftest 的 `postgresql://`）；测试合成秘密运行时拼接；结果仅 path/line/kind；CLI `--staged`（提交前扫描 `git diff --cached --name-only`）与默认 tracked 模式。
- [ ] **Step 2：运行 RED** — `pytest tests/unit/test_scan_sensitive.py -q -W error`。
- [ ] **Step 3：最小实现** — `scan_sensitive.py`（`--staged`/`--quiet`/exit 0|1，默认 tracked；不读文件外内容、不输出匹配内容）+ Makefile `check` 加 `python3 scripts/scan_sensitive.py`（**默认 tracked**）并**扩大 mypy 到 `domains shared tool_gateway apps workflows notification_gateway infra`** + CI 加 Sensitive scan 步骤（**默认 tracked**）、Type check 步骤同步扩范围。
- [ ] **Step 4：GREEN** — 同命令通过；`make check` 实测（190 文件 mypy 通过）。
- [ ] **Step 5：统一门禁块**（**扫描在 `git add` 之后执行 `--staged`**——本任务 Step 7 即此顺序；Makefile/CI 的默认 tracked 与提交前 --staged 是两种用途，已在 Step 3 区分）。
- [ ] **Step 6：仅 stage 精确文件** — `git add scripts/scan_sensitive.py tests/unit/test_scan_sensitive.py Makefile .github/workflows/ci.yml`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "chore(security): add sensitive scan and widen gate scope"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-2：opportunities/AGENTS 文档小修（D7/F2）

**Files**
- Modify: `domains/opportunities/AGENTS.md`
- Create: `tests/unit/test_opportunities_agents_doc.py`

- [ ] **Step 1：写失败测试** — 断言 events.py `PUBLISHES` 每个事件类名（含 `OpportunityWon`）出现在 AGENTS.md 文本。
- [ ] **Step 2：运行 RED** — `pytest tests/unit/test_opportunities_agents_doc.py -q -W error`。
- [ ] **Step 3：最小实现** — AGENTS.md「发布的事件」补 `OpportunityWon`。
- [ ] **Step 4：GREEN** — 同命令通过。
- [ ] **Step 5：统一门禁块**。
- [ ] **Step 6：仅 stage 精确文件** — `git add domains/opportunities/AGENTS.md tests/unit/test_opportunities_agents_doc.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "docs(opportunities): document OpportunityWon in published events"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-3：employees 域——Territory/归属锁纯逻辑 + 服务层授权（D8/R2/R4）

**Files**
- Modify: `domains/employees/models.py`（补 `OwnershipTransfer` 实体）、`schemas.py`（补公共 View）、`service.py`、`repository.py`（补 repo Protocol 的 list/add/transfer-history 等实际需要）、`errors.py`
- Create: `domains/employees/service_impl.py`、`domains/employees/permissions.py`（typed action/scope/actor + EmployeeAuthorizer Protocol，默认拒绝，不 import 其他域）、`tests/unit/test_employees_service.py`

- [ ] **Step 1：写失败测试** — 以现有 models/service/repository 为合同（不另造冲突 schema）：保留 Employee 的 user_id/team_id/manager_id/languages/timezone/is_active/max_active_accounts；TerritoryAssignment 的 plural arrays + manager/backup + priority + effective_from/until；OwnershipLock 的 locked_by_rule。`resolve_owner` 八级字典序（首命中即停、locked_by_rule 记录级别、已有锁幂等返回）；第 6 级活跃客户数最少 + **稳定 employee_id tie-break**；经理池由注入且已验证 active 的 employee_id 兜底、绝不随机；`transfer` 落 `OwnershipTransfer`（from/to/by/at/reason 非空）；`apply_territory_from_directive` 只改未来规则不回溯锁；公共 View 供 apps 消费（`employees.schemas`，不 import models）；**服务层授权**：employees 所有读写经 EmployeeAuthorizer、未知 action 默认拒绝、audit logger（actor/action/tenant/scope/rule，无敏感值）。
- [ ] **Step 2：运行 RED** — `pytest tests/unit/test_employees_service.py -q -W error`。
- [ ] **Step 3：最小实现** — `EmployeeServiceImpl`（注入 now、活跃计数、经理池、authorizer；八级解析含 tie-break）；`permissions.py`（Action/Scope/actor + EmployeeAuthorizer Protocol + 默认拒绝 + audit logger）；repository.py 补 `list_transfers` 等 Protocol；schemas.py 补 View。
- [ ] **Step 4：GREEN** — 同命令通过。
- [ ] **Step 5：统一门禁块**。
- [ ] **Step 6：仅 stage 精确文件** — `git add domains/employees/models.py domains/employees/schemas.py domains/employees/service.py domains/employees/repository.py domains/employees/errors.py domains/employees/service_impl.py domains/employees/permissions.py tests/unit/test_employees_service.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(employees): implement territory resolution ownership and authorization"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-4：employees 持久化——0003 + infra 仓储（R3/D8/F4）

**Files**
- Create: `migrations/versions/0003_employees.py`、`infra/db/repositories/employees.py`、`tests/integration/test_employees_repositories.py`
- Modify: `infra/db/tables.py`（EmployeeRow/TerritoryAssignmentRow/OwnershipLockRow/OwnershipTransferHistoryRow）

- [ ] **Step 1：写失败测试** — 0003 round-trip + 列集合逐字段匹配现有实体（TEXT[] 数组、有效窗口、manager/backup、locked_by_rule）；**复合 FK**：territory employee_id/manager_id/backup_employee_id、transfer from_owner/to_owner/transferred_by 均 (tenant_id, ...)→employees；`EmployeeRepositoryImpl`（get/add/update/list_active/count_active_accounts）；`TerritoryRepositoryImpl`（add、list_matching 按维度+priority、按员工 list）；`OwnershipRepositoryImpl`（try_lock 并发唯一、get、replace 归档 + 只增 transfer_history、list_transfers）；transfer reason 非空；只增触发器拒 UPDATE/DELETE；租户隔离负测。
- [ ] **Step 2：运行 RED** — `pytest tests/integration/test_employees_repositories.py -q -W error`。
- [ ] **Step 3：最小实现** — 0003 四表（附录）+ ORM 行 + 三个 repo impl（租户绑定同 S2-4 模式）。
- [ ] **Step 4：GREEN** — 同命令通过。
- [ ] **Step 5：统一门禁块**。
- [ ] **Step 6：仅 stage 精确文件** — `git add migrations/versions/0003_employees.py infra/db/tables.py infra/db/repositories/employees.py tests/integration/test_employees_repositories.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(infra): add employees territory and ownership persistence"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-5：opportunities 授权契约 + 全部读写判权 + 同 commit 适配调用方（D6/R4/F5）

**Files**
- Create: `domains/opportunities/permissions.py`、`tests/unit/test_opportunities_permissions.py`
- Modify: `domains/opportunities/service.py`、`service_impl.py`、`scripts/demo_opportunities.py`、`tests/unit/test_opportunities_service.py`、`tests/unit/test_opportunities_handoff.py`、`tests/unit/test_opportunities_contracts.py`

> Files 依据 `rg -l "OpportunityServiceImpl\\(" scripts tests`（实测直接构造：demo/service/handoff）与 `test_opportunities_contracts.py`（契约签名断言，actor 参数变更须同步断言）。**执行前再跑一次 rg 复核**，任何新增发现同 commit 加入 Files 并同步 stage；不得预列 scoring/models/repository 等无变化文件。

- [ ] **Step 1：写失败测试** — typed Action/Scope/actor；`OpportunityAuthorizer` Protocol（require(actor, action, scope, tenant)，默认拒绝未知 action）；OpportunityService **所有读写方法**带 actor 且经 authorizer（查询也 ABAC scope）；不 import employees；security authorization audit logger（无敏感值）；worker 用显式最小 system actor/scope 不旁路。
- [ ] **Step 2：运行 RED** — `pytest tests/unit/test_opportunities_permissions.py -q -W error`。
- [ ] **Step 3：最小实现** — permissions.py（Action/Scope/actor + OpportunityAuthorizer Protocol + 默认拒绝 + audit logger）；service.py 方法签名加 actor + 注入 authorizer；service_impl 各方法前 require；**同 commit** 适配 demo/service/handoff/contracts（显式 actor 参数 + 契约断言）。
- [ ] **Step 4：GREEN** — 目标测试全绿 + `make check` 全绿。
- [ ] **Step 5：统一门禁块**。
- [ ] **Step 6：仅 stage 精确文件** — `git add domains/opportunities/permissions.py domains/opportunities/service.py domains/opportunities/service_impl.py scripts/demo_opportunities.py tests/unit/test_opportunities_service.py tests/unit/test_opportunities_handoff.py tests/unit/test_opportunities_contracts.py tests/unit/test_opportunities_permissions.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(opportunities): enforce authorization on all service operations"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-6：Validated Need 证据 DTO + service 强制 + 同 commit 适配调用方（R5/F6）

**Files**
- Modify: `domains/opportunities/schemas.py`（新增 typed `ValidatedNeedEvidence` DTO）、`service.py`、`service_impl.py`、`scripts/demo_opportunities.py`、`tests/unit/test_opportunities_service.py`、`tests/unit/test_opportunities_contracts.py`
- Create: `tests/unit/test_opportunities_validated_need.py`

> Files 依据 `rg -l "create_from_need|OpportunityCreateRequest\\(" scripts tests`（实测：demo/service/contracts）。**执行前再跑一次 rg 复核**，任何新增发现同 commit 加入 Files 并同步 stage。

- [ ] **Step 1：写失败测试** — `ValidatedNeedEvidence`：level ≥ `CUSTOMER_INTEREST_REPLY`（LOW_MID/public event 拒绝）、source_type 仅 CONVERSATION/UPLOAD/EMPLOYEE_INPUT、source_id 非空白、provenance 指向客户消息/员工确认；service 强制（非 router）；create_from_need 接受证据 DTO，不满足拒绝；field provenance 仍逐 present 关键字段且禁 AGENT_INFERENCE。
- [ ] **Step 2：运行 RED** — `pytest tests/unit/test_opportunities_validated_need.py -q -W error`。
- [ ] **Step 3：最小实现** — schemas 加 DTO + service.py 签名调整（create_from_need 接收 `ValidatedNeedEvidence`）+ service_impl 校验（类型化错误）；**同 commit** 适配 demo/service/contracts（调用方传证据 DTO）。
- [ ] **Step 4：GREEN** — 同命令 + `make check` 全绿。
- [ ] **Step 5：统一门禁块**。
- [ ] **Step 6：仅 stage 精确文件** — `git add domains/opportunities/schemas.py domains/opportunities/service.py domains/opportunities/service_impl.py scripts/demo_opportunities.py tests/unit/test_opportunities_service.py tests/unit/test_opportunities_contracts.py tests/unit/test_opportunities_validated_need.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(opportunities): enforce validated need evidence gate"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-7：Postgres workflow 引擎——0004 + 对齐 runner.py（D3/R6/F4）

**Files**
- Create: `migrations/versions/0004_workflow_engine.py`、`infra/db/workflow_engine.py`、`tests/integration/test_workflow_engine.py`
- Modify: `infra/db/tables.py`（WorkflowRunRow/WorkflowStepRow）、`workflows/engine/runner.py`、`pyproject.toml`（同 commit 删 runner.py stub Ruff 豁免）

- [ ] **Step 1：写失败测试** — 严格对齐 runner.py：register/start（idempotency_key 必填，同 tenant+key 幂等返回既有 run）/poll_due（FOR UPDATE SKIP LOCKED、每步独立事务、TransientError 指数退避、超 max_retries 转 FAILED）/deliver_event（WAITING_EVENT 幂等）/cancel；WorkflowRun 字段与 0004 列一致（workflow_version/subject_ref/current_step/next_poll_at/retry_count/context/last_error）；run 幂等 UNIQUE(tenant_id, idempotency_key) + UNIQUE(tenant_id, run_id)；steps 复合 FK (tenant_id, run_id)；租户隔离；0004 round-trip。仅当 runner.py 接口确有缺口才增参数（显式契约变更 + 测试）。
- [ ] **Step 2：运行 RED** — `pytest tests/integration/test_workflow_engine.py -q -W error`。
- [ ] **Step 3：最小实现** — 0004 两表（附录）+ ORM + `PostgresWorkflowEngine`（实现 runner.py Protocol；SKIP LOCKED 取批；run/steps 幂等）；如需调整 runner.py 契约则先改接口 + 契约测试；删 runner.py F401 豁免。
- [ ] **Step 4：GREEN** — 同命令 + `make check` 全绿。
- [ ] **Step 5：统一门禁块**。
- [ ] **Step 6：仅 stage 精确文件** — `git add migrations/versions/0004_workflow_engine.py infra/db/tables.py infra/db/workflow_engine.py workflows/engine/runner.py pyproject.toml tests/integration/test_workflow_engine.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(infra): add postgres workflow engine aligned with engine interface"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-8：outbox 投递轮询——0005 自管 guard（D3/R7/F3/F7）

**Files**
- Create: `migrations/versions/0005_outbox_delivery.py`、`infra/db/outbox_delivery.py`、`tests/integration/test_outbox_delivery.py`
- Modify: `infra/db/tables.py`（OutboxEventRow 加 next_attempt_at/last_error + OutboxDeliveryRow）

> **不修改已发布的 `migrations/versions/0002_opportunities.py`**。0005 **自管**：DROP 并重建 outbox status CHECK（白名单含 'dead'）+ `CREATE OR REPLACE` guard 函数（允许 status/delivered_at/attempt/next_attempt_at/last_error 更新）+ 加 UNIQUE(tenant_id,event_id)；downgrade 恢复 0002 语义。测试覆盖 0002→0005 upgrade 与 downgrade 恢复。

- [ ] **Step 1：写失败测试** — 0005 迁移（含上述自管 guard 行为 + downgrade 恢复 0002 语义）；`outbox_deliveries` UNIQUE(tenant+event+handler)；**handler registry 与 EVENT_REGISTRY 分工**：事件类型白名单（EVENT_REGISTRY，S2-6）≠ 订阅 handler 白名单（handler_name 显式注册）；outbox event **仅当所有注册 handler 的 delivery=delivered 才 delivered**；**无 handler：event 标记 dead、`last_error` 只写固定 `NO_REGISTERED_HANDLER` + event_type（不含 payload）、记录结构化错误，不留 pending 紧循环、不静默 delivered**；drain 用 SKIP LOCKED；一个事件多订阅者 crash 后幂等续投；TransientError backoff；永久错误/耗尽进 dead-letter 可观测态（不 poison tight loop）；未注册事件类型原样失败不吞；并发/租户测试。
- [ ] **Step 2：运行 RED** — `pytest tests/integration/test_outbox_delivery.py -q -W error`。
- [ ] **Step 3：最小实现** — 0005（DROP/重建 CHECK + CREATE OR REPLACE guard + 加列 + UNIQUE）+ ORM + `OutboxDeliverer`（drain/deliver_one/deliver_pending；handler registry 显式注册；仅全部 handler delivered 才 event delivered；无 handler 标记 dead，`last_error` 固定 `NO_REGISTERED_HANDLER` + event_type，不含 payload）。`HandoffRequested`/`HandoffAccepted` 的 handlers 在 S3-10 装配注册。
- [ ] **Step 4：GREEN** — 同命令 + `make check` 全绿（round-trip 含 0002→0005→base）。
- [ ] **Step 5：统一门禁块**。
- [ ] **Step 6：仅 stage 精确文件** — `git add migrations/versions/0005_outbox_delivery.py infra/db/tables.py infra/db/outbox_delivery.py tests/integration/test_outbox_delivery.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(infra): add durable outbox delivery with retry and dead letter"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-9：notification_gateway 结构化日志 channel + durable dedup——0006（D3/R8/F8）

**Files**
- Create: `migrations/versions/0006_notification_dedup.py`、`notification_gateway/router.py`、`notification_gateway/channels/structured_log.py`、`notification_gateway/dedup.py`（NotificationDedupStore Protocol + 声明）、`infra/db/repositories/notifications.py`、`tests/unit/test_notification_router.py`、`tests/integration/test_notification_dedup.py`
- Modify: `infra/db/tables.py`（NotificationDeliveryRow）、`notification_gateway/models.py`（保留公共 Protocol，不留重复 stale class）

- [ ] **Step 1：写失败测试** — **unit（router）**：`NotificationRouter` 注入 `NotificationDedupStore`（非内存）+ 注入 **routing policy**（fake channels 测 URGENT 多渠道并发、单渠道失败不影响其他且可重试）；不写「LOW 只进 in-app」（Slice3 无 in-app/email，仅 structured_log）；deep link 政策：仅允许清理 query/token 的相对深链，**禁止绝对外链/凭证**（不自相矛盾）；路由失败抛 TransientError 可重试、不阻塞主业务。**integration（persistence）**：`tests/integration/test_notification_dedup.py`——0006 round-trip、UNIQUE(tenant_id, dedup_key, channel_name)、attempts/next_attempt_at/last_error durable retry、重复 dispatch 不重复投递、租户隔离。
- [ ] **Step 2：运行 RED** — `pytest tests/unit/test_notification_router.py tests/integration/test_notification_dedup.py -q -W error`。
- [ ] **Step 3：最小实现** — `StructuredLogChannel`（固定 logger + 结构化 key，相对深链已清理、禁绝对外链/凭证）+ `NotificationRouter`（注入 dedup store + routing policy）+ `NotificationDedupStore` Protocol + infra Postgres 实现（0006）+ ORM 行。
- [ ] **Step 4：GREEN** — 同命令 + `make check` 全绿。
- [ ] **Step 5：统一门禁块**。
- [ ] **Step 6：仅 stage 精确文件** — `git add migrations/versions/0006_notification_dedup.py notification_gateway/router.py notification_gateway/channels/structured_log.py notification_gateway/dedup.py infra/db/repositories/notifications.py infra/db/tables.py notification_gateway/models.py tests/unit/test_notification_router.py tests/integration/test_notification_dedup.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(notification): add structured log channel with durable dedup"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-10：human_handoff 流程——T1/T2 升级 + 机会域审计——0007（D3/R9/F9）

**Files**
- Create: `migrations/versions/0007_handoff_escalations.py`、`workflows/human_handoff/flow.py`、`tests/unit/test_human_handoff_flow.py`、`tests/integration/test_human_handoff_workflow.py`
- Modify: `infra/db/tables.py`（HandoffEscalationRow + HandoffRow 加 UNIQUE(tenant_id,handoff_id)）、`domains/opportunities/repository.py`、`service.py`、`service_impl.py`、`infra/db/repositories/opportunities.py`、`tests/integration/test_repositories.py`

- [ ] **Step 1：写失败测试** — 单条流程：HandoffRequested → start（subject_ref=handoff_id 幂等）；HandoffAccepted 经 handoff_id 查 active run 后 deliver_event 完成；T1 从 requested_at/occurred_at 起算（不从延迟消费时刻）；T1/T2 注入；升级不换负责人；每次升级调机会域公共 `record_handoff_escalation(tenant_id, handoff_id, level, at)`（**workflow 不直写机会域内部表**）；0007 UNIQUE(tenant+handoff+level)——重复 level 的 unique violation（SQLSTATE 23505）**精确映射为幂等 no-op，不吞其他 DB 错**；经理/老板经 EmployeeService 公共 DTO 解析；service 方法含 actor/system scope 仍走 authorizer。**仓储集成**：`record_handoff_escalation` 的 infra 实现（追加只增 + 幂等 no-op + 非 23505 上抛）与 `tests/integration/test_repositories.py` 对应测试；0007 round-trip + 只增。
- [ ] **Step 2：运行 RED** — `pytest tests/unit/test_human_handoff_flow.py tests/integration/test_human_handoff_workflow.py tests/integration/test_repositories.py -q -W error`。
- [ ] **Step 3：最小实现** — 0007（handoffs 加 UNIQUE + handoff_escalations 只增表）+ ORM + opportunities `record_handoff_escalation`（service 公共方法 + infra 仓储实现 + 23505 幂等 no-op）+ `workflows/human_handoff/flow.py`（注册定义、T1/T2 注入、HandoffRequested/HandoffAccepted handlers 装配，并经 outbox handler registry 注册）。
- [ ] **Step 4：GREEN** — 同命令 + `make check` 全绿。
- [ ] **Step 5：统一门禁块**。
- [ ] **Step 6：仅 stage 精确文件** — `git add migrations/versions/0007_handoff_escalations.py workflows/human_handoff/flow.py infra/db/tables.py domains/opportunities/repository.py domains/opportunities/service.py domains/opportunities/service_impl.py infra/db/repositories/opportunities.py tests/unit/test_human_handoff_flow.py tests/integration/test_human_handoff_workflow.py tests/integration/test_repositories.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(workflows): implement human handoff escalation with opportunity audit"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-11：scheduler_worker 单副本驱动（D3/R10）

**Files**
- Create: `apps/scheduler_worker/main.py`、`tests/integration/test_scheduler_worker.py`

- [ ] **Step 1：写失败测试** — 主循环：dedicated connection `pg_try_advisory_lock`，未获锁不启动第二副本、退出释放；每轮先 drain outbox → poll_due → 必要时再 drain 新事件；异常按类别记录且不中断其他项；幂等推进；通过 flow handler 的显式 system actor 调服务。
- [ ] **Step 2：运行 RED** — `pytest tests/integration/test_scheduler_worker.py -q -W error`。
- [ ] **Step 3：最小实现** — `main.py`：advisory lock（dedicated engine/connection）+ 循环（outbox deliver → workflow poll → 再 deliver）+ 异常分类日志 + 释放锁。
- [ ] **Step 4：GREEN** — 同命令 + `make check` 全绿。
- [ ] **Step 5：统一门禁块**。
- [ ] **Step 6：仅 stage 精确文件** — `git add apps/scheduler_worker/main.py tests/integration/test_scheduler_worker.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(apps): add single-replica scheduler worker"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-12：apps/api 基建——工厂/租户断言/actor 身份/错误转换/DI + 空 crm 骨架（D5/R11/F10）

**Files**
- Modify: `apps/api/main.py`、`apps/api/routers/crm.py`（本任务建空 `APIRouter` 骨架并 stage；后续 S3-14/15 加端点）
- Create: `apps/api/middleware.py`、`apps/api/dependencies.py`、`apps/api/identity.py`、`tests/unit/test_api_app.py`

- [ ] **Step 1：写失败测试** — `create_app`（**工厂**，运行统一 `uvicorn apps.api.main:create_app --factory`）：挂 `crm` 空 router（后续端点逐步加），**不挂 14 个 docstring stub**；`X-Tenant-Id` 与注入 settings 精确匹配，缺失/不匹配固定状态码（401/403）并测试；dev-mode `X-Employee-Id` assertion → 查当前租户员工推导 role/scope（绝不信任 role header），非 dev fail closed；错误转换（ValidationError→400、InvalidStateTransition→409、is_retryable→Retry-After）；DI 装配（opportunities/employees service + authorizer + workflow engine + outbox deliverer + notification router + dedup store）；settings/DI 全部注入，**import 模块不连接 DB**；外部契约 Pydantic v2。
- [ ] **Step 2：运行 RED** — `pytest tests/unit/test_api_app.py -q -W error`。
- [ ] **Step 3：最小实现** — middleware.py（TenantAssertion + 错误 handler）、identity.py（dev-mode assertion）、dependencies.py（判权第一道 + 注入）、crm.py（空 APIRouter 骨架）、main.py（create_app 装配 + 启动校验 + 挂 crm）。
- [ ] **Step 4：GREEN** — 同命令 + `make check` 全绿。
- [ ] **Step 5：统一门禁块**。
- [ ] **Step 6：仅 stage 精确文件** — `git add apps/api/main.py apps/api/routers/crm.py apps/api/middleware.py apps/api/dependencies.py apps/api/identity.py tests/unit/test_api_app.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(api): add app factory tenant and identity assertion"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-13：opportunities 列表扩展——scoped 机会列表 + pending 接管列表（R12/F11）

**Files**
- Modify: `domains/opportunities/service.py`、`repository.py`、`service_impl.py`、`schemas.py`（加 `HandoffQueueItemView`、`ProvenanceSummary`）、`infra/db/repositories/opportunities.py`、`tests/unit/test_opportunities_handoff.py`、`tests/unit/test_opportunities_service.py`、`tests/unit/test_opportunities_contracts.py`、`tests/integration/test_repositories.py`
- Create: `tests/unit/test_opportunities_lists.py`

- [ ] **Step 1：写失败测试** — 新增 `list_opportunities(tenant_id, actor, *, scope, states, limit)`（ABAC scope 经 authorizer）与 `list_pending_handoffs(tenant_id, actor, *, limit)`（含 wait_seconds 与 packet 摘要）；**scope 的 allowed owners/countries/categories 在 SQL 查询中 tenant-filtered，不拉全租户结果内存过滤**（`infra/db/repositories/opportunities.py` 条件 WHERE + integration 验证）；排序统一 `requested_at ASC` ≡ `wait_seconds DESC`（最久等待最前）；`HandoffQueueItemView`/`ProvenanceSummary` 公共 DTO；`OpportunityView`/detail 携带关键字段 provenance 摘要（service 从 provenance repo 读取并测试）；limit>0 校验；授权默认拒绝；contract test 补签名。
- [ ] **Step 2：运行 RED** — `pytest tests/unit/test_opportunities_lists.py tests/integration/test_repositories.py -q -W error`。
- [ ] **Step 3：最小实现** — service.py 两个方法 + schemas.py 公共 DTO + repository.py Protocol + `infra/db/repositories/opportunities.py` scoped SQL 查询 + service_impl（authorizer scope 校验、排序 ASC、provenance 摘要组装）。
- [ ] **Step 4：GREEN** — 同命令 + `make check` 全绿（既有测试适配）。
- [ ] **Step 5：统一门禁块**。
- [ ] **Step 6：仅 stage 精确文件** — `git add domains/opportunities/service.py domains/opportunities/repository.py domains/opportunities/service_impl.py domains/opportunities/schemas.py infra/db/repositories/opportunities.py tests/unit/test_opportunities_lists.py tests/unit/test_opportunities_handoff.py tests/unit/test_opportunities_service.py tests/unit/test_opportunities_contracts.py tests/integration/test_repositories.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(opportunities): add scoped list and pending handoff list"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-14：crm 机会端点 + intake composition（D1/D2/D6/R5/F12）

**Files**
- Modify: `apps/api/routers/crm.py`
- Create: `apps/api/composition/opportunity_intake.py`、`tests/unit/test_crm_router.py`、`tests/integration/test_crm_api.py`

- [ ] **Step 1：写失败测试** — **D1** `POST /opportunities`（operator-only；请求/响应经 **Pydantic v2** 验证，**域 schema 复用/TypeAdapter**，不用裸 dict、不复制业务字段；含 `ValidatedNeedEvidence`）；**intake：create gate 返回 None → 不继续 resolve_owner/assign**（composition 编排分支并测试）；成功→建机会→resolve_owner→assign；GET /opportunities（S3-13 scoped 列表）、GET /opportunities/{id}（含 provenance 摘要）、POST transition（409）、POST mark-lost（必须带 reason）；D6 未授权拒绝、authorizer 生效、审计带 actor/action/scope；composition 只 import 各域 public service/schemas/errors。
- [ ] **Step 2：运行 RED** — `pytest tests/unit/test_crm_router.py tests/integration/test_crm_api.py -q -W error`。
- [ ] **Step 3：最小实现** — crm.py 加 `/crm/opportunities` 路由组（四件套）+ `opportunity_intake.py`（create gate 分支 + resolve_owner→assign；employee 公共 DTO 翻译为 opportunity scope）。
- [ ] **Step 4：GREEN** — 同命令 + `make check` 全绿。
- [ ] **Step 5：统一门禁块**。
- [ ] **Step 6：仅 stage 精确文件** — `git add apps/api/routers/crm.py apps/api/composition/opportunity_intake.py tests/unit/test_crm_router.py tests/integration/test_crm_api.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(api): add opportunity endpoints and intake composition"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-15：crm 接管队列端点（R12/F15）

**Files**
- Modify: `apps/api/routers/crm.py`
- Create: `tests/unit/test_crm_handoff_router.py`、`tests/integration/test_crm_handoff_api.py`

- [ ] **Step 1：写失败测试** — crm.py 加 `/crm/handoffs` 路由组：GET /handoffs（S3-13 `list_pending_handoffs`，最久等待最前）、GET /handoffs/{id}（完整 packet + wait_seconds）、POST /handoffs/{id}/accept（authorizer + accept_if_requested 原子、并发二次 False→409）、GET /analytics/loss-reasons；未授权拒绝；错误映射。
- [ ] **Step 2：运行 RED** — `pytest tests/unit/test_crm_handoff_router.py tests/integration/test_crm_handoff_api.py -q -W error`。
- [ ] **Step 3：最小实现** — crm.py 加 `/crm/handoffs` 端点（复用 S2-11 service + S3-13 列表，authorizer 校验）。
- [ ] **Step 4：GREEN** — 同命令 + `make check` 全绿。
- [ ] **Step 5：统一门禁块**。
- [ ] **Step 6：仅 stage 精确文件** — `git add apps/api/routers/crm.py tests/unit/test_crm_handoff_router.py tests/integration/test_crm_handoff_api.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(api): add handoff queue endpoints"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-16：creative-production 设计关卡（D4/R13/F13，无 UI 编码）

**Files**
- Create: `docs/design/slice3/spec.md`、`docs/design/slice3/board.html`、`docs/design/slice3/queue.html`

- [ ] **Step 1：设计产出** — 机会看板 + 接管队列：`ProvenancePopover`、**事实与推断视觉分离**、**等待时长排序（最久最前）**、接受/推进交互；`spec.md` 写清组件树、数据绑定（来自生成 API types）、状态与错误态。
- [ ] **Step 2：验收** — 渲染 `board.html`/`queue.html` 到 `mktemp -d` 临时目录并**截图做视觉复核**，监督评审放行后才写 Vue；临时目录复核后清理，**截图不作为提交产物**、不藏 sidecar；Files 固定仅 `spec.md`/`board.html`/`queue.html` 三个，无条件句；此任务不写任何 Vue/前端代码。
- [ ] **Step 5：门禁** — 按统一门禁块（**先 stage 再 `scan_sensitive --staged`**）+ boundary/diff。
- [ ] **Step 6：仅 stage 精确文件** — `git add docs/design/slice3/spec.md docs/design/slice3/board.html docs/design/slice3/queue.html`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "docs(ui): opportunity board and handoff queue design"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-17：apps/web 工具链 + 生成 API types（D4/R14/F14）

**Files**
- Modify: `apps/web/package.json`、`.github/workflows/ci.yml`（Node setup/cache + npm 四件）
- Create: `apps/web/package-lock.json`（**Create，当前不存在**）、`apps/web/vite.config.ts`、`apps/web/tsconfig.json`、`apps/web/index.html`、`apps/web/eslint.config.js`、`apps/web/vitest.config.ts`、`apps/web/src/main.ts`、`apps/web/src/App.vue`、`apps/web/src/router.ts`（基础 router，S3-18/19 加路由）、`apps/web/scripts/export_openapi.py`（确定性 OpenAPI export：`create_app` 工厂注入 settings/dependencies，**import 不连接 DB**，输出 schema 文件）、`apps/web/src/api/api.d.ts`（openapi-typescript 生成 **types**）、`apps/web/src/api/client.ts`（openapi-fetch 类型化 wrapper）、`apps/web/tests/smoke.test.ts`、`apps/web/README.md`

> 路由在后续任务分别加：S3-18 `/crm/opportunities`、S3-19 `/crm/handoffs`。

- [ ] **Step 1：写失败测试（工具链）** — 确定性 OpenAPI export（生成后 diff 无漂移）；`npm run typecheck`、`npm run lint`、`npm run test`（smoke：types 可生成且正确）、`npm run build`；API types 从 openapi-typescript 生成（**types 非假称 client**），openapi-fetch wrapper 类型化请求。
- [ ] **Step 2：运行 RED** — `npm run typecheck && npm run lint && npm run test && npm run build`。
- [ ] **Step 3：最小实现** — 真实依赖（vue/vite/typescript/ant-design-vue/openapi-typescript/openapi-fetch/vitest/eslint）+ vite/tsconfig/index.html + export_openapi.py + 生成 types + client wrapper + vitest/eslint 配置。
- [ ] **Step 4：GREEN** — 同命令通过。
- [ ] **Step 5：统一门禁块** + 前端四件；**同 commit 更新 CI**（Node setup/cache、typecheck/lint/test/build 真实验证）。
- [ ] **Step 6：仅 stage 精确文件** — `git add apps/web/package.json apps/web/package-lock.json apps/web/vite.config.ts apps/web/tsconfig.json apps/web/index.html apps/web/eslint.config.js apps/web/vitest.config.ts apps/web/src/main.ts apps/web/src/App.vue apps/web/src/router.ts apps/web/scripts/export_openapi.py apps/web/src/api/api.d.ts apps/web/src/api/client.ts apps/web/tests/smoke.test.ts apps/web/README.md .github/workflows/ci.yml`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "chore(web): scaffold vue toolchain and typed api client"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-18：web 机会看板页（D4/D9/F15）

**Files**
- Modify: `apps/web/src/router.ts`（加 `/crm/opportunities` route）
- Create: `apps/web/src/views/crm/OpportunityList.vue`、`apps/web/src/views/crm/OpportunityDetail.vue`、`apps/web/src/components/ProvenancePopover.vue`、`apps/web/tests/opportunity-list.test.ts`

- [ ] **Step 1：写失败测试** — 看板列表用生成 types + openapi-fetch 取数、按后端数据渲染（前端不做权限最终裁决）；详情含打分解释与 ProvenancePopover；状态推进/终结调端点、409 展示错误；事实/推断视觉分离；无敏感信息；**route `/crm/opportunities` 保证 build 与 E2E 有真实 URL**。
- [ ] **Step 2：运行 RED** — `npm run typecheck && npm run lint && npm run test && npm run build`。
- [ ] **Step 3：最小实现** — 看板列表/详情/推进组件 + ProvenancePopover + route，按 S3-16 设计稿。
- [ ] **Step 4：GREEN** — 同命令通过。
- [ ] **Step 5：统一门禁块** + 前端四件。
- [ ] **Step 6：仅 stage 精确文件** — `git add apps/web/src/router.ts apps/web/src/views/crm/OpportunityList.vue apps/web/src/views/crm/OpportunityDetail.vue apps/web/src/components/ProvenancePopover.vue apps/web/tests/opportunity-list.test.ts`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(web): add opportunity board page"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-19：web 接管队列页（D4/D9/F15）

**Files**
- Modify: `apps/web/src/router.ts`（加 `/crm/handoffs` route）
- Create: `apps/web/src/views/crm/HandoffQueue.vue`、`apps/web/src/views/crm/HandoffPacketView.vue`、`apps/web/tests/handoff-queue.test.ts`

- [ ] **Step 1：写失败测试** — 队列最久等待最前渲染（requested_at ASC / wait_seconds DESC）；接受按钮调端点、成功刷新、409 显示「已被接受」；接管包视图（完整上下文 + wait_seconds + ProvenancePopover）；页面在 `views/crm`（不新建 views/handoff-queue）；**route `/crm/handoffs` 保证 build 与 E2E 有真实 URL**；无敏感信息。
- [ ] **Step 2：运行 RED** — 前端四件。
- [ ] **Step 3：最小实现** — 接管队列页 + 接管包视图 + route，按 S3-16 设计稿。
- [ ] **Step 4：GREEN** — 同命令通过。
- [ ] **Step 5：统一门禁块** + 前端四件。
- [ ] **Step 6：仅 stage 精确文件** — `git add apps/web/src/router.ts apps/web/src/views/crm/HandoffQueue.vue apps/web/src/views/crm/HandoffPacketView.vue apps/web/tests/handoff-queue.test.ts`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "feat(web): add handoff queue page"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-20：演示脚本（D9/R15）

**Files**
- Create: `scripts/demo_opportunity_board.py`、`tests/integration/test_demo_opportunity_board.py`

- [ ] **Step 1：写失败测试** — 子进程运行（最小 env 仅 DATABASE_URL、sys.executable、cwd 仓库根、失败消息脱敏、stdout/stderr 不含 db_url）：人工造 **5 条严格 Validated Need**（逐条经 operator 适配器 + `ValidatedNeedEvidence` 建机会）→ Territory 分配两个员工 → 队列最久等待最前 → 触发单条 T1/T2 升级（结构化日志 + `handoff_escalations` 审计）→ 断言机会 state/loss 字段真实、升级审计 level 存在；不得硬编码假输出。
- [ ] **Step 2：运行 RED** — `pytest tests/integration/test_demo_opportunity_board.py -q -W error`。
- [ ] **Step 3：最小实现** — `demo_opportunity_board.py`（连接串仅 env、UTC now、唯一 tenant、真实 service/UoW/engine/notification）。
- [ ] **Step 4：GREEN** — 同命令 + `make check` 全绿。
- [ ] **Step 5：统一门禁块**。
- [ ] **Step 6：仅 stage 精确文件** — `git add scripts/demo_opportunity_board.py tests/integration/test_demo_opportunity_board.py`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "docs(opportunities): add slice 3 board demo"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

### 任务 S3-21：E2E 验收（D9/R15/F16）

**Files**
- Create: `tests/e2e/test_board_e2e.py`、`tests/e2e/conftest.py`
- Modify: `pyproject.toml`（dev 依赖加 `playwright`）、`.github/workflows/ci.yml`（加 Playwright 浏览器安装 + `pytest tests/e2e`）

> 不使用 `apps/web/playwright.config.ts`（Python Playwright 不需要，避免 stage 遗漏）。服务统一 `uvicorn apps.api.main:create_app --factory`。

- [ ] **Step 1：写失败测试** — TestClient 不给浏览器访问、npm test 不假装跑 Python Playwright；e2e 用真实栈：testcontainers Postgres（迁移到 head）→ 动态端口起 `uvicorn apps.api.main:create_app --factory` → Vite preview/dev → pytest Playwright Chromium 访问 `views/crm` 页面（看板列表→详情 ProvenancePopover→状态推进→队列最久等待最前→接受→刷新）；显式动态端口、健康检查、finally 回收服务/容器；最小 env、stdout/stderr 不泄 DSN；**e2e 初始化 5 条数据经真实 API/fixture**，不写假前端响应；断言来自真实 API 与 DB。
- [ ] **Step 2：运行 RED** — `pytest tests/e2e -q -W error`。
- [ ] **Step 3：最小实现** — e2e conftest（容器/服务装配与回收）+ 测试 + pyproject 加 playwright dev dep + CI（`python -m playwright install --with-deps chromium`、显式 `pytest tests/e2e`）。
- [ ] **Step 4：GREEN** — 同命令通过。
- [ ] **Step 5：统一门禁块** + 前端四件。
- [ ] **Step 6：仅 stage 精确文件** — `git add tests/e2e/test_board_e2e.py tests/e2e/conftest.py pyproject.toml .github/workflows/ci.yml`
- [ ] **Step 7：commit + push + 核对** —
```bash
git commit -m "test(e2e): opportunity board and handoff queue end to end"
git push
git rev-parse HEAD
git ls-remote origin refs/heads/codex/phase1-implementation
# CI watch：见「门禁命令前缀」节的固定块
```

---

## 非范围（Slice 3 明确不做）

通知投递到真实外部渠道（邮件/macOS/企微，仅结构化日志；Slice 4 接渠道）；发件身份/Campaign/回复识别（后续切片）；需求发现/回复资格（6/7 切片）；寻源/成本/报价自动化；认证/SSO/多租户开关（单租户断言 + dev-mode identity）；AI 模糊绩效分/持续工作量再平衡（D8）；Temporal/图库/桌面端/WhatsApp；模型/LLM 调用；除 crm 外的 14 个 docstring stub router 不挂载实现。

## 风险与说明

- workflow/outbox/通知/scheduler/human_handoff 是本切片最大新增（D3），拆五个任务逐项可验收。
- opportunities 授权契约（S3-5）与 Validated Need 证据（S3-6）改全部 service 签名——**同 commit 适配 rg 确认的调用方并列出精确文件**。
- 迁移顺序固定 0003→0007（R16），每任务只建自己的表；0005 **自管 guard**，不修改已发布 0002。
- 前端从零建工具链（S3-17）且受设计关卡（S3-16）约束；E2E 用真实服务栈（R15/F16）。
- 敏感扫描（S3-1）全切片强制执行：默认 tracked（Makefile/CI），`--staged` 仅提交前 add 后；只匹配高置信凭证形态，允许占位符/scheme-only 字面量；结果只输出 path/line/kind。

## 计划自检（F17，计划完成后执行）

- `git diff --check` 无问题；仅一个 untracked 计划文件。
- 逐任务 Files 与 Step 6 `git add` 完全一致；无「如有必要」占位措辞、无重复 Files、无回改 0002、无 scan-before-stage（统一门禁块顺序已固定）、无 wait ascending（统一 `requested_at ASC` ≡ `wait_seconds DESC`）、无 LOW_MID gate（≥ CUSTOMER_INTEREST_REPLY）、无 `views/handoff-queue`（均在 `views/crm`）。
