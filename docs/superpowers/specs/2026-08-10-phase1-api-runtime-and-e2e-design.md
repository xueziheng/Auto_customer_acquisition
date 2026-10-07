# Phase 1 API Runtime 与真实浏览器 E2E 设计

**状态：** 已接受

**日期：** 2026-08-10

**适用范围：** Slice 3 收口；不改变 Phase 1 单租户范围

**批准方案：** A——先补正式、失败关闭的 Phase 1 runtime composition，再做真实 E2E

## 1. 背景与已证实的问题

Slice 3 的域服务、PostgreSQL 仓储、工作流、CRM API、Vue 机会看板和接管队列已经分别通过测试，但它们还没有形成可启动的真实应用栈。

当前 `apps.api.main:create_app()` 零参数构造 `UnconfiguredApiDependencies`。这是刻意的安全行为：OpenAPI 导出不读环境、不连接数据库，未配置业务请求固定返回 503。原 S3-21 计划却要求用 `uvicorn apps.api.main:create_app --factory` 跑真实 CRM E2E；该命令只能启动未配置应用，不能访问真实业务依赖。

只在 `tests/e2e` 内 monkeypatch 依赖或启动假 API，会保留生产装配缺口，不能证明 PostgreSQL、HTTP 和浏览器形成了闭环，因此不采用。

审计还确认了一个 runtime 上线前必须封闭的写侧权限缺口：部分机会写操作虽然调用 typed authorizer，但 authorizer 只看到 actor/action/scope，不知道目标资源；`assign`、`transition`、`mark_lost`、`mark_won`、`request_handoff`、`accept_handoff` 必须在领域服务内读取目标资源并执行 owner/country/category ABAC，不能把“知道资源 ID”当成写权限。

## 2. 目标

1. 提供可由 Uvicorn factory 启动的、显式配置且失败关闭的 Phase 1 API runtime。
2. 保留零参数 `create_app()` 的纯 OpenAPI / 未配置语义，不让导出类型时接触数据库或环境凭证。
3. 把真实 SQLAlchemy engine、request-scoped EmployeeService、OpportunityService、WorkflowEngine、OutboxDeliverer、NotificationRouter 和 durable dedup store 接入同一个生命周期。
4. 提供默认拒绝、逐角色逐 action 的具体授权策略，并补齐写侧资源 ABAC。
5. 提供精确 CORS allowlist、存活检查和真实数据库 readiness。
6. 用 Python Playwright Chromium 验证 PostgreSQL → Uvicorn → Vite → 浏览器 → API → PostgreSQL 的真实链路；禁止前端路由拦截和假响应。
7. runtime 与 E2E 分成两个独立、可审查、可回退的小提交，每个提交都跑完整门禁并 push。

## 3. 非目标

- 不引入 SSO、OAuth、多租户开关或客户可配置 RBAC；Phase 1 继续使用固定租户加开发身份断言。
- 不把数据库连接串、Token 或 Cookie 暴露给模型、浏览器、前端构建或日志。
- 不实现 Gmail、站内信或其他真实外部通知渠道；本切片仍只注册结构化日志渠道。
- 不把浏览器接受接管误称为验证了 scheduler 的 T1/T2 升级；升级链继续由 S3-20 的真实 PostgreSQL 集成测试证明。
- 不为 E2E 新增创建员工、Territory 或 handoff 的公开 HTTP 管理端点。
- 不改变机会打分、SLA 或重试的业务数字；runtime 必须从显式配置读取，缺失即拒绝启动。

## 4. 方案选择

### 方案 A：正式 Phase 1 runtime composition（采用）

新增独立 `create_runtime_app()`，由严格配置构造真实依赖；零参数 `create_app()` 保持纯净。E2E 启动这个新 factory。

优点：补上真实部署缺口；测试结果能代表实际边界；不破坏 OpenAPI 确定性。代价：需要先完成授权、生命周期和运行配置。

### 方案 B：测试目录内 composition（拒绝）

E2E 能跑，但 production factory 仍然 503；测试夹具会成为第二套未受约束的装配代码。

### 方案 C：前端 fake transport 浏览器回归（拒绝）

只能证明 Vue 交互；现有前端测试已经覆盖这一级，不能作为 S3-21 的真实 E2E。

## 5. 运行入口与配置

### 5.1 两个 factory 的职责

- `apps.api.main:create_app`：纯函数式应用工厂。无参数时继续安装未配置依赖；不读环境、不连接数据库，供 OpenAPI 导出和显式注入测试使用。
- `apps.api.runtime:create_runtime_app`：唯一的 Phase 1 真实运行入口。只在 factory 被 Uvicorn 调用时读取环境并构造依赖，import 模块本身不得连接数据库。

Uvicorn 的真实启动命令固定为：

```bash
uvicorn apps.api.runtime:create_runtime_app --factory --no-access-log
```

### 5.2 必填配置

runtime 使用 Pydantic v2 strict/frozen 配置；不使用隐式默认业务数字。

| 环境变量 | 形状 | 约束 |
|---|---|---|
| `DATABASE_URL` | 非空字符串，内部立即包成 `SecretStr` | 只传给 SQLAlchemy；不得出现在 repr、日志、HTTP 或异常文本 |
| `TRADEOS_TENANT_ID` | 非空、无边界空白字符串 | 固定单租户断言 |
| `TRADEOS_DEV_MODE` | 精确 `true` | 当前没有正式认证；其他值拒绝启动，不能启动一个所有请求都 403 的假健康进程 |
| `TRADEOS_CORS_ALLOWED_ORIGINS` | JSON string array | 每项必须是规范化 `http://` 或 `https://` origin；禁止 `*`、路径、query、fragment、userinfo、重复值 |
| `TRADEOS_API_RETRY_AFTER_SECONDS` | 正整数 | HTTP 503 的 `Retry-After` |
| `TRADEOS_HANDOFF_POLICY` | JSON object | 必含正整数 `sla_seconds`、`backlog_threshold`、`t1_seconds`、`t2_seconds` |
| `TRADEOS_SCORING_POLICY` | JSON object | 必含非空 `version`、ISO currency、至少一个严格升序 Decimal string `value_band_boundaries`、完整键 `1..7` 的 `bucket_map` |
| `TRADEOS_OUTBOX_MAX_ATTEMPTS` | 正整数 | Outbox 瞬时失败耗尽上限 |

配置错误只抛固定、脱敏的启动错误；日志只记录配置项名称和错误类别，不记录值。`infra/.env.example` 只放不可用占位符和 JSON 形状示例，不放真实连接串或替用户选择的业务阈值。

仓库当前缺少根规则宣称应存在的 `infra/AGENTS.md`。修改 `infra/.env.example` 前，runtime 小任务必须先补该目录规则文件，内容只能收紧根边界：infra 只实现部署/持久化适配，不放业务规则，不记录凭证，所有查询带 tenant filter；不能靠缺少就近规则继续扩散配置。

## 6. 授权设计

### 6.1 具体 authorizer 的归属

角色/action 规则属于各自领域，不写进 router 或 runtime `if`。机会域和员工域各提供一个 Phase 1 concrete authorizer；它们先验证 typed action、角色、scope 与组合是否精确匹配，未列出的组合一律 `PermissionDenied`。runtime 只实例化它们。

### 6.2 机会域最小权限矩阵

| actor | scope | 允许 action |
|---|---|---|
| `sales` | `SELF` | `OPPORTUNITY_READ`、`OPPORTUNITY_LIST`、`OPPORTUNITY_TRANSITION`、`OPPORTUNITY_MARK_LOST`、`HANDOFF_READ`、`HANDOFF_QUEUE_READ`、`HANDOFF_ACCEPT` |
| `manager` | `MANAGER` | 与 sales 相同，资源范围仅限 `allowed_owners/countries/categories` |
| `boss` | `TENANT` | 上述 action，加 `OPPORTUNITY_CREATE`、`OPPORTUNITY_ASSIGN`、`HANDOFF_REQUEST`、`LOSS_REASON_READ` |
| `system` | `SYSTEM` | 仅 `HANDOFF_REQUEST`、`HANDOFF_ESCALATION_RECORD` |

`OPPORTUNITY_MARK_WON` 在本 runtime 中对所有角色拒绝；赢单需要人工审批链，当前 CRM 没有该端点。任何未来新增 action 在矩阵更新前默认拒绝。

HTTP `POST /crm/opportunities` 的第一道 gate 同步收紧为 `boss`；否则第二道 domain authorizer 虽能拒绝，却会留下误导性的公开契约。其余 CRM 端点保持当前角色集合，并由资源 ABAC 再收窄。

### 6.3 员工域最小权限矩阵

| actor | scope | 允许 action |
|---|---|---|
| runtime identity/flow `system` | `SYSTEM` | `EMPLOYEE_READ`、`EMPLOYEE_LIST` |
| `boss` | `TENANT` | `OWNERSHIP_READ`、`OWNERSHIP_LOCK`、`OWNERSHIP_TRANSFER`、`TERRITORY_APPLY`、`EMPLOYEE_READ`、`EMPLOYEE_LIST`、`ASSIGNMENT_LIST` |

manager/sales 的员工域 action 在当前 API 中没有必要，默认拒绝。未来 Team/Territory API 必须以自己的任务扩展矩阵并先写测试。

### 6.4 写侧资源 ABAC

领域服务的写操作遵循统一顺序：

```text
typed authorizer.require
→ tenant-filtered 读取目标资源
→ owner/country/category 资源 ABAC
→ 状态/输入校验
→ 原子写入与 outbox
→ 唯一一条 allow audit
```

拒绝路径不得在进入 UoW 前写 allow audit。跨租户、缺失关联机会、handoff 与 opportunity 标识不一致都失败关闭，并沿用固定安全审计/告警。

`assign` 在写入前校验目标 owner 在 actor 的 `allowed_owners` 内；`transition`、`mark_lost`、`mark_won`、`request_handoff` 校验目标 opportunity；`accept_handoff` 校验 handoff、关联 opportunity、assigned owner，并要求非 system 的 `accepted_by == EmployeeId(actor.actor_id)`。这些校验由真实 PostgreSQL 反例测试证明，不能只用 permissive fake。

## 7. 依赖装配与生命周期

`create_runtime_app()` 只构造一个 AsyncEngine 和一个 `async_sessionmaker(expire_on_commit=False)`，所有仓储共享该 factory；禁止 `session_factory(url)` 隐式产生第二个无法 dispose 的 engine。

### 7.1 request-scoped EmployeeService

每次调用 provider 都新建 AsyncSession，构造 `EmployeeRepositoryImpl`、`TerritoryRepositoryImpl`、`OwnershipRepositoryImpl` 与 `EmployeeServiceImpl`：

- 正常退出 commit；
- 任意 `BaseException` rollback 后原样传播；
- finally close；
- service 实例绝不挂成 app singleton。

### 7.2 Opportunity 与工作流组件

- OpportunityService 使用真实 `SqlAlchemyOpportunityUnitOfWork`、`OpportunityScorerImpl`、显式 scoring/handoff policy、具体 authorizer 和 `StandardAuditLogger`。
- WorkflowEngine 使用 `PostgresWorkflowEngine` 与完整 human-handoff step handler registry。
- OutboxDeliverer 使用同一 factory、固定 tenant、显式 max attempts，并通过 `register_human_handoff` 注册 `HandoffRequested/HandoffAccepted` handlers；禁止空 registry。
- NotificationRouter 使用 `PostgresNotificationDedupStore` 与 structured-log-only routing policy，只注册 `StructuredLogChannel`。该选择是 Slice 3 的明确范围，不代表外部渠道已完成。
- flow 的员工读取适配器每次进入真实 EmployeeService scope，不保留 session-bound singleton。

### 7.3 FastAPI lifespan

startup：

1. 建立数据库连接；
2. 读取 Alembic 当前 revision 并与本地唯一 head 精确比较；
3. 不一致或数据库不可用时使 lifespan 启动失败，Uvicorn 不开始接流量；
4. 不自动执行迁移。

shutdown：无论正常退出还是取消，都 `await engine.dispose()`；dispose 失败只记固定中文日志和错误类型，不回显 DSN。

## 8. HTTP 边界、CORS 与健康检查

runtime 在 `create_app()` 构造期间把 CORS middleware 放在 `SafeUnhandledExceptionMiddleware` 内侧，确保安全异常边界仍是最外层 user middleware。

CORS：

- `allow_origins` 仅来自精确配置；
- `allow_methods=["GET", "POST", "OPTIONS"]`；
- `allow_headers=["Content-Type", "X-Tenant-Id", "X-Employee-Id"]`；
- `allow_credentials=False`；
- 不反射任意 Origin。

健康端点仍受租户断言，监控方必须发送唯一正确的 `X-Tenant-Id`：

- `GET /health/live`：只返回固定 `{"status":"live"}`，证明事件循环可处理请求；不查数据库。
- `GET /health/ready`：执行 `SELECT 1`，成功返回固定 `{"status":"ready"}`；失败返回脱敏 503 `service_unavailable`，不含异常、SQL 或 DSN。

readiness 不替代 startup migration check；它用于运行期连接故障探测。

## 9. 真实 E2E 数据流

### 9.1 服务拓扑

```text
testcontainers PostgreSQL（Alembic head）
          ↑
configured Uvicorn runtime（动态端口）
          ↑ 精确 CORS origin
Vite dev server（动态端口 + build-time API/tenant/employee env）
          ↑
Python Playwright Chromium
```

测试禁止 `page.route`、请求拦截、fake fetch、TestClient 和 ASGITransport。

### 9.2 数据准备

1. e2e fixture 在隔离 tenant 下只直接 seed 运行前置数据：4 个 EmployeeRow 和 2 个 TerritoryAssignmentRow；这是测试装配，不是业务结果。
2. 启动 runtime 后，用真实网络 HTTP `POST /crm/opportunities` 创建 5 条带完整 ValidatedNeedEvidence/Provenance 的机会；身份为 boss。
3. 通过独立数据库 session 证明 5 条机会、两个 owner、5 个 score snapshot、ownership locks、provenance 与 outbox 真实存在。
4. 通过具体 authorizer + 真实 OpportunityService 的 fixture 调 public `request_handoff` 创建两条 handoff；禁止直接插 handoff、workflow 或 outbox 行。
5. 两条 handoff 使用不同 `requested_at`，数据库和 UI 都必须证明最久等待项在前。

### 9.3 浏览器行为与数据库事实

单个真实浏览器场景至少验证：

- 机会页显示 5 条真实 API 数据；选择详情并展开 ProvenancePopover，六个 provenance 字段来自数据库；
- 合法状态推进后，页面 authoritative refetch 与数据库 state 一致；
- 另一条机会 mark-lost 后，固定 LossReason、`died_at_state`、`closed_by/closed_at` 与 outbox 事件一致；
- 接管队列按 `requested_at ASC`；打开最旧接管包能看到真实关联机会和 provenance；
- 接受后返回 204，队列 authoritative refresh 移除该项并把焦点移到下一项；数据库只存在一个 HandoffAccepted event，`accepted_by` 等于浏览器身份；
- 浏览器控制台无 error，页面没有未处理异常或 DSN/客户原话泄漏到安全错误文案。

测试结束必须在 `finally` 中依次关闭 Playwright context/browser、终止 Vite、终止 Uvicorn、等待后升级 kill，并停止容器；每个等待都有上限。动态端口使用预绑定 socket 选择并要求 Vite `strictPort`，不能默默换端口。

## 10. CI 与依赖

- `pyproject.toml` 的 dev extra 增加 Python `playwright`；不新增 Node Playwright 配置。
- CI 在 Python dev 依赖安装后执行 `python -m playwright install --with-deps chromium`。
- CI 保留现有 Python/Web 门禁，并增加显式 `pytest tests/e2e -q -W error`。默认 `pytest -q` 仍会收集 e2e，因此浏览器安装必须发生在所有 Python 测试之前；显式步骤作为可见验收会重复一次，接受该成本。
- CI timeout 根据本地冷运行证据调整为足以覆盖浏览器安装和两次 E2E，但不得用无限 timeout。
- E2E stdout/stderr 不含 DATABASE_URL；Uvicorn access log 关闭。

## 11. 测试策略

### 11.1 Runtime 单元测试

- 每个缺失、空白、错误类型、未知字段配置都固定失败且不回显值；`SecretStr` repr 不含 DSN。
- CORS 拒绝未知 origin，允许精确 origin；重复 tenant/employee header 继续失败关闭。
- concrete authorizer 对每个允许四元组返回稳定 rule，对错 role/scope/action 默认拒绝。
- `create_app()` 零参数仍不读取环境、不开 socket/engine，OpenAPI 字节确定。

### 11.2 Runtime PostgreSQL 集成测试

- request-scoped employee service 的 commit/rollback/close；不同请求不复用 session。
- startup 接受 Alembic head，拒绝落后、未知和多 head；不自动迁移。
- readiness 真实查询数据库，连接终止后返回安全 503。
- write ABAC 对 self/manager/boss/system 的允许和拒绝用真实行证明；拒绝发生在写入/outbox 前，deny audit 唯一且无 payload。
- lifespan 无论正常、启动失败或取消都 dispose engine。

### 11.3 E2E mutation 强度

测试必须能杀死这些现实回归：API 指向未配置 factory、CORS wildcard/错 origin、前端假数据、列表不是 5 条、provenance 来自常量、状态写入未 refetch、handoff 逆序、accept 只改 DOM 未改 DB、重复 HandoffAccepted、服务退出遗留子进程。

## 12. 交付拆分

### 小任务 1：Phase 1 runtime composition

包含配置、具体 authorizer、写侧资源 ABAC、真实依赖装配、CORS、health/readiness、生命周期、环境样例和对应 unit/integration tests。独立审查、全门禁、提交并 push。

### 小任务 2：S3-21 真实 E2E

包含 `tests/e2e`、Playwright dev dependency 和 CI Chromium/显式 E2E 步骤。独立审查、Python/Web 全门禁、提交并 push。

两个任务之间不得把测试专用 fake 放入 production，也不得为了让 E2E 通过削弱 middleware、authorizer、tenant filter 或浏览器安全策略。

## 13. 验收标准

只有以下证据同时成立，Slice 3 才能标记完成：

1. `create_app()` 无参数仍纯净且业务请求 503；`create_runtime_app()` 在完整显式配置下启动真实依赖，缺配置或迁移不匹配拒绝启动。
2. 所有写操作具有 typed action + resource ABAC + tenant-filtered DB 的两层证明。
3. `make check`、`pytest tests/integration -q -W error`、前端 typecheck/lint/test/build、`pytest tests/e2e -q -W error` 全部新鲜通过。
4. `python3 scripts/check_boundaries.py`、敏感扫描、`git diff --check` 通过。
5. Chromium 场景无请求拦截，浏览器行为与独立数据库 read-back 一致。
6. runtime 与 E2E 各自经过独立 review、精确 stage、普通 commit、push，远端 CI 成功。
