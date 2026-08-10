# Slice 4A 发件身份与域名信誉 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不接触 Gmail/DNS 凭证、不发送邮件、不引入 Tool Gateway 的前提下，交付 tenant-safe、Decimal 精确、默认拒绝且具备真实 PostgreSQL 并发证明的发件身份、预热额度与信誉熔断领域纵切面。

**Architecture:** `domains/sending_identity` 只定义纯领域模型、typed permissions、公共 DTO/Protocol 与服务实现；`infra/db` 提供 0008 迁移、tenant-bound repositories、request-scoped UoW 与 durable outbox。所有写操作先 typed authorize，再在同一事务中完成状态、history 与 outbox，事务成功后才写一条 allow audit。发送前唯一权威入口是原子 `reserve_send_slot`；投递事件按 7 天滚动窗口计算 Decimal 指标，并对身份与同域全部身份执行确定性熔断。

**Tech Stack:** Python 3.12.13（conda `tradeos-py312`）、dataclasses、Decimal、IDNA、SQLAlchemy 2.x async + asyncpg、Alembic、PostgreSQL 16 testcontainers、pytest/pytest-asyncio、现有 durable outbox 与 GitHub Actions。

## Global Constraints

- 权威规格：`docs/superpowers/specs/2026-08-10-slice4a-sending-identity-design.md`。本计划不得扩大到 4B Gmail/DNS Connector、Tool Gateway，也不得扩大到 4C 真实发送、通知渠道、API 或 UI。
- 每次 Python 命令固定加：`env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH"`。不得把本机 conda 绝对路径写进产品代码或配置。
- 执行每个任务前完整阅读根 `AGENTS.md`、`HANDBOOK.md` 以及所改目录的就近 `AGENTS.md`。缺失的非顶层规则文件不是扩大范围的理由。
- `domains/sending_identity` 只允许导入 `shared.*` 和本域模块；禁止 SQLAlchemy、connector、app 和其他 domain。
- `connector_ref`、`check_ref` 只是短引用名。实体、DTO、事件、异常、日志、审计、测试输出都不得携带凭证、DNS 原始响应、邮箱正文或 webhook payload。
- 信誉比率和阈值只用 `Decimal`；数据库使用 `NUMERIC(9,6)`；不得用 float、中间浮点运算或 JSON number 表示比率。
- 所有新表的主键、唯一键、外键都包含 `tenant_id`。repository 构造时绑定租户，参数 tenant 不匹配时 fail closed，并记录固定中文 CRITICAL 安全告警。
- 写操作顺序固定为：typed require → tenant-filtered load → resource/domain ABAC → state/input validation → DB/history/outbox → UoW commit → exactly one allow audit。所有失败路径零 allow audit。
- 领域状态变化与 outbox 同事务。现有 shared event 字段不改，不触发事件契约 ADR；重复状态评估由 row lock + identity version + action key 阻止重复 outbox。
- 新文件在 exFAT 上用 `git add --chmod=-x`，cached mode 必须是 `100644`。只精确 stage 当前任务文件；禁止 amend，禁止 push 未审查的代码。
- 每个任务都建立 ignored 工作记录目录 `.superpowers/sdd/2026-08-10-slice4a-sending-identity/`，保存 brief、RED/GREEN 证据、review package 和 findings；不得把该目录加入提交。
- 每个任务完成后必须独立实现审查与安全审查；Critical/Important finding 必须先补 genuine RED，再做最小修复，再复审。
- 每个任务独立普通 commit、push，并等待远端 CI 成功后才能开始下一任务。CI watch 固定使用：

```bash
RUN_ID=$(gh run list --branch codex/phase1-implementation --commit "$(git rev-parse HEAD)" --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN_ID" --exit-status
```

---

## Public Contract Target

任务 1 完成后，`domains/sending_identity/service.py` 必须公开并 re-export 以下 typed contract；后续任务只实现它，不再改签名：

```python
class SendingIdentityService(Protocol):
    async def register(
        self,
        tenant_id: TenantId,
        request: IdentityRegisterRequest,
        *,
        actor: Actor,
    ) -> SendingIdentityId: ...

    async def begin_authentication(
        self, tenant_id: TenantId, identity_id: SendingIdentityId, *, actor: Actor
    ) -> None: ...

    async def record_authentication_result(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        result: AuthenticationResult,
        *,
        actor: Actor,
    ) -> None: ...

    async def start_warmup(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        target_daily_volume: int,
        *,
        actor: Actor,
    ) -> None: ...

    async def advance_warmup(
        self, tenant_id: TenantId, identity_id: SendingIdentityId, *, actor: Actor
    ) -> None: ...

    async def check_send_permission(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        for_cold_outreach: bool,
        *,
        actor: Actor,
    ) -> SendPermission: ...

    async def reserve_send_slot(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        reservation_key: IdempotencyKey,
        for_cold_outreach: bool,
        *,
        actor: Actor,
    ) -> SendReservation: ...

    async def record_delivery_event(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        event: DeliveryEventRecord,
        *,
        actor: Actor,
    ) -> bool: ...

    async def evaluate_reputation(
        self, tenant_id: TenantId, identity_id: SendingIdentityId, *, actor: Actor
    ) -> ReputationView: ...

    async def resume_from_throttle(
        self, tenant_id: TenantId, identity_id: SendingIdentityId, *, actor: Actor
    ) -> None: ...

    async def resume_from_suspension(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        investigation_note: str,
        *,
        actor: Actor,
    ) -> None: ...

    async def retire(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        reason: str,
        *,
        actor: Actor,
    ) -> None: ...

    async def get(
        self, tenant_id: TenantId, identity_id: SendingIdentityId, *, actor: Actor
    ) -> IdentityView: ...

    async def list_available_for_campaign(
        self, tenant_id: TenantId, *, limit: int, actor: Actor
    ) -> list[IdentityView]: ...

    async def get_domain_reputation(
        self, tenant_id: TenantId, domain: str, *, actor: Actor
    ) -> DomainReputationView: ...

    async def get_warmup_progress(
        self, tenant_id: TenantId, identity_id: SendingIdentityId, *, actor: Actor
    ) -> WarmupProgressView: ...
```

时间只能由 `SendingIdentityServiceImpl(..., now: Callable[[], datetime])` 注入；公共方法不接受 `started_on`、`on_day` 或 `computed_at`。

---

## Data Contract Target

0008 必须精确创建以下七表；ORM、迁移、repository 映射逐列一致：

1. `sending_domains`
   - `(tenant_id VARCHAR(32), domain VARCHAR(253))` composite PK。
   - `role VARCHAR(32) NOT NULL`，`created_at TIMESTAMPTZ NOT NULL`。
   - role/domain/tenant 不可更新。
2. `sending_identities`
   - `(tenant_id, identity_id VARCHAR(32))` composite PK。
   - `domain VARCHAR(253)` composite FK → `sending_domains`；`address VARCHAR(320)`；`display_name VARCHAR(200) NULL`；`state VARCHAR(32)`；`connector_ref VARCHAR(64) NULL`。
   - `warmup_started_on DATE NULL`、`target_daily_volume INT NULL`、`activated_at/suspended_at/retired_at TIMESTAMPTZ NULL`。
   - `sendable_state_before_restriction VARCHAR(32) NULL`、`suspension_category VARCHAR(64) NULL`、`version INT NOT NULL DEFAULT 0`。
   - 四个阈值 `NUMERIC(9,6)`、两个立即熔断 bool、`minimum_sample INT`。
   - `UNIQUE(tenant_id,address)`；target `5..100`；version/sample 非负；状态/预热字段配对 CHECK。
   - PostgreSQL CHECK `split_part(address,'@',2) = domain`；restriction previous state 只能是 `warming/active`，且仅 restricted state 可保留该字段。
3. `sending_auth_checks`
   - `(tenant_id, auth_check_id VARCHAR(32))` composite PK；identity composite FK。
   - `checked_at`、三个 bool、`failures JSONB`、`check_ref VARCHAR(64)`、`created_at`。
   - `UNIQUE(tenant_id,identity_id,check_ref)`；append-only trigger。
4. `sending_reputation_events`
   - `(tenant_id, reputation_event_id VARCHAR(32))` composite PK；identity composite FK。
   - `event_type VARCHAR(32)`、`occurred_at`、`dedup_key VARCHAR(200)`、`source_ref VARCHAR(64)`、`created_at`。
   - `UNIQUE(tenant_id,dedup_key)`；append-only trigger；时间查询 index `(tenant_id,identity_id,occurred_at)`。
5. `sending_daily_counters`
   - composite PK `(tenant_id,identity_id,on_day)`；identity composite FK；`sent_attempts INT >= 0`。
   - guard trigger 只允许 `sent_attempts` 单调增加，键不可变。
6. `sending_send_reservations`
   - `(tenant_id,reservation_id VARCHAR(32))` composite PK；identity composite FK。
   - `reservation_key VARCHAR(200)`、`on_day DATE`、`sequence INT >= 1`、`created_at`。
   - `UNIQUE(tenant_id,identity_id,reservation_key)` 与 `UNIQUE(tenant_id,identity_id,on_day,sequence)`；append-only trigger。
7. `sending_identity_actions`
   - `(tenant_id,action_id VARCHAR(32))` composite PK；identity composite FK。
   - `action_key VARCHAR(200)`、`action VARCHAR(64)`、`before_state/after_state VARCHAR(32) NULL`、`actor_id VARCHAR(64)`、`scope VARCHAR(32)`、`rule VARCHAR(128)`、`note TEXT NULL`、`occurred_at`。
   - `UNIQUE(tenant_id,identity_id,action_key)`；append-only trigger。

迁移 downgrade 必须按 trigger → function → child table → parent table 顺序精确恢复 0007。

---

### Task 1: 纯领域模型、DTO 与 Typed Permissions

**Files:**
- Modify: `domains/sending_identity/models.py`
- Modify: `domains/sending_identity/schemas.py`
- Modify: `domains/sending_identity/service.py`
- Modify: `domains/sending_identity/repository.py`
- Modify: `domains/sending_identity/errors.py`
- Create: `domains/sending_identity/permissions.py`
- Create: `tests/unit/test_sending_identity_models.py`
- Create: `tests/unit/test_sending_identity_contracts.py`
- Create: `tests/unit/test_sending_identity_permissions.py`

**Interfaces:**
- Consumes: `TenantId`、`SendingIdentityId`、`IdempotencyKey`、`EventBus`、`ValidationError`、`PolicyViolation`、`PermissionDenied`、`InvalidStateTransition`。
- Produces: 本计划 `Public Contract Target`；`SendingDomain`、`DomainRole`、`IdentityState`、`AuthCheck`、`AuthenticationFailureCategory`、`AuthenticationFixInstruction`、`DeliveryEventType`、`WarmupPlan`、`ReputationWindow`、`ReputationThresholds`；`SendingIdentityAction`、`ScopeLevel`、`SendingIdentityScope`、`Actor`、`SendingIdentityAuthorizer`、`Phase1SendingIdentityAuthorizer`、`AuditLogger`、`StandardAuditLogger`。
- Later tasks may import domain internals only from within this domain or `infra/db`; apps/other domains consume only `service.py` and `schemas.py`.

本任务同时把 `repository.py` 收敛为以下七个 repo + UoW 契约；记录对象均为本域冻结 dataclass，不暴露 ORM：

```python
@dataclass(frozen=True)
class SendingDomain:
    tenant_id: TenantId
    domain: str
    role: DomainRole
    created_at: datetime

@dataclass(frozen=True)
class AuthenticationCheckRecord:
    auth_check_id: str
    tenant_id: TenantId
    identity_id: SendingIdentityId
    result: AuthenticationResult
    created_at: datetime

@dataclass(frozen=True)
class IdentityActionRecord:
    action_id: str
    tenant_id: TenantId
    identity_id: SendingIdentityId
    action_key: str
    action: SendingIdentityAction
    before_state: IdentityState | None
    after_state: IdentityState | None
    actor_id: str
    scope: str
    rule: str
    note: str | None
    occurred_at: datetime

class SendingDomainRepository(Protocol): ...
class SendingIdentityRepository(Protocol): ...
class AuthenticationCheckRepository(Protocol): ...
class ReputationRepository(Protocol): ...
class SendCounterRepository(Protocol): ...
class SendReservationRepository(Protocol): ...
class IdentityActionRepository(Protocol): ...

class SendingIdentityUnitOfWork(Protocol):
    domains: SendingDomainRepository
    identities: SendingIdentityRepository
    auth_checks: AuthenticationCheckRepository
    reputation: ReputationRepository
    counters: SendCounterRepository
    reservations: SendReservationRepository
    actions: IdentityActionRepository
    bus: EventBus

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

class SendingIdentityUnitOfWorkFactory(Protocol):
    def __call__(self, tenant_id: TenantId) -> SendingIdentityUnitOfWork: ...
```

`schemas.py` 的对外视图固定使用 `Decimal`：`ReputationView.hard_bounce_rate/complaint_rate/delivery_rate` 与 nearest threshold value/distance 都是 Decimal；`DomainReputationView` 包含 normalized domain、role、identity/active counts、聚合 `ReputationView`、`at_risk` 和 worst identity ID。`IdentityRegisterRequest.role` 是 `DomainRole` typed enum，不是自由字符串。

`errors.py` 收敛为固定安全分类：`InvalidSendingDomainError`、`InvalidSendingAddressError`、`InvalidConnectorReferenceError`、`InvalidAuthenticationResultError`、`InvalidDeliveryEventError`、`DomainRoleConflictError` 继承 `ValidationError`；`SendingIdentityNotFoundError`、`ColdOutreachDomainViolation`、`AuthenticationNotVerifiedError`、`IdentitySuspendedError`、`IdentityRetiredError`、`WarmupLimitExceededError` 继承 `PolicyViolation`。异常可包含 state/limit/remaining，不得包含 address/domain/ref/failure instruction/note。

- [ ] **Step 1: 写规范化、凭证引用和状态机 RED**

在 `tests/unit/test_sending_identity_models.py` 先覆盖：

```python
@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("EXAMPLE.COM.", "example.com"),
        ("bücher.example", "xn--bcher-kva.example"),
    ],
)
def test_normalize_sending_domain(raw: str, normalized: str) -> None:
    assert normalize_sending_domain(raw) == normalized


@pytest.mark.parametrize(
    "raw",
    ["", "localhost", "https://example.com", "a@example.com", "a..example", "a:443"],
)
def test_normalize_sending_domain_rejects_non_domain_shapes(raw: str) -> None:
    with pytest.raises(InvalidSendingDomainError):
        normalize_sending_domain(raw)


@pytest.mark.parametrize(
    "raw",
    ["", "Bearer abc", "https://vault/item", "a\nb", "password" + "=" + "value", "x" * 65],
)
def test_validate_connector_ref_rejects_secret_or_transport_shapes(raw: str) -> None:
    with pytest.raises(InvalidConnectorReferenceError):
        validate_connector_ref(raw)
```

Phase 1 邮箱规范化固定：strip、ASCII local part、小写 local/domain、恰一个 `@`、local 不允许引号/注释/空段，域经同一 IDNA normalizer；邮箱域必须等于 request normalized domain。

完整枚举测试每条允许/拒绝转换；`RETIRED` 无后继；`THROTTLED/SUSPENDED` 只恢复到持久化的 `WARMING/ACTIVE`，且从 throttled 升 suspended 不覆盖原值。

- [ ] **Step 2: 写固定 28 天预热曲线 RED**

使用注入日期，不接受调用方 schedule：

```python
@pytest.mark.parametrize(
    ("day_number", "expected"),
    [(0, 0), (1, 5), (3, 5), (4, 15), (7, 15), (8, 30),
     (14, 30), (15, 50), (21, 50), (22, 58), (28, 100), (29, 100)],
)
def test_warmup_plan_target_100_has_exact_boundaries(
    day_number: int, expected: int
) -> None:
    plan = WarmupPlan.create(date(2026, 8, 1), 100)
    assert plan.daily_limit_on(date(2026, 8, 1) + timedelta(days=day_number - 1)) == expected
```

另用 target 5/50/100 property-like loop 证明：28 个值单调不减、不超 target、第 28 天等于 target；target=5 仍在第 29 天前 `is_complete_on=False`。插值使用 `(numerator + 6) // 7`，不得用 `ceil(float(...))`。

- [ ] **Step 3: 写 Decimal 信誉 RED**

把骨架中所有 rate/threshold float 改为 Decimal：

```python
window = ReputationWindow(
    window_days=7,
    computed_at=datetime(2026, 8, 10, tzinfo=UTC),
    sent_attempts=100,
    delivered=93,
    hard_bounced=5,
    soft_bounced=2,
    complaints=1,
    unsubscribed=0,
)
assert window.hard_bounce_rate == Decimal("0.05")
assert window.complaint_rate == Decimal("0.01")
assert window.delivery_rate == Decimal("0.93")
```

零分母全部返回 `Decimal("0")`。`ReputationThresholds` 默认精确为 `.03/.05/.001/.003`，拒绝 float、bool、负值、throttle 大于等于 suspend、minimum sample 非真 int 或小于 1。

- [ ] **Step 4: 写 typed DTO 与公共签名 RED**

在 `schemas.py` 定义冻结 DTO：

```python
@dataclass(frozen=True)
class AuthenticationFailure:
    check: AuthCheck
    category: AuthenticationFailureCategory
    instruction: AuthenticationFixInstruction

@dataclass(frozen=True)
class AuthenticationResult:
    checked_at: datetime
    spf_passed: bool
    dkim_passed: bool
    dmarc_passed: bool
    failures: tuple[AuthenticationFailure, ...]
    check_ref: str

@dataclass(frozen=True)
class DeliveryEventRecord:
    event_type: DeliveryEventType
    occurred_at: datetime
    dedup_key: IdempotencyKey
    source_ref: str

@dataclass(frozen=True)
class SendReservation:
    reservation_id: str
    identity_id: SendingIdentityId
    reservation_key: IdempotencyKey
    on_day: date
    sequence: int
    daily_limit: int
    remaining_today: int
```

`AuthenticationFailureCategory` 固定为 `RECORD_MISSING/RECORD_INVALID/ALIGNMENT_FAILED/POLICY_INSUFFICIENT/LOOKUP_UNAVAILABLE`；instruction 是固定 enum code，不接收 DNS 原文。失败检查必须恰有 failure，成功检查不得附 failure。所有 datetime 必须 UTC aware；bool 不接受 `0/1` 冒充。

通过 `inspect.signature` 锁定 `Public Contract Target`，证明没有 `started_on/on_day/computed_at/skip_warmup/approved_by` 客户端时间或旁路参数。

- [ ] **Step 5: 写 Phase 1 权限矩阵 RED**

`SendingIdentityScope` 是 frozen DTO：`level=None` 默认无权限；`None` 维度=不限制、空集合=全拒；MANAGER 至少显式收窄 identity/domain 一个维度；SELF 不获得任何 action；SYSTEM 资源写 scope 的 identity 集合必须精确是目标单例。
`allowed_domains` 每项必须已经是规范化 domain；`actor_id` 必须是 1..64 的安全标识符且无空白/换行；不在权限层静默改写输入。

完整 allow 集合固定如下，未列组合全拒：

- boss/TENANT：register、begin auth、start warmup、identity read/list、reputation read、send-permission read、suspension resume、retire；
- manager/MANAGER：identity read/list、reputation read、send-permission read；
- system/SYSTEM singleton identity：auth result record、warmup advance、identity read、reputation read、send-permission read、send-slot reserve、delivery event record、reputation evaluate、throttle resume；
- sales/SELF：空集合。

参数化覆盖 15 个 action 的完整矩阵，以及错 tenant、空 actor、scope mismatch、manager unrestricted、system unrestricted、多 identity SYSTEM、未知 role/action 全拒。rule 固定为 `phase1:{role}:{scope}:{action}`，deny 文案固定，不包含 identity/domain/ref。

`StandardAuditLogger` 只记录固定中文 `授权审计` 与 `actor/action/tenant_id/scope/rule` 五个 extra 字段。
service 捕获 typed `PermissionDenied` 后写一条 `rule="deny:authorization"` 的安全 deny audit 再重抛；authorizer 不自行记录日志，避免重复。

- [ ] **Step 6: 运行 genuine RED**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_sending_identity_models.py \
         tests/unit/test_sending_identity_contracts.py \
         tests/unit/test_sending_identity_permissions.py -q -W error
```

预期：只因新 enum/validator/permissions/签名缺失或骨架 `NotImplementedError` 失败；不得把 import、AppleDouble、fixture 或 warning 失败计为 RED。

- [ ] **Step 7: 最小实现并跑 GREEN**

实现纯函数、冻结 DTO、typed permissions 和 Protocol；`models.py` 不保留 float 或 `NotImplementedError`。运行：

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_sending_identity_models.py \
         tests/unit/test_sending_identity_contracts.py \
         tests/unit/test_sending_identity_permissions.py -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  ruff check domains/sending_identity tests/unit/test_sending_identity_*.py --no-cache
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  mypy domains/sending_identity
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python3 scripts/check_boundaries.py
git diff --check
```

- [ ] **Step 8: 独立审查、全门禁、提交与 CI**

审查重点：凭证形态拒绝、Decimal/整数曲线、PUBLIC/INTERNAL 导入、权限默认拒绝、错误文案脱敏。修复 findings 后运行 `make check` 与 `pytest tests/integration -q -W error`。精确 stage 9 个任务文件，staged sensitive/cached diff 通过后：

```bash
git commit -m "feat(sending-identity): define domain contracts and permissions"
git push origin codex/phase1-implementation
```

等待 CI success 后再开始 Task 2。

---

### Task 2: 0008 Migration、ORM、Repositories、UoW 与 Outbox Registry

**Files:**
- Modify: `domains/sending_identity/repository.py`
- Create: `migrations/versions/0008_sending_identity.py`
- Modify: `infra/db/tables.py`
- Create: `infra/db/repositories/sending_identities.py`
- Create: `infra/db/sending_identity_uow.py`
- Modify: `infra/db/outbox.py`
- Modify: `tests/integration/test_migrations.py`
- Create: `tests/integration/test_sending_identity_repositories.py`
- Modify: `tests/integration/test_outbox_transaction.py`
- Modify: `tests/integration/test_repositories.py`
- Modify: `tests/unit/test_outbox_serialization.py`
- Modify: `domains/sending_identity/models.py`
- Modify: `tests/unit/test_sending_identity_models.py`

**Interfaces:**
- Consumes: Task 1 entity/DTO/repository Protocol、`TenantScopedRepository`、`PostgresEventBus`、现有四个 sending identity catalog events。
- Produces: 0008 七表；对应七个 ORM Row；`SendingIdentityRepositoryImpl`、`AuthenticationCheckRepositoryImpl`、`ReputationRepositoryImpl`、`SendCounterRepositoryImpl`、`SendReservationRepositoryImpl`、`IdentityActionRepositoryImpl`；`SqlAlchemySendingIdentityUnitOfWork(factory, tenant_id, *, now=None)`。
- Later services receive only a `SendingIdentityUnitOfWorkFactory` Protocol, never AsyncSession.

**Protocol conflict ruling（2026-08-10）：** 权威设计要求 reservation 时间只来自 service 注入时钟，因此 Task 2 给 `SendReservationRepository.reserve_if_below` 补回必需的 `created_at: datetime`。其余以 Task 1 已审查契约为准：domain 写入留在独立 `SendingDomainRepository.add`，信誉身份窗口方法名保持 `compute_window`。这三项分别保证单一时间权威、repository 单一职责和已发布方法名稳定。

**Caller parity ruling（2026-08-10）：** 全库既有 ORM metadata 与 outbox registry 测试使用精确全集断言。0008 新增信誉索引与四个已批准事件后，这两条 caller 测试必须同步更新；禁止通过隐藏 metadata 或 registry 项规避。因此 Task 2 精确扩展到上述 11 文件，仅适配 `tests/integration/test_repositories.py` 的索引全集和 `tests/unit/test_outbox_serialization.py` 的事件白名单全集。

**Review R1 contract ruling（2026-08-10）：** 独立审查确认 `suspension_category` 已进入权威设计与 0008 数据契约，但 Task 1 实体遗漏 typed 表示，导致 repository 无法诚实 round-trip。Task 2 修复轮窄扩到上述 13 文件：在 `models.py` 增加固定 suspension category / reputation metric / severity 类型并让身份实体持有、校验该 category；`test_sending_identity_models.py` 先行覆盖。四个既有共享事件的字段签名保持不变，outbox 持久化边界用这些 typed vocabulary 和受限 Decimal 字符串 fail closed，避免未经 ADR 改写公共事件字段。

**Review R2 encoding ruling（2026-08-10）：** `SendingIdentityId` 的唯一可持久化 wire 形态固定为 `sid_` + 26 位 Crockford Base32 ULID（总长 30，适配 `VARCHAR(32)`）；Task 3 `register` 必须使用 `SendingIdentityId(new_id("sid"))`。outbox 比率字段接受领域 Decimal 默认上下文产生的非指数、有限、`0..1` 字符串（coefficient 最多 28 位有效数字；`1/51` 因前导小数零可有 29 位小数），不量化或舍入到六位；继续拒绝指数、NaN/Infinity、符号、越界和自由文本。若 commit/body 已有主异常，rollback/close 的任何 `BaseException` 只写固定脱敏清理日志，不得覆盖主异常。

Task 2 在 `repository.py` 已声明的内部结果类型上实现原子 reservation：

```python
class ReservationOutcome(str, Enum):
    CREATED = "created"
    EXISTING = "existing"
    CAP_REACHED = "cap_reached"

@dataclass(frozen=True)
class ReservationResult:
    outcome: ReservationOutcome
    reservation: SendReservation | None
    sent_attempts: int
```

`CAP_REACHED` 时 reservation 必须为 None；其他两种必须非 None。repository 不抛业务 cap error，由 service 映射为 `WarmupLimitExceededError`。

- [ ] **Step 1: 写 migration/ORM parity 与 roundtrip RED**

扩展 `test_migrations.py`，按 `Data Contract Target` 精确比较列、类型、nullable、PK、unique、FK、CHECK、index 和 trigger。用隔离 DB 执行：

```text
upgrade head(0008) → inspect seven tables → downgrade 0007 → seven absent → upgrade head
```

真实 SQL 证明：跨租户 composite FK、同租户重复 address、同域角色冲突、reservation 重复 sequence、负 counter/sequence 被数据库拒绝；auth/reputation/reservation/action UPDATE/DELETE 被 append-only trigger 拒绝；counter decrease 被 guard 拒绝。

- [ ] **Step 2: 写 repository tenant isolation 与映射 RED**

覆盖每个 repository 的 add/get/list/update；所有读均显式 tenant predicate，错 tenant 返回 None/[]，所有写 tenant mismatch 固定拒绝。CRITICAL 日志固定 `检测到跨租户数据隔离违规`，extra 只含 repository/tenant_id/rule，不含 address/domain/ref。

`SendingIdentityRepository` 必须提供：

```python
async def get(..., *, for_update: bool = False) -> SendingIdentity | None: ...
async def list_domain_for_update(... ) -> list[SendingIdentity]:  # identity_id ASC
async def find_domain_role(... ) -> DomainRole | None: ...
async def list_available_for_campaign(..., scope, limit) -> list[SendingIdentity]: ...
```

域名写入使用 UoW 的独立 `SendingDomainRepository.add(domain: SendingDomain)`；不得在 `SendingIdentityRepository` 重复增加 `add_domain`。

`list_available_for_campaign` 用 correlated latest-auth row（`checked_at DESC, auth_check_id DESC`）并在 SQL WHERE 中完成 tenant、scope、role、三项 auth、state 过滤，再按 `(created_at,identity_id)` 排序和 LIMIT。测试插入超过 limit 的不合格早期行，证明不是先 limit 后 Python 过滤。
repository 直调也 fail closed：`level=None`、无限制 SYSTEM、MANAGER 未收窄、tenant mismatch 均返回空，不退化为 tenant-wide 查询。

- [ ] **Step 3: 写原子 reservation repository RED**

repository 接口固定：

```python
async def reserve_if_below(
    tenant_id: TenantId,
    identity_id: SendingIdentityId,
    reservation_key: IdempotencyKey,
    on_day: date,
    daily_limit: int,
    created_at: datetime,
) -> ReservationResult: ...
```

实现必须在同 session 中：先查 reservation key；锁/创建当日 counter；若已满返回 typed `CAP_REACHED`；否则 counter +1 并插 reservation，sequence 等于递增后计数。唯一冲突只在精确 reservation key 冲突时恢复为原 reservation；其他 IntegrityError 原样 rollback，不做字符串分类。

真实 PostgreSQL 并发测试留在 Task 4；本任务先证明单事务映射、幂等与 rollback。

- [ ] **Step 4: 写滚动窗口 SQL RED**

`ReputationRepositoryImpl.compute_window(..., computed_at)` 与 `compute_domain_window`：

- 分母从 immutable reservations 的 `created_at` 在 `[computed_at-7d, computed_at]` 精确计数，不从日 counter 推测；
- event 边界精确 `occurred_at >= computed_at - timedelta(days=7)` 且 `<= computed_at`；
- domain query 先 tenant+domain 连接 identity，再聚合；
- 返回整数计数，Decimal 比率仍由纯领域模型计算。

边界 `-7d` 与 `computed_at` 含入，前后 1 微秒排除；跨租户、同名跨租户 domain 不混入。

- [ ] **Step 5: 写 UoW/outbox RED**

`SqlAlchemySendingIdentityUnitOfWork.__aenter__` 从 factory 创建新 AsyncSession，装配所有 repositories 与同 session `PostgresEventBus`；正常 exit commit，异常 rollback，finally close。连续两次 enter session identity 不同。

把四个现有事件加入 `EVENT_REGISTRY`，补 serialize/deserialize roundtrip 与 unknown-event fail closed。事件断言 payload 不含 address/domain/connector_ref/note。

- [ ] **Step 6: 运行 genuine RED 后实现 GREEN**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/integration/test_migrations.py \
         tests/integration/test_sending_identity_repositories.py \
         tests/integration/test_outbox_transaction.py -q -W error
```

预期仅缺 0008/Row/repository/UoW/registry。实现后同命令全绿，再跑 scoped ruff/mypy、boundary、sensitive 与 diff check。

- [ ] **Step 7: 独立审查、全门禁、提交与 CI**

审查重点：migration/ORM parity、复合 tenant FK、append-only、counter 单调、SQL WHERE-before-LIMIT、latest auth tie-break、IntegrityError 分类、session 生命周期、事件白名单。

```bash
git commit -m "feat(sending-identity): add postgres persistence"
git push origin codex/phase1-implementation
```

精确 stage 9 文件并等待 CI success。

---

### Task 3: 生命周期 Service、认证门禁与查询

**Files:**
- Create: `domains/sending_identity/service_impl.py`
- Create: `tests/unit/test_sending_identity_service.py`
- Create: `tests/integration/test_sending_identity_lifecycle.py`

**Interfaces:**
- Consumes: Task 1 public contract/permissions、Task 2 UoW factory/repositories/outbox。
- Produces: `SendingIdentityServiceImpl(uow_factory, authorizer, audit_logger, *, now)` 的 register/auth/warmup/resume-suspension/retire/get/list/progress 方法。本任务的 concrete class 不显式继承或声称已满足完整 `SendingIdentityService` Protocol；Task 4 增加 reservation 方法，Task 5 增加 reputation/throttle 方法后才用静态赋值测试证明完整 structural conformance。任何阶段都不加入占位实现或 `NotImplementedError`。

- [ ] **Step 1: 写 service fake UoW 行为 RED**

fake 记录严格调用顺序。每个 public method 首先 `authorizer.require`；写操作在 UoW 成功退出后才 `audit.log`。参数化 commit error、validation error、not found、state error、tenant corruption，全部零 allow audit。

tenant corruption 使用共用 `_deny_tenant_isolation`：固定中文 CRITICAL + deny audit，随后 `TenantIsolationViolation`；日志不含实体字段。

- [ ] **Step 2: 写 register/auth 状态 RED**

覆盖：

- register 只用 `SendingIdentityId(new_id("sid"))` 生成 `CREATED`，标准化 address/domain/ref；同 tenant address 幂等返回既有 ID 仅当全部安全字段一致，否则固定冲突；
- domain row 在 transaction 内锁定/创建；并发同 role 可共存，不同 role 只有一个成功；
- begin 只允许 CREATED→AUTH_PENDING；
- auth history 总是 append；部分通过留 AUTH_PENDING；全过也不自动 WARMING；
- checked_at 早于 identity.created_at 或晚于 service `now+5m` 拒绝；乱序的较旧结果只进 history，不覆盖 latest、不触发 regression；同 checked_at 以 auth_check_id DESC 确定唯一最新行；
- 相同 check_ref + 相同 typed result 幂等 no-op，相同 check_ref + 不同结果固定拒绝，不用异常文本判断 unique；
- WARMING/ACTIVE 最新 auth 退化立即 SUSPENDED、保存原 WARMING/ACTIVE、action+outbox 同事务；THROTTLED 退化升级 SUSPENDED 但不覆盖原 sendable state；
- RETIRED 不记录新结果。

每次状态 action key 固定为 `identity:{identity_id}:v{next_version}:{action}`。transaction retry 使用同 next version；commit 后相同调用因当前状态不再重复发布。

- [ ] **Step 3: 写 warmup/恢复/退役 RED**

`start_warmup` 只允许 AUTH_PENDING 且 latest auth all passed；开始日来自 `now().date()`。`advance_warmup` day 28 仍 WARMING，day 29 转 ACTIVE 并只发布一次 Activated。

`resume_from_suspension` 仅 boss/TENANT action、note strip 后 1..1000 字符、最新 auth all passed，恢复到 saved WARMING/ACTIVE；note 只写 action 表，不进 audit/event/error。`retire` 任意非 retired → retired，重复 retire 幂等或固定 InvalidState，但测试与实现必须选一种并锁定；本计划选择**重复 retire 幂等 no-op 且不重复 action/outbox/audit allow 以外副作用**。

- [ ] **Step 4: 写 query/ABAC RED**

get/list/reputation/progress 都先 authorize；requested scope 必须等于 actor.scope。repository 结果逐行复核 tenant、identity/domain scope、role/auth/state；任何损坏整单失败，不返回部分数据。

`limit` 必须真 int `1..200`，但 authorizer 在 limit validation 之前执行。`IdentityView` 不返回 connector_ref、auth check_ref、investigation note。`AuthStatusView` 只返回固定 failure category/instruction code 与固定中文显示文案。

- [ ] **Step 5: 真实 PostgreSQL 生命周期 RED/GREEN**

用真实 UoW 验证状态、auth history、action history、outbox 同事务；注入 commit failure 后三者全无且零 allow。并发 domain role、重复 address、auth regression 与 activation 事件只一次。

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_sending_identity_service.py \
         tests/integration/test_sending_identity_lifecycle.py -q -W error
```

- [ ] **Step 6: 独立审查、全门禁、提交与 CI**

审查重点：公开方法时间不可回拨、auth 不自动预热、恢复不跳过 warmup、commit 后 audit、tenant row defense、事件只一次、note/ref 不泄露。

```bash
git commit -m "feat(sending-identity): implement lifecycle service"
git push origin codex/phase1-implementation
```

精确 stage 3 文件，等待 CI success。

---

### Task 4: 原子发送名额 Reservation

**Files:**
- Modify: `domains/sending_identity/service_impl.py`
- Modify: `tests/unit/test_sending_identity_service.py`
- Create: `tests/integration/test_sending_identity_reservations.py`

**Interfaces:**
- Consumes: `check_send_permission` 与 `reserve_if_below`。
- Produces: 只读诊断 `check_send_permission` 和外部发送唯一权威门禁 `reserve_send_slot`；后续 4B 只能调用 reservation，不能把 check 结果当授权缓存。

- [ ] **Step 1: 写 gate matrix RED**

对 CREATED/AUTH_PENDING/THROTTLED/SUSPENDED/RETIRED、latest auth 未全过、before-start、WARMING day1/day28、ACTIVE、错误 domain role、identity/domain 任一当前窗口已超阈值、scope 不含 identity 全部覆盖。

`PRIMARY_BUSINESS`/`TRANSACTIONAL` + `for_cold_outreach=True` 抛 `ColdOutreachDomainViolation`，没有 bypass 参数；permission DTO 原因固定中文，不含 address/domain。

- [ ] **Step 2: 写幂等与 cap RED**

同 reservation key 重试返回同 reservation 和相同 sequence/remaining，不重复计数。不同 key 到 cap 后抛 `WarmupLimitExceededError`。`reservation_key` strip 后非空、≤200、拒绝换行/secret-like；当前 UTC date 来自 service clock。

- [ ] **Step 3: 写真实并发 RED**

真实 PostgreSQL 起始 count=3、daily limit=5，同时启动 20 个独立 UoW reservations：成功恰 2、最终 count=5、sequence `{4,5}`、其余固定 cap error。另 20 个相同 key 并发：全部获得同一 reservation ID，counter 只 +1。

同时用两个 identities 同域并发，证明固定锁序 domain→identity ASC→counter/reservation 无死锁；测试设置 timeout，deadlock/serialization error 不得被吞成业务 cap。

- [ ] **Step 4: 写 DB failure/audit RED**

counter update 后 reservation insert 人为 unique failure、commit failure、domain/identity corruption 都 rollback counter/reservation，零 allow audit。成功与幂等命中各恰一条 allow audit；幂等命中不写第二条 action/outbox。

- [ ] **Step 5: 最小实现 GREEN**

service 在同一 UoW 中先锁 domain、再锁目标 identity、读 latest auth/窗口、算 limit，最后调用 atomic repository。`check_send_permission` 复用纯 `_decide_send_permission(...)`，但 reservation 必须重新加载和重新计算，不调用公共 check 后相信旧结果。

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_sending_identity_service.py \
         tests/integration/test_sending_identity_reservations.py -q -W error
```

- [ ] **Step 6: 独立审查、全门禁、提交与 CI**

审查重点：TOCTOU、锁序、idempotency unique recovery、cap 边界、无退款 API、诊断 check 非权威、role 永不旁路、错误/日志不泄实体内容。

```bash
git commit -m "feat(sending-identity): reserve send slots atomically"
git push origin codex/phase1-implementation
```

等待 CI success。

---

### Task 5: 信誉事件、Decimal 滚动窗口与域名级熔断

**Files:**
- Modify: `domains/sending_identity/service_impl.py`
- Modify: `tests/unit/test_sending_identity_service.py`
- Create: `tests/unit/test_sending_identity_reputation.py`
- Create: `tests/integration/test_sending_identity_reputation.py`

**Interfaces:**
- Consumes: Task 2 reputation/window repositories、Task 3 lifecycle action/outbox helpers。
- Produces: `record_delivery_event`、`evaluate_reputation`、`resume_from_throttle` 的完整实现；身份级和域名级 fuse 同事务。

- [ ] **Step 1: 写事件验证与 dedup RED**

`DeliveryEventRecord` 只接受 typed enum、UTC aware time、非空安全 dedup/source ref；service 拒绝早于 identity.created_at、晚于 `now+5m`。重复 dedup 返回 False，不更新 history/state/outbox/audit allow 之外的任何业务数据；因这是成功幂等调用，仍记录一次安全 allow audit，测试锁定该选择。

- [ ] **Step 2: 写阈值 truth table RED**

参数化证明：

- sample 49 时 100% 比率不触发普通 ratio fuse；sample 50 时 `>=` 阈值触发；
- hard bounce `.03` throttle、`.05` suspend；complaint `.001` throttle、`.003` suspend；
- spam trap/blocklist 一次立即 suspend，zero sample 也生效；
- 同时命中选择最严重 `SUSPENDED`；
- THROTTLED/SUSPENDED/RETIRED 不发生非法降级；
- 所有比较对象是 Decimal exact value。

- [ ] **Step 3: 写域名聚合 RED**

三个 identity 各自因 `sent_attempts < minimum_sample` 不参与普通 ratio fuse、但 domain 聚合样本达到 minimum 且 ratio 达阈值时，锁 domain，再 identity_id ASC 锁全部 WARMING/ACTIVE/THROTTLED 身份；所有可发送身份一起进入相同 restriction，原 sendable state 分别保存。不要声称相同阈值下的加权聚合率能高于每个成员率；域名层在 Phase 1 的新增价值是合并不足样本并统一保护同域。每个被改变身份有一条 action 与对应 Throttled/Suspended outbox；触发源另有一条 `ReputationThresholdBreached`。重复 evaluate 不重复事件。

跨租户同名 domain 永不混入；同域 `PRIMARY_BUSINESS` 数据也只在其独立 tenant/domain role 行内聚合。

- [ ] **Step 4: 写恢复 80% RED**

`resume_from_throttle` 只允许 SYSTEM singleton scope。身份和 domain hard bounce 必须都 `< .024`，complaint 都 `< .0008`，并且窗口无 spamtrap/blocklist；**等于** 80% 仍拒绝。成功恢复到 saved WARMING/ACTIVE，清 restriction fields；认证不全拒绝。SUSPENDED 不能走此方法。

- [ ] **Step 5: 写真实事务/竞争 RED**

真实 PostgreSQL 覆盖：event+state+actions+outbox 同事务；commit failure 全 rollback；同 dedup 并发仅一事件；两个 identity 同时触发 domain suspension 不漏停、无重复 outbox、无 deadlock。outbox payload 逐键断言不含 address/domain/source_ref/ref/note。

- [ ] **Step 6: 最小实现 GREEN**

`record_delivery_event` 在一个 UoW 内 append-if-new 后立即调用私有 `_evaluate_locked(...)`；不能先 commit event 再另事务 fuse。`evaluate_reputation` 使用同 helper。severity 纯函数输入 identity/domain windows + thresholds，输出 typed decision；不在查询中隐式恢复。

本任务增加 mypy structural conformance probe：

```python
def assert_service_contract(service: SendingIdentityServiceImpl) -> None:
    public_service: SendingIdentityService = service
    assert public_service is service
```

该 probe 在 Task 5 前不加入；Task 5 完成后证明 concrete class 已实现 Task 1 的全部公共方法。

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_sending_identity_service.py \
         tests/unit/test_sending_identity_reputation.py \
         tests/integration/test_sending_identity_reputation.py -q -W error
```

- [ ] **Step 7: 独立审查、全门禁、提交与 CI**

审查重点：窗口边界、Decimal、minimum sample、immediate hazards、domain aggregate、锁序、恢复严格 `<80%`、dedup、transaction atomicity、outbox 安全字段。

```bash
git commit -m "feat(sending-identity): enforce reputation circuit breakers"
git push origin codex/phase1-implementation
```

等待 CI success。

---

### Task 6: 无网络 PostgreSQL Demo、架构文档同步与最终验收

**Files:**
- Create: `scripts/demo_sending_identity.py`
- Create: `tests/integration/test_demo_sending_identity.py`
- Modify: `domains/sending_identity/AGENTS.md`
- Modify: `docs/architecture/07-sending-identity.md`

**Interfaces:**
- Consumes: `SqlAlchemySendingIdentityUnitOfWork`、`SendingIdentityServiceImpl`、Phase1 authorizer、现有 engine/session factory。
- Produces: `async def main() -> None` demo 与同步 CLI exit boundary；不产出 production composition，不读 Gmail/DNS 环境变量。

- [ ] **Step 1: 写 demo subprocess genuine RED**

测试用真实 testcontainers PostgreSQL + Alembic head，以最小 env `{"DATABASE_URL": db_url}` 启动 `sys.executable scripts/demo_sending_identity.py`。脚本缺失时 RED 必须精确是 file missing；Docker/migration/import/PATH 正常。

另用无效 DSN marker 证明 nonzero + stderr 固定 `发件身份演示运行失败`，stdout/stderr 都不含 DSN、driver error 或 traceback。

- [ ] **Step 2: 实现真实 demo 行为**

脚本只读取 `os.environ["DATABASE_URL"]`，创建随机 tenant 与两个冷开发 identity；使用真实 service 完成：register→begin auth→record typed pass→start warmup→day 1 并发 reservation 证明低 cap→clock 到 day 29 并显式 advance 激活→让两个 identity 各累计低于 50、domain 合计恰达 50 个 reservation→注入去重后的 3 个 hard-bounce 事件，使单 identity 因样本不足不独立判定而 domain 聚合 6% 触发 suspension。不得直接 INSERT 任何七张业务表；Alembic/测试 fixture 负责 schema。

stdout 只输出一行安全 JSON：tenant_id、identity IDs、最终 state、reservation success count、history/outbox counts；不输出 address/domain/ref。内部 structured logs 固定字段；异常只固定中文边界，finally dispose engine。

- [ ] **Step 3: 独立 DB read-back 验收**

subprocess 返回后，测试用独立 session 按 stdout tenant 过滤并断言：2 identities、auth history、warming actions、cap 未突破、reservation key 去重、reputation events 去重、两 identity suspended、action/outbox 精确数量。无跨 tenant delete/cleanup；随机 tenant 保证重复运行不冲突，并在同一 DB 连跑两次证明两次都成功。

- [ ] **Step 4: 同步架构文档**

同步 `domains/sending_identity/AGENTS.md` 与 `docs/architecture/07-sending-identity.md`：角色为 `cold_outreach/primary_business/transactional`；预热曲线与 day29；阈值为 `.03/.05/.001/.003` + sample 50 + immediate hazards；THROTTLED 语义为阻断新冷开发；SUSPENDED 只能 boss 留调查记录后恢复到先前 WARMING/ACTIVE；明确 4A 不含 suppression/Gmail/DNS/真实发送。不得提前把 HANDBOOK Slice 4 整体标为完成。

- [ ] **Step 5: 运行完整本地验收**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_sending_identity_*.py -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/integration/test_sending_identity_*.py \
         tests/integration/test_demo_sending_identity.py -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/integration -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python3 scripts/check_boundaries.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python3 scripts/scan_sensitive.py
git diff --check
```

- [ ] **Step 6: 最终独立 review**

创建 immutable diff package，进行实现、测试强度、安全、migration、并发、计划符合性五类审查。review 必须逐项对照设计验收 1–10；所有 Critical/Important 先补 RED、修复、复审。Minor 建 ledger，不得用 Minor 掩盖未证明的硬边界。

- [ ] **Step 7: 精确 stage、提交、push 与 CI**

确认工作树只含 4 个任务文件；新 Python 文件 cached mode 100644；运行 staged sensitive/cached diff：

```bash
git commit -m "feat(sending-identity): add postgres demo and final docs"
git push origin codex/phase1-implementation
```

等待 CI success 后，4A 才可声明完成。随后返回 brainstorming/spec 门禁设计 4B；不得直接开始 Gmail/Tool Gateway 编码。

---

## Final Acceptance Matrix

| 设计验收 | 证明任务 |
|---|---|
| 主业务/transactional 永不冷发 | Task 1 role policy + Task 4 gate + Task 2 DB role lock |
| 未认证/未预热不可 reservation | Task 3 lifecycle + Task 4 gate/concurrency |
| 并发不超 cap、幂等不重复 | Task 2 atomic repo + Task 4 real PostgreSQL race |
| Decimal/7-day/minimum sample | Task 1 pure model + Task 2 SQL boundary + Task 5 truth table |
| identity/domain 自动熔断 | Task 5 domain race/outbox |
| suspended boss 恢复、retired 不可逆 | Task 1 permissions + Task 3 lifecycle |
| tenant/outbox/audit 顺序 | Task 2 composite constraints + Task 3/4/5 failure tests |
| 凭证与客户内容不泄露 | 所有任务 sensitive assertions + Task 6 subprocess |
| 无网络真实 PostgreSQL demo | Task 6 |
| 本地门禁/review/远端 CI | 每任务 final step |

## Plan Self-Review Checklist

- [x] 每个设计章节至少映射到一个任务和一个可观察断言。
- [x] Public service 参数、DTO、repository/UoW 生产者与消费者名称一致。
- [x] 没有未决占位标记、模糊类比措辞、任意选择措辞或未定义类型。
- [x] 没有把 Gmail、DNS、Tool Gateway、Suppression、Campaign、API、UI 偷渡进 4A。
- [x] 没有修改现有 shared event 字段；新增 outbox registry 有 roundtrip 测试。
- [x] 所有时间来自注入 clock，所有比率来自 Decimal，所有写路径含 tenant 与 post-commit audit。
- [x] 每任务可独立 RED/GREEN、review、commit、push、CI，失败时不会污染下一任务。
