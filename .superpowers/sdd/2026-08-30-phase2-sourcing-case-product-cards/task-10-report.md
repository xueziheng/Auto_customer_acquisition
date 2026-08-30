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
