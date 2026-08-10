# Slice 4B-1 Outreach and Global Suppression Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 Campaign 不可变版本、Enrollment、原子配额、Message Attempt 与联系人/账户级全局抑制，为后续 Tool Gateway 提供默认拒绝且可审计的发送前业务事实。

**Architecture:** `domains/outreach` 独占触达业务规则，通过四个窄 Protocol 消费跨域安全快照；`infra/db` 用 PostgreSQL tenant-bound repositories、request-scoped UoW、不可变约束和 transactional outbox 落地。4B-1 只准备 Message Attempt，不调用 Gmail；真实发送前的最终门禁留给 4B-2。

**Tech Stack:** Python 3.12、dataclasses、Protocol、FastAPI 项目公共错误契约、SQLAlchemy 2.x async、PostgreSQL 16、Alembic、pytest/pytest-asyncio、ruff、mypy。

## Global Constraints

- 所有 Python 命令使用 `env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH"`；不得以系统旧 Python 的解析失败冒充 RED。
- 依赖方向固定为 `apps → workflows/agent-runtime → domains → shared`；`domains/outreach` 不直接导入其他 domain、SQLAlchemy、connector 或 app。
- 本切片不接外网、不读取凭证、不调用 Gmail、不生成邮件正文、不增加 API/UI/worker/scheduler。
- 所有业务表带 `tenant_id`；主键、唯一键、外键和查询均 tenant-aware；错租户写抛 `TenantIsolationViolation` 并写固定中文 CRITICAL 安全日志。
- Campaign 边界版本与 Sequence Step 不可更新/删除；抑制与 Action 只增；每日计数只能增加且不可删除。
- `stop_on_reply=True` 固定不可关闭；步骤 1..5，第一步 `DISCOVERY`，第一步 `wait_days=0`，后续 `wait_days>0`。
- 联系人资格、审批、回复和发件身份 provider 失败全部 fail closed；不得退化成未抑制、未回复、已验证或可发送。
- Message Attempt 不是发送授权；4B-2 必须在 connector 调用前重新检查当前事实并占用发件身份名额。
- 事件、审计、异常、日志、demo stdout/stderr 不包含联系人地址、邮件正文、客户原话、provider 原始响应、DSN、Token、Secret 或 connector ref。
- 每个公共 service 方法执行 `preauthorize → trusted resource/provider load → require → write/read → UoW commit → exactly-one allow audit`；所有失败零 allow，授权拒绝恰一 `deny:authorization`。
- 任何测试先取得 genuine RED；先排除 import、PATH、Docker、migration、fixture、warning、AppleDouble 和测试自身错误。
- 所有新文件最终以 Git mode `100644` 暂存；每个 Task 通过独立 review 后普通 commit、push，等待精确 HEAD CI success，再进入下一 Task；禁止 amend 和强推。

## File Structure

| 文件 | 单一职责 |
|---|---|
| `domains/outreach/models.py` | 纯状态机、不可变边界、Campaign/Enrollment/Suppression/Attempt 实体 |
| `domains/outreach/schemas.py` | 公共 frozen DTO 与四类 provider snapshot |
| `domains/outreach/errors.py` | 固定安全 typed errors |
| `domains/outreach/permissions.py` | domain-local actor/scope/action、两阶段 Phase 1 authorizer、审计 Protocol |
| `domains/outreach/repository.py` | repository/UoW Protocol 与 typed atomic outcomes |
| `domains/outreach/service.py` | 公共 OutreachService 与四个跨域 provider Protocol |
| `domains/outreach/service_impl.py` | service 编排、授权/审计顺序与业务事务 |
| `migrations/versions/0009_outreach.py` | 八张表、复合约束、索引与不可变/单调 trigger |
| `infra/db/tables.py` | 0009 的声明式 ORM 映射 |
| `infra/db/repositories/outreach.py` | tenant-bound PostgreSQL repositories 与精确 ON CONFLICT outcomes |
| `infra/db/outreach_uow.py` | 单 AsyncSession UoW 与 outbox 原子提交/清理 |
| `infra/db/outbox.py` | 显式注册并值级验证 `MessageSent`/`SuppressionAdded` |
| `tests/outreach_fakes.py` | service 单测共享的 in-memory UoW/provider/audit/trace fakes |
| `scripts/demo_outreach.py` | demo-only、无网络的真实 PostgreSQL composition |

---

### Task 1: Domain Models, DTOs, Permissions, and Protocols

**Files:**
- Modify: `shared/schemas/identifiers.py`
- Modify: `domains/outreach/models.py`
- Modify: `domains/outreach/schemas.py`
- Modify: `domains/outreach/errors.py`
- Create: `domains/outreach/permissions.py`
- Modify: `domains/outreach/repository.py`
- Modify: `domains/outreach/service.py`
- Modify: `domains/outreach/events.py`
- Create: `tests/unit/test_outreach_models.py`
- Create: `tests/unit/test_outreach_contracts.py`
- Create: `tests/unit/test_outreach_permissions.py`

**Interfaces:**
- Consumes: `TenantId`, `CampaignId`, `EnrollmentId`, `MessageAttemptId`, `MessageId`, `ContactPointId`, `ProspectAccountId`, `SendingIdentityId`, `ApprovalId`, `EmployeeId`, `IdempotencyKey`, `EventBus`。
- Produces: `SuppressionId`；全部领域 enum/entity/DTO；`OutreachAuthorizer`、`AuditLogger`；四个 provider Protocol；六类 repository 与 `OutreachUnitOfWorkFactory`；完整 `OutreachService` 签名。Task 2–6 只能消费这些名字，不得另造平行接口。

- [ ] **Step 1: Write pure model tests before changing production**

Create `tests/unit/test_outreach_models.py` with an independent transition table and boundary cases. Tests must dynamically load `domains.outreach.models` with `importlib` to satisfy the repository boundary checker.

```python
_CAMPAIGN_TRANSITIONS = {
    "draft": {"pending_approval", "cancelled"},
    "pending_approval": {"active", "cancelled"},
    "active": {"paused", "completed", "cancelled", "pending_approval"},
    "paused": {"active", "completed", "cancelled", "pending_approval"},
    "completed": set(),
    "cancelled": set(),
}

def test_campaign_transition_table_is_closed() -> None:
    models = importlib.import_module("domains.outreach.models")
    for source in models.CampaignState:
        for target in models.CampaignState:
            if target.value in _CAMPAIGN_TRANSITIONS[source.value]:
                models.validate_campaign_transition(source, target)
            else:
                with pytest.raises(InvalidStateTransition):
                    models.validate_campaign_transition(source, target)

@pytest.mark.parametrize(
    ("steps", "expected_code"),
    [
        ((), "steps_required"),
        ((SequenceStepRequest(1, StepIntent.FOLLOW_UP, 0),), "first_step_discovery"),
        ((SequenceStepRequest(1, StepIntent.DISCOVERY, 1),), "first_wait_zero"),
        (
            (
                SequenceStepRequest(1, StepIntent.DISCOVERY, 0),
                SequenceStepRequest(3, StepIntent.FOLLOW_UP, 1),
            ),
            "step_numbers_contiguous",
        ),
    ],
)
def test_campaign_boundary_rejects_each_independent_violation(steps, expected_code):
    request = valid_campaign_request(steps=steps)
    with pytest.raises(CampaignBoundaryInvalidError) as caught:
        CampaignBoundary.from_request(request)
    assert expected_code in caught.value.codes
```

Also test:

- external mutable lists/sets cannot mutate a constructed boundary, scope, snapshot, suppression target, or request;
- `stop_on_reply=False`, empty market/sender/type/category, duplicate sender, non-positive limits, `new > total`, >5 steps, bad handoff vocabulary all reject;
- Enrollment can move only `ENROLLED→IN_SEQUENCE/terminal`, `IN_SEQUENCE→IN_SEQUENCE/terminal`, and terminal states have no successor;
- Campaign revision increments version and never mutates an existing `CampaignVersion`;
- `SuppressionTarget` accepts exactly one canonical `cp_` or `acc_` ULID and rejects wrong namespace/free scope;
- Message Attempt state and provider/failure field combinations are structurally valid;
- `IdempotencyKey`, `source_ref`, provider ref, name and reason validators reject whitespace/control/URL/DSN/credential markers without echoing input.

- [ ] **Step 2: Write DTO and public signature contract tests**

Create `tests/unit/test_outreach_contracts.py` and lock exact signatures:

```python
_PUBLIC_SIGNATURES = {
    "create_campaign": ("self", "tenant_id", "request", "actor"),
    "submit_campaign": ("self", "tenant_id", "campaign_id", "actor"),
    "revise_campaign": ("self", "tenant_id", "campaign_id", "request", "actor"),
    "activate_campaign": ("self", "tenant_id", "campaign_id", "actor"),
    "pause_campaign": ("self", "tenant_id", "campaign_id", "reason", "actor"),
    "cancel_campaign": ("self", "tenant_id", "campaign_id", "actor"),
    "get_campaign": ("self", "tenant_id", "campaign_id", "actor"),
    "list_campaigns": ("self", "tenant_id", "scope", "limit", "actor"),
    "enroll": ("self", "tenant_id", "campaign_id", "request", "actor"),
    "prepare_message_attempt": ("self", "tenant_id", "enrollment_id", "actor"),
    "record_sent": ("self", "tenant_id", "attempt_id", "provider_ref", "actor"),
    "record_send_failure": ("self", "tenant_id", "attempt_id", "category", "actor"),
    "stop_enrollment": ("self", "tenant_id", "enrollment_id", "reason", "actor"),
    "get_enrollment": ("self", "tenant_id", "enrollment_id", "actor"),
    "list_enrollments": ("self", "tenant_id", "scope", "limit", "actor"),
    "add_suppression": ("self", "tenant_id", "request", "actor"),
    "is_suppressed": ("self", "tenant_id", "target", "actor"),
    "list_suppressions": ("self", "tenant_id", "scope", "limit", "actor"),
}

def test_public_service_signatures_are_exact() -> None:
    for name, expected in _PUBLIC_SIGNATURES.items():
        assert tuple(inspect.signature(getattr(OutreachService, name)).parameters) == expected
```

Contract tests must prove:

- `OutreachService` has no `prepare_send`, `verified: bool`, `suppress(scope, target_id)`, `unsuppress`, or public worker `list_due_enrollments` compatibility path;
- provider methods are async and exactly named `get_contact_eligibility`, `get_sending_identity_eligibility`, `get_campaign_approval`, `get_reply_status`;
- provider snapshots are frozen, require runtime string IDs and UTC times, and reject tenant/resource mismatch construction;
- repository atomic outcomes validate `CREATED/EXISTING/CONFLICT/CAP_REACHED` against optional winner fields;
- `SuppressionId` is exported from `shared.schemas.identifiers` and `new_id("sup")` has the canonical prefix.

- [ ] **Step 3: Write permission matrix and mutation tests**

Create `tests/unit/test_outreach_permissions.py` with a written matrix, not a copy of production sets:

```python
_ALLOWED = {
    ("boss", "tenant"): {
        "campaign:create", "campaign:submit", "campaign:revise",
        "campaign:activate", "campaign:pause", "campaign:cancel",
        "campaign:read", "campaign:list", "enrollment:create",
        "enrollment:stop", "enrollment:read", "enrollment:list",
        "suppression:add", "suppression:read", "suppression:list",
    },
    ("manager", "manager"): {
        "campaign:submit", "campaign:revise", "campaign:pause",
        "campaign:cancel", "campaign:read", "campaign:list",
        "enrollment:create", "enrollment:stop", "enrollment:read",
        "enrollment:list", "suppression:read", "suppression:list",
    },
    ("system", "system"): {
        "enrollment:prepare_send", "enrollment:record_sent",
        "enrollment:record_failure", "enrollment:stop",
        "enrollment:read", "suppression:add", "suppression:read",
    },
    ("sales", "self"): {
        "campaign:read", "campaign:list", "enrollment:read", "enrollment:list",
    },
}
```

For every action/role/level combination assert allow only if listed. Add mutations for wrong tenant, `scope != actor.scope`, empty actor, unknown role/action, unrestricted manager/system, two-target system, mutable source set expansion, manager row mismatch on every non-`None` dimension, and system suppression reasons outside `UNSUBSCRIBE/COMPLAINT/HARD_BOUNCE`.

- [ ] **Step 4: Run the required RED commands**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_outreach_models.py tests/unit/test_outreach_contracts.py \
  tests/unit/test_outreach_permissions.py -q -W error
```

Expected: collected tests fail only because new types, signatures and permission implementation are absent or the current skeleton still exposes unsafe interfaces. Import/PATH/boundary failures do not count.

- [ ] **Step 5: Implement the immutable domain model and DTO vocabulary**

Use these exact closed vocabularies in `models.py`:

```python
class CampaignState(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

class EnrollmentState(str, Enum):
    ENROLLED = "enrolled"
    IN_SEQUENCE = "in_sequence"
    REPLIED = "replied"
    COMPLETED = "completed"
    STOPPED_SUPPRESSED = "stopped_suppressed"
    STOPPED_BOUNCED = "stopped_bounced"
    STOPPED_MANUAL = "stopped_manual"
    STOPPED_IDENTITY_UNAVAILABLE = "stopped_identity_unavailable"

class SuppressionReason(str, Enum):
    UNSUBSCRIBE = "unsubscribe"
    COMPLAINT = "complaint"
    HARD_BOUNCE = "hard_bounce"
    MANUAL_BLOCK = "manual_block"
    COMPETITOR = "competitor"
    EXISTING_CUSTOMER_CONFLICT = "existing_customer_conflict"

class MessageAttemptState(str, Enum):
    RESERVED = "reserved"
    SENT = "sent"
    FAILED_TRANSIENT = "failed_transient"
    FAILED_PERMANENT = "failed_permanent"

class SendFailureCategory(str, Enum):
    PROVIDER_TRANSIENT = "provider_transient"
    IDENTITY_UNAVAILABLE = "identity_unavailable"

class EnrollmentStopReason(str, Enum):
    REPLY = "reply"
    SUPPRESSION = "suppression"
    HARD_BOUNCE = "hard_bounce"
    MANUAL = "manual"
    IDENTITY_UNAVAILABLE = "identity_unavailable"
```

Replace mutable collection fields with tuples/frozensets at construction. Implement explicit transition functions; no enum fallback or string coercion. Add `SuppressionId = NewType("SuppressionId", str)` beside outreach identifiers.

Replace the skeleton error surface with these fixed public domain errors in `errors.py`: `CampaignBoundaryInvalidError`, `CampaignApprovalRequiredError`, `ContactNotEligibleError`, `OutreachProviderUnavailableError`, `CampaignQuotaExceededError`, `SendingIdentityUnavailableError`, `AccountAlreadyEnrolledError`, `IdempotencyConflictError`, `MessageAttemptConflictError`, `ReplyAlreadyReceivedError`, and `SuppressedError`. Input/shape errors inherit `ValidationError`; state/policy errors inherit the narrow existing `PolicyViolation` or `InvalidStateTransition`; provider unavailability inherits `TransientError`. Messages are fixed Chinese text and never interpolate caller/provider/database values.

- [ ] **Step 6: Implement public DTOs, repositories and service Protocols**

The four provider interfaces in `service.py` must be exact:

```python
@runtime_checkable
class ContactEligibilityProvider(Protocol):
    async def get_contact_eligibility(
        self, tenant_id: TenantId, contact_point_id: ContactPointId,
        account_id: ProspectAccountId,
    ) -> ContactEligibilitySnapshot: ...

@runtime_checkable
class SendingIdentityEligibilityProvider(Protocol):
    async def get_sending_identity_eligibility(
        self, tenant_id: TenantId, identity_id: SendingIdentityId,
    ) -> SendingIdentityEligibilitySnapshot: ...

@runtime_checkable
class CampaignApprovalProvider(Protocol):
    async def get_campaign_approval(
        self, tenant_id: TenantId, campaign_id: CampaignId, version: int,
    ) -> CampaignApprovalSnapshot | None: ...

@runtime_checkable
class ReplyStatusProvider(Protocol):
    async def get_reply_status(
        self, tenant_id: TenantId, contact_point_id: ContactPointId,
        account_id: ProspectAccountId,
    ) -> ReplyStatusSnapshot: ...
```

Define all Task 2 atomic repository outcomes now: `EnrollmentInsertResult`, `SuppressionAppendResult`, `QuotaReservationResult`, `MessageAttemptCreateResult`. Enrollment status is exactly `CREATED/EXISTING/IDEMPOTENCY_CONFLICT/ACCOUNT_CONFLICT`; suppression and attempt status are exactly `CREATED/EXISTING/CONFLICT`; quota status is exactly `RESERVED/CAP_REACHED`. `CREATED/EXISTING` require a typed winner, conflict/cap outcomes forbid one, and malformed combinations raise `ValidationError`. `OutreachUnitOfWork` exposes `campaigns`, `enrollments`, `suppressions`, `quotas`, `attempts`, `actions`, `bus` and async context manager methods.

Define the request, target and provider vocabulary in `schemas.py`; later tasks must not replace these with booleans or free strings:

```python
class ContactVerificationStatus(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    RISKY = "risky"
    INVALID = "invalid"

class ContactLegalBasis(str, Enum):
    LEGITIMATE_INTEREST = "legitimate_interest"
    CONSENT = "consent"
    EXISTING_CUSTOMER = "existing_customer"

class OutreachSenderRole(str, Enum):
    COLD_OUTREACH = "cold_outreach"
    PRIMARY_BUSINESS = "primary_business"
    TRANSACTIONAL = "transactional"

class CampaignApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"

class ReplyState(str, Enum):
    NO_REPLY = "no_reply"
    REPLIED = "replied"

@dataclass(frozen=True)
class SequenceStepRequest:
    step_number: int
    intent: StepIntent
    wait_days: int

@dataclass(frozen=True)
class CampaignCreateRequest:
    name: str
    markets: tuple[str, ...]
    target_entity_types: tuple[str, ...]
    allowed_categories: tuple[str, ...]
    sender_identity_ids: tuple[SendingIdentityId, ...]
    steps: tuple[SequenceStepRequest, ...]
    daily_new_contact_limit: int
    daily_total_message_limit: int
    handoff_triggers: tuple[str, ...]
    stop_on_reply: bool = True

@dataclass(frozen=True)
class EnrollmentCreateRequest:
    account_id: ProspectAccountId
    contact_point_id: ContactPointId
    idempotency_key: IdempotencyKey

@dataclass(frozen=True)
class SuppressionTarget:
    contact_point_id: ContactPointId | None = None
    account_id: ProspectAccountId | None = None

    def __post_init__(self) -> None:
        present = [
            value for value in (self.contact_point_id, self.account_id)
            if value is not None
        ]
        if len(present) != 1:
            raise ValidationError("抑制目标必须且只能指定一个资源")
        prefix = "cp" if self.contact_point_id is not None else "acc"
        if re.fullmatch(
            rf"{prefix}_[0-7][0-9A-HJKMNP-TV-Z]{{25}}", str(present[0])
        ) is None:
            raise ValidationError("抑制目标无效")

    @property
    def scope(self) -> SuppressionScope:
        return (
            SuppressionScope.CONTACT
            if self.contact_point_id is not None
            else SuppressionScope.ACCOUNT
        )

    @property
    def canonical_id(self) -> str:
        value = self.contact_point_id or self.account_id
        assert value is not None
        return str(value)

@dataclass(frozen=True)
class SuppressionRequest:
    target: SuppressionTarget
    reason: SuppressionReason
    occurred_at: datetime
    source_ref: str
    idempotency_key: IdempotencyKey

@dataclass(frozen=True)
class ContactEligibilitySnapshot:
    tenant_id: TenantId
    contact_point_id: ContactPointId
    account_id: ProspectAccountId
    verification: ContactVerificationStatus
    verified_at: datetime | None
    legal_basis: ContactLegalBasis
    legal_basis_ref: str
    contact_belongs_to_account: bool
    country: str
    entity_type: str
    qualified_categories: frozenset[str]
    observed_at: datetime

@dataclass(frozen=True)
class SendingIdentityEligibilitySnapshot:
    tenant_id: TenantId
    identity_id: SendingIdentityId
    role: OutreachSenderRole
    authentication_passed: bool
    sendable: bool
    remaining_slots: int
    observed_at: datetime

@dataclass(frozen=True)
class CampaignApprovalSnapshot:
    tenant_id: TenantId
    campaign_id: CampaignId
    version: int
    approval_id: ApprovalId
    state: CampaignApprovalState
    approved_by: EmployeeId | None
    approved_at: datetime | None

@dataclass(frozen=True)
class ReplyStatusSnapshot:
    tenant_id: TenantId
    contact_point_id: ContactPointId
    account_id: ProspectAccountId
    state: ReplyState
    replied_at: datetime | None
    observed_at: datetime

@dataclass(frozen=True)
class CampaignBoundaryView:
    markets: tuple[str, ...]
    target_entity_types: tuple[str, ...]
    allowed_categories: tuple[str, ...]
    sender_identity_ids: tuple[SendingIdentityId, ...]
    steps: tuple[SequenceStepRequest, ...]
    daily_new_contact_limit: int
    daily_total_message_limit: int
    handoff_triggers: tuple[str, ...]
    stop_on_reply: bool

@dataclass(frozen=True)
class CampaignView:
    tenant_id: TenantId
    campaign_id: CampaignId
    name: str
    state: CampaignState
    version: int
    boundary: CampaignBoundaryView
    approval_id: ApprovalId | None
    approved_by: EmployeeId | None
    approved_at: datetime | None
    paused_reason: str | None
    created_by: EmployeeId
    created_at: datetime
    today_new_contacts_reserved: int
    today_messages_reserved: int

@dataclass(frozen=True)
class EnrollmentView:
    tenant_id: TenantId
    enrollment_id: EnrollmentId
    campaign_id: CampaignId
    campaign_version: int
    account_id: ProspectAccountId
    contact_point_id: ContactPointId
    sending_identity_id: SendingIdentityId
    state: EnrollmentState
    current_step: int
    next_send_at: datetime | None
    enrolled_at: datetime
    stopped_at: datetime | None
    stop_reason: EnrollmentStopReason | None

@dataclass(frozen=True)
class MessageAttemptView:
    tenant_id: TenantId
    attempt_id: MessageAttemptId
    message_id: MessageId
    campaign_id: CampaignId
    enrollment_id: EnrollmentId
    campaign_version: int
    step_number: int
    sending_identity_id: SendingIdentityId
    idempotency_key: IdempotencyKey
    state: MessageAttemptState
    provider_ref: str | None
    failure_category: SendFailureCategory | None
    created_at: datetime
    updated_at: datetime

@dataclass(frozen=True)
class SuppressionView:
    tenant_id: TenantId
    suppression_id: SuppressionId
    target: SuppressionTarget
    reason: SuppressionReason
    occurred_at: datetime
    source_ref: str
    idempotency_key: IdempotencyKey
    created_at: datetime

@dataclass(frozen=True)
class SuppressionResult:
    created: bool
    suppression: SuppressionView
    stopped_count: int
```

Every snapshot validates runtime ID/string/bool/int types and UTC-aware times. `APPROVED` requires approver/time and every other approval state forbids them; `VERIFIED` requires `verified_at` and a non-empty safe legal-basis reference; `REPLIED` requires `replied_at`. `SuppressionTarget` requires exactly one canonical target. `CampaignCreateRequest` copies all collections to tuples and validates handoff triggers against the closed public wire vocabulary copied from the approved opportunity contract without importing that domain.

`OutreachService` annotations must exactly match the approved spec: Campaign methods return `CampaignView`; Enrollment methods return `EnrollmentView` except prepare/record methods, which return `MessageAttemptView`; `add_suppression` returns `SuppressionResult`; `is_suppressed` returns `SuppressionView | None`; list methods return typed lists and expose keyword-only `limit` and `actor`. Contract tests compare both `inspect.signature` and `typing.get_type_hints`, so a matching parameter name with the wrong request or return type is a RED.

- [ ] **Step 7: Implement two-phase permissions and fixed audit**

Mirror the proven 4A pattern but use outreach resources:

```python
@dataclass(frozen=True)
class OutreachScope:
    level: ScopeLevel | None = None
    allowed_campaign_ids: frozenset[CampaignId] | None = None
    allowed_account_ids: frozenset[ProspectAccountId] | None = None
    allowed_enrollment_ids: frozenset[EnrollmentId] | None = None
    allowed_suppression_targets: frozenset[str] | None = None

class OutreachAuthorizer(Protocol):
    def preauthorize(self, actor, action, scope, tenant_id) -> str: ...
    def require(
        self, actor, action, scope, tenant_id, *, campaign_id=None,
        account_id=None, enrollment_id=None, suppression_target=None,
        suppression_reason=None,
    ) -> str: ...
```

`OutreachScope.__post_init__` defensively copies sets, validates canonical IDs, rejects unrestricted MANAGER and SELF, requires MANAGER to narrow campaign or account, requires SELF to carry an explicit campaign/enrollment ownership set, and requires an exact singleton SYSTEM enrollment or suppression target for writes. `Phase1OutreachAuthorizer` applies every non-`None` scope dimension to a real row; `DefaultDenyAuthorizer` rejects both phases. `StandardAuditLogger` logs only actor/action/tenant_id/scope/rule with fixed Chinese message `授权审计`.

- [ ] **Step 8: Run focused gates, review, commit, push, and wait for CI**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" pytest tests/unit/test_outreach_models.py tests/unit/test_outreach_contracts.py tests/unit/test_outreach_permissions.py -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" ruff check shared/schemas/identifiers.py domains/outreach tests/unit/test_outreach_models.py tests/unit/test_outreach_contracts.py tests/unit/test_outreach_permissions.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" mypy shared/schemas/identifiers.py domains/outreach
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py
git diff --check
```

Freeze the task diff, obtain an independent spec/code review, fix every Critical/Important with new RED→GREEN, then:

```bash
git add --chmod=-x -- shared/schemas/identifiers.py domains/outreach tests/unit/test_outreach_models.py tests/unit/test_outreach_contracts.py tests/unit/test_outreach_permissions.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/scan_sensitive.py --staged --quiet
git diff --cached --check
git commit -m "feat(outreach): define campaign and suppression contracts"
git push origin codex/phase1-implementation
```

Wait until `gh run list --commit "$(git rev-parse HEAD)"` reports success for that exact SHA.

---

### Task 2: PostgreSQL Schema, Repositories, UoW, and Outbox

**Files:**
- Create: `migrations/versions/0009_outreach.py`
- Modify: `infra/db/tables.py`
- Create: `infra/db/repositories/outreach.py`
- Create: `infra/db/outreach_uow.py`
- Modify: `infra/db/outbox.py`
- Modify: `tests/integration/test_migrations.py`
- Modify: `tests/integration/test_repositories.py`
- Create: `tests/integration/test_outreach_repositories.py`
- Modify: `tests/unit/test_outbox_serialization.py`

**Interfaces:**
- Consumes: all Task 1 entities/repository outcomes/Protocol and existing `PostgresEventBus`.
- Produces: Alembic head `0009`; eight ORM rows; concrete repository classes; `SqlAlchemyOutreachUnitOfWork`; registered, value-validated `MessageSent` and `SuppressionAdded` outbox events.

- [ ] **Step 1: Write migration and ORM parity RED tests**

Extend the existing exact table/index registries and add these assertions:

```python
OUTREACH_TABLES = (
    "outreach_campaigns",
    "outreach_campaign_versions",
    "outreach_sequence_steps",
    "outreach_enrollments",
    "outreach_suppressions",
    "outreach_daily_quotas",
    "outreach_message_attempts",
    "outreach_actions",
)

async def test_0009_outreach_schema_and_roundtrip(db_url: str) -> None:
    assert set(OUTREACH_TABLES) <= await _table_names(engine)
    _run_alembic(db_url, "downgrade", "0008")
    assert set(OUTREACH_TABLES).isdisjoint(await _table_names(engine))
    _run_alembic(db_url, "upgrade", "0009")
    assert set(OUTREACH_TABLES) <= await _table_names(engine)
```

Test composite tenant FKs, partial active-account uniqueness, immutable version/step/suppression/action triggers, quota UPDATE decrease and DELETE rejection, explicit NULL-sensitive CHECKs, and ORM constraint/index parity by name.

- [ ] **Step 2: Write repository/UoW RED tests against real PostgreSQL**

Create `tests/integration/test_outreach_repositories.py` with function-scoped engine/session factories. Cover every repository:

```python
async def test_repository_reads_are_tenant_scoped_and_wrong_tenant_writes_alert(
    outreach_repository_harness, caplog
):
    campaign = campaign_for(TENANT_A)
    await outreach_repository_harness.campaigns(TENANT_A).add(campaign)
    assert await outreach_repository_harness.campaigns(TENANT_B).get(
        TENANT_B, campaign.campaign_id
    ) is None
    with pytest.raises(TenantIsolationViolation):
        await outreach_repository_harness.campaigns(TENANT_B).add(campaign)
    records = [r for r in caplog.records if r.name == "security.tenant_isolation"]
    assert [(r.levelname, r.message) for r in records] == [
        ("CRITICAL", "检测到跨租户数据隔离违规")
    ]

async def test_uow_commit_failure_rolls_back_campaign_action_and_outbox(
    outreach_uow_factory, fresh_outreach_reader
):
    with pytest.raises(IntegrityError):
        async with outreach_uow_factory(TENANT) as uow:
            await seed_duplicate_campaign_graph(uow)
    assert await fresh_outreach_reader.count_campaigns(TENANT) == 0
    assert await fresh_outreach_reader.count_actions(TENANT) == 0
    assert await fresh_outreach_reader.count_outbox(TENANT) == 0
```

Also prove exact typed outcomes for same-key same-payload, same-key different-payload, cap reached, account conflict, locked campaign/enrollment retrieval, canonical ordering, and cleanup `BaseException` preservation.

- [ ] **Step 3: Write outbox whitelist and malicious payload RED tests**

Extend the explicit registry expectation with exactly two event names and test both valid round trips:

```python
assert EVENT_REGISTRY["MessageSent"] is MessageSent
assert EVENT_REGISTRY["SuppressionAdded"] is SuppressionAdded
assert deserialize(MessageSent, serialize(valid_message_sent)) == valid_message_sent
assert deserialize(SuppressionAdded, serialize(valid_suppression_added)) == valid_suppression_added
```

Mutations must reject wrong ID prefixes, free scope/reason, control characters, URL/DSN, credential markers, address-shaped target IDs, body-like text and non-UTC time with fixed `ValidationError("触达事件载荷无效")`.

- [ ] **Step 4: Run the genuine RED suite**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/integration/test_migrations.py tests/integration/test_repositories.py \
  tests/integration/test_outreach_repositories.py tests/unit/test_outbox_serialization.py \
  -q -W error
```

Expected: failures are only absent 0009 tables/rows/repositories/UoW and missing event whitelist validation. Docker, Alembic base fixtures and all pre-existing tests must already pass.

- [ ] **Step 5: Create migration 0009 with exact constraints and guards**

Implement the eight tables with this key matrix:

```text
outreach_campaigns:
  PK (tenant_id,campaign_id); state/current_version/cursor checks
outreach_campaign_versions:
  PK (tenant_id,campaign_id,version); FK campaign; typed scalar columns plus
  JSONB only for validated frozen string-list boundaries
outreach_sequence_steps:
  PK (tenant_id,campaign_id,version,step_number); FK version
outreach_enrollments:
  PK (tenant_id,enrollment_id); FK campaign+version; UNIQUE tenant+idempotency_key;
  partial UNIQUE (tenant_id,account_id) WHERE state IN ('enrolled','in_sequence')
outreach_suppressions:
  PK (tenant_id,suppression_id); UNIQUE tenant+idempotency_key;
  exactly-one contact/account target CHECK
outreach_daily_quotas:
  PK (tenant_id,campaign_id,on_day); both counters >=0
outreach_message_attempts:
  PK (tenant_id,attempt_id); UNIQUE tenant+idempotency_key; FK enrollment/campaign/version
outreach_actions:
  PK (tenant_id,action_id); UNIQUE tenant+action_key
```

Use JSONB only for frozen string lists where normalized child rows add no query value; Sequence Steps remain normalized rows. Add covering indexes for suppression contact/account lookup, scoped Campaign/Enrollment lists and active target locking. Triggers block UPDATE/DELETE on versions, steps, suppressions, actions; quota trigger blocks DELETE or either counter decreasing.

- [ ] **Step 6: Add ORM rows and concrete repositories**

Map every migration column/constraint/index in `infra/db/tables.py`. In `infra/db/repositories/outreach.py`, bind `tenant_id` in each constructor and centralize wrong-tenant rejection:

```python
def _require_tenant(bound: TenantId, actual: TenantId, *, action: str) -> None:
    if actual != bound:
        _tenant_logger.critical(
            "检测到跨租户数据隔离违规",
            extra={"action": action, "tenant_id": str(bound)},
        )
        raise TenantIsolationViolation("跨租户数据隔离违规")
```

Use PostgreSQL `INSERT ... ON CONFLICT` with exact named conflict targets for enrollment/suppression/attempt idempotency. Re-read the winner and compare canonical payload fields; return `CONFLICT` on mismatch without parsing `IntegrityError` strings. Quota reservations use one conditional statement and return `CAP_REACHED` when no row can increment below limit.

- [ ] **Step 7: Implement UoW and outreach event validation**

`SqlAlchemyOutreachUnitOfWork.__aenter__` creates one session and all six repositories plus actions/bus. Copy the proven cleanup semantics:

```python
async def __aexit__(self, exc_type, exc, tb) -> None:
    preserve_primary = exc_type is not None
    try:
        if exc_type is None:
            try:
                await self._session.commit()
            except BaseException:
                preserve_primary = True
                try:
                    await self._session.rollback()
                except BaseException:
                    _cleanup_logger.error("触达事务回滚失败")
                raise
        else:
            try:
                await self._session.rollback()
            except BaseException:
                _cleanup_logger.error("触达事务回滚失败")
    finally:
        try:
            await self._session.close()
        except BaseException:
            if not preserve_primary:
                raise
            _cleanup_logger.error("触达事务关闭失败")
```

In `infra/db/outbox.py`, add an explicit `_validate_outreach_event`; validate canonical `msg_/cmp_/sid_/cp_/acc_` IDs and closed scope/reason vocabularies before serialization. Repository-generated business IDs use `cmp_`, `enr_`, `mat_` and `sup_` ULIDs respectively. Do not import `domains.outreach.models` into `shared`.

- [ ] **Step 8: Run persistence gates, review, commit, push, and wait for CI**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" pytest tests/integration/test_migrations.py tests/integration/test_repositories.py tests/integration/test_outreach_repositories.py tests/unit/test_outbox_serialization.py -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" ruff check migrations/versions/0009_outreach.py infra/db tests/integration/test_outreach_repositories.py tests/unit/test_outbox_serialization.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" mypy infra/db/outreach_uow.py infra/db/repositories/outreach.py infra/db/tables.py infra/db/outbox.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py
git diff --check
```

After independent review and any RED→GREEN fixes, stage only the listed files, verify new modes `100644`, staged sensitive scan and cached diff, then:

```bash
git commit -m "feat(outreach): add postgres persistence"
git push origin codex/phase1-implementation
```

Wait for exact HEAD CI success before Task 3.

---

### Task 3: Campaign Versioning, Approval, and Lifecycle Service

**Files:**
- Create: `domains/outreach/service_impl.py`
- Create: `tests/outreach_fakes.py`
- Create: `tests/unit/test_outreach_service.py`
- Create: `tests/integration/test_outreach_campaign_lifecycle.py`

**Interfaces:**
- Consumes: Task 1 `OutreachService`, providers, authorizer/audit, models/schemas; Task 2 `OutreachUnitOfWorkFactory`.
- Produces: `OutreachServiceImpl` with fully implemented Campaign create/submit/revise/activate/pause/cancel/get/list and reusable authorization/audit/UoW helpers for Tasks 4–5.

- [ ] **Step 1: Build shared behavioral fakes, not implementation mocks**

Create `tests/outreach_fakes.py` with:

```python
@dataclass
class Trace:
    calls: list[tuple[str, object, ...]] = field(default_factory=list)

class FakeAudit:
    def __init__(self, trace: Trace) -> None:
        self.trace = trace
        self.records: list[dict[str, str]] = []

    def log(self, *, actor, action, tenant_id, scope, rule) -> None:
        self.trace.calls.append(("audit", action, rule))
        self.records.append({
            "actor": actor, "action": action, "tenant_id": str(tenant_id),
            "scope": scope, "rule": rule,
        })

class FakeUow:
    async def __aenter__(self):
        self.trace.calls.append(("uow_enter",))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.trace.calls.append(("uow_exit", exc_type))
        if exc_type is None:
            self.store.commit_snapshot()
        else:
            self.store.restore_snapshot()
```

The fake store must preserve committed vs working snapshots, atomic outcomes and optional commit failure. It must not reproduce Campaign validation, permission matrices, sender selection, quota decisions or state transitions; those remain production behavior observed by tests.

- [ ] **Step 2: Write authorizer-first and audit-order RED tests for every Campaign method**

Create a parameterized trace test:

```python
@pytest.mark.parametrize(
    ("method", "action"),
    [
        ("create_campaign", "campaign:create"),
        ("submit_campaign", "campaign:submit"),
        ("revise_campaign", "campaign:revise"),
        ("activate_campaign", "campaign:activate"),
        ("pause_campaign", "campaign:pause"),
        ("cancel_campaign", "campaign:cancel"),
        ("get_campaign", "campaign:read"),
        ("list_campaigns", "campaign:list"),
    ],
)
async def test_campaign_methods_preauthorize_before_clock_provider_or_uow(method, action):
    service, trace = build_service(preauthorize_error=PermissionDenied("拒绝"))
    with pytest.raises(PermissionDenied):
        await invoke_campaign_method(service, method)
    assert trace.calls == [("preauthorize", action)]
```

For each method, test success order includes resource `require`, UoW exit, then exactly one allow; permission/provider/validation/not-found/state/commit failure writes zero allow. A denied preauthorization writes exactly one fixed `deny:authorization` and never reads the clock.

- [ ] **Step 3: Write Campaign behavior RED tests**

Tests must prove:

- create validates the whole boundary, calls every sender provider once, rejects wrong tenant/ID/role/auth and does no UoW write on provider failure;
- create accepts zero remaining sender quota but only authenticated `COLD_OUTREACH` role;
- initial submit is `DRAFT→PENDING_APPROVAL`; repeat same state is idempotent only if no data differs;
- activate accepts exact current version `APPROVED` snapshot and stores its approval ID/by/time;
- missing, pending, rejected, expired, wrong tenant/campaign/version approval rejects;
- pause keeps version; `activate_campaign` resumes PAUSED only after rechecking exact approval;
- revise appends version+1, leaves old version/steps byte-for-byte unchanged, clears current activation approval and enters PENDING;
- old approval cannot activate new version;
- cancel/completed terminal transitions cannot reopen;
- list repository rows are all re-authorized; one out-of-scope row fails the whole result with zero allow;
- returned views defensively copy tuples/lists and use the injected UTC day for quota fields.

- [ ] **Step 4: Write real PostgreSQL lifecycle/rollback RED tests**

Create `tests/integration/test_outreach_campaign_lifecycle.py` using Task 2 UoW:

```python
async def test_exact_version_approval_and_revision_are_durable(
    outreach_service, approvals, fresh_outreach_reader
):
    service = outreach_service
    created = await service.create_campaign(tenant, request_v1, actor=boss)
    await service.submit_campaign(tenant, created.campaign_id, actor=boss)
    approvals.put(approved_snapshot(created.campaign_id, 1))
    active = await service.activate_campaign(tenant, created.campaign_id, actor=boss)
    revised = await service.revise_campaign(tenant, created.campaign_id, request_v2, actor=boss)
    assert revised.version == 2
    with pytest.raises(CampaignApprovalRequiredError):
        await service.activate_campaign(tenant, created.campaign_id, actor=boss)
    assert await fresh_outreach_reader.read_version(
        tenant, created.campaign_id, 1
    ) == frozen_v1
```

Add real commit unique failure asserting Campaign/current version/steps/action/outbox all rollback, wrong tenant CRITICAL log has only safe fields, and a fresh session can read the exact approval reference after activation.

- [ ] **Step 5: Run genuine RED**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_outreach_service.py \
  tests/integration/test_outreach_campaign_lifecycle.py -q -W error
```

Expected: failures are missing `OutreachServiceImpl`; Task 1/2 contract, fixtures, migrations and fakes collect cleanly.

- [ ] **Step 6: Implement the service shell and safe authorization boundary**

Use this constructor exactly:

```python
class OutreachServiceImpl:
    def __init__(
        self,
        uow_factory: OutreachUnitOfWorkFactory,
        contact_eligibility: ContactEligibilityProvider,
        sending_identity_eligibility: SendingIdentityEligibilityProvider,
        approval_provider: CampaignApprovalProvider,
        reply_status_provider: ReplyStatusProvider,
        authorizer: OutreachAuthorizer,
        audit: AuditLogger,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._contacts = contact_eligibility
        self._senders = sending_identity_eligibility
        self._approvals = approval_provider
        self._replies = reply_status_provider
        self._authorizer = authorizer
        self._audit = audit
        self._now = now if now is not None else lambda: datetime.now(UTC)
```

`_preauthorize` must reject `None`/foreign actor before dereferencing and log fixed unknown deny; `_require` logs only deny, never allow; `_allow` is called only after `async with` exits successfully. Tenant isolation helper emits one fixed CRITICAL without entity payload.

- [ ] **Step 7: Implement Campaign methods with stable action keys**

Create uses `CampaignId(new_id("cmp"))`, version 1, canonical sorted/frozen boundary, and action key `campaign:{campaign_id}:v1:create`. Revision action key is `campaign:{campaign_id}:v{next_version}:revise`; activation key includes approval ID and version. Each state mutation and action row share the UoW; repeated action keys return current durable view only when canonical content matches.

Activation performs this exact resource validation before state mutation:

```python
approval = await self._approvals.get_campaign_approval(
    tenant_id, campaign.campaign_id, campaign.current_version
)
if (
    approval is None
    or approval.tenant_id != tenant_id
    or approval.campaign_id != campaign.campaign_id
    or approval.version != campaign.current_version
    or approval.state is not CampaignApprovalState.APPROVED
):
    raise CampaignApprovalRequiredError("Campaign 当前版本缺少有效审批")
```

Never persist provider exception strings or approval free-form notes.

- [ ] **Step 8: Run focused/full gates, review, commit, push, and wait for CI**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" pytest tests/unit/test_outreach_service.py tests/integration/test_outreach_campaign_lifecycle.py -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" ruff check domains/outreach/service_impl.py tests/outreach_fakes.py tests/unit/test_outreach_service.py tests/integration/test_outreach_campaign_lifecycle.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" mypy domains/outreach/service_impl.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py
git diff --check
```

Independently review exact method order, ABAC, approval version binding, rollback and payload safety. Close every Critical/Important with new tests. Stage exact files, scan staged content, commit and push:

```bash
git commit -m "feat(outreach): implement campaign lifecycle"
git push origin codex/phase1-implementation
```

Wait for exact HEAD CI success before Task 4.

---

### Task 4: Enrollment, Round-Robin, Campaign Quotas, and Message Attempts

**Files:**
- Modify: `domains/outreach/service_impl.py`
- Modify: `tests/outreach_fakes.py`
- Create: `tests/unit/test_outreach_enrollment_service.py`
- Create: `tests/integration/test_outreach_enrollment_lifecycle.py`
- Create: `tests/integration/test_outreach_concurrency.py`

**Interfaces:**
- Consumes: Task 1–3 contracts, concrete service shell and all Task 2 atomic repositories.
- Produces: `enroll`, `prepare_message_attempt`, `record_sent`, `record_send_failure`, `stop_enrollment`, `get_enrollment`, `list_enrollments`; durable round-robin cursor and atomic Campaign quota behavior.

- [ ] **Step 1: Write eligibility and Campaign-boundary RED tests**

Create independent snapshot mutations:

```python
@pytest.mark.parametrize(
    "mutation",
    [
        {"tenant_id": OTHER_TENANT},
        {"contact_point_id": OTHER_CONTACT},
        {"account_id": OTHER_ACCOUNT},
        {"verification": ContactVerificationStatus.RISKY},
        {"legal_basis": None},
        {"contact_belongs_to_account": False},
        {"country": "DE"},
        {"entity_type": "retailer"},
        {"qualified_categories": frozenset({"unapproved"})},
    ],
)
async def test_enroll_rejects_each_untrusted_contact_snapshot_mutation(mutation):
    service, store = active_campaign_service(contact_snapshot=contact(**mutation))
    with pytest.raises(ContactNotEligibleError):
        await service.enroll(TENANT, CAMPAIGN, enrollment_request(), actor=manager)
    assert store.enrollments == {}
    assert store.quotas == {}
```

Add sender snapshot mutations for wrong tenant/ID/role/auth/sendable/remaining. Contact-provider failures occur before write UoW; sender-provider failures may occur under the Campaign lock but must roll back cursor/quota/enrollment and yield zero allow.

- [ ] **Step 2: Write enrollment idempotency, scope, and round-robin RED tests**

Tests must lock these exact outcomes:

- same idempotency key + same campaign/contact/account returns original Enrollment and does not move cursor or quota;
- same key + changed payload raises `IdempotencyConflictError`;
- different key + same account across another Campaign raises `AccountAlreadyEnrolledError`;
- manager must match every configured campaign/account dimension; system and sales cannot enroll;
- canonical sender order is independent of request order; cursor persists A→B→A across committed enrollments;
- ineligible candidate is skipped, but cursor advances only to the committed winner;
- no winner leaves cursor/quota/enrollment unchanged;
- quota date comes only from injected UTC clock.

- [ ] **Step 3: Write prepare/record behavior and failure RED tests**

Cover:

```python
async def test_prepare_is_not_send_authorization_and_uses_stable_key():
    attempt = await service.prepare_message_attempt(TENANT, ENROLLMENT, actor=system)
    assert attempt.idempotency_key == IdempotencyKey(
        f"{TENANT}:{CAMPAIGN}:{ENROLLMENT}:v1:step1"
    )
    assert attempt.state is MessageAttemptState.RESERVED
    assert not hasattr(attempt, "authorized")
```

Also prove current reply commits Enrollment `REPLIED` plus one Action, creates no attempt/quota, then raises fixed `ReplyAlreadyReceivedError` after the UoW exits; current contact/account suppression does the same with `STOPPED_SUPPRESSED` and `SuppressedError`. These are committed non-send outcomes, not send authorization: they write zero allow audit and never publish `MessageSent`. Campaign PAUSED/current quota cap/sender unavailable/due time not reached reject with zero writes; Enrollment-bound old version supplies step content while current active version supplies total-message limit; same attempt key returns original quota snapshot; same key corrupted row fails closed.

For results:

- `record_sent` same provider ref is no-op, different ref conflicts;
- first success changes Attempt to SENT, advances current step and computes `next_send_at = service_now + next_step.wait_days`; final step completes Enrollment;
- `PROVIDER_TRANSIENT` yields FAILED_TRANSIENT and keeps sequence retryable with the same key;
- `IDENTITY_UNAVAILABLE` yields FAILED_PERMANENT and `STOPPED_IDENTITY_UNAVAILABLE`;
- hard bounce/complaint/unsubscribe are rejected as failure categories and must use suppression;
- all result paths lock Campaign→Enrollment→Attempt and publish `MessageSent` exactly once only after SENT.

- [ ] **Step 4: Write real PostgreSQL concurrency RED tests**

In `tests/integration/test_outreach_concurrency.py` run with independent sessions:

```python
results = await asyncio.gather(
    *(attempt_enroll_or_none(
        service_factory(), same_account, IdempotencyKey(new_id("idem"))
      ) for _ in range(20))
)
assert sum(result is not None for result in results) == 1
assert await count_active_enrollments(tenant, same_account) == 1
```

Add 20 distinct-account enrollments against cap 5, 20 Message Attempt creations against message cap 7, same attempt key ×20 exactly one quota increment, two Campaigns competing for one account, and two concurrent cursor allocations producing distinct A/B winners without lost update. Assert durable counts through a fresh session, not service return text.

- [ ] **Step 5: Run genuine RED**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_outreach_enrollment_service.py \
  tests/integration/test_outreach_enrollment_lifecycle.py \
  tests/integration/test_outreach_concurrency.py -q -W error
```

Expected: failures only because Task 4 service methods are absent; Task 2 atomic primitives and Task 3 campaign behavior remain green.

- [ ] **Step 6: Implement enroll with authoritative checks and fixed lock order**

The observable order must be:

```text
preauthorize
contact provider snapshot
UoW enter
campaign FOR UPDATE + current version
resource require
contact/account suppression lookup
atomic new-contact quota reserve
sender eligibility snapshots + persistent round-robin winner
atomic enrollment insert
action insert
UoW exit/commit
allow audit
```

If a late conflict occurs, the UoW rolls back quota and cursor. Repository outcomes distinguish same-key existing from account conflict; the service compares canonical payload before returning an existing Enrollment.

- [ ] **Step 7: Implement attempt preparation and result recording**

Preparation locks Campaign then Enrollment, reads exact Enrollment version, checks due/reply/suppression/contact/sender, reserves current Campaign message quota and inserts Attempt. Enrollment creation uses `EnrollmentId(new_id("enr"))`; preparation uses `MessageAttemptId(new_id("mat"))`, `MessageId(new_id("msg"))`, and the stable key from the spec.

`record_sent` locks Campaign→Enrollment→Attempt, compares provider ref, updates Attempt and Enrollment, adds Action, then publishes:

```python
await uow.bus.publish(
    MessageSent(
        tenant_id=tenant_id,
        occurred_at=now,
        run_id=None,
        message_id=attempt.message_id,
        campaign_id=attempt.campaign_id,
        sending_identity_id=attempt.sending_identity_id,
    )
)
```

Never pass contact address, body, provider response or failure text to the event.

- [ ] **Step 8: Run gates, independent review, commit, push, and wait for CI**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" pytest tests/unit/test_outreach_service.py tests/unit/test_outreach_enrollment_service.py tests/integration/test_outreach_campaign_lifecycle.py tests/integration/test_outreach_enrollment_lifecycle.py tests/integration/test_outreach_concurrency.py -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" ruff check domains/outreach/service_impl.py tests/outreach_fakes.py tests/unit/test_outreach_enrollment_service.py tests/integration/test_outreach_enrollment_lifecycle.py tests/integration/test_outreach_concurrency.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" mypy domains/outreach/service_impl.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py
git diff --check
```

Review must probe mutation resistance for provider mismatch, cursor rollback, cap races, lock order, old/current version semantics, provider ref idempotency, commit-failure audit and outbox ownership. Fix Critical/Important with new tests, then:

```bash
git commit -m "feat(outreach): enroll contacts and reserve attempts"
git push origin codex/phase1-implementation
```

Wait for exact HEAD CI success before Task 5.

---

### Task 5: Global Suppression and Cross-Campaign Stop

**Files:**
- Modify: `domains/outreach/service_impl.py`
- Modify: `tests/outreach_fakes.py`
- Create: `tests/unit/test_outreach_suppression_service.py`
- Create: `tests/integration/test_outreach_suppression.py`
- Modify: `tests/integration/test_outreach_concurrency.py`

**Interfaces:**
- Consumes: Task 1 typed target/reason/request, Task 2 atomic suppression/enrollment/outbox repositories, Task 3–4 service shell and lock order.
- Produces: `add_suppression`, `is_suppressed`, `list_suppressions`; immutable global suppression facts that atomically stop every matching active Enrollment across Campaigns.

- [ ] **Step 1: Write target, reason, permission and idempotency RED tests**

Create `tests/unit/test_outreach_suppression_service.py` with:

```python
@pytest.mark.parametrize(
    "reason",
    [
        SuppressionReason.UNSUBSCRIBE,
        SuppressionReason.COMPLAINT,
        SuppressionReason.HARD_BOUNCE,
    ],
)
async def test_exact_target_system_may_add_only_automatic_reason(reason):
    result = await service.add_suppression(
        TENANT, suppression_request(reason=reason), actor=system_for(TARGET)
    )
    assert result.created is True

@pytest.mark.parametrize(
    "reason",
    [
        SuppressionReason.MANUAL_BLOCK,
        SuppressionReason.COMPETITOR,
        SuppressionReason.EXISTING_CUSTOMER_CONFLICT,
    ],
)
async def test_system_cannot_add_human_reason(reason):
    with pytest.raises(PermissionDenied):
        await service.add_suppression(
            TENANT, suppression_request(reason=reason), actor=system_for(TARGET)
        )
```

Boss/TENANT may add all six; manager/sales may add none. Wrong/multiple/unrestricted target scopes reject before clock/UoW. Same key+same canonical payload returns original result; same key with changed target/reason/source ref/occurred time raises fixed `IdempotencyConflictError`.

- [ ] **Step 2: Write global stop, read scope, and audit RED tests**

Seed active/terminal Enrollments across three Campaigns and two targets. Contact suppression must stop only exact contact rows; account suppression must stop all contacts for that account. Terminal rows stay unchanged. Assert one Action per changed Enrollment, one suppression Action, one outbox event, exactly one post-commit allow, and no target/raw source in audit/log/error.

For reads, repository scope and service row reauthorization must both apply. A malicious fake repository returning one out-of-scope suppression makes the whole list fail with zero allow. Provider/repository read failure propagates; `is_suppressed` never returns `None` on backend failure.

- [ ] **Step 3: Write real PostgreSQL atomicity and concurrency RED tests**

Create `tests/integration/test_outreach_suppression.py` and extend concurrency tests:

```python
results = await asyncio.gather(
    *(service_factory().add_suppression(TENANT, same_request, actor=system)
      for _ in range(20))
)
assert sum(result.created for result in results) == 1
assert await count_suppressions(TENANT, TARGET) == 1
assert await count_outbox(TENANT, "SuppressionAdded") == 1
```

Also prove:

- account suppression stops active Enrollments in different Campaigns in canonical enrollment ID order;
- same contact/account IDs in another tenant remain active;
- forced action/outbox/commit failure rolls back suppression and every Enrollment state;
- UPDATE/DELETE suppression through SQL is rejected;
- prepare-vs-suppress race leaves either no Attempt or a RESERVED Attempt plus stopped Enrollment, but never `MessageSent`; 4B-2 can therefore recheck and block it;
- deadlock/serialization exceptions propagate unchanged without parsing error text.

- [ ] **Step 4: Run genuine RED**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_outreach_suppression_service.py \
  tests/integration/test_outreach_suppression.py \
  tests/integration/test_outreach_concurrency.py -q -W error
```

Expected: failures are missing Task 5 service behavior; Task 4 enrollment/quota/attempt tests stay green.

- [ ] **Step 5: Implement add_suppression as one UoW**

Use this exact order:

```text
preauthorize actor/action/reason
validate UTC occurred_at <= now+5m, safe source_ref and key
UoW enter
resource require exact typed target/reason
append_if_absent suppression
if EXISTING: compare full canonical payload, return without new actions/outbox
if CREATED: lock matching active enrollments by enrollment_id ASC
transition each to STOPPED_SUPPRESSED + append per-row action
append suppression action
publish one SuppressionAdded
UoW exit/commit
allow audit
```

Use `SuppressionId(new_id("sup"))`; outbox scope/reason values come only from typed enums. Action keys are `suppression:{suppression_id}:add` and `enrollment:{enrollment_id}:suppression:{suppression_id}`.

- [ ] **Step 6: Implement fail-closed suppression reads**

`is_suppressed` must query the exact typed contact/account target with covering indexes and return `SuppressionView | None`; it catches no storage exception. `list_suppressions` validates `1 <= limit <= 200`, authorizes the requested scope before validation, applies SQL scope before LIMIT, then resource-authorizes every returned row before assembling copies.

- [ ] **Step 7: Run all outreach gates and independent security review**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" pytest tests/unit/test_outreach_*.py -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" pytest tests/integration/test_outreach_*.py -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" ruff check domains/outreach infra/db/repositories/outreach.py infra/db/outreach_uow.py tests/outreach_fakes.py tests/unit/test_outreach_*.py tests/integration/test_outreach_*.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" mypy domains/outreach infra/db/repositories/outreach.py infra/db/outreach_uow.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/scan_sensitive.py
git diff --check
```

Reviewer must attempt authorization bypass, cross-tenant IDs, suppression deletion, idempotency payload mismatch, partial rollback, prepare race, audit leakage and query-failure-as-allow mutations. Close all Critical/Important via TDD.

- [ ] **Step 8: Commit, push, and wait for CI**

Stage only listed files, verify staged modes/sensitive/diff, then:

```bash
git commit -m "feat(outreach): enforce global suppression"
git push origin codex/phase1-implementation
```

Wait for exact HEAD CI success before Task 6.

---

### Task 6: Offline PostgreSQL Demo, Architecture Docs, and Final Acceptance

**Files:**
- Create: `scripts/demo_outreach.py`
- Create: `tests/integration/test_demo_outreach.py`
- Modify: `domains/outreach/AGENTS.md`
- Modify: `docs/architecture/08-compliance.md`
- Modify: `docs/architecture/10-database.md`

**Interfaces:**
- Consumes: complete Task 1–5 public service and concrete UoW.
- Produces: a no-network process-level demo with independent database readback; synchronized domain/compliance/database documentation; final Slice 4B-1 acceptance evidence.

- [ ] **Step 1: Write process-level demo RED tests before creating the script**

Create `tests/integration/test_demo_outreach.py`. Run the script twice against the same migrated test database with the exact environment `{"DATABASE_URL": str(db_url)}`. On success require one JSON line with only:

```python
_SUMMARY_KEYS = {
    "tenant_id",
    "campaign_id",
    "campaign_version",
    "enrollment_ids",
    "message_attempt_id",
    "suppression_id",
    "stopped_count",
    "outbox_counts",
}
```

Do not trust the summary for business success. Use a fresh engine/session and the reported tenant to prove:

- exactly one Campaign with version 1 ACTIVE and exact approval reference;
- two Sequence Steps, first DISCOVERY/0 days;
- two Enrollments for distinct accounts, both originally distributed across two sender IDs;
- one RESERVED Message Attempt and one Campaign message quota reservation;
- one account suppression that leaves the matching Enrollment `STOPPED_SUPPRESSED` and the other active;
- exact Actions and one `SuppressionAdded` outbox; no `MessageSent` because no connector was called;
- a second run has a different tenant/business IDs and does not mutate the first run.

Invalid DSN must return nonzero with stdout empty and stderr exactly `触达演示运行失败\n`; the DSN marker must appear nowhere.

Successful stderr may contain only newline-delimited audit JSON records with exactly `message/actor/action/tenant_id/scope/rule`; every `message` is `授权审计`, every tenant matches the summary tenant, and no contact address, Campaign name, target ID, provider/source reference, DSN or credential marker appears in stdout or stderr.

- [ ] **Step 2: Run the script-missing genuine RED**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/integration/test_demo_outreach.py -q -W error
```

Expected: subprocess fails only because `scripts/demo_outreach.py` does not exist. Testcontainers, Alembic 0009 and Python environment must be healthy.

- [ ] **Step 3: Implement a demo-only fail-closed composition**

The script reads only `os.environ["DATABASE_URL"]`, creates one engine, and disposes it in `finally`. It defines local providers that require the exact random tenant and exact known IDs; any other tenant/resource raises `PermissionDenied`. It must use:

```python
service = OutreachServiceImpl(
    lambda tenant: SqlAlchemyOutreachUnitOfWork(factory, tenant, now=clock.now),
    contact_eligibility=demo_contacts,
    sending_identity_eligibility=demo_senders,
    approval_provider=demo_approvals,
    reply_status_provider=demo_replies,
    authorizer=Phase1OutreachAuthorizer(tenant_id),
    audit=StandardAuditLogger(_AUDIT_LOGGER_NAME),
    now=clock.now,
)
```

All eight outreach tables are written only through public service/UoW. The local provider data is safe typed metadata and is never presented as production composition. Catch `Exception` only at the process boundary, print the fixed failure line without traceback, and never log the DSN.

- [ ] **Step 4: Synchronize domain and architecture documentation**

Update `domains/outreach/AGENTS.md` to state:

- exact Campaign and Enrollment state graphs;
- immutable version/reapproval rule;
- no public suppression removal path;
- Message Attempt is not authorization and 4B-2 must recheck;
- exact provider and dependency boundaries;
- exact six suppression reasons and three SYSTEM-allowed automatic reasons.

Update `08-compliance.md` with append-only minimal suppression facts, typed target scopes, no delete API and fail-closed lookup. Update `10-database.md` from old generic `campaigns/suppression_entries` names to the eight exact 0009 tables, partial active-account uniqueness, immutable/monotonic triggers and tenant composite keys.

- [ ] **Step 5: Run demo and documentation verification**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" pytest tests/integration/test_demo_outreach.py -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" ruff check scripts/demo_outreach.py tests/integration/test_demo_outreach.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" mypy scripts/demo_outreach.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/scan_sensitive.py scripts/demo_outreach.py tests/integration/test_demo_outreach.py domains/outreach/AGENTS.md docs/architecture/08-compliance.md docs/architecture/10-database.md
git diff --check
```

- [ ] **Step 6: Run final Slice 4B-1 acceptance gates**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" pytest tests/unit/test_outreach_*.py -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" pytest tests/integration/test_outreach_*.py tests/integration/test_demo_outreach.py -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" make check
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" pytest tests/integration -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/check_boundaries.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/scan_sensitive.py
git diff --check
```

Confirm no `node_modules`, `dist`, coverage, AppleDouble, temporary schema, credentials, live network calls or unapproved files remain.

- [ ] **Step 7: Conduct final broad review against the approved spec**

Freeze `Task 1 base..Task 6 HEAD` and independently audit every acceptance item:

1. exact-version approval and immutable revision;
2. provider fail-closed current facts;
3. account uniqueness and quota/idempotency concurrency;
4. global immutable suppression and atomic cross-Campaign stop;
5. Message Attempt cannot authorize external send;
6. tenant/UoW/outbox/audit atomicity;
7. safe payload/log/error/demo boundaries;
8. full tests, local gates and exact CI.

Any Critical/Important requires a new behavior RED, a minimal fix, affected/full gates and an ordinary follow-up commit; do not amend.

- [ ] **Step 8: Commit, push, and verify exact HEAD CI**

```bash
git add --chmod=-x -- scripts/demo_outreach.py tests/integration/test_demo_outreach.py domains/outreach/AGENTS.md docs/architecture/08-compliance.md docs/architecture/10-database.md
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" python3 scripts/scan_sensitive.py --staged --quiet
git diff --cached --check
git commit -m "feat(outreach): add offline postgres demo"
git push origin codex/phase1-implementation
gh run list --commit "$(git rev-parse HEAD)" --limit 5
```

Wait for the exact HEAD run to finish with `success`. Only then mark Slice 4B-1 complete; the wider project goal remains active for 4B-2 and later slices.

## Plan Completion Checklist

- [ ] Every approved spec section maps to a Task above.
- [ ] No Task imports another domain from `domains/outreach`.
- [ ] No Task calls connector/network or produces UI.
- [ ] Every write path has preauth/resource auth/commit/audit evidence.
- [ ] Every persistence invariant has a real PostgreSQL mutation test.
- [ ] Every task ends in independent review, ordinary commit, push and exact HEAD CI.
- [ ] Final completion claims only Slice 4B-1, never the entire TradeOS framework.
