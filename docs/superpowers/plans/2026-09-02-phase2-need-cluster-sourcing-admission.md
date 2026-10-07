# Phase 2 NeedCluster Sourcing Admission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a tenant-safe, durable sourcing admission queue that starts existing Sourcing Case V2 runs in deterministic Need Cluster order only under an active boss-confirmed policy.

**Architecture:** Need readiness events continue to create one canonical Sourcing Case per Validated Need, but now enqueue an admission with an immutable priority snapshot instead of starting a workflow immediately. Demand owns cluster membership facts, sourcing owns admission state and snapshots, and the scheduler application layer consumes a confirmed Directive policy before atomically claiming ordered admissions and idempotently starting the existing V2 workflow.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x, PostgreSQL/Alembic, Vue 3, TypeScript, Vite, Ant Design Vue, pytest, Vitest.

**Spec:** `docs/superpowers/specs/2026-09-02-phase2-need-cluster-sourcing-admission-design.md`

## Global Constraints

- Preserve `Demand Signal → Need Hypothesis → Validated Need → Trade Opportunity`; cluster facts never become customer-confirmed demand, supply, or price facts.
- Keep one `Validated Need` and one immutable `need_snapshot` per Sourcing Case; never merge quantities, units, countries, specifications, or Provenance across cluster members.
- Rank only by `cluster_member_count DESC → ready_at ASC → need_id ASC`; do not add weights, model confidence, quantity, country count, profitability, or an aging threshold.
- Automatic admission requires an active boss-confirmed Directive with explicit `automatic_admission_enabled` and `batch_limit`; missing, disabled, and unknown policy states start zero workflows.
- Every table and query is tenant-bound; every external action still goes through Tool Gateway; no new provider call is introduced.
- Existing started or terminal V1/V2 cases and runs remain unchanged; old Directives without the new section mean “policy not configured.”
- Use Decimal for money, although this slice introduces no monetary arithmetic; do not add floating-point business values.
- Customer-facing text remains English; internal UI, docs, errors, and audit explanations remain Chinese.
- Add an ADR for the new event/Directive section and changed trigger behavior; do not mutate existing event fields or meanings.
- Each implementation slice starts with a failing test, ends with focused passing tests, and is committed independently.

---

### Task 1: Lock the public event and identifier contracts

**Files:**
- Create: `docs/adr/0024-need-cluster-sourcing-admission.md`
- Modify: `shared/schemas/identifiers.py`
- Modify: `shared/events/catalog.py`
- Modify: `infra/db/outbox.py`
- Modify: `domains/demand/events.py`
- Test: `tests/unit/test_outbox_serialization.py`
- Test: `tests/unit/test_sourcing_trigger_contracts.py`

**Interfaces:**
- Produces: `SourcingAdmissionId`, `SourcingPrioritySnapshotId`, and `NeedClusterMembershipChanged(tenant_id, occurred_at, run_id, cluster_id, changed_need_id, member_count)`.
- Preserves: every existing `NeedClusterFormed`, `NeedValidated`, and `NeedBecameSourcingReady` field and serialization name.

- [ ] **Step 1: Write failing identifier and event serialization tests**

Add tests that construct the new typed IDs and round-trip the exact event payload:

```python
event = NeedClusterMembershipChanged(
    tenant_id=TenantId("tn_0" + "A" * 25),
    occurred_at=NOW,
    cluster_id=NeedClusterId("ncl_0" + "B" * 25),
    changed_need_id=ValidatedNeedId("vnd_0" + "C" * 25),
    member_count=2,
)
payload = serialize_event(event)
assert payload["event_type"] == "NeedClusterMembershipChanged"
assert deserialize_event(payload) == event
```

Also assert that `member_count=0`, a blank cluster ID, or a blank changed Need ID is rejected at the consumer/domain validation boundary rather than being accepted as a useful fact.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_outbox_serialization.py tests/unit/test_sourcing_trigger_contracts.py -q
```

Expected: collection/import failure because the IDs and event do not exist.

- [ ] **Step 3: Add the minimal contracts and ADR**

Define the IDs with the repository’s existing `NewType` pattern and the event as:

```python
@dataclass(frozen=True)
class NeedClusterMembershipChanged(DomainEvent):
    """一条已验证需求已归入需求簇；成员数是变更后的累计事实。"""

    cluster_id: NeedClusterId | None = None
    changed_need_id: ValidatedNeedId | None = None
    member_count: int = 0
```

Register the event in outbox serialization/deserialization and re-export it from `domains/demand/events.py`. The ADR must record: new event rather than changing `NeedClusterFormed`; Directives remain the policy source; readiness triggers enqueue rather than start; old runs are untouched; catalog proposal is excluded.

- [ ] **Step 4: Run focused tests and boundary checks**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_outbox_serialization.py tests/unit/test_sourcing_trigger_contracts.py -q
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH python3 scripts/check_boundaries.py
```

Expected: PASS, including existing event round trips.

- [ ] **Step 5: Commit**

```bash
git add docs/adr/0024-need-cluster-sourcing-admission.md shared/schemas/identifiers.py shared/events/catalog.py infra/db/outbox.py domains/demand/events.py tests/unit/test_outbox_serialization.py tests/unit/test_sourcing_trigger_contracts.py
git commit -m "feat: add need cluster membership event"
```

---

### Task 2: Expose verified Need Cluster priority facts

**Files:**
- Modify: `domains/demand/schemas.py`
- Modify: `domains/demand/service.py`
- Modify: `domains/demand/service_impl.py`
- Modify: `domains/demand/AGENTS.md`
- Create: `tests/unit/test_demand_service.py`
- Test: `tests/integration/test_demand_sourcing_ready_event.py`

**Interfaces:**
- Consumes: `NeedClusterMembershipChanged` from Task 1.
- Produces: immutable `NeedClusterPriorityFacts` and `DemandService.get_cluster_priority_facts(tenant_id, need_id)`.
- Publishes: one membership-changed event whenever `try_assign_cluster` first assigns a Need, including a one-member new cluster.

- [ ] **Step 1: Write failing service tests**

Add unit tests for unclustered and clustered facts:

```python
facts = await service.get_cluster_priority_facts(TENANT, NEED_ID)
assert facts == NeedClusterPriorityFacts(
    need_id=str(NEED_ID),
    cluster_id=str(CLUSTER_ID),
    cluster_member_count=3,
    facts_observed_at=NOW,
)
```

Cover: tenant mismatch before repository IO, missing cluster member rejected, a Need pointing to a cluster that does not point back rejected, and an unclustered Validated Need returning `(None, 1)`. Also prove that a completeness-2 Validated Need can supply membership facts for refreshing other waiting admissions; the admission trigger separately enforces completeness 3.

Update the integration test so first assignment publishes `member_count=1`, second assignment publishes `member_count=2`, duplicate assignment publishes no new membership event, and `NeedClusterFormed` still occurs only when the second member forms the multi-member cluster.

- [ ] **Step 2: Run tests and verify RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_demand_service.py tests/integration/test_demand_sourcing_ready_event.py -q
```

Expected: failures for the absent DTO/method/event publication.

- [ ] **Step 3: Implement the DTO and service read**

Add:

```python
@dataclass(frozen=True)
class NeedClusterPriorityFacts:
    need_id: str
    cluster_id: str | None
    cluster_member_count: int
    facts_observed_at: datetime
```

The service method must validate tenant and Need, load the cluster only when `cluster_id` exists, reuse `_load_cluster_needs` for the bidirectional membership check, and return a UTC-aware observation time from the injected clock. Do not enforce sourcing readiness here and do not expose quantities or account fields.

- [ ] **Step 4: Publish membership changes atomically**

Inside the existing `try_assign_cluster` UoW, after the Need points to the canonical cluster, publish exactly one `NeedClusterMembershipChanged` with the post-change count. Preserve `NeedClusterFormed`’s existing second-member-only meaning and existing duplicate early return.

- [ ] **Step 5: Run focused tests**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_demand_service.py tests/integration/test_demand_sourcing_ready_event.py tests/integration/test_outbox_transaction.py -q
```

Expected: PASS with atomic events and no duplicate publication.

- [ ] **Step 6: Commit**

```bash
git add domains/demand/schemas.py domains/demand/service.py domains/demand/service_impl.py domains/demand/AGENTS.md tests/unit/test_demand_service.py tests/integration/test_demand_sourcing_ready_event.py
git commit -m "feat: expose need cluster priority facts"
```

---

### Task 3: Add boss-confirmed sourcing admission policy to Directives

**Files:**
- Modify: `domains/directives/models.py`
- Modify: `domains/directives/schemas.py`
- Modify: `domains/directives/service.py`
- Modify: `domains/directives/service_impl.py`
- Modify: `infra/db/repositories/directives.py`
- Create: `migrations/versions/0052_sourcing_admission_directive.py`
- Modify: `domains/directives/AGENTS.md`
- Create: `tests/unit/test_directives_service.py`
- Create: `tests/integration/test_directives_persistence.py`
- Modify: `tests/integration/test_migrations.py`

**Interfaces:**
- Produces: `SourcingAdmissionConfig`, `SourcingAdmissionConfigInput`, `DirectiveService.submit_sourcing_admission_proposal(...)`, and safe fields on `ProposalView`/`DirectiveView`.
- Preserves: historical serialized Directive content without `sourcing_admission`.

- [ ] **Step 1: Write failing model and compatibility tests**

Use this exact public input:

```python
SourcingAdmissionConfigInput(
    mode="cluster_ranked",
    automatic_admission_enabled=True,
    batch_limit=3,
)
```

Test rejection of bool-as-int, limits `0` and `51`, unknown mode, extra fields, non-boss confirmation, and empty expected behavior changes. Persist and reload both a new Directive and an old JSON document with no sourcing section; the old view must expose all three new fields as `None`. Seed an active Directive containing market, discovery, outreach, handoff, and budget fields; the admission proposal must copy them byte-for-byte except for the new section. Activate another Directive before confirmation and assert the stale admission proposal is rejected with zero overwrite.

- [ ] **Step 2: Run tests and verify RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_directives_service.py tests/integration/test_directives_persistence.py -q
```

Expected: missing input/model/service symbols.

- [ ] **Step 3: Implement strict configuration and serialization**

Add a frozen domain value object:

```python
@dataclass(frozen=True)
class SourcingAdmissionConfig:
    mode: str
    automatic_admission_enabled: bool
    batch_limit: int
```

Validate exact `mode == "cluster_ranked"`, exact bool type, and exact integer `1 <= batch_limit <= 50`. Add the optional field to `DirectiveContent`, JSON serialization, and deserialization with absent-key compatibility. Add nullable `base_directive_version` to `DirectiveProposal` and its table/repository. Migration 0052 (`down_revision = "0051"`) leaves historical rows NULL, adds a nonnegative CHECK, and replaces `directive_proposal_guard()` so `base_directive_version` is immutable. Its downgrade refuses when any non-NULL base version exists, restores the old guard function, then drops the constraint and column. Update the tested Alembic head to 0052 in this commit.

- [ ] **Step 4: Add the explicit proposal service**

Define:

```python
async def submit_sourcing_admission_proposal(
    self,
    tenant_id: TenantId,
    raw_text: str,
    config: SourcingAdmissionConfigInput,
    interpretation_summary: str,
    expected_behavior_changes: list[str],
    parsed_by: str,
) -> str: ...
```

Within the proposal UoW, read the current active Directive, copy its complete `DirectiveContent` with `dataclasses.replace(..., sourcing_admission=...)`, and store `base_directive_version=active.version` or `0` when absent. Confirmation of a proposal carrying `sourcing_admission` must lock/read the current active version and reject unless it equals the recorded base; it then reuses the existing immutable activation/version/rollback path. Extend views with nullable `sourcing_admission_mode`, `automatic_sourcing_admission_enabled`, and `sourcing_admission_batch_limit`.

- [ ] **Step 5: Run tests**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_directives_service.py tests/integration/test_directives_persistence.py tests/integration/test_migrations.py -q
```

Expected: PASS, including old JSON compatibility and rollback producing a new version.

- [ ] **Step 6: Commit**

```bash
git add domains/directives/models.py domains/directives/schemas.py domains/directives/service.py domains/directives/service_impl.py infra/db/repositories/directives.py migrations/versions/0052_sourcing_admission_directive.py domains/directives/AGENTS.md tests/unit/test_directives_service.py tests/integration/test_directives_persistence.py tests/integration/test_migrations.py
git commit -m "feat: add sourcing admission directive policy"
```

---

### Task 4: Define sourcing admission state, snapshots, permissions, and pure ranking

**Files:**
- Modify: `domains/sourcing/models.py`
- Modify: `domains/sourcing/schemas.py`
- Modify: `domains/sourcing/repository.py`
- Modify: `domains/sourcing/service.py`
- Modify: `domains/sourcing/permissions.py`
- Modify: `domains/sourcing/AGENTS.md`
- Create: `domains/sourcing/admission.py`
- Create: `tests/unit/test_sourcing_admission.py`
- Test: `tests/unit/test_sourcing_permissions.py`

**Interfaces:**
- Consumes: typed IDs from Task 1.
- Produces: `AdmissionState`, `AdmissionBlockedReason`, `SourcingAdmission`, `SourcingPrioritySnapshot`, public commands/views, repository protocols, permissions, and `priority_sort_key`.

- [ ] **Step 1: Write failing pure-domain tests**

Test the exact key:

```python
assert priority_sort_key(snapshot) == (-8, READY_AT, str(NEED_ID))
```

Cover stable `8 → 3 → 1` order, unclustered count exactly one, UTC-aware dates, invalid hashes, invalid state-field combinations, no quantity field, claim transitions, expired claim release, admitted immutability, and deterministic Chinese explanation text.

- [ ] **Step 2: Run tests and verify RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_sourcing_admission.py tests/unit/test_sourcing_permissions.py -q
```

Expected: missing admission module/types/actions.

- [ ] **Step 3: Add focused models and pure functions**

Define states `waiting`, `starting`, `admitted`, `blocked`; blocked reasons `priority_facts_invalid` and `case_state_mismatch`; and ranking version constant `need-cluster-admission-v1`. Define:

```python
def priority_sort_key(snapshot: SourcingPrioritySnapshot) -> tuple[int, datetime, str]:
    return (-snapshot.cluster_member_count, snapshot.ready_at, str(snapshot.need_id))

def priority_explanation(snapshot: SourcingPrioritySnapshot) -> str:
    if snapshot.cluster_id is None:
        return "该需求尚未归入多成员需求簇；按等待时间排序。"
    return f"该需求簇当前有 {snapshot.cluster_member_count} 条已验证需求；同规模需求按等待时间排序。"
```

Model methods must enforce field/state combinations and never accept free-form blocked reasons.

- [ ] **Step 4: Add public service/repository contracts and permissions**

Add system actions `ADMISSION_ENQUEUE`, `ADMISSION_REFRESH`, `ADMISSION_CLAIM`, `ADMISSION_COMPLETE`; tenant actions `ADMISSION_READ` for boss/product/sourcing/finance and `ADMISSION_MANUAL_START` only for boss/sourcing. Add repository methods with exact tenant argument for get-or-create, append-snapshot-if-changed, ordered claim, complete, release, block, and list/read.

- [ ] **Step 5: Run tests and static checks**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_sourcing_admission.py tests/unit/test_sourcing_permissions.py tests/unit/test_sourcing_v2_contracts.py -q
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH mypy domains/sourcing
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add domains/sourcing/models.py domains/sourcing/schemas.py domains/sourcing/repository.py domains/sourcing/service.py domains/sourcing/permissions.py domains/sourcing/AGENTS.md domains/sourcing/admission.py tests/unit/test_sourcing_admission.py tests/unit/test_sourcing_permissions.py
git commit -m "feat: define sourcing admission domain"
```

---

### Task 5: Persist admission and immutable priority snapshots

**Files:**
- Create: `migrations/versions/0053_sourcing_admission.py`
- Modify: `infra/db/tables.py`
- Modify: `infra/db/repositories/sourcing.py`
- Modify: `infra/db/sourcing_uow.py`
- Test: `tests/integration/test_sourcing_migrations.py`
- Create: `tests/integration/test_sourcing_admission_repository.py`
- Modify: `tests/integration/test_migrations.py`

**Interfaces:**
- Consumes: Task 4 repository contracts/models.
- Produces: tenant-scoped PostgreSQL implementation with immutable snapshots and `FOR UPDATE SKIP LOCKED` claims.

- [ ] **Step 1: Write failing schema and repository tests**

Assert exact tables `sourcing_admissions` and `sourcing_priority_snapshots`; composite tenant foreign keys to Case/Need/admission; uniqueness on tenant+case and tenant+need; state CHECKs; 64-character lowercase hash CHECK; and a current-snapshot FK.

Repository tests must create ordered snapshots for counts 1, 8, 3 and assert `claim_ordered(..., limit=2)` returns 8 then 3. Add two concurrent transactions and assert only one can claim the same row. Assert cross-tenant IDs return no data and cannot update owner rows.

- [ ] **Step 2: Run tests and verify RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/integration/test_sourcing_migrations.py tests/integration/test_sourcing_admission_repository.py tests/integration/test_migrations.py -q
```

Expected: missing migration/tables/repository implementation.

- [ ] **Step 3: Implement migration and ORM rows**

Migration 0053 uses `down_revision = "0052"`. Create admissions first, snapshots second, then add the current snapshot FK. Add separate indexes `sourcing_admissions(tenant_id, state, current_snapshot_id)` and `sourcing_priority_snapshots(tenant_id, cluster_member_count DESC, ready_at, need_id, snapshot_id)`; PostgreSQL cannot create one index across a join. Add a database trigger that rejects UPDATE and DELETE on priority snapshots. The repository joins the current snapshot and locks only admission rows with `FOR UPDATE OF ... SKIP LOCKED`. `downgrade()` must first execute:

```sql
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM sourcing_admissions LIMIT 1)
     OR EXISTS (SELECT 1 FROM sourcing_priority_snapshots LIMIT 1) THEN
    RAISE EXCEPTION '0053 refuses to drop sourcing admission evidence';
  END IF;
END $$;
```

Then remove constraints and tables in reverse dependency order.

- [ ] **Step 4: Implement repositories and UoW wiring**

Use PostgreSQL `INSERT ... ON CONFLICT DO NOTHING` for canonical admission and snapshot deduplication. Claim with `SELECT ... FOR UPDATE SKIP LOCKED`, current snapshot join, explicit tenant/state/expired-lease predicates, and a conditional update to `starting` with claim token and expiry. Never load unscoped rows before `_require_tenant`.

- [ ] **Step 5: Run migration round-trip and repository tests**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/integration/test_sourcing_migrations.py tests/integration/test_sourcing_admission_repository.py tests/integration/test_migrations.py -q
```

Expected: upgrade → downgrade → upgrade passes on empty structures; downgrade with evidence fails safely; concurrency admits once.

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/0053_sourcing_admission.py infra/db/tables.py infra/db/repositories/sourcing.py infra/db/sourcing_uow.py tests/integration/test_sourcing_migrations.py tests/integration/test_sourcing_admission_repository.py tests/integration/test_migrations.py
git commit -m "feat: persist sourcing admission queue"
```

---

### Task 6: Implement tenant-safe sourcing admission services

**Files:**
- Modify: `domains/sourcing/service_impl.py`
- Test: `tests/unit/test_sourcing_service.py`
- Test: `tests/integration/test_sourcing_service_persistence.py`

**Interfaces:**
- Consumes: Task 4 service contracts and Task 5 repositories.
- Produces: idempotent enqueue/refresh/claim/complete/release/block/list service behavior.

- [ ] **Step 1: Write failing service tests**

Cover these calls and outcomes:

```python
admission_id = await service.enqueue_admission(
    TENANT, case_id, need_id, ready_at=READY_AT, facts=facts, actor=SYSTEM
)
claimed = await service.claim_admissions(
    TENANT, limit=2, claim_token="claim-1", claim_expires_at=LEASE_END, actor=SYSTEM
)
await service.complete_admission(
    TENANT, admission_id, claim_token="claim-1", workflow_run_id=RUN_ID,
    admitted_by="system:sourcing", admitted_at=NOW, actor=SYSTEM
)
```

Test same enqueue returns the same ID and snapshot; changed count appends one snapshot; admitted refresh is a no-op; invalid facts block only the affected admission; transient fact absence is not represented as blocked; stale token cannot complete; and authorization happens before UoW construction.

- [ ] **Step 2: Run tests and verify RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_sourcing_service.py tests/integration/test_sourcing_service_persistence.py -q
```

Expected: protocol methods exist but implementation is absent.

- [ ] **Step 3: Implement canonical facts hashing and services**

Hash only canonical JSON containing `need_id`, nullable `cluster_id`, integer count, UTC `ready_at`, UTC `facts_observed_at`, and ranking version. Use `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)` and SHA-256. Reject naive timestamps, bool counts, tenant/case/need mismatch, and malformed IDs before writing.

- [ ] **Step 4: Implement safe read views**

List waiting and blocked items in repository order; build waiting duration from injected `now`; return deterministic explanation and fixed policy/blocked codes; never return `claim_token`, lease value, internal exception, full Need snapshot, or Workflow context.

- [ ] **Step 5: Run tests**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_sourcing_service.py tests/integration/test_sourcing_service_persistence.py tests/integration/test_sourcing_repositories.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add domains/sourcing/service_impl.py tests/unit/test_sourcing_service.py tests/integration/test_sourcing_service_persistence.py
git commit -m "feat: implement sourcing admission service"
```

---

### Task 7: Replace immediate workflow starts with durable enqueue and cluster refresh

**Files:**
- Modify: `apps/scheduler_worker/sourcing_events.py`
- Modify: `apps/scheduler_worker/sourcing_runtime.py`
- Modify: `apps/scheduler_worker/AGENTS.md`
- Test: `tests/unit/test_scheduler_sourcing_events.py`
- Test: `tests/integration/test_sourcing_runtime_composition.py`

**Interfaces:**
- Consumes: Demand priority facts from Task 2 and sourcing admission services from Task 6.
- Produces: enqueue-only `SourcingTriggerHandler` and `SourcingClusterMembershipHandler`.

- [ ] **Step 1: Rewrite tests first to require zero immediate starts**

For both readiness events assert: Need snapshot read once, canonical Case opened once, priority facts read once, admission enqueued once, and `engine.start` calls are exactly zero. Add duplicate and out-of-order membership tests: membership-before-admission is a no-op; admission later uses current count; membership-after-admission refreshes waiting snapshots; admitted snapshot is unchanged.

- [ ] **Step 2: Run tests and verify RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_scheduler_sourcing_events.py tests/integration/test_sourcing_runtime_composition.py -q
```

Expected: existing handler still starts the workflow and no membership handler is registered.

- [ ] **Step 3: Implement enqueue-only triggering**

Remove `WorkflowEngine` from `SourcingTriggerHandler`. After `open_case`, call Demand’s public priority read and sourcing’s enqueue method with `ready_at=event.occurred_at`. Preserve the existing stable key `sourcing-case:v2:{tenant}:{need}` on `OpenSourcingCase`; sanitize dependency errors with fixed messages and detached exception chains.

- [ ] **Step 4: Implement and register cluster refresh handler**

Validate event tenant, IDs, positive exact integer count, and current Demand facts. Refresh only waiting/blocked matching admissions. Register the exact outbox consumer name `sourcing_case.cluster_membership` and keep existing `NeedClusterFormed` unconsumed by this path.

- [ ] **Step 5: Run tests**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_scheduler_sourcing_events.py tests/integration/test_sourcing_runtime_composition.py tests/unit/test_outbox_serialization.py -q
```

Expected: PASS and zero workflow starts during event delivery.

- [ ] **Step 6: Commit**

```bash
git add apps/scheduler_worker/sourcing_events.py apps/scheduler_worker/sourcing_runtime.py apps/scheduler_worker/AGENTS.md tests/unit/test_scheduler_sourcing_events.py tests/integration/test_sourcing_runtime_composition.py
git commit -m "feat: enqueue sourcing cases before workflow start"
```

---

### Task 8: Build the policy-gated admission driver and scheduler lifecycle

**Files:**
- Create: `apps/scheduler_worker/sourcing_admission.py`
- Modify: `apps/scheduler_worker/directive_reader.py`
- Modify: `apps/scheduler_worker/main.py`
- Modify: `apps/scheduler_worker/runtime.py`
- Create: `tests/unit/test_sourcing_admission_driver.py`
- Modify: `tests/integration/test_scheduler_worker.py`
- Modify: `tests/unit/test_scheduler_sourcing_runtime.py`
- Modify: `tests/integration/test_sourcing_runtime_composition.py`

**Interfaces:**
- Produces: `SourcingAdmissionPolicyRead`, `DirectiveSourcingAdmissionPolicyReader`, `SourcingAdmissionDriver.scan_once() -> SourcingAdmissionScanResult`, and optional `SchedulerRuntime.sourcing_admission_driver`.
- Uses: existing `WorkflowEngine.start` with the unchanged workflow type/context/idempotency key.

- [ ] **Step 1: Write failing policy and driver tests**

Test `policy_not_configured`, `automatic_admission_disabled`, and `policy_status_unknown` as distinct scan results with zero claim/start. Test enabled policy claims exactly `batch_limit`, uses database order, and starts with:

```python
await engine.start(
    TENANT,
    "sourcing_case",
    str(case_id),
    safe_context,
    f"sourcing-case:v2:{TENANT}:{need_id}",
)
```

Cover transient start returning admission to waiting, unexpected/unknown exception preserving starting until lease expiry, permanent validation blocking the item, restart with the same idempotency key returning the same Run, and failure after start/before bind recovering one canonical Run.

- [ ] **Step 2: Run tests and verify RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_sourcing_admission_driver.py tests/integration/test_scheduler_worker.py tests/unit/test_scheduler_sourcing_runtime.py -q
```

Expected: absent driver/runtime field and old cycle order.

- [ ] **Step 3: Implement strict Directive policy reader**

Map an active `DirectiveView` to:

```python
@dataclass(frozen=True)
class SourcingAdmissionPolicyRead:
    directive_id: str
    directive_version: int
    enabled: bool
    batch_limit: int
```

Return `None` only for a verified missing section; raise a fixed transient error for storage/shape uncertainty. Revalidate `cluster_ranked`, exact bool, and `1..50` even though the Directives domain already validated it.

- [ ] **Step 4: Implement driver recovery semantics**

Use a non-secret random claim token, a fixed deployment lease duration passed by composition, and the unchanged `_safe_context` builder moved to a reusable private helper. Process claimed rows independently so one invalid row does not erase successful bindings. Return counts and a fixed stop reason; logs contain only tenant, counts, reason, and cycle metadata.

- [ ] **Step 5: Insert driver into the locked scheduler cycle**

Extend runtime with a protocol and optional driver. Confirm the scheduler lock immediately before admission scan, then run order:

```text
outbox_pre → campaign → quote_expiry → sourcing_admission → workflow → outbox_post
```

Admission failure is phase-isolated and fixed-message logged; lock loss prevents admission and every later phase. Do not reuse global scheduler `batch_limit`; the driver reads the confirmed Directive limit.

- [ ] **Step 6: Run focused tests**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_sourcing_admission_driver.py tests/integration/test_scheduler_worker.py tests/unit/test_scheduler_sourcing_runtime.py tests/integration/test_sourcing_runtime_composition.py -q
```

Expected: PASS, including exact cycle order and restart recovery.

- [ ] **Step 7: Commit**

```bash
git add apps/scheduler_worker/sourcing_admission.py apps/scheduler_worker/directive_reader.py apps/scheduler_worker/main.py apps/scheduler_worker/runtime.py tests/unit/test_sourcing_admission_driver.py tests/integration/test_scheduler_worker.py tests/unit/test_scheduler_sourcing_runtime.py tests/integration/test_sourcing_runtime_composition.py
git commit -m "feat: admit sourcing workflows by cluster priority"
```

---

### Task 9: Add policy and admission HTTP surfaces with manual admission

**Files:**
- Modify: `workflows/sourcing_case/application.py`
- Modify: `apps/api/dependencies.py`
- Modify: `apps/api/composition/runtime.py`
- Modify: `apps/api/routers/command_center.py`
- Modify: `apps/api/routers/sourcing.py`
- Modify: `apps/api/AGENTS.md`
- Create: `tests/unit/test_command_center_router.py`
- Test: `tests/unit/test_sourcing_router.py`
- Test: `tests/integration/test_api_runtime.py`

**Interfaces:**
- Produces: `/sourcing-admission-proposals`, `/sourcing-admission-proposals/{id}/confirm`, `/sourcing-admissions`, `/sourcing-admissions/{id}`, and `/sourcing-admissions/{id}/admit`.
- Reuses: the Task 8 driver’s single-item `admit_one(...)` path and exact raw `Idempotency-Key` validation.

- [ ] **Step 1: Write failing router tests**

The proposal body is strict and explicit:

```json
{
  "message": "按需求簇排序，每轮最多启动 3 个寻源案例",
  "mode": "cluster_ranked",
  "automatic_admission_enabled": true,
  "batch_limit": 3
}
```

Assert boss-only create/confirm, expected behavior changes shown before confirmation, version returned after confirmation, read roles for queue/detail, boss/sourcing-only manual admit, exactly one raw idempotency header, 403 before service IO for wrong roles/tenant, 404 for absent IDs, and 503 for unavailable policy/driver dependencies.

- [ ] **Step 2: Run tests and verify RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_command_center_router.py tests/unit/test_sourcing_router.py tests/integration/test_api_runtime.py -q
```

Expected: route and dependency failures.

- [ ] **Step 3: Add proposal endpoints and projections**

Construct `SourcingAdmissionConfigInput` only from strict Pydantic fields. The expected behavior list must explicitly say whether automation is enabled and the exact batch limit. Confirmation calls the existing Directive confirmation service and does not start any sourcing workflow itself.

- [ ] **Step 4: Add queue/detail/manual endpoints**

List/detail call sourcing safe views. Manual admission calls an application method with trusted `SourcingActor`, raw request ID, and the Task 8 single-item start/bind path. The application must first read the tenant-bound admission/Case, reject already blocked invalid facts, and return the canonical admitted view on idempotent replay.

- [ ] **Step 5: Wire real API composition**

Provide the same Demand priority reader, sourcing service, Directive policy reader, and Workflow engine used by production runtime. API construction performs no claim, workflow start, secret resolution, or network call.

- [ ] **Step 6: Run API and OpenAPI contract tests**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_command_center_router.py tests/unit/test_sourcing_router.py tests/integration/test_api_runtime.py tests/unit/test_api_app.py -q
```

Expected: backend tests PASS and OpenAPI contains the new paths and schemas. Generated frontend types are updated and committed in Task 10.

- [ ] **Step 7: Commit backend API work**

```bash
git add workflows/sourcing_case/application.py apps/api/dependencies.py apps/api/composition/runtime.py apps/api/routers/command_center.py apps/api/routers/sourcing.py apps/api/AGENTS.md tests/unit/test_command_center_router.py tests/unit/test_sourcing_router.py tests/integration/test_api_runtime.py
git commit -m "feat: expose sourcing admission controls"
```

---

### Task 10: Wire generated types and the Command/Sourcing Center UI

**Files:**
- Modify: `apps/web/src/api/api.d.ts`
- Modify: `apps/web/src/views/command-center/CommandCenter.vue`
- Modify: `apps/web/src/views/sourcing/SourcingCenter.vue`
- Modify: `apps/web/src/views/sourcing/SourcingCaseDetail.vue`
- Modify: `apps/web/AGENTS.md`
- Create: `apps/web/tests/command-center.test.ts`
- Modify: `apps/web/tests/sourcing-center.test.ts`
- Create: `apps/web/tests/sourcing-case-detail.test.ts`

**Interfaces:**
- Consumes: generated OpenAPI contracts from Task 9.
- Produces: confirmed policy controls, separate waiting/in-progress sections, deterministic priority explanation, manual admission interaction, and immutable snapshot detail.

- [ ] **Step 1: Write failing component tests**

Add fixtures for an 8-member waiting item, an unclustered waiting item, a blocked item, and an admitted Case. Assert ordering remains server-provided; UI does not resort. Verify policy missing/disabled/unknown labels remain distinct, manual admit disables only the selected row while pending, 409/503 restore controls with local error text, and no private fields are rendered.

- [ ] **Step 2: Run tests and verify RED**

```bash
cd apps/web && npm test -- --run tests/command-center.test.ts tests/sourcing-center.test.ts tests/sourcing-case-detail.test.ts
```

Expected: absent controls/sections.

- [ ] **Step 3: Regenerate API types and implement views**

```bash
cd apps/web && PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH npm run gen:api
```

Use only `components["schemas"][...]` types. Render “等待准入” separately from “处理中”; show member count, ready/wait time, ranking version, explanation, fixed reason labels, directive version, admitted actor/time, and a confirmation dialog for manual admission. Never show claim token, lease, full Workflow context, raw exception, or infer that a cluster is a combined order.

- [ ] **Step 4: Run frontend checks**

```bash
cd apps/web && npm test -- --run tests/command-center.test.ts tests/sourcing-center.test.ts tests/sourcing-case-detail.test.ts
cd apps/web && npm run typecheck
cd apps/web && npm run build
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/api/api.d.ts apps/web/src/views/command-center/CommandCenter.vue apps/web/src/views/sourcing/SourcingCenter.vue apps/web/src/views/sourcing/SourcingCaseDetail.vue apps/web/AGENTS.md apps/web/tests/command-center.test.ts apps/web/tests/sourcing-center.test.ts apps/web/tests/sourcing-case-detail.test.ts
git commit -m "feat: show need cluster sourcing admission"
```

---

### Task 11: Add explicit historical repair tooling

**Files:**
- Create: `scripts/backfill_sourcing_admissions.py`
- Create: `tests/unit/test_backfill_sourcing_admissions.py`
- Create: `docs/operations/sourcing-admission.md`

**Interfaces:**
- Produces: dry-run-by-default administrator command that discovers only OPENED V2 Cases without Workflow Runs or admissions and can create admissions using verifiable `opened_at` as `ready_at` under explicit `--apply`.

- [ ] **Step 1: Write failing CLI tests**

Test no arguments/dry-run performs zero writes; `--apply --tenant-id <exact-id>` writes only eligible owner-tenant rows; started, terminal, other-tenant, malformed snapshot, and already-admitted cases are reported with fixed skip reasons. Assert stdout is structured JSON without DSN, Need snapshot, Provenance text, or exceptions.

- [ ] **Step 2: Run tests and verify RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_backfill_sourcing_admissions.py -q
```

Expected: script missing.

- [ ] **Step 3: Implement the safe command and runbook**

Load environment without printing it, require exact tenant on apply, use the production Demand priority reader and sourcing service, and emit counts plus `{case_id, status, reason}` only. Do not start workflows; after backfill, normal confirmed policy/manual admission controls execution.

- [ ] **Step 4: Run tests**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/unit/test_backfill_sourcing_admissions.py -q
```

Expected: PASS with dry-run zero writes.

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_sourcing_admissions.py tests/unit/test_backfill_sourcing_admissions.py docs/operations/sourcing-admission.md
git commit -m "feat: add safe sourcing admission backfill"
```

---

### Task 12: Controlled end-to-end acceptance, docs, and complete gates

**Files:**
- Create: `tests/e2e/test_sourcing_admission_controlled.py`
- Create: `docs/acceptance/2026-09-02-phase2-need-cluster-sourcing-admission.md`
- Modify: `HANDBOOK.md`
- Modify: `ROADMAP.md`
- Modify: `workflows/sourcing_case/AGENTS.md`

**Interfaces:**
- Consumes: complete Tasks 1–11.
- Produces: controlled real-core evidence, Browser evidence, exact remaining Phase 2 inventory, and final verification record.

- [ ] **Step 1: Write the controlled E2E as RED**

Use real PostgreSQL migrations, repositories, domain services, Outbox, scheduler cycle, Workflow engine, FastAPI, and Vite. Seed eight validated Needs in one cluster, three in another, and one unclustered; publish real readiness/membership events. Assert no Run before policy confirmation; confirm batch limit 2; first cycle starts two cases from the 8-member cluster; restart runtime and continue without duplicate Case/Run; manual admission is idempotent; all Need snapshots remain separate.

- [ ] **Step 2: Run the E2E and fix only product defects**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/e2e/test_sourcing_admission_controlled.py -q -rs
```

Expected: PASS. External Tavily/page/model/contact/email/procurement/Quote calls are all zero because this acceptance stops after Workflow admission/start.

- [ ] **Step 3: Perform Browser acceptance**

Start the real controlled API/Vite stack and inspect desktop and 390px mobile layouts. Record evidence for policy proposal/confirmation, distinct waiting/in-progress sections, 8-member priority explanation, exact two starts, immutable snapshot detail, manual admission replay, and zero console errors. Store only approved screenshots under a new `output/playwright/t11-*` directory; do not delete existing `t10-*` artifacts.

- [ ] **Step 4: Update scope documentation and acceptance ledger**

HANDBOOK must add the operating/stop/recovery sequence. ROADMAP must mark only `NeedCluster Sourcing Admission` complete and retain `Catalog Product Proposal`, contact waterfall, 70/30 allocator, backpressure, real direct supplier quote, and commercial sources as incomplete. The acceptance report must separate controlled core, Browser, real external calls (`not_run`/zero), and deployment/push status.

- [ ] **Step 5: Run structure, static, migration, backend, and frontend gates**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH ruff check .
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH mypy domains shared tool_gateway apps workflows notification_gateway infra
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH python3 scripts/check_boundaries.py
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH python3 scripts/scan_sensitive.py
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest -q -rs
cd apps/web && PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH npm run gen:api
git diff --exit-code -- apps/web/src/api/api.d.ts
cd apps/web && npm test
cd apps/web && npm run typecheck
cd apps/web && npm run lint
cd apps/web && npm run build
```

Expected: all commands PASS; lint may retain only the repository’s accepted warning baseline and no errors; OpenAPI has no drift.

- [ ] **Step 6: Commit final acceptance**

```bash
git add tests/e2e/test_sourcing_admission_controlled.py docs/acceptance/2026-09-02-phase2-need-cluster-sourcing-admission.md HANDBOOK.md ROADMAP.md workflows/sourcing_case/AGENTS.md output/playwright
git commit -m "docs: accept need cluster sourcing admission"
```

## Execution Notes

- Before each task, reread the nearest `AGENTS.md` for every directory being changed.
- Do not spawn parallel workers that edit shared contracts, `infra/db/tables.py`, migrations, API schema, or runtime composition simultaneously.
- Preserve all pre-existing untracked acceptance artifacts and unrelated worktrees.
- Do not repair, delete, or repack the known AppleDouble `._pack-*.idx` warning without separate user authorization.
- A controlled passing E2E does not authorize production activation, external sourcing, contact discovery, supplier contact, email, procurement, or customer quotation.
- After implementation and review, merge into local `main` only with explicit user confirmation; do not push or deploy unless separately authorized.
