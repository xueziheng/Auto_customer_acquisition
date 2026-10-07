# TradeOS HANDBOOK Phase 1 Implementation-First Completion Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐 HANDBOOK 七个切片中仍缺失的 Phase 1 运行链路，使网页端能从老板指令和需求信号开始，经企业/联系人发现、Campaign 触达、回复识别、Validated Need 到人工接管闭环运行。

**Architecture:** 沿既有单向依赖 `apps → workflows / agent_runtime → domains → shared` 补齐纵向切片；所有外部搜索、联系人补全、验证和邮件发送继续只走 Tool Gateway。模型只输出受限 ChangeSet，联系人 PII 不进入模型、workflow context、ledger、outbox 或日志。用户已明确批准“实现优先、测试后补”，因此 Task 1–5 每项只运行现有最小静态/边界门禁并独立提交推送，Task 6 再逐切片补齐行为测试与全量验收。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2.x、PostgreSQL 状态机、Vue 3、TypeScript、Vite、Ant Design Vue、conda `tradeos-py312`。

**Spec:** `HANDBOOK.md`、`ROADMAP.md`、`AGENTS.md`、各目标目录就近 `AGENTS.md`。

## Global Constraints

- 只实现 ROADMAP Phase 1；不自动化寻源、成本、报价，不引入 Temporal、多租户开关、WhatsApp、桌面端、模型路由或联系人多源瀑布。
- 模型永不接触凭证；外部动作全部经 `tool_gateway/`。
- PII 只在 connector、Gateway task-local typed slot、Prospecting 业务表和受信 workflow 调用栈内出现；不得进入模型、workflow context、tool ledger、outbox 或日志。
- 金额只用 `Decimal`/`Money`；模型不产最终金额或概率置信度。
- 关键字段必须带 Provenance；事实与推断使用不同结构和网页视觉样式。
- 只有 `VERIFIED` 联系方式可以进入 Campaign；`RISKY`、`UNVERIFIED`、`INVALID` 一律拒绝。
- 每个仓储查询必须携带 `tenant_id`，跨租户与不存在返回相同 NotFound。
- 每个小任务完成后运行触达文件的 ruff/mypy、`scripts/check_boundaries.py`、`scripts/scan_sensitive.py`，独立提交并推送；现有 GitHub CI 不关闭。

---

### Task 1: Account Discovery 纵向闭环

**Files:**
- Modify: `domains/prospecting/schemas.py`
- Modify: `domains/prospecting/service.py`
- Modify: `domains/prospecting/repository.py`
- Modify: `domains/prospecting/service_impl.py`
- Modify: `infra/db/repositories/prospecting.py`
- Create: `workflows/account_discovery/ports.py`
- Create: `workflows/account_discovery/steps.py`
- Create: `workflows/account_discovery/flow.py`
- Modify: `workflows/account_discovery/__init__.py`
- Modify: `agent_runtime/account_discovery/agent.py`
- Modify: `apps/scheduler_worker/runtime.py`
- Modify: `apps/scheduler_worker/main.py`
- Modify: `apps/api/dependencies.py`
- Modify: `apps/api/composition/runtime.py`
- Modify: `apps/api/routers/customer_discovery.py`
- Modify: `apps/api/main.py`
- Create: `apps/web/src/views/customer-discovery/CustomerDiscovery.vue`
- Modify: `apps/web/src/router.ts`
- Modify: `apps/web/src/App.vue`

**Interfaces:**
- Produces: `AccountDiscoveryAgent.run(task: AgentTask, context: object) -> ChangeSet` with only `prospecting.resolve_account` candidates; no contact PII.
- Produces: `build_account_discovery_definition() -> WorkflowDefinition` and `build_account_discovery_handlers(...) -> dict[str, StepHandler]`.
- Consumes: `ToolGatewayContactEnricher.find_contacts(...)` and `ToolGatewayContactVerifier.verify(...)` in the same trusted call stack.
- Consumes: `ProspectingService.resolve_account/create_contact/add_contact_point/record_verification/list_verified_contact_points`.
- Consumes: `EmployeeService.resolve_owner(...)` and `OutreachService.enroll(...)` through explicitly injected actors derived from the initiating employee.
- Produces API: `GET /prospects/accounts`, `GET /prospects/accounts/{account_id}`, `GET /prospects/accounts/{account_id}/contacts`, `POST /prospects/discoveries`.

- [ ] **Step 1: Add tenant-scoped query DTOs and repository reads**

  Add `ProspectAccountDetailView`, `ProspectContactDetailView`, `list_accounts(tenant_id, limit)`, and `list_contacts_for_account(tenant_id, account_id)`; implementation must bind tenant filtering in repository methods and sort deterministically by newest account/contact ID.

- [ ] **Step 2: Implement the constrained AccountDiscoveryAgent boundary**

  Define an `AccountDiscoveryModelPort` that receives only the approved hypothesis/evidence/page snapshot projection and returns JSON with keys `entity_name`, `country`, `website_domain`, `entity_type`, `industry`, `size_hint`, `source_signal_refs`. Reject unknown keys, missing domain, credentials, contact names, emails, phone numbers, numeric confidence, and side-effect verbs; emit one low-risk `prospecting.resolve_account` ChangeSet.

- [ ] **Step 3: Implement the durable account discovery workflow**

  Use states `find_company_details → resolve_account → find_contacts → verify_contacts → assign_owner → enroll_campaign → complete`. Persist only typed IDs, counts, fixed result categories and safe cost-note enum values in workflow context. In `find_contacts`, immediately persist candidate name/title/email plus legal basis from `ContactSource`; never put candidate values in context. In `verify_contacts`, persist all four verification outcomes, erase a privacy-claimed address through `handle_erasure_request`, and pass only verified contact point IDs forward.

- [ ] **Step 4: Enforce assignment and enrollment gates**

  Call `resolve_owner` before enrollment. For each verified point call `OutreachService.enroll` with deterministic idempotency key `account-discovery:{run_id}:{contact_point_id}`. No verified contacts is a successful `complete` result with `verified_count=0`, not an automatic retry loop.

- [ ] **Step 5: Register production composition fail-closed**

  Register `contact.enrich` only when an explicit country policy reader, Playbook reader, suppression reader, quota guard, Hunter secret resolver and fixed-host Hunter transport are all configured. Missing any dependency keeps account discovery unavailable with a typed readiness reason; it must never substitute an allow-all reader.

- [ ] **Step 6: Mount API and web page**

  The page lists enterprises, source-signal refs, contacts and verification state; raw email is visible only in the authorized employee API response and never written to browser logs. Discovery submission accepts a hypothesis ID, Campaign ID and role hints; tenant and employee identity come from server-side request identity, not request body.

- [ ] **Step 7: Run minimal implementation gate and publish**

  Run `ruff check` and `mypy` on touched Python modules, `python scripts/check_boundaries.py`, `python scripts/scan_sensitive.py`, `npm run typecheck`, and `npm run build`. Commit `feat: complete account discovery workflow` and push the exact branch HEAD.

---

### Task 2: Demand Discovery、Web Search 与 Demand Radar

**Files:**
- Replace: `connectors/web_search/client.py`
- Create: `connectors/web_search/transport.py`
- Create: `connectors/web_search/manifest.py`
- Create: `tool_gateway/handlers/web_search.py`
- Create: `tool_gateway/handlers/web_read_page.py`
- Modify: `tool_gateway/handlers/__init__.py`
- Modify: `agent_runtime/demand_intelligence/agent.py`
- Create: `workflows/demand_discovery/ports.py`
- Create: `workflows/demand_discovery/steps.py`
- Create: `workflows/demand_discovery/flow.py`
- Modify: `workflows/demand_discovery/__init__.py`
- Modify: `domains/demand/schemas.py`
- Modify: `domains/demand/service.py`
- Modify: `domains/demand/repository.py`
- Modify: `domains/demand/service_impl.py`
- Modify: `infra/db/repositories/demand.py`
- Modify: `apps/agent_worker/main.py`
- Modify: `apps/scheduler_worker/runtime.py`
- Modify: `apps/api/dependencies.py`
- Modify: `apps/api/composition/runtime.py`
- Modify: `apps/api/routers/demand_radar.py`
- Modify: `apps/api/routers/command_center.py`
- Modify: `apps/api/main.py`
- Create: `apps/web/src/views/demand-radar/DemandRadar.vue`
- Create: `apps/web/src/views/command-center/CommandCenter.vue`
- Modify: `apps/web/src/router.ts`
- Modify: `apps/web/src/App.vue`

**Interfaces:**
- Produces: `WebSearchResult`, `PageSnapshot`, `WebSearchConnector.search(...)`, `WebSearchConnector.read_page(...)` with URL, UTC observation time, SHA-256 content hash and immutable artifact ref.
- Produces tools: `web.search` and `web.read_page`; ledger outputs contain only task-local result handles and safe counts.
- Produces: `DemandIntelligenceAgent.run(...) -> ChangeSet` containing only `demand.capture_signal` and `demand.create_hypothesis` operations with signal evidence IDs.
- Produces: `build_demand_discovery_definition()` with `plan_search → execute_search → generate_hypotheses → score_and_queue`.
- Produces API: `GET /demand/signals`, `GET /demand/hypotheses`, `GET /demand/validated-needs`, `POST /commands/discovery-proposals`, `POST /commands/discovery-proposals/{id}/confirm`.

- [ ] **Step 1: Implement fixed-host search transport and typed snapshots**

  Resolve `WEB_SEARCH_API_KEY_REF` only inside connector configuration. Search results must be public HTTP(S) URLs. Page reads reject loopback/private/link-local destinations before and after redirects, write content to Artifact Store, and return a snapshot containing canonical URL, UTC `observed_at`, SHA-256 `content_hash`, and `snapshot_artifact_ref`.

- [ ] **Step 2: Register read-only Tool Gateway plugins**

  Both tools use `tenant → permission → playbook → country_policy → rate_limit`; `web.read_page` additionally validates the URL against the approved search-result handle. Tool ledger and workflow context persist handles, counts and hashes only, never page body or API key.

- [ ] **Step 3: Implement DemandIntelligenceAgent constrained output**

  The model input contains approved page text plus snapshot metadata and Playbook exclusions. Output schema allows signal observations and hypotheses only; every hypothesis references at least one produced signal index, wording remains explicitly inferential, and unknown keys, numeric confidence, final prices, credentials and actions are rejected.

- [ ] **Step 4: Implement bounded demand discovery workflow**

  The initiating proposal stores explicit `max_search_queries`, `max_pages_read`, `max_signals`, target markets/categories and strategy group. Every step decrements deterministic budgets before IO; exhaustion completes the run with a fixed reason. Persist signals through `DemandService.capture_signal`, resolve/create prospect accounts through public service ports, create hypotheses through `DemandService.create_hypothesis`, and queue only hypotheses whose deterministic confidence tier meets the confirmed proposal threshold.

- [ ] **Step 5: Add tenant-scoped Demand Radar queries**

  Add list views for signals, hypotheses and validated needs. The API includes evidence summaries and source URLs but not artifact object keys. The web page renders Signal as fact, Hypothesis with a prominent “推断” badge, and Validated Need separately with clickable provenance.

- [ ] **Step 6: Add command proposal/confirmation seam**

  Natural-language input creates an immutable proposal only. Confirmation records the exact exploration caps and starts a workflow; changing any cap produces a new proposal version. No unconfirmed command starts search or contact enrichment.

- [ ] **Step 7: Run minimal implementation gate and publish**

  Run touched-file ruff/mypy, boundary/sensitive scans, web typecheck/build; commit `feat: complete demand discovery workflow` and push exact HEAD.

---

### Task 3: Campaign Center 与审批边界网页闭环

**Files:**
- Modify: `apps/api/routers/campaigns.py`
- Modify: `apps/api/routers/approvals.py`
- Modify: `apps/api/main.py`
- Modify: `apps/api/dependencies.py`
- Modify: `apps/api/composition/runtime.py`
- Create: `apps/web/src/views/campaigns/CampaignCenter.vue`
- Create: `apps/web/src/views/approvals/ApprovalCenter.vue`
- Modify: `apps/web/src/router.ts`
- Modify: `apps/web/src/App.vue`

**Interfaces:**
- Produces API: campaign list/create/submit/revise/activate/pause/cancel/enrollments and approval list/detail/decide.
- Consumes only existing `OutreachService`, `SendingIdentityService`, and `ApprovalService` public contracts with request-derived typed actors.

- [ ] **Step 1: Expose complete Campaign state transitions**

  Request models exactly mirror `CampaignCreateRequest`; IDs and employee identity are server-derived. Activation requires an approved current version; revise always creates a new version and removes activation eligibility until reapproved.

- [ ] **Step 2: Expose approval decisions without bypasses**

  Approval details return the immutable boundary snapshot and `can_current_user_decide` from backend authorization. Decision body contains only `approve|reject` plus required rejection reason. No force/override query parameter is permitted.

- [ ] **Step 3: Build web Campaign and Approval pages**

  Show daily reserved/new-message limits, sender authentication/warmup/breaker state, sequence steps, approval state and enrollment progress. Disable unavailable actions using backend capabilities while preserving backend as final authority.

- [ ] **Step 4: Run minimal implementation gate and publish**

  Run touched gates; commit `feat: complete campaign approval web flow` and push exact HEAD.

---

### Task 4: Smart Inbox、回复全动作与 Validated Need 接线

**Files:**
- Create: `agent_runtime/qualification_agent/openai_port.py`
- Modify: `workflows/reply_qualification/ports.py`
- Modify: `workflows/reply_qualification/steps.py`
- Modify: `workflows/reply_qualification/flow.py`
- Modify: `apps/scheduler_worker/runtime.py`
- Modify: `apps/scheduler_worker/main.py`
- Modify: `apps/api/dependencies.py`
- Modify: `apps/api/composition/runtime.py`
- Modify: `apps/api/routers/inbox.py`
- Modify: `apps/api/routers/crm.py`
- Modify: `apps/api/main.py`
- Create: `apps/web/src/views/inbox/SmartInbox.vue`
- Create: `apps/web/src/views/demand-radar/ValidatedNeedDetail.vue`
- Modify: `apps/web/src/router.ts`
- Modify: `apps/web/src/App.vue`

**Interfaces:**
- Produces: real `ReplyModelPort` adapter receiving only guarded subject/body and returning strict JSON text; model credential remains inside adapter runtime configuration.
- Completes actions: `route_bounce`, `record_complaint`, `handoff`, `start_qualification`, `extract_need_fields`, `mark_future_restart`, `create_follow_up`, `intake_new_contact` through narrow public-domain ports.
- Produces API: inbox list/detail/classification evidence/follow-up approval and validated-need detail with original quote/provenance.

- [ ] **Step 1: Wire the production reply model port fail-closed**

  Resolve model API credentials in the adapter composition only. Apply input credential marker guard before the call, set output byte cap, strict JSON response, deterministic timeout, no raw prompt/body logging, and fixed transient/permanent error categories.

- [ ] **Step 2: Complete reply workflow actions**

  `extract_need_fields` updates an existing need or promotes a hypothesis only when source message evidence satisfies the demand-domain gate. `handoff` starts `human_handoff` with a packet containing metadata IDs and artifact links, never raw body in workflow context. `route_bounce` and `record_complaint` use existing delivery-feedback services. Follow-up produces an English draft and routes price/commitment content through approval before Tool Gateway.

- [ ] **Step 3: Build Smart Inbox and evidence view**

  Separate auto-reply, unsubscribe/complaint, interest, quote/sample/specification and ordinary conversations. Show classification model/version, customer quote, original artifact link, resulting action states and any approval/handoff; do not show model probability.

- [ ] **Step 4: Run minimal implementation gate and publish**

  Run touched gates; commit `feat: complete reply qualification web flow` and push exact HEAD.

---

### Task 5: Phase 1 运行进程与网页浅骨架收口

**Files:**
- Modify: `apps/agent_worker/main.py`
- Modify: `apps/browser_worker/main.py`
- Modify: `apps/api/main.py`
- Modify: `apps/web/src/router.ts`
- Modify: `apps/web/src/App.vue`
- Create: `apps/web/src/views/runs/RunCenter.vue`
- Create: `apps/web/src/views/settings/SettingsCenter.vue`
- Create: `apps/web/src/views/team/TeamCenter.vue`
- Create: `apps/web/src/views/work-uploads/WorkUploads.vue`
- Create: `apps/web/src/views/commitments/CommitmentCenter.vue`
- Create: `apps/web/src/views/manual-phase1/ManualOperations.vue`

**Interfaces:**
- `agent_worker` claims only approved Trade Run jobs and executes registered Phase 1 capability agents.
- `browser_worker` accepts only Tool Gateway-issued public-page read jobs, applies fixed browser policy and returns artifact snapshot metadata.
- Web routes cover the 16-page information architecture; Phase 2 automation actions are absent, while Phase 1 manual Product/Sourcing/Costing/Quote views are explicitly labeled and approval-protected.

- [ ] **Step 1: Implement worker lifecycle and readiness**

  Both workers validate database schema and registry before readiness, use bounded batch claims with `FOR UPDATE SKIP LOCKED`, finish the current job on SIGTERM, and never log job payloads, page bodies, PII or secrets.

- [ ] **Step 2: Mount all Phase 1 routers and navigation**

  Core routers are live; manual Product/Sourcing/Costing/Quote pages expose existing safe read/manual-entry interfaces only. Billing remains Phase 3 “未开通”. Empty Phase 2 workflow directories remain untouched.

- [ ] **Step 3: Centralize web request identity**

  Configure API base URL, tenant assertion and employee ID in one request interceptor/store. Production builds reject missing authenticated identity; dev assertions are allowed only with explicit dev mode and fixed tenant configuration.

- [ ] **Step 4: Run minimal implementation gate and publish**

  Run touched gates; commit `feat: complete phase1 runtime skeleton` and push exact HEAD.

---

### Task 6: Deferred Test Completion and Phase 1 Acceptance

**Files:**
- Create/Modify: `tests/unit/agent_runtime/test_account_discovery_agent.py`
- Create/Modify: `tests/unit/agent_runtime/test_demand_intelligence_agent.py`
- Create/Modify: `tests/unit/workflows/test_account_discovery.py`
- Create/Modify: `tests/unit/workflows/test_demand_discovery.py`
- Create/Modify: `tests/unit/workflows/test_reply_qualification.py`
- Create/Modify: `tests/integration/test_account_discovery_postgres.py`
- Create/Modify: `tests/integration/test_demand_discovery_postgres.py`
- Create/Modify: `tests/integration/test_phase1_closed_loop.py`
- Create/Modify: `apps/web/tests/customer-discovery.test.ts`
- Create/Modify: `apps/web/tests/demand-radar.test.ts`
- Create/Modify: `apps/web/tests/campaign-center.test.ts`
- Create/Modify: `apps/web/tests/smart-inbox.test.ts`
- Create/Modify: `tests/e2e/test_phase1_browser.py`

- [ ] **Step 1: Add account discovery tests**

  Cover duplicate account resolution, PII absence from workflow context/ledger/outbox/logging, legal-basis requirement, four verification outcomes, privacy erasure, verified-only enrollment, deterministic enrollment idempotency and cross-tenant indistinguishability.

- [ ] **Step 2: Add demand discovery tests**

  Cover SSRF/redirect blocking, page four-tuple evidence, task-local result handles, hard exploration caps, unconfirmed proposal no-op, evidence-required hypothesis, no numeric confidence and fact/inference separation.

- [ ] **Step 3: Add Campaign and reply tests**

  Cover current-version approval, pause semantics, stop-on-reply race, all 14 reply classes, unsubscribe zero leak, auto-reply zero sequence stop, every reply action, provenance on extracted fields and handoff packet completeness.

- [ ] **Step 4: Add frontend and browser acceptance**

  Browser flow: confirm discovery proposal → inspect signal evidence → inspect inferential hypothesis → discover/verify contact → approve Campaign → observe enrollment → ingest reply → inspect Validated Need quote/provenance → inspect handoff queue ordered by waiting time.

- [ ] **Step 5: Run full local acceptance**

  In conda `tradeos-py312`, run migrations on a fresh PostgreSQL database; then run ruff, full configured mypy, boundary scan, sensitive scan, all pytest suites including evals, front lint/typecheck/tests/build, Browser E2E and mutation audit.

- [ ] **Step 6: Publish exact green HEAD**

  Commit each test slice independently, push after each, wait for the GitHub Actions run tied to the exact commit SHA, and fix any red run before advancing. Final evidence records exact HEAD, local counts, GitHub run URL and the four HANDBOOK Phase 1 completion criteria.

---

## Self-Review

- Spec coverage: Tasks 1–5 cover remaining Slice 5–7 production gaps and web-first presentation; previously completed Slice 1–4 code is retained. Task 6 covers HANDBOOK acceptance and its high-risk traps.
- Scope boundary: No Phase 2 automated sourcing/costing/quote workflow, multi-provider waterfall, adaptive allocator, Temporal, desktop or billing implementation is included.
- Type consistency: workflow contexts use shared typed IDs; models return ChangeSet; domain writes go through public services; connector results cross Gateway only through task-local typed slots.
- Placeholder scan: Every planned step has a concrete behavior, target files, interface and gate; intentional Phase 2 stubs are explicitly preserved rather than treated as implementation gaps.
