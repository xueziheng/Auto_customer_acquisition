# Slice 4C1 Gmail 投递反馈与 One-click 退订 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用独立 Gmail feedback worker、严格 DSN 解析、整页 PostgreSQL 原子事务和 RFC 8058 one-click 入口，把 hard bounce、soft bounce、隔离、退订、Outreach suppression 与 Sending Identity reputation 接成可重放、可审计、默认拒绝的 Phase 1 闭环。

**Architecture:** Gmail 外部读取仍经 Tool Gateway；handler 只把低敏 `fpg_` 临时句柄写入 Tool Gateway ledger，严格 typed page 留在单次进程的有界 one-shot store，workflow 取走后立即删除。发送前将 route-scoped deterministic Message-ID 与 X-TradeOS correlation 绑定到 MessageAttempt。worker 在网络阶段不持业务事务，随后由一个 session-owned `FeedbackPageUnitOfWork` 复用两个域公开服务的 transaction-bound UoW，整页提交 receipt、quarantine、Outreach/Sending Identity effects 与 cursor；回滚时两域和 cursor 一起回滚。One-click token 只存 nonce SHA-256，匿名 API 始终绑定当前 runtime 的固定 tenant，GET 不写、POST 并发幂等。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2.x async、PostgreSQL 16、Alembic、Gmail REST API、RFC 3464、RFC 8058、pytest、mypy、ruff、GitHub Actions。

## Global Constraints

- 开工前完整读取根目录及目标目录就近 `AGENTS.md`、`HANDBOOK.md`、本计划和已批准规格 `docs/superpowers/specs/2026-08-13-slice4c1-email-feedback-design.md`。
- 所有 Python/test 命令使用 `PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH`，不得用系统旧 Python 冒充门禁。
- 严格 TDD：先写行为测试并取得 genuine RED；import、fixture、Docker、PATH、warning、AppleDouble、event loop 或测试自身错误必须先排除。
- 所有 Gmail HTTP 调用必须经 Tool Gateway handler；OAuth value 只存在于 Gmail Connector 的 secret wrapper/transport 调用栈。workflow、worker、域、模型、日志和数据库都不得获得它。
- Tool Gateway 的 `ToolCallResult.output` 继续只允许低敏标量；不得为反馈页放宽成任意 object、raw MIME 或 headers。新增 read tool 不修改 `pipeline.py` 的全局 stage/安全输出语义。
- 一页最多 100 项；原始 MIME 在 Connector 内硬限制大小并立即丢弃。上层 DTO 不含地址、正文、subject、任意 header、diagnostic text 或 provider payload。
- 关联只允许当前 tenant 下的 deterministic Message-ID 或 `X-TradeOS-Idempotency-V1`；两者同时存在时必须指向同一 Attempt。禁止邮箱+时间模糊匹配。
- route ID 只用于进程外先判定 feedback 是否属于当前单租户 runtime；它不是 tenant ID，不授予权限，也不替代数据库 tenant filter。
- 整页网络读取发生在业务事务外；cursor 行锁、receipt/quarantine、Outreach/Sending Identity effects、domain actions/outbox 和 cursor 更新在同一个 session/commit 内。
- 领域逻辑只经两个域的 `service.py` 公共 contract；workflow/infra 不复制 suppression、warm-up、reputation threshold、state transition 或 action/outbox 算法。
- 普通 one-click 只抑制准确 ContactPoint。Account 抑制仍只允许显式公司级请求或人工确认。
- hard bounce 产生 ContactPoint suppression 和 Sending Identity hard-bounce fact；soft bounce 只保存 receipt，不永久抑制、不自动重试、不升级成 hard bounce。
- duplicate 不重复写 domain effect、action、outbox 或 allow audit。正常 effect 的 allow audit 只在外层 commit 后由 deferred buffer flush；事务内 durable action/receipt 才是 crash-safe 事实。
- outer commit 成功后 audit sink 自身失败不得把已提交页面伪装成可回滚失败或触发重放；记录固定低敏 operational error，继续以 durable action/receipt 为权威。commit 前失败则丢弃全部 buffered allow。
- 每个新表和查询必须 tenant-scoped；匿名 token API 只查 runtime 固定 tenant，不允许 token hash 的跨 tenant 全局查找。
- 新 migration 必须真实执行 `0012 → 0011 → 0012`，并验证 ORM metadata parity、trigger/constraint 行为与跨 tenant 隔离。
- 不实现 Gmail Pub/Sub/watch、自然语言回复分类、FBL 逐封 complaint、Campaign 自动发送/重试、隔离 UI 或 4C2 通知 UI。
- 每个任务只提交列出的文件；新文件使用 `git add --chmod=-x` 形成 100644。提交前检查 AppleDouble/NUL、cached diff 和 staged sensitive scan；普通 commit，不 amend。
- 用户要求每个小任务提交并推送。每个任务 commit 后 push `codex/phase1-implementation`，等待精确 commit SHA 的 GitHub Actions success 后才进入下一任务。

## Implementation Rulings

### Typed read 不改 Tool Gateway pipeline

`email.feedback.fetch` 使用现有 `ToolManifest + ToolHandler` 插件点，LOW/FREE、`IdempotencyRequirement.NONE`，checks 精确为 `tenant → permission`。handler 把 `EmailFeedbackPage` 放入容量为 1 的进程内 one-shot store，返回安全 canonical `provider_ref=fpg_<ULID>`；Tool Gateway ledger 只持久化这个句柄、计数和固定分类。受信 `ToolEmailFeedbackReader` 在 `invoke()` 成功后按句柄 `take()`，任何失败、取消或 ledger completion 异常都清掉未领取 page。进程崩溃只导致安全重拉，不推进 Gmail cursor。

### 关联键与跨租户 route

发送时生成：

```text
Message-ID: <{route_id}.{64-lower-hex}@messages.tradeos.invalid>
X-TradeOS-Idempotency-V1: {route_id}.{64-lower-hex}
```

`route_id` 是 1..32 位小写字母/数字/连字符的非敏感 runtime alias；worker 与发送 runtime 必须精确一致。MessageAttempt 持久化完整安全 correlation 值，二者成对设置、设置后不可改。未知/legacy route 进入 quarantine；route mismatch 固定 `cross-tenant-correlation` + CRITICAL，不做跨 tenant 数据库探测。

### 整页锁序与语义顺序

Connector 保留 provider page ordinal。workflow 先按 ordinal 建 receipt intent、执行 tenant-scoped correlation read，并经 Sending Identity 公共 read DTO取得 domain。为避免跨资源锁环，所有 hard reputation facts 先按 `(sending_domain, sending_identity_id, provider_event_id)` 执行，再按 `(contact_point_id, enrollment_id, provider_event_id)` 执行 Outreach suppression/stop；receipt/quarantine 仍按原 ordinal 写入。两个域效果直到同一个 outer commit 才可见。这样同时满足“provider 顺序可追溯”、现有 Sending Identity 域内锁序和跨 mailbox 并发固定锁序。

### One-click 重试与指纹

nonce 必须是 CSPRNG 32 bytes，数据库只存 SHA-256。prepare 重跑可创建多个映射到同一 Attempt/ContactPoint 的有效 token；未发送的 token自然过期。`EmailSendHandler` 请求 fingerprint 不含随机 token 本身，而包含稳定的 `(tenant, attempt, contact, policy_version, active_key_id, expires_at_policy)` binding fingerprint；因此 crash recovery 不会因重新发行等价 token产生幂等冲突，且不同业务目标仍会冲突。

### Secret capability 分离

strict config 只持 secret reference，不持 secret value。Gmail OAuth reference 只传给 Gmail Connector 的 lazy resolver；unsubscribe HMAC key references 只传给 token key-ring builder。两个组件都用 `repr=False` wrapper，任何上层 DTO/依赖容器均不暴露 raw value。feedback worker 不装配 HMAC signer；anonymous API/token issuer 不装配 Gmail feedback reader。

## File Map

### Shared/domain contracts

- Create then extend `shared/schemas/email_feedback.py`: persistence vocabulary first, then provider-neutral strict feedback page/item/correlation DTO。
- Modify `domains/outreach/models.py`: Attempt correlation fields and typed feedback kind/action facts。
- Modify `domains/outreach/events.py`: remove the stale “three soft bounces become hard” contract text without widening the event registry。
- Modify `domains/outreach/schemas.py`: correlation binding/lookup/target/hard-bounce result DTO。
- Modify `domains/outreach/permissions.py`: exact Attempt/SendingIdentity SYSTEM scopes and feedback actions。
- Modify `domains/outreach/repository.py`: bind/find correlation primitives。
- Modify `domains/outreach/service.py`: bind、resolve、apply hard-bounce public methods。
- Modify `domains/outreach/service_impl.py`: two-phase ABAC、idempotent correlation、suppression/enrollment actions。

### Persistence/transaction boundary

- Create `migrations/versions/0012_email_feedback.py`: Attempt binding columns and four feedback/token tables/guards。
- Modify `infra/db/tables.py`: exact ORM parity。
- Modify `infra/db/repositories/outreach.py`: correlation queries/binding。
- Create `workflows/email_feedback/repository.py`: cursor/receipt/quarantine/token/outer-UoW Protocol。
- Create `infra/db/repositories/email_feedback.py`: tenant-scoped repositories/CAS/append-only token consume。
- Create `infra/db/email_feedback_uow.py`: one-session outer UoW, bound domain UoWs and deferred audit flush。

### Connector/Tool Gateway

- Modify `connectors/gmail/transport.py`: profile/messages/history/raw read transport with 30s timeout and strict limits。
- Modify `connectors/gmail/client.py`: feedback cursor codec、pagination、typed fetch、secret-safe error mapping。
- Create `connectors/gmail/feedback.py`: RFC 3464 parser and provider-event digest。
- Create `tool_gateway/handlers/email_feedback.py`: manifest、handler、one-shot store and trusted reader adapter。
- Modify `tool_gateway/handlers/__init__.py`: explicit exports only。

### Workflow/API/worker

- Create `workflows/email_feedback/flow.py`: whole-page orchestration and deterministic quarantine/effects。
- Create `workflows/email_feedback/unsubscribe.py`: HMAC key ring、token issue/consume service。
- Create `workflows/email_feedback/__init__.py` and `AGENTS.md`.
- Create `apps/api/routers/unsubscribe.py`: anonymous GET/POST fixed contract。
- Modify `apps/api/middleware.py`, `main.py`, `dependencies.py`, `runtime_config.py`, `runtime.py`, `composition/runtime.py`: exact anonymous path, token composition and outbound link integration。
- Create `apps/email_feedback_worker/{__init__.py,AGENTS.md,config.py,health.py,main.py,runtime.py}`: strict runtime、lock、loop、health、signals。
- Create `infra/db/advisory_lock.py`: reusable dedicated-connection lock/PID heartbeat。
- Create `infra/db/schema.py`: shared exact-single-Alembic-head readiness adapter used by API and worker without cross-app imports。

### Operations/docs/demo

- Modify `infra/.env.example` with non-runnable refs/placeholders only。
- Modify `apps/AGENTS.md`, `tool_gateway/AGENTS.md`, `connectors/gmail/AGENTS.md`, `domains/outreach/AGENTS.md`, `workflows/AGENTS.md` as each contract becomes real。
- Modify `docs/architecture/00-overview.md`, `04-tool-gateway.md`, `08-compliance.md`, `09-outreach-campaign.md` to match shipped behavior。
- Create `scripts/demo_email_feedback.py` and `tests/integration/test_demo_email_feedback.py` for offline fake-Gmail + real-Postgres acceptance。

---

## Task 1: Outreach Correlation and Delivery-feedback Domain Contract

**Files:**
- Modify: `domains/outreach/AGENTS.md`
- Modify: `domains/outreach/models.py`
- Modify: `domains/outreach/events.py`
- Modify: `domains/outreach/schemas.py`
- Modify: `domains/outreach/permissions.py`
- Modify: `domains/outreach/repository.py`
- Modify: `domains/outreach/service.py`
- Modify: `domains/outreach/service_impl.py`
- Modify: `tests/unit/test_outreach_contracts.py`
- Modify: `tests/unit/test_outreach_models.py`
- Modify: `tests/unit/test_outreach_enrollment_service.py`
- Modify: `tests/unit/test_outreach_send_claim.py`
- Create: `tests/unit/test_outreach_delivery_feedback.py`

**Interfaces:**

```python
class DeliveryFeedbackKind(str, Enum):
    HARD_BOUNCE = "hard_bounce"
    SOFT_BOUNCE = "soft_bounce"


@dataclass(frozen=True)
class DeliveryCorrelationBinding:
    deterministic_message_id: str
    idempotency_header: str
    route_id: str


@dataclass(frozen=True)
class DeliveryCorrelationLookup:
    deterministic_message_id: str | None = None
    idempotency_header: str | None = None


@dataclass(frozen=True)
class DeliveryFeedbackTarget:
    tenant_id: TenantId
    attempt_id: MessageAttemptId
    enrollment_id: EnrollmentId
    account_id: ProspectAccountId
    contact_point_id: ContactPointId
    sending_identity_id: SendingIdentityId


class OutreachService(Protocol):
    async def bind_delivery_correlation(
        self, tenant_id: TenantId, attempt_id: MessageAttemptId,
        binding: DeliveryCorrelationBinding, *, actor: Actor,
    ) -> MessageAttemptView: ...

    async def resolve_delivery_feedback(
        self, tenant_id: TenantId, lookup: DeliveryCorrelationLookup,
        *, actor: Actor,
    ) -> DeliveryFeedbackTarget | None: ...

    async def apply_hard_bounce(
        self, tenant_id: TenantId, target: DeliveryFeedbackTarget,
        provider_event_id: str, occurred_at: datetime, *, actor: Actor,
    ) -> SuppressionResult: ...
```

- [ ] **Step 1: Update the local binding rules before tests**

Replace the stale Outreach rule “three soft bounces become hard” with the approved 4C1 rule: soft bounce is receipt-only; hard bounce is automatic ContactPoint suppression; account scope requires explicit company-level intent/manual confirmation. Add exact correlation/no fuzzy matching and SYSTEM scope requirements.

- [ ] **Step 2: Write strict DTO/model RED tests**

Cover route/correlation grammar, pairwise Attempt invariant, one-way binding, UTC occurred time, lower-hex provider event ID, type/control/credential-like payload rejection and non-reflective errors.

```python
def test_attempt_correlation_is_pairwise_and_immutable() -> None:
    attempt = _reserved_attempt()
    attempt.bind_delivery_correlation(_binding("route-a"))
    with pytest.raises(InvalidStateTransition):
        attempt.bind_delivery_correlation(_binding("route-b"))
```

- [ ] **Step 3: Write permission RED matrix**

Add `MESSAGE_DELIVERY_BIND`, `DELIVERY_FEEDBACK_RESOLVE`, `HARD_BOUNCE_APPLY`. Prove boss/manager/sales/default deny; SYSTEM may bind one exact Attempt or process one exact SendingIdentity, never unrestricted/multi/empty/mutable scope. Prove two-phase mismatch and wrong tenant produce fixed `PermissionDenied` + exactly-one deny/zero allow.

- [ ] **Step 4: Write service behavior RED tests**

Cover:

- binding idempotent same payload, conflicting payload fixed typed conflict;
- lookup by either key; both keys same target success; differing targets typed ambiguous; missing returns `None`;
- hard bounce re-locks Attempt/Enrollment, verifies exact target and identity, appends one ContactPoint suppression, stops every matching active Enrollment in canonical order, publishes existing `SuppressionAdded`, writes unique actions;
- duplicate provider event/suppression creates no second effect/action/outbox/allow;
- soft kind is not accepted by `apply_hard_bounce`;
- all failures and `resolve_delivery_feedback(...)->None` missing-correlation results produce zero allow; successfully resolved/bound/applied public calls produce exactly-one allow after their UoW exit.

- [ ] **Step 5: Run focused tests and certify genuine RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_outreach_contracts.py \
       tests/unit/test_outreach_models.py \
       tests/unit/test_outreach_delivery_feedback.py -q -W error
```

Expected: only missing enum/DTO/method/scope behavior; no collection or fixture failure.

- [ ] **Step 6: Implement the minimal public contracts and pure model rules**

Attempt correlation fields default `None`; `bind_delivery_correlation()` permits `None/None → valid pair`, same pair no-op, any rewrite rejects. Keep all correlation strings `repr=False` where appropriate and never put them in audit/log messages.

- [ ] **Step 7: Implement service two-phase authorization and domain effects**

Use preauthorize before candidate lookup, full require after resource load, no write before full require, unique action keys derived only from safe typed IDs/digests, and allow audit only after UoW success.

- [ ] **Step 8: Run all affected tests and gates**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_outreach_*.py -q -W error
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
ruff check domains/outreach tests/unit/test_outreach_delivery_feedback.py
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
mypy domains/outreach
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH python3 scripts/check_boundaries.py
git diff --check
```

- [ ] **Step 9: Commit, push, and certify exact SHA**

Commit message: `feat(outreach): add delivery feedback contract`

Run staged sensitive scan and push. Resolve `HEAD_SHA=$(git rev-parse HEAD)`, then resolve `RUN_ID=$(gh run list --commit "$HEAD_SHA" --limit 1 --json databaseId --jq '.[0].databaseId')`; require a non-empty ID, run `gh run watch "$RUN_ID" --exit-status`, and verify `headSha == HEAD_SHA` plus `conclusion == success` before Task 2.

---

## Task 2: Migration 0012, Feedback Persistence, and Shared-session UoW

**Files:**
- Create: `shared/schemas/email_feedback.py`
- Create: `migrations/versions/0012_email_feedback.py`
- Modify: `infra/db/tables.py`
- Modify: `infra/db/repositories/outreach.py`
- Create: `workflows/email_feedback/__init__.py`
- Create: `workflows/email_feedback/AGENTS.md`
- Create: `workflows/email_feedback/repository.py`
- Create: `infra/db/repositories/email_feedback.py`
- Create: `infra/db/email_feedback_uow.py`
- Modify: `tests/integration/test_migrations.py`
- Modify: `tests/integration/test_repositories.py`
- Modify: `tests/integration/test_outreach_repositories.py`
- Create: `tests/integration/test_email_feedback_repositories.py`
- Create: `tests/integration/test_email_feedback_uow.py`

**Schema:**

```text
outreach_message_attempts
  + deterministic_message_id varchar(256) NULL
  + idempotency_header varchar(128) NULL
  CHECK both NULL or both non-NULL
  UNIQUE (tenant_id, deterministic_message_id) WHERE non-NULL
  UNIQUE (tenant_id, idempotency_header) WHERE non-NULL

email_feedback_cursors
  PK (tenant_id, mailbox_alias)
  provider_cursor varchar(32768), version, bootstrap_started_at, last_succeeded_at

email_feedback_receipts
  PK/UNIQUE (tenant_id, mailbox_alias, provider_event_id)
  ordinal, kind, occurred_at, result, nullable safe typed correlation IDs, created_at

email_feedback_quarantines
  PK/FK (tenant_id, mailbox_alias, provider_event_id)
  reason, provider_ref_digest, created_at

unsubscribe_tokens
  PK (tenant_id, nonce_sha256)
  contact_point_id, message_attempt_id, key_id, expires_at, consumed_at, created_at
```

The shared file initially defines strict `EmailFeedbackKind`, `EmailFeedbackResult` and `EmailFeedbackQuarantineReason` enums used by migration/repository contracts. Task 3 extends the same file with transport DTOs rather than creating a second vocabulary.

`mailbox_alias` is a low-cardinality operator label (`[a-z][a-z0-9-]{0,31}`), never an email address. Provider cursor is opaque, bounded and excluded from indexes/logs/audit; its monotonic evidence is the strictly increasing version, not lexical comparison of provider bytes.

- [ ] **Step 1: Write migration shape/roundtrip RED tests**

Assert exact head `0012`, table/column/index/constraint/trigger names, ORM parity, `0012→0011→0012`, and attempt correlation pair/unique rules.

- [ ] **Step 2: Write real PostgreSQL guard RED tests**

Prove receipt/quarantine UPDATE and DELETE fail; cursor version must increment exactly one and cannot delete; token nonce hash is exactly 32 bytes, key ID/typed IDs are valid, `expires_at = created_at + interval '90 days'`, and token may only transition `consumed_at NULL→UTC value` with `created_at <= consumed_at < expires_at`, all other fields unchanged, and cannot delete/revert/rewrite. Add DB-level grammar checks for mailbox alias, lower-hex provider IDs/digests, result/reason vocabularies and paired Attempt correlation fields.

- [ ] **Step 3: Write repository protocol and behavior RED tests**

```python
class FeedbackCursorRepository(Protocol):
    async def get(self, tenant_id: TenantId, mailbox_alias: str) -> FeedbackCursor | None: ...
    async def lock_expected(
        self, tenant_id: TenantId, mailbox_alias: str, expected_cursor: str | None,
    ) -> FeedbackCursor: ...
    async def advance(self, cursor: FeedbackCursor, next_cursor: str, at: datetime) -> None: ...

class FeedbackReceiptRepository(Protocol):
    async def append_if_absent(
        self, receipt: FeedbackReceipt,
    ) -> FeedbackReceiptAppendResult: ...

class UnsubscribeTokenRepository(Protocol):
    async def add(self, token: UnsubscribeTokenRecord) -> None: ...
    async def get_for_update(
        self, tenant_id: TenantId, nonce_sha256: bytes,
    ) -> UnsubscribeTokenRecord | None: ...
    async def mark_consumed(self, record: UnsubscribeTokenRecord, at: datetime) -> bool: ...
```

Cover tenant isolation, same provider ID/hash across tenants, CAS stale cursor, concurrent duplicate append, result roundtrip and no raw payload columns. `FeedbackReceiptAppendResult` must distinguish CREATED, byte-for-byte semantic EXISTING and same-key/different-payload CONFLICT; conflict is corruption and cannot be treated as duplicate or advance cursor.

- [ ] **Step 4: Run genuine RED on real PostgreSQL**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/integration/test_migrations.py \
       tests/integration/test_repositories.py \
       tests/integration/test_outreach_repositories.py \
       tests/integration/test_email_feedback_repositories.py -q -W error
```

- [ ] **Step 5: Implement migration and exact ORM metadata**

Use tenant-first composite keys/indexes. Use fixed trigger functions that raise no customer text. Cursor trigger validates immutable tenant/mailbox/bootstrap and `NEW.version = OLD.version + 1`; opaque cursor value is never logged.

- [ ] **Step 6: Implement tenant-scoped repositories**

Correlation queries always include `tenant_id`; both-key resolution happens by two tenant-filtered queries and rejects differing Attempt IDs. Use explicit `ON CONFLICT` targets, never broad `IntegrityError` parsing.

- [ ] **Step 7: Implement one-session outer UoW**

```python
class FeedbackPageUnitOfWork(Protocol):
    cursors: FeedbackCursorRepository
    receipts: FeedbackReceiptRepository
    quarantines: FeedbackQuarantineRepository
    tokens: UnsubscribeTokenRepository
    outreach: OutreachService
    sending_identities: SendingIdentityService

    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
```

Concrete UoW creates one `AsyncSession`. Transaction-bound domain UoWs wire existing domain repositories/event bus to that session and do not commit/rollback/close. Outer UoW alone commits; on success it flushes copied deferred audit records after commit, on any primary/commit failure it rolls back and discards them. A post-commit audit-sink failure is logged with fixed safe metadata and cannot turn committed work into a retry signal. Cleanup `BaseException` must not replace a primary error; no-primary cancellation propagates.

- [ ] **Step 8: Prove cross-domain atomicity**

Inject failures at Outreach repo, Sending Identity repo, outbox append and outer commit. Assert receipt/quarantine/domain actions/outbox/cursor all absent and audit buffer empty; success commits all and flushes exact allow records once.

- [ ] **Step 9: Run focused/full persistence gates**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/integration/test_email_feedback_repositories.py \
       tests/integration/test_email_feedback_uow.py \
       tests/integration/test_migrations.py \
       tests/integration/test_repositories.py -q -W error
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
ruff check migrations/versions/0012_email_feedback.py infra/db workflows/email_feedback tests/integration/test_email_feedback_*.py
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
mypy infra workflows
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH python3 scripts/check_boundaries.py
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH python3 scripts/scan_sensitive.py
git diff --check
```

- [ ] **Step 10: Commit, push, and certify exact SHA**

Commit message: `feat(email-feedback): add postgres inbox transaction`

Require exact-SHA GitHub Actions success before Task 3.

---

## Task 3: Strict Gmail DSN Connector and Typed Tool Read

**Files:**
- Modify: `shared/schemas/email_feedback.py`
- Create: `connectors/gmail/feedback.py`
- Modify: `connectors/gmail/client.py`
- Modify: `connectors/gmail/transport.py`
- Modify: `connectors/gmail/AGENTS.md`
- Create: `tool_gateway/handlers/email_feedback.py`
- Modify: `tool_gateway/handlers/__init__.py`
- Modify: `tool_gateway/AGENTS.md`
- Create: `tests/unit/test_email_feedback_contracts.py`
- Create: `tests/unit/test_gmail_feedback_parser.py`
- Create: `tests/unit/test_email_feedback_handler.py`
- Modify: `tests/unit/test_tool_gateway_manifest.py`
- Create: `tests/integration/test_gmail_feedback_http.py`
- Create: `tests/integration/test_tool_gateway_email_feedback.py`

**Interfaces:**

```python
class EmailFeedbackKind(str, Enum):
    HARD_BOUNCE = "hard_bounce"
    SOFT_BOUNCE = "soft_bounce"
    UNPARSEABLE = "unparseable"


class EmailFeedbackParseIssue(str, Enum):
    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class EmailFeedbackCorrelation:
    route_id: str | None
    deterministic_message_id: str | None
    idempotency_header: str | None


@dataclass(frozen=True)
class EmailFeedbackItem:
    provider_event_id: str
    provider_ref_digest: str
    ordinal: int
    kind: EmailFeedbackKind
    occurred_at: datetime
    correlation: EmailFeedbackCorrelation | None
    parse_issue: EmailFeedbackParseIssue | None


@dataclass(frozen=True)
class EmailFeedbackPage:
    starting_cursor: str | None
    next_cursor: str
    items: tuple[EmailFeedbackItem, ...]


class ToolEmailFeedbackReader(Protocol):
    async def fetch(
        self, tenant_id: TenantId, mailbox_alias: str,
        cursor: str | None, page_limit: int,
    ) -> EmailFeedbackPage: ...
```

- [ ] **Step 1: Add strict provider-neutral DTO RED tests**

Reject non-enum kind/parse issue, invalid kind↔issue combinations, naive/non-UTC time, invalid lower-hex digest, ordinal outside `0..99`, mutable items, more than 100 items, duplicate provider event ID/ordinal, invalid cursor/type/control chars, raw address/header/body/diagnostic fields and secret-like values. A valid empty page may carry an unchanged cursor (poll no-op) or a newer bootstrap/history cursor. `repr` and errors must not reflect raw inputs.

- [ ] **Step 2: Add RFC 3464 parser RED table**

Use byte fixtures built in tests, not customer samples. Cover:

- `multipart/report; report-type=delivery-status` and strict `message/delivery-status` blocks;
- `Status: 5.x.x → HARD_BOUNCE`, `4.x.x → SOFT_BOUNCE`;
- multiple recipient blocks produce distinct ordinal/digest and deterministic replay;
- missing/invalid/2.x.x/mixed/contradictory status → `UNPARSEABLE`;
- duplicate/conflicting Message-ID/X-TradeOS headers → unparseable;
- no inference from Subject, human diagnostic, recipient address or body text;
- ordinary non-`multipart/report` mail returns no feedback item and only advances provider cursor; a `multipart/report` candidate with unsupported report type becomes `UNPARSEABLE/UNSUPPORTED`;
- malformed claimed-report MIME with a provider message ref still becomes one deterministic unparseable item; missing safe provider ref makes the whole fetch fail.

```python
def test_provider_event_id_is_digest_of_message_ref_and_block_ordinal() -> None:
    first = parse_delivery_status(_dsn(blocks=("5.1.1", "4.2.2")), "gmail-safe-1")
    replay = parse_delivery_status(_dsn(blocks=("5.1.1", "4.2.2")), "gmail-safe-1")
    assert [item.provider_event_id for item in first] == [
        item.provider_event_id for item in replay
    ]
    assert len(set(item.provider_event_id for item in first)) == 2
```

- [ ] **Step 3: Add Gmail cursor/transport RED tests with a local HTTP server**

Freeze cursor v1 as a base64url encoded strict internal state, never caller-controlled query fragments:

```text
bootstrap: fixed_after_epoch + boundary_history_id + optional messages.nextPageToken
history: start_history_id + optional history.nextPageToken
both modes: bounded pending_message_refs + pending_block_offset
```

Initial fetch first freezes `after_epoch = floor(now_utc - 30 days)` and captures `users.getProfile.historyId`, then scans `messages.list q=after:{fixed_after_epoch}` on every bootstrap page; the cutoff never slides while paginating. After the final bootstrap page it switches to the captured boundary. Incremental fetch uses `users.history.list?historyTypes=messageAdded`; final next cursor uses response `historyId`. Deduplicate repeated message IDs while preserving first provider order. Convert each provider list response to a bounded pending-ref queue. If multi-recipient blocks would cross `page_limit`, encode the remaining refs and block offset in the next opaque cursor, re-fetch that immutable Gmail message next cycle, and skip already emitted ordinals. This keeps every typed page within `1..100` feedback items without dropping a block or persisting raw MIME. Cursor bytes are capped at 32 KiB and never logged.

Cover 401/403, 429 with integer `Retry-After 1..3600`, 5xx, timeout/network, 404 stale history, malformed/oversized JSON/raw MIME, redirect rejection, max 100 and exact 30-second timeout.

- [ ] **Step 4: Run connector genuine RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_email_feedback_contracts.py \
       tests/unit/test_gmail_feedback_parser.py \
       tests/integration/test_gmail_feedback_http.py -q -W error
```

- [ ] **Step 5: Implement DTO, cursor codec, transport and parser**

Use `email.parser.BytesParser` with a strict policy and byte cap. Extract only the two approved correlation headers from the encapsulated original-message header section, normalize surrounding `<...>` only for Message-ID, then discard MIME bytes. Never preserve Final-Recipient, Diagnostic-Code, subject or body. Replace the old free-`dict` `fetch_new_messages`/`parse_bounce` stubs and legacy `BounceEvent`; do not leave two competing connector contracts.

- [ ] **Step 6: Add Tool Gateway read plugin RED tests**

Assert manifest:

```python
assert manifest.tool_id == "email.feedback.fetch"
assert manifest.risk_level is RiskLevel.LOW
assert manifest.idempotency is IdempotencyRequirement.NONE
assert manifest.checks == ("tenant", "permission")
assert manifest.required_permissions == ("email:feedback_read",)
```

Prove unauthorized/wrong tenant never configures Connector or hits HTTP; ledger only contains `fpg_` handle and safe counts; one-shot store capacity is one; `take()` deletes; wrong/duplicate handle fails closed; cancellation/failure clears page; raw DTO values never appear in tool_call tables/logs/exceptions.

- [ ] **Step 7: Implement handler and trusted reader without changing pipeline**

```python
class FeedbackPageSlot:
    def put(self, page: EmailFeedbackPage) -> str: ...
    def take(self, handle: str) -> EmailFeedbackPage: ...
    def discard_all(self) -> None: ...


class EmailFeedbackFetchHandler:
    async def prepare(self, ctx: ToolCallContext, preflight: object | None) -> PreparedToolCall: ...
    async def execute(self, tenant_id: TenantId, prepared: PreparedToolCall) -> Mapping[str, SafeScalar]: ...
```

`prepare` audit projection contains only mailbox alias, page limit and `has_cursor`; fingerprint covers tenant/mailbox/cursor digest/limit, never raw cursor. `execute` returns only `provider_ref=fpg_...`.

- [ ] **Step 8: Prove full real Tool Gateway path**

Run local fake Gmail HTTP + real PostgreSQL Tool Gateway ledger. Assert typed page reaches reader, token appears only as Authorization at fake server, and DB/log/exception capture excludes token, MIME, address, correlation headers and fake DSN marker.

- [ ] **Step 9: Run Task 3 gates**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_gmail_feedback_parser.py \
       tests/unit/test_email_feedback_handler.py \
       tests/integration/test_gmail_feedback_http.py \
       tests/integration/test_tool_gateway_email_feedback.py -q -W error
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
ruff check shared/schemas/email_feedback.py connectors/gmail tool_gateway/handlers tests/unit/test_*feedback*.py tests/integration/test_*feedback*.py
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
mypy shared connectors tool_gateway
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH python3 scripts/check_boundaries.py
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH python3 scripts/scan_sensitive.py
git diff --check
```

- [ ] **Step 10: Commit, push, and certify exact SHA**

Commit message: `feat(gmail): add typed delivery feedback reader`

Require exact-SHA GitHub Actions success before Task 4.

---

## Task 4: Whole-page Feedback Workflow and Atomic Domain Effects

**Files:**
- Modify: `workflows/email_feedback/AGENTS.md`
- Create: `workflows/email_feedback/flow.py`
- Modify: `workflows/email_feedback/__init__.py`
- Modify: `workflows/AGENTS.md`
- Modify: `infra/db/email_feedback_uow.py`
- Create: `tests/unit/test_email_feedback_flow.py`
- Create: `tests/integration/test_email_feedback_flow.py`
- Modify: `tests/unit/test_outreach_delivery_feedback.py`
- Modify: `tests/unit/test_sending_identity_reputation.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class FeedbackPageResult:
    processed: int
    duplicates: int
    hard_bounces: int
    soft_bounces: int
    quarantined: int
    next_cursor: str


class FeedbackPageProcessor:
    async def process(
        self,
        tenant_id: TenantId,
        mailbox_alias: str,
        configured_identity_id: SendingIdentityId,
        expected_cursor: str | None,
        page: EmailFeedbackPage,
    ) -> FeedbackPageResult: ...
```

The processor consumes the shared `EmailFeedbackQuarantineReason` vocabulary created in Task 2; it must not define a workflow-local synonym.

- [ ] **Step 1: Write decision-table RED tests**

Cover new/duplicate hard, new/duplicate soft, unparseable, missing one/both keys, both keys same/different target, route mismatch, wrong configured identity, wrong tenant row returned by malicious fake, same provider-event ID with changed payload (whole-page failure), provider ordinal stability, an empty page with newer cursor, and an empty unchanged-cursor no-op that does not increment version/audit.

- [ ] **Step 2: Write actor/action/audit RED matrix**

Inject actor factories and strict authorizers. Prove correlation read and each actual domain mutation receive exact tenant/action/scope/resource. Any pre/full deny yields zero receipt/domain/cursor/allow. Duplicate receipt calls neither domain and writes zero allow.

- [ ] **Step 3: Write real PostgreSQL happy-path RED**

Seed two sent Attempts and active Enrollments. Process one page containing hard, soft and deterministic malformed:

- hard: receipt applied, exact ContactPoint suppression, all matching active enrollments stopped, hard-bounce reputation event with `dedup_key=provider_event_id`, state action/outbox only if threshold changes;
- soft: receipt recorded only;
- malformed: receipt + quarantine, no business effect;
- cursor advances once after every item;
- all persisted correlation IDs belong to current tenant and no address/MIME/header is stored.

- [ ] **Step 4: Write failure/rollback RED matrix**

Inject cursor stale/lost, receipt insert, correlation repo, suppression, sending reputation, outbox and commit failures. After each, assert old cursor and zero partial writes in all touched tables. Repeat with deadlock/serialization exception sentinels and prove original typed exception propagates for worker retry classification.

- [ ] **Step 5: Write replay and concurrency RED tests**

Run the same page 20-way and after reconstructed processor/UoW. Assert one receipt per provider event, one quarantine, one suppression, one reputation fact, correct conditional outbox and one cursor version step. Add two mailbox processors affecting contacts in inverse provider order and assert fixed resource lock ordering/no deadlock.

- [ ] **Step 6: Run genuine RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_email_feedback_flow.py \
       tests/integration/test_email_feedback_flow.py -q -W error
```

- [ ] **Step 7: Implement pure classification and pre-resolution**

Process receipt intents by provider ordinal; check route before DB; append duplicate-safe receipt first within the uncommitted transaction; resolve only new correlatable items. Convert only approved deterministic problems to quarantine. Buffer the fixed cross-route CRITICAL record and emit it only after its quarantine commit, with tenant/mailbox/reason and no route/correlation/provider value. Provider/config/DB failures must escape and roll back the page.

- [ ] **Step 8: Implement stable effect application**

Use exact read actors to load Sending Identity public views and derive the stable domain order. Apply every hard `record_delivery_event(DeliveryEventType.HARD_BOUNCED)` first in `(domain, identity, provider_event)` order, then apply every Outreach `apply_hard_bounce` in `(contact, enrollment, provider_event)` order, using separate exact write actors. The shared outer transaction preserves atomic visibility despite the phase order. Soft never calls either write service.

- [ ] **Step 9: Advance cursor last and rely on outer UoW commit**

Require `page.starting_cursor == expected_cursor`; lock cursor and re-check expected value; advance only after all item operations and only when `next_cursor != expected_cursor`. An unchanged empty page is a transaction/audit no-op. Return counts only after outer context exits successfully so callers cannot report uncommitted success.

- [ ] **Step 10: Run Task 4 gates**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_email_feedback_flow.py \
       tests/integration/test_email_feedback_flow.py \
       tests/unit/test_outreach_delivery_feedback.py \
       tests/unit/test_sending_identity_reputation.py -q -W error
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
ruff check workflows/email_feedback infra/db/email_feedback_uow.py tests/unit/test_email_feedback_flow.py tests/integration/test_email_feedback_flow.py
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
mypy workflows infra
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH python3 scripts/check_boundaries.py
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH python3 scripts/scan_sensitive.py
git diff --check
```

- [ ] **Step 11: Commit, push, and certify exact SHA**

Commit message: `feat(email-feedback): process pages atomically`

Require exact-SHA GitHub Actions success before Task 5.

---

## Task 5: One-click Token, Anonymous API, and Outbound Binding

**Files:**
- Create: `workflows/email_feedback/unsubscribe.py`
- Modify: `workflows/email_feedback/repository.py`
- Modify: `workflows/email_feedback/__init__.py`
- Create: `infra/secrets.py`
- Modify: `tool_gateway/handlers/email_send.py`
- Modify: `connectors/gmail/client.py`
- Create: `apps/api/routers/unsubscribe.py`
- Modify: `apps/api/middleware.py`
- Modify: `apps/api/dependencies.py`
- Modify: `apps/api/main.py`
- Modify: `apps/api/runtime_config.py`
- Modify: `apps/api/runtime.py`
- Modify: `apps/api/composition/runtime.py`
- Modify: `infra/.env.example`
- Create: `tests/unit/test_unsubscribe_tokens.py`
- Create: `tests/unit/test_unsubscribe_router.py`
- Modify: `tests/unit/test_email_send_handler.py`
- Modify: `tests/unit/test_gmail_connector.py`
- Modify: `tests/unit/test_api_app.py`
- Modify: `tests/unit/test_api_runtime.py`
- Modify: `tests/integration/test_manual_email_send_api.py`
- Modify: `scripts/demo_gmail_manual_send.py`
- Modify: `tests/integration/test_demo_gmail_manual_send.py`
- Create: `tests/integration/test_unsubscribe_api.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class UnsubscribeKeyReference:
    key_id: str
    secret_ref: str = field(repr=False)


@dataclass(frozen=True)
class UnsubscribeLink:
    tenant_id: TenantId
    attempt_id: MessageAttemptId
    contact_point_id: ContactPointId
    url: str = field(repr=False)
    active_key_id: str
    policy_version: str


class UnsubscribeLinkProvider(Protocol):
    async def build(
        self, tenant_id: TenantId, preflight: MessageSendPreflight
    ) -> UnsubscribeLink: ...


class UnsubscribeService(Protocol):
    async def issue(
        self, tenant_id: TenantId, preflight: MessageSendPreflight
    ) -> UnsubscribeLink: ...

    async def consume(self, opaque_token: str) -> bool: ...


class UnsubscribeMetrics(Protocol):
    def increment(self, result: Literal["valid", "expired", "invalid"]) -> None: ...
```

- [ ] **Step 1: Write key-ring/token RED tests**

Cover active + verification-only key rotation, key ID `[a-z0-9-]{1,32}`, minimum 32-byte key, duplicate/unknown IDs, 32-byte CSPRNG nonce, unpadded base64url canonical form, HMAC domain separation, constant-time signature check, token length/control chars and `repr`/exception/log non-disclosure. Inject a nonce-hash collision and require bounded regeneration with a fresh CSPRNG nonce; after the fixed attempt cap, fail safely without overwriting another mapping. The metrics sink receives only the fixed result vocabulary and never token/key/contact labels.

```python
def test_token_contains_no_business_identifier() -> None:
    token = service.issue_for(
        _record(
            contact_id="cp_00000000000000000000000001",
            attempt_id="mat_00000000000000000000000001",
        )
    )
    assert "cp_" not in token
    assert "mat_" not in token
    assert "tn_" not in token
```

- [ ] **Step 2: Write expiry/concurrency RED tests**

Freeze issue expiry to `issued_at + timedelta(days=90)` and validity to `now < expires_at`; exact expiry is expired. Real PostgreSQL 20-way consume must produce one `True`, nineteen `False`, one ContactPoint suppression, canonical stopped enrollments, one consumed transition and no duplicate actions/outbox/allow. Unknown/expired/reused all return `False` without distinguishable logs.

- [ ] **Step 3: Implement key ring and token service**

Sign `b"tradeos-unsubscribe-v1\0" + tenant_id + b"\0" + key_id + b"\0" + nonce`; key-ID substitution must invalidate the signature even if two configured IDs accidentally reference equal bytes. Lookup token row only under the configured runtime tenant after signature verification. Issue stores `sha256(nonce).digest()`, never nonce/token/signature. Consume uses the outer FeedbackPage UoW, calls transaction-bound Outreach `add_suppression` with exact ContactPoint SYSTEM actor and `SuppressionReason.UNSUBSCRIBE`, using the nonce-hash lower-hex as the safe source/idempotency reference, then marks consumed in the same commit.

- [ ] **Step 4: Write anonymous router RED tests**

Assert:

- exact `GET /unsubscribe/{token}` is the only tenant-header bypass; nearby/nested paths still require tenant assertion;
- GET never calls token repository and returns fixed Chinese HTML without echoing token, external resource, script, email or IDs;
- headers: `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, strict CSP and no `Set-Cookie`;
- POST accepts only exact `Content-Type: application/x-www-form-urlencoded` and body `List-Unsubscribe=One-Click` within fixed byte limits;
- valid, duplicate, unknown, expired, malformed, oversized and bad-body POST all return identical empty 204 externally;
- cancellation propagates; internal DB/transient failure is a fixed 503, not a false 204 success, and leaks no token/DSN/exception chain.
- one-click routes use `include_in_schema=False`; OpenAPI does not advertise bearer capability paths or add the normal authenticated 400/403 contract to them.

- [ ] **Step 5: Implement router and narrowly-scoped middleware bypass**

`TenantAssertionMiddleware` receives an immutable exact anonymous route matcher, not a broad prefix wildcard. Only GET/POST with one token segment can bypass; router performs all capability validation. Read request stream with a hard cap rather than unbounded `request.body()`.

- [ ] **Step 6: Write outbound binding RED tests**

Prove `EmailSendHandler.prepare`:

1. obtains preflight;
2. creates route-scoped deterministic Message-ID/header;
3. calls Outreach public `bind_delivery_correlation` before Gmail;
4. issues an unsubscribe link for the same Attempt/ContactPoint;
5. fingerprints the stable binding, not random nonce/token;
6. produces current Gmail headers exactly once.

The handler verifies every returned binding ID against preflight before accepting the URL and computes the stable fingerprint itself from those typed fields; it never trusts provider-supplied fingerprint bytes. Same preflight with a newly issued equivalent token must retain the same request fingerprint; changed Attempt/Contact/key ID/policy version must change it. Correlation bind or token issue failure must prevent Gmail.

- [ ] **Step 7: Adapt strict runtime config and secret capabilities**

Add:

```text
TRADEOS_EMAIL_FEEDBACK_ROUTE_ID
TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID
TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON
```

The JSON value maps key ID to environment secret-reference name, never raw key. Pydantic/config rejects unknown fields inside JSON, duplicate IDs, invalid refs/types and missing active key. `EnvironmentSecretResolver` reads only an explicitly requested safe env name and returns the value directly to the consuming secret wrapper; it never logs/reprs/enumerates environment values.

- [ ] **Step 8: Compose token issue/consume independently of Gmail feedback reader**

API runtime builds token key ring/service and anonymous router dependency. Manual send composition receives a Gmail Connector capability/material providers, not raw OAuth value. Feedback worker code is not imported. Zero-arg/unconfigured app keeps fixed GET page and constant 204 for unknown POST but cannot mutate data.

- [ ] **Step 9: Run genuine RED/GREEN and affected API tests**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_unsubscribe_tokens.py \
       tests/unit/test_unsubscribe_router.py \
       tests/unit/test_email_send_handler.py \
       tests/integration/test_unsubscribe_api.py \
       tests/integration/test_manual_email_send_api.py -q -W error
```

- [ ] **Step 10: Run Task 5 gates**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
ruff check workflows/email_feedback/unsubscribe.py apps/api tool_gateway/handlers/email_send.py infra/secrets.py tests/unit/test_unsubscribe_*.py tests/integration/test_unsubscribe_api.py
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
mypy workflows apps tool_gateway infra
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH python3 scripts/check_boundaries.py
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH python3 scripts/scan_sensitive.py
git diff --check
```

- [ ] **Step 11: Commit, push, and certify exact SHA**

Commit message: `feat(api): add one-click unsubscribe capability`

Require exact-SHA GitHub Actions success before Task 6.

---

## Task 6: Email Feedback Worker Runtime, Lock, Backoff, and Health

**Files:**
- Create: `infra/db/advisory_lock.py`
- Create: `infra/db/schema.py`
- Modify: `apps/api/runtime.py`
- Create: `apps/email_feedback_worker/__init__.py`
- Create: `apps/email_feedback_worker/AGENTS.md`
- Create: `apps/email_feedback_worker/config.py`
- Create: `apps/email_feedback_worker/health.py`
- Create: `apps/email_feedback_worker/main.py`
- Create: `apps/email_feedback_worker/runtime.py`
- Modify: `apps/AGENTS.md`
- Modify: `infra/.env.example`
- Create: `tests/unit/test_email_feedback_worker.py`
- Create: `tests/unit/test_email_feedback_worker_config.py`
- Modify: `tests/unit/test_api_runtime.py`
- Create: `tests/integration/test_email_feedback_worker.py`

**Runtime contracts:**

```python
@dataclass(frozen=True)
class EmailFeedbackWorkerConfig:
    tenant_id: TenantId
    mailbox_alias: str
    sending_identity_id: SendingIdentityId
    feedback_route_id: str
    poll_interval_seconds: int = 30
    page_limit: int = 100
    enabled: bool = False
    health_port: int = 8092


class WorkerRunStatus(str, Enum):
    STARTED = "started"
    DISABLED = "disabled"
    LOCK_NOT_ACQUIRED = "lock_not_acquired"
    LOCK_LOST = "lock_lost"


@dataclass(frozen=True)
class EmailFeedbackRuntime:
    lock_engine: AsyncEngine
    reader: ToolEmailFeedbackReader
    processor: FeedbackPageProcessor
    config: EmailFeedbackWorkerConfig
    health: EmailFeedbackHealthState


class EmailFeedbackMetricName(str, Enum):
    CURSOR_LAG = "cursor_lag"
    PROCESSED = "processed"
    DUPLICATE = "duplicate"
    QUARANTINED = "quarantined"
    HARD_BOUNCE = "hard_bounce"
    SOFT_BOUNCE = "soft_bounce"
    PAGE_ROLLBACK = "page_rollback"
    CONSECUTIVE_FAILURE = "consecutive_failure"


class EmailFeedbackMetrics(Protocol):
    def record(
        self, name: EmailFeedbackMetricName, *, tenant_id: TenantId, mailbox_alias: str,
        value: int = 1,
        kind: EmailFeedbackKind | None = None,
        result: EmailFeedbackResult | None = None,
    ) -> None: ...
```

- [ ] **Step 1: Write strict config RED tests**

Config reads required `DATABASE_URL` as `SecretStr`, OAuth secret ref, exact tenant/mailbox alias/identity/route, enabled flag, health port, interval and page limit. Reject bool-as-int, unknown/blank/control values, interval outside `5..3600`, limit outside `1..100`, invalid typed IDs and secret-like aliases. Bootstrap remains fixed 30 days and has no env override. HMAC key fields must be rejected/ignored as unknown worker config.

- [ ] **Step 2: Write reusable advisory lock RED tests on real PostgreSQL**

Acquire with one parameterized SELECT returning acquired + backend PID, commit immediately, keep dedicated connection across lifecycle, heartbeat same PID before and after network fetch, normal unlock+commit. Competing worker cannot fetch. `pg_terminate_backend` during fetch produces `LOCK_LOST`, discards page and performs zero processing/cursor writes; no ResourceWarning.

- [ ] **Step 3: Implement advisory lock boundary**

Derive signed int64 key from length-prefixed SHA-256 of tenant+mailbox alias. Never use Python randomized `hash()`. Lock helper owns/cleans connection; `CancelledError` propagates but finally closes. Unlock failure logs fixed Chinese phase/error type only.

Extract the existing exact-single-Alembic-head probe from `apps/api/runtime.py` into `infra/db/schema.py` so both apps depend downward on one safe adapter. Preserve the API's fixed error mapping/tests; the worker must not import `apps.api` or copy its implementation.

- [ ] **Step 4: Write worker cycle/backoff RED tests**

One cycle:

1. heartbeat;
2. read committed cursor in a short session;
3. fetch typed page via Tool Gateway outside business transaction;
4. heartbeat again;
5. process page atomically;
6. reset backoff only after commit.

Retry sequence is exactly `5,10,20,40,80,160,300,300...`; valid provider Retry-After integer `1..3600` overrides one wait, invalid/absent/out-of-range uses default. Inject clock/wait; never real sleep in tests.

- [ ] **Step 5: Write failure classification and safe-log RED tests**

Provider auth/network/429/5xx, cursor CAS, deadlock/serialization/commit and registry/config failures are retryable page failures with old cursor. Deterministic quarantine is success. Unknown programming error is fixed unexpected category and does not include exception text. Capture complete LogRecord/exception context and assert absence of OAuth, mailbox address, provider ID, Message-ID, token, DSN and fake customer strings. Freeze metric names for cursor lag, processed, duplicate, quarantined, hard/soft, page rollback and consecutive failure; reject arbitrary labels and prove the sink only sees tenant, mailbox alias, kind and result category.

- [ ] **Step 6: Write signal/cancellation RED tests**

SIGINT/SIGTERM set only stop event. If set during fetch/process, current page finishes or rolls back, no next cycle starts, then lock/session/HTTP/engine/health server close. External cancellation propagates after cleanup; cleanup BaseException never replaces primary.

- [ ] **Step 7: Write health RED tests**

The health server binds fixed `0.0.0.0:<strict health_port>`, disables access logs, and exposes only the two fixed paths. `/health/live` is live after server start. `/health/ready` is 503 until strict config, exact Alembic head, DB probe and handler registry succeed; then 200. Provider failure changes a low-cardinality `provider=degraded` field but keeps ready 200. Responses/logs expose no config value or exception; all other paths/methods return fixed 404/405.

- [ ] **Step 8: Run worker genuine RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_email_feedback_worker_config.py \
       tests/unit/test_email_feedback_worker.py \
       tests/integration/test_email_feedback_worker.py -q -W error
```

- [ ] **Step 9: Implement runtime composition**

Create engine/session factory, schema assertion, environment secret resolver → Gmail Connector, typed read manifest/handler/Tool Gateway ledger, `FeedbackPageProcessor`, exact domain actor factories and outer UoW. Use a dedicated read Gateway whose configured checks mapping contains only `tenant` and `permission`; do not reuse the API send Gateway or its suppression/approval/idempotency/rate-limit completion hooks. Register no send handler in worker. Enabled false starts health in disabled/not-ready mode and never obtains lock/fetches.

- [ ] **Step 10: Implement lifecycle loop and entrypoint**

`main()` maps startup/config to fixed nonzero codes, disables access logs, installs signals, and emits only fixed Chinese messages + safe dimensions. `RuntimeFactory` context owns/disposes Connector/HTTP, engine and health server. Zero-arg production factory reads environment; injected factory supports tests without monkeypatching global state.

- [ ] **Step 11: Run Task 6 gates**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_email_feedback_worker*.py \
       tests/integration/test_email_feedback_worker.py -q -W error
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
ruff check apps/email_feedback_worker infra/db/advisory_lock.py tests/unit/test_email_feedback_worker*.py tests/integration/test_email_feedback_worker.py
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
mypy apps infra
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH python3 scripts/check_boundaries.py
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH python3 scripts/scan_sensitive.py
git diff --check
```

- [ ] **Step 12: Commit, push, and certify exact SHA**

Commit message: `feat(worker): add gmail feedback poller`

Require exact-SHA GitHub Actions success before Task 7.

---

## Task 7: Process Acceptance, Offline Demo, Documentation, and Final Gates

**Files:**
- Create: `scripts/demo_email_feedback.py`
- Create: `tests/integration/test_demo_email_feedback.py`
- Modify: `apps/AGENTS.md`
- Modify: `tool_gateway/AGENTS.md`
- Modify: `connectors/gmail/AGENTS.md`
- Modify: `domains/outreach/AGENTS.md`
- Modify: `workflows/AGENTS.md`
- Modify: `docs/architecture/00-overview.md`
- Modify: `docs/architecture/04-tool-gateway.md`
- Modify: `docs/architecture/08-compliance.md`
- Modify: `docs/architecture/09-outreach-campaign.md`
- Modify: `infra/.env.example`

- [ ] **Step 1: Write subprocess/demo genuine RED**

Run `scripts/demo_email_feedback.py` against the migrated testcontainer using `sys.executable` from `tradeos-py312`. Initial RED must be file-missing only. The demo uses an in-process fake Gmail transport/secret resolver; it must never call external network and never read `.env`.

- [ ] **Step 2: Implement minimal offline demo through real public composition**

Seed one tenant, SendingIdentity, Campaign/Enrollment/MessageAttempt through real services/repositories as existing demos permit; bind deterministic correlation; fake a page with hard + soft + malformed; process via real Tool Gateway/outer UoW; issue and consume one token via public service. Direct ORM seed is limited to prerequisite employee/contact facts for which no public creation API exists; never insert feedback/suppression/reputation/action/outbox rows directly.

Successful stdout is one safe JSON object containing only tenant/typed IDs, fixed result counts, cursor version and final enum states. It must not include domain/address/message/provider/token/DSN. Failure stderr is one fixed Chinese line, stdout empty, no traceback.

- [ ] **Step 3: Prove process-level behavior and DB readback**

Integration test asserts:

- two independent demo runs in the same database both return 0 with distinct tenant/typed IDs and no cross-run mutation;
- exact receipt/quarantine/cursor/suppression/enrollment/reputation/action/outbox/token-consumed rows match stdout counts;
- replay changes duplicate count only, not effects;
- invalid DSN and invalid secret reference return nonzero fixed safe message;
- full stdout/stderr/DB/log capture excludes all injected secret/customer markers.

- [ ] **Step 4: Add real worker subprocess acceptance with fake Gmail server**

Launch migrated PostgreSQL + local fake Gmail HTTP + actual `python -m apps.email_feedback_worker.main`. Verify fixed 30-day bootstrap, cursor persistence, next incremental history request, hard/soft/quarantine DB effects, duplicate restart, SIGTERM page completion and no child process/port/temp residue. Provider 429/degraded must keep health ready and honor bounded Retry-After.

- [ ] **Step 5: Synchronize rules and architecture docs**

Document only shipped behavior:

- process inventory includes email feedback worker (update stale “七个进程” count precisely);
- Tool Gateway typed read uses safe ledger handle + ephemeral page and does not weaken output safety;
- Gmail connector strict RFC 3464/no natural-language inference/no raw MIME persistence;
- Outreach soft bounce is receipt-only and one-click is ContactPoint scope;
- whole-page transaction, cursor/quarantine recovery, route-scoped deterministic correlation;
- RFC 8058 GET/POST behavior, token/key rotation, release/rollback/degraded runbook;
- 4C2 UI remains explicitly unimplemented.

Do not add invented equivalence rules or future Campaign retry policy. Add no missing top-level `AGENTS.md` outside the approved touched directories.

- [ ] **Step 6: Run focused Slice 4C1 acceptance**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_email_feedback_contracts.py \
       tests/unit/test_gmail_feedback_parser.py \
       tests/unit/test_email_feedback_flow.py \
       tests/unit/test_unsubscribe_tokens.py \
       tests/unit/test_unsubscribe_router.py \
       tests/unit/test_email_feedback_worker.py \
       tests/integration/test_gmail_feedback_http.py \
       tests/integration/test_tool_gateway_email_feedback.py \
       tests/integration/test_email_feedback_repositories.py \
       tests/integration/test_email_feedback_uow.py \
       tests/integration/test_email_feedback_flow.py \
       tests/integration/test_unsubscribe_api.py \
       tests/integration/test_email_feedback_worker.py \
       tests/integration/test_demo_email_feedback.py -q -W error
```

- [ ] **Step 7: Run mandatory full gates once, without duplicate long runs**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH make check
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/integration -q -W error
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH python3 scripts/check_boundaries.py
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH python3 scripts/scan_sensitive.py
git diff --check
```

If OpenAPI changes, also run deterministic export twice and `apps/web` `typecheck/lint/test/build`; no generated API drift is acceptable.

- [ ] **Step 8: Perform final security and scope audit**

Inspect exact diff/allowlist and query real DB for forbidden columns/values. Search captured logs, exception chains, stdout/stderr and staged patch for Gmail OAuth, HMAC values, injected email/body/token/DSN/provider payload markers. Verify all new files UTF-8/no NUL and index mode 100644; delete only task-owned AppleDouble/build/temp artifacts.

- [ ] **Step 9: Commit, push, and certify final exact SHA**

Commit message: `docs(email-feedback): add acceptance demo and runbook`

```bash
git push origin codex/phase1-implementation
HEAD_SHA=$(git rev-parse HEAD)
gh run list --commit "$HEAD_SHA" --json databaseId,headSha,status,conclusion,url
RUN_ID=$(gh run list --commit "$HEAD_SHA" --limit 1 --json databaseId --jq '.[0].databaseId')
test -n "$RUN_ID"
gh run watch "$RUN_ID" --exit-status
gh run view "$RUN_ID" --json headSha,conclusion,url,jobs
```

Do not declare Slice 4C1 complete unless exact `headSha` matches, conclusion is `success`, every job/step is green, the branch is 0 ahead/0 behind after push and the tracked worktree/index are clean.

## Final Acceptance Checklist

- [ ] hard bounce under exact same-tenant correlation creates one ContactPoint suppression, stops matching active enrollments and records one Sending Identity hard-bounce fact.
- [ ] soft bounce creates receipt only; no permanent suppression, no hard-bounce fact and no automatic retry.
- [ ] unparseable/missing/ambiguous/cross-route feedback creates safe quarantine, no business effects; cross-route also emits fixed CRITICAL without raw values.
- [ ] duplicate, 20-way concurrency, crash/restart and two workers cannot duplicate effects or regress cursor.
- [ ] any retryable page failure leaves old cursor and zero partial Outreach/Sending Identity effects.
- [ ] Gmail OAuth never leaves Connector/transport; Tool Gateway ledger never stores page/raw MIME/header/address/token/exception text.
- [ ] one-click GET never mutates; valid POST mutates exactly once; invalid/expired/reused token is indistinguishable empty 204; DB/transient failure is safely retryable rather than false success.
- [ ] token nonce/signature/raw key never persists or logs; key rotation and exact 90-day boundary are covered.
- [ ] all new queries/keys are tenant-scoped and malicious wrong-tenant rows fail closed with zero mutation.
- [ ] advisory lock loss, SIGTERM, cancellation and cleanup preserve primary errors and release all resources.
- [ ] migrations roundtrip, focused tests, full `make check`, integration `-W error`, boundary/sensitive/diff gates and exact-SHA GitHub Actions are green.
