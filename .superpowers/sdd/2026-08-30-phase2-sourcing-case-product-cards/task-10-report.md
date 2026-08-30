# Task 10 implementation report

## Initial delivery

- Commit: `e87d4d14d496fe75a4f251b1b5743db97f61dacb`
- Scope: bounded public sourcing, persistent locator receipts, restart rehydration,
  safe candidate drafts, exact stop reasons, and `research_only` zero downstream
  contact/email/quote actions.

## Fix round 1

### RED evidence

- Focused command:
  `/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q tests/unit/workflows/test_sourcing_public_search.py tests/unit/test_sourcing_v2_contracts.py tests/unit/test_web_search_discovery.py tests/unit/test_free_search_quota.py`
- Result before fixes: `18 failed, 154 passed`.
- The failures reproduced: failed/uncertain searches reporting zero attempts; an
  existing reservation without receipt reporting zero attempts; restart rereading an
  already-attempted page; raw page/extractor exceptions escaping; 51-word and
  over-limit plan text being accepted; page/robots 429 and robots 5xx being classified
  as access forbidden; and determinate Tavily rate-limit/transient errors becoming
  reconciliation.
- Direct Postgres regression tests were added for semantic plan-row corruption,
  receipt binding/type/URL/status conflicts, durable page-attempt replay, and 0050
  downgrade with all three new categories.

### Implementation

- Reconstruct and strictly validate every persisted `PublicSourcingPlanCommand`, then
  recompute and compare its canonical hash before returning a plan.
- Count each actual or already-reserved search slot exactly once, including all typed
  failure paths; Gateway validation now fails closed instead of becoming provider
  timeout.
- Added tenant/run/plan/query/result-bound `sourcing_page_attempts`. Claim is committed
  before page IO; restart counts and skips prior claims across read, extraction, and
  draft-save crash windows.
- Target and robots 429 map to rate limited; target/robots 5xx and transport timeouts
  map to transient. Tavily determinate rate-limit/transient errors retain those exact
  Gateway categories, while unknown post-dispatch windows remain reconciliation.
- Receipt restore now requires exact execution/plan/run/request/query binding, strict
  locator strings, public HTTP(S) URLs, and status/locator cardinality.
- Plan text limits now match Gateway: query 400 characters and 50 words; category 100
  characters.
- 0050 downgrade normalizes the three new categories to `provider_permanent` before
  restoring old checks, temporarily disabling only the append-only event trigger in
  the same migration transaction.
- Workflow ports now use `agent_runtime.SafeSourcingPageSnapshot`; no Connector DTO is
  imported by Workflow. Cleanup and dependency exceptions are sanitized and cleanup
  failures cannot mask the primary safe result.
- Fixed the free-search manifest property unpack typing without weakening the schema.

### GREEN and gates

- Focused unit after fixes: `173 passed in 0.57s`.
- Final focused + related sourcing/Postgres suite:
  `221 passed in 19.42s`.
- Full unit: `6161 passed in 66.61s`.
- Related Postgres suite alone: `48 passed in 19.03s`.
- `ruff check .`: passed.
- `python scripts/check_boundaries.py`: passed.
- Touched-file Mypy: no touched-file errors; the command still reports the existing
  13-error baseline in `shared/schemas/quote_creation.py`.
- Full sensitive scan still reports eight pre-existing test-fixture shapes outside
  this diff; staged-diff sensitive scan is recorded after staging.
- `git diff --check`: passed.

### Commit

- Fix-round implementation commit: `d5d15e6`.

### Concerns

- Migration 0047 remains unpublished and is intentionally amended in place. Any local
  Phase 2 database that already applied an earlier 0047 shape must be rebuilt or
  explicitly migrated.
- The repository continues to emit its pre-existing non-monotonic AppleDouble pack
  index warning. This round does not modify Git object storage.
- Task 10B candidate qualification and Tasks 11–13 remain intentionally out of scope.

## Fix round 2

### RED evidence

- Initial focused command:
  `/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q tests/unit/workflows/test_sourcing_public_search.py tests/integration/test_sourcing_search_quota.py tests/integration/test_sourcing_migrations.py`
- Initial result: `35 failed, 17 passed`. The failures demonstrated that restart had
  only a count/bare claim instead of durable slot state, completed draft/rejection
  outcomes could not be rehydrated, claimed crash windows could be reread or
  mislabeled, and safe wrapper exceptions retained raw dependency exceptions in
  `__context__`.
- A second focused corruption RED changed a completed slot's `result_index` outside
  its locator receipt; `restore_page_attempts` incorrectly accepted it (`1 failed`).
- The Postgres concurrency regression aligns two different-slot claims after plan
  load and before the budget serialization boundary, then asserts exactly one new
  claim under `max_pages_read=1`.

### Implementation

- Added strict frozen page-attempt status/outcome/claim contracts containing only
  tenant/Run/authorized-plan slot bindings, fixed safe outcomes, draft ID, and the
  derived supplier-identity flag; no locator or page payload is persisted there.
- Amended unpublished 0047 and current ORM with `claimed`/`completed` state,
  completion time, fixed outcome whitelist, and tenant-bound draft FK.
- Claims now take a tenant/Run/plan advisory transaction lock before canonical-slot
  lookup, page-budget count, and insert. Same-slot conflicts return the canonical
  slot; different-slot races cannot exceed the authorized budget.
- Completion is idempotent for the exact same result, rejects conflicts, and verifies
  an exact tenant/case/Run/plan/query/result draft binding before storing
  `draft_saved`.
- Restart validates every page slot against its exact successful search receipt and
  rehydrates completed drafts or fixed page stops without page IO. A claimed but
  unfinished slot always returns `reconciliation_required`.
- Search, quota, receipt, page-attempt, page-reader, extractor, draft and uncertain
  persistence exceptions are converted outside their `except` blocks. Outward safe
  errors have both `__cause__` and `__context__` unset; cleanup remains non-masking.

### GREEN and gates

- Focused public-search + migration/Postgres: `52 passed in 17.89s`.
- Expanded related sourcing suite: `233 passed in 18.97s`.
- Full unit suite: `6173 passed in 65.23s`.
- Related Postgres suite: `48 passed in 19.25s`.
- `ruff check .`, Python 3.12 `scripts/check_boundaries.py`, `git diff --check`, and
  staged sensitive scan: passed.
- Touched-file Mypy has no touched errors; it retains only the existing 13-error
  baseline in `shared/schemas/quote_creation.py`.
- Full sensitive scan retains only the existing eight unrelated test-fixture shapes.

### Commit

- Fix-round 2 implementation commit:
  `bcb8181c524b1759219f96e20679d6d39f027859`.

### Concerns

- Migration 0047 is still unpublished and intentionally amended in place. A local
  Phase 2 database that applied its older shape must be rebuilt or explicitly
  migrated.
- The existing AppleDouble pack index warning remains outside this change.
- Task 10B and Tasks 11–13 remain outside Task 10.

## Fix round 3

### RED evidence

- Focused command:
  `/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q tests/unit/workflows/test_sourcing_public_search.py`
- Result before fixes: `6 failed, 36 passed in 0.56s`.
- Two failures showed that completed canonical slots returned by a claim conflict
  were skipped instead of contributing their exact page, draft, supplier-identity,
  and rejection outcome, even though no page reread occurred.
- Four parametrized failures showed that raw `asyncio.CancelledError` from either
  `release` or `discard_all` escaped on a successful primary result and also masked
  an already-sanitized primary dependency failure.

### Implementation

- Added one completed-slot aggregation path shared by initial restart restoration
  and completed claim-conflict results. It records the canonical slot once, counts
  its durable page, restores its exact draft ID and derived supplier-identity flag,
  or retains its fixed rejection reason. A raced winner therefore advances or stops
  exactly like a slot restored at process start, with zero page/extractor/draft IO.
- Cleanup boundaries now contain every `BaseException`, including raw cancellation
  raised synchronously by `release` or `discard_all`; cleanup cannot replace either
  the primary safe result or the detached safe exception.

### GREEN and gates

- Focused public-search suite: `42 passed in 0.46s`.
- Expanded related unit suite: `191 passed in 0.57s`.
- Full unit suite: `6179 passed in 66.37s`.
- Related Postgres suite: `48 passed in 19.09s`.
- `ruff check .`, Python 3.12 `scripts/check_boundaries.py`, explicit changed-file
  sensitive scan, staged sensitive scan, and `git diff --check`: passed.
- Production touched-file Mypy has no touched errors; it retains only the existing
  13-error baseline in `shared/schemas/quote_creation.py`. Including the touched
  test file also exposes three existing fake/protocol typing errors in that test.
- Full sensitive scan retains only the existing eight unrelated test-fixture shapes.

### Commit

- Fix-round 3 implementation commit:
  `6b297b1`.

### Concerns

- Migration 0047 remains unpublished and intentionally amended by prior Task 10
  rounds. A local Phase 2 database that applied its older shape must be rebuilt or
  explicitly migrated.
- The existing AppleDouble pack index warning remains outside this change.
- Task 10B and Tasks 11–13 remain outside Task 10.
