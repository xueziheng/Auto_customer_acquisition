# Country Policy Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build tenant-scoped, immutable, independently approved country-policy packages with field-level provenance, fail-closed Gateway decisions, Settings management, and truthful pre-composition readiness.

**Architecture:** Add a new `domains/compliance` business domain whose public service owns normalized country keys, immutable versions, append-only activations, and structured policy decisions. Persist proposals and a `CountryPolicyVersionProposed` outbox event atomically, use a durable `country_policy_change` workflow to obtain independent approval and activate the exact version, then inject the service into Gateway readers and Settings without registering production `contact.enrich`.

**Tech Stack:** Python 3.12, Pydantic v2, SQLAlchemy 2.x async, PostgreSQL/Alembic, FastAPI, Vue 3 + TypeScript + Vite + Ant Design Vue, pytest, Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-08-24-country-policy-readiness-design.md`

## Global Constraints

- The system MUST NOT seed, infer, search for, or recommend any country's legal conclusions; tests use synthetic country labels and sources only.
- Every table and every repository query MUST include `tenant_id`; composite foreign keys and unique constraints include tenant scope.
- `domains/compliance` imports only `shared.*`; other domains do not import compliance internals. Upper layers and Tool Gateway may consume only `domains.compliance.service` and `domains.compliance.schemas`.
- Country matching is exact after NFKC, trim, whitespace collapse, and `casefold`; no alias or ISO inference is allowed.
- Unknown country, explicit denial, invalid reader output, and storage failure all fail closed before Provider IO.
- Every decision-bearing field has its own human-confirmed `Provenance`; `SourceType.AGENT_INFERENCE` is forbidden.
- Candidate versions, field provenance, and activations are append-only; there is no update, delete, force, bootstrap, direct activate, or apply-now public path.
- Proposal and approval actors MUST differ. Activation accepts only a narrow fact read from an approved approval record.
- No money or numeric confidence fields are introduced. Strict booleans have no business defaults.
- Production `contact.enrich` remains unregistered in this plan. Readiness can reach `CONTACT_ENRICHMENT_NOT_COMPOSED`, never “ready”.
- New shared strong IDs require ADR 0010 before editing `shared/schemas/identifiers.py`.
- All production changes follow RED → verify expected failure → minimal GREEN → verify pass → refactor while green.
- Run Python commands through `/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312` or `/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3`.

---

## File Map

### New domain and public contract

- `docs/adr/0010-country-policy-version-approval-contract.md` — accepted contract decision for the new shared IDs and version/activation split.
- `domains/compliance/AGENTS.md` — policy-domain responsibilities, authorization matrix, state facts, dependencies, and prohibited shortcuts.
- `domains/compliance/{__init__,errors,events,models,permissions,repository,schemas,service,service_impl}.py` — pure domain contract and behavior.
- `shared/schemas/identifiers.py` — `CountryPolicyVersionId` and `CountryPolicyActivationId`.
- `shared/events/catalog.py` and `shared/events/registry.py` — `CountryPolicyVersionProposed` durable event.
- `domains/approvals/models.py` and public export — `COUNTRY_POLICY_CHANGE`, seven-day validity.

### Persistence

- `migrations/versions/0032_country_policy_packages.py` — versions, relational field provenance, activations, tenant constraints, append-only triggers.
- `infra/db/tables.py` — SQLAlchemy rows.
- `infra/db/repositories/compliance.py` — tenant-bound repositories.
- `infra/db/compliance_uow.py` — repositories and `PostgresEventBus` in one transaction.

### Workflow and runtime

- `workflows/country_policy_change/` — durable definition, approval event handler, steps, and local rules.
- `apps/scheduler_worker/runtime.py` — full handlers, event registrations, system actor, and real compliance service.
- `apps/api/composition/runtime.py` — request service, start-only workflow definition, API dependencies, and explicit `contact_enrichment_composed=False`.
- `apps/scheduler_worker/account_discovery.py` — remove deployment allowlist implementation after no consumer remains.
- `apps/scheduler_worker/web_discovery.py` — adapt public research policy reads to the compliance service.
- `tool_gateway/checks/contact_provider.py` and `tool_gateway/checks/web_discovery.py` — structured decision consumption and precise rejection reasons.
- `tool_gateway/checks/country_policy.py` — remove the unused skeleton and expose only shared mapping helpers if needed; no duplicate business policy.

### API and Web

- `apps/api/dependencies.py` — optional `ComplianceService` plus required composition truth.
- `apps/api/routers/settings.py` — policy list, version history, proposal, approval status, and coverage readiness.
- `apps/web/src/views/settings/SettingsCenter.vue` — country-policy workspace inside Settings.
- `apps/web/src/api/api.d.ts` — regenerated OpenAPI types.
- `apps/web/tests/settings.test.ts` and `tests/e2e/test_country_policy_settings.py` — UI and real-stack coverage.

### Documentation and acceptance

- `HANDBOOK.md`, `domains/AGENTS.md`, `docs/architecture/02-boundaries.md`, `04-tool-gateway.md`, `08-compliance.md` — truthful delivered and remaining scope.

---

### Task 1: Lock ADR, IDs, events, permissions, and pure policy contracts

**Files:**
- Create: `docs/adr/0010-country-policy-version-approval-contract.md`
- Create: `domains/compliance/AGENTS.md`
- Create: `domains/compliance/__init__.py`
- Create: `domains/compliance/errors.py`
- Create: `domains/compliance/events.py`
- Create: `domains/compliance/models.py`
- Create: `domains/compliance/permissions.py`
- Create: `domains/compliance/repository.py`
- Create: `domains/compliance/schemas.py`
- Create: `domains/compliance/service.py`
- Modify: `domains/AGENTS.md`
- Modify: `domains/approvals/AGENTS.md`
- Modify: `docs/architecture/02-boundaries.md`
- Modify: `shared/schemas/identifiers.py`
- Modify: `shared/events/catalog.py`
- Modify: `shared/events/registry.py`
- Modify: `domains/approvals/models.py`
- Modify: `domains/approvals/service_impl.py`
- Create: `tests/unit/test_country_policy_contracts.py`
- Modify: `tests/unit/test_approval_service.py`

**Interfaces:**
- Produces `CountryPolicyVersionId = NewType("CountryPolicyVersionId", str)` with `cpp_` values and `CountryPolicyActivationId = NewType("CountryPolicyActivationId", str)` with `cpa_` values.
- Produces `CountryPolicyField`, `CountryPolicyAction`, `CountryPolicyProposalCreate`, `CountryPolicyVersionView`, `CountryPolicyDecision`, `CountryPolicyCoverage`, `CountryPolicyApprovalFact`, `CountryPolicyChangeSnapshot`, `CountryPolicyProposalResult`, and `CountryPolicyActivationView`.
- Produces `ComplianceActor`, `ComplianceScope`, `ComplianceAction`, `ComplianceAuthorizer`, and `Phase1ComplianceAuthorizer`.
- Produces repository/UoW Protocols consumed by Tasks 2–4 and `ComplianceService` consumed by Tasks 5–7.

- [ ] **Step 1: Write the failing contract tests**

Add literal tests that construct a proposal containing every required field and a separate safe source input for each member of `CountryPolicyField`. Include these mutations as independent tests:

```python
DECISION_FIELDS = {
    CountryPolicyField.PUBLIC_RESEARCH_ALLOWED,
    CountryPolicyField.CONTACT_ENRICHMENT_ALLOWED,
    CountryPolicyField.COLD_B2B_EMAIL_ALLOWED,
    CountryPolicyField.PERSONAL_DATA_BASIS_REQUIRED,
    CountryPolicyField.SUBJECT_TYPE_AFFECTS_JUDGMENT,
    CountryPolicyField.CONTACT_TYPE_AFFECTS_JUDGMENT,
    CountryPolicyField.OPT_OUT_DEADLINE_DAYS,
    CountryPolicyField.LOCAL_REPRESENTATIVE_REQUIRED,
    CountryPolicyField.REQUIREMENTS,
}

def test_country_key_is_exact_deterministic_normalization() -> None:
    assert normalize_country_key("  SYNTHETIC\u3000Market  ") == "synthetic market"
    assert normalize_country_key("DE") != normalize_country_key("Germany")

def test_policy_requires_source_for_every_decision_field() -> None:
    payload = valid_payload()
    payload["field_sources"].pop(CountryPolicyField.CONTACT_ENRICHMENT_ALLOWED)
    with pytest.raises(ValidationError):
        CountryPolicyProposalCreate.model_validate(payload)

def test_policy_rejects_untrusted_sources_and_identity_facts() -> None:
    with pytest.raises(ValidationError):
        CountryPolicyProposalCreate.model_validate(payload_with_agent_source())
    with pytest.raises(ValidationError):
        CountryPolicyProposalCreate.model_validate(payload_with_confirmed_by())

@pytest.mark.parametrize("field", [
    "public_research_allowed",
    "contact_enrichment_allowed",
    "cold_b2b_email_allowed",
])
def test_policy_boolean_fields_are_strict_and_required(field: str) -> None:
    payload = valid_payload()
    payload[field] = 1
    with pytest.raises(ValidationError):
        CountryPolicyProposalCreate.model_validate(payload)
```

Also assert rejection of blank/control/65-character countries, malformed requirement codes, duplicate requirements after normalization, opt-out values `0`, `366`, `True`, and missing `notes`. Source tests reject malformed/overlong IDs, HTTP or credential-bearing URLs, missing/uppercase/malformed page hashes, and webpage-only fields on upload/employee input. In this same file, assert `ApprovalType.COUNTRY_POLICY_CHANGE.value == "country_policy_change"` and its validity is seven days. Assert `CountryPolicyVersionProposed` subclasses `DomainEvent`, carries version ID, country key, content hash, and proposed employee, and is registered by the event registry.

- [ ] **Step 2: Run RED and confirm missing contract symbols**

Run:

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_country_policy_contracts.py tests/unit/test_approval_service.py
```

Expected: collection/import failure for `domains.compliance` or missing `COUNTRY_POLICY_CHANGE`, not a fixture or syntax error.

- [ ] **Step 3: Add ADR and minimal pure contracts**

ADR 0010 records: independent compliance domain; immutable version and activation facts; narrow approval fact; `cpp_`/`cpa_` shared IDs; no legal defaults; exact normalized country matching; field-level human-confirmed Provenance.

Implement these exact public shapes in `schemas.py`:

```python
class CountryPolicyField(str, Enum):
    PUBLIC_RESEARCH_ALLOWED = "public_research_allowed"
    CONTACT_ENRICHMENT_ALLOWED = "contact_enrichment_allowed"
    COLD_B2B_EMAIL_ALLOWED = "cold_b2b_email_allowed"
    PERSONAL_DATA_BASIS_REQUIRED = "personal_data_basis_required"
    SUBJECT_TYPE_AFFECTS_JUDGMENT = "subject_type_affects_judgment"
    CONTACT_TYPE_AFFECTS_JUDGMENT = "contact_type_affects_judgment"
    OPT_OUT_DEADLINE_DAYS = "opt_out_deadline_days"
    LOCAL_REPRESENTATIVE_REQUIRED = "local_representative_required"
    REQUIREMENTS = "requirements"
class CountryPolicyAction(str, Enum):
    PUBLIC_RESEARCH = "public_research"
    CONTACT_ENRICHMENT = "contact_enrichment"
    COLD_B2B_EMAIL = "cold_b2b_email"

class CountryPolicyFieldSourceInput(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    source_type: SourceType
    source_id: str
    source_url: str | None = None
    page_hash: str | None = None

class CountryPolicyProposalCreate(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    country: str
    public_research_allowed: bool
    contact_enrichment_allowed: bool
    cold_b2b_email_allowed: bool
    personal_data_basis_required: bool
    subject_type_affects_judgment: bool
    contact_type_affects_judgment: bool
    opt_out_deadline_days: int | None
    local_representative_required: bool
    requirements: list[str]
    notes: str
    field_sources: dict[CountryPolicyField, CountryPolicyFieldSourceInput]

class CountryPolicyDecision(BaseModel):
    country_key: str
    action: CountryPolicyAction
    configured: bool
    allowed: bool
    active_version_id: CountryPolicyVersionId | None
    content_hash: str | None
    requirements: tuple[str, ...]

class CountryPolicyCoverage(BaseModel):
    active_policy_count: int
    contact_enrichment_allowed_count: int

class CountryPolicyProposalResult(BaseModel):
    country_policy_version_id: CountryPolicyVersionId
    country_key: str
    version_number: int
    content_hash: str
    change_set_ref: str
```

`CountryPolicyVersionView` contains the proposal fields plus `country_key`, version ID/number, content/base hashes, proposer/time, immutable field provenance, and change-set reference. `CountryPolicyActivationView` contains activation ID/sequence, country key, exact version/hash, approval/change-set, approved facts, and activated facts. `CountryPolicyApprovalFact` contains only approval ID/type/change-set/decider/time. `CountryPolicyChangeSnapshot` contains base/current/candidate and `base_is_current`.

`CountryPolicyFieldSourceInput` accepts only `WEB_PAGE`, `UPLOAD`, or `EMPLOYEE_INPUT`; its extra-forbid boundary rejects client-supplied extractor/confirmer identities and times. Source IDs match `[A-Za-z][A-Za-z0-9._:-]{0,199}`. A web source requires an HTTPS URL of at most 2048 characters with no userinfo plus a 64-character lowercase hexadecimal page hash; upload and employee input forbid both webpage-only fields. The decision model validator requires both version/hash or neither, requires no active facts when `configured=False`, and forbids `allowed=True` when unconfigured. `models.py` owns frozen `CountryPolicyVersion` and `CountryPolicyActivation` with `content_hash_for`, `from_command`, `to_view`, and `change_set_ref == f"country_policy:{version_id}:{content_hash}"`. `repository.py` includes `versions`, `provenance`, `activations`, and `bus: EventBus` on the UoW. Add the exact Chinese approval label `国家政策包变更`, seven-day validity, and the two public country-policy apply-failure codes to the approval service maps/allowlist.

Update `domains/AGENTS.md` from sixteen to seventeen domains and register compliance as a deep domain; register the new domain in `docs/architecture/02-boundaries.md`. Update `domains/approvals/AGENTS.md` so its central must-approve registry explicitly includes country-policy changes and preserves the independent-approver rule.

The Phase 1 authorizer matrix is exact:

```text
boss/TENANT  → COUNTRY_POLICY_READ, COUNTRY_POLICY_PROPOSE
system/SYSTEM → COUNTRY_POLICY_DECIDE, COUNTRY_POLICY_CHANGE_SNAPSHOT_READ,
                COUNTRY_POLICY_ACTIVATE
```

Actor scope tenant, configured authorizer tenant, and method tenant must all match. Request code has no convenience constructor for a system actor.

- [ ] **Step 4: Run GREEN, types, and boundary checks**

Run:

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_country_policy_contracts.py tests/unit/test_approval_service.py
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  mypy domains/compliance shared domains/approvals
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 scripts/check_boundaries.py
```

Expected: all selected tests pass, mypy reports no issues, boundaries pass with the new domain documented by its local `AGENTS.md`.

- [ ] **Step 5: Commit Task 1**

```bash
git add docs/adr/0010-country-policy-version-approval-contract.md domains/compliance \
  domains/AGENTS.md domains/approvals/AGENTS.md docs/architecture/02-boundaries.md \
  shared/schemas/identifiers.py shared/events/catalog.py shared/events/registry.py \
  domains/approvals/models.py domains/approvals/service_impl.py \
  tests/unit/test_country_policy_contracts.py tests/unit/test_approval_service.py
git commit -m "feat: define country policy contracts"
```

---

### Task 2: Persist immutable policy versions, field provenance, activations, and outbox

**Files:**
- Create: `migrations/versions/0032_country_policy_packages.py`
- Create: `infra/db/repositories/compliance.py`
- Create: `infra/db/compliance_uow.py`
- Modify: `infra/db/tables.py`
- Create: `tests/integration/test_country_policy_repository.py`
- Modify: `tests/integration/test_migrations.py`
- Modify: `tests/unit/test_work_intake_migration_head.py`

**Interfaces:**
- Consumes Task 1's models and repository Protocols.
- Produces `CountryPolicyVersionRepositoryImpl`, `CountryPolicyFieldProvenanceRepositoryImpl`, `CountryPolicyActivationRepositoryImpl`, and `SqlAlchemyComplianceUnitOfWork`.
- `next_version_number(tenant_id, country_key)` and activation sequencing share a `compliance-country-policy:{tenant}:{country_key}` PostgreSQL transaction advisory lock.

- [ ] **Step 1: Write failing repository and migration tests**

Test a real PostgreSQL container and migrated schema. Assert exact tables, tenant composite keys, and these observable behaviors:

| Test | Literal observable assertion |
|---|---|
| `test_policy_round_trip_keeps_field_provenance_separate` | nine provenance rows return with the exact field names and synthetic source IDs supplied; no provenance JSON column exists on the version row |
| `test_same_country_versions_increment_inside_tenant` | two committed versions for `synthetic market` have version numbers `1` and `2` |
| `test_other_country_and_other_tenant_have_independent_sequence` | a second country and second tenant each start at version `1` |
| `test_same_idempotency_key_different_tenant_is_allowed` | both tenant rows commit and retain their own tenant IDs |
| `test_repository_rejects_cross_tenant_reads` | a repository bound to tenant A raises `TenantIsolationViolation` before executing a tenant-B read |
| `test_activation_current_is_country_scoped` | current activation for country A never returns country B's later activation |
| `test_policy_facts_reject_update_and_delete` | direct SQL UPDATE and DELETE each raise integrity errors for all three append-only tables |
| `test_version_and_proposal_event_commit_or_rollback_together` | success commits one version and one matching outbox row; injected publish failure leaves both counts at zero |

Update migration-head assertions from `0031` to `0032` and keep the single-head assertion.

- [ ] **Step 2: Run RED and verify missing migration/repository**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/integration/test_country_policy_repository.py \
  tests/integration/test_migrations.py tests/unit/test_work_intake_migration_head.py
```

Expected: missing `0032`/repository symbols or schema assertions fail.

- [ ] **Step 3: Implement migration, rows, repositories, and UoW**

`0032` revises `0031` and creates:

```text
country_policy_versions
  PK (tenant_id, country_policy_version_id)
  UNIQUE (tenant_id, country_key, version_number)
  UNIQUE (tenant_id, idempotency_key)

country_policy_field_provenance
  PK (tenant_id, country_policy_version_id, field_name)
  FK (tenant_id, country_policy_version_id) RESTRICT

country_policy_activations
  PK (tenant_id, country_policy_activation_id)
  UNIQUE (tenant_id, country_key, activation_sequence)
  UNIQUE (tenant_id, country_policy_version_id)
  UNIQUE (tenant_id, approval_id)
  FK (tenant_id, country_policy_version_id) RESTRICT
```

The versions row contains strict boolean columns, nullable bounded opt-out days, deterministic JSONB requirements array, notes, base version/hash pair, content hash, proposer/time, and idempotency key. Field provenance uses relational columns `field_name`, `source_type`, `source_id`, `extracted_by`, `extracted_at`, `confirmed_by`, `confirmed_at`, `source_url`, `page_hash`; database checks require `confirmed_by/at` and reject `agent_inference`.

Activations store country key, monotonic sequence, exact version/hash, approval ID/change-set, approved and activated facts. Triggers reject UPDATE/DELETE on all three tables. UoW creates `PostgresEventBus` with the same session so a proposal event rolls back with the candidate.

- [ ] **Step 4: Run GREEN and inspect Alembic head**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/integration/test_country_policy_repository.py \
  tests/integration/test_migrations.py tests/unit/test_work_intake_migration_head.py
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 alembic heads
```

Expected: selected tests pass and exactly `0032 (head)` is printed.

- [ ] **Step 5: Commit Task 2**

```bash
git add migrations/versions/0032_country_policy_packages.py infra/db/tables.py \
  infra/db/repositories/compliance.py infra/db/compliance_uow.py \
  tests/integration/test_country_policy_repository.py tests/integration/test_migrations.py \
  tests/unit/test_work_intake_migration_head.py
git commit -m "feat: persist immutable country policies"
```

---

### Task 3: Implement proposal, read, decision, and coverage services

**Files:**
- Create: `domains/compliance/service_impl.py`
- Create: `tests/unit/test_country_policy_service.py`
- Create: `tests/integration/test_country_policy_service_postgres.py`

**Interfaces:**
- Consumes Task 2 UoW and Task 1 authorization/contracts.
- Produces a concrete `ComplianceServiceImpl` with `get_active_policy`, `get_version`, `list_active_policies`, `list_versions`, `get_country_policy_decision`, `get_coverage`, `get_change_snapshot`, and `propose_country_policy`.
- Proposal atomically publishes `CountryPolicyVersionProposed`; Task 5 consumes it.

- [ ] **Step 1: Write failing service tests**

Use real service objects with fake UoWs for branch behavior and PostgreSQL for transaction behavior. Cover these exact tests:

| Test | Literal observable assertion |
|---|---|
| `test_unknown_country_returns_configured_false_allowed_false` | decision has normalized requested key, requested action, both booleans false, no version/hash, and empty requirements |
| `test_explicit_denial_is_distinct_from_unknown_country` | active denied policy returns `configured=True`, `allowed=False`, and exact version/hash |
| `test_allowed_action_returns_exact_active_version_and_requirements` | allowed decision returns the current version ID/hash and the candidate's sorted requirement tuple |
| `test_action_maps_only_to_its_explicit_boolean` | a three-row literal matrix flips only the selected action field and observes only that action change |
| `test_storage_failure_propagates_instead_of_returning_denial` | injected repository exception escapes as a safe service failure; no unconfigured decision is returned |
| `test_boss_can_read_and_propose_but_not_decide_or_activate` | read/propose succeed and the three system actions raise `PermissionDenied` |
| `test_system_can_decide_but_cannot_propose` | decision read succeeds and proposal raises `PermissionDenied` |
| `test_proposal_is_idempotent_for_same_normalized_content` | two calls return the same version ID/number/hash and repository add/publish counts remain one |
| `test_idempotency_key_with_different_content_conflicts` | changing one literal boolean under the same key raises `CountryPolicyIdempotencyConflictError` |
| `test_first_proposal_has_no_base_and_revision_captures_current_base` | first result's stored base pair is null; revision base equals the active version ID/hash |
| `test_proposal_and_event_are_atomic_in_postgres` | committed proposal has one matching outbox event; forced publish failure leaves neither row |
| `test_coverage_counts_active_and_enrichment_allowed_countries` | two active countries with exactly one enrichment allow return counts `2` and `1` |
| `test_service_binds_provenance_to_authenticated_actor_and_server_time` | every stored field has `human:emp_proposer`, `confirmed_by=emp_proposer`, and both times equal injected UTC now regardless of rejected client extras |

The action-mapping test mutates one boolean at a time so a wrong-field lookup fails.

- [ ] **Step 2: Run RED and confirm service implementation is absent**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_country_policy_service.py \
  tests/integration/test_country_policy_service_postgres.py
```

Expected: import failure for `ComplianceServiceImpl` or behavior failures from unimplemented methods.

- [ ] **Step 3: Implement minimal service behavior**

Use these exact signatures:

```python
async def get_country_policy_decision(
    self, tenant_id: TenantId, country: str, action: CountryPolicyAction,
    *, actor: ComplianceActor,
) -> CountryPolicyDecision:
    raise NotImplementedError

async def get_coverage(
    self, tenant_id: TenantId, *, actor: ComplianceActor,
) -> CountryPolicyCoverage:
    raise NotImplementedError

async def propose_country_policy(
    self, tenant_id: TenantId, command: CountryPolicyProposalCreate,
    *, actor: ComplianceActor, idempotency_key: IdempotencyKey,
) -> CountryPolicyProposalResult:
    raise NotImplementedError
```

Validate idempotency with `[A-Za-z0-9][A-Za-z0-9._:-]{0,199}`. Compute the requested hash from normalized policy content plus safe source inputs before UoW entry; server-derived identity/time fields do not enter the hash. Double-check idempotency after the country lock, allocate a per-country version number, derive base from that country's current activation, convert every source into Provenance bound to `human:<actor_id>`, `confirmed_by=actor_id`, and the server `proposed_at`, then publish one event with the exact version/hash/country/proposer before transaction exit.

No active version returns a well-formed unconfigured decision; a missing version referenced by an activation raises `TransientError`. Invalid limit, country, action, repository return type, or mismatched tenant fails explicitly.

- [ ] **Step 4: Run GREEN and focused boundary checks**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_country_policy_service.py \
  tests/integration/test_country_policy_service_postgres.py
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  mypy domains/compliance infra/db/compliance_uow.py infra/db/repositories/compliance.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 scripts/check_boundaries.py
```

- [ ] **Step 5: Commit Task 3**

```bash
git add domains/compliance/service_impl.py tests/unit/test_country_policy_service.py \
  tests/integration/test_country_policy_service_postgres.py
git commit -m "feat: enforce country policy decisions"
```

---

### Task 4: Enforce exact approval-backed activation

**Files:**
- Modify: `domains/compliance/service_impl.py`
- Modify: `domains/compliance/errors.py`
- Create: `tests/unit/test_country_policy_activation.py`
- Modify: `tests/integration/test_country_policy_service_postgres.py`

**Interfaces:**
- Adds `activate_country_policy(tenant_id, version_id, approval, *, actor) -> CountryPolicyActivationView`.
- Emits stable domain errors `CountryPolicyBaseVersionConflictError`, `CountryPolicyApprovalFactInvalidError`, and `CountryPolicyActivationConflictError` for Task 5 mapping.

- [ ] **Step 1: Write failing activation tests**

| Test | Literal observable assertion |
|---|---|
| `test_activation_requires_system_actor` | boss actor receives `PermissionDenied`; system actor reaches the candidate read |
| `test_activation_requires_exact_approval_type_change_set_and_hash` | a table mutating type, change-set version, and hash raises `CountryPolicyApprovalFactInvalidError` for every row |
| `test_activation_rejects_future_or_naive_decided_time` | naive time and injected-now-plus-one-second each raise the approval-fact error |
| `test_first_activation_requires_country_to_still_have_no_current_version` | a no-base candidate fails once any current activation exists for that country |
| `test_revision_rejects_stale_country_base` | activating revision B then older sibling A makes A raise `CountryPolicyBaseVersionConflictError` |
| `test_exact_same_approval_and_version_replay_returns_existing_activation` | replay returns the same activation ID/sequence and row count remains one |
| `test_same_approval_or_version_with_different_fact_conflicts` | changed decider, decided time, approval ID, or version raises `CountryPolicyActivationConflictError` |
| `test_two_approved_revisions_cannot_both_become_current` | concurrent sibling activation yields one success and one base conflict |
| `test_activation_sequence_is_monotonic_per_country` | two sequential valid revisions have activation sequences `1` and `2` |

- [ ] **Step 2: Run RED and verify missing activation behavior**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_country_policy_activation.py \
  tests/integration/test_country_policy_service_postgres.py -k activation
```

- [ ] **Step 3: Implement exact activation gate**

Inside one tenant/country locked UoW: load candidate; find activation by approval and version; accept only exact replay; verify `approval_type == "country_policy_change"`, exact change-set, approved employee/time, approved time not after activation clock, and candidate base equals current active version/hash; allocate activation sequence; write append-only activation.

Do not import approvals-domain models. The method accepts only `CountryPolicyApprovalFact` from Task 1.

- [ ] **Step 4: Run GREEN**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_country_policy_activation.py \
  tests/integration/test_country_policy_service_postgres.py
```

- [ ] **Step 5: Commit Task 4**

```bash
git add domains/compliance/service_impl.py domains/compliance/errors.py \
  tests/unit/test_country_policy_activation.py \
  tests/integration/test_country_policy_service_postgres.py
git commit -m "feat: gate country policy activation"
```

---

### Task 5: Build durable country-policy approval workflow and crash recovery

**Files:**
- Create: `workflows/country_policy_change/AGENTS.md`
- Create: `workflows/country_policy_change/__init__.py`
- Create: `workflows/country_policy_change/flow.py`
- Create: `workflows/country_policy_change/steps.py`
- Modify: `workflows/AGENTS.md`
- Modify: `apps/scheduler_worker/runtime.py`
- Modify: `apps/api/composition/runtime.py`
- Create: `tests/unit/test_country_policy_change_workflow.py`
- Create: `tests/integration/test_country_policy_change_postgres.py`
- Modify: `tests/integration/test_outbox_delivery.py`

**Interfaces:**
- Produces workflow type `country_policy_change`, version `1`, and handlers `assemble`, `submit`, `wait`, `expire`, `apply`, `mark_applied`.
- Produces `CountryPolicyVersionProposedHandler`, which calls `WorkflowEngine.start` with the same deterministic idempotency key used by the API in Task 7.
- Registers `ApprovalDecidedHandler` only for `country_policy_change` approvals.

- [ ] **Step 1: Write failing workflow tests**

Assert this exact definition:

```text
assemble_package → submit_approval → wait_decision
wait_decision approved → apply_policy → mark_applied → complete
wait_decision rejected → complete
wait timeout → expire_approval; approved race → apply_policy; expired → complete
```

Cover this exact matrix:

| Test | Literal observable assertion |
|---|---|
| `test_package_contains_before_after_field_diff_and_safe_provenance_refs` | approval display has literal before/after values and safe source IDs, and contains no source page body |
| `test_submit_uses_country_policy_change_and_proposer_as_owner` | captured submit call has type `COUNTRY_POLICY_CHANGE`, proposer and owner equal candidate proposer, and exact change-set |
| `test_approval_event_routes_only_exact_change_set_to_exact_active_run` | wrong type returns without delivery; wrong change-set raises; exact event advances only the matching version run |
| `test_apply_maps_stale_base_to_country_policy_base_version_conflict` | workflow completes with `apply_failed` and the exact stable code |
| `test_apply_maps_invalid_fact_to_country_policy_approval_fact_invalid` | invalid-fact and activation-conflict branches both mark the exact stable code |
| `test_duplicate_proposal_event_starts_one_run` | two deliveries return one run ID and one workflow row |
| `test_api_start_then_outbox_delivery_reuses_same_run` | direct start and later event handler return the same run under the deterministic key |
| `test_crash_after_proposal_commit_is_recovered_by_outbox_start` | with no direct start, outbox delivery creates one runnable workflow from the committed candidate event |
| `test_duplicate_approval_event_does_not_activate_twice` | repeated event leaves one activation and approval state `applied` |

- [ ] **Step 2: Run RED**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_country_policy_change_workflow.py \
  tests/integration/test_country_policy_change_postgres.py \
  tests/integration/test_outbox_delivery.py -k country_policy
```

- [ ] **Step 3: Implement workflow and both composition roots**

The workflow context contains only safe values:

```python
{
    "country_policy_version_id": str(version_id),
    "country_key": country_key,
    "content_hash": content_hash,
    "change_set_ref": f"country_policy:{version_id}:{content_hash}",
    "proposed_by": str(proposed_by),
}
```

Both API direct start and proposal-event recovery use:

```python
idempotency_key = f"country-policy-change:{tenant_id}:{version_id}"
```

The API direct start provides immediate run ID in the normal path; the durable proposal event repairs the commit/start crash window and reuses the same run when the normal path already succeeded.

Use a seven-day wait timeout. Approval package `proposed_change` has literal `before`/`after` policy displays and per-field safe source IDs; it never contains raw web content. Scheduler registers full handlers and both proposal/approval event handlers. API registers the definition with start-only handlers so HTTP can start runs but never apply changes.

Update `workflows/AGENTS.md` in the same task so the workflow inventory includes the already-delivered configuration workflows and the new `country_policy_change` workflow; do not leave the local rule entry claiming a stale workflow count.

- [ ] **Step 4: Run GREEN, scheduler tests, and types**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_country_policy_change_workflow.py \
  tests/integration/test_country_policy_change_postgres.py \
  tests/integration/test_outbox_delivery.py -k country_policy
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/integration/test_scheduler_worker.py
```

- [ ] **Step 5: Commit Task 5**

```bash
git add workflows/country_policy_change workflows/AGENTS.md apps/scheduler_worker/runtime.py \
  apps/api/composition/runtime.py tests/unit/test_country_policy_change_workflow.py \
  tests/integration/test_country_policy_change_postgres.py \
  tests/integration/test_outbox_delivery.py
git commit -m "feat: apply approved country policies"
```

---

### Task 6: Replace policy allowlists with structured fail-closed decisions

**Files:**
- Modify: `tool_gateway/checks/contact_provider.py`
- Modify: `tool_gateway/checks/web_discovery.py`
- Delete: `tool_gateway/checks/country_policy.py`
- Modify: `tool_gateway/AGENTS.md`
- Modify: `apps/scheduler_worker/account_discovery.py`
- Modify: `apps/scheduler_worker/hunter_contacts.py`
- Modify: `apps/scheduler_worker/web_discovery.py`
- Modify: `apps/scheduler_worker/runtime.py`
- Modify: `tests/unit/test_contact_provider_gateway_support.py`
- Create: `tests/unit/test_country_policy_web_gateway.py`
- Create: `tests/unit/test_country_policy_runtime_composition.py`

**Interfaces:**
- Replaces boolean country-policy reader protocols with `decision(tenant_id, country, action) -> CountryPolicyDecision` adapters backed by `ComplianceService` and an explicit system `ComplianceActor`.
- Removes `ConfiguredContactCountryPolicy`; no environment/deployment allowlist remains.
- Keeps production `contact.enrich` absent from every runtime `ToolRegistry`.

- [ ] **Step 1: Write failing Gateway decision tests**

For both contact enrichment and public research assert:

| Test | Literal observable assertion |
|---|---|
| `test_unknown_country_rejects_not_configured_before_handler` | rejection stage is `country_policy`, reason is `country_policy:not_configured`, handler and transport calls are zero |
| `test_configured_denial_rejects_action_not_allowed_before_handler` | reason is `country_policy:action_not_allowed`, handler and transport calls are zero |
| `test_allowed_decision_with_exact_version_and_hash_continues` | the fake receives the exact normalized country/action; a valid allowed decision carrying the active version ID and content hash returns no rejection |
| `test_mismatched_country_or_action_in_decision_is_transient_failure` | either mismatch raises `ToolGatewayError(PROVIDER_TRANSIENT)` and does not call transport |
| `test_reader_exception_is_transient_failure_not_denial` | safe transient category is raised; no policy denial is fabricated |
| `test_rejected_path_calls_provider_transport_zero_times` | a counter fake remains exactly zero for unknown and denied table rows |

Add a real production-composition behavior test: build the configured runtime dependencies, invoke `contact.enrich`, and assert the registry rejects it as an unregistered tool before any handler or transport runs. Build the worker's policy adapter with a real `ComplianceService` fake and assert its structured decision reaches the Gateway check; do not assert on source text or constructor signatures.

- [ ] **Step 2: Run RED**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_contact_provider_gateway_support.py \
  tests/unit/test_country_policy_web_gateway.py \
  tests/unit/test_country_policy_runtime_composition.py
```

- [ ] **Step 3: Implement structured readers and rejection mapping**

Contact enrichment requests `CountryPolicyAction.CONTACT_ENRICHMENT`; public research requests `PUBLIC_RESEARCH`. Validate returned tenant-independent fields through the requested country key, exact action, configured/allowed pair, version/hash pair, and decision model type.

Map only valid decisions:

```text
configured=false → country_policy:not_configured
configured=true, allowed=false → country_policy:action_not_allowed
configured=true, allowed=true → continue
exception or malformed result → ToolErrorCategory.PROVIDER_TRANSIENT
```

Delete the unused `tool_gateway/checks/country_policy.py` skeleton; the two concrete checks remain in `contact_provider.py` and `web_discovery.py` and share the domain decision contract without duplicating legal rules.

Update `tool_gateway/AGENTS.md` in the same task: remove the deleted generic-file entry, document that the two capability-specific checks consume only the compliance public decision contract, and keep the production-registration limitation explicit.

- [ ] **Step 4: Run GREEN and verify no production registration**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_contact_provider_gateway_support.py \
  tests/unit/test_country_policy_web_gateway.py \
  tests/unit/test_country_policy_runtime_composition.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 scripts/check_boundaries.py
```

The runtime behavior test must report `contact.enrich` as unregistered and record zero Provider calls.

- [ ] **Step 5: Commit Task 6**

```bash
git add tool_gateway/checks/contact_provider.py tool_gateway/checks/web_discovery.py \
  tool_gateway/checks/country_policy.py tool_gateway/AGENTS.md \
  apps/scheduler_worker/account_discovery.py \
  apps/scheduler_worker/hunter_contacts.py apps/scheduler_worker/web_discovery.py \
  apps/scheduler_worker/runtime.py \
  tests/unit/test_contact_provider_gateway_support.py \
  tests/unit/test_country_policy_web_gateway.py \
  tests/unit/test_country_policy_runtime_composition.py
git commit -m "feat: enforce persisted country policies"
```

---

### Task 7: Expose boss-only policy Settings API and truthful readiness

**Files:**
- Modify: `apps/api/dependencies.py`
- Modify: `apps/api/composition/runtime.py`
- Modify: `apps/api/routers/settings.py`
- Modify: `tests/unit/test_settings_router.py`
- Create: `tests/integration/test_country_policy_settings_api.py`

**Interfaces:**
- Adds `compliance: ComplianceService | None` and fail-closed `contact_enrichment_composed: bool = False` to `ConfiguredApiDependencies`; production also passes `False` explicitly in this plan. The safe default preserves unrelated test builders and cannot create a false-ready state.
- Adds `GET /settings/country-policies`, `GET /settings/country-policies/versions`, and `POST /settings/country-policies/proposals`.
- POST returns `CountryPolicyProposalAccepted(country_policy_version_id, run_id, change_set_ref)`.

- [ ] **Step 1: Write failing router and real-API tests**

Cover:

| Test | Literal observable assertion |
|---|---|
| `test_only_active_boss_can_read_or_propose_country_policy` | active boss receives 200/202; manager, employee, inactive boss, and other tenant receive the established permission response |
| `test_request_body_cannot_supply_tenant_actor_or_activation` | each forbidden extra field returns 422 and service call count stays zero |
| `test_proposal_requires_idempotency_header_and_starts_exact_workflow` | missing header is 422; valid request captures exact type, subject, context, and deterministic key and returns its run ID |
| `test_versions_are_country_scoped_and_join_approval_state` | only requested normalized country versions appear and matching approvals map to the six allowed states |
| `test_readiness_none_active_is_country_policy_not_configured` | response state is blocked with exact reason and zero counts |
| `test_readiness_active_but_all_denied_is_contact_enrichment_not_allowed` | response keeps active count and exact denial reason |
| `test_readiness_allowed_but_not_composed_is_contact_enrichment_not_composed` | response shows allowed count and the exact not-composed reason, never ready |
| `test_router_has_no_country_policy_activate_update_or_delete_route` | OpenAPI path/method set equals the three approved routes and methods |
| `test_cross_tenant_version_is_not_visible` | tenant-A identity cannot retrieve tenant-B version through list or status composition |

- [ ] **Step 2: Run RED**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_settings_router.py \
  tests/integration/test_country_policy_settings_api.py
```

- [ ] **Step 3: Implement API mapping and dependency composition**

`GET /settings/country-policies` returns active policies, coverage counts, and exactly one contact-enrichment state/reason. `GET .../versions` accepts `country` plus `limit=1..200`, normalizes through the domain service, and maps generic approval views only when approval type/change-set match the version. POST takes `CountryPolicyProposalCreate`, trusted boss actor, and `Idempotency-Key`, calls proposal, then starts the workflow with the Task 5 deterministic key and exact safe context.

Update the existing Playbook overview to consume this readiness result instead of constructing `ContactEnrichmentBlocker()` unconditionally. A missing compliance service is a transient dependency failure, not “policy not configured”. Validate `contact_enrichment_composed` as a strict bool; production composition passes `False` explicitly.

- [ ] **Step 4: Run GREEN and validate the exported OpenAPI paths**

```bash
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/unit/test_settings_router.py \
  tests/integration/test_country_policy_settings_api.py tests/unit/test_api_app.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 \
  apps/web/scripts/export_openapi.py > /tmp/tradeos-country-policy-openapi.json
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 - <<'PY'
import json
from pathlib import Path
schema = json.loads(Path('/tmp/tradeos-country-policy-openapi.json').read_text())
paths = schema['paths']
assert '/settings/country-policies' in paths
assert '/settings/country-policies/versions' in paths
assert '/settings/country-policies/proposals' in paths
PY
```

Do not create or commit an `openapi.json`; this repository pipes the transient schema directly into `openapi-typescript` in Task 8.

- [ ] **Step 5: Commit Task 7**

```bash
git add apps/api/dependencies.py apps/api/composition/runtime.py \
  apps/api/routers/settings.py tests/unit/test_settings_router.py \
  tests/integration/test_country_policy_settings_api.py
git commit -m "feat: expose country policy settings api"
```

---

### Task 8: Build the Settings country-policy workspace

**Files:**
- Modify: `apps/web/src/views/settings/SettingsCenter.vue`
- Modify: `apps/web/tests/settings.test.ts`
- Regenerate: `apps/web/src/api/api.d.ts`

**Interfaces:**
- Consumes Task 7 API only; no handwritten duplicate response types.
- Produces empty state, active-country list, proposal/revision form, per-field safe-source inputs, server-bound Provenance display, diff, history, approval/application state, and readiness banner.

- [ ] **Step 1: Write failing Vitest interaction tests**

Add MSW/fetch fixtures that mirror the complete OpenAPI response. Cover:

| Vitest case | Literal observable assertion |
|---|---|
| no legal defaults | empty state contains “系统不提供国家法律默认值” and an authorized-entry instruction |
| configured versus allowed | active count and allowed-enrichment count render as separate values |
| every field needs a safe source | remove one source, submit stays disabled/invalid, POST count remains zero |
| exact proposal request | captured JSON has strict booleans and nine `field_sources`; one `Idempotency-Key` is reused during the in-flight attempt |
| revision base | selecting revise copies the active version values and displays its base version/hash |
| diff and approval | literal before/after field values and pending/applied labels render from complete fixtures |
| policy missing readiness | `COUNTRY_POLICY_NOT_CONFIGURED` maps to the dedicated Chinese explanation |
| all denied readiness | `CONTACT_ENRICHMENT_NOT_ALLOWED` maps to a different explanation |
| composition missing readiness | `CONTACT_ENRICHMENT_NOT_COMPOSED` maps to the Hunter-composition explanation |
| prohibited controls absent | role/name queries find no delete, direct activate, or legal-template controls |

The test that removes one provenance input must fail submission and make zero POST calls; the successful test asserts the literal request body and `Idempotency-Key` header.

- [ ] **Step 2: Run RED**

```bash
cd apps/web
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
  node_modules/vitest/vitest.mjs run tests/settings.test.ts
```

- [ ] **Step 3: Regenerate API types and implement minimal UI**

Run the exact generation command:

```bash
cd apps/web
export PATH="/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH"
npm run gen:api
```

Extend the existing Settings page rather than creating a second settings shell. Internal copy is Chinese; field identifiers stay English. Reuse existing components/styles, show sources as safe IDs, and ensure proposal request state generates one idempotency key per submission attempt and resets it only after a terminal response.

No control may imply that the system supplies legal advice. The empty-state copy must say that an authorized person must enter and confirm verified policy facts.

- [ ] **Step 4: Run GREEN, full frontend tests, typecheck, build, and lint**

```bash
cd apps/web
export PATH="/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH"
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
  node_modules/vitest/vitest.mjs run tests/settings.test.ts
npm test
npm run typecheck
npm run build
npm run lint
```

Report existing lint warnings separately; new Settings files must add zero errors and zero warnings.

- [ ] **Step 5: Commit Task 8**

```bash
git add apps/web/src/views/settings/SettingsCenter.vue apps/web/tests/settings.test.ts \
  apps/web/src/api/api.d.ts
git commit -m "feat: manage country policies in settings"
```

---

### Task 9: Prove the real stack, update truthful docs, and run full acceptance

**Files:**
- Create: `tests/e2e/test_country_policy_settings.py`
- Modify: `HANDBOOK.md`
- Modify: `docs/architecture/04-tool-gateway.md`
- Modify: `docs/architecture/08-compliance.md`

**Interfaces:**
- Proves real PostgreSQL migration + API + scheduler workflow + Vite + Chromium behavior.
- Leaves production `contact.enrich` unregistered and documents the next slice as controlled Hunter composition plus operational acceptance.

- [ ] **Step 1: Write the failing real-stack E2E**

The test launches a migrated PostgreSQL container, API, scheduler/outbox delivery loop, Vite, and Chromium. It must:

```text
1. Sign in as a synthetic active boss.
2. Observe COUNTRY_POLICY_NOT_CONFIGURED and the no-default explanation.
3. Submit one synthetic country candidate with every required safe field source.
4. Observe candidate and pending approval history.
5. Approve as a different synthetic manager through the existing approval boundary.
6. Deliver outbox/workflow until applied.
7. Refresh and observe the active version and CONTACT_ENRICHMENT_NOT_COMPOSED.
8. Check desktop and 390×844 mobile viewports for no horizontal overflow.
9. Assert no browser console errors and no production Hunter request.
```

- [ ] **Step 2: Run RED and verify the missing UI/runtime behavior**

```bash
export PATH="/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:$PATH"
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/e2e/test_country_policy_settings.py
```

- [ ] **Step 3: Complete E2E wiring and update documentation**

Fix only integration gaps revealed by the E2E. Update documentation with these exact facts:

```text
Implemented: immutable tenant-scoped country policy versions, field-level
human-confirmed provenance, independent approval, activation, Settings management,
structured fail-closed Gateway readers, and truthful readiness.

Still blocked: production contact.enrich registration, real Hunter credential/transport
composition, provider operational verification, and Phase 1 operational acceptance.
```

Verify the domain count/list and boundary map updated in Task 1 remain truthful. Remove stale claims that country-policy persistence is absent, but preserve every statement that production enrichment remains unavailable.

- [ ] **Step 4: Run focused E2E GREEN**

```bash
export PATH="/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:$PATH"
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 \
  pytest -q tests/e2e/test_country_policy_settings.py
```

- [ ] **Step 5: Run complete repository acceptance**

```bash
export PATH="/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin:/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH"
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 make check
cd apps/web
npm test
npm run typecheck
npm run build
npm run lint
cd ../..
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 scripts/check_boundaries.py
/Users/xueziheng/miniconda3/bin/conda run --no-capture-output -n tradeos-py312 alembic heads
git diff --check
git status --short
```

Record exact pass/skip/warning counts. The Alembic output must have one head. Tracked AppleDouble `._*` files must be zero. Do not claim Phase 1 complete.

- [ ] **Step 6: Commit Task 9**

```bash
git add tests/e2e/test_country_policy_settings.py HANDBOOK.md \
  docs/architecture/04-tool-gateway.md docs/architecture/08-compliance.md
git commit -m "docs: record country policy readiness boundary"
```

---

## Completion Gate

Before invoking `superpowers:finishing-a-development-branch`, verify every item from the approved spec:

- ADR 0010 precedes shared ID changes.
- No legal default, alias inference, environment allowlist, or production `contact.enrich` registration exists.
- All decision fields have separate human-confirmed, non-agent Provenance.
- Version, provenance, activation, approval, and workflow facts are tenant-scoped and append-only.
- Proposal + outbox are atomic; direct API start + outbox recovery converge on one run.
- Unknown, denied, malformed, and unavailable policy sources all fail closed before Provider IO.
- Settings exposes proposal/history/readiness without activate/update/delete/template controls.
- Full backend, frontend, browser, boundary, migration-head, and build evidence is fresh and green.
- Documentation says policy persistence/readiness is implemented while production composition and Phase 1 operational acceptance remain blocked.
