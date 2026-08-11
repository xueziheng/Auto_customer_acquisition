# Slice 4B2 Tool Gateway 与 Gmail 单封手动发送 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不保存邮件敏感内容、不绕过当前业务事实和发件身份限额的前提下，通过受保护 API、持久化 Tool Gateway 账本和 Gmail Connector 安全发送一封人工触发邮件。

**Architecture:** 采用同步 Tool Gateway + PostgreSQL durable ledger。Outreach 在短事务中重新核验并认领 `MessageAttempt`，Sending Identity 以同一幂等键原子预留发送额度，Gateway 在 Gmail 前持久化 `EXECUTING`；Gmail 结果不确定时进入人工对账，绝不依据一次搜索未命中自动重发。原始收件地址、主题、正文、退订链接和 OAuth Token 只存在于当前进程内存，不进入数据库、审计、日志或异常。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2.x async、PostgreSQL 16、Alembic、HTTPX、pytest、mypy、ruff。

## Global Constraints

- 开工前必须读取根目录及目标目录就近 `AGENTS.md`、`HANDBOOK.md`、本计划与已批准规格 `docs/superpowers/specs/2026-08-11-slice4b2-tool-gateway-gmail-design.md`。
- 所有 Python/test 命令使用 `PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH`；不得用系统旧 Python 冒充门禁。
- 严格 TDD：先写测试并取得 genuine RED，确认不是 import、fixture、Docker、PATH、warning 或 AppleDouble 假失败，再写最小 GREEN。
- 外部动作只能经 `tool_gateway/`；Connector 不读取业务表，域服务不导入 Connector 或 Tool Gateway 内部实现。
- `email.send` 的检查顺序固定为 `tenant → permission → suppression → approval → idempotency → rate_limit`；不得把幂等或限额放到 Gmail 调用之后。
- `MessageAttempt` 只是准备事实。每次真实发送前必须重新检查 Campaign、精确批准版本、Enrollment、回复、抑制、联系方式资格、发件身份绑定和发送额度。
- Gmail 没有本系统可依赖的强幂等 API。进入 Connector 后发生不确定结果时必须返回 `reconciliation_required`，自动重试不得因为一次搜索未命中而再次发送。
- 数据库、事件、日志、异常和 HTTP 响应不得保存或回显收件地址、发件地址、主题、正文、退订 URL、OAuth Token、完整请求头或 DSN。
- 客户端发送请求只允许 `subject` 与 `body`；tenant、actor、Attempt、Campaign、Enrollment、Contact Point、Account、发件身份、地址、Tool ID、幂等键、退订 URL 和 provider ref 均由服务端解析。
- 内容命中价格、折扣、交期、库存、付款条件、合同、认证、独家、保证或无法明确归类的商业承诺时固定返回 `approval_required`；本切片不接受 approval ref 自动放行。
- 所有查询强制 tenant filter；所有新业务表带 `tenant_id` 且 tenant 为复合键/索引首列。
- 不实现 Campaign 自动扫描、批量发送、发送 UI、Gmail 回复/退信 Worker、Playbook/国家政策包或 Gmail 之外的 Provider。
- 每个任务只提交自己列出的文件；新文件用 `git add --chmod=-x` 保证 index mode 100644；运行敏感扫描和 cached diff 后普通 commit，不 amend。
- 用户要求每个小任务提交并推送：每个任务 commit 后 push 当前 `codex/phase1-implementation`，并等待精确 commit SHA 的远端 CI 成功后进入下一任务。

---

## File Map

### Tool Gateway contracts and pipeline

- Create `tool_gateway/errors.py`: 固定、安全、可枚举的 Gateway/Connector 错误与结果分类。
- Create `tool_gateway/fingerprint.py`: HMAC-SHA-256 长度前缀指纹和 key-version provider，不暴露 secret。
- Modify `tool_gateway/manifest.py`: 严格 manifest、两阶段 handler、只读 registry。
- Modify `tool_gateway/pipeline.py`: 调用上下文、prepared call、durable pipeline orchestration 与安全结果。
- Modify `tool_gateway/checks/tenant.py`: tenant/current-resource gate。
- Modify `tool_gateway/checks/permission.py`: typed actor/tool permission gate。
- Modify `tool_gateway/checks/suppression.py`: 调 Outreach 公共 preflight，失败关闭。
- Modify `tool_gateway/checks/approval.py`: 确定性承诺内容 gate。
- Modify `tool_gateway/checks/idempotency.py`: 原子 canonical claim/duplicate/conflict/reclaim。
- Modify `tool_gateway/checks/rate_limit.py`: Outreach claim 后调用 Sending Identity 原子 slot reservation。
- Modify `domains/quotations/service.py`: 完成确定性 `contains_forbidden_commitment`。

### Persistence

- Create `migrations/versions/0010_tool_calls.py`: `tool_calls`、`tool_call_events`、约束、索引和 append-only guard。
- Modify `infra/db/tables.py`: ORM metadata 与 0010 完全一致。
- Create `tool_gateway/repository.py`: ledger record、claim outcome、repository/UoW Protocol。
- Create `infra/db/repositories/tool_calls.py`: tenant-scoped PostgreSQL repository 与精确 `ON CONFLICT`。
- Create `infra/db/tool_gateway_uow.py`: request-scoped session、commit/rollback/close，cleanup 不覆盖 primary。

### Outreach send claim

- Create `migrations/versions/0011_outreach_send_claim.py`: Attempt `sending` 状态、`send_claimed_at`、扩展 failure category CHECK。
- Modify `infra/db/tables.py`: Attempt ORM 新字段/约束。
- Modify `domains/outreach/models.py`: `SENDING` 状态、typed failure categories、状态机。
- Modify `domains/outreach/schemas.py`: `MessageSendPreflight` 与扩展 `MessageAttemptView`。
- Modify `domains/outreach/permissions.py`: `MESSAGE_SEND_PREFLIGHT`、`MESSAGE_SEND_CLAIM` actions。
- Modify `domains/outreach/repository.py`: Attempt 读取、Campaign→Enrollment→Attempt 锁能力。
- Modify `infra/db/repositories/outreach.py`: 精确锁序实现与新字段映射。
- Modify `domains/outreach/service.py`: 两个公共发送前契约。
- Modify `domains/outreach/service_impl.py`: 当前事实 preflight、原子 claim、失败/成功状态适配。

### Gmail connector and handler

- Modify `connectors/gmail/client.py`: typed Gmail request/result、search-before-send、错误分类、稳定 headers。
- Create `connectors/gmail/transport.py`: HTTP transport/secret resolver Protocol 与 Gmail API wire adapter。
- Create `tool_gateway/handlers/email_send.py`: ephemeral delivery material、prepare/HMAC fingerprint、execute。
- Modify `tool_gateway/handlers/__init__.py`: 显式导出 email handler types。

### API/runtime composition

- Modify `apps/api/runtime_config.py`: Gmail/指纹/退订 provider 的严格 secret refs 与显式 runtime 配置。
- Modify `apps/api/dependencies.py`: Outreach、Sending Identity、Tool Gateway、delivery/unsubscribe providers 的 configured dependencies。
- Modify `apps/api/composition/runtime.py`: 真实 services/UoWs/registry/checks/connector/handler 装配。
- Modify `apps/api/main.py`: 挂载 Campaign router，并把发送错误 schema 写入 OpenAPI。
- Modify `apps/api/routers/campaigns.py`: `POST /crm/message-attempts/{attempt_id}/send`。
- Modify `infra/.env.example`: 仅不可运行 placeholder refs，不放 token/address/default DSN。

### Recovery/demo/documentation

- Create `scripts/demo_gmail_manual_send.py`: controlled fake Gmail transport 的真实 Postgres/Gateway 演示，绝不发外部邮件。
- Create `tests/integration/test_demo_gmail_manual_send.py`: 子进程、DB readback、脱敏和 ambiguous reconciliation。
- Modify `tool_gateway/AGENTS.md`: durable ledger、两阶段 handler、ambiguous-send 规则。
- Modify `connectors/gmail/AGENTS.md`: Gmail 搜索不是强幂等证明、自动重发禁令。
- Modify `domains/outreach/AGENTS.md`: `SENDING`/claim/suppression 锁序。
- Modify `docs/architecture/04-tool-gateway.md`: 实际 Phase 1 流程、表、状态和恢复运维。

---

### Task 1: Typed Tool Contracts, HMAC Fingerprint, and Commitment Guard

**Files:**
- Create: `tool_gateway/errors.py`
- Create: `tool_gateway/fingerprint.py`
- Modify: `tool_gateway/manifest.py`
- Modify: `tool_gateway/pipeline.py`
- Modify: `domains/quotations/service.py`
- Create: `tests/unit/test_tool_gateway_contracts.py`
- Create: `tests/unit/test_tool_gateway_manifest.py`
- Create: `tests/unit/test_tool_gateway_fingerprint.py`
- Create: `tests/unit/test_quotation_commitment_guard.py`

**Interfaces:**
- Produces: `ToolErrorCategory`, `ToolCallStatus`, `DeliveryCertainty`, `ToolGatewayError`.
- Produces: `HmacFingerprintProvider.fingerprint(parts: tuple[bytes, ...]) -> tuple[str, str]`, returning `(64-lower-hex, key_version)`.
- Produces: `PreparedToolCall`, `ToolHandler.prepare(ctx)`, `ToolHandler.execute(tenant_id, prepared)`.
- Produces: strict `ToolRegistry` and unchanged global `STAGE_ORDER`.
- Consumes: existing `TenantId`, `UserId`, `RunId`, `IdempotencyKey`, `contains_forbidden_commitment` public boundary.

- [ ] **Step 1: Add failing contract tests for safe types and state invariants**

```python
def test_prepared_call_rejects_raw_or_non_safe_audit_values() -> None:
    with pytest.raises(ValidationError):
        PreparedToolCall(
            request_fingerprint="a" * 64,
            fingerprint_version="v1",
            audit_projection={"recipient": "buyer@example.com", "nested": {}},
            payload=object(),
        )


def test_tool_call_result_never_accepts_raw_output() -> None:
    with pytest.raises(ValidationError):
        ToolCallResult(
            tool_id="email.send",
            status=ToolCallStatus.SUCCEEDED,
            output={"body": "secret customer text"},
        )
```

The accepted safe output keys are fixed to `provider_ref`, `already_existed`, `duplicate`, `retry_after_seconds`, and opaque typed IDs; arbitrary nested dictionaries are rejected.

- [ ] **Step 2: Run the contract tests and certify genuine RED**

Run:

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_tool_gateway_contracts.py -q -W error
```

Expected: collection succeeds; failures are missing enums/dataclasses/validators, not import-path or warning failures.

- [ ] **Step 3: Add failing registry and two-stage handler tests**

```python
EMAIL_CHECKS = (
    "tenant", "permission", "suppression", "approval", "idempotency", "rate_limit"
)


def test_email_send_manifest_requires_exact_checks_and_required_idempotency() -> None:
    registry = ToolRegistry()
    registry.register(_manifest(checks=EMAIL_CHECKS), _handler())
    manifest, handler = registry.get("email.send")
    assert manifest.checks == EMAIL_CHECKS
    assert isinstance(handler, ToolHandler)


@pytest.mark.parametrize("missing", ["suppression", "approval", "idempotency"])
def test_high_risk_registration_fails_closed_when_required_check_is_missing(missing: str) -> None:
    with pytest.raises(ValidationError):
        ToolRegistry().register(
            _manifest(checks=tuple(item for item in EMAIL_CHECKS if item != missing)),
            _handler(),
        )
```

Also assert duplicate registration, unknown stage, out-of-order stage, blank permissions, mutable schema/redact containers, and a handler without both `prepare` and `execute` are rejected.

- [ ] **Step 4: Implement strict manifest, registry, and safe call types**

Implement these exact public shapes:

```python
class ToolErrorCategory(str, Enum):
    VALIDATION = "validation"
    PERMISSION_DENIED = "permission_denied"
    SUPPRESSED = "suppressed"
    APPROVAL_REQUIRED = "approval_required"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    IN_PROGRESS = "in_progress"
    RATE_LIMITED = "rate_limited"
    PROVIDER_AUTH_REQUIRED = "provider_auth_required"
    PROVIDER_PERMANENT = "provider_permanent"
    PROVIDER_TRANSIENT = "provider_transient"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    UNEXPECTED = "unexpected"


class ToolCallStatus(str, Enum):
    RECEIVED = "received"
    CLAIMED = "claimed"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    FAILED_TRANSIENT = "failed_transient"
    FAILED_PERMANENT = "failed_permanent"


@dataclass(frozen=True)
class PreparedToolCall:
    request_fingerprint: str
    fingerprint_version: str
    audit_projection: Mapping[str, str | int | bool | None]
    payload: object = field(repr=False, compare=False)


@runtime_checkable
class ToolHandler(Protocol):
    async def prepare(self, ctx: ToolCallContext) -> PreparedToolCall: ...
    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> Mapping[str, str | int | bool | None]: ...
```

Freeze copied `checks`, permissions, schemas, redact fields and audit mappings in `__post_init__`; reject blank/control-character values and all secret-like audit keys (`token`, `secret`, `password`, `authorization`, `recipient`, `subject`, `body`, `header`, `dsn`, `address`, `url`).

- [ ] **Step 5: Add failing HMAC fingerprint tests**

```python
def test_length_prefix_prevents_concatenation_collision() -> None:
    provider = HmacFingerprintProvider("fp-v1", b"x" * 32)
    assert provider.fingerprint((b"ab", b"c")) != provider.fingerprint((b"a", b"bc"))


def test_fingerprint_is_deterministic_and_never_contains_input() -> None:
    provider = HmacFingerprintProvider("fp-v1", b"k" * 32)
    digest, version = provider.fingerprint((b"buyer@example.com", b"body"))
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert version == "fp-v1"
    assert "buyer" not in digest
```

Reject weak keys, non-bytes parts, blank/control/secret-like key version, and part count/part length overflow. Verify changing one byte changes the fingerprint and the key is absent from `repr` and exception text.

- [ ] **Step 6: Implement domain-separated length-prefixed HMAC**

Encode each part as `4-byte big-endian length + bytes` and prefix the message with `b"tradeos.tool-call-fingerprint\x00v1\x00"`; calculate `hmac.new(key, encoded, hashlib.sha256).hexdigest()`. Keep key bytes in a private `repr=False` field and expose only the validated key version.

- [ ] **Step 7: Add independent multilingual commitment-guard RED tests**

```python
@pytest.mark.parametrize(
    "text",
    [
        "USD 12.50 per unit",
        "给您 8% 折扣",
        "库存现货 500 件",
        "delivery within 14 days",
        "Net 30 payment terms",
        "exclusive distributor",
        "CE certified",
        "we guarantee the quality",
        "签署正式合同后发货",
    ],
)
def test_forbidden_commitments_are_denied(text: str) -> None:
    matches = contains_forbidden_commitment(text)
    assert matches
    assert all(isinstance(item, ForbiddenAutoCommitment) for item in matches)
```

Add safe discovery controls such as `"Could you share the specification?"` and `"方便确认贵司当前采购需求吗？"`. Add mutation cases for punctuation, Unicode percent/currency symbols, casing, whitespace and ambiguous business numbers.

- [ ] **Step 8: Implement the deterministic commitment guard**

Normalize Unicode with NFKC and casefold for matching only; do not mutate the sent text. Reject control characters, overly large content and malformed inputs. Return a deterministic, de-duplicated list of existing `ForbiddenAutoCommitment` enum values in enum declaration order. Map price/currency, discount/percentage, inventory/quantity promise, delivery time/date, payment, contract, certification, exclusivity and guarantee to their existing enum members; map an ambiguous commercial number adjacent to commitment verbs conservatively to `FIRST_CONCRETE_PRICE`.

- [ ] **Step 9: Run Task 1 focused and structural gates**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_tool_gateway_contracts.py \
       tests/unit/test_tool_gateway_manifest.py \
       tests/unit/test_tool_gateway_fingerprint.py \
       tests/unit/test_quotation_commitment_guard.py -q -W error
ruff check tool_gateway domains/quotations/service.py tests/unit/test_tool_gateway_*.py tests/unit/test_quotation_commitment_guard.py
mypy tool_gateway domains/quotations/service.py
python3 scripts/check_boundaries.py
git diff --check
```

Expected: all pass; boundary checker reports all seven groups clean.

- [ ] **Step 10: Commit, push, and verify exact CI SHA**

```bash
git add --chmod=-x tool_gateway/errors.py tool_gateway/fingerprint.py \
  tool_gateway/manifest.py tool_gateway/pipeline.py domains/quotations/service.py \
  tests/unit/test_tool_gateway_contracts.py tests/unit/test_tool_gateway_manifest.py \
  tests/unit/test_tool_gateway_fingerprint.py tests/unit/test_quotation_commitment_guard.py
git diff --cached --check
python3 scripts/scan_sensitive.py --staged
git commit -m "feat(tool-gateway): define safe invocation contracts"
git push origin codex/phase1-implementation
gh run list --commit "$(git rev-parse HEAD)" --limit 1
```

Wait until the exact SHA is `success`; do not start Task 2 on a failed or absent run.

---

### Task 2: Durable Tool Call Ledger and PostgreSQL Claim Semantics

**Files:**
- Create: `migrations/versions/0010_tool_calls.py`
- Modify: `infra/db/tables.py`
- Create: `tool_gateway/repository.py`
- Create: `infra/db/repositories/tool_calls.py`
- Create: `infra/db/tool_gateway_uow.py`
- Create: `tests/unit/test_tool_gateway_uow.py`
- Create: `tests/integration/test_tool_call_repository.py`
- Modify: `tests/integration/test_migrations.py`
- Modify: `tests/integration/test_repositories.py`

**Interfaces:**
- Consumes: `ToolCallStatus`, `ToolErrorCategory`, safe scalar outputs from Task 1.
- Produces: `ToolCallRecord`, `ToolCallEventRecord`, `ClaimStatus`, `ClaimResult`, `ToolCallRepository`, `ToolGatewayUnitOfWork` and factory Protocol.
- Produces: Alembic head `0010` with exactly two new tenant-scoped tables.

- [ ] **Step 1: Write migration/ORM parity RED tests**

Assert exact columns, composite PK/FK, partial unique canonical key, tenant-first indexes, CHECK constraints, no forbidden content columns, event UPDATE/DELETE rejection, and `0010 → 0009 → 0010` roundtrip.

```python
FORBIDDEN_COLUMNS = {
    "params", "recipient", "sender", "subject", "body", "headers",
    "token", "authorization", "dsn", "unsubscribe_url", "exception",
}


async def test_tool_call_tables_never_expose_content_columns(engine) -> None:
    columns = await _columns(engine, "tool_calls") | await _columns(engine, "tool_call_events")
    assert not (columns & FORBIDDEN_COLUMNS)
```

- [ ] **Step 2: Run migration RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/integration/test_migrations.py tests/integration/test_repositories.py \
  -q -W error -k 'tool_call or metadata_parity or roundtrip'
```

Expected: genuine failures for missing 0010/tables/ORM metadata; existing 0001–0009 migrations still pass.

- [ ] **Step 3: Define repository records and claim outcomes**

```python
class ClaimStatus(str, Enum):
    CLAIMED = "claimed"
    DUPLICATE = "duplicate"
    IN_PROGRESS = "in_progress"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class ClaimResult:
    status: ClaimStatus
    canonical: ToolCallRecord


@runtime_checkable
class ToolCallRepository(Protocol):
    async def create_received(self, record: ToolCallRecord) -> None: ...
    async def claim(
        self,
        tenant_id: TenantId,
        tool_call_id: ToolCallId,
        *,
        tool_id: str,
        idempotency_key: IdempotencyKey,
        request_fingerprint: str,
        fingerprint_version: str,
        lease_owner: str,
        lease_expires_at: datetime,
    ) -> ClaimResult: ...
    async def mark_executing(self, tenant_id: TenantId, tool_call_id: ToolCallId) -> None: ...
    async def complete(
        self,
        tenant_id: TenantId,
        tool_call_id: ToolCallId,
        *,
        status: ToolCallStatus,
        provider_ref: str | None,
        error_category: ToolErrorCategory | None,
        retry_after_at: datetime | None,
    ) -> None: ...
    async def append_event(self, event: ToolCallEventRecord) -> None: ...
    async def get(self, tenant_id: TenantId, tool_call_id: ToolCallId) -> ToolCallRecord | None: ...
```

Add `ToolCallId = NewType("ToolCallId", str)` to `tool_gateway/repository.py` rather than `shared`, because no other layer needs it in this slice.

- [ ] **Step 4: Implement 0010 and ORM metadata**

`tool_calls` has composite PK `(tenant_id, tool_call_id)`; a partial unique index on `(tenant_id, tool_id, idempotency_key) WHERE idempotency_key IS NOT NULL`; a tenant-scoped self-FK for `duplicate_of`; status-field CHECKs; safe provider-ref CHECK; nonnegative `attempt_count`; UTC timestamps.

`tool_call_events` has composite PK `(tenant_id, event_id)`, composite FK to call, fixed stage/outcome/category columns, integer `duration_ms >= 0`, tenant-first index, and a PostgreSQL trigger rejecting UPDATE/DELETE.

- [ ] **Step 5: Add real PostgreSQL claim/concurrency RED tests**

```python
async def test_twenty_concurrent_same_key_have_one_canonical_claim(factory) -> None:
    results = await asyncio.gather(*(_claim(factory, "same-key", "a" * 64) for _ in range(20)))
    assert Counter(item.status for item in results) == {
        ClaimStatus.CLAIMED: 1,
        ClaimStatus.IN_PROGRESS: 19,
    }


async def test_same_key_different_fingerprint_is_conflict(factory) -> None:
    first = await _claim(factory, "same-key", "a" * 64)
    second = await _claim(factory, "same-key", "b" * 64)
    assert first.status is ClaimStatus.CLAIMED
    assert second.status is ClaimStatus.CONFLICT
```

Cover: completed duplicate, unexpired lease in-progress, expired recoverable transient reclaim, EXECUTING reconciliation not auto-reclaim, cross-tenant same key isolation, commit failure rollback, event append-only guard, corrupted status fails closed, and no raw values in DB rows.

- [ ] **Step 6: Implement repository with exact conflict target**

Use PostgreSQL `INSERT ... ON CONFLICT DO NOTHING` for the canonical-key race; read the winner with all three tenant/tool/key predicates and `FOR UPDATE`. Never catch broad `IntegrityError`, never parse exception strings. Compare both fingerprint value and fingerprint version; a version mismatch is conflict, not a new claim.

- [ ] **Step 7: Implement UoW cleanup semantics**

Mirror the hardened existing UoWs: create one session per entry; commit on clean exit; rollback on body/commit failure; catch cleanup `BaseException` only to preserve an existing primary; propagate cleanup failure when there is no primary; log only fixed Chinese messages and exception type.

- [ ] **Step 8: Run focused real-PG and roundtrip gates**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_tool_gateway_uow.py \
       tests/integration/test_tool_call_repository.py \
       tests/integration/test_migrations.py \
       tests/integration/test_repositories.py -q -W error \
       -k 'tool_call or metadata_parity or roundtrip or cleanup'
ruff check migrations/versions/0010_tool_calls.py infra/db/tables.py \
  tool_gateway/repository.py infra/db/repositories/tool_calls.py \
  infra/db/tool_gateway_uow.py tests/unit/test_tool_gateway_uow.py \
  tests/integration/test_tool_call_repository.py
mypy tool_gateway/repository.py infra/db/repositories/tool_calls.py infra/db/tool_gateway_uow.py
python3 scripts/check_boundaries.py
git diff --check
```

- [ ] **Step 9: Commit, push, and wait for exact CI**

```bash
git add --chmod=-x migrations/versions/0010_tool_calls.py infra/db/tables.py \
  tool_gateway/repository.py infra/db/repositories/tool_calls.py infra/db/tool_gateway_uow.py \
  tests/unit/test_tool_gateway_uow.py tests/integration/test_tool_call_repository.py \
  tests/integration/test_migrations.py tests/integration/test_repositories.py
git diff --cached --check
python3 scripts/scan_sensitive.py --staged
git commit -m "feat(tool-gateway): persist durable invocation ledger"
git push origin codex/phase1-implementation
gh run list --commit "$(git rev-parse HEAD)" --limit 1
```

---

### Task 3: Outreach Current-Fact Preflight, Atomic Send Claim, and Gateway Stages

**Files:**
- Create: `migrations/versions/0011_outreach_send_claim.py`
- Modify: `infra/db/tables.py`
- Modify: `domains/outreach/models.py`
- Modify: `domains/outreach/schemas.py`
- Modify: `domains/outreach/permissions.py`
- Modify: `domains/outreach/repository.py`
- Modify: `infra/db/repositories/outreach.py`
- Modify: `domains/outreach/service.py`
- Modify: `domains/outreach/service_impl.py`
- Modify: `tool_gateway/pipeline.py`
- Modify: `tool_gateway/checks/tenant.py`
- Modify: `tool_gateway/checks/permission.py`
- Modify: `tool_gateway/checks/suppression.py`
- Modify: `tool_gateway/checks/approval.py`
- Modify: `tool_gateway/checks/idempotency.py`
- Modify: `tool_gateway/checks/rate_limit.py`
- Create: `tests/unit/test_outreach_send_claim.py`
- Create: `tests/unit/test_tool_gateway_pipeline.py`
- Create: `tests/integration/test_outreach_send_claim.py`
- Create: `tests/integration/test_tool_gateway_pipeline.py`
- Modify: `tests/unit/test_outreach_contracts.py`
- Modify: `tests/unit/test_outreach_models.py`
- Modify: `tests/integration/test_migrations.py`
- Modify: `tests/integration/test_repositories.py`

**Interfaces:**
- Consumes: Task 1 typed pipeline/fingerprint; Task 2 ledger UoW; existing Outreach providers; existing `SendingIdentityService.reserve_send_slot`.
- Produces: `MessageSendPreflight`, `OutreachService.preflight_message_send`, `OutreachService.claim_message_send`, `ToolGateway.invoke`.
- Produces: Alembic head `0011` and Attempt state `SENDING` with `send_claimed_at`.

- [ ] **Step 1: Add public contract and model RED tests**

```python
@dataclass(frozen=True)
class MessageSendPreflight:
    tenant_id: TenantId
    attempt_id: MessageAttemptId
    campaign_id: CampaignId
    enrollment_id: EnrollmentId
    account_id: ProspectAccountId
    contact_point_id: ContactPointId
    sending_identity_id: SendingIdentityId
    campaign_version: int
    step_number: int
    idempotency_key: IdempotencyKey


class OutreachService(Protocol):
    async def preflight_message_send(
        self, tenant_id: TenantId, attempt_id: MessageAttemptId, *, actor: Actor
    ) -> MessageSendPreflight: ...
    async def claim_message_send(
        self, tenant_id: TenantId, attempt_id: MessageAttemptId, *, actor: Actor
    ) -> MessageAttemptView: ...
```

Assert `MessageAttemptState.SENDING`, `send_claimed_at`, transitions `RESERVED/FAILED_TRANSIENT → SENDING → SENT/FAILED_*`, and invalid persisted combinations fail closed.

- [ ] **Step 2: Run contract/model RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_outreach_contracts.py tests/unit/test_outreach_models.py \
       tests/unit/test_outreach_send_claim.py -q -W error
```

- [ ] **Step 3: Add preflight current-fact matrix**

Build independent cases for inactive Campaign, wrong/unapproved version, stopped Enrollment, step mismatch, replied contact, contact/account suppression, unverified contact, wrong account, illegal basis, disallowed category/entity/country, and sending identity outside Campaign selection. Every failure must occur before claim/write, produce zero allow audit and expose no customer content.

```python
@pytest.mark.parametrize("mutation", CURRENT_FACT_MUTATIONS)
async def test_preflight_rejects_every_stale_fact(mutation, harness) -> None:
    mutation(harness)
    with pytest.raises(PolicyViolation):
        await harness.service.preflight_message_send(
            harness.tenant, harness.attempt_id, actor=harness.actor
        )
    assert harness.store.writes == []
    assert harness.audit.allow_count == 0
```

- [ ] **Step 4: Add atomic claim/suppression race RED tests**

Use real PostgreSQL and two independent sessions. Assert the lock order is Campaign → sorted Enrollment IDs → Attempt. In a controlled race, suppression-first must make claim reject; claim-first may commit `SENDING`, after which suppression stops future enrollment but does not rewrite this attempt.

Also assert 20 concurrent claims produce one transition/action, same attempt claim is idempotent, a different binding conflicts, commit failure leaves original state/action/outbox unchanged, and no Gmail/slot fake is called by the domain service.

- [ ] **Step 5: Implement 0011, model/repository mapping, and service methods**

Add `send_claimed_at TIMESTAMPTZ NULL`; update CHECKs so only `SENDING` requires it. Extend failure categories:

```python
class SendFailureCategory(str, Enum):
    RATE_LIMITED = "rate_limited"
    PROVIDER_TRANSIENT = "provider_transient"
    PROVIDER_AUTH_REQUIRED = "provider_auth_required"
    PROVIDER_PERMANENT = "provider_permanent"
    IDENTITY_UNAVAILABLE = "identity_unavailable"
```

Map rate-limited/transient/auth-required to `FAILED_TRANSIENT`; provider-permanent/identity-unavailable to `FAILED_PERMANENT`; only identity-unavailable stops Enrollment. `record_sent` accepts same provider ref idempotently and rejects a different ref. Reconciliation does not call either record method.

- [ ] **Step 6: Add pipeline sequencing and short-circuit RED tests**

Use call traces to assert:

```python
assert trace == [
    "ledger.received",
    "tenant",
    "permission",
    "suppression.preflight",
    "approval",
    "handler.prepare",
    "idempotency.claim",
    "outreach.claim",
    "rate_limit.reserve",
    "ledger.executing",
    "handler.execute",
    "outreach.record_sent",
    "ledger.succeeded",
]
```

For every stage failure, assert all later calls are absent. Specifically assert approval/material failure does not claim Attempt or reserve slot; Outreach claim failure does not reserve; ledger EXECUTING commit failure calls connector zero times; duplicate returns saved safe result and consumes no slot.

- [ ] **Step 7: Implement the six Gateway stages and orchestrator**

`ToolGateway.__init__` receives registry, ordered check mapping, ledger UoW factory, lease duration, lease owner, clock and ID factory. `invoke` creates/commits `RECEIVED`, runs only manifest stages in global order, calls handler `prepare` after approval and before idempotency, translates typed errors, appends safe events, and never stores `ctx.params` or prepared payload.

`SuppressionCheck` delegates all current facts to `outreach.preflight_message_send`; it does not duplicate domain logic. `RateLimitCheck` first calls `outreach.claim_message_send`, then `sending_identity.reserve_send_slot(..., for_cold_outreach=True)`. If reservation fails, it records the exact typed attempt failure and leaves connector untouched.

- [ ] **Step 8: Add crash/reclaim/commit semantics on real PostgreSQL**

Cover:

- crash before `EXECUTING`: lease expiration may reclaim after rerunning all current checks;
- crash after `EXECUTING`: returns `reconciliation_required`, never auto-executes;
- same key + same fingerprint completed: duplicate safe result;
- same key + changed subject/body/address fingerprint: conflict;
- canonical commit failure: no connector call;
- Gmail-success/local-completion failure: canonical remains reconciliation-required and Attempt remains `SENDING`.

- [ ] **Step 9: Run Task 3 focused gates and migration roundtrip**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_outreach_contracts.py tests/unit/test_outreach_models.py \
       tests/unit/test_outreach_send_claim.py tests/unit/test_tool_gateway_pipeline.py \
       tests/integration/test_outreach_send_claim.py \
       tests/integration/test_tool_gateway_pipeline.py \
       tests/integration/test_migrations.py tests/integration/test_repositories.py \
       -q -W error -k 'send_claim or tool_gateway or outreach or metadata_parity or roundtrip'
ruff check domains/outreach tool_gateway infra/db tests/unit/test_outreach_send_claim.py \
  tests/unit/test_tool_gateway_pipeline.py tests/integration/test_outreach_send_claim.py \
  tests/integration/test_tool_gateway_pipeline.py migrations/versions/0011_outreach_send_claim.py
mypy domains/outreach tool_gateway infra/db/repositories/outreach.py
python3 scripts/check_boundaries.py
git diff --check
```

- [ ] **Step 10: Commit, push, and verify exact CI**

Stage only the files listed in Task 3, inspect cached names/modes, run staged sensitive scan, then:

```bash
git commit -m "feat(tool-gateway): claim outreach sends atomically"
git push origin codex/phase1-implementation
gh run list --commit "$(git rev-parse HEAD)" --limit 1
```

---

### Task 4: Gmail Connector and Ephemeral Email Handler

**Files:**
- Modify: `connectors/gmail/client.py`
- Create: `connectors/gmail/transport.py`
- Create: `tool_gateway/handlers/email_send.py`
- Modify: `tool_gateway/handlers/__init__.py`
- Create: `tests/unit/test_gmail_connector.py`
- Create: `tests/unit/test_email_send_handler.py`
- Create: `tests/integration/test_gmail_connector_http.py`

**Interfaces:**
- Consumes: Task 1 `PreparedToolCall`, HMAC provider, safe errors; Task 3 `MessageSendPreflight`.
- Produces: `GmailSendRequest`, `GmailSendResult`, `GmailConnector.send_once`, `DeliveryMaterialProvider`, `UnsubscribeLinkProvider`, `EmailSendHandler`.

- [ ] **Step 1: Add typed Gmail request/result RED tests**

```python
@dataclass(frozen=True)
class GmailSendRequest:
    from_address: str = field(repr=False)
    recipient_address: str = field(repr=False)
    subject: str = field(repr=False)
    body: str = field(repr=False)
    unsubscribe_url: str = field(repr=False)
    deterministic_message_id: str
    idempotency_header: str


@dataclass(frozen=True)
class GmailSendResult:
    provider_ref: str | None
    certainty: DeliveryCertainty
    already_existed: bool
```

Validate canonical mailbox syntax without DNS/network lookup, UTF-8 size limits, CR/LF header injection, control characters, stable HTTPS unsubscribe URL, safe deterministic IDs and provider refs. Ensure `repr`, logs and errors exclude all raw fields.

- [ ] **Step 2: Run Gmail contract RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_gmail_connector.py -q -W error
```

- [ ] **Step 3: Add controlled HTTP transport and error-classification tests**

Use an injected `GmailHttpTransport` fake/local ASGI server. Assert:

- search finds deterministic Message-ID + custom header → return existing, no send;
- search misses before first send → exactly one send;
- 401/403 → provider-auth-required;
- 429 parses bounded integer Retry-After → rate-limited;
- 4xx other than 408/409/425/429 → provider-permanent;
- connection failure before request bytes → definitely-not-sent transient;
- timeout/reset after request may have been written → reconciliation-required;
- 5xx after Gmail accepts body → reconciliation-required, not automatic retry;
- response/log/exception does not contain token, message body, addresses or raw headers.

- [ ] **Step 4: Implement secret resolver and Gmail wire adapter**

```python
@runtime_checkable
class SecretResolver(Protocol):
    def resolve(self, secret_ref: str) -> str: ...


@runtime_checkable
class GmailHttpTransport(Protocol):
    async def search(self, *, token: str, message_id: str, header: str) -> str | None: ...
    async def send(self, *, token: str, raw_message: bytes) -> str: ...
```

`GmailConnector.configure` resolves only `GMAIL_OAUTH_TOKEN_REF`; keep token in a private `repr=False` value. Build RFC 5322 bytes with deterministic `Message-ID`, `X-TradeOS-Idempotency-V1`, `List-Unsubscribe`, and `List-Unsubscribe-Post: List-Unsubscribe=One-Click`. Do not log request/response bodies.

- [ ] **Step 5: Add handler prepare/execute RED tests**

```python
async def test_prepare_resolves_material_once_and_persists_only_safe_projection(harness) -> None:
    prepared = await harness.handler.prepare(harness.context)
    assert prepared.audit_projection == {
        "attempt_id": str(harness.attempt_id),
        "subject_bytes": len(harness.subject.encode()),
        "body_bytes": len(harness.body.encode()),
        "has_unsubscribe": True,
    }
    assert "buyer@example.com" not in repr(prepared)
    assert harness.gmail.calls == []
```

Test tenant/contact/account/identity binding mismatch, stale or missing delivery material, unstable unsubscribe link, key rotation comparison, raw whitespace preservation in fingerprint, one-byte changes, and secret-like provider output.

- [ ] **Step 6: Implement app-facing provider Protocols and handler**

```python
@runtime_checkable
class DeliveryMaterialProvider(Protocol):
    async def resolve(
        self, tenant_id: TenantId, preflight: MessageSendPreflight
    ) -> DeliveryMaterial: ...


@runtime_checkable
class UnsubscribeLinkProvider(Protocol):
    async def build(
        self, tenant_id: TenantId, preflight: MessageSendPreflight
    ) -> str: ...
```

`DeliveryMaterial` contains addresses only in `repr=False` fields. `EmailSendHandler.prepare` resolves both providers, validates exact preflight binding, creates deterministic Message-ID/header and HMAC request fingerprint, and returns ephemeral payload. `execute` calls only the configured `GmailConnector` and returns safe provider metadata.

- [ ] **Step 7: Run connector/handler tests and scanners**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_gmail_connector.py tests/unit/test_email_send_handler.py \
       tests/integration/test_gmail_connector_http.py -q -W error
ruff check connectors/gmail tool_gateway/handlers tests/unit/test_gmail_connector.py \
  tests/unit/test_email_send_handler.py tests/integration/test_gmail_connector_http.py
mypy connectors/gmail tool_gateway/handlers
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
```

- [ ] **Step 8: Commit, push, and verify exact CI**

```bash
git add --chmod=-x connectors/gmail/client.py connectors/gmail/transport.py \
  tool_gateway/handlers/email_send.py tool_gateway/handlers/__init__.py \
  tests/unit/test_gmail_connector.py tests/unit/test_email_send_handler.py \
  tests/integration/test_gmail_connector_http.py
git diff --cached --check
python3 scripts/scan_sensitive.py --staged
git commit -m "feat(gmail): add safe single-message connector"
git push origin codex/phase1-implementation
gh run list --commit "$(git rev-parse HEAD)" --limit 1
```

---

### Task 5: Protected Manual-Send API and Production Composition

**Files:**
- Modify: `apps/api/runtime_config.py`
- Modify: `apps/api/dependencies.py`
- Modify: `apps/api/composition/runtime.py`
- Modify: `apps/api/main.py`
- Modify: `apps/api/routers/campaigns.py`
- Modify: `infra/.env.example`
- Create: `tests/unit/test_campaign_send_router.py`
- Modify: `tests/unit/test_api_app.py`
- Modify: `tests/unit/test_api_runtime.py`
- Create: `tests/integration/test_manual_email_send_api.py`
- Modify: `tests/integration/test_api_runtime.py`

**Interfaces:**
- Consumes: `OutreachService`, `SendingIdentityService`, Task 3 `ToolGateway`, Task 4 provider/handler/connector contracts.
- Produces: `POST /crm/message-attempts/{attempt_id}/send`, strict runtime configuration and complete fail-closed dependency composition.

- [ ] **Step 1: Add HTTP contract RED tests**

```python
class ManualEmailSendBody(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    subject: str
    body: str


@router.post(
    "/message-attempts/{attempt_id}/send",
    response_model=ManualEmailSendResponse,
    responses={
        400: {"model": ApiErrorResponse},
        403: {"model": ApiErrorResponse},
        409: {"model": ApiErrorResponse},
        429: {"model": ApiErrorResponse},
        503: {"model": ApiErrorResponse},
    },
)
```

Assert body rejects extra tenant/address/identity/tool/idempotency/approval/provider fields, unknown attempt, tenant mismatch, inactive/stale/suppressed attempt, forbidden content, duplicate/conflict/in-progress/rate-limit/auth/permanent/transient/reconciliation mappings, and fixed safe Chinese messages. HTTP errors never echo body or addresses.

- [ ] **Step 2: Run API RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_campaign_send_router.py tests/unit/test_api_app.py -q -W error
```

Expected: route/dependency/schema symbols missing; existing CRM routes remain green.

- [ ] **Step 3: Define exact runtime settings and fail-closed parsing**

Add required env names:

```text
GMAIL_OAUTH_TOKEN_REF
TOOL_CALL_FINGERPRINT_KEY_REF
TOOL_CALL_FINGERPRINT_KEY_VERSION
TRADEOS_UNSUBSCRIBE_BASE_URL
TRADEOS_TOOL_LEASE_SECONDS
```

Only refs/version/base URL/positive lease enter `Phase1RuntimeSettings`; secret values are resolved by an injected production `SecretResolver`. Reject missing, blank, boundary whitespace, control characters, non-HTTPS unsubscribe base, embedded credentials/query/fragment, and nonpositive lease. Error messages reveal only the config name.

- [ ] **Step 4: Extend configured dependency container**

Add exact fields:

```python
outreach: OutreachService
sending_identities: SendingIdentityService
tool_gateway: ToolGateway
delivery_materials: DeliveryMaterialProvider
unsubscribe_links: UnsubscribeLinkProvider
```

`ConfiguredApiDependencies.__post_init__` rejects missing/non-runtime-checkable providers. `UnconfiguredApiDependencies` remains unchanged; zero-arg `create_app()` and OpenAPI export do not read env, create engines, resolve secrets, or register a partial Gmail handler.

- [ ] **Step 5: Implement protected route and safe result mapping**

The route derives `MessageAttemptId`, tenant and Outreach actor from `RequestIdentity`, calls only `dependencies.tool_gateway.invoke`, and maps:

```text
succeeded/duplicate → 200
validation/approval_required → 400
permission/suppressed → 403
idempotency_conflict/in_progress/reconciliation_required → 409
rate_limited → 429 + bounded Retry-After
provider transient/auth/runtime unavailable → 503 + bounded Retry-After
provider permanent → 400 fixed request_rejected
```

The response contains only `tool_call_id`, `status`, `duplicate`, `provider_ref`, `error_category`, and optional integer `retry_after_seconds`.

- [ ] **Step 6: Implement production composition without hidden defaults**

Create request-scoped Outreach/Sending Identity services using the existing SQLAlchemy UoWs and Phase 1 authorizers; do not keep a session-bound singleton. Register exact `email.send` manifest/handler/checks in `ToolRegistry`; create Tool Gateway with ledger UoW. Inject concrete delivery/unsubscribe providers supplied by application composition; if the repository still lacks a real contact-address source, runtime construction must fail with the fixed configured-dependency error instead of substituting env/test data.

The implementation may expose a narrow `build_phase1_dependencies(..., delivery_materials, unsubscribe_links, secret_resolver)` signature so deployment/E2E can inject real providers. `create_runtime_app()` must require explicit production provider factories from its approved composition source; it must not monkeypatch or import test fakes.

- [ ] **Step 7: Add true PostgreSQL + ASGI integration tests**

Seed current business facts through public services/repositories, call the real FastAPI route with real Gateway/UoWs and controlled Gmail transport. Prove:

- success writes one Attempt `SENT`, one canonical tool call, append-only stage events and one reservation;
- 20 concurrent HTTP sends produce one Gmail send and duplicates/in-progress for the rest;
- suppression-before-claim yields zero slot/Gmail;
- claim-before-suppression has one honest sent/reconciliation outcome and future enrollment stopped;
- forbidden promise yields zero claim/slot/Gmail;
- Gmail success + local commit failure yields `reconciliation_required`, Attempt `SENDING`, no automatic resend;
- tenant B cannot see/reuse tenant A attempt/key/tool call;
- DB/log/HTTP payload scans contain none of the raw recipient, subject, body, token, URL or DSN markers.

- [ ] **Step 8: Verify zero-arg OpenAPI and configured runtime**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_campaign_send_router.py tests/unit/test_api_app.py \
       tests/unit/test_api_runtime.py tests/integration/test_manual_email_send_api.py \
       tests/integration/test_api_runtime.py -q -W error
env -i PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/usr/bin:/bin" \
  python apps/web/scripts/export_openapi.py >/tmp/tradeos-slice4b2-openapi.json
```

Assert cold export makes zero socket/engine/secret calls; configured runtime checks exact Alembic head and disposes engine on startup/body/shutdown failures.

- [ ] **Step 9: Run API/runtime structural gates**

```bash
ruff check apps/api infra/.env.example tests/unit/test_campaign_send_router.py \
  tests/unit/test_api_app.py tests/unit/test_api_runtime.py \
  tests/integration/test_manual_email_send_api.py tests/integration/test_api_runtime.py
mypy apps
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
```

- [ ] **Step 10: Commit, push, and verify exact CI**

Stage only Task 5 files, use `git add --chmod=-x` for the two new tests, then:

```bash
git commit -m "feat(api): expose protected gmail manual send"
git push origin codex/phase1-implementation
gh run list --commit "$(git rev-parse HEAD)" --limit 1
```

---

### Task 6: Recovery Proof, Offline Demo, Architecture Sync, and Final Gates

**Files:**
- Create: `scripts/demo_gmail_manual_send.py`
- Create: `tests/integration/test_demo_gmail_manual_send.py`
- Create: `tests/integration/test_tool_gateway_email_recovery.py`
- Modify: `tool_gateway/AGENTS.md`
- Modify: `connectors/gmail/AGENTS.md`
- Modify: `domains/outreach/AGENTS.md`
- Modify: `docs/architecture/04-tool-gateway.md`

**Interfaces:**
- Consumes: complete Task 1–5 runtime components.
- Produces: reproducible controlled-transport demo, recovery runbook and final verified Slice 4B2 state.

- [ ] **Step 1: Add subprocess demo RED tests**

The script accepts only `DATABASE_URL` plus explicit safe demo mode, creates a random tenant, uses real Outreach/Sending Identity/Tool Gateway services and a local controlled Gmail transport, and prints one safe JSON line. It never resolves a production OAuth secret or makes an external network connection.

```python
async def test_demo_proves_success_duplicate_and_reconciliation(db_url: str) -> None:
    result = await _run_demo(db_url)
    summary = json.loads(result.stdout)
    assert summary["gmail_send_count"] == 1
    assert summary["first_status"] == "succeeded"
    assert summary["duplicate_status"] == "duplicate"
    assert summary["ambiguous_status"] == "reconciliation_required"
```

The DB readback—not stdout alone—must prove canonical call states, append-only events, Attempt transitions, single reservation, cross-tenant isolation and no content columns. Run twice in the same migrated DB and prove unique tenants/IDs with no destructive cleanup.

- [ ] **Step 2: Run genuine file-missing RED**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/integration/test_demo_gmail_manual_send.py -q -W error
```

Expected: real Testcontainers/Alembic setup succeeds; subprocess fails only because `scripts/demo_gmail_manual_send.py` does not exist.

- [ ] **Step 3: Implement controlled demo and fixed failure boundary**

`main() -> int` catches ordinary exceptions and emits only `Gmail 手动发送演示运行失败` to stderr with exit 1. It always disposes engine, closes sessions, shuts down the local transport and removes temporary artifacts. Success stdout contains only safe IDs, typed statuses and integer counts; no domain, address, subject, body, URL, token or DSN.

- [ ] **Step 4: Add recovery mutation tests**

`tests/integration/test_tool_gateway_email_recovery.py` must inject boundaries independently:

- process crash after claim but before EXECUTING → expired lease may safely rerun checks;
- EXECUTING commit success then process crash → restart returns reconciliation-required and calls Gmail zero times;
- Gmail search finds deterministic message → complete sent without resend;
- Gmail search misses after ambiguous result → remains reconciliation-required;
- definitely-not-sent transport failure → rerun current facts, suppression can stop retry;
- provider ref mismatch → permanent conflict, no state overwrite;
- append-event/Attempt completion/canonical completion commit failures preserve honest state and no raw data.

- [ ] **Step 5: Update binding rules and architecture document**

Document the exact state machines, six-stage order, two migrations, safe-column policy, Outreach lock order, HMAC key rotation, Gmail deterministic headers, ambiguity classification, manual reconciliation decision tree and metrics/alerts. Include this operational rule verbatim in meaning:

```text
一次 Gmail 搜索未命中不等于邮件确定未发送；只要 Connector 可能已开始，系统不得自动重发。
```

Do not document auto-scanner, UI, reply Worker or accepted approval behavior as implemented.

- [ ] **Step 6: Run full targeted Slice 4B2 suite**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/unit/test_tool_gateway_contracts.py \
       tests/unit/test_tool_gateway_manifest.py \
       tests/unit/test_tool_gateway_fingerprint.py \
       tests/unit/test_quotation_commitment_guard.py \
       tests/unit/test_tool_gateway_uow.py \
       tests/unit/test_outreach_send_claim.py \
       tests/unit/test_tool_gateway_pipeline.py \
       tests/unit/test_gmail_connector.py \
       tests/unit/test_email_send_handler.py \
       tests/unit/test_campaign_send_router.py \
       tests/integration/test_tool_call_repository.py \
       tests/integration/test_outreach_send_claim.py \
       tests/integration/test_tool_gateway_pipeline.py \
       tests/integration/test_gmail_connector_http.py \
       tests/integration/test_manual_email_send_api.py \
       tests/integration/test_tool_gateway_email_recovery.py \
       tests/integration/test_demo_gmail_manual_send.py -q -W error
```

- [ ] **Step 7: Run repository-wide verification**

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH make check
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
pytest tests/integration -q -W error
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
```

If OpenAPI changed, regenerate frontend types twice and require byte-identical output, then run `npm run typecheck`, `npm run lint`, `npm run test`, and `npm run build` from `apps/web`; remove `node_modules` only if it was created by this task and remove generated `dist`/coverage/AppleDouble artifacts before staging.

- [ ] **Step 8: Perform final security/mutation self-review**

Search the complete range for forbidden payload keys and raw markers. Verify every table/query is tenant-scoped; every connector call is preceded by committed EXECUTING; every failure path has zero later calls; every allow audit occurs only after durable success; every ambiguous path has zero automatic resends; all new migration guards have real PG mutation tests.

```bash
rg -n -i 'recipient|from_address|to_address|subject|body|authorization|oauth|token|secret|dsn|unsubscribe_url' \
  migrations infra/db tool_gateway domains/outreach connectors/gmail apps/api
git diff --check
```

Classify each match; raw payload persistence/logging is a blocking failure, while private `repr=False` in-memory fields and secret-ref names are allowed.

- [ ] **Step 9: Commit documentation/demo, push, and wait for exact CI**

```bash
git add --chmod=-x scripts/demo_gmail_manual_send.py \
  tests/integration/test_demo_gmail_manual_send.py \
  tests/integration/test_tool_gateway_email_recovery.py \
  tool_gateway/AGENTS.md connectors/gmail/AGENTS.md domains/outreach/AGENTS.md \
  docs/architecture/04-tool-gateway.md
git diff --cached --check
python3 scripts/scan_sensitive.py --staged
git commit -m "docs(tool-gateway): verify gmail recovery workflow"
git push origin codex/phase1-implementation
gh run list --commit "$(git rev-parse HEAD)" --limit 1
```

The task is complete only when the exact final SHA has a successful remote CI run and the worktree/index are clean.

---

## Plan Self-Review Checklist

- [ ] Every approved design section maps to at least one task: boundaries (Tasks 1/3/5), contracts (1/3/4), persistence (2/3), flow and races (3/5/6), Gmail ambiguity (4/6), API/runtime (5), recovery/docs (6).
- [ ] No task stores raw recipient/sender/subject/body/URL/token/header/DSN; tests actively scan DB/log/HTTP output.
- [ ] `PreparedToolCall`, `ClaimResult`, `MessageSendPreflight`, Gmail request/result and API response names/signatures are identical wherever consumed.
- [ ] Migration ordering is fixed at current verified head `0009`: Task 2 creates `0010`; Task 3 creates `0011`; both include downgrade/upgrade roundtrip and ORM parity.
- [ ] Every external-side-effect path has a genuine RED, a minimal GREEN, real PostgreSQL concurrency coverage and a committed pre-send ledger transition.
- [ ] Every task ends in a normal commit, push and exact-SHA CI verification; no amend, hidden stage, broad cleanup or destructive reset.
