# AI Outbound MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local-first, end-to-end AI B2B lead acquisition MVP with the selected smart-inbox UI, SQLite state, Codex-driven public-web research, Gmail-plugin email cycles, authorized quoting, and Obsidian CRM handoff.

**Architecture:** A dependency-free Node.js 24 application serves a localhost UI and stores operational state in built-in `node:sqlite`. A CLI exports deterministic research/email actions and ingests structured Codex results. Gmail and web access remain connector-driven; Obsidian is a private source and handoff target, not the runtime database.

**Tech Stack:** Node.js 24 ESM, `node:sqlite`, `node:http`, browser-native HTML/CSS/JavaScript, `node:test`, Gmail Codex plugin, Codex recurring automation, Obsidian Markdown.

## Global Constraints

- Bind the UI and API only to `127.0.0.1`; default port `4317`.
- Use no runtime npm dependencies; tests run with `node --test`.
- Use English for customer-facing email content and Chinese for internal UI, summaries, and tasks.
- Default new-send budget is `30/day`; hard maximum is `50/day` per mailbox.
- Follow-up schedule is day `0`, `3`, `7`, and `14`; no fifth no-reply message.
- A suppression, permanent bounce, complaint, or handoff state blocks every future automatic send.
- Obsidian source facts marked `conflict`, unchecked, or missing evidence cannot enter the authorized runtime catalog.
- An unmatched quote request must hand off; never synthesize price, MOQ, stock, certification, lead time, freight, or payment terms.
- Every external action has a stable idempotency key and an immutable audit event.
- The selected visual direction is `ui-inbox-focus`; production UI must not contain `DEMO DATA` unless demo mode is explicitly enabled.

---

### Task 1: Repository, configuration, and SQLite foundation

**Files:**
- Create: `.gitignore`
- Create: `package.json`
- Create: `src/config.mjs`
- Create: `src/db/schema.sql`
- Create: `src/db/database.mjs`
- Test: `tests/database.test.mjs`

**Interfaces:**
- Produces: `loadConfig(env): AppConfig`, `openDatabase(path): DatabaseSync`, `migrate(db): void`, `withTransaction(db, fn): unknown`.

- [ ] **Step 1: Initialize local version control and add runtime exclusions**

Run: `git init`

Create `.gitignore` with `runtime/`, `.env`, `*.log`, and macOS metadata. Create `package.json` with `type: module` and scripts `test`, `start`, `dev`, `db:init`, and `cycle`.

- [ ] **Step 2: Write the failing database test**

```js
test('migrate creates operational tables and is idempotent', () => {
  const db = openDatabase(':memory:');
  migrate(db);
  migrate(db);
  const tables = db.prepare("select name from sqlite_master where type='table'").all().map(x => x.name);
  assert.ok(['authorized_products','campaigns','companies','contacts','threads','messages','sequence_actions','handoffs','suppression_entries','audit_events'].every(x => tables.includes(x)));
});
```

- [ ] **Step 3: Run the test and confirm the expected failure**

Run: `node --test tests/database.test.mjs`
Expected: FAIL because `src/db/database.mjs` does not exist.

- [ ] **Step 4: Implement configuration, schema, migrations, and transactions**

`loadConfig` must normalize the Vault path, runtime directory, host, port, demo mode, daily budget, and timezone. `migrate` must execute the versioned schema in a transaction and record schema version `1`.

- [ ] **Step 5: Run tests and commit**

Run: `node --test tests/database.test.mjs`
Expected: PASS.

```bash
git add .gitignore package.json src/config.mjs src/db tests/database.test.mjs
git commit -m "chore: establish local outbound runtime"
```

### Task 2: Authorized Obsidian product synchronization

**Files:**
- Create: `src/products/parse-product.mjs`
- Create: `src/products/sync-products.mjs`
- Create: `src/products/authorization.mjs`
- Create: `src/cli.mjs`
- Test: `tests/products.test.mjs`
- Test fixture: `tests/fixtures/vault/20-Products/Products/PRD-TEST-001.md`

**Interfaces:**
- Consumes: `openDatabase`, `loadConfig`.
- Produces: `parseProductMarkdown(text, path): ParsedProduct`, `classifyOutboundFields(product): AuthorizedProduct`, `syncProducts(db, vaultPath): SyncReport`.

- [ ] **Step 1: Write failing tests for safe field extraction**

Cover valid `source_only` specifications, `conflict` exclusion, unchecked “对外使用前待确认” fields, image path preservation, stable product ID, and idempotent resync.

```js
assert.deepEqual(result.publicFacts, { motor_power_w: '1000 W', payload_kg: '500 kg' });
assert.equal(result.blockedFacts.price, 'missing_or_unverified');
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `node --test tests/products.test.mjs`
Expected: FAIL because product modules do not exist.

- [ ] **Step 3: Implement the parser and authorization boundary**

Parse YAML front matter without third-party packages, preserve source paths, extract specification tables, and reject `data_status: conflict`. Store only authorized facts in SQLite; store a local source reference rather than raw PPT content.

- [ ] **Step 4: Add the CLI sync command**

Run shape: `node src/cli.mjs products:sync --vault /Users/xueziheng/Desktop/Obsidian/company`.
Output JSON contract: `{ scanned, inserted, updated, blocked, warnings }`.

- [ ] **Step 5: Run tests, execute a read-only dry run against the real Vault, and commit**

Run: `node --test tests/products.test.mjs`
Run: `node src/cli.mjs products:sync --dry-run --vault /Users/xueziheng/Desktop/Obsidian/company`

```bash
git add src/products src/cli.mjs tests/products.test.mjs tests/fixtures
git commit -m "feat: synchronize authorized product facts"
```

### Task 3: Campaigns, public-web research jobs, and lead ingestion

**Files:**
- Create: `src/campaigns/service.mjs`
- Create: `src/leads/service.mjs`
- Create: `src/cycles/contracts.mjs`
- Create: `src/cycles/export-cycle.mjs`
- Create: `src/cycles/ingest-cycle.mjs`
- Test: `tests/campaigns.test.mjs`
- Test: `tests/cycle-contracts.test.mjs`

**Interfaces:**
- Produces: `createCampaign(db, input): Campaign`, `approveCampaign(db, id): Campaign`, `exportCycle(db, now): CycleEnvelope`, `ingestCycle(db, result): IngestReport`.
- `CycleEnvelope` contains `research_jobs`, `email_actions`, `reply_checks`, and `handoff_jobs` arrays with stable IDs.

- [ ] **Step 1: Write failing lifecycle and idempotency tests**

Test draft campaigns cannot emit research/email work, approved campaigns can, duplicate company domains merge, contact sources retain URL and discovery time, and duplicate cycle results do not duplicate leads.

- [ ] **Step 2: Run tests and confirm failure**

Run: `node --test tests/campaigns.test.mjs tests/cycle-contracts.test.mjs`

- [ ] **Step 3: Implement campaign approval and research job export**

Campaign inputs must include `mode` (`product` or `sourcing`), `country`, `buyer_type`, `language: en`, `product_ids`, `daily_budget`, and approval timestamps. New-product hypotheses remain `pending_approval` until explicitly approved.

- [ ] **Step 4: Implement structured lead ingestion**

Require company name, domain, country, buyer-fit evidence, source URL, and source timestamp. Contact email remains optional until verified. Reject consumer-only records and entries without traceable sources.

- [ ] **Step 5: Run tests and commit**

```bash
node --test tests/campaigns.test.mjs tests/cycle-contracts.test.mjs
git add src/campaigns src/leads src/cycles tests
git commit -m "feat: add approved campaign and lead research cycles"
```

### Task 4: Email sequence scheduler and Gmail action protocol

**Files:**
- Create: `src/email/scheduler.mjs`
- Create: `src/email/templates.mjs`
- Create: `src/email/gmail-contract.mjs`
- Create: `src/email/suppression.mjs`
- Test: `tests/email-scheduler.test.mjs`
- Test: `tests/gmail-contract.test.mjs`

**Interfaces:**
- Produces: `scheduleSequence(db, threadId, startAt): SequenceAction[]`, `getDueEmailActions(db, now, budget): GmailAction[]`, `applyGmailResult(db, result): void`, `suppress(db, identity, reason): void`.
- `GmailAction` variants: `send_new`, `reply`, `apply_label`, `stop_sequence`, `check_thread`.

- [ ] **Step 1: Write failing scheduling and hard-stop tests**

Verify D0/D3/D7/D14 dates, 30 default and 50 hard cap, no duplicate actions, a reply cancels future no-reply steps, and suppression/handoff blocks every send.

- [ ] **Step 2: Run tests and confirm failure**

Run: `node --test tests/email-scheduler.test.mjs tests/gmail-contract.test.mjs`

- [ ] **Step 3: Implement deterministic English templates**

Templates must receive only authorized product facts and campaign context. Every message includes real sender identity and a concise opt-out sentence. Follow-ups reference the prior thread without inventing new claims.

- [ ] **Step 4: Implement Gmail action/result contracts**

Each action includes `idempotency_key`, sender mailbox, recipient, subject/thread ID, body, labels, and authorization evidence. Results include Gmail message/thread IDs, sent/delivered timestamps, bounce/auto-reply/unsubscribe flags, and error class.

- [ ] **Step 5: Run tests and commit**

```bash
node --test tests/email-scheduler.test.mjs tests/gmail-contract.test.mjs
git add src/email tests
git commit -m "feat: schedule safe Gmail outreach sequences"
```

### Task 5: Reply intent, quote authorization, and human handoff

**Files:**
- Create: `src/intent/classifier.mjs`
- Create: `src/quotes/service.mjs`
- Create: `src/handoffs/service.mjs`
- Test: `tests/intent.test.mjs`
- Test: `tests/quotes-handoffs.test.mjs`

**Interfaces:**
- Produces: `classifyReply(input): IntentDecision`, `matchQuoteRule(db, request): QuoteDecision`, `createHandoff(db, input): Handoff`.
- `IntentDecision.intent` is one of `purchase_action`, `question`, `not_now`, `not_interested`, `unsubscribe`, `auto_reply`, `unknown`.

- [ ] **Step 1: Write failing intent and quote tests**

Cover price/catalog/spec/sample/meeting requests, polite non-interest, opt-out, out-of-office, matching quantity/Incoterm/validity ranges, expired rules, and missing prices.

- [ ] **Step 2: Run tests and confirm failure**

Run: `node --test tests/intent.test.mjs tests/quotes-handoffs.test.mjs`

- [ ] **Step 3: Implement the deterministic fallback classifier and agent contract**

The local classifier supplies safe fallback labels; Codex may return a structured higher-quality decision containing `intent`, `confidence`, `evidence_quote`, and `normalized_need`. A purchase action always stops the no-reply sequence.

- [ ] **Step 4: Implement quote matching and handoff creation**

Only `enabled` rules with matching product, currency, quantity band, Incoterm, and validity dates can produce a quote action. Otherwise create a handoff with reason `quote_required` or `sourcing_required`. Handoff due time is the next Shanghai business day boundary within 24 hours.

- [ ] **Step 5: Run tests and commit**

```bash
node --test tests/intent.test.mjs tests/quotes-handoffs.test.mjs
git add src/intent src/quotes src/handoffs tests
git commit -m "feat: route purchase intent to quotes or humans"
```

### Task 6: Obsidian CRM handoff adapter

**Files:**
- Create: `src/obsidian/ids.mjs`
- Create: `src/obsidian/render.mjs`
- Create: `src/obsidian/export-handoff.mjs`
- Test: `tests/obsidian.test.mjs`

**Interfaces:**
- Produces: `renderCompanyNote`, `renderContactNote`, `renderInteractionNote`, `exportHandoff(db, handoffId, vaultPath): ExportReport`.

- [ ] **Step 1: Write failing snapshot tests using a temporary Vault**

Assert stable IDs, valid YAML, resolvable company WikiLinks, ISO timestamps with offsets, one and only one unfinished `#follow-up` task, `stage: replied`, source references, and idempotent repeated exports.

- [ ] **Step 2: Run tests and confirm failure**

Run: `node --test tests/obsidian.test.mjs`

- [ ] **Step 3: Implement safe note rendering**

Follow the existing Vault data dictionary exactly. Do not set `qualifying` or later stages automatically. Store the Chinese summary and next action in the note body while keeping YAML fields within the allowed set.

- [ ] **Step 4: Implement atomic handoff export**

Write new files through a temporary sibling and atomic rename; detect duplicates before creation; update the same company follow-up task instead of creating competing tasks. Real Vault export requires explicit `--apply`; default is `--dry-run`.

- [ ] **Step 5: Run tests, dry-run one fixture handoff, and commit**

```bash
node --test tests/obsidian.test.mjs
node src/cli.mjs obsidian:handoff --fixture --dry-run
git add src/obsidian tests/obsidian.test.mjs
git commit -m "feat: export qualified leads to Obsidian CRM"
```

### Task 7: Local API and selected smart-inbox UI

**Files:**
- Create: `src/http/server.mjs`
- Create: `src/http/routes.mjs`
- Create: `public/index.html`
- Create: `public/styles.css`
- Create: `public/app.js`
- Create: `public/assets/README.md`
- Test: `tests/http.test.mjs`
- Test: `tests/ui-contract.test.mjs`

**Interfaces:**
- Produces HTTP endpoints: `GET /api/dashboard`, `GET /api/threads`, `GET /api/threads/:id`, `POST /api/threads/:id/takeover`, `GET /api/campaigns`, `GET /api/products`, `GET /api/health`.

- [ ] **Step 1: Write failing API and UI contract tests**

Test localhost-only binding, JSON content types, dashboard counts, thread detail shape, takeover idempotency, no `DEMO DATA` in live mode, and presence of Gmail state, English message, Chinese summary, and takeover button containers.

- [ ] **Step 2: Run tests and confirm failure**

Run: `node --test tests/http.test.mjs tests/ui-contract.test.mjs`

- [ ] **Step 3: Implement the local API**

Use `node:http`, explicit route matching, request body size limit `64 KiB`, JSON error envelopes, and `Cache-Control: no-store` for operational data. Reject non-loopback Host headers.

- [ ] **Step 4: Adapt the selected B visual direction**

Convert `ui-inbox-focus.html` into production `index.html`, `styles.css`, and `app.js`. Populate status cards, thread list, messages, Chinese summary, opportunity facts, and takeover actions from API responses. Preserve the blue-purple visual direction and responsive behavior down to 1180 px width.

- [ ] **Step 5: Run tests, render a 1536×1024 screenshot, and commit**

```bash
node --test tests/http.test.mjs tests/ui-contract.test.mjs
npm start
git add src/http public tests
git commit -m "feat: deliver the smart inbox operations UI"
```

### Task 8: Codex cycle prompt, Gmail labels, and recurring operation

**Files:**
- Create: `automation/outbound-cycle.md`
- Create: `automation/result.schema.json`
- Create: `docs/operations.md`
- Modify: `src/cli.mjs`
- Test: `tests/automation-contract.test.mjs`

**Interfaces:**
- CLI: `node src/cli.mjs cycle:export --out <file>`, `node src/cli.mjs cycle:ingest --file <file>`.
- Gmail labels: `AI-Outbound/Active`, `AI-Outbound/Handoff`, `AI-Outbound/Quoted`, `AI-Outbound/Suppressed`.

- [ ] **Step 1: Write a failing result-schema contract test**

The test must reject missing source URLs, missing Gmail IDs, unrecognized action IDs, invalid intent enums, duplicate idempotency keys, and result batches larger than the exported action set.

- [ ] **Step 2: Run tests and confirm failure**

Run: `node --test tests/automation-contract.test.mjs`

- [ ] **Step 3: Write the bounded Codex cycle prompt**

The prompt must: export due work; use public-web evidence without uploading Vault originals; use Gmail only for exported actions; classify replies; stop on opt-out/bounce/handoff; write one result file; ingest it; and report counts. It must never create actions outside the exported envelope.

- [ ] **Step 4: Implement result validation and ingestion**

Validate all action IDs and schemas before changing SQLite. Apply the whole result batch transactionally; failed batches make no partial state changes.

- [ ] **Step 5: Install and authorize the Gmail plugin, then create one hourly Codex heartbeat**

The heartbeat checks replies every run and emits new sends only during configured windows and remaining daily budget. Keep status `disabled` until fixture and dry-run checks pass; then enable after the user confirms the connected Gmail identity.

- [ ] **Step 6: Run tests and commit**

```bash
node --test tests/automation-contract.test.mjs
git add automation docs/operations.md src/cli.mjs tests/automation-contract.test.mjs
git commit -m "feat: orchestrate recurring Codex Gmail cycles"
```

### Task 9: End-to-end verification and controlled live launch

**Files:**
- Create: `tests/e2e.test.mjs`
- Create: `scripts/seed-demo.mjs`
- Create: `scripts/verify.mjs`
- Create: `README.md`

**Interfaces:**
- Produces: `npm run verify`, fixture demo mode, dry-run checklist, and live launch checklist.

- [ ] **Step 1: Write the failing end-to-end test**

Seed one authorized product and approved campaign, ingest a sourced company/contact, schedule D0, ingest a Gmail send and buyer price request, assert the sequence stops, assert quote mismatch creates a handoff, export the Obsidian notes, and assert the smart inbox API shows the same handoff.

- [ ] **Step 2: Run the test and confirm failure**

Run: `node --test tests/e2e.test.mjs`

- [ ] **Step 3: Implement deterministic demo seeding and verification**

`scripts/verify.mjs` runs all tests, checks schema version, confirms host binding, scans tracked files for secret-like values, validates Gmail label configuration, and verifies no enabled campaign exceeds 50 new sends per day.

- [ ] **Step 4: Complete fixture and dry-run acceptance**

Run: `npm run verify`.
Expected: all tests pass; fixture cycle completes without external sends; Obsidian output is generated only in a temporary Vault.

- [ ] **Step 5: Perform controlled real-world launch**

Connect the chosen Gmail account, approve one English-language campaign, set daily new sends to `20`, preview all four sequence messages, perform a five-recipient internal/test run, review Gmail threads and SQLite audit events, then raise the budget to `30` only if there are zero duplicate sends and zero unauthorized claims.

- [ ] **Step 6: Commit the verified MVP**

```bash
git add README.md scripts tests/e2e.test.mjs
git commit -m "feat: complete verified AI outbound MVP"
```

