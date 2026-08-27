# SDD ledger — plan: docs/superpowers/plans/2026-08-21-handbook-phase1-implementation-first-completion.md

## 当前结论（2026-08-27，本地合并复验修复）

- Ruling R8：首次合并到 main 后复验得到 `3 failed / 3996 passed`。原工作树的部分脚本
  子进程实际加载 editable 安装指向的主目录，因此历史“精确 HEAD 全绿”不能覆盖这部分
  子进程。此结论已更正，保留历史记录但不沿用其完整性判断。
- 修复提交 `6a0e644312983514114336d61822ed53f78fec90`：对齐正式
  `CampaignStateChanged` 事件的严格计数；8 个演示启动器显式绑定当前 checkout，
  不继承父进程额外环境。新增 8 项真实子进程回归先 RED 后 GREEN，相关 17 项通过。
- 独立审查通过，无 Critical / Important / Minor。修复分支和合并后的 main 均重新执行：
  后端 `4007 passed / 6 deselected`、前端 `151 passed`、强制浏览器 `6 passed`；
  Ruff、mypy 394 文件、七类边界、敏感扫描、API 类型零差异、前端类型与构建均通过。
- 用户已选择本地合并；修复已快进到 main，未 push/deploy。开发分支及工作树登记已清理；
  简报、审查记录和目录残留均已归档保留。详情见 [修复验收报告](merge-repair-report.md)。
- 以上仅恢复受控代码验收结论；真实 provider/model/mail 与四项运营标准仍为 `not_run`。

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
- Task 6B3: initial implementation submitted at `138c25a41c7a85401ef1126890d024e650af211e`; implementation evidence reported affected unit `90 passed`, focused PostgreSQL integration `16 passed`, and full migration file `58 passed`; real provider/model/mail remains `not_run`.
- Task 6B3: initial independent review returned 1 Critical and 5 Important findings (unverified customer-evidence public backdoor; promotion/intake crash-window replay gap; fabricated critical-field provenance; owner without ASSIGNED transition; acceptance bypassed confirmed proposal and durable reply artifact; missing ADR for public contracts).
- Task 6B3: fix round 1/5 implementation submitted（6 项已逐项修复：durable evidence verifier、
  crash replay、field-specific provenance/0038、ASSIGNED recovery、public proposal + artifact
  closed loop、ADR 0014；受影响 unit 116、PG integration 50、full migrations 59 均 GREEN；
  精确提交 SHA 见本轮 handoff，独立复审 pending）。
- Task 6B3: fix round 1/5 scoped re-review fully resolved 5 original findings and substantially closed the Critical public-method backdoor, but returned 2 Important integrity findings (verifier did not revalidate returned enrollment/delivery tenant and identity; migration 0038 JSONB lacked an object-shape CHECK).
- Task 6B3: fix round 2/5 implementation submitted (2 Important findings addressed, 0 implementation findings open: verifier now revalidates exact Enrollment/Delivery DTO tenant, type and identity; migration 0038 now enforces matching DB/ORM JSON object shape while preserving the `{}` legacy default; affected unit 120、PG integration 50、scheduler reply 6、full migrations 59 均 GREEN；独立复审 pending).
- Task 6B3: fix round 2/5 scoped re-review resolved both findings but returned 1 Important fail-closed finding (`get_enrollment` result was dereferenced before proving it is a real `EnrollmentView`, so a malformed adapter result could escape as `AttributeError`).
- Task 6B3: fix round 3/5 implementation submitted (1 Important addressed, 0 implementation findings open: real `EnrollmentView` required before dereference; full-shape impostor and missing-shape results now raise domain `ValidationError`; affected unit 122 and reply PostgreSQL integration 18 GREEN).
- Task 6B3: complete (commits `ce995ad..df9e574`, final scoped re-review Approved; real provider/model/mail remains explicitly `not_run`).
- Task 6C: implementation submitted from base `df9e57410a10ae6e3afb182f4453df0a4c8ca34e`（frontend focused RED `3 failed / 3 passed` → GREEN `6 passed`；full frontend `151 passed`；真实 PostgreSQL/Uvicorn/Vite/Chromium E2E `1 passed`；Browser 插件可用且完成 rendered QA；外部 provider/model/mail `not_run`；独立复审 pending）。
- Task 6C: initial independent review returned 6 Important findings (activation route interception bypassed production approval wiring; UI did not prove the durable chain ID-by-ID; Campaign-before-discovery order contradicted the Handbook flow; body-safety report/assertions were incomplete; per-surface interaction/page checks were incomplete; real handoffs were mislabeled as demo data).
- Task 6C: fix round 1/5 pending (6 findings open; the workflow order gap will be fixed to the Handbook sequence rather than waived).
- Task 6C: fix round 1/5 implementation submitted (6 Important findings addressed: real service-backed Campaign approval/activation; exact durable chain IDs; verified-contact pre-approval durable wait with exact-version event resume/fail-closed replay-reject-cancel-revise; >500-character raw-only body safety; per-surface identity/interaction/console checks; real handoff demo labels removed; independent re-review pending; external provider/model/mail remains `not_run`).
- Task 6C: fix round 1/5 scoped re-review fully closed 5 original findings and partially closed raw-body safety, but returned 6 Important findings (in-flight v1 workflow incompatibility; non-exact Campaign event version; cross-version Enrollment idempotency mismatch; incomplete event publication contract with one red backend test; Opportunity demo labels; incomplete credential/all-console/in-process-log leakage proof).
- Task 6C: fix round 2/5 pending (6 findings open).
- Task 6C: fix round 2/5 implementation submitted (6 Important findings addressed: executable persisted-v1
  compatibility alongside v2 new starts; exact event/run/persisted Campaign version gating; cross-version
  Enrollment replay conflicts with legacy `None` compatibility; complete Campaign event publication contract;
  Opportunity demo labels removed with exact-ID E2E; raw credential/tail/email markers scanned across durable
  state, all console levels and captured in-process logs; independent re-review pending; external
  provider/model/mail remains `not_run`).
- Task 6C: complete (commits `df9e574..7d098fc`, final scoped re-review Approved; external provider/model/mail/network remains explicitly `not_run`).
- Task 6D: complete from base `7d098fc48f000a55586870c1dd855454bce1af98` at exact acceptance HEAD `3e4d3ad72641bf772fef26f15092828cea933f9e`（修复 manual-send 并发连接池死锁 `945db24` 与 E2E append-only 隔离缺陷 `3e4d3ad`；fresh migration 59、frontend 151、non-e2e 3988、E2E 6、mutation/security 166+3 全绿；reply keyword smoke 160/0 errors；真实 provider/model/mail 与四项运营标准均 `not_run`；证据见 `task-6d-report.md`；未 push/deploy）。
- Final whole-branch review returned 2 Important findings (QualificationAgent allowed an unbounded full-body candidate quote into durable classification/Need Provenance; a normal second reply against an existing Need/Opportunity failed because intake incorrectly required all historical Need fields to originate from the current message). Task 6D completion is reopened pending TDD fixes, full exact-HEAD re-verification, and final re-review.
- Task 6D final-review fixes submitted at exact tested code HEAD `184fc15bc4f69b29ff82bb22a5b8868558322bb5`（两项 Important 通过 500-code-point 多层拒绝与跨消息 field-specific verified provenance/既有 Opportunity 幂等接管修复；同时修复同租户 engine 抢占未注册 workflow type 的 E2E 竞态；fresh migration 59、frontend 151、non-e2e 3999、forced E2E 6、mutation/security 323、crash/replay/claim 5 全绿；reply keyword smoke 160/0 errors；真实 provider/model/mail 与四项运营标准仍为 `not_run`；final independent re-review pending；未 push/deploy）。
