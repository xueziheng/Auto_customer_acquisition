# Handbook Phase 1 Slice 4 Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 `HANDBOOK.md` Slice 4 的真实生产闭环：员工从 Web 发出单封邮件，认证、抑制、幂等、额度、反馈、投诉、信誉熔断和站内/邮件通知均可运行并有证据。

**Architecture:** 保留现有 SendingIdentity、Outreach、Tool Gateway、Gmail、Outbox 和 workflow engine。新增 PostgreSQL 通知任务/站内通知、独立 notification worker、transactional Gmail 工具、DNS authentication workflow、ARF complaint 回流与三个内部 Web 页面；外部网络动作仍只经 Tool Gateway → Connector。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2.x、Alembic、PostgreSQL 16、Vue 3、TypeScript 5、Vite、Ant Design Vue、Vitest、Python Playwright、GitHub Actions。

## Global Constraints

- 权威设计：`docs/superpowers/specs/2026-08-14-handbook-phase1-slice4-completion-design.md`。
- 所有 Python 命令使用 `PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH`。
- 模型和 Agent 永不接触 OAuth、邮箱地址目录、DNS provider 密钥、Cookie、DSN 或原始 MIME。
- 所有外部动作只能走 `tool_gateway/`；Connector 不写业务表，领域不发网络请求。
- 所有表和查询强制 `tenant_id`；所有资源写采用 API first gate + 领域第二次 ABAC。
- 拒绝审计立即写；允许审计只在业务事务提交后写。
- 发送正文、主题、邮箱地址、原始 MIME、异常消息和 provider 原始响应不得进入日志、Outbox、通知 context 或 Tool Gateway ledger。
- 金额不用 `float`；本计划不引入模型概率或无 Provenance 商业字段。
- 新文件必须以 Git mode `100644` 提交；不提交 AppleDouble、`.env`、凭证、构建产物或本地截图。
- 每个任务独立 RED → GREEN → review → commit → push → 等待精确 SHA GitHub CI success；CI 未成功不得开始下一任务。
- 每个任务至少运行 focused pytest `-W error`、Ruff、mypy、`scripts/check_boundaries.py`、`scripts/scan_sensitive.py` 和 `git diff --check`。

## File and Interface Map

```text
shared/schemas/identifiers.py                 新增 NotificationJobId/NotificationId/AuthCheckRequestId
notification_gateway/jobs.py                  通知任务、typed context、job store Protocol
notification_gateway/inbox.py                 站内通知 View 与 store/service Protocol
notification_gateway/models.py                Notification 改用 typed context
notification_gateway/templates.py             typed context→固定通知文案/相对深链
notification_gateway/channels/structured_log.py typed context 安全日志序列化
infra/db/repositories/notification_jobs.py    PostgreSQL claim/fencing/retry
infra/db/repositories/in_app_notifications.py PostgreSQL append/list/mark-read
apps/scheduler_worker/notification_projection.py 领域事件→通知任务投影
apps/scheduler_worker/runtime.py               正式 scheduler composition
apps/notification_worker/{config,health,runtime,main}.py 生产 worker
notification_gateway/channels/{in_app,email}.py 两个 Phase 1 channel
tool_gateway/handlers/notification_email.py   transactional email 工具
connectors/dns_auth/                           公开 DNS Connector
workflows/sending_identity_auth/               认证检查 workflow
connectors/gmail/arf.py                         ARF 安全解析
apps/api/routers/{campaigns,sending_identities,notifications}.py 内部 API
apps/web/src/views/{OutreachWorkbench,SendingIdentityCenter,NotificationCenter}.vue
```

---

### Task 1: Durable Notification Jobs and In-App Inbox

**Files:**
- Modify: `shared/schemas/identifiers.py`
- Modify: `notification_gateway/models.py`
- Modify: `notification_gateway/channels/structured_log.py`
- Create: `notification_gateway/jobs.py`
- Create: `notification_gateway/inbox.py`
- Modify: `apps/api/composition/runtime.py`
- Modify: `scripts/demo_opportunity_board.py`
- Modify: `infra/db/tables.py`
- Create: `infra/db/repositories/notification_jobs.py`
- Create: `infra/db/repositories/in_app_notifications.py`
- Create: `migrations/versions/0015_notification_jobs.py`
- Modify: `tests/integration/test_migrations.py`
- Modify: `tests/integration/test_repositories.py`
- Create: `tests/unit/test_notification_contracts.py`
- Modify: `tests/unit/test_notification_router.py`
- Create: `tests/integration/test_notification_jobs.py`
- Create: `tests/integration/test_in_app_notifications.py`
- Modify: `tests/integration/test_notification_dedup.py`

**Interfaces:**
- Consumes: `TenantId`, `EmployeeId`, `new_id`, SQLAlchemy session factory and existing `NotificationPriority`.
- Produces:

```python
NotificationJobId = NewType("NotificationJobId", str)
NotificationId = NewType("NotificationId", str)
AuthenticationCheckRequestId = NewType("AuthenticationCheckRequestId", str)

class NotificationKind(str, Enum):
    HANDOFF_ESCALATION = "handoff_escalation"
    HANDOFF_QUEUE_BACKLOGGED = "handoff_queue_backlogged"
    SENDING_IDENTITY_SUSPENDED = "sending_identity_suspended"
    REPUTATION_THRESHOLD_BREACHED = "reputation_threshold_breached"
    COMMITMENT_OVERDUE = "commitment_overdue"
    APPROVAL_DECIDED = "approval_decided"

class NotificationJobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    REJECTED = "rejected"

@dataclass(frozen=True, repr=False)
class NotificationContext:
    kind: NotificationKind
    primary_id: str
    secondary_id: str | None
    reason_code: str | None
    level: int | None

@dataclass(frozen=True)
class NotificationJob:
    job_id: NotificationJobId
    tenant_id: TenantId
    recipient: EmployeeId
    priority: NotificationPriority
    context: NotificationContext
    source_event_fingerprint: str
    source_event: str
    dedup_key: str
    created_at: datetime

@dataclass(frozen=True)
class Notification:
    tenant_id: TenantId
    recipient: EmployeeId
    priority: NotificationPriority
    title: str
    context: NotificationContext
    source_event: str
    dedup_key: str
    next_step: str | None = None
    due_at: datetime | None = None
    link: str | None = None
    source_job_id: NotificationJobId | None = None

@dataclass(frozen=True)
class NotificationJobClaim:
    job_id: NotificationJobId
    tenant_id: TenantId
    recipient: EmployeeId
    priority: NotificationPriority
    context: NotificationContext
    source_event: str
    dedup_key: str
    claim_token: str
    attempt_count: int

@dataclass(frozen=True, repr=False)
class NotificationCursor:
    created_at: datetime
    notification_id: NotificationId

@dataclass(frozen=True)
class InAppNotification:
    notification_id: NotificationId
    tenant_id: TenantId
    recipient: EmployeeId
    priority: NotificationPriority
    title: str
    context: NotificationContext
    relative_link: str | None
    source_job_id: NotificationJobId
    created_at: datetime

@dataclass(frozen=True)
class InAppNotificationView:
    notification_id: NotificationId
    tenant_id: TenantId
    priority: NotificationPriority
    title: str
    context: NotificationContext
    relative_link: str | None
    created_at: datetime
    read_at: datetime | None

@dataclass(frozen=True)
class InboxActor:
    tenant_id: TenantId
    employee_id: EmployeeId

class InAppNotificationService(Protocol):
    async def list_notifications(
        self, tenant_id: TenantId, *, actor: InboxActor,
        limit: int, before: NotificationCursor | None
    ) -> tuple[InAppNotificationView, ...]: ...
    async def mark_read(
        self, tenant_id: TenantId, notification_id: NotificationId,
        *, actor: InboxActor
    ) -> InAppNotificationView: ...

class NotificationJobStore(Protocol):
    async def enqueue(self, job: NotificationJob) -> bool: ...
    async def claim_due(
        self, tenant_id: TenantId, *, limit: int, lease_owner: str
    ) -> tuple[NotificationJobClaim, ...]: ...
    async def complete(
        self, tenant_id: TenantId, job_id: NotificationJobId, *, claim_token: str
    ) -> bool: ...
    async def retry(
        self, tenant_id: TenantId, job_id: NotificationJobId,
        *, claim_token: str, error: BaseException
    ) -> bool: ...
    async def reject(
        self, tenant_id: TenantId, job_id: NotificationJobId,
        *, claim_token: str, error: BaseException
    ) -> bool: ...

class InAppNotificationStore(Protocol):
    async def append(self, notification: InAppNotification) -> bool: ...
    async def list_for_recipient(
        self, tenant_id: TenantId, recipient: EmployeeId,
        *, limit: int, before: NotificationCursor | None
    ) -> tuple[InAppNotificationView, ...]: ...
    async def mark_read(
        self, tenant_id: TenantId, recipient: EmployeeId,
        notification_id: NotificationId, *, read_at: datetime
    ) -> InAppNotificationView: ...

class InAppNotificationServiceImpl:
    def __init__(
        self, store: InAppNotificationStore,
        *, now: Callable[[], datetime]
    ) -> None: ...
    async def list_notifications(
        self, tenant_id: TenantId, *, actor: InboxActor,
        limit: int, before: NotificationCursor | None
    ) -> tuple[InAppNotificationView, ...]: ...
    async def mark_read(
        self, tenant_id: TenantId, notification_id: NotificationId,
        *, actor: InboxActor
    ) -> InAppNotificationView: ...
```

- [ ] **Step 1: Write strict contract tests**

Add tests that reject unknown context kinds, empty/secret-like IDs, free-form dict context, non-UTC times, invalid priorities, limit `0/101/True`, foreign-tenant recipient access and a second `read_at` mutation. Lock safe `repr` behavior so context and cursor values are not printed.

```python
def test_context_rejects_free_form_and_secret_markers() -> None:
    with pytest.raises(ValidationError, match="通知上下文无效"):
        NotificationContext(
            NotificationKind.SENDING_IDENTITY_SUSPENDED,
            "Bearer-private",
            None,
            None,
            None,
        )
```

Update every existing `Notification(...)` caller to construct the typed context explicitly. The structured-log channel must validate and serialize only the fixed fields `kind/primary_id/secondary_id/reason_code/level`; it must not regain a generic mapping path. Existing credential-shape, relative-link and durable rejection tests remain in force.

- [ ] **Step 2: Write real PostgreSQL RED tests**

The tests must prove:

```python
assert await store.enqueue(job) is True
assert await store.enqueue(job) is False
claims = await asyncio.gather(*[store.claim_due(tenant, limit=1, lease_owner=f"w{i}") for i in range(20)])
assert sum(len(batch) for batch in claims) == 1
assert await store.complete(tenant, job.job_id, claim_token="stale") is False
assert await inbox.list_for_recipient(other_tenant, employee, limit=50, before=None) == ()
```

Also assert migration `0015 → 0014 → 0015`, table/check/unique/FK parity, `FOR UPDATE SKIP LOCKED` behavior, lease expiry, increasing retry delay, fixed exception type persistence and immutable inbox content.

- [ ] **Step 3: Run RED**

Run:

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_notification_contracts.py \
       tests/integration/test_notification_jobs.py \
       tests/integration/test_in_app_notifications.py \
       tests/integration/test_migrations.py -q -W error
```

Expected: collection succeeds; failures identify missing contracts, tables and repositories. Existing migration tests must continue to pass.

- [ ] **Step 4: Implement contracts, migration and repositories**

Use PostgreSQL `INSERT ... ON CONFLICT DO NOTHING` for enqueue/append. Claim only `pending` rows whose `available_at <= now` and whose lease is absent/expired; assign a new unpredictable token in the same transaction. Every complete/retry/reject update must match tenant, job ID, `processing` status and claim token. Retry stores only `type(error).__name__` and uses the existing 30-second exponential schedule capped at 3600 seconds. `mark_read` must be a conditional `UPDATE ... WHERE read_at IS NULL RETURNING` and return the existing row when already read.

- [ ] **Step 5: Run GREEN and scoped gates**

Run the RED command again, then:

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH ruff check \
  shared/schemas/identifiers.py notification_gateway/jobs.py notification_gateway/inbox.py \
  notification_gateway/models.py notification_gateway/channels/structured_log.py \
  apps/api/composition/runtime.py scripts/demo_opportunity_board.py \
  infra/db/tables.py infra/db/repositories/notification_jobs.py \
  infra/db/repositories/in_app_notifications.py tests/unit/test_notification_contracts.py \
  tests/unit/test_notification_router.py tests/integration/test_notification_jobs.py \
  tests/integration/test_in_app_notifications.py tests/integration/test_notification_dedup.py
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH mypy \
  shared/schemas/identifiers.py notification_gateway/jobs.py notification_gateway/inbox.py \
  notification_gateway/models.py notification_gateway/channels/structured_log.py \
  apps/api/composition/runtime.py scripts/demo_opportunity_board.py \
  infra/db/repositories/notification_jobs.py infra/db/repositories/in_app_notifications.py
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
```

- [ ] **Step 6: Review, commit, push and wait exact CI**

Stage only Task 1 files with new files forced to `100644`; remove only Task 1 AppleDouble sidecars. Commit:

```bash
git commit -m "feat(notifications): persist jobs and inbox"
git push
```

Wait for `gh run list --commit "$(git rev-parse HEAD)"` to return a run whose `headSha` equals HEAD and whose final conclusion is `success`.

---

### Task 2: Notification Projection, In-App Channel and Worker Runtime

**Files:**
- Create: `apps/scheduler_worker/notification_projection.py`
- Create: `notification_gateway/templates.py`
- Create: `notification_gateway/channels/in_app.py`
- Create: `apps/notification_worker/config.py`
- Create: `apps/notification_worker/health.py`
- Create: `apps/notification_worker/runtime.py`
- Modify: `apps/notification_worker/main.py`
- Create: `tests/unit/test_notification_projection.py`
- Create: `tests/unit/test_notification_templates.py`
- Create: `tests/unit/test_in_app_channel.py`
- Create: `tests/unit/test_notification_worker.py`
- Create: `tests/integration/test_notification_worker.py`

**Interfaces:**
- Consumes: Task 1 `NotificationJobStore`, `InAppNotificationStore`; existing `NotificationRouter`, `OutboxDeliverer`, domain events and `StructuredLogChannel`.
- Produces:

```python
@dataclass(frozen=True)
class NotificationAudienceMember:
    tenant_id: TenantId
    employee_id: EmployeeId

class NotificationAudienceResolver(Protocol):
    async def recipients_for(
        self, tenant_id: TenantId, event: DomainEvent
    ) -> tuple[NotificationAudienceMember, ...]: ...

class NotificationProjectionHandler:
    async def handle(self, event: DomainEvent) -> None: ...

def notification_source_fingerprint(event: DomainEvent) -> str: ...

class NotificationJobHandoffNotifier:
    async def notify(self, notice: HandoffEscalationNotice) -> None: ...

class NotificationRenderer(Protocol):
    def render(self, claim: NotificationJobClaim) -> Notification: ...

class FixedNotificationTemplateRenderer:
    def render(self, claim: NotificationJobClaim) -> Notification: ...

@dataclass(frozen=True)
class NotificationWorkerConfig:
    database_url: SecretStr = field(repr=False)
    tenant_id: TenantId
    poll_interval_seconds: int
    batch_limit: int
    health_port: int
    lease_owner: str

    @classmethod
    def from_environ(
        cls, environ: Mapping[str, str]
    ) -> Self: ...

@dataclass
class NotificationHealthState:
    _ready: set[str] = field(default_factory=set, repr=False)
    _degraded: bool = field(default=False, repr=False)

    @property
    def is_ready(self) -> bool: ...
    def mark_ready(self, checkpoint: str) -> None: ...
    def mark_degraded(self) -> None: ...
    def mark_ok(self) -> None: ...

def create_notification_health_app(
    state: NotificationHealthState
) -> FastAPI: ...

class NotificationHealthServer:
    def __init__(self, state: NotificationHealthState, port: int) -> None: ...
    async def serve(self) -> None: ...
    async def close(self) -> None: ...

@dataclass(frozen=True)
class NotificationWorkerRuntime:
    jobs: NotificationJobStore
    router: NotificationRouter
    renderer: NotificationRenderer
    config: NotificationWorkerConfig
    health: NotificationHealthState

@asynccontextmanager
async def notification_worker_runtime(
    config: NotificationWorkerConfig,
) -> AsyncIterator[NotificationWorkerRuntime]: ...

class WorkerRunStatus(str, Enum):
    STARTED = "started"

@dataclass(frozen=True)
class WorkerRunResult:
    status: WorkerRunStatus
    cycles_completed: int
    jobs_completed: int

WaitForNextCycle = Callable[[int, asyncio.Event], Awaitable[None]]

async def run_notification_worker(
    runtime: NotificationWorkerRuntime,
    *, stop_event: asyncio.Event | None = None,
    wait: WaitForNextCycle | None = None,
) -> WorkerRunResult: ...
```

- [ ] **Step 1: Write projection RED tests**

For each supported event, use a fake typed audience resolver and assert exact job priority, kind, primary/secondary IDs, recipient and dedup key. Assert `notification_source_fingerprint` is SHA-256 lower-hex over event type + existing canonical serialization, changes when the typed payload changes and never returns/persists raw payload. Assert duplicate event delivery calls `enqueue` twice but creates one durable job. Assert wrong tenant, empty recipient set, unsupported event and resolver returning a `NotificationAudienceMember` for another tenant fail closed without a job. Verify no event payload free text reaches context. Cover `NotificationJobHandoffNotifier` separately: it fingerprints the fixed notice type plus `notice.dedup_key`, so owner/manager/boss escalations also enter the same durable job store rather than dispatching channels inside the scheduler.

- [ ] **Step 2: Write worker lifecycle RED tests**

Test config type/range validation, exact health paths, no trailing-slash redirect, `SKIP LOCKED` multi-worker claim, transient router failure → retry, policy rejection → reject, success → complete, stop during wait, cancellation during first yield, signal cleanup, engine disposal and fixed safe logs. A claim must remain leased if the process dies before result persistence and become claimable only after expiry.

- [ ] **Step 3: Run RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_notification_projection.py \
       tests/unit/test_notification_templates.py \
       tests/unit/test_in_app_channel.py \
       tests/unit/test_notification_worker.py \
       tests/integration/test_notification_worker.py -q -W error
```

Expected: missing projection/channel/runtime symbols only; PostgreSQL fixture and Alembic setup must be green.

- [ ] **Step 4: Implement projection and in-app delivery**

`NotificationProjectionHandler.handle` must reject unsupported event types before audience lookup. For each recipient, build a typed context and call `enqueue`; never call `NotificationRouter` from the scheduler. Persist only the lower-hex event fingerprint, never the canonical payload. `enqueue` commits the source-event-fingerprint-unique job before the handler returns; only then may Outbox commit the handler delivery. A crash between those commits causes safe event replay and `enqueue=False`, not a duplicate job. Register the handler under stable names such as `notification.sending_identity_suspended.v1` in the scheduler's complete registry during Task 4 composition.

`FixedNotificationTemplateRenderer` maps every `NotificationKind` to a fixed Chinese title, next step and validated relative-link template. It interpolates only typed IDs and bounded integer levels from `NotificationContext`; it never consumes source event payload text. Unknown kinds, missing required IDs or an invalid link fail closed before router dispatch.

`InAppChannel.deliver` validates `notification.recipient`, typed context, non-null `source_job_id` and relative link, then calls `InAppNotificationStore.append`. It must not accept a raw dict context, a job-less direct notification or an absolute URL.

- [ ] **Step 5: Implement worker runtime**

Each cycle calls `claim_due(limit=batch_limit)`, renders each claim through `NotificationRenderer`, dispatches claims independently and persists complete/retry/reject with the claim token. Ordinary `Exception` from one claim must not stop later claims; `CancelledError` propagates after cleanup. Health ready requires exactly config/schema/database/registry checkpoints. Main reads explicit environment through `NotificationWorkerConfig.from_environ`; missing configuration returns a fixed nonzero exit and no DSN text.

- [ ] **Step 6: Run GREEN, full integration and gates**

Run focused tests, then:

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/integration -q -W error
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH make check
```

Run scoped Ruff/mypy, boundary, sensitive and diff checks before staging.

- [ ] **Step 7: Commit, push and exact CI**

```bash
git commit -m "feat(notifications): project events to in-app worker"
git push
```

Do not start Task 3 until the exact SHA CI succeeds.

---

### Task 3: Transactional Email Notification Channel

**Files:**
- Modify: `connectors/gmail/client.py`
- Modify: `connectors/gmail/transport.py`
- Create: `tool_gateway/handlers/notification_email.py`
- Create: `notification_gateway/channels/email.py`
- Create: `apps/notification_worker/recipients.py`
- Modify: `apps/notification_worker/config.py`
- Modify: `apps/notification_worker/runtime.py`
- Modify: `infra/.env.example`
- Modify: `tests/unit/test_gmail_connector.py`
- Create: `tests/unit/test_notification_email_handler.py`
- Create: `tests/unit/test_notification_recipients.py`
- Create: `tests/integration/test_notification_email_gateway.py`
- Modify: `tests/integration/test_notification_worker.py`

**Interfaces:**
- Consumes: Task 2 worker and Task 1 jobs; existing Gmail transport and Tool Gateway pipeline.
- Produces:

```python
@dataclass(frozen=True)
class GmailTransactionalSendRequest:
    from_address: str = field(repr=False)
    recipient_address: str = field(repr=False)
    subject: str = field(repr=False)
    body: str = field(repr=False)
    deterministic_message_id: str
    idempotency_header: str

@dataclass(frozen=True, repr=False)
class NotificationRecipient:
    tenant_id: TenantId
    employee_id: EmployeeId
    address: str = field(repr=False)

class NotificationRecipientDirectory(Protocol):
    async def resolve(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> NotificationRecipient: ...

class GmailConnector:
    async def send_transactional_once(
        self, request: GmailTransactionalSendRequest
    ) -> GmailSendResult: ...
    async def reconcile_transactional_once(
        self, request: GmailTransactionalSendRequest
    ) -> GmailSendResult: ...

class TransactionalNotificationSender(Protocol):
    async def send(self, notification: Notification) -> None: ...

class EmailNotificationChannel:
    name = "email"
    async def deliver(self, notification: Notification) -> None: ...
```

- [ ] **Step 1: Write connector and gateway RED tests**

Assert transactional requests do not contain `List-Unsubscribe`, cannot use a `COLD_OUTREACH` identity, and never serialize address/subject/body into Tool Gateway persisted params, fingerprint evidence, audit projection, repr, logs or error text. Test deterministic Message-ID recovery, 401/403, 429 bounds, explicit not-written retry and ambiguous-write reconciliation exactly as manual send.

- [ ] **Step 2: Write permission and directory RED tests**

The recipient directory is injected into worker composition and returns a repr-hidden address only for the configured `(tenant_id, employee_id)`. Unknown employee, cross-tenant lookup, duplicate mapping, malformed address and a secret-looking configuration key fail closed. The browser/API never receives this directory.

- [ ] **Step 3: Run RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_gmail_connector.py \
       tests/unit/test_notification_email_handler.py \
       tests/unit/test_notification_recipients.py \
       tests/integration/test_notification_email_gateway.py \
       tests/integration/test_notification_worker.py -q -W error
```

- [ ] **Step 4: Implement transactional Gmail request and tool**

Keep the existing cold outreach `GmailSendRequest` unchanged. Add a separate `_build_transactional_message` path without customer unsubscribe headers. Register `notification.email.send` with exactly `tenant`, `permission`, `idempotency`, `rate_limit`; it must not use customer suppression or campaign approval. Handler input is notification ID + recipient employee ID + fixed template code; address/title/body are resolved after preflight and are stored only in the prepared ephemeral payload. Use a dedicated `TRANSACTIONAL` SendingIdentity and call `check_send_permission(..., for_cold_outreach=False)` plus `reserve_send_slot` before execution.

- [ ] **Step 5: Register email channel in worker routing policy**

URGENT and NORMAL notifications route to `in_app` and `email`; LOW routes only to `in_app`. Policy must verify the exact registered channel set and reject unknown/duplicate channels. A failed email channel leaves in-app delivered and only retries email through existing per-channel dedup.

- [ ] **Step 6: Run GREEN and all relevant gates**

Run focused tests, Gmail integration, notification worker integration, full integration, `make check`, scoped Ruff/mypy, boundary, sensitive and diff checks.

- [ ] **Step 7: Commit, push and exact CI**

```bash
git commit -m "feat(notifications): send transactional email alerts"
git push
```

Wait for exact SHA success.

---

### Task 4: DNS Authentication Connector and Scheduler Workflow

**Files:**
- Modify: `pyproject.toml`
- Modify: `connectors/gmail/client.py`
- Modify: `connectors/gmail/AGENTS.md`
- Modify: `tool_gateway/handlers/__init__.py`
- Modify: `shared/schemas/identifiers.py`
- Create: `shared/schemas/dns_auth.py`
- Modify: `shared/events/catalog.py`
- Modify: `infra/db/outbox.py`
- Modify: `tests/unit/test_outbox_serialization.py`
- Modify: `domains/sending_identity/permissions.py`
- Modify: `domains/sending_identity/repository.py`
- Modify: `domains/sending_identity/service.py`
- Modify: `domains/sending_identity/service_impl.py`
- Modify: `infra/db/tables.py`
- Modify: `infra/db/repositories/sending_identities.py`
- Modify: `infra/db/sending_identity_uow.py`
- Create: `migrations/versions/0016_auth_check_requests.py`
- Create: `connectors/dns_auth/__init__.py`
- Create: `connectors/dns_auth/client.py`
- Create: `tool_gateway/handlers/dns_auth.py`
- Create: `workflows/sending_identity_auth/__init__.py`
- Create: `workflows/sending_identity_auth/flow.py`
- Create: `apps/scheduler_worker/config.py`
- Create: `apps/scheduler_worker/runtime.py`
- Modify: `apps/scheduler_worker/main.py`
- Modify: `infra/.env.example`
- Create: `tests/unit/test_dns_auth_contracts.py`
- Create: `tests/unit/test_dns_auth_connector.py`
- Modify: `tests/unit/test_gmail_connector.py`
- Create: `tests/unit/test_sending_identity_auth_requests.py`
- Create: `tests/unit/test_sending_identity_auth_workflow.py`
- Create: `tests/integration/test_dns_auth_gateway.py`
- Create: `tests/integration/test_sending_identity_auth_workflow.py`
- Modify: `tests/integration/test_scheduler_worker.py`
- Modify: `tests/integration/test_migrations.py`
- Modify: `tests/integration/test_repositories.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class DnsAuthenticationRequest:
    domain: str
    dkim_selector: str

class DnsAuthenticationFailureCategory(str, Enum):
    MISSING = "missing"
    MALFORMED = "malformed"
    POLICY_UNSAFE = "policy_unsafe"

@dataclass(frozen=True)
class DnsAuthenticationFailure:
    check: str
    category: DnsAuthenticationFailureCategory
    instruction: str

@dataclass(frozen=True)
class DnsAuthenticationFacts:
    checked_at: datetime
    spf_passed: bool
    dkim_passed: bool
    dmarc_passed: bool
    failures: tuple[DnsAuthenticationFailure, ...]
    check_ref: str

class AuthenticationCheckRequestStatus(str, Enum):
    REQUESTED = "requested"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

@dataclass(frozen=True)
class AuthenticationCheckRequestView:
    request_id: AuthenticationCheckRequestId
    tenant_id: TenantId
    sending_identity_id: SendingIdentityId
    request_key: IdempotencyKey
    status: AuthenticationCheckRequestStatus
    requested_at: datetime
    completed_at: datetime | None

@dataclass(frozen=True)
class AuthenticationCheckRequested(DomainEvent):
    request_id: AuthenticationCheckRequestId
    sending_identity_id: SendingIdentityId

async def request_authentication_check(
    self, tenant_id: TenantId, identity_id: SendingIdentityId,
    request_key: IdempotencyKey, *, actor: Actor
) -> AuthenticationCheckRequestView: ...
```

- [ ] **Step 1: Write domain request RED tests**

Test boss/TENANT allow, manager/SELF/SYSTEM wrong action deny, API-style duplicate key idempotency, same key/different identity conflict, retired identity rejection, wrong tenant, deny immediate audit, successful allow after commit, commit rollback and `AuthenticationCheckRequested` outbox atomicity.

- [ ] **Step 2: Write DNS connector RED tests**

Use an injected async resolver, never real DNS in unit tests. SPF passes only with one syntactically valid SPF TXT policy; DKIM passes only when the configured selector TXT contains a valid public key marker; DMARC passes only at `_dmarc.<domain>` with `v=DMARC1` and a policy. NXDOMAIN/no-answer are typed failed facts. Timeout/temporary DNS errors are retryable. Reject IP literals, wildcard, public suffix-only, mixed case, credentials, URL syntax, empty labels, multiple trailing dots, selector controls and response sizes beyond fixed limits. Raw TXT and resolver exceptions must not appear in DTO/repr/log/error.

- [ ] **Step 3: Write workflow and real PostgreSQL RED tests**

Assert request event creates one workflow run, the tool step invokes `dns.auth.check`, SYSTEM actor is scoped to exactly one identity, successful facts call `record_authentication_result`, retryable errors preserve due work, permanent malformed results fail the run, duplicate event/request does not create a second run/check, and restart resumes from durable state. Include scheduler runtime schema/DB/registry readiness, advisory lock, cancellation cleanup and zero-arg fail-closed tests.

- [ ] **Step 4: Run RED**

Run all new Task 4 tests with `-W error`. Failures must be missing contracts/behavior, not DNS network availability or collection errors.

- [ ] **Step 5: Implement domain request, migration and outbox registration**

Create an append-only authentication request row with unique `(tenant_id, request_key)` and status `requested/running/succeeded/failed`. The service writes request + event in one UoW and records allow only after exit. Add `AUTH_CHECK_REQUEST` to typed actions; only boss/TENANT may request, while result recording remains exact SYSTEM identity scope.

For a newly registered `CREATED` identity, the first request transitions it to `AUTH_PENDING` in the same transaction. A recheck for `WARMING`, `ACTIVE`, `THROTTLED` or `SUSPENDED` preserves the current state until the new result is recorded; a failed latest result then follows the existing authentication-regression suspension rule. `RETIRED` always rejects.

- [ ] **Step 6: Implement connector, tool, workflow and production scheduler composition**

Use `dnspython` only inside `connectors/dns_auth`. Tool manifest is `dns.auth.check`, read-only external risk, with tenant/permission/idempotency/rate-limit stages. Remove the stale `dns.check_auth` capability and `check_dns_auth` skeleton from the Gmail connector and update its local rules/tests: DNS authentication has exactly one connector owner. Export the new handler from `tool_gateway.handlers` without adding a tool-ID branch to the pipeline. Workflow maps `DnsAuthenticationFacts` to the existing domain `AuthenticationResult`; it never passes raw DNS records. `apps/scheduler_worker/runtime.py` builds the complete Outbox registry: human handoff, notification projection and auth workflow launcher. It also registers the auth workflow definition/step handler, validates unique Alembic head and disposes all resources on every exit path.

- [ ] **Step 7: Run GREEN and gates**

Run Task 4 focused tests, sending identity suites, scheduler suites, full integration and `make check`. Run mypy on all new production files and the standard boundary/sensitive/diff gates.

- [ ] **Step 8: Commit, push and exact CI**

```bash
git commit -m "feat(sending-identity): verify dns authentication"
git push
```

Wait for exact SHA success.

---

### Task 5: ARF Complaint Parsing and Reputation Feedback

**Files:**
- Modify: `shared/schemas/email_feedback.py`
- Create: `connectors/gmail/arf.py`
- Modify: `connectors/gmail/feedback.py`
- Modify: `connectors/gmail/client.py`
- Modify: `workflows/email_feedback/flow.py`
- Modify: `workflows/email_feedback/repository.py`
- Modify: `domains/outreach/permissions.py`
- Modify: `domains/outreach/service.py`
- Modify: `domains/outreach/service_impl.py`
- Modify: `apps/email_feedback_worker/runtime.py`
- Modify: `infra/db/tables.py`
- Create: `migrations/versions/0017_email_complaints.py`
- Modify: `tests/unit/test_email_feedback_contracts.py`
- Create: `tests/unit/test_gmail_arf.py`
- Modify: `tests/integration/test_gmail_feedback_http.py`
- Modify: `tests/integration/test_email_feedback_flow.py`
- Modify: `tests/integration/test_email_feedback_worker.py`
- Modify: `tests/integration/test_migrations.py`
- Modify: `tests/integration/test_repositories.py`
- Modify: `tests/unit/test_outreach_permissions.py`

**Interfaces:**
- Extends `EmailFeedbackKind` with `COMPLAINT = "complaint"`.
- Produces `parse_abuse_report(raw: bytes, *, provider_ref_digest: str, occurred_at: datetime) -> tuple[EmailFeedbackItem, ...]`.
- Adds the public Outreach operation:

```python
async def apply_complaint(
    self,
    tenant_id: TenantId,
    target: DeliveryFeedbackTarget,
    provider_event_id: str,
    occurred_at: datetime,
    *,
    actor: Actor,
) -> SuppressionResult: ...
```

- [ ] **Step 1: Write ARF parser RED tests**

Accept only `multipart/report; report-type=feedback-report` containing one bounded `message/feedback-report` part and a TradeOS-generated correlation part. Require `Feedback-Type: abuse`; ordinary mail, DSN, malformed multipart, more than 100 reports, oversized MIME, missing/ambiguous correlation and non-UTC time must return typed unparseable/quarantine outcomes. Do not inspect Subject or prose body to infer a complaint.

- [ ] **Step 2: Write end-to-end feedback RED tests**

Create a real sent attempt/correlation, feed one ARF complaint, and assert in one transaction: receipt kind complaint, Outreach contact suppression, enrollment stopped, SendingIdentity complaint reputation event, `ComplaintReceived` outbox, and cursor advance. Inject failure after either domain participant and prove every write and cursor rolls back. Duplicate same fingerprint is no-op; same event ID/different fingerprint fails before any domain call.

- [ ] **Step 3: Run RED**

Run contract, parser, Gmail HTTP, feedback flow/worker and migration tests with `-W error`.

- [ ] **Step 4: Implement parser and flow mapping**

Parser must discard original MIME after returning typed DTOs. Use provider digest + ordinal for event IDs; source refs remain safe lower-hex digests. Map complaint to `DeliveryEventType.COMPLAINT` and `OutreachService.apply_complaint` using existing exact correlation. Add a typed Outreach action that only the exact feedback SYSTEM actor may call; it atomically suppresses the contact, stops the enrollment and publishes `ComplaintReceived`. Add complaint to database check constraints and worker metrics without changing DSN behavior.

- [ ] **Step 5: Run GREEN and gates**

Run all email feedback tests, full integration and `make check`, then scoped static/security gates.

- [ ] **Step 6: Commit, push and exact CI**

```bash
git commit -m "feat(email-feedback): process arf complaints"
git push
```

Wait for exact SHA success.

---

### Task 6: Internal Outreach, Sending Identity and Notification APIs

**Files:**
- Modify: `apps/api/dependencies.py`
- Modify: `apps/api/composition/runtime.py`
- Modify: `apps/api/routers/campaigns.py`
- Create: `apps/api/routers/sending_identities.py`
- Create: `apps/api/routers/notifications.py`
- Modify: `apps/api/main.py`
- Modify: `tests/unit/test_api_app.py`
- Create: `tests/unit/test_outreach_api.py`
- Create: `tests/unit/test_sending_identity_api.py`
- Create: `tests/unit/test_notification_api.py`
- Modify: `tests/integration/test_manual_email_send_api.py`
- Create: `tests/integration/test_slice4_api.py`

**Interfaces:**
- Adds request-scoped `InAppNotificationService` and existing `SendingIdentityService` to configured dependencies.
- Adds endpoints:

```text
GET  /crm/enrollments
POST /crm/enrollments/{enrollment_id}/attempts/prepare
POST /crm/message-attempts/{attempt_id}/send
GET  /crm/sending-identities
GET  /crm/sending-identities/{identity_id}
POST /crm/sending-identities/{identity_id}/authentication-checks
GET  /notifications
POST /notifications/{notification_id}/read
```

- [ ] **Step 1: Write API RED tests**

Use `httpx.AsyncClient + ASGITransport`. Assert strict Pydantic bodies, typed IDs, exact OpenAPI responses, no 422, no role/scope/tenant body fields, API first gate calls and domain second gate calls with actor/action/scope/tenant. Test sales SELF enrollment visibility, manager scoped visibility, boss SendingIdentity access, employee-only inbox access, cross-tenant/cross-recipient denial and zero writes on deny.

- [ ] **Step 2: Write manual send current-fact tests**

Prepare an attempt, mutate suppression/reply/identity/approval/rate facts, then send and prove the current state wins. Duplicate same content returns the existing outcome; different content under same idempotency key returns 409. Ambiguous Gmail result returns fixed reconciliation 409 and a second click performs no new send. Error bodies/logs must not contain subject/body/address/provider error.

- [ ] **Step 3: Run RED**

Run the four API test files and real runtime integration with `-W error`; expected failures are missing routes/dependencies only.

- [ ] **Step 4: Implement routes and composition**

Use generated/public domain DTOs only. Derive tenant and actors from `RequestIdentity`. Inbox list and read must ignore any recipient query/body input. Authentication check route accepts only an idempotency key and invokes `request_authentication_check`; it never accepts SPF/DKIM/DMARC results. Preserve existing manual send error mapping.

- [ ] **Step 5: Regenerate and verify OpenAPI**

Run `npm run gen:api` twice and assert `apps/web/src/api/api.d.ts` is byte-identical on the second run. Cold export must not connect to DB.

- [ ] **Step 6: Run GREEN and gates**

Run API unit/integration, full integration, `make check`, API type generation, Web typecheck and standard security gates.

- [ ] **Step 7: Commit, push and exact CI**

```bash
git commit -m "feat(api): expose slice4 internal operations"
git push
```

Wait for exact SHA success.

---

### Task 7: Vue Internal Operations UI and Browser E2E

**Files:**
- Create: `apps/web/src/views/OutreachWorkbench.vue`
- Create: `apps/web/src/views/SendingIdentityCenter.vue`
- Create: `apps/web/src/views/NotificationCenter.vue`
- Create: `apps/web/src/components/NotificationBadge.vue`
- Modify: `apps/web/src/router.ts`
- Modify: `apps/web/src/App.vue`
- Create: `apps/web/tests/outreach-workbench.test.ts`
- Create: `apps/web/tests/sending-identity-center.test.ts`
- Create: `apps/web/tests/notification-center.test.ts`
- Modify: `apps/web/tests/smoke.test.ts`
- Create: `tests/e2e/test_slice4_manual_send.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes generated `paths` from `apps/web/src/api/api.d.ts`; no handwritten API DTOs.
- Registers `/crm/outreach`, `/crm/sending-identities`, `/notifications`.

- [ ] **Step 1: Complete Creative Production selection**

Reuse the existing Creative Production board for this thread. Provide the approved functional contracts and current Opportunity Board/Handoff Queue references. Select one internal-operations direction covering all three pages; record the chosen hierarchy, spacing, state treatment and accessibility decisions in the Task 7 report. Do not let the visual output change permissions, API fields or domain states.

- [ ] **Step 2: Write Vue RED tests**

Test route registration, empty/loading/success states, 403 clearing sensitive content, 409 fixed reconciliation/duplicate states, 429/503 stale-data behavior, Retry-After manual retry, send double-click lock, request race suppression, notification read monotonic behavior, Escape/focus return and no body/provider error leakage.

- [ ] **Step 3: Write real Browser E2E RED**

Start testcontainers PostgreSQL + Alembic head, real configured Uvicorn, fake local Gmail/DNS transports through the real Connector/Tool Gateway boundary, Vite and Chromium. Seed employees/territories/verified contact/Campaign/SendingIdentity through real repositories/services. The fixed journey must:

```text
open outreach → prepare attempt → send once → DB attempt sent
open identity center → request DNS check → workflow result visible
inject hard bounce + complaint through fake Gmail HTTP → identity suspended
retry send → fixed fail-closed UI
open notifications → in-app alert visible → mark read
```

Assert exactly one provider send, no cross-tenant rows, no console errors, no document overflow at 1440×900 and 1180×800.

- [ ] **Step 4: Run RED**

```bash
cd apps/web
npm run typecheck
npm run lint
npm run test -- outreach-workbench.test.ts sending-identity-center.test.ts notification-center.test.ts
cd ../..
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
TRADEOS_REQUIRE_E2E=1 pytest tests/e2e/test_slice4_manual_send.py -q -W error
```

Expected: new routes/components/journey are missing; environment and existing tests collect cleanly.

- [ ] **Step 5: Implement the three pages**

Use Ant Design Vue and existing CRM tokens. Keep the send editor mounted only while the user remains authorized; wipe subject/body on 403 and after successful send. Sending Identity cards show authentication, warmup, daily capacity and reputation as code-derived values. Notification Center never renders raw HTML and only follows validated relative links.

- [ ] **Step 6: Visual and accessibility verification**

Use real Vite pages, take 1440×900 and 1180×800 screenshots, inspect them in the same turn against the selected Creative Production direction, and record accepted differences. Verify keyboard order, visible focus, Escape close/return, alert roles, disabled write controls, no horizontal document overflow and zero console errors.

- [ ] **Step 7: Run GREEN and all Web gates**

```bash
cd apps/web
npm run typecheck
npm run lint
npm run test
npm run build
cd ../..
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
TRADEOS_REQUIRE_E2E=1 pytest tests/e2e/test_slice4_manual_send.py -q -W error
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH make check
```

Remove `apps/web/dist`, coverage, screenshots outside the approved report path and AppleDouble sidecars before staging.

- [ ] **Step 8: Commit, push and exact CI**

```bash
git commit -m "feat(web): add slice4 outreach operations"
git push
```

Wait for exact SHA success including Browser E2E.

---

### Task 8: Slice 4 Demo, Runbook and Acceptance Evidence

**Files:**
- Create: `scripts/demo_slice4_manual_send.py`
- Create: `tests/integration/test_demo_slice4_manual_send.py`
- Create: `docs/operations/slice4-email-operations.md`
- Modify: `docs/architecture/11-deployment.md`
- Modify: `domains/sending_identity/AGENTS.md`
- Modify: `domains/outreach/AGENTS.md`
- Modify: `connectors/gmail/AGENTS.md`
- Modify: `notification_gateway/AGENTS.md`
- Modify: `apps/notification_worker/AGENTS.md`
- Modify: `infra/.env.example`

**Interfaces:**
- Demo reads only explicit environment and prints one safe JSON summary.
- Runbook separates container acceptance from real-domain external acceptance.

- [ ] **Step 1: Write demo RED tests**

Run the script twice against the same migrated PostgreSQL and local fake Gmail/DNS servers. Assert distinct tenants, exactly one send per run, authentication facts, one hard bounce, one complaint, suspended identity, blocked second send, one inbox notification and per-channel delivery states. Invalid DSN/OAuth/DNS config must return nonzero with fixed Chinese stderr and no marker leakage.

- [ ] **Step 2: Run RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/integration/test_demo_slice4_manual_send.py -q -W error
```

Expected: script missing or acceptance behavior missing; PostgreSQL fixture/Alembic remain green.

- [ ] **Step 3: Implement demo and operations runbook**

The demo must use real services/UoWs/Tool Gateway/workers and local controlled transports; it may directly seed only employee/territory and connector configuration prerequisites explicitly allowed by existing demo patterns. It must not insert outreach attempts, feedback, reputation, notification or tool-call business rows directly. Output only tenant/typed IDs/states/counts.

The runbook must include:

```text
required env refs and process commands
Alembic/readiness checks
SPF/DKIM/DMARC interpretation
Gmail OAuth rotation and reconciliation procedure
notification backlog and dead-letter handling
identity suspension recovery checklist
safe log fields and forbidden data
container acceptance command
real-domain acceptance checklist and evidence capture
```

- [ ] **Step 4: Run full local acceptance**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH make check
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH pytest tests/integration -q -W error
cd apps/web && npm run typecheck && npm run lint && npm run test && npm run build
cd ../..
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
TRADEOS_REQUIRE_E2E=1 pytest tests/e2e -q -W error
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
```

- [ ] **Step 5: Run real-domain acceptance when external inputs exist**

Using an independent cold outreach domain and Google Workspace mailbox configured outside Git:

1. verify real SPF/DKIM/DMARC facts;
2. warm or use an already compliant identity;
3. send one approved test message through the UI;
4. generate a real hard bounce and confirm cursor/reputation/suppression;
5. generate a real complaint only with a controlled mailbox/provider that supports ARF and confirm complaint reputation/suspension;
6. prove a subsequent send is blocked;
7. prove in-app and transactional alert delivery;
8. store only safe IDs, timestamps and screenshots with redacted addresses in the acceptance report.

If the required domain/OAuth/FBL capability is unavailable, record the external acceptance as `not_run` and do not claim Handbook Slice 4 complete.

- [ ] **Step 6: Commit, push and exact CI**

```bash
git commit -m "docs(phase1): add slice4 acceptance runbook"
git push
```

Wait for exact SHA success. Update the Slice 4 progress ledger only after container acceptance; mark Slice 4 complete only after Step 5 also passes.

---

## Plan Self-Review Checklist

- [x] Every requirement in design sections 3–10 maps to at least one task above.
- [x] Every public type/method consumed by a later task is produced by an earlier task.
- [x] No task adds a second Gmail cold-send pipeline or bypasses Tool Gateway.
- [x] Notification projection uses scheduler's complete Outbox registry; notification worker does not scan Outbox with a partial registry.
- [x] All external data is converted to typed safe DTOs before domain calls.
- [x] Every database task includes tenant isolation, transaction rollback, concurrency and migration roundtrip evidence.
- [x] Every UI endpoint derives recipient/tenant/actor server-side.
- [x] Every task ends with commit, push and exact SHA CI success.
- [x] The plan does not claim real-domain acceptance without external credentials and provider support.
