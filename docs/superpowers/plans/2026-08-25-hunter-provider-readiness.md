# Hunter Provider Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a tenant-scoped, append-only Hunter readiness chain that keeps both contact tools closed until an exact safe configuration is manually validated and the singleton scheduler records successful runtime composition.

**Architecture:** `tool_gateway/provider_readiness.py` owns the operational contract, deterministic state derivation, permissions, and service; `infra/db` persists one append-only event stream. A privileged configuration command declares safe version metadata without reading a secret, a Tool Gateway validation plugin checks Hunter `/account` through the fixed transport, and scheduler composition registers both contact tools only for the exact validated configuration. Settings reads the same PostgreSQL facts and never guesses scheduler state from a local boolean.

**Tech Stack:** Python 3.12, Pydantic v2 conventions, SQLAlchemy 2.x async, PostgreSQL/Alembic, FastAPI, Vue 3 + TypeScript + Vite, pytest, Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-08-25-hunter-provider-readiness-design.md`

## Global Constraints

- No implementation or automated acceptance command may access real Hunter network or read a real Hunter API key.
- Hunter API key values and hashes MUST NOT enter request params, DTOs, repr, exceptions, logs, Tool Gateway ledger, readiness events, API responses, or model context.
- The fixed connector host remains `https://api.hunter.io/v2`; callers cannot provide a base URL or absolute URL.
- Provider validation calls only `/account`; it does not submit email, name, company, country, or other contact data.
- Every table, unique constraint, repository method, and query includes `tenant_id`.
- Readiness rows are append-only; PostgreSQL triggers reject UPDATE and DELETE.
- Configuration hashes contain only provider/capability/profile/configuration/key-version metadata, never secret refs or values.
- `TRADEOS_HUNTER_CONTACTS_ENABLED` accepts only lowercase `true` or `false`; enabled configuration is all-or-none.
- A new configured event invalidates every validation and runtime-composed fact for the previous configuration.
- `validation_started` is committed after canonical Gateway `EXECUTING` and before Provider IO.
- Validation failure or inconclusive results never trigger an automatic retry.
- Only the scheduler process that has acquired and rechecked the singleton advisory lock may append `runtime_composed`.
- Both `contact.enrich` and `contact.verify` are registered together or neither is registered.
- Both Hunter provider adapters re-read current readiness before connector creation or secret resolution; stale configuration and reader failure are zero-IO failures.
- Provider readiness does not replace `contact.enrich` Playbook, country-policy, suppression, tenant, permission, or quota checks.
- No money, float, model confidence, Provider score, Provider confidence, or package-price field is introduced.
- No new business domain and no Tool Gateway pipeline stage is added.
- Real Provider validation and Phase 1 operating acceptance remain `not_run` after this plan.
- Every production change follows RED → expected failure → minimal GREEN → focused verification → commit.
- Run Python through `/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312` or `/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3`.

---

## File Map

### Operational contract

- `tool_gateway/provider_readiness.py` — local strong ID, safe configuration hash, event/snapshot types, permission-bearing actor, repository/UoW Protocols, deterministic service implementation, and runtime guard.
- `tests/unit/test_provider_readiness.py` — pure validation, authorization, event transition, idempotency, and state-priority tests using an in-memory UoW.
- `tool_gateway/AGENTS.md` — readiness ownership, exact-config invalidation, and zero-IO guard rules.

### Persistence

- `migrations/versions/0033_provider_readiness_events.py` — tenant-scoped event stream, constraints, indexes, and append-only trigger.
- `infra/db/tables.py` — `ProviderReadinessEventRow`.
- `infra/db/repositories/provider_readiness.py` — advisory-locked event append and tenant-filtered reads.
- `infra/db/provider_readiness_uow.py` — transaction boundary.
- `tests/integration/test_provider_readiness_repository.py` — migration, isolation, concurrency, append-only, and service/Postgres tests.

### Deployment declaration and validation

- `apps/scheduler_worker/config.py` — `HunterContactsSettings` and strict environment parsing.
- `scripts/configure_hunter_provider.py` — privileged no-secret/no-network configuration declaration command.
- `tests/unit/test_scheduler_worker_config.py` — exact configuration matrix and repr secrecy.
- `tests/integration/test_configure_hunter_provider.py` — command idempotency and conflict against PostgreSQL.
- `connectors/hunter/client.py` — classified `validate_account()` instead of lossy boolean health validation.
- `connectors/hunter/secrets.py` — repr-safe binding from Hunter's fixed logical key reference to the configured deployment reference.
- `tool_gateway/handlers/provider_validation.py` — validation manifest, handler, typed preparation, and one-in-flight rate-limit check.
- `scripts/validate_hunter_provider.py` — explicit Tool Gateway validation command with injectable transport for tests.
- `tests/unit/test_provider_validation_handler.py` — fixed endpoint, error mapping, event order, and canary secrecy.
- `tests/integration/test_provider_validation_gateway.py` — canonical ledger/readiness transaction behavior with controlled transport.

### Scheduler composition and activation

- `tool_gateway/handlers/contact_enrichment.py` — exact-config guard before connector creation.
- `tool_gateway/handlers/contact_verification.py` — the same guard before connector creation.
- `apps/scheduler_worker/hunter_contacts.py` — conditional dual registration and matching runtime metadata.
- `apps/scheduler_worker/runtime.py` — read current readiness, build fixed production transport, reject incomplete production composition, and create a post-lock activation hook.
- `apps/scheduler_worker/main.py` — execute activation only after singleton lock acquisition and before the first cycle.
- `apps/scheduler_worker/AGENTS.md` — post-lock fact and live configuration guard requirements.
- `tests/unit/test_country_policy_runtime_composition.py` — replace the intentional unregistered assertion with the full readiness matrix.
- `tests/unit/test_contact_enrichment_handler.py` and `tests/unit/test_contact_verification_handler.py` — stale/read-failure zero-IO proofs.
- `tests/integration/test_scheduler_worker.py` — lock ownership, activation failure, and first-cycle ordering.
- `tests/integration/test_tool_gateway_contact_enrichment.py` and `tests/integration/test_tool_gateway_contact_verification.py` — ready-path Gateway regression.

### API, Web, operations, and acceptance

- `apps/api/dependencies.py` — replace `contact_enrichment_composed: bool` with a required readiness reader.
- `apps/api/composition/runtime.py` — construct the PostgreSQL readiness service/reader.
- `apps/api/routers/settings.py` — exact reason-code priority.
- `tests/unit/test_settings_router.py`, `tests/integration/test_api_runtime.py`, and `tests/integration/test_country_policy_settings_api.py` — API dependency and readiness behavior.
- `apps/web/src/api/api.d.ts` — regenerated OpenAPI contract.
- `apps/web/src/views/settings/SettingsCenter.vue` — truthful Chinese readiness copy.
- `apps/web/tests/settings.test.ts` — all Provider states and no false-ready assertions.
- `tests/e2e/test_hunter_provider_readiness_settings.py` — real API/Web/Postgres pending-state journey with zero Hunter requests.
- `infra/.env.example` — empty, non-runnable configuration shape only.
- `docs/operations/hunter-provider-readiness.md` — configure, validate, restart, reconcile, and `not_run` evidence format.
- `HANDBOOK.md`, `docs/architecture/04-tool-gateway.md`, `docs/architecture/08-compliance.md`, `docs/architecture/11-deployment.md`, and `README.md` — delivered/remaining scope.

---

### Task 1: Define the pure Provider readiness contract and state machine

**Files:**
- Create: `tool_gateway/provider_readiness.py`
- Create: `tests/unit/test_provider_readiness.py`
- Modify: `tool_gateway/AGENTS.md`

**Interfaces:**
- Produces `ProviderReadinessEventId`, `ProviderId`, `ProviderCapability`, `ProviderReadinessEventType`, `ProviderReadinessState`, `ProviderReadinessPermission`, `ProviderValidationFailureCode`, and `ProviderReadinessUnavailableError`.
- Produces `ProviderConfiguration.hunter_contacts(configuration_version: str, api_key_version: str) -> ProviderConfiguration`.
- Produces `ProviderReadinessActor`, `ProviderReadinessEvent`, `ProviderReadinessSnapshot`, `ProviderReadinessRepository`, `ProviderReadinessUnitOfWork`, `ProviderReadinessUnitOfWorkFactory`, `ProviderReadinessService`, `ProviderReadinessServiceImpl`, and `ProviderRuntimeGuard`.
- Task 2 implements the repository/UoW Protocols. Tasks 3–8 consume the service and snapshot contract without importing persistence rows.

- [ ] **Step 1: Write failing configuration and state tests**

Create tests with these exact public names and core assertions:

```python
def test_hunter_configuration_hash_uses_safe_exact_metadata() -> None:
    first = ProviderConfiguration.hunter_contacts("deploy-v1", "key-v1")
    replay = ProviderConfiguration.hunter_contacts("deploy-v1", "key-v1")
    rotated = ProviderConfiguration.hunter_contacts("deploy-v2", "key-v2")
    assert first == replay
    assert first.configuration_hash == replay.configuration_hash
    assert first.configuration_hash != rotated.configuration_hash
    assert first.capabilities == (
        ProviderCapability.CONTACT_ENRICHMENT,
        ProviderCapability.CONTACT_VERIFICATION,
    )
    assert "secret" not in repr(first).casefold()

@pytest.mark.parametrize("value", ["", " V1", "v1 ", "V1", "v/1", "x" * 33])
def test_configuration_versions_are_canonical(value: str) -> None:
    with pytest.raises(ValidationError):
        ProviderConfiguration.hunter_contacts(value, "key-v1")

async def test_new_configuration_invalidates_old_validation_and_runtime() -> None:
    service, uow = in_memory_service()
    await declare_and_compose(service, CONFIG_V1)
    assert (await service.get_snapshot(TENANT, HUNTER_CAPABILITIES, actor=READER)).state is ProviderReadinessState.READY
    await service.declare_configuration(TENANT, CONFIG_V2, actor=CONFIGURER, idempotency_key=IdempotencyKey("cfg:v2"))
    snapshot = await service.get_snapshot(TENANT, HUNTER_CAPABILITIES, actor=READER)
    assert snapshot.state is ProviderReadinessState.VALIDATION_NOT_RUN
    assert snapshot.configuration == CONFIG_V2

async def test_started_without_terminal_fact_is_inconclusive() -> None:
    service, _ = in_memory_service()
    await service.declare_configuration(TENANT, CONFIG_V1, actor=CONFIGURER, idempotency_key=IdempotencyKey("cfg:v1"))
    await service.mark_validation_started(TENANT, CONFIG_V1.configuration_hash, validation_key=IdempotencyKey("validate:v1"), actor=VALIDATOR)
    assert (await service.get_snapshot(TENANT, HUNTER_CAPABILITIES, actor=READER)).state is ProviderReadinessState.VALIDATION_INCONCLUSIVE
```

Add separate tests for no configuration, failed validation, passed-without-runtime, ready, same-key same-payload no-op, same-key different-payload conflict, terminal validation without matching started rejection, runtime without matching pass rejection, cross-tenant actor rejection, and permission rejection for configure/validate/compose/read.

- [ ] **Step 2: Run RED and confirm missing module**

Run:

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_provider_readiness.py
```

Expected: collection fails because `tool_gateway.provider_readiness` does not exist.

- [ ] **Step 3: Implement strict types and deterministic configuration hashing**

Implement these exact enum values and configuration factory:

```python
ProviderReadinessEventId = NewType("ProviderReadinessEventId", str)

class ProviderId(str, Enum):
    HUNTER = "hunter"

class ProviderCapability(str, Enum):
    CONTACT_ENRICHMENT = "contact.enrich"
    CONTACT_VERIFICATION = "contact.verify"

HUNTER_CONTACT_CAPABILITIES = (
    ProviderCapability.CONTACT_ENRICHMENT,
    ProviderCapability.CONTACT_VERIFICATION,
)

class ProviderReadinessEventType(str, Enum):
    CONFIGURED = "configured"
    VALIDATION_STARTED = "validation_started"
    VALIDATION_PASSED = "validation_passed"
    VALIDATION_FAILED = "validation_failed"
    RUNTIME_COMPOSED = "runtime_composed"

class ProviderReadinessState(str, Enum):
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    VALIDATION_NOT_RUN = "validation_not_run"
    VALIDATION_INCONCLUSIVE = "validation_inconclusive"
    VALIDATION_FAILED = "validation_failed"
    RUNTIME_NOT_COMPOSED = "runtime_not_composed"
    READY = "ready"

class ProviderValidationFailureCode(str, Enum):
    AUTH_REQUIRED = "auth_required"
    RATE_LIMITED = "rate_limited"
    PROVIDER_TRANSIENT = "provider_transient"
    PROVIDER_PERMANENT = "provider_permanent"
    RESPONSE_INVALID = "response_invalid"
    RECONCILIATION_REQUIRED = "reconciliation_required"

@dataclass(frozen=True)
class ProviderConfiguration:
    provider: ProviderId
    capabilities: tuple[ProviderCapability, ...]
    configuration_version: str
    connector_profile_version: str
    transport_profile: str
    api_key_version: str
    configuration_hash: str

    @classmethod
    def hunter_contacts(cls, configuration_version: str, api_key_version: str) -> Self:
        safe_configuration_version = require_version(configuration_version)
        safe_api_key_version = require_version(api_key_version)
        payload = {
            "provider": "hunter",
            "capabilities": ["contact.enrich", "contact.verify"],
            "connector_profile_version": "hunter-contacts-v1",
            "transport_profile": "hunter_api_v2_fixed_host",
            "configuration_version": safe_configuration_version,
            "api_key_version": safe_api_key_version,
        }
        digest = hashlib.sha256(canonical_json(payload)).hexdigest()
        return cls(
            provider=ProviderId.HUNTER,
            capabilities=HUNTER_CONTACT_CAPABILITIES,
            configuration_version=safe_configuration_version,
            connector_profile_version="hunter-contacts-v1",
            transport_profile="hunter_api_v2_fixed_host",
            api_key_version=safe_api_key_version,
            configuration_hash=digest,
        )
```

`canonical_json` uses UTF-8 JSON with sorted keys and separators `(",", ":")`. Version values match `[a-z0-9][a-z0-9._-]{0,31}`. Generate event IDs with `ProviderReadinessEventId(new_id("pre"))`. The configuration object never contains a secret ref or value.

- [ ] **Step 4: Implement event validation, permissions, service transitions, and runtime guard**

Use these public method signatures:

```python
class ProviderReadinessService(Protocol):
    async def get_snapshot(
        self, tenant_id: TenantId, capabilities: tuple[ProviderCapability, ...], *, actor: ProviderReadinessActor
    ) -> ProviderReadinessSnapshot: ...
    async def declare_configuration(
        self, tenant_id: TenantId, configuration: ProviderConfiguration, *, actor: ProviderReadinessActor, idempotency_key: IdempotencyKey
    ) -> ProviderReadinessSnapshot: ...
    async def mark_validation_started(
        self, tenant_id: TenantId, configuration_hash: str, *, validation_key: IdempotencyKey, actor: ProviderReadinessActor
    ) -> ProviderReadinessEvent: ...
    async def mark_validation_passed(
        self, tenant_id: TenantId, configuration_hash: str, *, validation_key: IdempotencyKey, evidence_ref: str, actor: ProviderReadinessActor
    ) -> ProviderReadinessEvent: ...
    async def mark_validation_failed(
        self, tenant_id: TenantId, configuration_hash: str, *, validation_key: IdempotencyKey, failure_code: ProviderValidationFailureCode, actor: ProviderReadinessActor
    ) -> ProviderReadinessEvent: ...
    async def mark_runtime_composed(
        self, tenant_id: TenantId, configuration_hash: str, *, actor: ProviderReadinessActor, idempotency_key: IdempotencyKey
    ) -> ProviderReadinessEvent: ...

class ProviderRuntimeGuard(Protocol):
    async def require_current(
        self, tenant_id: TenantId, configuration_hash: str
    ) -> None: ...
```

`ProviderReadinessActor` contains `actor_id`, `tenant_id`, and a frozen set of `ProviderReadinessPermission` values (`configure`, `validate`, `compose`, `read`). Service authorization checks tenant equality and the exact action. State is derived only from ordered events for the current configured event. `require_current` requires current hash plus `READY`; mismatch or repository failure raises `ProviderReadinessUnavailableError("Provider 配置当前不可用")` with no event or secret material in its text/repr.

- [ ] **Step 5: Run GREEN, types, and boundary checks**

Run:

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_provider_readiness.py
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  mypy tool_gateway/provider_readiness.py
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  python scripts/check_boundaries.py
```

Expected: all commands pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add tool_gateway/provider_readiness.py tool_gateway/AGENTS.md tests/unit/test_provider_readiness.py
git commit -m "feat: define provider readiness contract"
```

---

### Task 2: Persist the tenant-scoped append-only readiness stream

**Files:**
- Create: `migrations/versions/0033_provider_readiness_events.py`
- Create: `infra/db/repositories/provider_readiness.py`
- Create: `infra/db/provider_readiness_uow.py`
- Modify: `infra/db/tables.py`
- Create: `tests/integration/test_provider_readiness_repository.py`

**Interfaces:**
- Consumes Task 1 `ProviderReadinessEvent`, repository/UoW Protocols, and `ProviderReadinessServiceImpl`.
- Produces `ProviderReadinessEventRow`, `ProviderReadinessRepositoryImpl`, and `SqlAlchemyProviderReadinessUnitOfWork`.
- Tasks 3–8 construct `ProviderReadinessServiceImpl(lambda tenant: SqlAlchemyProviderReadinessUnitOfWork(factory, tenant, now=now), ...)`.

- [ ] **Step 1: Write failing migration, isolation, append-only, and concurrency tests**

Create these named tests:

```python
async def test_configuration_and_validation_round_trip_is_tenant_scoped(db_factory): ...
async def test_other_tenant_cannot_read_or_append_stream(db_factory): ...
async def test_same_stream_concurrent_events_receive_unique_monotonic_sequences(db_factory): ...
async def test_same_idempotency_key_same_payload_is_noop(db_factory): ...
async def test_same_idempotency_key_different_payload_conflicts(db_factory): ...
async def test_new_configuration_invalidates_persisted_old_runtime(db_factory): ...
async def test_runtime_composed_requires_current_passed_configuration(db_factory): ...
async def test_database_rejects_update_and_delete(db_factory): ...
async def test_migration_upgrade_downgrade_upgrade_round_trip(alembic_runner): ...
```

The concurrency test opens two independent sessions, waits on a barrier, appends to the same tenant/provider/capability stream, and asserts sequences `{2, 3}` after the initial configured event. The cross-tenant test inserts identical configuration versions for two tenants and proves each snapshot contains only its own actor and event IDs.

- [ ] **Step 2: Run RED and confirm missing migration/table**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/integration/test_provider_readiness_repository.py
```

Expected: collection or schema failure names `ProviderReadinessEventRow` or migration `0033`, not a Docker/network failure.

- [ ] **Step 3: Add migration and ORM row**

Create `0033` with `down_revision = "0032"`. The table has these concrete columns:

```text
tenant_id varchar(32) not null
provider_readiness_event_id varchar(30) not null
provider varchar(32) not null
capability_set varchar(64)[] not null
sequence bigint not null
event_type varchar(32) not null
configuration_version varchar(32) not null
configuration_hash char(64) not null
connector_profile_version varchar(32) not null
transport_profile varchar(64) not null
api_key_version varchar(32) not null
validation_key varchar(200) null
outcome_code varchar(64) null
evidence_ref varchar(200) null
actor_id varchar(64) not null
occurred_at timestamptz not null
idempotency_key varchar(200) not null
```

Add primary key `(tenant_id, provider_readiness_event_id)`, unique `(tenant_id, provider, capability_set, sequence)`, and unique `(tenant_id, provider, capability_set, idempotency_key)`. Add CHECK constraints for the five event types, lower-case provider/profile/version shapes, lowercase 64-hex hash, nonempty two-item canonical capability array, and event-specific null pairs. The migration creates `guard_provider_readiness_append_only()` and a trigger rejecting UPDATE/DELETE. Downgrade drops trigger, function, indexes, and table in reverse order.

- [ ] **Step 4: Implement advisory-locked repository and UoW**

`ProviderReadinessRepositoryImpl.append()` first asserts the method tenant matches its bound tenant, then executes:

```sql
SELECT pg_advisory_xact_lock(
  hashtextextended(
    'provider-readiness:' || :tenant_id || ':' || :provider || ':' || :capability_key,
    0
  )
)
```

Inside the same transaction it checks the idempotency key, loads `max(sequence)`, assigns `sequence + 1`, validates the transition against locked current events, and inserts. `list_events()` filters by tenant, provider, and exact capability array and orders by sequence ascending. The UoW binds every repository instance to one tenant and commits/rolls back/closes like `SqlAlchemyComplianceUnitOfWork`.

- [ ] **Step 5: Run GREEN, migration roundtrip, and single-head check**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/integration/test_provider_readiness_repository.py
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 alembic heads
```

Expected: tests pass and output is exactly `0033 (head)`.

- [ ] **Step 6: Commit Task 2**

```bash
git add migrations/versions/0033_provider_readiness_events.py infra/db/tables.py \
  infra/db/repositories/provider_readiness.py infra/db/provider_readiness_uow.py \
  tests/integration/test_provider_readiness_repository.py
git commit -m "feat: persist provider readiness events"
```

---

### Task 3: Parse Hunter deployment metadata and declare configurations without secrets

**Files:**
- Modify: `apps/scheduler_worker/config.py`
- Create: `scripts/configure_hunter_provider.py`
- Modify: `infra/.env.example`
- Create: `tests/unit/test_scheduler_worker_config.py`
- Create: `tests/integration/test_configure_hunter_provider.py`

**Interfaces:**
- Consumes Task 1 `ProviderConfiguration`, `ProviderReadinessActor`, permissions, and service.
- Produces `HunterContactsSettings.from_environ(environ: Mapping[str, str]) -> HunterContactsSettings` and `configuration: ProviderConfiguration | None`.
- Produces `scripts.configure_hunter_provider.main(environ: Mapping[str, str] | None = None, argv: Sequence[str] | None = None) -> int`.
- Tasks 5–6 consume `SchedulerWorkerConfig.hunter_contacts`.

- [ ] **Step 1: Write the strict environment matrix tests**

```python
def test_disabled_hunter_configuration_contains_no_secret_metadata() -> None:
    settings = HunterContactsSettings.from_environ({"TRADEOS_HUNTER_CONTACTS_ENABLED": "false"})
    assert settings.enabled is False
    assert settings.configuration is None
    assert settings.secret_ref is None

def test_enabled_hunter_configuration_builds_safe_hash_without_resolving_secret() -> None:
    environ = {
        "TRADEOS_HUNTER_CONTACTS_ENABLED": "true",
        "TRADEOS_HUNTER_CONFIGURATION_VERSION": "deploy-v1",
        "TRADEOS_HUNTER_API_KEY_SECRET_REF": "HUNTER_API_KEY_PROD",
        "TRADEOS_HUNTER_API_KEY_VERSION": "key-v1",
    }
    settings = HunterContactsSettings.from_environ(environ)
    assert settings.configuration == ProviderConfiguration.hunter_contacts("deploy-v1", "key-v1")
    assert settings.secret_ref == "HUNTER_API_KEY_PROD"
    assert "HUNTER_API_KEY_PROD" not in repr(settings)
```

Parametrize rejection of missing enable flag, `True`, `FALSE`, `1`, enabled with any one field missing, disabled with any nonempty companion field, malformed secret reference, malformed versions, control characters, and secret-value-looking text in version fields.

- [ ] **Step 2: Run RED and confirm missing settings type**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_scheduler_worker_config.py
```

Expected: import failure for `HunterContactsSettings`.

- [ ] **Step 3: Implement strict config parsing and attach it to SchedulerWorkerConfig**

Add:

```python
@dataclass(frozen=True)
class HunterContactsSettings:
    enabled: bool
    configuration: ProviderConfiguration | None
    secret_ref: str | None = field(repr=False)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> Self: ...
```

`SchedulerWorkerConfig` gains `hunter_contacts: HunterContactsSettings` and always requires `TRADEOS_HUNTER_CONTACTS_ENABLED`. Existing scheduler fixtures explicitly set it to `false`; no implicit compatibility default is added. `infra/.env.example` contains empty safe reference/version shapes and explanatory comments, never a runnable key.

- [ ] **Step 4: Write failing PostgreSQL command tests**

The integration tests call `main()` with a temporary database URL, tenant, explicit operator ID, and enabled safe configuration. Assert first invocation appends one configured event, exact replay returns 0 without adding a row, same configuration version with changed API-key version returns a fixed nonzero code and no secret output, disabled config returns a fixed nonzero code, and neither a resolver nor transport is constructed.

- [ ] **Step 5: Implement the configuration declaration command**

The command accepts `--actor-id` and reads only `DATABASE_URL`, `TRADEOS_TENANT_ID`, and the four Hunter metadata variables. It constructs a `ProviderReadinessActor` with `CONFIGURE` and `READ`, invokes `declare_configuration()` using idempotency key `hunter-config:<configuration_version>`, prints only this JSON shape, and disposes the engine in `finally`:

```json
{"provider":"hunter","configuration_version":"deploy-v1","state":"validation_not_run"}
```

All exceptions map to fixed messages and nonzero exit codes; DSN, secret ref, environment values, raw exception text, and traceback are not printed.

- [ ] **Step 6: Run focused tests and sensitive scan**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_scheduler_worker_config.py tests/integration/test_configure_hunter_provider.py
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  python scripts/scan_sensitive.py
```

Expected: all pass; scan output contains no canary.

- [ ] **Step 7: Commit Task 3**

```bash
git add apps/scheduler_worker/config.py scripts/configure_hunter_provider.py infra/.env.example \
  tests/unit/test_scheduler_worker_config.py tests/integration/test_configure_hunter_provider.py
git commit -m "feat: declare hunter provider configuration"
```

---

### Task 4: Validate Hunter `/account` through Tool Gateway

**Files:**
- Modify: `connectors/hunter/client.py`
- Create: `connectors/hunter/secrets.py`
- Create: `tool_gateway/handlers/provider_validation.py`
- Modify: `tool_gateway/handlers/__init__.py`
- Create: `scripts/validate_hunter_provider.py`
- Create: `tests/unit/test_provider_validation_handler.py`
- Create: `tests/integration/test_provider_validation_gateway.py`

**Interfaces:**
- Consumes current configuration and Task 1 service transitions.
- Produces `BoundHunterSecretResolver` and `HunterConnector.validate_account() -> None` with classified exceptions.
- Produces `PROVIDER_VALIDATION_MANIFEST`, `ProviderValidationHandler`, and `ProviderValidationRateLimitCheck`.
- Produces a CLI that constructs Tool Gateway with controlled dependency injection in tests and `HunterApiHttpTransport` in production.

- [ ] **Step 1: Write connector and handler RED tests**

Test exact behavior:

```python
async def test_validate_account_uses_fixed_non_pii_endpoint() -> None:
    connector, transport = configured_connector([HunterHttpResponse(200, {"data": {"requests": {}}})])
    await connector.validate_account()
    assert transport.calls == [("/account", ())]

async def test_handler_commits_started_before_provider_io() -> None:
    order: list[str] = []
    handler = validation_handler(readiness=RecordingReadiness(order), transport=RecordingTransport(order))
    await handler.execute(TENANT, await handler.prepare(context(), None))
    assert order == ["validation_started", "provider", "validation_passed"]

async def test_uncertain_transport_leaves_only_started() -> None:
    handler, readiness = validation_handler(error=HunterNetworkError(True))
    with pytest.raises(ToolGatewayError) as captured:
        await handler.execute(TENANT, await handler.prepare(context(), None))
    assert captured.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert readiness.events == [ProviderReadinessEventType.VALIDATION_STARTED]
```

Add mappings for 401/auth, 403 and 429/rate-limited with bounded retry-after, 5xx/transient, malformed 200/response-invalid, resolver failure/auth, definite network failure/transient, and readiness-result commit failure/inconclusive. Test `BoundHunterSecretResolver` maps only the connector's fixed logical `HUNTER_API_KEY_REF` to the configured safe deployment reference and keeps both references out of repr/errors. Canary tests inspect `repr`, `str(error)`, caplog, returned mapping, readiness events, and persisted tool ledger.

- [ ] **Step 2: Run RED and confirm missing validation symbols**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_provider_validation_handler.py
```

Expected: missing `validate_account` or `provider_validation` module.

- [ ] **Step 3: Implement classified account validation**

`HunterConnector.validate_account()` requires prior `configure()`, calls transport `get("/account", (), api_key=...)`, requires HTTP 200 plus a mapping `data`, discards the payload, and raises the existing classified Hunter exceptions. Add `HunterResponseInvalidError(HunterConnectorError)` for malformed successful responses so the handler persists `response_invalid` distinctly from `provider_permanent`; do not include payload or nested exception text.

- [ ] **Step 4: Implement manifest, prepared payload, handler, and nonnumeric rate-limit check**

Use this manifest:

```python
PROVIDER_VALIDATION_MANIFEST = ToolManifest(
    tool_id="provider.hunter.validate",
    version="v1",
    description="验证当前 Hunter Provider 安全配置",
    risk_level=RiskLevel.MEDIUM,
    cost_class=CostClass.LOW,
    requires_approval=False,
    idempotency=IdempotencyRequirement.REQUIRED,
    required_permissions=("provider:validate",),
    checks=("tenant", "permission", "idempotency", "rate_limit"),
    input_schema={
        "type": "object",
        "properties": {
            "configuration_version": {
                "type": "string",
                "pattern": "^[a-z0-9][a-z0-9._-]{0,31}$",
            },
        },
        "required": ("configuration_version",),
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "provider_ref": {"type": "string"},
            "configuration_version": {"type": "string"},
            "status": {"type": "string", "enum": ("validation_passed",)},
        },
        "required": ("provider_ref", "configuration_version", "status"),
        "additionalProperties": False,
    },
    redact_fields=(),
)
```

`prepare()` requires a matching current configuration, copies the canonical Gateway idempotency key into a repr-disabled payload as `validation_key`, and fingerprints only tenant/provider/configuration hash/version. `execute()` appends started, configures connector through `BoundHunterSecretResolver`, validates account, appends passed with safe evidence ref `hunter-account:<validation-event-id>`, and returns only `provider_ref`, `configuration_version`, and fixed `status="validation_passed"`.

`ProviderValidationRateLimitCheck` reads the current snapshot. It allows exactly `VALIDATION_NOT_RUN` and `VALIDATION_FAILED`, and rejects `VALIDATION_INCONCLUSIVE`, `RUNTIME_NOT_COMPOSED`, and `READY`; this enforces one active/manual validation per configuration without inventing a numeric business threshold. A retry after failed validation requires a new explicit Gateway idempotency key.

- [ ] **Step 5: Implement the explicit validation CLI**

The CLI requires enabled Hunter settings, a matching durable configuration, `--actor-id`, and `--idempotency-key`. It constructs `HunterApiHttpTransport`, `EnvironmentSecretResolver`, the validation handler, exact four checks, and `SqlAlchemyToolGatewayUnitOfWork`. It prints only tool ID, fixed status/category, safe tool-call ID, and current configuration version. Duplicate returns the canonical result; reconciliation-required remains nonzero and prints no exception text.

- [ ] **Step 6: Run unit and PostgreSQL Gateway tests**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_provider_validation_handler.py tests/integration/test_provider_validation_gateway.py
```

Expected: pass with controlled transport only. The integration test queries `tool_calls`, `tool_call_events`, and `provider_readiness_events` and proves no API key canary or raw response field exists.

- [ ] **Step 7: Commit Task 4**

```bash
git add connectors/hunter/client.py connectors/hunter/secrets.py \
  tool_gateway/handlers/provider_validation.py \
  tool_gateway/handlers/__init__.py scripts/validate_hunter_provider.py \
  tests/unit/test_provider_validation_handler.py tests/integration/test_provider_validation_gateway.py
git commit -m "feat: validate hunter provider readiness"
```

---

### Task 5: Register both Hunter contact tools only for the validated configuration

**Files:**
- Modify: `tool_gateway/handlers/contact_enrichment.py`
- Modify: `tool_gateway/handlers/contact_verification.py`
- Modify: `apps/scheduler_worker/hunter_contacts.py`
- Modify: `tests/unit/test_contact_enrichment_handler.py`
- Modify: `tests/unit/test_contact_verification_handler.py`
- Modify: `tests/unit/test_country_policy_runtime_composition.py`
- Modify: `tests/integration/test_tool_gateway_contact_enrichment.py`
- Modify: `tests/integration/test_tool_gateway_contact_verification.py`

**Interfaces:**
- Consumes Task 1 `ProviderRuntimeGuard`, snapshot, and configuration hash.
- Changes `HunterProviderContactEnricher` and `HunterProviderContactVerifier` constructors to accept `readiness_guard` and `configuration_hash`.
- Changes `HunterContactComposition` to carry only `discovery_policy` and optional quota; secret resolver/ref/transport are supplied by scheduler production composition.
- Changes `HunterContactTools` to include `registered_configuration_hash: str | None`.

- [ ] **Step 1: Replace the old intentional-unregistered test with a full matrix**

Add parametrized cases:

```python
@pytest.mark.parametrize("state", [
    ProviderReadinessState.PROVIDER_NOT_CONFIGURED,
    ProviderReadinessState.VALIDATION_NOT_RUN,
    ProviderReadinessState.VALIDATION_FAILED,
    ProviderReadinessState.VALIDATION_INCONCLUSIVE,
])
async def test_nonvalidated_state_registers_neither_contact_tool(state): ...

async def test_passed_current_configuration_registers_exactly_both_tools() -> None:
    tools = build_tools(snapshot=passed_snapshot())
    assert tools.registered_configuration_hash == CONFIG.configuration_hash
    assert tools.manifest_ids == ("contact.enrich", "contact.verify")

async def test_already_composed_current_configuration_registers_exactly_both_tools() -> None:
    tools = build_tools(snapshot=ready_snapshot())
    assert tools.registered_configuration_hash == CONFIG.configuration_hash
    assert tools.manifest_ids == ("contact.enrich", "contact.verify")
```

The first activation construction input is Task 1 state `RUNTIME_NOT_COMPOSED`; a later scheduler restart sees `READY` and must rebuild the same two tools for the same current configuration. Keep the existing country-policy test and assert unknown/denied/read-failure paths call transport zero times.

- [ ] **Step 2: Write stale-runtime and reader-failure adapter tests**

For each contact adapter, create a guard that raises `ProviderReadinessUnavailableError` and assert connector factory, secret resolver, and transport counters remain zero. Add a guard that first allows and then observes a new config; the second call must fail before connector creation. Assert the handler maps this operational failure to a fixed permanent Gateway category without error text.

- [ ] **Step 3: Run RED and verify current one-tool registry fails**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_country_policy_runtime_composition.py \
    tests/unit/test_contact_enrichment_handler.py tests/unit/test_contact_verification_handler.py
```

Expected: the dual-registration and new constructor assertions fail against the current verify-only implementation.

- [ ] **Step 4: Add the live readiness guard to both Provider adapters**

The first line that can create a connector becomes:

```python
await self._readiness_guard.require_current(tenant_id, self._configuration_hash)
connector = self._connector_factory(tenant_id)
```

Catch `ProviderReadinessUnavailableError` in both handlers before the Hunter error mapping and raise `ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)` from `None`. Do not include current/expected hashes in the exception or ledger.

- [ ] **Step 5: Implement conditional exact dual registration**

`build_hunter_contact_tools()` accepts explicit `secret_resolver`, `secret_ref`, `transport`, `snapshot`, and `readiness_guard`, and reuses Task 4 `BoundHunterSecretResolver` instead of keeping a scheduler-private duplicate. If the snapshot is neither `RUNTIME_NOT_COMPOSED` nor `READY` for the same current configuration, create an empty registry and return two fail-closed trusted adapters with `registered_configuration_hash=None`. If it matches either allowed state, register `CONTACT_ENRICH_MANIFEST` and `CONTACT_VERIFY_MANIFEST`, authorize only the exact two tool IDs for the scheduler tool user, and assert sorted registry IDs equal `("contact.enrich", "contact.verify")`.

- [ ] **Step 6: Run focused unit and real-ledger regression tests**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_country_policy_runtime_composition.py \
    tests/unit/test_contact_enrichment_handler.py tests/unit/test_contact_verification_handler.py \
    tests/integration/test_tool_gateway_contact_enrichment.py \
    tests/integration/test_tool_gateway_contact_verification.py
```

Expected: all pass; ready fixtures still use controlled transports.

- [ ] **Step 7: Commit Task 5**

```bash
git add tool_gateway/handlers/contact_enrichment.py tool_gateway/handlers/contact_verification.py \
  apps/scheduler_worker/hunter_contacts.py tests/unit/test_contact_enrichment_handler.py \
  tests/unit/test_contact_verification_handler.py tests/unit/test_country_policy_runtime_composition.py \
  tests/integration/test_tool_gateway_contact_enrichment.py \
  tests/integration/test_tool_gateway_contact_verification.py
git commit -m "feat: gate hunter contact tool registration"
```

---

### Task 6: Compose Hunter in scheduler and record runtime only after lock acquisition

**Files:**
- Modify: `apps/scheduler_worker/main.py`
- Modify: `apps/scheduler_worker/runtime.py`
- Modify: `apps/scheduler_worker/AGENTS.md`
- Modify: `tests/integration/test_scheduler_worker.py`
- Modify: `tests/e2e/conftest.py`

**Interfaces:**
- Consumes Tasks 2–5 readiness service, strict config, fixed transport, and conditional tool builder.
- Produces `RuntimeActivation` Protocol and optional `SchedulerRuntime.activation`.
- Produces a scheduler composition that uses `HunterApiHttpTransport()` by default and permits an injected `hunter_transport_factory` only for tests.

- [ ] **Step 1: Write lock-order and cleanup RED tests**

Add:

```python
async def test_runtime_activation_runs_after_lock_and_before_first_cycle(db_engine) -> None:
    order: list[str] = []
    runtime = scheduler_runtime(
        activation=RecordingActivation(order),
        outbox=RecordingOutbox(order),
        workflow=RecordingWorkflow(order),
    )
    stop_after_one_cycle(runtime)
    result = await run_scheduler_worker(runtime, install_signal_handlers=False)
    assert result.status is WorkerStartStatus.STARTED
    assert order[:3] == ["lock_confirmed", "runtime_composed", "outbox_pre"]

async def test_activation_failure_releases_lock_and_runs_no_cycle(db_engine) -> None:
    runtime = scheduler_runtime(activation=FailingActivation())
    with pytest.raises(TransientError):
        await run_scheduler_worker(runtime, install_signal_handlers=False)
    assert await can_acquire_same_lock(db_engine)
    assert runtime.outbox.calls == 0
    assert runtime.workflow.calls == 0

async def test_worker_without_lock_never_writes_runtime_composed(db_engine) -> None: ...
```

- [ ] **Step 2: Run RED and confirm activation is absent**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/integration/test_scheduler_worker.py -k 'activation or runtime_composed'
```

Expected: constructor/signature failures for `RuntimeActivation` or missing order assertions.

- [ ] **Step 3: Add post-lock activation to the scheduler loop**

Define:

```python
class RuntimeActivation(Protocol):
    async def activate(self) -> None: ...

@dataclass(frozen=True)
class SchedulerRuntime:
    ...
    activation: RuntimeActivation | None = None
```

After `pg_try_advisory_lock` succeeds and `_same_lock_backend` confirms the dedicated connection, call `await runtime.activation.activate()` before `_run_cycle`. Let failure propagate through the existing process boundary, while `finally` releases the same advisory lock. Do not log exception text.

- [ ] **Step 4: Write runtime-factory RED tests for disabled, incomplete, pending, and passed composition**

Use controlled readiness events and an injected transport factory. Assert:

| Settings/config state | Runtime behavior |
|---|---|
| Hunter disabled | scheduler builds, no contact registration, no activation |
| enabled but no matching configured event | startup fails before secret resolution |
| configured/pending | scheduler builds fail-closed contact adapters, no activation |
| passed + account discovery absent | startup fails as incomplete production composition |
| passed + account discovery present | exact two tools built, activation present, transport still unused |

- [ ] **Step 5: Implement production composition and matching runtime activation**

`SchedulerRuntimeFactory` gains `hunter_transport_factory: Callable[[], HunterHttpTransport] = HunterApiHttpTransport`. It constructs `EnvironmentSecretResolver`, but never resolves the Hunter ref during startup. It reads the current snapshot from `SqlAlchemyProviderReadinessUnitOfWork`, verifies environment configuration hash equality, and passes the fixed transport plus guard into Task 5.

Create a private `_HunterRuntimeActivation` whose `activate()` calls `mark_runtime_composed()` with system actor `system:scheduler-hunter`, permission `COMPOSE`, and idempotency key `hunter-runtime:<configuration_version>:<connector_profile_version>`. Only attach it when both tools were registered for the validated current hash.

- [ ] **Step 6: Update E2E default scheduler environment and run integration tests**

Every existing scheduler environment fixture adds `TRADEOS_HUNTER_CONTACTS_ENABLED=false`. Run:

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/integration/test_scheduler_worker.py \
    tests/integration/test_scheduler_campaign_driver.py tests/integration/test_scheduler_reply_trigger.py
```

Expected: pass without real DNS, Gmail, or Hunter calls.

- [ ] **Step 7: Commit Task 6**

```bash
git add apps/scheduler_worker/main.py apps/scheduler_worker/runtime.py \
  apps/scheduler_worker/AGENTS.md tests/integration/test_scheduler_worker.py tests/e2e/conftest.py
git commit -m "feat: activate hunter tools after scheduler lock"
```

---

### Task 7: Replace API composition guesses with durable readiness

**Files:**
- Modify: `apps/api/dependencies.py`
- Modify: `apps/api/composition/runtime.py`
- Modify: `apps/api/routers/settings.py`
- Modify: `tests/unit/test_settings_router.py`
- Modify: `tests/unit/test_api_app.py`
- Modify: `tests/unit/test_api_runtime.py`
- Modify: `tests/integration/test_api_runtime.py`
- Modify: `tests/integration/test_country_policy_settings_api.py`

**Interfaces:**
- Consumes Task 1 `ProviderReadinessService`/snapshot and Task 2 PostgreSQL UoW.
- Removes `ConfiguredApiDependencies.contact_enrichment_composed: bool`.
- Adds required `provider_readiness: ProviderReadinessService` and a tenant-bound system read actor.
- Expands `ContactEnrichmentReason` to five Provider blocked reasons plus the two country-policy reasons.

- [ ] **Step 1: Write the exact reason-priority RED matrix**

```python
@pytest.mark.parametrize(("provider_state", "reason"), [
    (ProviderReadinessState.PROVIDER_NOT_CONFIGURED, "CONTACT_ENRICHMENT_PROVIDER_NOT_CONFIGURED"),
    (ProviderReadinessState.VALIDATION_NOT_RUN, "CONTACT_ENRICHMENT_PROVIDER_VALIDATION_PENDING"),
    (ProviderReadinessState.VALIDATION_FAILED, "CONTACT_ENRICHMENT_PROVIDER_VALIDATION_FAILED"),
    (ProviderReadinessState.VALIDATION_INCONCLUSIVE, "CONTACT_ENRICHMENT_PROVIDER_VALIDATION_INCONCLUSIVE"),
    (ProviderReadinessState.RUNTIME_NOT_COMPOSED, "CONTACT_ENRICHMENT_RUNTIME_NOT_COMPOSED"),
])
def test_settings_maps_provider_state_to_exact_reason(provider_state, reason): ...
```

Also assert country-policy-not-configured and all-denied return without calling the Provider reader; ready requires both allowed policy coverage and Provider `READY`; Provider reader failure after allowed policy returns the existing sanitized 503; an invalid snapshot type returns sanitized 503; cross-tenant actor fails.

- [ ] **Step 2: Run RED and verify old boolean contract fails**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_settings_router.py tests/unit/test_api_app.py
```

Expected: new reason values and dependency constructor fail against `contact_enrichment_composed=False`.

- [ ] **Step 3: Replace the dependency and router derivation**

Use this exact priority:

```python
if coverage.active_policy_count == 0:
    return blocked("COUNTRY_POLICY_NOT_CONFIGURED")
if coverage.contact_enrichment_allowed_count == 0:
    return blocked("CONTACT_ENRICHMENT_NOT_ALLOWED")
snapshot = await dependencies.provider_readiness.get_snapshot(
    identity.tenant_id, HUNTER_CONTACT_CAPABILITIES, actor=context.provider_readiness_actor
)
return readiness_from_provider_snapshot(snapshot)
```

The API runtime constructs `ProviderReadinessServiceImpl` with `SqlAlchemyProviderReadinessUnitOfWork` and a system READ actor bound to the configured tenant. It does not inspect API-process Hunter environment variables.

- [ ] **Step 4: Regenerate OpenAPI and run API tests**

Start the project runtime in the existing OpenAPI export harness, then run:

```bash
cd apps/web
set -o pipefail
WEB_NODE=/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  python scripts/export_openapi.py | \
  "${WEB_NODE}" node_modules/openapi-typescript/bin/cli.js -o src/api/api.d.ts
cd ../..
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_settings_router.py tests/unit/test_api_app.py \
    tests/unit/test_api_runtime.py tests/integration/test_api_runtime.py \
    tests/integration/test_country_policy_settings_api.py
```

Expected: all pass and generated `ContactEnrichmentReason` contains no `CONTACT_ENRICHMENT_NOT_COMPOSED`.

- [ ] **Step 5: Commit Task 7**

```bash
git add apps/api/dependencies.py apps/api/composition/runtime.py apps/api/routers/settings.py \
  apps/web/src/api/api.d.ts tests/unit/test_settings_router.py tests/unit/test_api_app.py \
  tests/unit/test_api_runtime.py tests/integration/test_api_runtime.py \
  tests/integration/test_country_policy_settings_api.py
git commit -m "feat: expose durable hunter readiness"
```

---

### Task 8: Present truthful readiness in Settings and exercise the real stack

**Files:**
- Modify: `apps/web/src/views/settings/SettingsCenter.vue`
- Modify: `apps/web/tests/settings.test.ts`
- Create: `tests/e2e/test_hunter_provider_readiness_settings.py`

**Interfaces:**
- Consumes Task 7 generated API types and exact reason codes.
- Produces Chinese remediation copy only; it does not write readiness or accept secrets.

- [ ] **Step 1: Write failing component tests for every Provider state**

Assert the exact messages:

```typescript
const providerMessages = [
  ["CONTACT_ENRICHMENT_PROVIDER_NOT_CONFIGURED", "部署尚未声明 Hunter 安全配置版本"],
  ["CONTACT_ENRICHMENT_PROVIDER_VALIDATION_PENDING", "Hunter 配置已声明，等待人工 Provider 验证"],
  ["CONTACT_ENRICHMENT_PROVIDER_VALIDATION_FAILED", "Hunter Provider 验证失败，请按固定分类排查"],
  ["CONTACT_ENRICHMENT_PROVIDER_VALIDATION_INCONCLUSIVE", "Hunter 验证结果不确定，禁止自动重试"],
  ["CONTACT_ENRICHMENT_RUNTIME_NOT_COMPOSED", "验证已通过，等待 scheduler 重启并完成工具注册"],
] as const;
```

For each case render both Playbook and country-policy readiness banners. Assert `ready` renders “联系人补全生产组合已就绪” plus a warning that each target country is still checked. Assert the DOM contains no `api key`, `secret`, password input, validation success button, or direct activation control.

- [ ] **Step 2: Run RED and verify missing copy**

```bash
cd apps/web
WEB_NODE=/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node
"${WEB_NODE}" node_modules/vitest/vitest.mjs run tests/settings.test.ts
```

Expected: TypeScript or message assertions fail for the new reason codes.

- [ ] **Step 3: Implement the reason mapping and accessible status presentation**

Extend `readinessMessage()` with exhaustive generated-union cases. Keep state derived entirely from the API. Use the existing safe-banner structure, status text, and `aria-live` behavior; do not add a credential form or action button.

- [ ] **Step 4: Write and run real-stack E2E**

The E2E test:

1. starts the existing PostgreSQL/API/Vite stack with Hunter disabled;
2. inserts an active synthetic country policy allowing enrichment, then observes Provider not configured;
3. uses `ProviderReadinessServiceImpl` to declare a synthetic safe configuration without a secret;
4. reloads Settings and observes validation pending in both readiness locations;
5. verifies browser network requests contain no `hunter.io` URL;
6. verifies `tool_calls` has no `provider.hunter.validate`, `contact.enrich`, or `contact.verify` rows;
7. checks 1440×900 and 390×844 viewports for no horizontal overflow and no console errors.

Run:

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/e2e/test_hunter_provider_readiness_settings.py
```

Expected: pass with zero external Provider calls.

- [ ] **Step 5: Run complete frontend verification**

```bash
cd apps/web
WEB_NODE=/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node
"${WEB_NODE}" node_modules/vitest/vitest.mjs run
"${WEB_NODE}" node_modules/vue-tsc/bin/vue-tsc.js --noEmit
"${WEB_NODE}" node_modules/vite/bin/vite.js build
"${WEB_NODE}" node_modules/eslint/bin/eslint.js .
```

Expected: tests/typecheck/build pass; lint has zero errors. Existing warnings may remain only if the count and files are recorded in the task report.

- [ ] **Step 6: Commit Task 8**

```bash
git add apps/web/src/views/settings/SettingsCenter.vue apps/web/tests/settings.test.ts \
  tests/e2e/test_hunter_provider_readiness_settings.py
git commit -m "feat: show hunter provider readiness"
```

---

### Task 9: Document operations, audit scope, and run the complete acceptance gate

**Files:**
- Create: `docs/operations/hunter-provider-readiness.md`
- Modify: `HANDBOOK.md`
- Modify: `docs/architecture/04-tool-gateway.md`
- Modify: `docs/architecture/08-compliance.md`
- Modify: `docs/architecture/11-deployment.md`
- Modify: `README.md`
- Modify: `tool_gateway/AGENTS.md`
- Modify: `apps/scheduler_worker/AGENTS.md`
- Test: `tests/unit/test_scan_sensitive.py`

**Interfaces:**
- Consumes all delivered code and exposes no new runtime interface.
- Produces the exact operator sequence and truthful completion boundary.

- [ ] **Step 1: Write the operations runbook with fixed evidence states**

The runbook contains these sections and commands:

```text
1. Preconditions and identities
2. Declare configuration without resolving the secret
3. Observe validation_pending
4. Run one explicit provider.hunter.validate call
5. Handle auth/rate/transient/permanent/inconclusive outcomes
6. Restart singleton scheduler
7. Verify runtime_composed and Settings ready
8. Rotate or roll back by creating a new configuration version
9. Record real smoke status: not_run / passed / failed / inconclusive
10. Separate Provider readiness from Phase 1 operating acceptance
```

Commands use placeholders such as `$TRADEOS_OPERATOR_ID` and never echo or interpolate the API key. The evidence record contains only tenant ID, safe configuration version, validation key, tool-call ID, fixed outcome, UTC timestamps, scheduler runtime fact ID, and operator ID.

- [ ] **Step 2: Update architecture and handbook truth statements**

State precisely:

- production composition code and persistent activation gate are implemented;
- missing configuration/validation keeps both Hunter tools closed;
- real Hunter validation remains `not_run` in repository acceptance;
- readiness does not replace per-call country policy;
- Phase 1 still requires a real Campaign, evidence chain, healthy sending reputation, and measured handoff SLA.

Correct README’s obsolete “业务实现尚未开始” statement using current delivered slices without claiming Phase 1 completion.

- [ ] **Step 3: Extend the sensitive scanner regression**

Add a temporary-file test containing Hunter key canaries in validation-related JSON/log-shaped text. Assert `scripts/scan_sensitive.py` reports only the fixed category/path and never the matched value. Add repository assertions that committed fixtures and docs contain reference names but no runnable secret value.

- [ ] **Step 4: Run focused documentation/security checks**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_scan_sensitive.py
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  python scripts/scan_sensitive.py
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  python scripts/check_boundaries.py
```

Expected: all pass and no sensitive value appears in output.

- [ ] **Step 5: Run the full backend acceptance gate**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 make check
```

Expected: ruff, mypy, structural boundary scan, sensitive scan, and the complete pytest suite pass.

- [ ] **Step 6: Run the full frontend and migration acceptance gate**

```bash
cd apps/web
WEB_NODE=/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node
"${WEB_NODE}" node_modules/vitest/vitest.mjs run
"${WEB_NODE}" node_modules/vue-tsc/bin/vue-tsc.js --noEmit
"${WEB_NODE}" node_modules/vite/bin/vite.js build
"${WEB_NODE}" node_modules/eslint/bin/eslint.js .
cd ../..
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 alembic heads
```

Expected: frontend tests/typecheck/build pass, lint has zero errors, and Alembic prints exactly `0033 (head)`.

- [ ] **Step 7: Prove external actions remained absent**

Record in the task report:

```text
Hunter API key configured: no
Real Hunter network requested: no
Real Provider validation: not_run
Production contact tools activated: no external deployment fact
Phase 1 operating acceptance: not_run
```

Query test databases/log captures to prove no real Hunter hostname was contacted and no canary was persisted.

- [ ] **Step 8: Commit Task 9**

```bash
git add docs/operations/hunter-provider-readiness.md HANDBOOK.md README.md \
  docs/architecture/04-tool-gateway.md docs/architecture/08-compliance.md \
  docs/architecture/11-deployment.md tool_gateway/AGENTS.md \
  apps/scheduler_worker/AGENTS.md tests/unit/test_scan_sensitive.py
git commit -m "docs: record hunter readiness operations"
```

---

## Specification Coverage

| Design acceptance criterion | Implemented and proved by |
|---|---|
| 1. API and scheduler stop guessing composition from booleans | Tasks 6–7; API runtime and scheduler composition tests |
| 2. Tenant-scoped append-only facts link canonical validation key, actor, configuration, and Gateway ledger | Tasks 1–4; repository and Gateway integration tests |
| 3. Configuration/key-version changes invalidate prior validation/runtime | Tasks 1–3 and 5; state-machine, PostgreSQL, and stale-adapter tests |
| 4. Every closed state performs zero secret resolution and zero Hunter IO | Tasks 3, 5, 6, and 8; counter-based unit tests and real-stack E2E |
| 5. Manual validation uses only Tool Gateway `/account`, no PII or raw response persistence | Task 4; fixed-transport, ledger, and canary tests |
| 6. Only exact current passed configuration registers both contact tools | Tasks 5–6; dual-registration matrix |
| 7. Only lock owner writes runtime fact; failure releases lock before any cycle | Task 6; scheduler lock-order integration tests |
| 8. Settings reasons correspond to durable facts and never show false ready | Tasks 7–8; API matrix, component tests, and E2E |
| 9. Ready enrichment still applies Playbook, policy, suppression, and quota checks | Tasks 5 and 7; existing Gateway regression suites remain in the focused gate |
| 10. Full verification, one Alembic head, and no sensitive canary leakage | Tasks 2, 4, 8, and 9; full backend/frontend/security gates |
| 11. Real Hunter smoke remains `not_run` and no rollout is claimed | Task 9 runbook and completion evidence |
| 12. Real validation, Campaign journey, and HANDBOOK operating acceptance remain outstanding | Task 9 documentation and final task report |

---

## Final Review Checklist

- [ ] Every specification acceptance criterion maps to at least one task and named test.
- [ ] There is no production code path that registers only one Hunter contact tool.
- [ ] There is no environment-only boolean used by Settings as composition truth.
- [ ] `validation_started` ordering is Gateway EXECUTING → readiness started → Provider IO.
- [ ] New configuration disables old in-memory adapters before secret resolution.
- [ ] Only lock-owning scheduler writes runtime composition and failure prevents the first cycle.
- [ ] Provider validation error/result persistence contains fixed categories only.
- [ ] All config, repository, API, and UI paths remain tenant-scoped.
- [ ] Automated tests use controlled transport only.
- [ ] Real Provider validation and Phase 1 acceptance are reported as `not_run`.
