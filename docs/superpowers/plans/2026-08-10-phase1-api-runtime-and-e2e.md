# Phase 1 API Runtime 与真实浏览器 E2E Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立显式配置、默认拒绝、可由 Uvicorn 启动的 Phase 1 API runtime，并用真实 PostgreSQL、Uvicorn、Vite 与 Playwright Chromium 证明机会看板和人工接管闭环。

**Architecture:** 保留 `apps.api.main:create_app()` 的纯 OpenAPI / 未配置 503 语义，新增 `apps.api.runtime:create_runtime_app()` 读取严格环境配置并在一个 lifespan 内装配唯一 AsyncEngine、request-scoped EmployeeService、OpportunityService、workflow、outbox 与结构化日志通知。领域内 concrete authorizer 与机会写侧资源 ABAC 先于任何写入生效；第二阶段通过动态端口启动真实 Uvicorn 与 Vite，不做请求拦截，用独立数据库 read-back 核对浏览器结果。

**Tech Stack:** Python 3.12.13（conda `tradeos-py312`）、FastAPI、Pydantic v2、SQLAlchemy 2.x async + asyncpg、Alembic、PostgreSQL 16 testcontainers、pytest/pytest-asyncio、Vue 3 + TypeScript + Vite、Python Playwright Chromium、GitHub Actions。

## Global Constraints

- 全程使用 `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH"`；不得把本机 Codex 临时路径写进仓库。
- 九条全局硬边界全部生效；尤其凭证绝不进入模型、浏览器、日志或错误文本，金额边界只接受 Decimal string，所有 SQL 显式 tenant filter，依赖方向保持 `apps → workflows → domains → shared`。
- runtime 只从八个必填变量读取业务配置：`DATABASE_URL`、`TRADEOS_TENANT_ID`、`TRADEOS_DEV_MODE`、`TRADEOS_CORS_ALLOWED_ORIGINS`、`TRADEOS_API_RETRY_AFTER_SECONDS`、`TRADEOS_HANDOFF_POLICY`、`TRADEOS_SCORING_POLICY`、`TRADEOS_OUTBOX_MAX_ATTEMPTS`；缺失或格式错误一律拒绝启动。
- `TRADEOS_DEV_MODE` 必须精确等于 `true`；Phase 1 不伪装成已经具备正式认证。
- CORS 禁止 `*`，仅允许规范化 `http://` / `https://` origin；方法固定 `GET/POST/OPTIONS`，请求头固定 `Content-Type/X-Tenant-Id/X-Employee-Id`，credentials 固定 false。
- `create_app()` 零参数导入和调用不得读取环境、建 engine、开 socket 或连接数据库；真实入口固定为 `uvicorn apps.api.runtime:create_runtime_app --factory --no-access-log`。
- startup 只校验数据库可连接且 Alembic current heads 与本地唯一 head 精确一致；不得自动迁移。shutdown 必须 dispose 唯一 engine。
- 机会和员工 concrete authorizer 未列出的 role/action/scope/tenant 组合全部 `PermissionDenied`；`OPPORTUNITY_MARK_WON` 本阶段对所有角色拒绝。
- 写操作顺序固定为 typed require → tenant-filtered load → resource ABAC → state/input validation → atomic write/outbox → 单条 allow audit；拒绝路径无写入、无 outbox、无 allow audit。
- E2E 禁止 `page.route`、请求拦截、fake fetch、`TestClient` 和 `ASGITransport`；直接 seed 只允许 4 个 `EmployeeRow` 与 2 个 `TerritoryAssignmentRow`。
- 不改 UI 视觉和交互规格，本计划只接通真实运行栈；因此本轮不重新发散 Creative Production 方案。
- 每个小任务完成后独立 review、精确 stage、普通 commit、push，并等待远端 CI 成功；禁止 amend，禁止 push 失败后声称完成。
- 新文件位于 exFAT 时最终使用 `git add --chmod=-x <file>`，cached mode 必须为 `100644`。
- 每次提交前固定运行：目标测试、ruff、mypy、`make check`、integration `-W error`、前端四门禁、boundary、sensitive scan、`git diff --check`；stage 后再跑 staged sensitive 与 cached diff check。

---

## File Structure

### Runtime 小任务

- `infra/AGENTS.md`：补齐 infra 就近规则，只收紧部署、凭证与 tenant 约束。
- `infra/.env.example`：只给 runtime 变量名和不可用 JSON 形状，不选择业务阈值。
- `domains/opportunities/permissions.py`：新增 tenant-bound `Phase1OpportunityAuthorizer`。
- `domains/employees/permissions.py`：新增 tenant-bound `Phase1EmployeeAuthorizer` 与安全 `StandardAuditLogger`。
- `domains/opportunities/service_impl.py`：六个机会写操作补 tenant-filtered resource ABAC，并把 allow audit 移到事务成功后。
- `workflows/human_handoff/flow.py`：把 flow 对员工域的依赖缩窄为只含 `get_employee` 的 Protocol，允许安全的 request-scope reader。
- `apps/api/runtime_config.py`：严格、冻结、脱敏的环境配置解析与 domain policy 转换。
- `apps/api/composition/runtime.py`：唯一 engine 的所有真实依赖装配；不读取环境、不启动 IO。
- `apps/api/routers/health.py`：tenant assertion 内的 `/health/live` 与 `/health/ready`。
- `apps/api/runtime.py`：环境读取、engine 建立、Alembic head 校验、lifespan dispose 和真实 app factory。
- `apps/api/main.py`：给通用 app factory 注入 lifespan、精确 CORS 与可选 health router，同时保留 zero-arg 纯净性。
- `apps/api/routers/crm.py`：创建机会第一道 gate 收紧为 boss。
- `tests/unit/test_phase1_authorizers.py`：两域授权矩阵与默认拒绝。
- `tests/unit/test_api_runtime_config.py`：配置形状、Decimal、SecretStr 与 origin 校验。
- `tests/unit/test_api_runtime.py`：zero-arg 纯净、middleware 顺序、CORS 与固定健康响应。
- `tests/integration/test_opportunity_write_abac.py`：真实 PostgreSQL 写侧 ABAC、零写与审计顺序。
- `tests/integration/test_api_runtime.py`：真实迁移 head、readiness、request scope、workflow/outbox registry 和 dispose。
- `tests/unit/test_api_app.py`、`tests/unit/test_crm_router.py`、`tests/unit/test_human_handoff_flow.py`、`tests/unit/test_opportunities_permissions.py`、`tests/unit/test_opportunities_service.py`、`tests/unit/test_opportunities_handoff.py`、`tests/integration/test_crm_api.py`：适配公开工厂参数、boss-only create、写侧 load-before-write 与窄 employee reader 契约。

### E2E 小任务

- `tests/e2e/conftest.py`：隔离 PostgreSQL、动态端口、迁移、seed、受限子进程与 bounded cleanup。
- `tests/e2e/test_opportunity_board.py`：真实网络数据准备、Chromium 行为与独立 DB read-back。
- `pyproject.toml`：dev extra 增加 Python Playwright，并登记 `e2e` marker。
- `.github/workflows/ci.yml`：安装 Chromium、显式运行 E2E，并设置有限 CI timeout。

---

### Task 1: 正式 Phase 1 Runtime Composition

**Files:**
- Create: `infra/AGENTS.md`
- Create: `apps/api/runtime_config.py`
- Create: `apps/api/composition/runtime.py`
- Create: `apps/api/routers/health.py`
- Create: `apps/api/runtime.py`
- Create: `tests/unit/test_phase1_authorizers.py`
- Create: `tests/unit/test_api_runtime_config.py`
- Create: `tests/unit/test_api_runtime.py`
- Create: `tests/integration/test_opportunity_write_abac.py`
- Create: `tests/integration/test_api_runtime.py`
- Modify: `infra/.env.example`
- Modify: `domains/opportunities/permissions.py`
- Modify: `domains/employees/permissions.py`
- Modify: `domains/opportunities/service_impl.py`
- Modify: `workflows/human_handoff/flow.py`
- Modify: `apps/api/main.py`
- Modify: `apps/api/routers/crm.py`
- Modify: `tests/unit/test_api_app.py`
- Modify: `tests/unit/test_crm_router.py`
- Modify: `tests/unit/test_human_handoff_flow.py`
- Modify: `tests/unit/test_opportunities_permissions.py`
- Modify: `tests/unit/test_opportunities_service.py`
- Modify: `tests/unit/test_opportunities_handoff.py`
- Modify: `tests/integration/test_crm_api.py`

**Interfaces:**
- Consumes: `ConfiguredApiDependencies`, `ApiSettings`, `SqlAlchemyOpportunityUnitOfWork`, `OpportunityServiceImpl`, `EmployeeServiceImpl`, `PostgresWorkflowEngine`, `OutboxDeliverer`, `register_human_handoff`, `NotificationRouter`, `PostgresNotificationDedupStore`, `StructuredLogChannel`。
- Produces: `Phase1RuntimeSettings.from_environ(environ: Mapping[str, str]) -> Phase1RuntimeSettings`；`Phase1OpportunityAuthorizer(tenant_id)`；`Phase1EmployeeAuthorizer(tenant_id)`；`HumanHandoffEmployeeReader`；`build_phase1_dependencies(settings, factory, *, now) -> ConfiguredApiDependencies`；`DatabaseReadinessProbe.is_ready() -> bool`；`assert_database_schema_current(engine) -> None`；`create_runtime_app() -> FastAPI`。
- Later task contract: E2E 只启动 `apps.api.runtime:create_runtime_app`，并复用 `Phase1RuntimeSettings` 与 `build_phase1_dependencies` 创建真实 handoff fixture；不得自行复制 authorizer 或 service wiring。

- [ ] **Step 1: 写两域具体授权矩阵的失败测试**

在 `tests/unit/test_phase1_authorizers.py` 用真实 typed actor/scope/action 覆盖所有明确 allow 与四类 deny：错 tenant、错 role、错 scope、未列 action。测试使用稳定 rule 文本，不用 permissive fake：

```python
def _opportunity_scope(
    level: ScopeLevel, employee_id: EmployeeId
) -> OpportunityScope:
    if level is ScopeLevel.SELF:
        return OpportunityScope(
            level=level,
            allowed_owners=frozenset({employee_id}),
        )
    if level is ScopeLevel.MANAGER:
        return OpportunityScope(
            level=level,
            allowed_owners=frozenset({employee_id}),
        )
    return OpportunityScope(level=level)


def _opportunity_actor_for_role(role: str) -> OpportunityActor:
    levels = {
        "sales": ScopeLevel.SELF,
        "manager": ScopeLevel.MANAGER,
        "boss": ScopeLevel.TENANT,
        "system": ScopeLevel.SYSTEM,
    }
    employee_id = EmployeeId("emp-sales")
    scope = _opportunity_scope(levels[role], employee_id)
    return OpportunityActor(str(employee_id), scope, role)


@pytest.mark.parametrize(
    ("role", "level", "action"),
    [
        ("sales", ScopeLevel.SELF, OpportunityAction.OPPORTUNITY_READ),
        ("sales", ScopeLevel.SELF, OpportunityAction.OPPORTUNITY_LIST),
        ("sales", ScopeLevel.SELF, OpportunityAction.OPPORTUNITY_TRANSITION),
        ("sales", ScopeLevel.SELF, OpportunityAction.OPPORTUNITY_MARK_LOST),
        ("sales", ScopeLevel.SELF, OpportunityAction.HANDOFF_READ),
        ("sales", ScopeLevel.SELF, OpportunityAction.HANDOFF_QUEUE_READ),
        ("sales", ScopeLevel.SELF, OpportunityAction.HANDOFF_ACCEPT),
        ("manager", ScopeLevel.MANAGER, OpportunityAction.OPPORTUNITY_READ),
        ("manager", ScopeLevel.MANAGER, OpportunityAction.OPPORTUNITY_LIST),
        ("manager", ScopeLevel.MANAGER, OpportunityAction.OPPORTUNITY_TRANSITION),
        ("manager", ScopeLevel.MANAGER, OpportunityAction.OPPORTUNITY_MARK_LOST),
        ("manager", ScopeLevel.MANAGER, OpportunityAction.HANDOFF_READ),
        ("manager", ScopeLevel.MANAGER, OpportunityAction.HANDOFF_QUEUE_READ),
        ("manager", ScopeLevel.MANAGER, OpportunityAction.HANDOFF_ACCEPT),
        ("boss", ScopeLevel.TENANT, OpportunityAction.OPPORTUNITY_READ),
        ("boss", ScopeLevel.TENANT, OpportunityAction.OPPORTUNITY_LIST),
        ("boss", ScopeLevel.TENANT, OpportunityAction.OPPORTUNITY_TRANSITION),
        ("boss", ScopeLevel.TENANT, OpportunityAction.OPPORTUNITY_MARK_LOST),
        ("boss", ScopeLevel.TENANT, OpportunityAction.HANDOFF_READ),
        ("boss", ScopeLevel.TENANT, OpportunityAction.HANDOFF_QUEUE_READ),
        ("boss", ScopeLevel.TENANT, OpportunityAction.HANDOFF_ACCEPT),
        ("boss", ScopeLevel.TENANT, OpportunityAction.OPPORTUNITY_CREATE),
        ("boss", ScopeLevel.TENANT, OpportunityAction.OPPORTUNITY_ASSIGN),
        ("boss", ScopeLevel.TENANT, OpportunityAction.HANDOFF_REQUEST),
        ("boss", ScopeLevel.TENANT, OpportunityAction.LOSS_REASON_READ),
        ("system", ScopeLevel.SYSTEM, OpportunityAction.HANDOFF_REQUEST),
        ("system", ScopeLevel.SYSTEM, OpportunityAction.HANDOFF_ESCALATION_RECORD),
    ],
)
def test_phase1_opportunity_authorizer_allows_only_explicit_matrix(
    role: str, level: ScopeLevel, action: OpportunityAction
) -> None:
    tenant = TenantId("tenant-runtime")
    scope = _opportunity_scope(level, EmployeeId("emp-sales"))
    actor = OpportunityActor("emp-sales", scope, role)
    rule = Phase1OpportunityAuthorizer(tenant).require(actor, action, scope, tenant)
    assert rule == f"phase1:{role}:{level.value}:{action.value}"


@pytest.mark.parametrize("role", ["sales", "manager", "boss", "system"])
def test_phase1_authorizer_denies_mark_won_for_every_role(role: str) -> None:
    tenant = TenantId("tenant-runtime")
    actor = _opportunity_actor_for_role(role)
    with pytest.raises(PermissionDenied, match="Phase 1 机会授权拒绝"):
        Phase1OpportunityAuthorizer(tenant).require(
            actor,
            OpportunityAction.OPPORTUNITY_MARK_WON,
            actor.scope,
            tenant,
        )


@pytest.mark.parametrize(
    ("actor", "action", "tenant"),
    [
        (_opportunity_actor_for_role("sales"), OpportunityAction.OPPORTUNITY_CREATE, TenantId("tenant-runtime")),
        (_opportunity_actor_for_role("manager"), OpportunityAction.OPPORTUNITY_ASSIGN, TenantId("tenant-runtime")),
        (_opportunity_actor_for_role("system"), OpportunityAction.OPPORTUNITY_READ, TenantId("tenant-runtime")),
        (_opportunity_actor_for_role("boss"), OpportunityAction.OPPORTUNITY_READ, TenantId("tenant-other")),
    ],
)
def test_phase1_opportunity_authorizer_denies_unlisted_or_cross_tenant(
    actor: OpportunityActor,
    action: OpportunityAction,
    tenant: TenantId,
) -> None:
    with pytest.raises(PermissionDenied, match="Phase 1 机会授权拒绝"):
        Phase1OpportunityAuthorizer(TenantId("tenant-runtime")).require(
            actor,
            action,
            actor.scope,
            tenant,
        )


def test_phase1_opportunity_authorizer_denies_scope_different_from_actor() -> None:
    actor = _opportunity_actor_for_role("boss")
    with pytest.raises(PermissionDenied, match="Phase 1 机会授权拒绝"):
        Phase1OpportunityAuthorizer(TenantId("tenant-runtime")).require(
            actor,
            OpportunityAction.OPPORTUNITY_READ,
            OpportunityScope(level=ScopeLevel.SYSTEM),
            TenantId("tenant-runtime"),
        )


@pytest.mark.parametrize(
    ("role", "scope", "action"),
    [
        ("system", EmployeeScope.SYSTEM, EmployeeAction.EMPLOYEE_READ),
        ("system", EmployeeScope.SYSTEM, EmployeeAction.EMPLOYEE_LIST),
        ("boss", EmployeeScope.TENANT, EmployeeAction.OWNERSHIP_READ),
        ("boss", EmployeeScope.TENANT, EmployeeAction.OWNERSHIP_LOCK),
        ("boss", EmployeeScope.TENANT, EmployeeAction.OWNERSHIP_TRANSFER),
        ("boss", EmployeeScope.TENANT, EmployeeAction.TERRITORY_APPLY),
        ("boss", EmployeeScope.TENANT, EmployeeAction.EMPLOYEE_READ),
        ("boss", EmployeeScope.TENANT, EmployeeAction.EMPLOYEE_LIST),
        ("boss", EmployeeScope.TENANT, EmployeeAction.ASSIGNMENT_LIST),
    ],
)
def test_phase1_employee_authorizer_allows_only_explicit_matrix(
    role: str, scope: EmployeeScope, action: EmployeeAction
) -> None:
    tenant = TenantId("tenant-runtime")
    actor = EmployeeActor("actor-runtime", scope, role)
    rule = Phase1EmployeeAuthorizer(tenant).require(actor, action, scope, tenant)
    assert rule == f"phase1:{role}:{scope.value}:{action.value}"


@pytest.mark.parametrize(
    ("role", "scope", "action", "tenant"),
    [
        ("sales", EmployeeScope.SELF, EmployeeAction.EMPLOYEE_READ, TenantId("tenant-runtime")),
        ("manager", EmployeeScope.MANAGER, EmployeeAction.OWNERSHIP_TRANSFER, TenantId("tenant-runtime")),
        ("system", EmployeeScope.SYSTEM, EmployeeAction.OWNERSHIP_LOCK, TenantId("tenant-runtime")),
        ("boss", EmployeeScope.TENANT, EmployeeAction.EMPLOYEE_READ, TenantId("tenant-other")),
    ],
)
def test_phase1_employee_authorizer_denies_unlisted_or_cross_tenant(
    role: str,
    scope: EmployeeScope,
    action: EmployeeAction,
    tenant: TenantId,
) -> None:
    actor = EmployeeActor("actor-runtime", scope, role)
    with pytest.raises(PermissionDenied, match="Phase 1 员工授权拒绝"):
        Phase1EmployeeAuthorizer(TenantId("tenant-runtime")).require(
            actor,
            action,
            scope,
            tenant,
        )
```

- [ ] **Step 2: 运行授权 RED**

Run:

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_phase1_authorizers.py -q -W error
```

Expected: collection 正常，失败只因 `Phase1OpportunityAuthorizer`、`Phase1EmployeeAuthorizer` 与员工 `StandardAuditLogger` 尚不存在；不得出现 import/PATH/warning 假失败。

- [ ] **Step 3: 在各自领域实现 tenant-bound、默认拒绝的具体 authorizer**

`domains/opportunities/permissions.py` 的实现必须精确比较 bound tenant、`scope == actor.scope`、role 与 level，再查固定 allow set：

```python
_OPPORTUNITY_STAFF_ACTIONS = frozenset(
    {
        OpportunityAction.OPPORTUNITY_READ,
        OpportunityAction.OPPORTUNITY_LIST,
        OpportunityAction.OPPORTUNITY_TRANSITION,
        OpportunityAction.OPPORTUNITY_MARK_LOST,
        OpportunityAction.HANDOFF_READ,
        OpportunityAction.HANDOFF_QUEUE_READ,
        OpportunityAction.HANDOFF_ACCEPT,
    }
)
_OPPORTUNITY_BOSS_ACTIONS = _OPPORTUNITY_STAFF_ACTIONS | frozenset(
    {
        OpportunityAction.OPPORTUNITY_CREATE,
        OpportunityAction.OPPORTUNITY_ASSIGN,
        OpportunityAction.HANDOFF_REQUEST,
        OpportunityAction.LOSS_REASON_READ,
    }
)


class Phase1OpportunityAuthorizer:
    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id

    def require(
        self,
        actor: Actor,
        action: OpportunityAction,
        scope: OpportunityScope,
        tenant_id: TenantId,
    ) -> str:
        allowed = {
            ("sales", ScopeLevel.SELF): _OPPORTUNITY_STAFF_ACTIONS,
            ("manager", ScopeLevel.MANAGER): _OPPORTUNITY_STAFF_ACTIONS,
            ("boss", ScopeLevel.TENANT): _OPPORTUNITY_BOSS_ACTIONS,
            ("system", ScopeLevel.SYSTEM): frozenset(
                {
                    OpportunityAction.HANDOFF_REQUEST,
                    OpportunityAction.HANDOFF_ESCALATION_RECORD,
                }
            ),
        }
        if not isinstance(action, OpportunityAction) or not isinstance(
            scope, OpportunityScope
        ):
            raise PermissionDenied("Phase 1 机会授权拒绝")
        level = scope.level
        key = (actor.role, level)
        if (
            tenant_id != self._tenant_id
            or scope != actor.scope
            or not isinstance(actor.actor_id, str)
            or not actor.actor_id.strip()
            or level is None
            or action not in allowed.get(key, frozenset())
        ):
            raise PermissionDenied("Phase 1 机会授权拒绝")
        return f"phase1:{actor.role}:{level.value}:{action.value}"
```

员工域使用同样结构，精确 allow set 为：

```python
_EMPLOYEE_SYSTEM_ACTIONS = frozenset(
    {EmployeeAction.EMPLOYEE_READ, EmployeeAction.EMPLOYEE_LIST}
)
_EMPLOYEE_BOSS_ACTIONS = frozenset(
    {
        EmployeeAction.OWNERSHIP_READ,
        EmployeeAction.OWNERSHIP_LOCK,
        EmployeeAction.OWNERSHIP_TRANSFER,
        EmployeeAction.TERRITORY_APPLY,
        EmployeeAction.EMPLOYEE_READ,
        EmployeeAction.EMPLOYEE_LIST,
        EmployeeAction.ASSIGNMENT_LIST,
    }
)


class Phase1EmployeeAuthorizer:
    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id

    def require(self, actor, action, scope, tenant_id) -> str:
        allowed = {
            ("system", EmployeeScope.SYSTEM): _EMPLOYEE_SYSTEM_ACTIONS,
            ("boss", EmployeeScope.TENANT): _EMPLOYEE_BOSS_ACTIONS,
        }
        if (
            not isinstance(action, EmployeeAction)
            or not isinstance(scope, EmployeeScope)
            or tenant_id != self._tenant_id
            or scope is not actor.scope
            or not isinstance(actor.actor_id, str)
            or not actor.actor_id.strip()
            or action not in allowed.get((actor.role, scope), frozenset())
        ):
            raise PermissionDenied("Phase 1 员工授权拒绝")
        return f"phase1:{actor.role}:{scope.value}:{action.value}"
```

在 `domains/employees/permissions.py` 同时增加只记录 `actor/action/tenant_id/scope/rule` 的 `StandardAuditLogger`；异常文本与业务 payload 不进入日志。两个 concrete authorizer 均不得继承一个含默认 allow 的基类。

- [ ] **Step 4: 运行授权 GREEN 与局部静态门禁**

Run:

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_phase1_authorizers.py -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  ruff check domains/opportunities/permissions.py domains/employees/permissions.py tests/unit/test_phase1_authorizers.py --no-cache
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  mypy domains/opportunities/permissions.py domains/employees/permissions.py
```

Expected: all pass；测试必须断言 `mark_won` 全拒、manager/sales 员工域 action 全拒、错 tenant 全拒。

- [ ] **Step 5: 写真实 PostgreSQL 写侧 ABAC 与审计顺序失败测试**

在 `tests/integration/test_opportunity_write_abac.py` seed 两租户、两 owner、机会与 requested handoff；用真实 `SqlAlchemyOpportunityUnitOfWork` 和 `OpportunityServiceImpl`。分别执行越权 `assign`、`transition`、`mark_lost`、`mark_won`、`request_handoff`、`accept_handoff`，并在每次调用后从独立 session 核对原行、loss、handoff、provenance 和 outbox 基线未变：

```python
@dataclass(frozen=True)
class _ExactActionAuthorizer:
    actor: OpportunityActor
    action: OpportunityAction
    tenant_id: TenantId

    def require(self, actor, action, scope, tenant_id) -> str:
        if (
            actor != self.actor
            or action is not self.action
            or scope != self.actor.scope
            or tenant_id != self.tenant_id
        ):
            raise PermissionDenied("测试精确授权拒绝")
        return f"test:exact:{action.value}"


@pytest.mark.parametrize(
    "operation",
    ["assign", "transition", "mark_lost", "mark_won", "request_handoff", "accept_handoff"],
)
async def test_write_resource_abac_denies_before_any_durable_effect(
    db_url: str, operation: str
) -> None:
    fixture = await _seed_write_abac_fixture(db_url)
    audit = _Audit()
    actor = fixture.sales_a_actor
    action = _ACTION_BY_OPERATION[operation]
    service = _real_service(
        fixture,
        audit=audit,
        authorizer=_ExactActionAuthorizer(actor, action, fixture.tenant),
    )

    with pytest.raises(PermissionDenied):
        await _invoke_cross_owner_operation(service, fixture, operation)

    after = await _durable_snapshot(fixture)
    assert after == fixture.before
    assert audit.entries == [
        {
            "actor": str(fixture.sales_a),
            "action": action.value,
            "tenant_id": str(fixture.tenant),
            "scope": "self",
            "rule": "deny:abac:owner",
        }
    ]


async def test_accept_requires_actor_to_equal_accepted_by_and_linked_resource_scope(
    db_url: str,
) -> None:
    fixture = await _seed_write_abac_fixture(db_url)
    service = _real_service(fixture, audit=_Audit())
    with pytest.raises(PermissionDenied):
        await service.accept_handoff(
            fixture.tenant,
            fixture.handoff_b,
            fixture.sales_b,
            actor=fixture.sales_a_actor,
        )
    assert await _handoff_state(fixture) == ("requested", None)
    assert await _event_count(fixture, "HandoffAccepted") == 0


async def test_successful_write_audits_once_only_after_commit(db_url: str) -> None:
    fixture = await _seed_write_abac_fixture(db_url)
    audit = _Audit()
    service = _real_service(fixture, audit=audit)
    await service.transition(
        fixture.tenant,
        fixture.opportunity_a,
        OpportunityState.ASSIGNED,
        actor=fixture.sales_a_actor,
    )
    assert await _opportunity_state(fixture) == "assigned"
    assert [entry["rule"] for entry in audit.entries] == [
        "phase1:sales:self:opportunity:transition"
    ]
```

`_ExactActionAuthorizer` 只放行测试声明的唯一 actor/action/scope/tenant 四元组，不是 permissive fake；它使 runtime 默认拒绝的 `mark_won` 也能单独验证 service 的 defense-in-depth resource ABAC。`_durable_snapshot` 必须至少计数/读取 `OpportunityRow.state/owner`、`HandoffRow.state/accepted_by`、`LossRecordRow`、`ProvenanceRecordRow`、`OutboxEventRow`；不以 service 返回值代替数据库事实。另用真实 `Phase1OpportunityAuthorizer` 跑成功 transition 和 HTTP create boss/sales 反例，证明 runtime matrix 与 service 能组合。

- [ ] **Step 6: 运行写侧 ABAC RED**

Run:

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/integration/test_opportunity_write_abac.py -q -W error
```

Expected: 真实 Postgres fixture/migrations 正常；越权操作至少在 owner/resource 断言失败，现有实现会提前写 allow audit，`accept_handoff` 也会错误允许不匹配 accepted_by。若 seed 或 fixture 失败，先只修测试装配并重新取得行为 RED。

- [ ] **Step 7: 按统一顺序补齐六个写操作的资源 ABAC**

在 `domains/opportunities/service_impl.py` 每个方法都先 `_authorize`，再在 tenant-bound UoW 中 load，调用 `_enforce_resource_abac`，完成状态/输入与写入；退出 UoW commit 成功后才 `_audit_allow`。关键结构固定如下：

```python
async def transition(self, tenant_id, opportunity_id, target, *, actor) -> None:
    action = OpportunityAction.OPPORTUNITY_TRANSITION
    rule = self._authorize(actor, action, tenant_id)
    async with self._uow_factory() as uow:
        opp = await uow.opportunities.get(tenant_id, opportunity_id)
        if opp is None:
            raise ValidationError("机会不存在或不属于该租户")
        self._enforce_resource_abac(
            actor,
            owner=opp.owner,
            country=opp.country,
            product_category=opp.product_category,
            tenant_id=tenant_id,
            action=action,
        )
        if target in (OpportunityState.WON, OpportunityState.LOST):
            raise InvalidStateTransition("终态不能走普通 transition")
        if not opp.can_transition_to(target):
            raise InvalidStateTransition("非法机会状态转换")
        if not await uow.opportunities.advance_state(
            tenant_id, opportunity_id, opp.state, target
        ):
            raise InvalidStateTransition("机会状态已被并发修改")
    self._audit_allow(actor, action, tenant_id, rule)
```

`assign` 的 ABAC owner 参数使用目标 `owner`，country/category 来自已加载 opportunity；`mark_lost`/`mark_won` 在 resource ABAC 后再检查 reason/state；`request_handoff` 先加载 opportunity、做 ABAC，再检查 existing pending 与 packet，幂等成功也只在退出事务后写一条 allow；`accept_handoff` 先读取 handoff 与 linked opportunity，校验 tenant/id/state/assigned owner，再做 ABAC，并执行：

```python
if actor.scope.level is not ScopeLevel.SYSTEM and accepted_by != EmployeeId(actor.actor_id):
    self._deny_abac(
        actor,
        OpportunityAction.HANDOFF_ACCEPT,
        tenant_id,
        rule="deny:abac:accepted_by",
        message="接管接受人必须等于当前身份",
    )
```

任何固定错误消息不得包含 opportunity ID、handoff ID、客户原话或数据库异常文本。

- [ ] **Step 8: 运行写侧 ABAC GREEN 与既有机会域回归**

Run:

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/integration/test_opportunity_write_abac.py \
         tests/unit/test_opportunities_permissions.py \
         tests/unit/test_opportunities_service.py \
         tests/unit/test_opportunities_handoff.py \
         tests/integration/test_crm_api.py \
         tests/integration/test_crm_handoff_api.py -q -W error
```

Expected: all pass；若既有 fake 没有新 load 方法，只补 fake 的真实 public contract，不给 production 增 test-only 分支。

- [ ] **Step 9: 写严格 runtime 配置失败测试**

`tests/unit/test_api_runtime_config.py` 定义唯一合法 env，逐项删除、空白、错类型、JSON number Decimal、未知嵌套字段、错误 origin、重复 origin、非正整数，再证明异常/repr 不含 secret marker：

```python
_VALID_ENV = {
    "DATABASE_URL": (
        "postgresql+asyncpg://" + "runtime-user" + ":" + "runtime-secret"
        + "@db.invalid/tradeos"
    ),
    "TRADEOS_TENANT_ID": "tenant-runtime",
    "TRADEOS_DEV_MODE": "true",
    "TRADEOS_CORS_ALLOWED_ORIGINS": '["http://127.0.0.1:4173"]',
    "TRADEOS_API_RETRY_AFTER_SECONDS": "30",
    "TRADEOS_HANDOFF_POLICY": (
        '{"sla_seconds":300,"backlog_threshold":20,"t1_seconds":120,"t2_seconds":180}'
    ),
    "TRADEOS_SCORING_POLICY": (
        '{"version":"phase1-v1","currency":"USD",'
        '"value_band_boundaries":["1000","5000"],'
        '"bucket_map":{"1":"low","2":"low","3":"mid","4":"mid",'
        '"5":"high","6":"high","7":"high"}}'
    ),
    "TRADEOS_OUTBOX_MAX_ATTEMPTS": "3",
}


def test_runtime_settings_parse_exact_configuration_without_exposing_dsn() -> None:
    settings = Phase1RuntimeSettings.from_environ(_VALID_ENV)
    assert settings.tenant_id == "tenant-runtime"
    assert settings.scoring_policy.value_band_boundaries[0].amount == Decimal("1000")
    assert settings.t1 == timedelta(seconds=120)
    assert "runtime-secret" not in repr(settings)


@pytest.mark.parametrize("name", sorted(_VALID_ENV))
def test_every_runtime_variable_is_required_and_error_is_sanitized(name: str) -> None:
    env = dict(_VALID_ENV)
    value = env.pop(name)
    with pytest.raises(RuntimeConfigurationError, match="API runtime 配置无效") as exc:
        Phase1RuntimeSettings.from_environ(env)
    assert value not in str(exc.value)


@pytest.mark.parametrize(
    ("origin_json", "valid"),
    [
        ('["http://127.0.0.1:4173"]', True),
        ('["*"]', False),
        ('["http://127.0.0.1:4173/path"]', False),
        ('["http://user@127.0.0.1:4173"]', False),
        ('["http://127.0.0.1:4173","http://127.0.0.1:4173"]', False),
        ("[]", False),
    ],
)
def test_cors_origin_is_canonical_and_exact(origin_json: str, valid: bool) -> None:
    env = {**_VALID_ENV, "TRADEOS_CORS_ALLOWED_ORIGINS": origin_json}
    if valid:
        assert Phase1RuntimeSettings.from_environ(env).cors_allowed_origins
    else:
        with pytest.raises(RuntimeConfigurationError):
            Phase1RuntimeSettings.from_environ(env)
```

- [ ] **Step 10: 运行配置 RED**

Run:

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_api_runtime_config.py -q -W error
```

Expected: 只因 `apps.api.runtime_config` 不存在而 RED；测试不得读取实际 shell env。

- [ ] **Step 11: 先补 infra 规则，再实现配置与环境样例**

`infra/AGENTS.md` 写成中文并明确：infra 只做部署/持久化适配；不放业务规则；不得打印 DSN/凭证；所有业务 SQL tenant filtered；修改迁移需 roundtrip。然后在 `apps/api/runtime_config.py` 实现 frozen/strict/extra-forbid 的嵌套模型与一个固定启动错误：

```python
def _nonblank_exact(value: str) -> str:
    if not value or not value.strip() or value != value.strip():
        raise ValueError("blank configuration")
    return value


def _parse_database_url(value: str) -> SecretStr:
    return SecretStr(_nonblank_exact(value))


def _parse_tenant_id(value: str) -> str:
    return _nonblank_exact(value)


def _parse_dev_mode(value: str) -> bool:
    if value != "true":
        raise ValueError("dev mode must be explicit")
    return True


def _parse_positive_integer(value: str) -> int:
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise ValueError("positive integer required")
    return int(value)


class RuntimeConfigurationError(RuntimeError):
    def __init__(self, field_name: str) -> None:
        super().__init__("API runtime 配置无效")
        self.field_name = field_name


class _HandoffPayload(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    sla_seconds: StrictInt = Field(gt=0)
    backlog_threshold: StrictInt = Field(gt=0)
    t1_seconds: StrictInt = Field(gt=0)
    t2_seconds: StrictInt = Field(gt=0)


class _ScoringPayload(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    version: str
    currency: str
    value_band_boundaries: tuple[WireDecimal, ...]
    bucket_map: dict[Literal["1", "2", "3", "4", "5", "6", "7"], Literal["high", "mid", "low"]]


_HANDOFF_ADAPTER = TypeAdapter(_HandoffPayload)
_SCORING_ADAPTER = TypeAdapter(_ScoringPayload)
_ORIGIN_ADAPTER = TypeAdapter(tuple[str, ...])


def _parse_origins(value: str) -> tuple[str, ...]:
    origins = _ORIGIN_ADAPTER.validate_json(value)
    if not origins or len(origins) != len(set(origins)):
        raise ValueError("origin list invalid")
    for origin in origins:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.hostname is None
            or not parsed.hostname.isascii()
            or parsed.hostname != parsed.hostname.lower()
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("origin invalid")
        port = parsed.port
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        default_port = (parsed.scheme == "http" and port == 80) or (
            parsed.scheme == "https" and port == 443
        )
        canonical = f"{parsed.scheme}://{host}"
        if port is not None and not default_port:
            canonical = f"{canonical}:{port}"
        if origin != canonical:
            raise ValueError("origin not canonical")
    return origins


def _parse_scoring_policy(value: str) -> ScoringPolicy:
    payload = _SCORING_ADAPTER.validate_json(value)
    version = _nonblank_exact(payload.version)
    currency = payload.currency
    if (
        len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or not currency.isupper()
    ):
        raise ValueError("currency invalid")
    return ScoringPolicy(
        version=version,
        value_band_boundaries=tuple(
            Money(amount, CurrencyCode(currency))
            for amount in payload.value_band_boundaries
        ),
        bucket_map={int(rank): bucket for rank, bucket in payload.bucket_map.items()},
    )


_T = TypeVar("_T")


def _read(
    environ: Mapping[str, str],
    name: str,
    parser: Callable[[str], _T],
) -> _T:
    try:
        return parser(environ[name])
    except (
        KeyError,
        ValueError,
        PydanticValidationError,
        ValidationError,
    ):
        raise RuntimeConfigurationError(name) from None


@dataclass(frozen=True)
class Phase1RuntimeSettings:
    database_url: SecretStr
    tenant_id: str
    dev_mode: bool
    cors_allowed_origins: tuple[str, ...]
    retry_after_seconds: int
    handoff_policy: HandoffPolicy
    t1: timedelta
    t2: timedelta
    scoring_policy: ScoringPolicy
    outbox_max_attempts: int

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> Phase1RuntimeSettings:
        database_url = _read(environ, "DATABASE_URL", _parse_database_url)
        tenant_id = _read(environ, "TRADEOS_TENANT_ID", _parse_tenant_id)
        dev_mode = _read(environ, "TRADEOS_DEV_MODE", _parse_dev_mode)
        origins = _read(
            environ,
            "TRADEOS_CORS_ALLOWED_ORIGINS",
            _parse_origins,
        )
        retry_after = _read(
            environ,
            "TRADEOS_API_RETRY_AFTER_SECONDS",
            _parse_positive_integer,
        )
        handoff = _read(
            environ,
            "TRADEOS_HANDOFF_POLICY",
            _HANDOFF_ADAPTER.validate_json,
        )
        scoring_policy = _read(
            environ,
            "TRADEOS_SCORING_POLICY",
            _parse_scoring_policy,
        )
        max_attempts = _read(
            environ,
            "TRADEOS_OUTBOX_MAX_ATTEMPTS",
            _parse_positive_integer,
        )
        return cls(
            database_url=database_url,
            tenant_id=tenant_id,
            dev_mode=dev_mode,
            cors_allowed_origins=origins,
            retry_after_seconds=retry_after,
            handoff_policy=HandoffPolicy(
                sla_seconds=handoff.sla_seconds,
                backlog_threshold=handoff.backlog_threshold,
            ),
            t1=timedelta(seconds=handoff.t1_seconds),
            t2=timedelta(seconds=handoff.t2_seconds),
            scoring_policy=scoring_policy,
            outbox_max_attempts=max_attempts,
        )
```

`_HandoffPayload` 精确包含四个 `StrictInt = Field(gt=0)`。`_parse_database_url` 与 `_parse_tenant_id` 拒绝空值和边界空白；前者返回 `SecretStr`。`_parse_dev_mode` 只接受精确 `true`。`_parse_origins` 先用 `_ORIGIN_ADAPTER.validate_json`，再要求非空、无重复，且每项是 http/https、无 userinfo/path/query/fragment并等于 canonical origin。`_parse_positive_integer` 用 `re.fullmatch(r"[1-9][0-9]*", value)` 后才 `int(value)`。`_parse_scoring_policy` 用 `_SCORING_ADAPTER.validate_json` 后验证 version 非空、currency 为三位大写 ASCII，并把每条 Decimal 构造成同币种 `Money`，最后交给 `ScoringPolicy` 验证非空、finite、严格升序和完整 bucket map。这样每个失败都由 `_read` 精确归到变量名，日志可以记录 name/category 而不记录值。

`infra/.env.example` 只新增以下不可运行形状，不放任何实际阈值或密码示例：

```dotenv
TRADEOS_DEV_MODE=<true-required-for-phase1>
TRADEOS_CORS_ALLOWED_ORIGINS=<json-array-of-exact-http-or-https-origins>
TRADEOS_API_RETRY_AFTER_SECONDS=<positive-integer>
TRADEOS_HANDOFF_POLICY=<json-object-with-sla_seconds-backlog_threshold-t1_seconds-t2_seconds>
TRADEOS_SCORING_POLICY=<json-object-with-version-currency-decimal-string-boundaries-and-1-to-7-bucket-map>
TRADEOS_OUTBOX_MAX_ATTEMPTS=<positive-integer>
```

同时把 `DATABASE_URL` 注释改成纯占位说明，不出现 `user:password@host` 形态。

- [ ] **Step 12: 运行配置 GREEN、敏感扫描和 boundary**

Run:

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_api_runtime_config.py -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  ruff check apps/api/runtime_config.py tests/unit/test_api_runtime_config.py --no-cache
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  mypy apps/api/runtime_config.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python3 scripts/scan_sensitive.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python3 scripts/check_boundaries.py
```

Expected: all pass；secret marker 不出现在 pytest failure、repr、日志 capture。

- [ ] **Step 13: 写 runtime 纯净性、CORS、health、migration 与依赖装配失败测试**

`tests/unit/test_api_runtime.py` 证明 import 与 zero-arg `create_app()` 不读 env/建 engine；真实 runtime 的 middleware stack 为 Safe → CORS → Tenant → app；错误 origin 无 allow header：

```python
def test_import_and_zero_arg_app_do_not_read_environment_or_create_engine(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "secret-marker")
    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", _unexpected_call)
    module = importlib.reload(importlib.import_module("apps.api.runtime"))
    app = create_app()
    assert module.create_runtime_app
    assert isinstance(app.state.dependencies, UnconfiguredApiDependencies)


async def test_runtime_cors_allows_only_configured_origin(runtime_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=runtime_app), base_url="http://test"
    ) as client:
        allowed = await client.options(
            "/health/live",
            headers={
                "Origin": "http://127.0.0.1:4173",
                "Access-Control-Request-Method": "GET",
                "X-Tenant-Id": "tenant-runtime",
            },
        )
        denied = await client.options(
            "/health/live",
            headers={
                "Origin": "http://evil.invalid",
                "Access-Control-Request-Method": "GET",
                "X-Tenant-Id": "tenant-runtime",
            },
        )
    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:4173"
    assert "access-control-allow-origin" not in denied.headers
```

`tests/integration/test_api_runtime.py` 使用 migrated `db_url` 覆盖：head pass、临时 downgrade 后 fail 并 finally upgrade head；未监听端口的 asyncpg URL readiness 返回 false；正常 lifespan 中 dependencies 均真实、outbox registry 处理 `HandoffRequested` 而非 dead；两个 employee scopes 的 session identity 不同且异常 rollback/close；退出 lifespan 后 engine disposed。测试不检查内部异常原文：

```python
async def test_runtime_accepts_exact_head_and_rejects_downgraded_schema(db_url: str) -> None:
    engine = create_engine_from(str(db_url))
    try:
        await assert_database_schema_current(engine)
        _run_alembic(str(db_url), "downgrade", "-1")
        with pytest.raises(RuntimeStartupError, match="API runtime 启动检查失败"):
            await assert_database_schema_current(engine)
    finally:
        _run_alembic(str(db_url), "upgrade", "head")
        await engine.dispose()


@pytest.mark.parametrize(
    "include_local_head",
    [False, True],
)
async def test_runtime_rejects_unknown_or_multiple_database_heads(
    db_url: str,
    include_local_head: bool,
) -> None:
    engine = create_engine_from(str(db_url))
    local_head = _local_unique_head()
    database_heads = (
        (local_head, "unknown-revision")
        if include_local_head
        else ("unknown-revision",)
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM alembic_version"))
            for head in database_heads:
                await connection.execute(
                    text("INSERT INTO alembic_version(version_num) VALUES (:head)"),
                    {"head": head},
                )
        with pytest.raises(RuntimeStartupError, match="API runtime 启动检查失败"):
            await assert_database_schema_current(engine)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM alembic_version"))
            await connection.execute(
                text("INSERT INTO alembic_version(version_num) VALUES (:head)"),
                {"head": local_head},
            )
        await engine.dispose()


async def test_runtime_lifespan_builds_real_registered_components_and_disposes(
    db_url: str, monkeypatch
) -> None:
    env = _runtime_env(str(db_url))
    monkeypatch.setattr(os, "environ", env)
    dispose_calls: list[AsyncEngine] = []
    original_dispose = AsyncEngine.dispose

    async def recording_dispose(engine: AsyncEngine) -> None:
        dispose_calls.append(engine)
        await original_dispose(engine)

    monkeypatch.setattr(AsyncEngine, "dispose", recording_dispose)
    app = create_runtime_app()
    async with app.router.lifespan_context(app):
        deps = get_api_dependencies(_request_for(app))
        assert isinstance(deps.workflow_engine, PostgresWorkflowEngine)
        assert isinstance(deps.outbox_deliverer, OutboxDeliverer)
        assert isinstance(deps.notification_dedup_store, PostgresNotificationDedupStore)
        assert await app.state.readiness_probe.is_ready() is True
    assert dispose_calls == [app.state.runtime_engine]
```

生产代码不得增加 test-only hook；测试 monkeypatch `AsyncEngine.dispose` 时必须保存并调用原方法，因此真实连接仍被释放。最终断言对象 identity，而不是依赖 pool status 文本。

- [ ] **Step 14: 运行 runtime RED**

Run:

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_api_runtime.py tests/integration/test_api_runtime.py -q -W error
```

Expected: 正常收集后失败于 `apps.api.runtime`、health factory、app factory 新参数与真实 composition 尚未实现；Postgres/Alembic 夹具正常。若 `-W error` 暴露 httpx/TestClient warning，使用现有 `AsyncClient+ASGITransport`，不得移除 `-W error`。

- [ ] **Step 15: 缩窄 human handoff 员工读取接口**

在 `workflows/human_handoff/flow.py` 新增只读 Protocol，并把 handlers 的参数类型从完整 `EmployeeService` 改为它：

```python
class HumanHandoffEmployeeReader(Protocol):
    async def get_employee(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        *,
        actor: EmployeeActor,
    ) -> EmployeeView: ...
```

在 `tests/unit/test_human_handoff_flow.py` 新增一个只实现 `get_employee` 的 reader 装配测试，证明不需要 cast 或未使用的员工写能力；原 flow 行为测试全部保留。

- [ ] **Step 16: 实现 health router 与通用 app factory 扩展**

`apps/api/routers/health.py` 定义窄 probe 与固定响应：

```python
class ReadinessProbe(Protocol):
    async def is_ready(self) -> bool: ...


def build_health_router(probe: ReadinessProbe) -> APIRouter:
    router = APIRouter()

    @router.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @router.get("/health/ready")
    async def ready() -> dict[str, str] | JSONResponse:
        if await probe.is_ready():
            return {"status": "ready"}
        return JSONResponse(
            status_code=503,
            content={"code": "service_unavailable", "message": "服务暂时不可用"},
        )

    return router
```

`create_app` 扩成：

```python
def create_app(
    *,
    settings: ApiSettings | None = None,
    dependencies: ApiDependencies | None = None,
    lifespan: Lifespan[FastAPI] | None = None,
    cors_allowed_origins: tuple[str, ...] = (),
    readiness_probe: ReadinessProbe | None = None,
) -> FastAPI:
```

构造顺序固定：`FastAPI(lifespan=lifespan)` → error handlers → TenantAssertion → 若非空则 CORSMiddleware → SafeUnhandledException → CRM router → 若 probe 则 health router → OpenAPI contract。`CORSMiddleware` 的 exact allowlist 按 Global Constraints 固定；zero-arg 不安装 CORS/health，不改变原 OpenAPI 字节与 503 行为。

同文件 `main()` 的 Uvicorn target 改为唯一真实入口字符串：

```python
uvicorn.run(
    "apps.api.runtime:create_runtime_app",
    factory=True,
    access_log=False,
)
```

这不会让 import 或 `create_app()` 读取环境；只有显式执行进程入口时 Uvicorn 才调用 runtime factory。`tests/unit/test_api_app.py` 必须锁定 target 与 `access_log=False`。

- [ ] **Step 17: 实现唯一真实依赖装配**

在 `apps/api/composition/runtime.py` 定义：

```python
@asynccontextmanager
async def employee_service_scope(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    *,
    now: Callable[[], datetime],
    authorizer: EmployeeAuthorizer,
    audit: EmployeeAuditLogger,
) -> AsyncIterator[EmployeeService]:
    session = factory()
    try:
        employees = EmployeeRepositoryImpl(session, tenant_id)
        yield EmployeeServiceImpl(
            employees=employees,
            territories=TerritoryRepositoryImpl(session, tenant_id),
            ownership=OwnershipRepositoryImpl(session, tenant_id),
            now=now,
            manager_pool=lambda _: (),
            count_active_accounts=employees.count_active_accounts,
            authorizer=authorizer,
            audit=audit,
        )
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()
```

空 manager pool 是明确 fail-closed 行为：当前 runtime 没有可安全注入的经理 ID 配置；正常首次归属必须命中真实 Territory，未命中由 EmployeeService 抛 `NoAssignmentRuleError`，不得随机选人。

同文件定义 `RequestScopedHandoffEmployeeReader`，每次 `get_employee` 都进入上述 scope；`StructuredLogOnlyPolicy` 只接受唯一名为 `structured_log` 的 channel；`RuntimeHandoffNotifier` 把 notice 转成固定安全 Notification；再装配：

```python
class RequestScopedHandoffEmployeeReader:
    def __init__(self, scope: EmployeeServiceScope) -> None:
        self._scope = scope

    async def get_employee(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        *,
        actor: EmployeeActor,
    ) -> EmployeeView:
        async with self._scope(tenant_id) as service:
            return await service.get_employee(
                tenant_id,
                employee_id,
                actor=actor,
            )


class StructuredLogOnlyPolicy:
    def channels_for(
        self,
        notification: Notification,
        available: list[NotificationChannel],
    ) -> list[NotificationChannel]:
        del notification
        if len(available) != 1 or available[0].name != "structured_log":
            raise PolicyViolation("Phase 1 通知渠道配置无效")
        return list(available)


class RuntimeHandoffNotifier:
    def __init__(self, router: NotificationRouter) -> None:
        self._router = router

    async def notify(self, notice: HandoffEscalationNotice) -> None:
        await self._router.dispatch(
            Notification(
                tenant_id=notice.tenant_id,
                recipient=notice.recipient_id,
                priority=NotificationPriority.URGENT,
                title="人工接管提醒",
                context={
                    "kind": "handoff_escalation",
                    "level": notice.level,
                },
                source_event="HandoffRequested",
                dedup_key=notice.dedup_key,
                next_step="处理人工接管任务",
                due_at=notice.sla_due_at,
                link=f"/crm/handoffs/{notice.handoff_id}",
            )
        )


def build_phase1_dependencies(
    settings: Phase1RuntimeSettings,
    factory: async_sessionmaker[AsyncSession],
    *,
    now: Callable[[], datetime],
) -> ConfiguredApiDependencies:
    tenant = TenantId(settings.tenant_id)
    opportunity_authorizer = Phase1OpportunityAuthorizer(tenant)
    employee_authorizer = Phase1EmployeeAuthorizer(tenant)
    opportunity_audit = OpportunityStandardAuditLogger()
    employee_audit = EmployeeStandardAuditLogger()
    employees = partial(
        employee_service_scope,
        factory,
        now=now,
        authorizer=employee_authorizer,
        audit=employee_audit,
    )
    opportunities = OpportunityServiceImpl(
        lambda: SqlAlchemyOpportunityUnitOfWork(factory, tenant),
        OpportunityScorerImpl(settings.scoring_policy),
        settings.handoff_policy,
        authorizer=opportunity_authorizer,
        audit=opportunity_audit,
        now=now,
    )
    dedup = PostgresNotificationDedupStore(factory, now=now)
    router = NotificationRouter(dedup, StructuredLogOnlyPolicy())
    router.register_channel(StructuredLogChannel())
    employee_system_actor = EmployeeActor(
        "system:phase1-handoff", EmployeeScope.SYSTEM, "system"
    )
    opportunity_system_actor = OpportunityActor(
        "system:phase1-handoff",
        OpportunityScope(level=ScopeLevel.SYSTEM),
        "system",
    )
    handlers = build_human_handoff_step_handlers(
        opportunity_service=opportunities,
        employee_service=RequestScopedHandoffEmployeeReader(employees),
        notifier=RuntimeHandoffNotifier(router),
        opportunity_system_actor=opportunity_system_actor,
        employee_system_actor=employee_system_actor,
        t1=settings.t1,
        t2=settings.t2,
        now=now,
    )
    workflow = PostgresWorkflowEngine(factory, handlers, now=now)
    outbox = OutboxDeliverer(
        factory,
        tenant,
        now=now,
        max_attempts=settings.outbox_max_attempts,
    )
    register_human_handoff(workflow, outbox, t1=settings.t1, t2=settings.t2)
    return ConfiguredApiDependencies(
        opportunities=opportunities,
        employees=employees,
        opportunity_authorizer=opportunity_authorizer,
        employee_authorizer=employee_authorizer,
        workflow_engine=workflow,
        outbox_deliverer=outbox,
        notification_router=router,
        notification_dedup_store=dedup,
        employee_lookup_actor=employee_system_actor,
    )
```

实现时 `partial` 的第一个 tenant 参数必须保持由 `EmployeeServiceScope.__call__(tenant_id)` 传入；mypy 必须证明签名匹配，不用 `cast` 掩盖。

- [ ] **Step 18: 实现 runtime factory、迁移检查、readiness 与 dispose**

`apps/api/runtime.py` 模块 import 只定义函数。factory 被调用后才读 `os.environ`、创建一次 engine/factory：

```python
class RuntimeStartupError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("API runtime 启动检查失败")


class DatabaseReadinessProbe:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def is_ready(self) -> bool:
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False


async def assert_database_schema_current(engine: AsyncEngine) -> None:
    config = AlembicConfig(str(_REPO_ROOT / "alembic.ini"))
    local_heads = tuple(ScriptDirectory.from_config(config).get_heads())
    try:
        async with engine.connect() as connection:
            database_heads = tuple(
                await connection.run_sync(
                    lambda sync_connection: MigrationContext.configure(
                        sync_connection
                    ).get_current_heads()
                )
            )
    except Exception:
        raise RuntimeStartupError() from None
    if len(local_heads) != 1 or database_heads != local_heads:
        raise RuntimeStartupError()


def create_runtime_app() -> FastAPI:
    try:
        settings = Phase1RuntimeSettings.from_environ(os.environ)
    except RuntimeConfigurationError as exc:
        logger.error(
            "API runtime 配置无效",
            extra={
                "config_name": exc.field_name,
                "error_type": type(exc).__name__,
            },
        )
        raise
    try:
        engine = create_engine_from(settings.database_url.get_secret_value())
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        dependencies = build_phase1_dependencies(
            settings, factory, now=lambda: datetime.now(UTC)
        )
    except Exception as exc:
        logger.error(
            "API runtime 装配失败",
            extra={"error_type": type(exc).__name__},
        )
        raise RuntimeStartupError() from None
    probe = DatabaseReadinessProbe(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            await assert_database_schema_current(engine)
            yield
        finally:
            try:
                await engine.dispose()
            except Exception as exc:
                logger.error(
                    "API runtime 数据库资源释放失败",
                    extra={"error_type": type(exc).__name__},
                )

    app = create_app(
        settings=ApiSettings(
            tenant_id=settings.tenant_id,
            dev_mode=settings.dev_mode,
            retry_after_seconds=settings.retry_after_seconds,
        ),
        dependencies=dependencies,
        lifespan=lifespan,
        cors_allowed_origins=settings.cors_allowed_origins,
        readiness_probe=probe,
    )
    app.state.runtime_engine = engine
    app.state.readiness_probe = probe
    return app
```

engine 构造与所有 composition constructor 都不触发 IO；真正连接只发生在 lifespan startup。startup 检查失败仍进入 lifespan 的 `finally` 并 `await engine.dispose()`。测试不得要求 sync factory 在 event loop 外执行异步 dispose，也不得记录 `settings` 或原异常消息。

另加无效 SQLAlchemy URL 单测：marker 只存在输入，`create_runtime_app()` 抛固定 `RuntimeStartupError`，caplog 的 message/extra 和异常文本都不含 marker；这证明 Uvicorn 不会打印底层 URL parser 的原文。

- [ ] **Step 19: 收紧创建机会第一道 gate 并适配调用方测试**

`apps/api/routers/crm.py` 把 create endpoint 的 `allowed_roles` 从 `_CRM_ROLES` 改为 `_BOSS_ROLE`。`tests/unit/test_crm_router.py` 保留 boss 成功路径并新增 sales/manager 403、零 service 调用；`tests/unit/test_api_app.py` 继续锁定 zero-arg 未配置 503 与 OpenAPI 确定性；`tests/integration/test_crm_api.py` 的真实 create identity 使用 boss。

- [ ] **Step 20: 运行完整 runtime focused GREEN**

Run:

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_phase1_authorizers.py \
         tests/unit/test_api_runtime_config.py \
         tests/unit/test_api_runtime.py \
         tests/unit/test_api_app.py \
         tests/unit/test_crm_router.py \
         tests/unit/test_human_handoff_flow.py \
         tests/integration/test_opportunity_write_abac.py \
         tests/integration/test_api_runtime.py \
         tests/integration/test_crm_api.py \
         tests/integration/test_crm_handoff_api.py \
         tests/integration/test_human_handoff_workflow.py -q -W error
```

Expected: all pass；特别核对 startup mismatch 没有自动 upgrade、readiness 503 body 固定、CORS 未反射 unknown origin、outbox `HandoffRequested` 有注册 handler。

- [ ] **Step 21: Runtime 全门禁与 OpenAPI/front-end 回归**

Run:

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/integration -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  mypy domains shared tool_gateway apps workflows notification_gateway infra
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python3 scripts/check_boundaries.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python3 scripts/scan_sensitive.py
git diff --check
cd apps/web
npm run gen:api
git diff --exit-code -- src/api/api.d.ts
npm run typecheck
npm run lint
npm run test
npm run build
cd ../..
```

Expected: every command exit 0；`apps/web/dist` 清理后不进 status；OpenAPI types 没有非预期漂移。

- [ ] **Step 22: 独立 review runtime package 并只修 Critical/Important**

Review 必须逐项回答：authorizer 是否默认拒绝；六个写操作是否 load-before-ABAC 且 audit-after-commit；跨租户 SQL 是否全带 tenant；SecretStr/异常/log 是否可能泄 DSN；CORS 是否位于 Safe 内侧；migration check 是否 exact unique head；employee session 是否每 scope commit/rollback/close；outbox registry 是否非空；engine 是否所有失败路径 dispose。每个行为 finding 都先写真实 RED 再修，修后重跑 Step 20/21。

- [ ] **Step 23: 精确 stage、提交、push 并等待 CI**

Run:

```bash
git status --short
git add infra/.env.example \
        domains/opportunities/permissions.py \
        domains/employees/permissions.py \
        domains/opportunities/service_impl.py \
        workflows/human_handoff/flow.py \
        apps/api/main.py \
        apps/api/routers/crm.py \
        tests/unit/test_api_app.py \
        tests/unit/test_crm_router.py \
        tests/unit/test_human_handoff_flow.py \
        tests/unit/test_opportunities_permissions.py \
        tests/unit/test_opportunities_service.py \
        tests/unit/test_opportunities_handoff.py \
        tests/integration/test_crm_api.py
git add --chmod=-x infra/AGENTS.md \
        apps/api/runtime_config.py \
        apps/api/composition/runtime.py \
        apps/api/routers/health.py \
        apps/api/runtime.py \
        tests/unit/test_phase1_authorizers.py \
        tests/unit/test_api_runtime_config.py \
        tests/unit/test_api_runtime.py \
        tests/integration/test_opportunity_write_abac.py \
        tests/integration/test_api_runtime.py
git diff --cached --name-only
git diff --cached --summary
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python3 scripts/scan_sensitive.py --staged
git diff --cached --check
git commit -m "feat(api): add configured phase1 runtime"
git push origin codex/phase1-implementation
RUN_ID=$(gh run list --branch codex/phase1-implementation --commit "$(git rev-parse HEAD)" --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN_ID" --exit-status
```

Expected: cached files 精确等于 Task 1 Files；所有新 `.py`/`.md` mode `100644`；commit 与 remote branch SHA 相同；CI success。

---

### Task 2: S3-21 真实 PostgreSQL → Uvicorn → Vite → Chromium E2E

**Files:**
- Create: `tests/e2e/conftest.py`
- Create: `tests/e2e/test_opportunity_board.py`
- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `apps.api.runtime:create_runtime_app`；`Phase1RuntimeSettings.from_environ`；`build_phase1_dependencies(settings, factory, *, now)`；真实 CRM endpoints；现有 `/crm/opportunities` 与 `/crm/handoffs` Vue routes。
- Produces: `e2e_stack` pytest fixture（base URLs、tenant、boss/sales IDs、engine/factory、process handles）；一条无请求拦截的 Chromium E2E；CI Chromium 安装与显式 E2E gate。

- [ ] **Step 1: 在 conda 环境预装 Python Playwright 与 Chromium，排除环境假 RED**

Run:

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python -m pip install playwright
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python -m playwright install chromium
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python -c 'from playwright.sync_api import sync_playwright; print("playwright-ready")'
```

Expected: exact `playwright-ready`；这只准备本地测试环境，不构成仓库交付。

- [ ] **Step 2: 写隔离 E2E 栈 fixture**

`tests/e2e/conftest.py` 自己启动 session-scoped `pgvector/pgvector:pg16` container、转 asyncpg URL、用 subprocess `alembic upgrade head`；不 import `tests/integration/conftest.py`，避免全套 pytest plugin 重复注册。

文件首行使用 `from __future__ import annotations`。定义 `ManagedProcess`，所有输出写 `TemporaryDirectory` 下文件而非 capture pipe，避免子进程缓冲阻塞；清理固定 terminate→wait(10s)→kill→wait(5s)：

```python
@dataclass(frozen=True)
class E2EEmployees:
    boss: EmployeeId
    manager: EmployeeId
    sales_a: EmployeeId
    sales_b: EmployeeId


@dataclass(frozen=True)
class E2EStack:
    api_origin: str
    web_origin: str
    tenant_id: TenantId
    employees: E2EEmployees
    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    runtime_settings: Phase1RuntimeSettings
    api_process: ManagedProcess
    vite_process: ManagedProcess


@dataclass(frozen=True)
class E2EScenario:
    opportunity_ids: tuple[OpportunityId, ...]
    oldest_handoff: HandoffId
    second_handoff: HandoffId


@dataclass
class ManagedProcess:
    process: subprocess.Popen[bytes]
    stdout_path: Path
    stderr_path: Path

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def _minimal_process_env() -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(_REPO_ROOT),
        "LANG": "C.UTF-8",
    }


def _runtime_process_env(
    database_url: str,
    tenant_id: TenantId,
    vite_origin: str,
) -> dict[str, str]:
    return {
        **_minimal_process_env(),
        "DATABASE_URL": database_url,
        "TRADEOS_TENANT_ID": str(tenant_id),
        "TRADEOS_DEV_MODE": "true",
        "TRADEOS_CORS_ALLOWED_ORIGINS": json.dumps([vite_origin]),
        "TRADEOS_API_RETRY_AFTER_SECONDS": "2",
        "TRADEOS_HANDOFF_POLICY": json.dumps(
            {
                "sla_seconds": 30,
                "backlog_threshold": 10,
                "t1_seconds": 2,
                "t2_seconds": 2,
            }
        ),
        "TRADEOS_SCORING_POLICY": json.dumps(
            {
                "version": "e2e-v1",
                "currency": "USD",
                "value_band_boundaries": ["1000.00", "5000.00"],
                "bucket_map": {
                    "1": "low",
                    "2": "low",
                    "3": "mid",
                    "4": "mid",
                    "5": "high",
                    "6": "high",
                    "7": "high",
                },
            }
        ),
        "TRADEOS_OUTBOX_MAX_ATTEMPTS": "3",
    }
```

Uvicorn 用预绑定 socket + `--fd`，避免端口竞态；命令和环境固定：

```python
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen()
api_port = listener.getsockname()[1]
api_process = subprocess.Popen(
    [
        sys.executable,
        "-m",
        "uvicorn",
        "apps.api.runtime:create_runtime_app",
        "--factory",
        "--fd",
        str(listener.fileno()),
        "--no-access-log",
    ],
    cwd=_REPO_ROOT,
    env=_runtime_process_env(db_url, tenant_id, vite_origin),
    stdout=api_stdout,
    stderr=api_stderr,
    pass_fds=(listener.fileno(),),
)
listener.close()
```

Vite 先用临时 bind 找端口再关闭，随后 `--strictPort`：

```python
vite_process = subprocess.Popen(
    [
        "npm",
        "run",
        "dev",
        "--",
        "--host",
        "127.0.0.1",
        "--port",
        str(vite_port),
        "--strictPort",
    ],
    cwd=_REPO_ROOT / "apps/web",
    env={
        **_minimal_process_env(),
        "VITE_API_BASE_URL": api_origin,
        "VITE_TENANT_ID": str(tenant_id),
        "VITE_EMPLOYEE_ID": str(boss_id),
    },
    stdout=vite_stdout,
    stderr=vite_stderr,
)
```

`_runtime_process_env` 只含 PATH/PYTHONPATH 与八个 runtime 变量；policy JSON 使用测试私有的短 SLA/明确阈值，DATABASE_URL 永不进入断言消息。API readiness 每 100ms 请求 `/health/ready` 并带唯一 `X-Tenant-Id`，总上限 20s；Vite readiness 请求 `/crm/opportunities` HTML，总上限 20s。任一进程提前退出，错误只报告 returncode 与日志文件路径，不把文件内容/DSN拼进 assertion。`e2e_stack` async fixture 用一个外层 `try/finally`：yield 后先 `vite_process.stop()`、再 `api_process.stop()`、再 `await engine.dispose()`、最后关闭四个日志 file handle 与 TemporaryDirectory；API/Vite 任一启动中途失败也走同一 finally。

- [ ] **Step 3: 只 seed 运行前置员工与 Territory**

在 fixture 中用同一个随机 tenant 与随机 `new_id("emp")` 写 4 个 `EmployeeRow`：boss → manager → sales A/B；写 2 个 `TerritoryAssignmentRow`，分别让 sales A 匹配 `US/hinges`、sales B 匹配 `CA/fasteners`。具体字段固定为 active、manager chain、空 languages、UTC 时区；Territory `priority=1`、`effective_from=2026-08-10T00:00:00Z`、`need_categories` 使用对应品类、其他数组为空、manager 为 manager ID、backup 为 null。先 flush employees 再 flush territories，commit 后关闭 seed session。禁止直接写 Opportunity/Handoff/Workflow/Outbox/Notification 表。

- [ ] **Step 4: 写真实 HTTP 数据准备与独立 DB read-back helpers**

`tests/e2e/test_opportunity_board.py` 使用 `urllib.request` 或 Playwright `APIRequestContext` 发真实网络请求；不直接调用 ASGI app。机会 body 每条使用唯一 need/account/source ID，完整 `conversation` provenance 与 Decimal string：

```python
def _opportunity_body(index: int, country: str, category: str) -> dict[str, object]:
    source_id = f"e2e-message-{index}"
    provenance = {
        "source_type": "conversation",
        "source_id": source_id,
        "extracted_by": "human",
        "extracted_at": "2026-08-10T02:00:00Z",
    }
    return {
        "request": {
            "need_id": f"e2e-need-{index}",
            "account_id": f"e2e-account-{index}",
            "account_name": f"E2E 企业 {index}",
            "country": country,
            "product_category": category,
            "evidence_tier": "customer_interest_reply",
            "has_verified_contact": True,
            "category_allowed": True,
            "minimum_order_value": {"amount": "1000.00", "currency": "USD"},
            "supply_available": True,
            "estimated_order_value": {"amount": "5000.00", "currency": "USD"},
            "field_provenance": {
                "account_name": provenance,
                "country": provenance,
            },
        },
        "evidence": {
            "level": "customer_interest_reply",
            "provenance": provenance,
        },
    }
```

创建顺序使用 `(US, hinges)` 三条与 `(CA, fasteners)` 两条。每个 POST 必须 201，返回的 OpportunityView owner 分属两个 sales。独立 session 断言：

```python
assert await _count_rows(session, OpportunityRow, tenant_id) == 5
assert await _count_rows(session, ScoreSnapshotRow, tenant_id) == 5
assert await _count_rows(session, OwnershipLockRow, tenant_id) == 5
assert await _count_rows(session, OutboxEventRow, tenant_id, "OpportunityQualified") == 5
assert set(await _opportunity_owners(session, tenant_id)) == {sales_a, sales_b}
assert await _conversation_provenance_count(session, tenant_id) == 10
```

- [ ] **Step 5: 通过真实 production composition + public service 创建两条 handoff**

fixture 在启动进程前用同一 runtime env 构造 `Phase1RuntimeSettings` 并放入 `E2EStack.runtime_settings`；测试进程内用独立 engine/factory 和 `build_phase1_dependencies` 得到 concrete `opportunities`。以 boss TENANT actor 调 public `request_handoff`。`_Clock.now()` 返回内部 UTC aware datetime，`advance(delta)` 只做确定性相加。两个 request 的 `requested_at` 相差 10 秒，packet 的 account/country 必须来自刚创建的 OpportunityView：

```python
oldest = await dependencies.opportunities.request_handoff(
    tenant_id,
    _handoff_request(created[0], "e2e-handoff-message-1"),
    actor=boss_actor,
)
clock.advance(timedelta(seconds=10))
second = await dependencies.opportunities.request_handoff(
    tenant_id,
    _handoff_request(created[1], "e2e-handoff-message-2"),
    actor=boss_actor,
)
```

独立 DB session 断言只有两条 requested handoff，严格 `requested_at ASC == [oldest, second]`，各有 `HandoffRequested` outbox；不得直接 INSERT handoff/workflow/outbox。

准备函数必须给后续浏览器与 DB 断言返回强类型 ID，不从页面文本反推：

```python
class _Clock:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value

    def advance(self, delta: timedelta) -> None:
        self._value += delta


def _handoff_request(
    opportunity: OpportunityView,
    source_id: str,
) -> HandoffCreateRequest:
    return HandoffCreateRequest(
        opportunity_id=opportunity.opportunity_id,
        trigger="quote_requested",
        account_name=opportunity.account_name,
        country=opportunity.country,
        why_valuable="客户已明确请求报价并具备可供应条件",
        customer_verbatim="Please prepare a formal quotation for our team.",
        customer_verbatim_provenance=Provenance(
            source_type=SourceType.CONVERSATION,
            source_id=source_id,
            extracted_by="human",
            extracted_at=datetime(2026, 8, 10, 2, tzinfo=UTC),
        ),
        validated_need_summary="客户明确表达采购需求",
        suggested_next_step="真人核对规格后准备报价",
        evidence_links=[f"/crm/opportunities/{opportunity.opportunity_id}"],
    )


async def _prepare_real_scenario(stack: E2EStack) -> E2EScenario:
    combinations = [
        ("US", "hinges"),
        ("CA", "fasteners"),
        ("US", "hinges"),
        ("CA", "fasteners"),
        ("US", "hinges"),
    ]
    created = [
        await _post_opportunity(stack, index, country, category)
        for index, (country, category) in enumerate(combinations, start=1)
    ]
    await _assert_created_rows(stack, created)

    clock = _Clock(datetime(2026, 8, 10, 3, tzinfo=UTC))
    dependencies = build_phase1_dependencies(
        stack.runtime_settings,
        stack.factory,
        now=clock.now,
    )
    boss_scope = OpportunityScope(level=ScopeLevel.TENANT)
    boss_actor = OpportunityActor(
        str(stack.employees.boss), boss_scope, "boss"
    )
    oldest = await dependencies.opportunities.request_handoff(
        stack.tenant_id,
        _handoff_request(created[0], "e2e-handoff-message-1"),
        actor=boss_actor,
    )
    clock.advance(timedelta(seconds=10))
    second = await dependencies.opportunities.request_handoff(
        stack.tenant_id,
        _handoff_request(created[1], "e2e-handoff-message-2"),
        actor=boss_actor,
    )
    await _assert_handoff_order(stack, oldest, second)
    return E2EScenario(
        opportunity_ids=tuple(
            OpportunityId(item.opportunity_id) for item in created
        ),
        oldest_handoff=oldest,
        second_handoff=second,
    )
```

`_post_opportunity` 用 `asyncio.to_thread(urllib.request.urlopen, request, timeout=10)`，请求头精确 `Content-Type`、`X-Tenant-Id`、`X-Employee-Id=boss`，要求 status 201，并用 `TypeAdapter(OpportunityView).validate_json(response_bytes)`；`_assert_created_rows` 与 `_assert_handoff_order` 只执行 tenant-filtered `select/count`。所有独立 engine/factory 在 fixture finally 由 `await engine.dispose()` 收口。

- [ ] **Step 6: 写 Chromium 行为与数据库事实 E2E**

单个 async 测试使用 `async_playwright`，注册 `page.on("console")` 与 `page.on("pageerror")` 收集 error；不调用 `page.route`。行为与 locator 固定：

```python
async def test_real_opportunity_board_and_handoff_queue(
    e2e_stack: E2EStack,
) -> None:
    scenario = await _prepare_real_scenario(e2e_stack)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        browser_errors: list[str] = []
        page.on("console", lambda message: browser_errors.append("console:error") if message.type == "error" else None)
        page.on("pageerror", lambda error: browser_errors.append(type(error).__name__))
        try:
            await page.goto(f"{e2e_stack.web_origin}/crm/opportunities", wait_until="networkidle")
            await expect(page.locator('ol[aria-label="机会列表"] > li > button')).to_have_count(5)
            await page.get_by_role("button", name=re.compile("E2E 企业 1")).click()
            await page.get_by_role("button", name="查看来源").first.click()
            await expect(page.get_by_role("dialog")).to_contain_text("来源类型")
            await expect(page.get_by_role("dialog")).to_contain_text("e2e-message-1")
            await page.get_by_role("button", name="关闭来源").click()

            await page.get_by_label("选择合法目标状态").select_option("assigned")
            await page.get_by_role("button", name="推进状态").click()
            await expect(page.get_by_label("机会详情")).to_contain_text("已分配")

            await page.get_by_role("button", name=re.compile("E2E 企业 3")).click()
            await page.locator("summary", has_text="标记为流失").click()
            await page.get_by_label("流失原因").select_option("price_too_high")
            await page.get_by_role("button", name="确认标记流失").click()
            await expect(page.get_by_label("机会详情")).to_contain_text("已流失")

            await page.goto(f"{e2e_stack.web_origin}/crm/handoffs", wait_until="networkidle")
            cards = page.locator('ol[aria-label="最久等待接管队列"] > li > button')
            await expect(cards).to_have_count(2)
            await expect(cards.first).to_have_attribute(
                "data-handoff-id", str(scenario.oldest_handoff)
            )
            await cards.first.click()
            await expect(page.get_by_label("接管包详情")).to_contain_text("E2E 企业 1")
            await page.get_by_role("button", name="接受接管").click()
            await expect(cards).to_have_count(1)
            await expect(cards.first).to_be_focused()
            assert browser_errors == []
        finally:
            await context.close()
            await browser.close()
    await _assert_durable_browser_results(e2e_stack, scenario)
```

浏览器结束后独立 DB read-back 不能复用页面 JSON；函数按 scenario typed ID 和 tenant 查询：

```python
async def _assert_durable_browser_results(
    stack: E2EStack,
    scenario: E2EScenario,
) -> None:
    async with stack.factory() as session:
        transitioned = await session.scalar(
            select(OpportunityRow).where(
                OpportunityRow.tenant_id == str(stack.tenant_id),
                OpportunityRow.opportunity_id == str(scenario.opportunity_ids[0]),
            )
        )
        lost = await session.scalar(
            select(OpportunityRow).where(
                OpportunityRow.tenant_id == str(stack.tenant_id),
                OpportunityRow.opportunity_id == str(scenario.opportunity_ids[2]),
            )
        )
        oldest = await session.scalar(
            select(HandoffRow).where(
                HandoffRow.tenant_id == str(stack.tenant_id),
                HandoffRow.handoff_id == str(scenario.oldest_handoff),
            )
        )
        second = await session.scalar(
            select(HandoffRow).where(
                HandoffRow.tenant_id == str(stack.tenant_id),
                HandoffRow.handoff_id == str(scenario.second_handoff),
            )
        )
        assert transitioned is not None and transitioned.state == "assigned"
        assert lost is not None and lost.state == "lost"
        assert lost.loss_reason == "price_too_high"
        assert lost.died_at_state == "qualified"
        assert lost.closed_by == str(stack.employees.boss)
        assert lost.closed_at is not None
        assert oldest is not None and oldest.state == "accepted"
        assert oldest.accepted_by == str(stack.employees.boss)
        assert second is not None and second.state == "requested"
        assert second.accepted_by is None
        assert await _event_count(
            session,
            stack.tenant_id,
            "OpportunityLost",
            str(scenario.opportunity_ids[2]),
        ) == 1
        assert await _event_count(
            session,
            stack.tenant_id,
            "HandoffAccepted",
            str(scenario.oldest_handoff),
        ) == 1
```

`_event_count` 对 `OutboxEventRow.tenant_id/event_type` 过滤，并在 JSONB payload 的 typed subject 字段上匹配 ID，防同租户其他事件掩盖重复。安全断言读取 Uvicorn/Vite 日志文件并确认不含完整 database URL、通过 `urlsplit` 取出的 password fragment、客户 verbatim，也不含 `Traceback`；日志断言失败只报告安全布尔结果，不把原日志或 URL 拼进 pytest 消息。

- [ ] **Step 7: 先用错误 factory 取得 genuine E2E RED，再恢复真实 factory**

在测试 fixture 中临时把 Uvicorn target 改为 `apps.api.main:create_app`，运行：

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/e2e/test_opportunity_board.py -q -W error -m e2e
```

Expected: readiness 或首次 CRM create 固定失败于 503，证明测试能杀掉“仍启动未配置 factory”的现实回归；Postgres、Vite、Chromium 已正常启动，不是环境假失败。立即把 target 恢复为 `apps.api.runtime:create_runtime_app`，`git diff` 不得保留 mutation。

- [ ] **Step 8: 运行真实 E2E GREEN 与资源残留检查**

Run:

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/e2e/test_opportunity_board.py -q -W error -m e2e
ps -axo pid,command | rg 'uvicorn apps.api.runtime|vite.*strictPort' || true
```

Expected: E2E pass；第二条命令无残留子进程。删除测试生成的截图、trace、video、`apps/web/dist`、AppleDouble 与临时日志；失败诊断文件只在 pytest temp dir，不进仓库。

- [ ] **Step 9: 把 Playwright 与 E2E gate 写入项目和 CI**

`pyproject.toml` 的 dev extra 增加 `playwright`，markers 增加：

```toml
markers = [
    "db: 需 Docker 后端的 Postgres 容器测试",
    "e2e: 真实 PostgreSQL、Uvicorn、Vite 与 Chromium 端到端测试",
]
```

`.github/workflows/ci.yml`：保留所有现有步骤；Python dev install 后、任何 pytest 前新增：

```yaml
      - name: Install Chromium
        run: python -m playwright install --with-deps chromium
```

现有 `Tests` 后新增：

```yaml
      - name: Browser E2E
        run: pytest tests/e2e -q -W error
```

job `timeout-minutes` 从 15 调成 30；这是有限上限，用于 Chromium 冷安装与 default pytest + 显式 E2E 两次运行，不得设无限 timeout。

- [ ] **Step 10: E2E 与全库门禁**

Run:

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/e2e -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/integration -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  mypy domains shared tool_gateway apps workflows notification_gateway infra
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python3 scripts/check_boundaries.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python3 scripts/scan_sensitive.py
git diff --check
cd apps/web
npm run gen:api
git diff --exit-code -- src/api/api.d.ts
npm run typecheck
npm run lint
npm run test
npm run build
cd ../..
```

Expected: all exit 0；默认 pytest 真实收集 E2E，显式 E2E 再通过一次；frontend 仍为 52+ tests 或最新更高数量；清理 `apps/web/dist` 后 status 只含四个 Task 2 文件。

- [ ] **Step 11: 独立 review E2E package 并做 mutation 审计**

Review 必须验证：没有 `page.route`/fake transport；只有 employee/territory direct seed；五机会来自真实 HTTP；handoff 来自 public service；UI 顺序/写入由独立 DB read-back 证明；CORS origin exact；进程所有失败路径 bounded cleanup；日志无 DSN/verbatim；测试能杀掉 wrong factory、DOM-only accept、逆序 queue、重复 outbox。任何 Critical/Important 先写行为 RED，修后完整重跑 Step 8/10。

- [ ] **Step 12: 精确 stage、提交、push 并等待 CI**

Run:

```bash
git status --short
git add pyproject.toml .github/workflows/ci.yml
git add --chmod=-x tests/e2e/conftest.py tests/e2e/test_opportunity_board.py
git diff --cached --name-only
git diff --cached --summary
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python3 scripts/scan_sensitive.py --staged
git diff --cached --check
git commit -m "test(e2e): verify phase1 opportunity and handoff stack"
git push origin codex/phase1-implementation
RUN_ID=$(gh run list --branch codex/phase1-implementation --commit "$(git rev-parse HEAD)" --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN_ID" --exit-status
```

Expected: cached files 精确四个、两份 Python 新文件 mode `100644`；remote SHA 与 local HEAD 相同；CI 经过 Chromium install、默认 suite 与显式 Browser E2E 后 success。

---

## Completion Evidence

完成 Task 1 与 Task 2 后，只在以下证据全部成立时把 Slice 3 标为完成：

1. zero-arg `create_app()` 仍纯净且 CRM 503；configured `create_runtime_app()` 对完整配置/精确 Alembic head 启动，对缺配置、错 CORS、错迁移拒绝。
2. authorizer matrix 与六个写侧 resource ABAC 都有真实 deny/allow 证据；拒绝路径零写、零 outbox、零 allow audit。
3. `make check`、integration `-W error`、前端四门禁、显式 E2E、boundary、sensitive、diff check 全部 fresh GREEN。
4. Chromium 未拦截网络，请求真实跨 origin；五机会、两 handoff、transition、mark-lost、accept 与独立 PostgreSQL read-back 一致。
5. 两个普通 commit 均已 push；各自独立 review 无开放 Critical/Important；对应远端 CI run success。
