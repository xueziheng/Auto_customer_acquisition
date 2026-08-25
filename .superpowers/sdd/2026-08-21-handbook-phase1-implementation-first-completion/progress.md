# SDD ledger — plan: docs/superpowers/plans/2026-08-21-handbook-phase1-implementation-first-completion.md

## Preflight

| Tasks / interface | Producer → consumer | Finding |
|---|---|---|
| 1 → 6 | Account Discovery runtime → account discovery unit/integration/closed-loop tests | Consistent; production commits already exist, acceptance remains. |
| 2 → 6 | Demand Discovery/Web Search/Demand Radar → demand and SSRF/budget/provenance tests | Consistent; production commits already exist, acceptance remains. |
| 3 → 6 | Campaign/approval APIs and UI → lifecycle/frontend/closed-loop tests | Consistent; production commits already exist, acceptance remains. |
| 4 → 6 | Reply workflow/Smart Inbox/Validated Need detail → reply/eval/frontend/closed-loop tests | Consistent; production commits already exist, acceptance remains. |
| 5 → 6 | Worker lifecycle/identity/router skeleton → browser and full acceptance | Consistent; production commits already exist, acceptance remains. |
| 6A → 6B–6D | Repeatable Alembic discovery on T7 → database/E2E/full gates | Plan gap: AppleDouble metadata is a reproduced local acceptance blocker and must be closed first. |
| 6B → 6C | Backend closed-loop fixture and safe IDs → real browser acceptance | Consistent; browser test must consume public API/UI behavior rather than private helpers. |
| 6B/6C → 6D | Focused acceptance slices → full local gate and final evidence | Consistent; final gate validates the exact integrated HEAD. |

- Ruling R1: Tasks 1–5 are pre-existing completed production work evidenced by their commit sequence and current files, so this run resumes at Task 6 instead of re-dispatching them — if wrong, Task 6 will expose a missing production dependency and that gap must enter its fix loop.
- Ruling R2: split oversized Task 6 into sequential 6A AppleDouble/Alembic repeatability, 6B backend/eval closed loop, 6C frontend/browser closed loop, and 6D full acceptance — each receives its own implementation and review gate while the final whole-branch review restores cross-slice coverage — if wrong, fragmented review could miss an interface mismatch until 6D/final review.
- Ruling R3: treat the reproduced T7 AppleDouble migration discovery failure as a Task 6 Step 5 prerequisite; the fix must identify/ignore or fail safely on AppleDouble metadata without weakening revision validation or silently deleting arbitrary files — if wrong, local acceptance remains non-repeatable or the guard may overfit one filesystem.
- Ruling R4: every Task 6 acceptance path uses controlled local transports and synthetic data; no real Hunter key, Hunter network, customer message, Gmail send, deployment, or Phase 1 operating-completion claim is authorized — if wrong, the tests could create external cost/compliance side effects.
- Ruling R5: the plan's per-task push/GitHub-run steps are deferred to finishing-a-development-branch because pushing is an external shared-branch side effect requiring the user's explicit integration choice — if wrong, final evidence will lack a GitHub Actions URL even though local exact-HEAD evidence exists.
- Ruling R6: new behavior and bug fixes follow current TDD instructions even though the historical plan recorded implementation-first/test-later; each production change needs a witnessed RED before GREEN — if wrong, the implementation order differs from the historical authorization but yields stronger regression evidence.
- Ruling R7: refine 6B into sequential 6B1 account/demand discovery acceptance, 6B2 Campaign/reply/eval acceptance, and 6B3 PostgreSQL Phase 1 closed-loop acceptance because the original backend slice spans independent failure domains — if wrong, extra review boundaries add ceremony and may defer a cross-slice mismatch until 6B3/6D.

## Baseline

- Main exact HEAD `9f2063014f865a0c06379ff996689cdc19d5b652`: merged-result `make check` passed with 3833 tests; frontend 148 tests/typecheck/build passed; ESLint 0 errors with 133 pre-existing warnings.
- New T7 worktree reproducibly contains AppleDouble `migrations/versions/._*.py`; `alembic heads` exits 1 with `SyntaxError: source code string cannot contain null bytes`; this is the witnessed RED acceptance blocker for Task 6A.

## Task progress

- Tasks 1–5: pre-existing complete; not re-dispatched under R1.
- Task 6A: dispatched from base `9f2063014f865a0c06379ff996689cdc19d5b652`.
- Task 6A: initial review returned 2 Important findings (four-byte magic accepted malformed/arbitrary content; mixed candidates caused unreported partial deletion).
- Task 6A: fix round 1/5 (2 addressed, 0 open — complete AppleDouble v2 validation; classify-all-before-delete; commits `3f5376e..9aff3a0`).
- Task 6A: complete (commits `9f20630..9aff3a0`, review clean).
- Task 6B1: dispatched from base `9aff3a01d8ce5217f77077fe22ae5edd5dcab566`.
- Task 6B1: initial review returned 4 Important findings (contact names still reach the model; model output can persist PII/credentials/actions/probability; numeric-probability variants bypass the rail; snapshot artifact references lack tenant/kind/hash referential integrity).
- Task 6B1: fix round 1/5 scoped re-review resolved artifact integrity only; 3 original Important findings remain and 1 new Important false-positive regression was found.
- Task 6B1: fix round 2/5 scoped re-review resolved probability and preserved artifact integrity, but returned 2 Important production-origin/identity findings.
- Task 6B1: fix round 3/5 scoped re-review resolved both identity findings but found 1 Important durable-outbox URL-path leak.
- Task 6B1: fix round 4/5 scoped re-review Approved (1 addressed, 0 open).
- Task 6B1: complete (commits `9aff3a0..b727069`, review clean; full suite last run 3899 passed before the metadata-only event contraction, followed by 159 affected and 139 independent review tests).
- Task 6B2: dispatched from base `b727069a2d47cc04066ef5a12b0380dde63d3dac`.
- Task 6B2: implementation submitted at `990adf706dbfc434eb9082284c7111343a652b41`; independent review pending.
- Task 6B2: initial review returned 1 Critical and 5 Important findings (account-wide unsubscribe downgrade; four actions permanently unavailable; suppression retry conflict; eval runner ignores action/scope and error denominators; full migration suite red; missing ADR for public Provenance/trigger changes).
- Task 6B2: fix round 1/5 scoped re-review resolved 5 findings and partially resolved the ADR finding; 1 Important full-body persistence mismatch remains.
- Task 6B2: fix round 2/5 scoped re-review resolved the Important finding but found 1 Minor leading-whitespace policy mismatch.
- Task 6B2: fix round 3/5 scoped re-review Approved (1 addressed, 0 open).
- Task 6B2: complete (commits `b727069..ce995ad`, review clean; real-provider/model acceptance remains explicitly `not_run`).
- Task 6B3: implementation complete from base `ce995ad63c3da20472dd8d7706e738569b493aeb`; implementation/report/ledger share one commit whose exact SHA is recorded in handoff; independent review pending. Final affected unit `90 passed`, focused PostgreSQL integration `16 passed`, full migration file `58 passed` (including migration-from-empty); real provider/model/mail remains `not_run`.
- Task 6C: pending.
- Task 6D: pending.
