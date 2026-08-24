# Company Playbook Versioned Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an immutable, tenant-scoped Company Playbook version chain whose initial configuration and every revision require independent approval before automatic activation.

**Architecture:** `domains/organization` owns immutable version payloads and append-only activation facts; `domains/approvals` owns decision/application state. A durable `workflows/playbook_change` run submits the exact content hash for approval, waits for `ApprovalDecided`, validates the approved fact and base version, activates idempotently, then marks the approval applied. API and Web expose only read/propose/status operations; there is no direct update, force, or apply endpoint.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x async, PostgreSQL/Alembic, pytest/pytest-asyncio, testcontainers, Python Playwright, Vue 3, TypeScript, Vite, Vitest, openapi-typescript.

**Spec:** `docs/superpowers/specs/2026-08-24-company-playbook-approval-design.md`

**Execution environment:** Run every backend command shown below through `/Users/xueziheng/miniconda3/bin/conda run -n tradeos-py312`; for example, Task 1's first command is `/Users/xueziheng/miniconda3/bin/conda run -n tradeos-py312 pytest -q tests/unit/test_organization_playbook.py`. Run `make check` through the same prefix. Do not use macOS `/usr/bin/python3` 3.9; the repository contains Python 3.12 syntax. Run commands from `/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/handbook-phase1-slice4-execution` unless a step explicitly changes into `apps/web`.

## Global Constraints

- 首次配置和后续修改都必须由不同于提交人的 boss 或 manager 审批；不提供 bootstrap 旁路。
- 金额只使用 `Decimal`/`Money`；JSON 金额只接受十进制字符串，拒绝 float、int、bool 和非有限值。
- 所有 Playbook 版本、激活记录、Repository 方法和查询都必须带 `tenant_id`。
- `domains/organization` 不得导入 `domains/approvals`；跨域编排只放 `workflows/playbook_change`。
- 候选版本和 activation 全部 append-only；公共 API 不提供 update/delete/force/override/apply-now。
- API 与 domain service 双重判权；只有 boss 可读/提交，只有 composition root 构造的 system actor 可激活。
- 模型、API、Web、日志和 workflow context 永不接触或返回 Token、Secret、Cookie、数据库凭证或 Connector Key。
- Playbook 生效只解除 `PLAYBOOK_NOT_CONFIGURED`；`contact.enrich` 继续因国家政策包未配置而不可用。
- 客户可见内容规则不在本切片改变；内部 UI 和错误摘要使用中文，代码标识符使用英文。
- 每个生产行为先写测试并观察预期失败，再写最小实现；提交前运行 `python3 scripts/check_boundaries.py`。

---

## File Map

### Domain and ADR

- Create `docs/adr/0009-playbook-version-approval-contract.md`: record the cross-domain contract, append-only activation decision and no-bootstrap rule.
- Modify `shared/schemas/identifiers.py`: add `PlaybookVersionId` and `PlaybookActivationId`.
- Modify `domains/organization/models.py`: immutable `CompanyPlaybookVersion`, `PlaybookActivation`, canonical content and hash helpers.
- Create `domains/organization/permissions.py`: typed read/propose/activate actions and Phase 1 authorizer.
- Replace `domains/organization/schemas.py`: strict proposal commands, approval fact and public views.
- Replace `domains/organization/service.py`: safe public Protocol; remove direct `update_playbook`.
- Replace `domains/organization/repository.py`: version/activation repositories and UoW protocols.
- Modify `domains/organization/errors.py`: not-configured, idempotency, approval-fact, activation-replay and base-conflict errors.
- Create `domains/organization/service_impl.py`: authorization, proposal, read and activation behavior.

### Persistence

- Create `migrations/versions/0031_company_playbook.py`: version/activation tables, constraints, indexes and immutable triggers.
- Modify `infra/db/tables.py`: SQLAlchemy rows matching migration 0031.
- Create `infra/db/repositories/organization.py`: tenant-bound repositories with advisory locks.
- Create `infra/db/organization_uow.py`: shared transaction for organization repositories.

### Approval workflow and composition

- Modify `domains/approvals/models.py`: add `PLAYBOOK_CHANGE` and seven-day validity.
- Modify `domains/approvals/schemas.py`: expose safe `change_set_ref` needed for event correlation.
- Modify `domains/approvals/service_impl.py`: label and view mapping.
- Create `workflows/playbook_change/AGENTS.md`, `__init__.py`, `flow.py`, `steps.py`: durable approval/application flow.
- Modify `apps/api/dependencies.py`: add `organization` dependency.
- Modify `apps/api/composition/runtime.py`: construct organization service and register the start-only workflow definition.
- Modify `apps/scheduler_worker/runtime.py`: optional real Playbook composition, handlers and event registration.

### API and Web

- Replace `apps/api/routers/settings.py`: active/version/proposal endpoints.
- Modify `apps/web/src/api/api.d.ts`: regenerate from OpenAPI; never edit by hand.
- Replace `apps/web/src/views/settings/SettingsCenter.vue`: active version, proposal form, diff and approval state.
- Create `apps/web/tests/settings.test.ts`: real Settings behavior tests.
- Create `tests/e2e/test_playbook_settings.py`: real Postgres + API + Vite + Chromium Settings acceptance.

### Tests and documentation

- Expand `tests/unit/test_organization_playbook.py`.
- Create `tests/unit/test_organization_service.py`.
- Create `tests/integration/test_organization_service_postgres.py`.
- Create `tests/unit/test_approval_service.py`.
- Create `tests/unit/test_playbook_change_workflow.py`.
- Create `tests/unit/test_settings_router.py`.
- Create `tests/integration/test_organization_repository.py`.
- Create `tests/integration/test_playbook_change_postgres.py`.
- Modify `tests/integration/test_migrations.py`, `tests/integration/test_repositories.py`, `tests/integration/test_api_runtime.py`, `tests/integration/test_scheduler_worker.py`.
- Modify `HANDBOOK.md` and `docs/architecture/04-tool-gateway.md` only after implementation evidence is green.

---

### Task 1: Lock the ADR and pure organization contracts

**Files:**
- Create: `docs/adr/0009-playbook-version-approval-contract.md`
- Modify: `shared/schemas/identifiers.py`
- Modify: `domains/organization/models.py`
- Create: `domains/organization/permissions.py`
- Replace: `domains/organization/schemas.py`
- Replace: `domains/organization/service.py`
- Replace: `domains/organization/repository.py`
- Modify: `domains/organization/errors.py`
- Test: `tests/unit/test_organization_playbook.py`

**Interfaces:**
- Consumes: existing `Money`, `Provenance`, `SourceType`, `TenantId`, `EmployeeId`, `IdempotencyKey`, `new_id`.
- Produces: `PlaybookVersionId`, `PlaybookActivationId`, `PlaybookProposalCreate`, `PlaybookApprovalFact`, `PlaybookVersionView`, `PlaybookChangeSnapshot`, `PlaybookActivationView`, `OrganizationScopeLevel`, `OrganizationScope`, `OrganizationActor`, `OrganizationService`, `OrganizationUnitOfWorkFactory`.

- [ ] **Step 1: Add failing pure-contract tests**

Add tests that hand-derive the expected canonical payload/hash, reject JSON numeric inputs, and exercise the default-deny permission matrix. The mutation each test catches must be named in the test name. Include one content-change test proving a real business-field change changes the hash, and one test proving every candidate has `SourceType.EMPLOYEE_INPUT` Provenance tied to its exact version ID and proposer.

```python
def test_playbook_hash_is_stable_after_list_normalization() -> None:
    first = CompanyPlaybookVersion.from_command(
        tenant_id=TenantId("tenant-one"),
        version_id=PlaybookVersionId("pbv_01K00000000000000000000000"),
        version_number=1,
        command=_command(excluded_categories=[" firearms ", "Adult", "adult"]),
        base_version_id=None,
        base_content_hash=None,
        proposed_by=EmployeeId("emp_01K00000000000000000000000"),
        proposed_at=NOW,
        idempotency_key=IdempotencyKey("settings-submit-1"),
    )
    second = CompanyPlaybookVersion.from_command(
        tenant_id=first.tenant_id,
        version_id=PlaybookVersionId("pbv_01K00000000000000000000001"),
        version_number=2,
        command=_command(excluded_categories=["adult", "firearms"]),
        base_version_id=None,
        base_content_hash=None,
        proposed_by=first.proposed_by,
        proposed_at=NOW,
        idempotency_key=IdempotencyKey("settings-submit-2"),
    )
    assert first.content_hash == second.content_hash
    assert first.excluded_categories == ("adult", "firearms")


@pytest.mark.parametrize("value", [10.5, 10, True, "NaN", "Infinity"])
def test_playbook_proposal_rejects_non_decimal_json_amount(value: object) -> None:
    with pytest.raises(ValidationError):
        PlaybookProposalCreate.model_validate(_payload(minimum_deal_amount=value))


def test_phase1_organization_authorizer_allows_only_boss_and_system_actions() -> None:
    authorizer = Phase1OrganizationAuthorizer(TenantId("tenant-one"))
    authorizer.require(_boss_actor(), OrganizationAction.PLAYBOOK_READ, TenantId("tenant-one"))
    authorizer.require(_boss_actor(), OrganizationAction.PLAYBOOK_PROPOSE, TenantId("tenant-one"))
    authorizer.require(_system_actor(), OrganizationAction.PLAYBOOK_CHANGE_SNAPSHOT_READ, TenantId("tenant-one"))
    authorizer.require(_system_actor(), OrganizationAction.PLAYBOOK_ACTIVATE, TenantId("tenant-one"))
    with pytest.raises(PermissionDenied):
        authorizer.require(_boss_actor(), OrganizationAction.PLAYBOOK_ACTIVATE, TenantId("tenant-one"))
    with pytest.raises(PermissionDenied):
        authorizer.require(_system_actor(), OrganizationAction.PLAYBOOK_READ, TenantId("tenant-one"))
```

In the same module, define `NOW` as a fixed aware UTC datetime; `_command` and `_payload` must return complete valid baselines and apply only named keyword overrides. `_boss_actor` and `_system_actor` must construct tenant-scoped `OrganizationActor` values with fixed typed employee IDs. Add a separate test proving `approval_requirements` is normalized only as an additive list of action IDs: blank, control-character and removal-shaped entries such as `!quote_send` are rejected, while unknown non-removal action IDs remain fail-closed through `requires_approval`.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
pytest -q tests/unit/test_organization_playbook.py
```

Expected: collection or assertion failure because version IDs, DTOs, immutable version models and authorizer do not exist. A failure caused only by a typo does not satisfy RED.

- [ ] **Step 3: Write ADR 0009 and minimal contracts**

The ADR status is `已接受`, date `2026-08-24`, and records these exact decisions:

```text
direct update_playbook is removed
version payloads and activations are separate append-only facts
workflow passes a narrow approved fact instead of importing approvals into organization
first configuration requires an independent approver
PlaybookVersionId/PlaybookActivationId are added to shared identifiers
employee-input provenance stays on the immutable version; approval confirmation and system activation times are separate activation facts
```

Define the new IDs:

```python
PlaybookVersionId = NewType("PlaybookVersionId", str)
PlaybookActivationId = NewType("PlaybookActivationId", str)
```

Define the authorization identity without a privileged default:

```python
class OrganizationScopeLevel(str, Enum):
    SYSTEM = "system"
    TENANT = "tenant"


@dataclass(frozen=True)
class OrganizationScope:
    level: OrganizationScopeLevel
    tenant_id: TenantId


@dataclass(frozen=True)
class OrganizationActor:
    actor_id: str
    scope: OrganizationScope
    role: str
```

Validate nonblank bounded actor IDs, exact enum/scope types and nonblank tenant IDs. The Phase 1 authorizer permits `boss + TENANT` only for ordinary read/propose and `system + SYSTEM` only for `PLAYBOOK_CHANGE_SNAPSHOT_READ`/activate; actor scope tenant, authorizer tenant and method tenant must all match. Do not add a convenience constructor that lets request code silently mint a system actor.

Define strict request fields without embedding identity or tenant:

```python
class PlaybookProposalCreate(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    company_type: str
    minimum_deal_amount: DecimalStringInput
    minimum_deal_currency: str
    excluded_categories: list[str] = Field(default_factory=list, max_length=200)
    sourcing_regions: list[str] = Field(default_factory=list, max_length=200)
    excluded_countries: list[str] = Field(default_factory=list, max_length=200)
    monthly_budget_credits: int | None = Field(default=None, ge=0)
    approval_requirements: list[str] = Field(default_factory=list, max_length=200)
    supply_capabilities_note: str | None = Field(default=None, max_length=4_000)
```

`DecimalStringInput` must use a `BeforeValidator` that accepts `Decimal` or a JSON string matching `(?:0|[1-9][0-9]*)(?:\.[0-9]+)?` only and rejects bool/int/float/exponent notation/non-finite/negative values, more than 28 total digits, or more than 12 fractional digits before persistence. OpenAPI describes it as `type: string`. Normalize the currency to a validated three-letter uppercase `CurrencyCode`; reject blank/control-character company type, list items and notes. NFKC/casefold/sort/deduplicate categorical lists, preserve trimmed semantic case in the free-text note, and reject removal-shaped approval actions. The idempotency key is a separate typed service argument and an HTTP `Idempotency-Key` header; it is not business content and is not accepted inside the proposal body.

Define the narrow cross-domain approval fact and public views in `domains/organization/schemas.py` with frozen/strict/extra-forbid Pydantic models:

```python
class PlaybookApprovalFact(BaseModel):
    approval_id: ApprovalId
    approval_type: str
    change_set_ref: str
    decided_by: EmployeeId
    decided_at: datetime


class PlaybookVersionView(BaseModel):
    playbook_version_id: PlaybookVersionId
    version_number: int
    content_hash: str
    base_version_id: PlaybookVersionId | None
    base_content_hash: str | None
    company_type: str
    minimum_deal_amount: str
    minimum_deal_currency: str
    excluded_categories: tuple[str, ...]
    sourcing_regions: tuple[str, ...]
    excluded_countries: tuple[str, ...]
    monthly_budget_credits: int | None
    approval_requirements: tuple[str, ...]
    supply_capabilities_note: str | None
    proposed_by: EmployeeId
    proposed_at: datetime
    content_provenance: Provenance
    change_set_ref: str


class PlaybookProposalResult(BaseModel):
    playbook_version_id: PlaybookVersionId
    version_number: int
    content_hash: str
    change_set_ref: str


class PlaybookChangeSnapshot(BaseModel):
    base: PlaybookVersionView | None
    current: PlaybookVersionView | None
    candidate: PlaybookVersionView
    base_is_current: bool


class PlaybookActivationView(BaseModel):
    activation_id: PlaybookActivationId
    playbook_version_id: PlaybookVersionId
    content_hash: str
    approval_id: ApprovalId
    change_set_ref: str
    approved_by: EmployeeId
    approved_at: datetime
    activated_by: str
    activated_at: datetime
```

Attach `ConfigDict(strict=True, frozen=True, extra="forbid")` to each. Serialize `minimum_deal_amount` with `_canonical_decimal`: start with `format(amount, "f")`, strip insignificant fractional zeroes and the trailing dot, and canonicalize signed zero to `"0"`; never expose a JSON float. Pairwise validators enforce base ID/hash nullability and aware UTC datetimes.

Use immutable dataclasses:

```python
@dataclass(frozen=True)
class CompanyPlaybookVersion:
    tenant_id: TenantId
    playbook_version_id: PlaybookVersionId
    version_number: int
    content_hash: str
    company_type: str
    minimum_deal_value: Money
    proposed_by: EmployeeId
    proposed_at: datetime
    idempotency_key: IdempotencyKey
    content_provenance: Provenance
    base_version_id: PlaybookVersionId | None = None
    base_content_hash: str | None = None
    excluded_categories: tuple[str, ...] = ()
    sourcing_regions: tuple[str, ...] = ()
    excluded_countries: tuple[str, ...] = ()
    monthly_budget_credits: int | None = None
    approval_requirements: tuple[str, ...] = ()
    supply_capabilities_note: str | None = None


@dataclass(frozen=True)
class PlaybookActivation:
    tenant_id: TenantId
    activation_id: PlaybookActivationId
    playbook_version_id: PlaybookVersionId
    content_hash: str
    approval_id: ApprovalId
    change_set_ref: str
    approved_by: EmployeeId
    approved_at: datetime
    activated_by: str
    activated_at: datetime
```

Replace the old mutable `CompanyPlaybook` with a frozen active read model carrying the active version/activation IDs, version number/hash, base ID/hash, approval/change-set refs, proposed/approved/activated actors and times, all business fields, and `content_provenance`. `get_playbook` derives its returned provenance with `confirmed_by=activation.approved_by` and `confirmed_at=activation.approved_at` from the two append-only facts; no UPDATE is used to confirm the candidate row.

Canonical JSON contains only business content, uses normalized sorted tuples and the same `_canonical_decimal`, and hashes `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))` encoded as UTF-8 with SHA-256.

Define the public service exactly:

```python
@runtime_checkable
class OrganizationService(Protocol):
    async def get_playbook(self, tenant_id: TenantId, *, actor: OrganizationActor) -> CompanyPlaybook: ...
    async def get_version(self, tenant_id: TenantId, version_id: PlaybookVersionId, *, actor: OrganizationActor) -> PlaybookVersionView: ...
    async def list_versions(self, tenant_id: TenantId, *, actor: OrganizationActor, limit: int = 50) -> list[PlaybookVersionView]: ...
    async def get_change_snapshot(self, tenant_id: TenantId, version_id: PlaybookVersionId, *, actor: OrganizationActor) -> PlaybookChangeSnapshot: ...
    async def propose_playbook(self, tenant_id: TenantId, command: PlaybookProposalCreate, *, actor: OrganizationActor, idempotency_key: IdempotencyKey) -> PlaybookProposalResult: ...
    async def activate_playbook(self, tenant_id: TenantId, version_id: PlaybookVersionId, approval: PlaybookApprovalFact, *, actor: OrganizationActor) -> PlaybookActivationView: ...
```

Remove `update_playbook`; do not leave a compatibility method, `force` parameter or overload.

Define distinct errors: `PlaybookNotConfiguredError`, `PlaybookIdempotencyConflictError`, `PlaybookApprovalFactInvalidError`, `PlaybookActivationConflictError`, and `PlaybookBaseVersionConflictError`. Approval type/version/hash/change-set or timestamp mismatches use `PlaybookApprovalFactInvalidError`; same approval/version replay with a different counterpart uses `PlaybookActivationConflictError`; only a latest-active/base mismatch uses `PlaybookBaseVersionConflictError`. Approval state is not accepted in this DTO and is verified by the workflow immediately before constructing it.

- [ ] **Step 4: Run unit tests and type-check the domain**

Run:

```bash
pytest -q tests/unit/test_organization_playbook.py
mypy domains/organization shared/schemas/identifiers.py
python3 scripts/check_boundaries.py
```

Expected: all commands exit 0; boundary output reports every category green.

- [ ] **Step 5: Commit the contracts**

```bash
git add docs/adr/0009-playbook-version-approval-contract.md \
  shared/schemas/identifiers.py domains/organization \
  tests/unit/test_organization_playbook.py
git commit -m "feat: define versioned playbook contracts"
```

---

### Task 2: Persist immutable versions and activations

**Files:**
- Create: `migrations/versions/0031_company_playbook.py`
- Modify: `infra/db/tables.py`
- Create: `infra/db/repositories/organization.py`
- Create: `infra/db/organization_uow.py`
- Create: `tests/integration/test_organization_repository.py`
- Modify: `tests/integration/test_migrations.py`
- Modify: `tests/integration/test_repositories.py`

**Interfaces:**
- Consumes: `CompanyPlaybookVersion`, `PlaybookActivation`, `OrganizationUnitOfWorkFactory` from Task 1.
- Produces: `SqlAlchemyOrganizationUnitOfWork`, tenant-bound version and activation repositories.

- [ ] **Step 1: Add failing PostgreSQL migration/repository tests**

Cover schema parity, tenant isolation, concurrent version allocation and activation uniqueness using the real PostgreSQL fixture. Parameterize append-only checks across UPDATE and DELETE for both `company_playbook_versions` and `company_playbook_activations`; all four mutations must raise `DBAPIError` with SQLSTATE `23514`.

```python
@pytest.mark.asyncio
async def test_playbook_versions_are_append_only(db_factory) -> None:
    async with db_factory() as session:
        session.add(_version_row("tenant-a", "pbv_01K00000000000000000000000", 1))
        await session.commit()
        with pytest.raises(DBAPIError):
            await session.execute(
                sa.update(CompanyPlaybookVersionRow)
                .where(CompanyPlaybookVersionRow.tenant_id == "tenant-a")
                .values(company_type="factory")
            )


@pytest.mark.asyncio
async def test_concurrent_version_numbers_are_unique(organization_uow_factory) -> None:
    numbers = await asyncio.gather(
        *(allocate_version(organization_uow_factory, "request-" + str(index)) for index in range(20))
    )
    assert sorted(numbers) == list(range(1, 21))
```

Define `_version_row` in this integration module as a complete valid `CompanyPlaybookVersionRow` factory with fixed Decimal/string/UTC values and keyword overrides. Define `allocate_version` as a real transaction that calls `next_version_number`, inserts a minimally valid version carrying the supplied idempotency key, commits through `OrganizationUnitOfWork`, and returns the allocated number. Import and assert `DBAPIError` for the trigger violation because PostgreSQL raises during `execute`, not at a later commit.

- [ ] **Step 2: Run PostgreSQL tests and verify RED**

Run:

```bash
pytest -q tests/integration/test_organization_repository.py tests/integration/test_migrations.py -k 'playbook or migration_head'
```

Expected: failure because migration 0031, ORM rows and repositories are absent.

- [ ] **Step 3: Add migration 0031 and ORM rows**

Create `company_playbook_versions` with composite tenant PK/FKs, `NUMERIC(28, 12)`, JSONB array checks, nonblank checks, hash shape, paired base fields, and required `source_type`, `source_id`, `extracted_by`, `extracted_at` columns for `content_provenance`. Restrict source type to `employee_input` in this human-only write path and enforce `source_id = playbook_version_id`, `extracted_by = 'human:' || proposed_by`, and `extracted_at = proposed_at`. Add these unique constraints:

```python
sa.UniqueConstraint("tenant_id", "version_number", name="uq_company_playbook_versions_number")
sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_company_playbook_versions_idempotency")
```

Create `company_playbook_activations` with composite FK to the version; required approval ID/change-set, `approved_by`/`approved_at`, system `activated_by`/actual `activated_at`; a check that `approved_at <= activated_at`; and:

```python
sa.UniqueConstraint("tenant_id", "playbook_version_id", name="uq_company_playbook_activations_version")
sa.UniqueConstraint("tenant_id", "approval_id", name="uq_company_playbook_activations_approval")
```

Install one immutable trigger function per table or one shared function that always raises SQLSTATE `23514` for UPDATE/DELETE. Downgrade drops triggers/functions before tables.

- [ ] **Step 4: Implement tenant-bound repositories and UoW**

Repository operations are exact and narrow:

```text
versions.add(version)
versions.get(tenant_id, version_id)
versions.find_by_idempotency_key(tenant_id, idempotency_key)
versions.next_version_number(tenant_id)  # tenant advisory xact lock
versions.list(tenant_id, limit)
activations.get_current(tenant_id)       # activated_at DESC, activation_id DESC
activations.get_by_version(tenant_id, version_id)
activations.get_by_approval(tenant_id, approval_id)
activations.add(activation)
activations.lock_tenant(tenant_id)       # advisory xact lock before base check
```

`versions.next_version_number` and `activations.lock_tenant` must acquire the identical transaction-scoped key with `pg_advisory_xact_lock(hashtextextended(:lock_name, 0))`, where `lock_name` is exactly `organization-playbook:<tenant_id>`; never use Python's process-randomized `hash()`. This serializes proposal base capture, numbering and activation base checks for one tenant. Every method calls `_require_tenant`. Hydration reconstructs `Money(Decimal(row.minimum_deal_amount), CurrencyCode(row.minimum_deal_currency))`, `Provenance(SourceType.EMPLOYEE_INPUT, ...)` and typed IDs; it does not use float. Activation confirmation remains a separate fact and is combined only in the active read model.

- [ ] **Step 5: Verify repository behavior and migration roundtrip**

Run:

```bash
pytest -q tests/integration/test_organization_repository.py
pytest -q tests/integration/test_migrations.py tests/integration/test_repositories.py -k 'playbook or migration or metadata'
alembic upgrade head
alembic downgrade 0030
alembic upgrade head
python3 scripts/check_boundaries.py
```

Expected: all exit 0, Alembic reports a single `0031` head, and both immutable triggers reject mutation.

- [ ] **Step 6: Commit persistence**

```bash
git add migrations/versions/0031_company_playbook.py infra/db/tables.py \
  infra/db/repositories/organization.py infra/db/organization_uow.py \
  tests/integration/test_organization_repository.py \
  tests/integration/test_migrations.py tests/integration/test_repositories.py
git commit -m "feat: persist immutable playbook versions"
```

---

### Task 3: Implement the organization service gates

**Files:**
- Create: `domains/organization/service_impl.py`
- Create: `tests/unit/test_organization_service.py`
- Create: `tests/integration/test_organization_service_postgres.py`
- Modify: `tests/integration/test_organization_repository.py`

**Interfaces:**
- Consumes: Task 1 public contracts and Task 2 UoW.
- Produces: `OrganizationServiceImpl` with idempotent proposal and exact-base activation.

- [ ] **Step 1: Write failing service behavior tests**

Use a real in-memory fake UoW that stores domain objects, not mocks. Cover no default, idempotency conflict, authorization, exact approval match, initial base, revision base and replay.

```python
@pytest.mark.asyncio
async def test_missing_activation_raises_instead_of_returning_default() -> None:
    service = _service()
    with pytest.raises(PlaybookNotConfiguredError):
        await service.get_playbook(TENANT, actor=_boss())


@pytest.mark.asyncio
async def test_activation_rejects_stale_base_without_appending_fact() -> None:
    service, store = _service_with_active_version(version_number=2)
    stale = store.add_candidate(base_version_number=1)
    with pytest.raises(PlaybookBaseVersionConflictError):
        await service.activate_playbook(
            TENANT,
            stale.playbook_version_id,
            _approved_fact(stale),
            actor=_system(),
        )
    assert store.activations_for(stale.playbook_version_id) == []


@pytest.mark.asyncio
async def test_activation_replay_returns_original_fact() -> None:
    service, candidate = _service_with_candidate()
    first = await service.activate_playbook(TENANT, candidate.playbook_version_id, _approved_fact(candidate), actor=_system())
    second = await service.activate_playbook(TENANT, candidate.playbook_version_id, _approved_fact(candidate), actor=_system())
    assert second == first
```

The module-local fake must implement every Task 1 repository/UoW method and transaction boundary. `_service`, `_service_with_active_version`, `_service_with_candidate`, `_approved_fact`, `_boss` and `_system` return concrete domain objects with fixed typed IDs and aware UTC timestamps; they are fixtures in this file, not unstated production helpers. Add tests proving only the system actor can obtain `PlaybookChangeSnapshot`, first configuration returns `base=None/current=None`, revisions return the exact captured base plus candidate, and a later activation makes `base_is_current=False` without replacing the captured base with invented/current content. Add a concurrent identical-proposal test proving one version is returned, plus a test proving the same content with a new idempotency key after the base changes creates a distinct candidate. In `test_organization_service_postgres.py`, create two candidates from the same active base and approve them concurrently; assert exactly one activation succeeds and the other raises `PlaybookBaseVersionConflictError`.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
pytest -q tests/unit/test_organization_service.py tests/integration/test_organization_service_postgres.py
```

Expected: failure because `OrganizationServiceImpl` does not exist.

- [ ] **Step 3: Implement proposal/read behavior**

Before opening a UoW, validate the separate idempotency key against `[A-Za-z0-9][A-Za-z0-9._:-]{0,199}`; reject whitespace, control characters and Unicode lookalikes with a fixed `ValidationError` that does not echo the input. `propose_playbook` must:

```python
self._authorizer.require(actor, OrganizationAction.PLAYBOOK_PROPOSE, tenant_id)
requested_hash = CompanyPlaybookVersion.content_hash_for(command)
existing = await uow.versions.find_by_idempotency_key(tenant_id, idempotency_key)
if existing is not None:
    if existing.content_hash != requested_hash:
        raise PlaybookIdempotencyConflictError("Playbook 幂等键对应不同内容")
    return _proposal_result(existing)
await uow.activations.lock_tenant(tenant_id)
existing = await uow.versions.find_by_idempotency_key(tenant_id, idempotency_key)
if existing is not None:
    if existing.content_hash != requested_hash:
        raise PlaybookIdempotencyConflictError("Playbook 幂等键对应不同内容")
    return _proposal_result(existing)
current = await uow.activations.get_current(tenant_id)
number = await uow.versions.next_version_number(tenant_id)
version = CompanyPlaybookVersion.from_command(
    tenant_id=tenant_id,
    version_id=PlaybookVersionId(new_id("pbv")),
    version_number=number,
    command=command,
    base_version_id=(
        None if current is None else current.playbook_version_id
    ),
    base_content_hash=None if current is None else current.content_hash,
    proposed_by=EmployeeId(actor.actor_id),
    proposed_at=self._now(),
    idempotency_key=idempotency_key,
)
await uow.versions.add(version)
```

`get_playbook` reads the current activation, raises `PlaybookNotConfiguredError` when absent, then loads the exact referenced version under the same tenant and derives the active read model. A missing referenced version is an integrity failure surfaced as `TransientError`, never as “not configured” or a default. `get_change_snapshot` requires `PLAYBOOK_CHANGE_SNAPSHOT_READ`, loads the candidate, its captured base when present, and the current activation/version in one UoW; `base_is_current` compares both ID and hash. A missing referenced base is an integrity failure, while `current=None` is valid only when no activation exists. The content hash covers normalized business content only. A same-key replay compares the business-content hash and returns the originally stored candidate without recomputing its server-derived base. Different content under the same key conflicts; the same content under a new key may intentionally create a candidate against a newer base. `list_versions` validates `1 <= limit <= 200` and returns stable version-number descending views.

- [ ] **Step 4: Implement exact activation behavior**

Inside one organization UoW:

```python
await uow.activations.lock_tenant(tenant_id)
version = await uow.versions.get(tenant_id, version_id)
existing_by_approval = await uow.activations.get_by_approval(tenant_id, approval.approval_id)
existing_by_version = await uow.activations.get_by_version(tenant_id, version_id)
if existing_by_approval is not None or existing_by_version is not None:
    return _require_exact_replay(existing_by_approval, existing_by_version, version, approval)
_require_approval_matches(version, approval)
current = await uow.activations.get_current(tenant_id)
_require_base_matches(version, current)
activation = PlaybookActivation(
    tenant_id=tenant_id,
    activation_id=PlaybookActivationId(new_id("pba")),
    playbook_version_id=version.playbook_version_id,
    content_hash=version.content_hash,
    approval_id=approval.approval_id,
    change_set_ref=approval.change_set_ref,
    approved_by=approval.decided_by,
    approved_at=approval.decided_at,
    activated_by=actor.actor_id,
    activated_at=self._now(),
)
await uow.activations.add(activation)
```

Only approval type `playbook_change` and exact `playbook:<id>:<hash>` pass. Reject cross-tenant actors and naive/non-UTC times before opening the UoW.

- [ ] **Step 5: Verify unit and real-DB service behavior**

Run:

```bash
pytest -q tests/unit/test_organization_service.py tests/unit/test_organization_playbook.py
pytest -q tests/integration/test_organization_repository.py tests/integration/test_organization_service_postgres.py
mypy domains/organization infra/db/repositories/organization.py infra/db/organization_uow.py
python3 scripts/check_boundaries.py
```

Expected: all exit 0.

- [ ] **Step 6: Commit service implementation**

```bash
git add domains/organization/service_impl.py tests/unit/test_organization_service.py \
  tests/integration/test_organization_repository.py \
  tests/integration/test_organization_service_postgres.py
git commit -m "feat: enforce playbook activation gates"
```

---

### Task 4: Build approval-backed durable workflow

**Files:**
- Modify: `domains/approvals/models.py`
- Modify: `domains/approvals/schemas.py`
- Modify: `domains/approvals/service.py`
- Modify: `domains/approvals/service_impl.py`
- Create: `workflows/playbook_change/AGENTS.md`
- Create: `workflows/playbook_change/__init__.py`
- Create: `workflows/playbook_change/flow.py`
- Create: `workflows/playbook_change/steps.py`
- Create: `tests/unit/test_approval_service.py`
- Create: `tests/unit/test_playbook_change_workflow.py`
- Create: `tests/integration/test_playbook_change_postgres.py`

**Interfaces:**
- Consumes: `OrganizationService`, `ApprovalService`, `ApprovalDecided`, `WorkflowEngine`.
- Produces: `build_playbook_change_definition()`, `build_playbook_change_handlers(...)`, `register_playbook_change(...)`.

- [ ] **Step 1: Write failing approval/workflow tests**

Test the exact state table, self-approval, event correlation, timeout expiration, rejection, approval application, stale-base apply failure and crash replay. The PostgreSQL test must include the no-bootstrap case: the first candidate proposer cannot decide the approval, a different boss/manager employee ID can approve it, outbox delivery wakes the run, and scheduler polling creates the first activation automatically.

```python
def test_playbook_change_definition_waits_for_approval_event() -> None:
    definition = build_playbook_change_definition()
    wait = next(step for step in definition.steps if step.step_name == "wait_decision")
    assert wait.wait_event_type == "ApprovalDecided"
    assert wait.timeout_context_key == "approval_timeout_seconds"
    assert wait.on_timeout == "expire_approval"


@pytest.mark.asyncio
async def test_apply_step_marks_stale_candidate_failed() -> None:
    approvals = FakeApprovals(_approved_view())
    organization = FailingOrganization(PlaybookBaseVersionConflictError("stale"))
    step = ApplyPlaybookStep(organization, approvals, _system_actor())
    result = await step.execute(_approved_run())
    assert result == ("complete", None, {"application_state": "apply_failed"})
    assert approvals.failed == [(APPROVAL_ID, "PLAYBOOK_BASE_VERSION_CONFLICT")]
```

Define `APPROVAL_ID` and all workflow IDs as fixed typed constants. In the same test module, `_approved_view` and `_approved_run` build complete public DTOs/run records; `FakeApprovals` records submit/get/expire/mark calls; `FailingOrganization` raises only the injected deterministic domain error; `_system_actor` is tenant-scoped. Add explicit tests for rejected/expired no-activation paths, invalid approval type/hash/change-set, duplicate `ApprovalDecided`, safe apply-error allowlisting, and the assertion that workflow context/outbox payloads contain IDs/hash only—never full Playbook fields or credential-shaped keys.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
pytest -q tests/unit/test_playbook_change_workflow.py tests/unit/test_approval_service.py \
  tests/integration/test_playbook_change_postgres.py
```

Expected: failure because the approval type, safe correlation field and workflow do not exist.

- [ ] **Step 3: Extend the approval public view safely**

Add:

```python
class ApprovalType(str, Enum):
    PLAYBOOK_CHANGE = "playbook_change"
```

Set `DEFAULT_VALIDITY[ApprovalType.PLAYBOOK_CHANGE] = timedelta(days=7)`, add label `Company Playbook 变更`, and add `change_set_ref: str | None = None`, `decided_by_employee: EmployeeId | None = None`, `applied_at: datetime | None = None`, and `application_error_code: str | None = None` to `ApprovalView`. `_view` copies only these safe correlation/actor/time facts; workflow code must not parse the display-oriented `decided_by_name`. Map `apply_error` only when it is one of the fixed codes `PLAYBOOK_BASE_VERSION_CONFLICT` or `PLAYBOOK_APPROVAL_FACT_INVALID`; otherwise return `None`, never raw error text, credentials or hidden repository fields.

Update `ApprovalService.mark_applied`'s public docstring: it records completed application, not a pre-action reservation. A consumer must write the target effect through its own durable idempotency mechanism first and then mark applied; append-only Playbook activation follows this order. If a target action cannot prove replay safety, it needs a separate design and must not use “mark first” as a shortcut.

- [ ] **Step 4: Implement workflow definition and steps**

Write `workflows/playbook_change/AGENTS.md` in Chinese, narrowing the parent workflow rules to: use only public organization/approval services; context/outbox may contain typed IDs, state, fixed reason codes and content hash but never full Playbook or credentials; activation must commit before `mark_applied`; deterministic policy conflicts become `apply_failed`, while infrastructure failures remain retryable. Export only the three public builders/register function from `__init__.py`.

Definition and transitions are exact:

```python
WorkflowDefinition(
    workflow_type="playbook_change",
    version=1,
    steps=(
        StepDefinition("assemble_package", "playbook_change.assemble"),
        StepDefinition("submit_approval", "playbook_change.submit"),
        StepDefinition(
            "wait_decision",
            "playbook_change.wait",
            timeout_context_key="approval_timeout_seconds",
            on_timeout="expire_approval",
            wait_event_type="ApprovalDecided",
            run_on_entry=True,
        ),
        StepDefinition("expire_approval", "playbook_change.expire"),
        StepDefinition("apply_playbook", "playbook_change.apply"),
        StepDefinition("mark_applied", "playbook_change.mark_applied"),
    ),
    transitions={
        "assemble_package": ("submit_approval",),
        "submit_approval": ("wait_decision",),
        "wait_decision": ("apply_playbook",),
        "expire_approval": ("apply_playbook",),
        "apply_playbook": ("mark_applied",),
        "mark_applied": (),
    },
)
```

`AssemblePackageStep` parses the subject as `PlaybookVersionId`, calls `get_change_snapshot` with the injected system actor, and verifies the initial context's version ID, content hash and change-set reference exactly. It patches only those normalized IDs/hash values and the candidate proposer ID; it never persists the full before/after payload in workflow context.

`SubmitApprovalStep` reloads a fresh `PlaybookChangeSnapshot`, builds the transient `before` map from the captured base (not a later current version) and `after` from the candidate, and submits `PLAYBOOK_CHANGE` with the first-configuration `before` represented explicitly as “未配置” rather than invented defaults. When `base_is_current=False`, the approval reason/blast-radius text carries the fixed warning `候选基准已变化，批准后将进入 apply_failed，需基于最新版本重新提交`:

```python
BlastRadius(
    affected_entities=[f"Company Playbook {version_id}"],
    if_approved="自动激活该精确候选版本；所有探索与门禁随后读取新版本。",
    if_rejected="当前生效版本保持不变；候选版本仅保留为审计历史。",
    reversible=True,
)
```

Here `reversible=True` means “submit the prior content as a new version and approve it”; it does not authorize UPDATE, rollback-in-place, or an approval bypass. All amounts in the before/after maps remain decimal strings.

It sets both `proposed_by_employee` and `owner_employee` to the candidate proposer, and patches only `approval_id`, `approval_timeout_seconds`, `change_set_ref`.

`WaitDecisionStep` never trusts event decision alone: it verifies event approval ID, reloads `ApprovalView`, and advances only for exact approved state; rejected/expired completes without activation; pending returns wait.

`ExpireApprovalStep` calls `expire_overdue(tenant_id)`, reloads exact approval, completes if expired/rejected, advances if approved, and raises `TransientError` if still pending or unavailable.

`ApplyPlaybookStep` requires approved state plus non-null `decided_by_employee`/`decided_at`, constructs `PlaybookApprovalFact` from those safe facts, and calls organization first. `PlaybookBaseVersionConflictError` maps to `mark_apply_failed(..., "PLAYBOOK_BASE_VERSION_CONFLICT")`; `PlaybookApprovalFactInvalidError` and `PlaybookActivationConflictError` map to `PLAYBOOK_APPROVAL_FACT_INVALID`; infrastructure exceptions propagate for outbox retry.

`MarkAppliedStep` calls:

```python
await approvals.mark_applied(
    run.tenant_id,
    ApprovalId(run.context["approval_id"]),
    f"playbook-activate:{run.subject_ref}:{run.context['content_hash']}",
)
```

- [ ] **Step 5: Register the event correlator**

`ApprovalDecidedHandler` calls `approvals.get`, ignores non-Playbook approval types, validates `change_set_ref` with `playbook:<pbv_ULID>:<64-lower-hex>`, finds the active run by version ID, then delivers only:

```python
{
    "approval_id": str(event.approval_id),
    "decision": event.decision,
    "decided_by": None if event.decided_by is None else str(event.decided_by),
}
```

If no active run exists, `has_delivered_event` distinguishes an idempotent replay from a temporarily unavailable run; the latter raises `TransientError`.

- [ ] **Step 6: Verify workflow unit and PostgreSQL replay behavior**

Run:

```bash
pytest -q tests/unit/test_playbook_change_workflow.py tests/unit/test_approval_service.py
pytest -q tests/integration/test_playbook_change_postgres.py
mypy domains/approvals workflows/playbook_change
python3 scripts/check_boundaries.py
```

Expected: all exit 0; the PostgreSQL test proves activate-then-mark crash replay creates one activation and one approval application record.

- [ ] **Step 7: Commit approval workflow**

```bash
git add domains/approvals workflows/playbook_change \
  tests/unit/test_approval_service.py \
  tests/unit/test_playbook_change_workflow.py \
  tests/integration/test_playbook_change_postgres.py
git commit -m "feat: apply approved playbook changes"
```

---

### Task 5: Wire API-start and scheduler-apply composition roots

**Files:**
- Modify: `apps/api/dependencies.py`
- Modify: `apps/api/composition/runtime.py`
- Modify: `apps/scheduler_worker/runtime.py`
- Modify: `tests/integration/test_api_runtime.py`
- Modify: `tests/integration/test_scheduler_worker.py`
- Modify: `tests/unit/test_api_runtime.py`

**Interfaces:**
- Consumes: Task 2 UoW, Task 3 service, Task 4 workflow registration.
- Produces: API `organization` dependency and a production scheduler `PlaybookChangeComposition` built from the scheduler's own Postgres factory.

- [ ] **Step 1: Add failing composition tests**

```python
def test_configured_api_dependencies_expose_real_organization_service(runtime_dependencies) -> None:
    assert isinstance(runtime_dependencies.organization, OrganizationService)


def test_scheduler_rejects_partial_playbook_change_composition() -> None:
    with pytest.raises(ValidationError, match="Playbook 变更依赖未完整配置"):
        PlaybookChangeComposition(
            organization=object(),
            approvals=FakeApprovalService(),
            system_actor=_system_actor(),
        )
```

Integration coverage must assert `SchedulerRuntimeFactory` always constructs the full composition and registers both the workflow definition and a second named `ApprovalDecided` handler alongside the existing notification projection. The lower-level `configured_scheduler_runtime` test helper may omit it because that helper receives a prebuilt workflow rather than the production Postgres factory.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
pytest -q tests/unit/test_api_runtime.py tests/integration/test_api_runtime.py tests/integration/test_scheduler_worker.py -k playbook
```

Expected: failure because the organization dependency and production scheduler Playbook wiring are absent.

- [ ] **Step 3: Wire the API composition root**

Construct one tenant-bound authorizer/service:

```python
organization_authorizer = Phase1OrganizationAuthorizer(tenant)
organization = OrganizationServiceImpl(
    lambda requested_tenant: SqlAlchemyOrganizationUnitOfWork(factory, requested_tenant),
    organization_authorizer,
    now=now,
)
```

Add `organization: OrganizationService | None = None` beside the other feature-scoped optional services in `ConfiguredApiDependencies`; the production `build_phase1_dependencies` path must always set the real service, while unrelated unit fixtures may omit it. Settings endpoints convert `None` to `TransientError` rather than using a fake/default. Register `build_playbook_change_definition()` in the API `PostgresWorkflowEngine` and map each handler ref to the existing `_StartOnlyWorkflowHandler`; API starts runs but never applies approval decisions itself.

Rename `_StartOnlyWorkflowHandler`'s account-specific docstring/error to the generic fixed message `workflow 步骤只能由 scheduler 执行`, because it now protects account, demand and Playbook runs. Do not include workflow context in the exception.

- [ ] **Step 4: Wire the scheduler composition root**

Add:

```python
@dataclass(frozen=True)
class PlaybookChangeComposition:
    organization: OrganizationService
    approvals: ApprovalService
    system_actor: OrganizationActor

    def __post_init__(self) -> None:
        required = (
            getattr(self.organization, "get_change_snapshot", None),
            getattr(self.organization, "activate_playbook", None),
            getattr(self.approvals, "submit", None),
            getattr(self.approvals, "get", None),
            getattr(self.approvals, "expire_overdue", None),
            getattr(self.approvals, "mark_applied", None),
            getattr(self.approvals, "mark_apply_failed", None),
        )
        if (
            any(not callable(value) for value in required)
            or not isinstance(self.system_actor, OrganizationActor)
            or self.system_actor.role != "system"
            or self.system_actor.scope.level is not OrganizationScopeLevel.SYSTEM
        ):
            raise ValidationError("scheduler Playbook 变更依赖未完整配置")
```

Import this `ValidationError` from `shared.errors`, matching the existing scheduler composition checks; it is not Pydantic's exception type.

Inside `SchedulerRuntimeFactory._resources`, construct the real services from its existing `factory`, tenant and `now`:

```python
playbook_approvals = ApprovalServiceImpl(
    lambda requested_tenant: SqlAlchemyApprovalUnitOfWork(
        factory, requested_tenant, now=self._now
    ),
    now=self._now,
)
playbook_organization = OrganizationServiceImpl(
    lambda requested_tenant: SqlAlchemyOrganizationUnitOfWork(
        factory, requested_tenant
    ),
    Phase1OrganizationAuthorizer(config.tenant_id),
    now=self._now,
)
playbook_change = PlaybookChangeComposition(
    organization=playbook_organization,
    approvals=playbook_approvals,
    system_actor=OrganizationActor(
        "system:playbook-change",
        OrganizationScope(
            level=OrganizationScopeLevel.SYSTEM,
            tenant_id=config.tenant_id,
        ),
        "system",
    ),
)
```

Build real Playbook handlers from that composition and merge them into the `PostgresWorkflowEngine` handler map. After the outbox exists, call `register_playbook_change(workflow, outbox, playbook_approvals)`. Production construction is mandatory and fail-closed; do not add a feature flag, empty service or optional external injection. Keep the lower-level `configured_scheduler_runtime` helper unchanged except for tests that prove it does not falsely claim Playbook registration.

- [ ] **Step 5: Verify both composition roots**

Run:

```bash
pytest -q tests/unit/test_api_runtime.py tests/integration/test_api_runtime.py
pytest -q tests/integration/test_scheduler_worker.py -k 'playbook or ApprovalDecided or registry'
mypy apps/api apps/scheduler_worker
python3 scripts/check_boundaries.py
```

Expected: all exit 0; existing Approval notification projection remains registered alongside the Playbook handler.

- [ ] **Step 6: Commit runtime wiring**

```bash
git add apps/api/dependencies.py apps/api/composition/runtime.py \
  apps/scheduler_worker/runtime.py tests/unit/test_api_runtime.py \
  tests/integration/test_api_runtime.py tests/integration/test_scheduler_worker.py
git commit -m "feat: wire playbook approval runtime"
```

---

### Task 6: Replace the Settings status stub with safe API endpoints

**Files:**
- Replace: `apps/api/routers/settings.py`
- Create: `tests/unit/test_settings_router.py`
- Modify: `tests/unit/test_api_app.py`

**Interfaces:**
- Consumes: `OrganizationService`, `ApprovalService`, `WorkflowEngine`, `RequestIdentity`.
- Produces: `GET /settings/playbook`, `GET /settings/playbook/versions`, `POST /settings/playbook/proposals`.

Define router response models with frozen/strict/extra-forbid configuration. `PlaybookActiveView` contains `version: PlaybookVersionView` and `activation: PlaybookActivationView`, both mapped from the single `CompanyPlaybook` active read model without a default or unscoped query. `PlaybookOverview` contains `configured: bool`, `active_version: PlaybookActiveView | None`, and the fixed `contact_enrichment` blocker; a model validator enforces `configured is (active_version is not None)`. `PlaybookVersionStatusView` contains the version, `approval_id: ApprovalId | None`, `application_error_code: Literal["PLAYBOOK_BASE_VERSION_CONFLICT", "PLAYBOOK_APPROVAL_FACT_INVALID"] | None`, and `approval_state: Literal["proposal_pending_submission", "pending", "approved", "rejected", "expired", "applied", "apply_failed"]`. `PlaybookProposalAccepted` contains `playbook_version_id`, `run_id`, and `change_set_ref`. Do not include raw approval `proposed_change`, raw apply errors, credentials, or a client-settable activation field.

- [ ] **Step 1: Add failing router tests**

Test real HTTP responses, not mock call counts.

```python
def test_missing_playbook_is_explicit_and_has_no_default(client) -> None:
    response = client.get("/settings/playbook", headers=_boss_headers())
    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "active_version": None,
        "contact_enrichment": {
            "state": "blocked",
            "reason_code": "COUNTRY_POLICY_NOT_CONFIGURED",
        },
    }


def test_settings_has_no_direct_update_or_force_route(client) -> None:
    assert client.put("/settings/playbook", headers=_boss_headers(), json={}).status_code == 405
    assert client.post("/settings/playbook/apply", headers=_boss_headers(), json={"force": True}).status_code == 404


def test_employee_cannot_read_full_playbook(client) -> None:
    assert client.get("/settings/playbook", headers=_employee_headers()).status_code == 403


@pytest.mark.parametrize("amount", [10.5, 10, True, "NaN", "Infinity"])
def test_proposal_rejects_non_decimal_json_amount(client, valid_proposal, amount) -> None:
    response = client.post(
        "/settings/playbook/proposals",
        headers={**_boss_headers(), "Idempotency-Key": "settings-submit-1"},
        json={**valid_proposal, "minimum_deal_amount": amount},
    )
    assert response.status_code == 422


def test_proposal_rejects_body_idempotency_field(client, valid_proposal) -> None:
    response = client.post(
        "/settings/playbook/proposals",
        headers={**_boss_headers(), "Idempotency-Key": "settings-submit-1"},
        json={**valid_proposal, "idempotency_key": "body-key"},
    )
    assert response.status_code == 422


def test_proposal_requires_idempotency_header(client, valid_proposal) -> None:
    response = client.post(
        "/settings/playbook/proposals",
        headers=_boss_headers(),
        json=valid_proposal,
    )
    assert response.status_code == 422
```

In the same module, `_boss_headers`, `_employee_headers` and `valid_proposal` are complete request fixtures using the existing request-identity test mechanism. Add tests that missing organization/approvals composition returns 503 with numeric `Retry-After`, a mismatched approval type/change-set returns 503, and `application_error_code` never exposes arbitrary stored error text.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
pytest -q tests/unit/test_settings_router.py tests/unit/test_api_app.py -k settings
```

Expected: failure because only `/settings/status` exists.

- [ ] **Step 3: Implement boss-only read endpoints**

Define a frozen router-local `SettingsAccess` dataclass containing only `organization_actor: OrganizationActor`. Its dependency checks `identity.employee.role == "boss"` and constructs `OrganizationActor(str(employee_id), OrganizationScope(OrganizationScopeLevel.TENANT, identity.tenant_id), "boss")`; the domain service performs the second action check. Add `_organization_service` and `_approval_service` helpers that raise `TransientError` when the corresponding optional configured dependency is absent, so feature-local missing composition can never masquerade as an unconfigured Playbook or missing approval.

`GET /settings/playbook` catches only `PlaybookNotConfiguredError` and returns `configured=False`; any repository/transient error propagates as 503. The contact-enrichment blocker remains fixed to `COUNTRY_POLICY_NOT_CONFIGURED` for this slice.

`GET /settings/playbook/versions?limit=` validates `1..200`, loads organization versions and, for each, calls `approvals.get_by_change_set(tenant, change_set_ref)`. It returns exact state or `proposal_pending_submission` when the durable workflow has not yet created the approval. A returned view must have `approval_type == "playbook_change"` and the exact change-set reference; a mismatch is an integrity `TransientError`, not a status to display. It never interprets a service error as “no approval”.

- [ ] **Step 4: Implement proposal submission**

The endpoint is:

```python
@router.post("/playbook/proposals", response_model=PlaybookProposalAccepted, status_code=202)
async def propose_playbook(
    body: PlaybookProposalCreate,
    idempotency_key_header: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ],
    context: Annotated[SettingsAccess, Depends(resolve_settings_access)],
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> PlaybookProposalAccepted:
    organization = _organization_service(dependencies)
    proposal = await organization.propose_playbook(
        identity.tenant_id,
        body,
        actor=context.organization_actor,
        idempotency_key=IdempotencyKey(idempotency_key_header),
    )
    run_id = await dependencies.workflow_engine.start(
        identity.tenant_id,
        "playbook_change",
        proposal.playbook_version_id,
        {
            "playbook_version_id": str(proposal.playbook_version_id),
            "content_hash": proposal.content_hash,
            "change_set_ref": proposal.change_set_ref,
            "proposed_by": str(identity.employee.employee_id),
        },
        f"playbook-change:{identity.tenant_id}:{idempotency_key_header}",
    )
    return PlaybookProposalAccepted(
        playbook_version_id=proposal.playbook_version_id,
        run_id=str(run_id),
        change_set_ref=proposal.change_set_ref,
    )
```

The body has no tenant, employee, approval, activation or idempotency fields. Reject whitespace-only/control-character content and malformed idempotency headers in domain validation; never echo the header in errors or logs.

- [ ] **Step 5: Verify API, OpenAPI and boundary behavior**

Run:

```bash
pytest -q tests/unit/test_settings_router.py tests/unit/test_api_app.py -k settings
python3 apps/web/scripts/export_openapi.py > /tmp/tradeos-playbook-openapi.json
python3 scripts/check_boundaries.py
```

Expected: tests and boundary check exit 0; OpenAPI contains only GET/POST Settings paths and represents `minimum_deal_amount` as string.

- [ ] **Step 6: Commit Settings API**

```bash
git add apps/api/routers/settings.py tests/unit/test_settings_router.py tests/unit/test_api_app.py
git commit -m "feat: expose approved playbook settings api"
```

---

### Task 7: Build the Settings Playbook workspace

**Files:**
- Modify by generation: `apps/web/src/api/api.d.ts`
- Replace: `apps/web/src/views/settings/SettingsCenter.vue`
- Create: `apps/web/tests/settings.test.ts`
- Create: `tests/e2e/test_playbook_settings.py`

**Interfaces:**
- Consumes: generated `components["schemas"]` for Settings API from Task 6.
- Produces: boss-only active version view, candidate form, field diff, workflow/approval state and fixed country-policy blocker.

- [ ] **Step 1: Write failing UI behavior tests**

Use the injected real OpenAPI client shape and assert user-visible behavior. Also add the real-stack Playwright test using the existing `e2e_stack`/synthetic employee fixtures; it must call the real Settings API and must not install route-level response mocks.

```typescript
it("does not invent commercial defaults when no Playbook exists", async () => {
  mockGetPlaybook({
    configured: false,
    active_version: null,
    contact_enrichment: { state: "blocked", reason_code: "COUNTRY_POLICY_NOT_CONFIGURED" },
  });
  renderSettings();
  await flushPromises();
  expect(screen.getByText("尚未配置 Company Playbook")).toBeTruthy();
  expect((screen.getByLabelText("交易金额底线") as HTMLInputElement).value).toBe("");
  expect(screen.getByText("国家政策包未配置")).toBeTruthy();
});


it("submits a candidate without any direct-apply control", async () => {
  renderSettingsWithActiveVersion();
  await userEvent.type(screen.getByLabelText("交易金额底线"), "12000.50");
  await userEvent.click(screen.getByRole("button", { name: "提交审批" }));
  expect(lastProposalBody.minimum_deal_amount).toBe("12000.50");
  expect(lastProposalBody).not.toHaveProperty("idempotency_key");
  expect(lastProposalHeaders["Idempotency-Key"]).toMatch(/^[A-Za-z0-9._:-]+$/);
  expect(screen.queryByRole("button", { name: /强制|立即生效|跳过审批/ })).toBeNull();
});
```

The test module defines `mockGetPlaybook`, `renderSettings`, `renderSettingsWithActiveVersion`, `lastProposalBody` and `lastProposalHeaders` on top of the app's existing API-client injection seam; fixtures contain only synthetic IDs and data. Add cases for pending/rejected/applied/apply_failed labels, safe fixed error-code copy, 403, numeric `Retry-After`, and preserving one idempotency header across a simulated retry. Real overflow remains in Step 6 browser acceptance because jsdom does not provide trustworthy layout measurements.

- [ ] **Step 2: Run UI tests and verify RED**

Run:

```bash
cd apps/web && npm run test -- tests/settings.test.ts
cd ../.. && TRADEOS_REQUIRE_E2E=1 pytest -q tests/e2e/test_playbook_settings.py
```

Expected: both fail for the intended missing Settings behavior because the current page is static; Docker/runtime setup failures do not count as UI RED.

- [ ] **Step 3: Regenerate API types**

Run through the project Python 3.12 Conda environment so `gen:api` cannot pick macOS Python 3.9:

```bash
cd apps/web && /Users/xueziheng/miniconda3/bin/conda run -n tradeos-py312 npm run gen:api
```

Do not hand-edit `src/api/api.d.ts`. Confirm the generated proposal body uses `minimum_deal_amount: string`.

- [ ] **Step 4: Implement the Settings view**

Use generated types only:

```typescript
type PlaybookOverview = components["schemas"]["PlaybookOverview"];
type PlaybookVersion = components["schemas"]["PlaybookVersionStatusView"];
type ProposalBody = components["schemas"]["PlaybookProposalCreate"];
```

Keep `minimum_deal_amount` as a string from input through request serialization. Use a text input with the server-compatible decimal pattern; do not call `Number`, `parseFloat`, `toFixed`, or an Ant `InputNumber` path that emits a JavaScript number.

On load, fetch overview and versions in parallel. First configuration initializes every business input to empty/unchecked values; revisions copy the active version only after it loads. Generate a client idempotency key once per form submission attempt, send it only as the `Idempotency-Key` header, and retain it across network retry until success or an intentional field edit.

Render sections:

```text
当前生效版本
候选变更表单
旧值 / 新值逐字段差异
候选与审批历史
Connector readiness（国家政策包未配置）
```

The active/version sections show proposer, approver, proposed/activated UTC timestamps and the `employee_input` provenance label so the boss can trace every displayed business field back to the exact immutable version and approval fact.

Disable submit while the request is in flight. On 403 show `当前角色无权查看或修改 Company Playbook`; on 503 respect a numeric `Retry-After`; on 202 show candidate and Run IDs and link to `/approvals`.

Do not render secret/key inputs or controls whose accessible name contains `强制生效`, `立即生效`, `跳过审批`, `删除版本`.

- [ ] **Step 5: Verify UI tests, lint, types and build**

Run:

```bash
cd apps/web
npm run test -- tests/settings.test.ts
npx eslint src/views/settings/SettingsCenter.vue tests/settings.test.ts --max-warnings 0
npm run typecheck
npm run build
```

Expected: all commands exit 0 with no warnings in the targeted ESLint run.

- [ ] **Step 6: Browser acceptance**

Run the automated real-stack acceptance:

```bash
TRADEOS_REQUIRE_E2E=1 pytest -q tests/e2e/test_playbook_settings.py
```

The test verifies desktop plus 390×844 viewport:

```text
no active Playbook shows an empty form and explicit blocker
proposal submit returns a candidate/Run and never claims activation
version history shows pending/rejected/applied/apply_failed distinctly
no direct apply/force/delete control exists
no horizontal overflow
browser console has zero new warnings/errors
```

Capture no real connector credentials or customer data in fixtures/screenshots.

- [ ] **Step 7: Commit Web workspace**

```bash
git add apps/web/src/api/api.d.ts apps/web/src/views/settings/SettingsCenter.vue \
  apps/web/tests/settings.test.ts tests/e2e/test_playbook_settings.py
git commit -m "feat: enable playbook settings workspace"
```

---

### Task 8: Full verification and truthful progress documentation

**Files:**
- Modify: `HANDBOOK.md`
- Modify: `docs/architecture/04-tool-gateway.md`
- Verify: all files changed in Tasks 1–7

**Interfaces:**
- Consumes: all completed behavior and fresh verification evidence.
- Produces: truthful implementation status that still marks country policy and production `contact.enrich` as blocked.

- [ ] **Step 1: Run focused backend suites**

```bash
pytest -q \
  tests/unit/test_organization_playbook.py \
  tests/unit/test_organization_service.py \
  tests/unit/test_approval_service.py \
  tests/unit/test_playbook_change_workflow.py \
  tests/unit/test_settings_router.py \
  tests/integration/test_organization_repository.py \
  tests/integration/test_organization_service_postgres.py \
  tests/integration/test_playbook_change_postgres.py
```

Expected: 0 failed, 0 errors.

- [ ] **Step 2: Run the full repository gate**

Use the Python 3.12 environment, not macOS `/usr/bin/python3`:

```bash
/Users/xueziheng/miniconda3/bin/conda run -n tradeos-py312 make check
```

Expected: Ruff exits 0, mypy exits 0, boundary and sensitive scans pass, pytest reports 0 failures.

- [ ] **Step 3: Run the full Web gate**

```bash
cd apps/web
npm run test
npm run lint
npm run typecheck
npm run build
cd ../..
TRADEOS_REQUIRE_E2E=1 pytest -q tests/e2e/test_playbook_settings.py
```

Expected: every command exits 0, including real Postgres/API/Vite/Chromium acceptance. If full lint reports pre-existing warnings, record their exact count and prove no new warning in the targeted Settings lint; do not describe warnings as “clean”.

- [ ] **Step 4: Verify migration and worktree hygiene**

```bash
alembic heads
find . -name '._*' -not -path './.git/*' -not -path './.worktrees/*' -print
git diff --check
git status --short
```

Expected: one head `0031`; no newly tracked AppleDouble files; diff check exits 0; status contains only intended documentation edits before the final commit.

- [ ] **Step 5: Update truthful status docs**

Update HANDBOOK’s stale account-discovery progress note to state:

```text
Account discovery workflow, Hunter gateway, Campaign enrollment, API and UI exist.
Company Playbook versioning/approval is implemented.
Country policy packages and production contact.enrich registration remain blocked and are the next independent slice.
Phase 1 operational acceptance has not yet been proven.
```

Update `docs/architecture/04-tool-gateway.md` by removing only claims that account-discovery persistence/workflow/UI are absent. Preserve the explicit statement that production `contact.enrich` is not registered until real country-policy composition exists.

- [ ] **Step 6: Re-run documentation-sensitive gates and commit**

```bash
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
git add HANDBOOK.md docs/architecture/04-tool-gateway.md
git commit -m "docs: record playbook configuration boundary"
```

- [ ] **Step 7: Final evidence audit**

```bash
git status --short --branch
git log --oneline -8
```

Report exact test counts and commands from this execution. Do not claim Phase 1 complete; the next slice is country-policy persistence/readiness, followed by controlled production registration and real operational acceptance.

---

## Plan Self-Review Checklist

- Every spec section maps to at least one task: domain/ADR (1), persistence (2), service gates (3), workflow/recovery (4), composition (5), API (6), UI (7), verification/status (8).
- Type names are consistent across tasks: `PlaybookVersionId`, `PlaybookActivationId`, `PlaybookProposalCreate`, `PlaybookApprovalFact`, `PlaybookChangeSnapshot`, `OrganizationActor`, `PlaybookChangeComposition`.
- Activation always precedes `mark_applied`; no task introduces a reverse order or direct apply API.
- Initial and revision candidates use the same approval path and self-approval rule.
- Country policy and Connector secrets remain outside scope and explicitly blocked.
- Every business-content field shares explicit employee-input Provenance; activation supplies immutable confirmer/time facts without mutating the candidate.
- All production-code tasks contain an explicit failing-test run before implementation.
