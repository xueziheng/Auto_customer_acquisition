# Phase 2 Catalog Product Proposal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn repeated, provenance-backed Validated Needs into a deterministic, human-approved candidate-product cultivation queue without creating a formal Product, claiming supply, or exposing a customer-quotable price.

**Architecture:** Demand owns verified cluster facts and their provenance; Products owns separately versioned policy, deterministic evaluations, proposals, and cultivation cases; Approvals owns the two human decisions; `workflows/catalog_product_proposal` maps the public service contracts and recovers uncertain results; the singleton scheduler consumes metadata-only events and runs bounded reconciliation; API and Web expose internal views only.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x, PostgreSQL/Alembic, durable Postgres workflow/outbox, Vue 3, TypeScript, Vite, Ant Design Vue, pytest, Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-04-phase2-catalog-product-proposal-design.md`

## Global Constraints

- Preserve `Demand Signal → Need Hypothesis → Validated Need → Trade Opportunity`; a Catalog Product Proposal is only an internal cultivation suggestion.
- Approval creates exactly one `CatalogCultivationCase(state="queued")`; it must not create or promote a `Product`, modify `source_only`/`partial` products, start sourcing, contact a supplier, generate a Quote, or send anything externally.
- There is no production default policy. Missing, unreadable, stale, rejected, or expired policy state fails closed and creates no new proposal.
- The controlled-test policy is explicit data: three distinct accounts; recurrence, country, and quantity/unit are displayed but are not hard gates.
- Demand and Products never import each other. The workflow maps `NeedClusterCatalogFacts` into a Products-owned input DTO and imports domains only through `service.py`.
- Every new entity, table, repository call, and query is tenant-bound. Cross-tenant and missing objects use the same external Not Found behavior.
- Facts and inference stay structurally separate. Country, recurrence, quantity, and unit count only with qualified provenance; no model probability, web inference, TLD, search country, or legacy cluster summary may fill a missing fact.
- `safe_total_quantity` is an integer fact, not money. This slice adds no monetary arithmetic; existing Decimal-only money rules remain unchanged.
- Events, logs, errors, notifications, and workflow context are metadata-only: no customer quote, field value, credential, DSN, raw provenance body, or database exception text.
- Recheck the Alembic head immediately before Task 2. It is `0056` at plan time; if it changes, renumber the migration and its tests instead of creating a second head.
- Use `/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3` for Python 3.12 commands. The system `/usr/bin/python3` is Python 3.9 and is not an acceptance runtime.
- The existing Git AppleDouble warning and untracked `output/playwright/t10-*` directories are unrelated user state. Do not delete, stage, or “repair” them in this work.
- Each task begins with a failing test, ends with focused passing tests plus `scripts/check_boundaries.py`, and is committed independently.

---

### Task 1: Lock identifiers and metadata-only event contracts

**Files:**
- Modify: `shared/schemas/identifiers.py`
- Modify: `shared/events/catalog.py`
- Modify: `infra/db/outbox.py`
- Modify: `domains/demand/events.py`
- Modify: `domains/products/events.py`
- Modify: `domains/prospecting/events.py`
- Test: `tests/unit/test_outbox_serialization.py`
- Create: `tests/unit/test_catalog_product_event_contracts.py`

**Interfaces:**
- Produces IDs: `CatalogProposalPolicyVersionId`, `CatalogProposalEvaluationId`, `CatalogProductProposalId`, `CatalogCultivationCaseId`.
- Produces events: `NeedCatalogFactsChanged`, `AccountCountryFactsChanged`, `CatalogProposalPolicyActivated`, `CatalogProductProposalCreated`, `CatalogCultivationQueued`.
- Preserves every existing event name and payload unchanged.

- [ ] **Step 1: Write failing ID and round-trip tests**

Construct every event with a UTC timestamp, tenant, and only locator metadata. Assert exact round-trip serialization and assert the serialized payload does not contain keys such as `quantity`, `country`, `customer_quote`, `provenance`, `price`, or `credential`.

Use these exact event fields:

```python
@dataclass(frozen=True)
class NeedCatalogFactsChanged(DomainEvent):
    need_id: ValidatedNeedId | None = None
    cluster_id: NeedClusterId | None = None
    change_kind: str = ""  # quantity | unit | recurring_requirement

@dataclass(frozen=True)
class AccountCountryFactsChanged(DomainEvent):
    account_id: ProspectAccountId | None = None

@dataclass(frozen=True)
class CatalogProposalPolicyActivated(DomainEvent):
    policy_version_id: CatalogProposalPolicyVersionId | None = None
    content_hash: str = ""

@dataclass(frozen=True)
class CatalogProductProposalCreated(DomainEvent):
    proposal_id: CatalogProductProposalId | None = None
    evaluation_id: CatalogProposalEvaluationId | None = None
    cluster_id: NeedClusterId | None = None
    policy_version_id: CatalogProposalPolicyVersionId | None = None
    facts_hash: str = ""

@dataclass(frozen=True)
class CatalogCultivationQueued(DomainEvent):
    cultivation_case_id: CatalogCultivationCaseId | None = None
    proposal_id: CatalogProductProposalId | None = None
```

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_outbox_serialization.py tests/unit/test_catalog_product_event_contracts.py -q
```

Expected: import/registry failures because the new contracts do not exist.

- [ ] **Step 3: Add the minimal contracts and explicit event validation**

Use prefixes `cpv`, `cpe`, `cpr`, and `ccc`. Register all five events explicitly in `EVENT_REGISTRY`; add shape validators for nonblank typed IDs, exact allowed `change_kind`, lowercase 64-hex hashes, and UTC timestamps. Re-export only each domain's published events from its `events.py`. Do not add an `AccountCountryFactsChanged` producer yet because the repository has no country-correction command; Task 4 consumes the contract and the future correction path can publish it without a new schema change.

- [ ] **Step 4: Run focused tests and boundary checks**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_outbox_serialization.py tests/unit/test_catalog_product_event_contracts.py -q
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/check_boundaries.py
```

- [ ] **Step 5: Commit**

```bash
git add shared/schemas/identifiers.py shared/events/catalog.py infra/db/outbox.py domains/demand/events.py domains/products/events.py domains/prospecting/events.py tests/unit/test_outbox_serialization.py tests/unit/test_catalog_product_event_contracts.py
git commit -m "feat: add catalog proposal event contracts"
```

---

### Task 2: Add the tenant-safe catalog persistence schema

**Files:**
- Create: `migrations/versions/0057_catalog_product_proposals.py` (renumber if head changed)
- Modify: `infra/db/tables.py`
- Create: `tests/integration/test_catalog_product_migration.py`
- Modify: `tests/integration/test_migrations.py`

**Interfaces:**
- Adds nullable `validated_needs.recurring_requirement` JSONB; historical rows remain unknown.
- Adds `catalog_proposal_policy_versions`, `catalog_proposal_evaluations`, `catalog_product_proposals`, and `catalog_cultivation_cases`.
- Establishes database-level tenant, state, uniqueness, hash, and immutable-subject guards.
- Extends the existing Approval contract CHECK for the two explicit catalog namespaces without changing quote or legacy semantics.

- [ ] **Step 1: Recheck the migration head and write failing schema tests**

Run `alembic heads` through the Python 3.12 environment. The test must assert a single head and use that exact revision. Cover upgrade from the prior head, table/column/check/index presence, tenant-composite foreign keys, and rejection of malformed hashes/states/JSON shapes.

Assert these key uniqueness rules with two concurrent transactions where practical:

```text
one active policy per tenant
tenant + proposed_by + creation_key per policy submission
tenant + cluster_id + policy_version_id + facts_hash per evaluation
tenant + evaluation_id per proposal
tenant + proposal_id per cultivation case
tenant + approval_id for each policy/proposal binding
```

- [ ] **Step 2: Run the migration test and verify RED**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/integration/test_catalog_product_migration.py tests/integration/test_migrations.py -q
```

- [ ] **Step 3: Implement the ORM rows and migration**

Use these minimum stored fields:

```text
policy: tenant, id, content JSONB, content_hash, base_active_version_id,
        proposed_by, creation_key, creation_request_hash, approval_id, state,
        created_at, activated_at, terminal_at
evaluation: tenant, id, cluster, policy id, facts_hash, facts JSONB,
            rule_results JSONB, overall_passed, blocked_reason, proposed_by_run,
            created_at
proposal: tenant, id, evaluation id, cluster, policy id, facts_hash,
          owner_employee, proposed_by_run, approval_id, approval_request_hash,
          state, created_at, updated_at
cultivation: tenant, id, proposal id, approval id, cluster, policy id,
             facts_hash, evidence_refs JSONB, state='queued', queued_at
```

Use partial unique indexes for active policy and non-null approval bindings. The policy creation key is scoped to tenant plus proposer and binds a canonical request hash, so a lost HTTP response can be replayed with the original key but a different request cannot alias it. Extend `ck_approval_quote_contract` into a contract check that preserves its exact quote branch and additionally admits only `catalog-policy-v1` or `catalog-cultivation-v1` rows with matching tenant/type/subject/change-set/request-hash fields. Add trigger/check protection so immutable content, hashes, subjects, approval bindings, and queued cultivation identity cannot be rewritten after insertion; only declared lifecycle fields may advance.

The migration must not infer recurrence from `need_clusters.recurring_demand`, notes, web pages, or model output. Its downgrade must first refuse if any catalog table contains rows or any `recurring_requirement` is non-null, then drop in reverse dependency order. Do not silently erase business data.

- [ ] **Step 4: Verify upgrade → downgrade → upgrade and head parity**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/integration/test_catalog_product_migration.py tests/integration/test_migrations.py -q
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/check_boundaries.py
```

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/0057_catalog_product_proposals.py infra/db/tables.py tests/integration/test_catalog_product_migration.py tests/integration/test_migrations.py
git commit -m "feat: add catalog proposal persistence schema"
```

---

### Task 3: Persist qualified recurring-requirement facts in Demand

**Files:**
- Modify: `domains/demand/models.py`
- Modify: `domains/demand/schemas.py`
- Modify: `domains/demand/service.py`
- Modify: `domains/demand/service_impl.py`
- Modify: `domains/demand/AGENTS.md`
- Modify: `infra/db/repositories/need_hypotheses.py`
- Modify: `domains/demand/unit_service.py`
- Modify: `domains/demand/unit_repository.py`
- Modify: `infra/db/need_unit_uow.py`
- Test: `tests/unit/test_demand_service.py`
- Modify: `tests/integration/test_demand_sourcing_ready_event.py`
- Modify: `tests/integration/test_need_units.py`

**Interfaces:**
- Adds `ValidatedNeed.recurring_requirement: FactualField[bool] | None`.
- Allows `recurring_requirement` in promotion/update field validation without changing completeness 0–5.
- Publishes `NeedCatalogFactsChanged` after committed quantity, unit, or recurrence changes.

- [ ] **Step 1: Write failing fact, provenance, completeness, and event tests**

Cover `True`, `False`, and missing recurrence; reject bool-like strings, Agent inference, web evidence, and missing source messages. Assert adding or changing recurrence never changes completeness, sourcing readiness, quote readiness, or existing Need state. Assert identical updates are idempotent and do not publish another event.

For quantity updates and successful unit confirmation, assert one metadata-only event with the post-change cluster locator. A failed UoW must roll back both the fact and outbox row.

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_demand_service.py tests/integration/test_demand_sourcing_ready_event.py tests/integration/test_need_units.py -q
```

- [ ] **Step 3: Extend the model, whitelist, repository mapping, and history**

Add recurrence to the stored JSON mapping and the mutable-field whitelist. Reuse the existing `FactualField`/`Provenance` validation path; do not introduce a special weaker source rule. Append field history on change and preserve old recurrence facts exactly as other mutable Need facts are preserved.

- [ ] **Step 4: Publish catalog fact changes in the same transaction**

In `update_need_fields`, publish for changed `quantity` or `recurring_requirement`. Extend the unit repository with a tenant-bound event-locator read returning only the current `need_id` and `cluster_id`; extend the existing unit UoW Protocol and SQLAlchemy UoW with the same transaction's `EventBus`, then publish from `NeedUnitService` for a newly committed current unit binding. Use `cluster_id=None` for an unclustered Need; consumers must treat it as no evaluable cluster, not fabricate a single-member Catalog cluster.

- [ ] **Step 5: Run focused regression and commit**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_demand_service.py tests/integration/test_demand_sourcing_ready_event.py tests/integration/test_need_units.py tests/integration/test_outbox_transaction.py -q
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/check_boundaries.py
git add domains/demand/models.py domains/demand/schemas.py domains/demand/service.py domains/demand/service_impl.py domains/demand/AGENTS.md infra/db/repositories/need_hypotheses.py domains/demand/unit_service.py domains/demand/unit_repository.py infra/db/need_unit_uow.py tests/unit/test_demand_service.py tests/integration/test_demand_sourcing_ready_event.py tests/integration/test_need_units.py
git commit -m "feat: record recurring need facts"
```

---

### Task 4: Build provenance-backed Need Cluster catalog facts

**Files:**
- Modify: `domains/demand/schemas.py`
- Modify: `domains/demand/service.py`
- Modify: `domains/demand/service_impl.py`
- Modify: `domains/demand/repository.py`
- Modify: `infra/db/repositories/need_clusters.py`
- Create: `workflows/catalog_product_proposal/account_facts.py`
- Create: `workflows/catalog_product_proposal/AGENTS.md`
- Modify: `domains/demand/AGENTS.md`
- Create: `tests/unit/test_demand_catalog_facts.py`
- Create: `tests/integration/test_demand_catalog_facts.py`

**Interfaces:**
- Produces `NeedClusterCatalogFacts` and `CatalogEvidenceSummary` from `domains.demand.service`.
- Produces `DemandCatalogAccountFact` and `DemandCatalogAccountFactsReader` narrow contracts.
- Adds `DemandService.get_cluster_catalog_facts(...)`, `list_catalog_cluster_ids(...)`, and `list_catalog_cluster_ids_for_account(...)`.

- [ ] **Step 1: Write failing aggregation tests**

Use clusters that prove all non-obvious cases:

- three Needs from three accounts with matching valid units produce `safe_total_quantity` and one `unified_unit`;
- two Needs from one account make that account's quantity coverage unknown and make `safe_total_quantity=None`;
- mixed units never convert or total;
- stale unit-to-quantity hashes do not count;
- same-account recurrence `True + False` counts one recurring account and emits a mixed-fact display code;
- countries count only when `field_provenance["country"]` is complete and non-Agent;
- missing/reversed membership, tenant mismatch, or category mismatch fails closed before a facts hash is emitted.

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_demand_catalog_facts.py tests/integration/test_demand_catalog_facts.py -q
```

- [ ] **Step 3: Define the public immutable facts DTOs**

Expose the exact spec fields, using tuples for stable-order collections:

```python
@dataclass(frozen=True)
class NeedClusterCatalogFacts:
    tenant_id: TenantId
    cluster_id: NeedClusterId
    cluster_category: str
    member_need_ids: tuple[ValidatedNeedId, ...]
    distinct_account_ids: tuple[ProspectAccountId, ...]
    member_count: int
    distinct_account_count: int
    known_country_codes: tuple[str, ...]
    unknown_country_account_count: int
    recurring_true_account_count: int
    recurring_false_account_count: int
    recurring_unknown_account_count: int
    quantity_unit_covered_account_count: int
    unified_unit: str | None
    safe_total_quantity: int | None
    evidence_summaries: tuple[CatalogEvidenceSummary, ...]
    display_codes: tuple[str, ...]
    facts_observed_at: datetime
    facts_hash: str
```

`CatalogEvidenceSummary` may contain only source type/id, extractor, confirmer, confirmation/observation time, and content hash. It must not contain `value`, `source_quote`, message text, URL content, or model reasoning.

- [ ] **Step 4: Implement stable aggregation and hashing**

Load and verify the full bidirectional membership set in one tenant-bound read. Normalize only whitespace/case where the existing unit contract permits; do not convert units. Build one per-account record before counting anything. Compute `facts_observed_at` from the latest persisted participating fact time and compute SHA-256 from canonical JSON excluding read time and display-only wording.

The account adapter calls only `ProspectingService.get_account`, maps an exact assigned uppercase ISO-2 account country and its provenance into the narrow Demand contract, and treats any other country spelling as unknown rather than maintaining a partial alias table. It catches dependency errors into fixed retryable/permanent errors without retaining raw exception text. No domain imports another domain.

- [ ] **Step 5: Run tests and commit**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_demand_catalog_facts.py tests/integration/test_demand_catalog_facts.py tests/unit/test_demand_service.py -q
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/check_boundaries.py
git add domains/demand/schemas.py domains/demand/service.py domains/demand/service_impl.py domains/demand/repository.py infra/db/repositories/need_clusters.py workflows/catalog_product_proposal/account_facts.py workflows/catalog_product_proposal/AGENTS.md domains/demand/AGENTS.md tests/unit/test_demand_catalog_facts.py tests/integration/test_demand_catalog_facts.py
git commit -m "feat: expose catalog-ready cluster facts"
```

---

### Task 5: Define strict Products policy and evaluation contracts

**Files:**
- Modify: `domains/products/models.py`
- Modify: `domains/products/schemas.py`
- Modify: `domains/products/service.py`
- Create: `domains/products/catalog_rules.py`
- Modify: `domains/products/permissions.py`
- Modify: `domains/products/AGENTS.md`
- Create: `tests/unit/test_catalog_policy.py`
- Create: `tests/unit/test_catalog_evaluation.py`

**Interfaces:**
- Produces `CatalogProposalPolicyContent`, `CatalogClusterFactsInput`, `CatalogProposalRuleResult`, and safe views.
- Produces pure `catalog_policy_content_hash(...)` and `evaluate_catalog_facts(...)`.
- Adds Products actions for policy propose/read, evaluation, proposal read, cultivation read, and system application.

- [ ] **Step 1: Write failing strict policy tests**

Assert this controlled-test content is accepted and hashes identically regardless of construction order:

```python
CatalogProposalPolicyContent(
    minimum_distinct_accounts=3,
    minimum_recurring_accounts=None,
    minimum_distinct_countries=None,
    minimum_quantity_unit_accounts=None,
    require_unified_unit=False,
)
```

Reject bool-as-int, zero, negative, values above PostgreSQL signed-int maximum `2_147_483_647`, unknown fields, optional thresholds above `minimum_distinct_accounts` except country, country threshold `1`, and `require_unified_unit=True` without a quantity/unit threshold. The upper bound is storage safety, not a hidden business default.

- [ ] **Step 2: Write failing deterministic evaluator tests**

Assert rule order and results exactly: `membership_integrity`, `distinct_accounts`, `recurring_accounts`, `distinct_countries`, `quantity_unit_coverage`, `unified_unit`. Each result is `passed|failed|unknown|not_required`; it stores actual/required values and a fixed Chinese explanation code, never a probability or free-form model explanation.

Required `failed` or `unknown` makes `overall_passed=False`; non-required unknown values do not block the controlled-test policy. A damaged facts input produces a fixed blocked result, not guessed zeroes.

- [ ] **Step 3: Run tests and verify RED**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_catalog_policy.py tests/unit/test_catalog_evaluation.py -q
```

- [ ] **Step 4: Implement immutable schemas and pure rules**

Use Pydantic v2 `ConfigDict(frozen=True, extra="forbid", strict=True)` for external command/input models and frozen dataclasses for internal entities. Validate lowercase 64-hex hashes and UTC timestamps. The evaluator consumes only Products-owned inputs; it must not import Demand models or call repositories.

- [ ] **Step 5: Run tests, boundaries, and commit**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_catalog_policy.py tests/unit/test_catalog_evaluation.py tests/unit/test_product_service.py -q
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/check_boundaries.py
git add domains/products/models.py domains/products/schemas.py domains/products/service.py domains/products/catalog_rules.py domains/products/permissions.py domains/products/AGENTS.md tests/unit/test_catalog_policy.py tests/unit/test_catalog_evaluation.py
git commit -m "feat: define catalog proposal rules"
```

---

### Task 6: Implement catalog repositories and the Products UoW

**Files:**
- Modify: `domains/products/repository.py`
- Create: `infra/db/repositories/catalog_products.py`
- Create: `infra/db/catalog_products_uow.py`
- Create: `tests/integration/test_catalog_product_repositories.py`

**Interfaces:**
- Produces tenant-bound repositories for policy versions, evaluations, proposals, and cultivation cases.
- Produces `CatalogProductsUnitOfWork` with one transaction and `PostgresEventBus`.
- Provides canonical conflict recovery reads and bounded stable pagination.

- [ ] **Step 1: Write failing repository and concurrency tests**

Cover round trips for every state and JSON payload; wrong-tenant reads return no object and wrong-tenant writes reject before SQL. Verify concurrent insertion converges on the database unique winner for evaluation, proposal, policy approval binding, proposal approval binding, and cultivation case.

Verify cursor order is stable:

```text
policies: created_at DESC, policy_version_id DESC
evaluations: created_at DESC, evaluation_id DESC
proposals: updated_at DESC, proposal_id DESC
cultivation: queued_at DESC, cultivation_case_id DESC
reconciliation scans: created_at ASC, entity_id ASC
```

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/integration/test_catalog_product_repositories.py -q
```

- [ ] **Step 3: Implement repository protocols and SQL adapters**

Every method takes an explicit `tenant_id`, compares it with the repository-bound tenant, and includes tenant predicates. Lock lifecycle transitions with `SELECT ... FOR UPDATE`. Catch only expected unique conflicts; roll back, open a fresh UoW, validate the canonical winner has the same immutable subject/hash, then return it. Never treat a mismatched winner as idempotent success.

- [ ] **Step 4: Run tests and commit**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/integration/test_catalog_product_repositories.py tests/integration/test_supply_pool_repositories.py tests/integration/test_product_candidate_idempotency.py -q
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/check_boundaries.py
git add domains/products/repository.py infra/db/repositories/catalog_products.py infra/db/catalog_products_uow.py tests/integration/test_catalog_product_repositories.py
git commit -m "feat: persist catalog proposal aggregates"
```

---

### Task 7: Implement policy-version lifecycle in Products

**Files:**
- Modify: `domains/products/service.py`
- Create: `domains/products/catalog_service_impl.py`
- Modify: `domains/products/errors.py`
- Create: `tests/unit/test_catalog_policy_service.py`
- Create: `tests/integration/test_catalog_policy_service.py`

**Interfaces:**
- Produces `CatalogProposalService.create_policy_candidate`, `get_active_policy`, `list_policy_versions`, `get_policy_change_snapshot`, `bind_policy_approval`, and `apply_policy_decision`.
- Publishes `CatalogProposalPolicyActivated` only after a successful active-policy transition.

- [ ] **Step 1: Write failing lifecycle tests**

Cover `pending_approval → active → superseded`, plus `rejected`, `expired`, and `stale`. Verify no active policy is a normal `None`, not an implicit default. Verify base-active mismatch marks the candidate stale, never supersedes the current active policy, and never emits an activation event.

Verify `approval_id` binding and application are idempotent only for the identical subject. A different approval ID/hash for the same version is a conflict. Replaying an already applied approval returns the same active version without a second event.

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_catalog_policy_service.py tests/integration/test_catalog_policy_service.py -q
```

- [ ] **Step 3: Implement the public protocol and service**

Use these core signatures:

```python
async def create_policy_candidate(
    tenant_id: TenantId,
    content: CatalogProposalPolicyContent,
    *,
    idempotency_key: str,
    actor: ProductActor,
) -> CatalogProposalPolicyVersionId: ...

async def apply_policy_decision(
    tenant_id: TenantId,
    policy_version_id: CatalogProposalPolicyVersionId,
    decision: CatalogApprovalDecisionInput,
    *,
    actor: ProductActor,
) -> CatalogProposalPolicyView: ...
```

The service re-reads the candidate and current active policy under lock. The opaque idempotency key binds a canonical request hash over content, proposer, and current base version: same key/same request returns the original candidate, while same key/different request conflicts. It never trusts a client-provided base version, actor, approval state, or content hash. Only the workflow may construct `CatalogApprovalDecisionInput`; the API never accepts it directly.

- [ ] **Step 4: Run tests and commit**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_catalog_policy_service.py tests/integration/test_catalog_policy_service.py -q
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/check_boundaries.py
git add domains/products/service.py domains/products/catalog_service_impl.py domains/products/errors.py tests/unit/test_catalog_policy_service.py tests/integration/test_catalog_policy_service.py
git commit -m "feat: manage catalog proposal policy versions"
```

---

### Task 8: Implement evaluation, proposal, and cultivation lifecycle

**Files:**
- Modify: `domains/products/service.py`
- Modify: `domains/products/catalog_service_impl.py`
- Modify: `domains/products/errors.py`
- Create: `tests/unit/test_catalog_proposal_service.py`
- Create: `tests/integration/test_catalog_proposal_service.py`

**Interfaces:**
- Produces `evaluate_cluster`, evaluation/proposal list/detail reads, `bind_proposal_approval`, `apply_cultivation_decision`, and cultivation queue reads.
- Publishes `CatalogProductProposalCreated` and `CatalogCultivationQueued` atomically with their business rows.
- Enforces `awaiting_approval_submission → pending_review → cultivation_queued|rejected|expired|stale` with no reverse transition.

- [ ] **Step 1: Write failing evaluation/proposal tests**

Assert identical `(tenant, cluster, policy_version, facts_hash)` returns one evaluation under replay and concurrency. Failed evaluations persist with rule details but create no proposal. Passed evaluations create one `awaiting_approval_submission` proposal in the same transaction, bind its `proposed_by_run` to the trusted system run, and derive `owner_employee` from the active policy submitter.

Prove policy version and the full facts snapshot are copied into the evaluation record rather than re-read for historical display. Test stable list/detail views and safe evidence summaries.

- [ ] **Step 2: Write failing approval application tests**

Cover pending binding, rejection, expiration, stale policy, stale facts, request-hash mismatch, approval-type mismatch, owner/decider conflict, and response-loss recovery. Only an approved, exact, current subject creates one queued case. Assert product table row counts and external-call counters remain zero.

- [ ] **Step 3: Run tests and verify RED**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_catalog_proposal_service.py tests/integration/test_catalog_proposal_service.py -q
```

- [ ] **Step 4: Implement deterministic lifecycle methods**

Use `catalog-cultivation:{proposal_id}:{policy_version_id}:{facts_hash}` as the only cultivation approval reference. `evaluate_cluster` requires a trusted `RunId` plus a SYSTEM actor; callers cannot synthesize an employee proposer. `apply_cultivation_decision` must receive the current mapped facts input and re-read the current active policy and stored proposal under lock. Any mismatch transitions to `stale` and commits no cultivation row.

Create the cultivation row first; only after that transaction succeeds may the workflow mark the Approval as applied. A canonical existing row with identical immutable fields is successful recovery; a mismatched row is a permanent conflict.

- [ ] **Step 5: Run tests and commit**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_catalog_proposal_service.py tests/integration/test_catalog_proposal_service.py tests/integration/test_catalog_product_repositories.py -q
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/check_boundaries.py
git add domains/products/service.py domains/products/catalog_service_impl.py domains/products/errors.py tests/unit/test_catalog_proposal_service.py tests/integration/test_catalog_proposal_service.py
git commit -m "feat: create catalog cultivation proposals"
```

---

### Task 9: Register the two central approval types

**Files:**
- Modify: `domains/approvals/models.py`
- Modify: `domains/approvals/service_impl.py`
- Modify: `domains/approvals/service.py`
- Modify: `domains/approvals/schemas.py`
- Create: `domains/approvals/catalog_contract.py`
- Modify: `domains/approvals/AGENTS.md`
- Modify: `tests/unit/test_approval_service.py`
- Modify: `tests/integration/test_quote_approval_postgres.py`
- Create: `tests/integration/test_catalog_approval_contract.py`

**Interfaces:**
- Adds `ApprovalType.CATALOG_PROPOSAL_POLICY_CHANGE = "catalog_proposal_policy_change"` with seven-day validity.
- Adds `ApprovalType.CATALOG_PRODUCT_CULTIVATION = "catalog_product_cultivation"` with three-day validity.
- Adds strict `catalog-policy-v1` and `catalog-cultivation-v1` request hashing and trusted fact reads.
- Preserves the generic Approval state machine and decision endpoint.

- [ ] **Step 1: Write failing enum, validity, label, self-approval, and idempotency tests**

Verify both types are in the central registry, use their exact validity periods, appear with stable Chinese labels, reject proposed-by/owner self-approval, and preserve generic pending-change-set idempotency. For each catalog namespace, change any subject field under the same change-set reference and assert `request_hash` conflict rather than reuse. Do not create a Products-specific decision endpoint or import Products into Approvals.

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_approval_service.py tests/integration/test_catalog_approval_contract.py -q
```

- [ ] **Step 3: Implement minimal registry changes and run regression**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_approval_service.py tests/integration/test_quote_approval_postgres.py tests/integration/test_catalog_approval_contract.py -q
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/check_boundaries.py
```

- [ ] **Step 4: Commit**

```bash
git add domains/approvals/models.py domains/approvals/service.py domains/approvals/schemas.py domains/approvals/service_impl.py domains/approvals/catalog_contract.py domains/approvals/AGENTS.md tests/unit/test_approval_service.py tests/integration/test_quote_approval_postgres.py tests/integration/test_catalog_approval_contract.py
git commit -m "feat: register catalog proposal approvals"
```

---

### Task 10: Build the durable policy approval workflow

**Files:**
- Create: `workflows/catalog_product_proposal/policy_flow.py`
- Create: `workflows/catalog_product_proposal/policy_steps.py`
- Create: `workflows/catalog_product_proposal/mapping.py`
- Create: `workflows/catalog_product_proposal/__init__.py`
- Create: `tests/unit/test_catalog_policy_workflow.py`
- Create: `tests/integration/test_catalog_policy_workflow.py`

**Interfaces:**
- Produces workflow type `catalog_proposal_policy_change` and deterministic start key `catalog-policy-change:{tenant}:{policy_version_id}`.
- Maps Products snapshots into a complete Approval package and exact reusable-approval validator.
- Applies Products first, then calls `ApprovalService.mark_applied`.

- [ ] **Step 1: Write failing workflow definition and step tests**

Use the sequence:

```text
assemble_package → submit_approval → wait_decision
wait_decision → apply_policy | expire_policy
apply_policy → mark_applied
```

Cover restart before approval submission, response loss after Approval insert, repeated `ApprovalDecided`, rejected/expired decisions, base-policy staleness, and business-committed/mark-applied-not-yet-recorded recovery.

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_catalog_policy_workflow.py tests/integration/test_catalog_policy_workflow.py -q
```

- [ ] **Step 3: Implement strict mapping and workflow steps**

The Approval package must use `catalog-policy-v1`, `catalog-policy:{policy_version_id}:{content_hash}`, and a stored request hash. It shows before/after policy, content hash, base active version, “未配置即关闭”, exact effect, rejection effect, and no external action. Proposed employee and owner are the policy submitter; the boss decision therefore cannot self-approve.

On an existing `change_set_ref`, compare every immutable Approval field before reuse. Never accept “same ID” alone. On an exact business failure mark a stable `catalog_policy_apply_failed` code; transient/unknown outcomes remain recoverable and do not expose exception text.

- [ ] **Step 4: Run tests and commit**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_catalog_policy_workflow.py tests/integration/test_catalog_policy_workflow.py tests/unit/test_approval_service.py -q
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/check_boundaries.py
git add workflows/catalog_product_proposal/policy_flow.py workflows/catalog_product_proposal/policy_steps.py workflows/catalog_product_proposal/mapping.py workflows/catalog_product_proposal/__init__.py tests/unit/test_catalog_policy_workflow.py tests/integration/test_catalog_policy_workflow.py
git commit -m "feat: orchestrate catalog policy approval"
```

---

### Task 11: Build event-driven evaluation and cultivation approval

**Files:**
- Create: `workflows/catalog_product_proposal/application.py`
- Create: `workflows/catalog_product_proposal/evaluation_flow.py`
- Create: `workflows/catalog_product_proposal/evaluation_steps.py`
- Create: `workflows/catalog_product_proposal/proposal_flow.py`
- Create: `workflows/catalog_product_proposal/proposal_steps.py`
- Modify: `workflows/catalog_product_proposal/mapping.py`
- Modify: `workflows/catalog_product_proposal/__init__.py`
- Create: `tests/unit/test_catalog_product_application.py`
- Create: `tests/unit/test_catalog_product_workflow.py`
- Create: `tests/integration/test_catalog_product_workflow.py`

**Interfaces:**
- Produces `CatalogProductApplication.submit_policy_candidate(...)` plus event handlers for Need cluster/fact/account changes, policy activation, proposal creation, and matching Approval decisions.
- Produces workflow types `catalog_cluster_evaluation` and `catalog_product_cultivation`.
- Uses deterministic workflow key `catalog-cultivation:{tenant}:{proposal_id}`.

- [ ] **Step 1: Write failing event mapping and no-policy tests**

Assert `NeedClusterMembershipChanged` and `NeedCatalogFactsChanged` re-read current Demand facts instead of trusting event counts. `AccountCountryFactsChanged` resolves bounded current cluster IDs for the account. No active policy is an acknowledged no-op. A malformed or transient Demand fact read creates no Product record and follows fixed permanent/retryable semantics.

For each exact active-policy/facts version, start one `catalog_cluster_evaluation` run with key `catalog-evaluation:{tenant}:{cluster_id}:{policy_version_id}:{facts_hash}`. Workflow context contains only locator IDs and hashes, never the facts body. Its step re-reads current policy and Demand facts; a mismatch is a stable stale no-op and a matching snapshot calls Products with `run.run_id` as the trusted system proposer.

Assert `CatalogProposalPolicyActivated` schedules bounded full-cluster reconciliation rather than placing all cluster IDs in the event. Assert duplicate events converge on one evaluation/proposal.

- [ ] **Step 2: Write failing cultivation workflow tests**

Use:

```text
assemble_package → submit_approval → wait_decision
wait_decision → apply_cultivation | expire_proposal
apply_cultivation → mark_applied
```

The package must use `catalog-cultivation-v1` and the exact prescribed change-set reference. It includes the policy version/hash, facts hash, ordered rule results, safe evidence references, the three-day expiry, rejection behavior, and the fixed warning that this is not a formal Product, confirmed supply, or customer-quotable price. Its proposer is the trusted system Run, its owner is the active policy submitter, and its deciding boss must differ from that owner.

- [ ] **Step 3: Run tests and verify RED**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_catalog_product_application.py tests/unit/test_catalog_product_workflow.py tests/integration/test_catalog_product_workflow.py -q
```

- [ ] **Step 4: Implement cross-domain mapping and recovery**

`mapping.py` performs an explicit field-by-field conversion from `NeedClusterCatalogFacts` to `CatalogClusterFactsInput`. Do not use `model_dump()`/`asdict()` as a cross-domain shortcut. `submit_policy_candidate` forwards the original idempotency key, creates the Products candidate, then starts the policy workflow with its deterministic key; commit/start uncertainty is repaired by scheduler reconciliation. The evaluation step uses its durable workflow Run ID as `proposed_by_run`. Re-read the Approval through the new catalog fact reader; validate namespace, type, change-set reference, immutable request hash, owner, proposer, decision, and expiry.

Before cultivation application, re-read current Demand facts and let Products re-read the active policy. Commit the case before `mark_applied`. If the case exists after an unknown response, validate the exact canonical row and only repair the Approval application receipt.

- [ ] **Step 5: Run tests and commit**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_catalog_product_application.py tests/unit/test_catalog_product_workflow.py tests/integration/test_catalog_product_workflow.py tests/integration/test_catalog_proposal_service.py -q
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/check_boundaries.py
git add workflows/catalog_product_proposal/application.py workflows/catalog_product_proposal/evaluation_flow.py workflows/catalog_product_proposal/evaluation_steps.py workflows/catalog_product_proposal/proposal_flow.py workflows/catalog_product_proposal/proposal_steps.py workflows/catalog_product_proposal/mapping.py workflows/catalog_product_proposal/__init__.py tests/unit/test_catalog_product_application.py tests/unit/test_catalog_product_workflow.py tests/integration/test_catalog_product_workflow.py
git commit -m "feat: orchestrate catalog cultivation proposals"
```

---

### Task 12: Wire singleton scheduler events and bounded reconciliation

**Files:**
- Create: `apps/scheduler_worker/catalog_products.py`
- Create: `apps/scheduler_worker/catalog_product_runtime.py`
- Modify: `apps/scheduler_worker/main.py`
- Modify: `apps/scheduler_worker/runtime.py`
- Modify: `apps/scheduler_worker/AGENTS.md`
- Create: `tests/unit/test_catalog_product_driver.py`
- Modify: `tests/integration/test_scheduler_worker.py`
- Create: `tests/integration/test_catalog_product_scheduler.py`

**Interfaces:**
- Produces `CatalogProductDriver.scan_once()` with stable cursor and explicit batch limit.
- Registers all three workflow definitions/handlers and all exact outbox subscriptions.
- Runs only under the existing confirmed singleton scheduler lock.

- [ ] **Step 1: Write failing driver and phase-order tests**

Cover missing policy, active policy, policy-read failure, bounded cluster page, cursor wrap, pending policy workflow recovery, awaiting-proposal workflow recovery, and stale event replay. Verify lost lock before or after the driver stops all later phases. Driver failure is isolated and logs only fixed phase/category/tenant/cycle dimensions.

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_catalog_product_driver.py tests/integration/test_scheduler_worker.py tests/integration/test_catalog_product_scheduler.py -q
```

- [ ] **Step 3: Implement composition and subscriptions**

Construct a dedicated `DemandServiceImpl` with `ProspectingCatalogAccountFactsReader`, a dedicated `CatalogProposalServiceImpl`, trusted system Product actors, the existing Approval service, and the shared Workflow engine/outbox. Register multiple handlers for `NeedClusterMembershipChanged` without replacing the existing sourcing-admission subscriber.

The reconciliation batch order is stable and bounded. It first recovers pending policy/proposal workflow starts, then—only under an active policy—scans cluster IDs, reads current hashes, and starts the deterministic evaluation workflows. It never evaluates directly without a durable system Run and never creates a test policy. Use the existing `SchedulerConfig.batch_limit`; do not hide another threshold in code.

- [ ] **Step 4: Place the phase behind lock checks**

Add the driver between sourcing admission and workflow polling. Reconfirm the same scheduler backend before and after it, and again before post-workflow outbox delivery when necessary. A runtime with the driver but no `confirm_lock` fails closed.

- [ ] **Step 5: Run tests and commit**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_catalog_product_driver.py tests/integration/test_scheduler_worker.py tests/integration/test_catalog_product_scheduler.py tests/unit/test_scheduler_sourcing_runtime.py -q
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/check_boundaries.py
git add apps/scheduler_worker/catalog_products.py apps/scheduler_worker/catalog_product_runtime.py apps/scheduler_worker/main.py apps/scheduler_worker/runtime.py apps/scheduler_worker/AGENTS.md tests/unit/test_catalog_product_driver.py tests/integration/test_scheduler_worker.py tests/integration/test_catalog_product_scheduler.py
git commit -m "feat: schedule catalog proposal reconciliation"
```

---

### Task 13: Expose internal Catalog Product Proposal APIs

**Files:**
- Modify: `apps/api/dependencies.py`
- Modify: `apps/api/composition/runtime.py`
- Modify: `apps/api/routers/products.py`
- Modify: `tests/unit/test_products_router.py`
- Modify: `tests/integration/test_api_runtime.py`
- Create: `tests/integration/test_catalog_products_api.py`

**Interfaces:**
- Adds policy create/active/history, evaluation list/detail, proposal list/detail, and cultivation queue list/detail endpoints under `/products/catalog-*`.
- Reuses `/approvals/{approval_id}/decision` for approve/reject.
- Requires the original `Idempotency-Key` for policy creation and accepts no client tenant, actor, provenance, facts hash, policy version, approval decision, or cultivation state.

- [ ] **Step 1: Write failing route, schema, role, and error tests**

Required routes:

```text
POST /products/catalog-policies
GET  /products/catalog-policies/active
GET  /products/catalog-policies
GET  /products/catalog-evaluations
GET  /products/catalog-evaluations/{evaluation_id}
GET  /products/catalog-proposals
GET  /products/catalog-proposals/{proposal_id}
GET  /products/catalog-cultivation-cases
GET  /products/catalog-cultivation-cases/{case_id}
```

Product/sourcing/boss may submit a candidate policy; only permitted internal roles may read. Verify a missing, blank, trimmed, or duplicated `Idempotency-Key` is rejected, same key/same content returns the original candidate, and same key/different content conflicts. Verify extra body fields are 422, cross-tenant/missing is 404, denied is 403 before service IO, and dependency failure is a redacted 503. Empty lists must not hide a 5xx.

All list routes use explicit stable pagination with `limit` in `1..200`; they do not return unbounded history.

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_products_router.py tests/integration/test_catalog_products_api.py tests/integration/test_api_runtime.py -q
```

- [ ] **Step 3: Implement API composition and safe projections**

Add separate `catalog_products: CatalogProposalService | None` and `catalog_product_application: CatalogProductApplication | None` dependencies instead of overloading the existing supply-card service. Derive `ProductActor` only from `RequestIdentity`. On POST, pass exactly one untrimmed nonblank `Idempotency-Key` to the application service, create the candidate, and idempotently start the policy workflow; if start fails after commit, return a safe retryable error and leave the candidate for scheduler reconciliation.

Return safe rule/evidence summaries and linked approval state. Do not expose Approval request hashes, workflow context, source quotes, raw provenance, claim tokens, or internal exception text.

- [ ] **Step 4: Generate OpenAPI and run API regression**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit/test_products_router.py tests/integration/test_catalog_products_api.py tests/integration/test_api_runtime.py tests/unit/test_api_app.py -q
cd apps/web && npm run gen:api
git -C ../.. diff -- apps/web/src/api/api.d.ts
```

Inspect the generated diff before Task 14; do not claim no drift until the generated file is committed and a second regeneration is clean.

- [ ] **Step 5: Commit backend API changes**

```bash
git add apps/api/dependencies.py apps/api/composition/runtime.py apps/api/routers/products.py tests/unit/test_products_router.py tests/integration/test_api_runtime.py tests/integration/test_catalog_products_api.py
git commit -m "feat: expose catalog proposal api"
```

---

### Task 14: Add the three internal Product Center regions

**Files:**
- Modify: `apps/web/src/api/api.d.ts`
- Modify: `apps/web/src/views/products/ProductSupplyCenter.vue`
- Create: `apps/web/src/views/products/CatalogPolicyPanel.vue`
- Create: `apps/web/src/views/products/CatalogProposalPanel.vue`
- Create: `apps/web/src/views/products/CatalogCultivationPanel.vue`
- Modify: `apps/web/tests/product-supply-center.test.ts`
- Create: `apps/web/tests/catalog-product-proposal.test.ts`

**Interfaces:**
- Renders policy versions, evaluations/proposals by state, and queued cultivation cases from generated OpenAPI types.
- Displays `通过 / 未通过 / 未知 / 不要求` and the fixed candidate-only warning.
- Sends policy content only; decisions remain in Approval Center.

- [ ] **Step 1: Write failing component tests**

Cover: no-policy-disabled message; controlled three-account policy form; stable reuse of the original policy request key after an uncertain response; pending/active/superseded history; failed evaluation with exact rule reasons; pending/rejected/expired/stale/cultivation-queued proposal filters; cultivation queue details; 403/404/503 distinction; and retry without inventing a new policy or approval. The raw key is never rendered or logged.

Assert this exact warning is visible in proposal and cultivation regions:

```text
这是一项候选产品培养建议，不代表已确认供应、正式产品或可报价价格。
```

Assert there is no “自动批准”, “创建正式产品”, “生成报价”, “联系供应商”, or probability display.

- [ ] **Step 2: Run tests and verify RED**

```bash
cd apps/web && npm test -- product-supply-center.test.ts catalog-product-proposal.test.ts
```

- [ ] **Step 3: Implement responsive internal UI**

Keep the existing supply-card area intact and add three clearly separated internal regions. Render rule status from backend values only. Evidence references may be displayed as identifiers, but opening originals must continue through the existing authorized evidence route; do not make raw artifact URLs.

- [ ] **Step 4: Regenerate types and verify the frontend**

```bash
cd apps/web
npm run gen:api
npm test -- product-supply-center.test.ts catalog-product-proposal.test.ts
npm run typecheck
npm run lint
npm run build
npm run gen:api
git diff --exit-code -- src/api/api.d.ts
```

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/api/api.d.ts apps/web/src/views/products/ProductSupplyCenter.vue apps/web/src/views/products/CatalogPolicyPanel.vue apps/web/src/views/products/CatalogProposalPanel.vue apps/web/src/views/products/CatalogCultivationPanel.vue apps/web/tests/product-supply-center.test.ts apps/web/tests/catalog-product-proposal.test.ts
git commit -m "feat: show catalog cultivation proposals"
```

---

### Task 15: Run controlled PostgreSQL, scheduler, API, and browser acceptance

**Files:**
- Create: `tests/e2e/test_catalog_product_proposal_controlled.py`
- Create: `docs/acceptance/2026-09-04-phase2-catalog-product-proposal.md`
- Modify: `HANDBOOK.md`
- Modify: `ROADMAP.md`
- Modify: `domains/products/AGENTS.md`
- Modify: `apps/web/AGENTS.md`
- Create: `output/playwright/t12-catalog-product-proposal/` only for final approved screenshots

**Interfaces:**
- Demonstrates one controlled Kenya three-wheeler cluster with three distinct accounts and approved test policy.
- Proves the full chain: facts → evaluation → proposal → Approval → queued cultivation case.
- Separately records controlled results, real external activity, and production activation state.

- [ ] **Step 1: Write the failing controlled acceptance test**

Use real PostgreSQL migrations, SQLAlchemy repositories, outbox delivery, Workflow engine, scheduler driver, FastAPI HTTP, generated TypeScript client, Vite, and a real Chromium context. Seed only controlled local records and provenance fixtures. Do not replace domain services, Postgres, outbox, workflow, or API with fakes.

The scenario must prove:

1. no active policy produces no evaluation/proposal;
2. product/sourcing user submits the exact three-account policy and a different boss approves it;
3. three Kenya accounts with qualified country provenance and three same-category three-wheeler Needs create one passed evaluation and one pending proposal;
4. recurrence/country/quantity-unit unknown values display but do not block this policy;
5. proposal approval by a different boss creates exactly one queued cultivation case;
6. duplicate events, scheduler restart, and repeated Approval delivery create no duplicates;
7. changing facts before decision makes the old proposal stale and creates no case;
8. Products row count, supplier/contact/search/send/quote counters, and Tool Gateway external calls remain unchanged/zero.

- [ ] **Step 2: Run the E2E test and verify RED**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/e2e/test_catalog_product_proposal_controlled.py -q -rs
```

- [ ] **Step 3: Complete browser evidence and acceptance report**

Inspect desktop and 390px layouts for policy disabled, pending policy approval, active policy, proposal rule details, stale protection, and cultivation queue. Save only the final deterministic screenshots in `output/playwright/t12-catalog-product-proposal/`; do not touch `t10-*` or `t11-*` evidence.

The report must state:

```yaml
controlled_postgres_api_scheduler_browser: passed | failed
real_external_provider_calls: not_run
real_supplier_contact: not_run
real_customer_contact: not_run
real_quote_or_price_commitment: not_run
production_policy_activation: not_run
```

Do not call the controlled policy a production default or claim the entire Phase 2 is complete.

- [ ] **Step 4: Run the full verification matrix**

```bash
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/check_boundaries.py
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/unit -q
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/integration -q
/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest tests/e2e/test_catalog_product_proposal_controlled.py -q -rs
cd apps/web && npm test && npm run typecheck && npm run lint && npm run build && npm run gen:api
git -C ../.. diff --exit-code -- apps/web/src/api/api.d.ts
git diff --check
```

If model prompts were not changed, record `tests/evals` as not applicable instead of running unrelated paid/model work. If any command cannot run, record it as `not_run` with the exact safe reason; never convert it to passed.

- [ ] **Step 5: Audit the hard boundaries manually**

Read the final diff and explicitly confirm:

- no Demand↔Products import;
- no query lacks tenant filtering;
- no event/log/error/workflow context contains fact bodies;
- no numeric confidence or float business value exists;
- no approval self-bypass or stale-policy/facts bypass exists;
- no Product, supplier action, search, contact, sending, Quote, or customer-visible surface was added;
- every response-loss path reads and validates a canonical row before retrying.

- [ ] **Step 6: Commit acceptance artifacts and documentation**

```bash
git add tests/e2e/test_catalog_product_proposal_controlled.py docs/acceptance/2026-09-04-phase2-catalog-product-proposal.md HANDBOOK.md ROADMAP.md domains/products/AGENTS.md apps/web/AGENTS.md output/playwright/t12-catalog-product-proposal
git commit -m "test: accept catalog product proposal workflow"
```
