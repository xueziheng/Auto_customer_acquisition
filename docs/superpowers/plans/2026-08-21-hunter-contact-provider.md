# Hunter Contact Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不泄漏凭证、PII 或 Provider 概率分数的前提下，为 TradeOS Phase 1 提供 Hunter 单 Provider 的联系人补全与邮箱验证能力，并把所有结果安全接入 Prospecting 与 Tool Gateway 边界。

**Architecture:** `connectors/hunter` 负责固定 host 的 Hunter API v2 协议转换，`contact_enrichment` 与 `email_verification` 只保留 provider-neutral typed 契约。Tool Gateway 使用 tenant-bound preflight、抑制/政策/配额检查和 async-task-local 一次性 slot，让含 PII 的 typed 结果只在受信调用栈中传递；durable ledger 只保存安全 handle 与成本等级。Prospecting 持久化所有验证结果的检查时间与固定成本备注，以实现完整的 30 天缓存。

**Tech Stack:** Python 3.12、dataclasses/Protocol、asyncio/ContextVar、urllib 标准库、SQLAlchemy 2.x、Alembic、PostgreSQL 16、pytest/pytest-asyncio、ruff、mypy、GitHub Actions。

**Spec:** `docs/superpowers/specs/2026-08-20-hunter-contact-provider-design.md`

## Global Constraints

- 动手前完整阅读根目录及目标目录就近 `AGENTS.md`；冲突时根规则优先。
- 所有 Python 命令使用 `/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/`。
- 本地集成测试只使用 `TEST_DATABASE_URL` 指向的专用 PostgreSQL 数据库；不得启动 Docker。
- API Key 只由 connector 内部通过 `HUNTER_API_KEY_REF` 解析并放入 `X-API-KEY`；不得进入 URL、参数、返回值、repr、异常、日志、数据库或模型上下文。
- Provider response 中的 `confidence`、`score` 与 `decision_maker` 不得进入公共 DTO、数据库、outbox 或审计。
- 邮箱、姓名、职位、source URI 和 raw JSON 只允许存在于 repr-disabled typed DTO、prepared payload 与 async-task-local slot。
- 金额不使用 `float`；本切片只记录固定 cost label，不硬编码 Hunter 当前套餐价格。
- 新工具只能增加 manifest、handler 与 composition/check strategy；禁止按 tool id 修改 `tool_gateway/pipeline.py`。
- 每个任务严格执行 RED → GREEN；RED 必须是预期缺失行为导致的失败，不能跳过。
- 每个任务完成后运行目标测试、ruff、mypy 与 `python scripts/check_boundaries.py`，删除 AppleDouble 文件，独立 commit/push，并等待该精确 commit SHA 的 GitHub `ci` workflow 成功后才能进入下一任务。
- 每次 CI 关门使用以下命令；`task_sha` 与 `run_id` 是当前任务局部变量，不复用系统环境变量：

```bash
find . -name '._*' -not -path './.git/*' -delete
git status --short
git push origin HEAD
task_sha=$(git rev-parse HEAD)
run_id=""
while [ -z "$run_id" ]; do
  run_id=$(gh run list --commit "$task_sha" --workflow ci --limit 1 --json databaseId --jq '.[0].databaseId')
  if [ -z "$run_id" ]; then sleep 5; fi
done
gh run watch "$run_id" --exit-status --interval 10
gh run view "$run_id" --json headSha,status,conclusion,url
test "$(gh run view "$run_id" --json headSha --jq .headSha)" = "$task_sha"
test "$(gh run view "$run_id" --json conclusion --jq .conclusion)" = "success"
```

---

## File Map

### Prospecting cache/persistence

- Modify `domains/prospecting/models.py`: 验证观察字段与状态不变量。
- Modify `domains/prospecting/schemas.py`: `VerificationRecordRequest` 与公开 ContactPoint View。
- Modify `domains/prospecting/service.py`: typed 记录/读取接口。
- Modify `domains/prospecting/repository.py`: 继续使用 tenant-bound point 读写契约。
- Modify `domains/prospecting/service_impl.py`: 新鲜度、并发、事件幂等和公开读取。
- Modify `infra/db/tables.py`: ORM 列与 CHECK。
- Modify `infra/db/repositories/prospecting.py`: 新字段读写。
- Create `migrations/versions/0024_contact_verification_observations.py`: 可逆 schema 迁移。

### Connector contracts and Hunter implementation

- Modify `connectors/contact_enrichment/client.py`: typed DTO/Protocol，不含 Provider 实现。
- Modify `connectors/email_verification/client.py`: typed DTO/Protocol，不含 raw dict。
- Modify both connector `AGENTS.md`: 单 Hunter key、PII/score/caching/cost 边界。
- Create `connectors/hunter/AGENTS.md`: Provider-specific constraints。
- Create `connectors/hunter/__init__.py`: 稳定公开出口。
- Create `connectors/hunter/transport.py`: 固定 URL、header auth、bounded JSON 与 safe errors。
- Create `connectors/hunter/client.py`: configure/health、Domain Search、Verifier 与状态映射。

### Tool Gateway plugin

- Create `tool_gateway/checks/contact_provider.py`: resource shape、policy preflight、country、suppression 与 provider quota strategies。
- Create `tool_gateway/handlers/single_result_slot.py`: ContextVar task-local single-result slot。
- Create `tool_gateway/handlers/contact_provider_errors.py`: connector error 到固定 Gateway category 的唯一映射。
- Create `tool_gateway/handlers/contact_enrichment.py`: manifest、handler、trusted adapter。
- Create `tool_gateway/handlers/contact_verification.py`: cache-aware manifest、handler、trusted adapter。
- Modify `tool_gateway/handlers/__init__.py`: 公开稳定插件类型。
- Modify `tool_gateway/AGENTS.md` and `docs/architecture/04-tool-gateway.md`: ledger/PII/retry facts。
- Modify `HANDBOOK.md`: 如实更新已完成能力和仍未启用的政策包/composition。

---

### Task 1: Prospecting verification observation contracts

**Files:**
- Modify: `domains/prospecting/models.py`
- Modify: `domains/prospecting/schemas.py`
- Modify: `tests/unit/test_prospecting_models.py`
- Modify: `tests/unit/test_prospecting_contracts.py`

**Interfaces:**
- Consumes: existing `VerificationStatus`, `ContactPointId`, `ContactPointView`.
- Produces the request/View data contracts consumed by Task 2:

```python
@dataclass(frozen=True)
class VerificationRecordRequest:
    contact_point_id: ContactPointId
    result: VerificationStatus
    provider: str
    checked_at: datetime
    cost_note: str

verification_checked_at: datetime | None = None
verification_cost_note: str | None = None
```

- [ ] **Step 1: Write failing model and contract tests**

增加以下精确测试：

```python
def test_contact_point_accepts_complete_unverified_observation() -> None:
    point = make_point(
        verification=VerificationStatus.UNVERIFIED,
        verification_provider="hunter",
        verification_checked_at=CHECKED_AT,
        verification_cost_note="hunter.email_verifier.unknown",
    )
    assert point.verification_checked_at == CHECKED_AT

@pytest.mark.parametrize(
    ("provider", "checked_at", "cost_note"),
    [
        ("hunter", None, "hunter.email_verifier.unknown"),
        (None, CHECKED_AT, "hunter.email_verifier.unknown"),
        ("hunter", CHECKED_AT, None),
    ],
)
def test_contact_point_rejects_partial_observation(
    base_point: ContactPoint,
    provider: str | None,
    checked_at: datetime | None,
    cost_note: str | None,
) -> None:
    with pytest.raises(ValidationError, match="联系方式验证观察无效"):
        replace(
            base_point,
            verification_provider=provider,
            verification_checked_at=checked_at,
            verification_cost_note=cost_note,
        )

def test_verification_record_request_is_frozen() -> None:
    request = VerificationRecordRequest(
        ContactPointId("cp_01J00000000000000000000000"),
        VerificationStatus.VERIFIED,
        "hunter",
        CHECKED_AT,
        "hunter.email_verifier.counted",
    )
    with pytest.raises(FrozenInstanceError):
        request.provider = "changed"  # type: ignore[misc]
```

同时锁定：legacy `invalid/risky` 允许 `provider != None` 且 checked/cost 都为 `None`；
`checked_at` 必须 UTC；`verified_at` 仍只允许 `VERIFIED`。

- [ ] **Step 2: Run RED**

```bash
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_prospecting_models.py \
  tests/unit/test_prospecting_contracts.py
```

Expected: FAIL because `verification_checked_at`, `verification_cost_note`, and
`VerificationRecordRequest` do not exist.

- [ ] **Step 3: Implement the contracts and invariants**

模型形状必须精确实现：

```python
observed = self.verification_checked_at is not None
complete_observation = (
    self.verification_provider is not None
    and self.verification_cost_note is not None
)
if observed != complete_observation:
    raise ValidationError("联系方式验证观察无效")
if self.verification_checked_at is not None:
    _require_utc(self.verification_checked_at, "联系方式验证观察无效")
```

状态 shape 同时改为：initial `unverified` 只允许 provider/checked/cost 全空；observed
`unverified` 必须三者齐全；`verified` 仍要求 UTC `verified_at`；`invalid/risky` 要求 provider，
并允许 checked/cost 都为空的 legacy 行。legacy 行不参与缓存。
`schemas.__all__` 必须导出 `VerificationRecordRequest`。

- [ ] **Step 4: Run GREEN and static gates**

```bash
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_prospecting_models.py \
  tests/unit/test_prospecting_contracts.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/ruff check \
  domains/prospecting tests/unit/test_prospecting_models.py \
  tests/unit/test_prospecting_contracts.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/mypy \
  domains/prospecting tests/unit/test_prospecting_models.py \
  tests/unit/test_prospecting_contracts.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python scripts/check_boundaries.py
```

Expected: all pass.

- [ ] **Step 5: Commit, push, and wait for exact HEAD CI**

```bash
git add domains/prospecting/models.py domains/prospecting/schemas.py \
  tests/unit/test_prospecting_models.py \
  tests/unit/test_prospecting_contracts.py
git commit -m "feat: define contact verification observations"
```

执行 Global Constraints 的 CI 关门命令，确认 exact HEAD success 后再继续。

---

### Task 2: Persist verification observations and enforce cache-safe service semantics

**Files:**
- Create: `migrations/versions/0024_contact_verification_observations.py`
- Modify: `domains/prospecting/service.py`
- Modify: `infra/db/tables.py`
- Modify: `infra/db/repositories/prospecting.py`
- Modify: `domains/prospecting/service_impl.py`
- Modify: `tests/unit/test_prospecting_service.py`
- Modify: `tests/unit/test_prospecting_contracts.py`
- Modify: `tests/integration/test_prospecting_repositories.py`
- Modify: `tests/integration/test_migrations.py`

**Interfaces:**
- Consumes: Task 1 `VerificationRecordRequest` and ContactPoint fields.
- Produces:

```python
async def ProspectingService.record_verification(
    tenant_id: TenantId,
    request: VerificationRecordRequest,
) -> None:
    raise NotImplementedError

async def ProspectingService.get_contact_point(
    tenant_id: TenantId,
    contact_point_id: ContactPointId,
) -> ContactPointView:
    raise NotImplementedError
```

and durable `verification_checked_at`/`verification_cost_note`, tenant-bound point reads, plus
stale/conflict-safe state updates.

- [ ] **Step 1: Write failing migration/repository/service tests**

Add tests that prove:

```python
async def test_record_unknown_persists_complete_observation(service):
    await service.record_verification(
        TENANT,
        VerificationRecordRequest(
            point_id,
            VerificationStatus.UNVERIFIED,
            "hunter",
            CHECKED_AT,
            "hunter.email_verifier.unknown",
        ),
    )
    view = await service.get_contact_point(TENANT, point_id)
    assert (
        view.verification,
        view.verification_provider,
        view.verification_checked_at,
        view.verification_cost_note,
    ) == (
        VerificationStatus.UNVERIFIED,
        "hunter",
        CHECKED_AT,
        "hunter.email_verifier.unknown",
    )

```

The same test module must also contain these named cases with exact outcomes:

- `test_older_verification_result_cannot_overwrite_newer`: raises
  `InvalidStateTransition`; row and outbox remain unchanged.
- `test_same_timestamp_different_result_is_conflict`: raises
  `ProspectingConflictError`; row and outbox remain unchanged.
- `test_same_complete_request_is_idempotent`: returns normally; update and publish call counts are 0.
- `test_reverification_of_verified_point_updates_checked_at_without_second_event`: checked time/cost
  update, first `verified_at` remains, outbox count remains 1.
- `test_cross_tenant_get_contact_point_is_not_found`: raises the same
  `ContactPointNotFoundError("潜在联系方式不存在")` as an unknown ID.

Migration parity must assert both new columns, nullable shape, the new
`ck_contact_points_verification_observation` constraint, removal of
`ck_contact_points_provider_state`, and `upgrade → downgrade → upgrade`.
`test_prospecting_contracts.py` must inspect both new public service signatures and assert callers
cannot pass the legacy four positional verification arguments.

- [ ] **Step 2: Run RED against the dedicated local PostgreSQL database**

```bash
TEST_DATABASE_URL="$TEST_DATABASE_URL" \
  /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_prospecting_service.py \
  tests/unit/test_prospecting_contracts.py \
  tests/integration/test_prospecting_repositories.py \
  tests/integration/test_migrations.py
```

Expected: FAIL on missing migration columns/service behavior; Docker call count remains zero because `TEST_DATABASE_URL` is set.

- [ ] **Step 3: Implement migration and ORM/repository parity**

Migration `0024` must:

```python
op.add_column(
    "contact_points",
    sa.Column("verification_checked_at", sa.DateTime(timezone=True), nullable=True),
)
op.add_column(
    "contact_points",
    sa.Column("verification_cost_note", sa.String(200), nullable=True),
)
op.drop_constraint("ck_contact_points_provider_state", "contact_points", type_="check")
op.create_check_constraint(
    "ck_contact_points_verification_observation",
    "contact_points",
    "(verification_checked_at IS NULL AND verification_cost_note IS NULL AND "
    "((verification_status = 'unverified' AND verification_provider IS NULL) OR "
    "(verification_status <> 'unverified' AND verification_provider IS NOT NULL))) OR "
    "(verification_checked_at IS NOT NULL AND verification_provider IS NOT NULL "
    "AND verification_cost_note IS NOT NULL)",
)
```

Downgrade first nulls `verification_provider` for `unverified`, drops the new constraint/columns, then restores the exact 0023 provider-state CHECK。禁止用 `created_at` 回填 checked time，因为那会伪造验证时间。

- [ ] **Step 4: Implement service ordering and event semantics**

Within the existing row lock:

```python
if current.verification_checked_at is not None:
    if request.checked_at < current.verification_checked_at:
        raise InvalidStateTransition("验证结果早于当前观察")
    if request.checked_at == current.verification_checked_at:
        if same_observation(current, request):
            return
        raise ProspectingConflictError("验证观察时间冲突")

transitioned_to_verified = (
    current.verification is not VerificationStatus.VERIFIED
    and request.result is VerificationStatus.VERIFIED
)
```

进入 `VERIFIED` 时 `verified_at=request.checked_at`；已 verified 再次 valid 时保留首次
`verified_at`；离开 verified 时清空。只有 `transitioned_to_verified` 发布一个 metadata-only
`ContactPointVerified`。`get_contact_point` 在一个 tenant-bound UoW 内读取 point 和 contact，
用真实 `contact.account_id` 构造 View；跨租户与不存在同样报固定 NotFound。

- [ ] **Step 5: Run GREEN and regression gates**

```bash
DATABASE_URL="$TEST_DATABASE_URL" \
  /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/alembic upgrade head
TEST_DATABASE_URL="$TEST_DATABASE_URL" \
  /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_prospecting_service.py \
  tests/unit/test_prospecting_contracts.py \
  tests/integration/test_prospecting_repositories.py \
  tests/integration/test_migrations.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/ruff check \
  migrations/versions/0024_contact_verification_observations.py \
  domains/prospecting infra/db tests/unit/test_prospecting_service.py \
  tests/unit/test_prospecting_contracts.py \
  tests/integration/test_prospecting_repositories.py tests/integration/test_migrations.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/mypy \
  domains/prospecting infra/db/repositories/prospecting.py infra/db/tables.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python scripts/check_boundaries.py
```

Expected: all pass, including concurrent event idempotence and migration round-trip.

- [ ] **Step 6: Commit, push, and exact CI**

```bash
git add migrations/versions/0024_contact_verification_observations.py \
  domains/prospecting/service.py \
  infra/db/tables.py infra/db/repositories/prospecting.py \
  domains/prospecting/service_impl.py tests/unit/test_prospecting_service.py \
  tests/unit/test_prospecting_contracts.py \
  tests/integration/test_prospecting_repositories.py tests/integration/test_migrations.py
git commit -m "feat: persist contact verification observations"
```

执行 Global Constraints 的 CI 关门命令。

---

### Task 3: Provider-neutral contact connector contracts

**Files:**
- Modify: `connectors/contact_enrichment/client.py`
- Modify: `connectors/email_verification/client.py`
- Modify: `connectors/contact_enrichment/AGENTS.md`
- Modify: `connectors/email_verification/AGENTS.md`
- Create: `tests/unit/test_contact_provider_contracts.py`

**Interfaces:**
- Produces:

```python
class ContactEmailKind(str, Enum):
    PERSONAL = "personal"
    GENERIC = "generic"

@dataclass(frozen=True, repr=False)
class ContactSource:
    uri: str = field(repr=False)
    first_seen_on: date
    last_seen_on: date
    still_on_page: bool

@dataclass(frozen=True, repr=False)
class ContactCandidate:
    email: str = field(repr=False)
    full_name: str | None = field(default=None, repr=False)
    role_title: str | None = field(default=None, repr=False)
    email_kind: ContactEmailKind = ContactEmailKind.PERSONAL
    sources: tuple[ContactSource, ...] = field(default=(), repr=False)

class EnrichmentCostNote(str, Enum):
    COUNTED = "hunter.domain_search.counted"
    NO_RESULT = "hunter.domain_search.no_result"

@dataclass(frozen=True, repr=False)
class ContactEnrichmentResult:
    candidates: tuple[ContactCandidate, ...] = field(repr=False)
    provider: str
    cost_note: EnrichmentCostNote

class EmailVerificationOutcome(str, Enum):
    VERIFIED = "verified"
    INVALID = "invalid"
    RISKY = "risky"
    UNVERIFIED = "unverified"

class VerificationCostNote(str, Enum):
    COUNTED = "hunter.email_verifier.counted"
    PRIVACY_REFUSED = "hunter.email_verifier.privacy_refused"
    UNKNOWN = "hunter.email_verifier.unknown"
    CACHE_HIT = "hunter.email_verifier.cache_hit"

@dataclass(frozen=True, repr=False)
class EmailVerificationResult:
    outcome: EmailVerificationOutcome
    provider: str
    checked_at: datetime
    cost_note: VerificationCostNote
    privacy_claimed: bool = False
```

The two runtime-checkable Protocols expose
`find_contacts(company_domain: str, role_hints: tuple[str, ...]) -> ContactEnrichmentResult` and
`verify(email: str) -> EmailVerificationResult`; neither exposes `provider_raw` or dict.

- [ ] **Step 1: Write RED contract, immutability, validation, and repr tests**

Tests must inspect the complete object graph and assert canary email/source values do not appear in `repr`; mutate input lists after construction and prove tuples are unchanged; reject non-UTC checked times, source date reversal, no-source candidate, invalid email kind, blank provider, and any unexpected dict result.

- [ ] **Step 2: Run RED**

```bash
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_contact_provider_contracts.py
```

Expected: import/signature failures from the current raw-dict skeletons.

- [ ] **Step 3: Implement minimal typed contracts and update AGENTS**

Use frozen dataclasses, defensive tuple copies, fixed safe messages, UTC validation, and no Provider manifest in the generic contract modules. Both AGENTS files name exactly `HUNTER_API_KEY_REF`; verification docs state all four outcomes cache for 30 days and risky/unverified remain unusable.

- [ ] **Step 4: Run GREEN and gates**

```bash
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_contact_provider_contracts.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/ruff check \
  connectors/contact_enrichment connectors/email_verification \
  tests/unit/test_contact_provider_contracts.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/mypy \
  connectors/contact_enrichment connectors/email_verification
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python scripts/check_boundaries.py
```

- [ ] **Step 5: Commit, push, and exact CI**

```bash
git add connectors/contact_enrichment connectors/email_verification \
  tests/unit/test_contact_provider_contracts.py
git commit -m "feat: type contact provider contracts"
```

执行 Global Constraints 的 CI 关门命令。

---

### Task 4: Safe Hunter HTTP transport

**Files:**
- Create: `connectors/hunter/AGENTS.md`
- Create: `connectors/hunter/__init__.py`
- Create: `connectors/hunter/transport.py`
- Create: `tests/unit/test_hunter_transport.py`

**Interfaces:**

```python
class HunterErrorCode(str, Enum):
    CLAIMED_EMAIL = "claimed_email"
    OTHER = "other"

@dataclass(frozen=True)
class HunterHttpResponse:
    status_code: int
    payload: Mapping[str, object] = field(repr=False)
    retry_after_seconds: int | None = None

@runtime_checkable
class HunterHttpTransport(Protocol):
    async def get(
        self,
        path: str,
        params: tuple[tuple[str, str], ...],
        *,
        api_key: str,
    ) -> HunterHttpResponse:
        raise NotImplementedError

class HunterHttpStatusError(Exception):
    status_code: int
    error_code: HunterErrorCode
    retry_after_seconds: int | None

class HunterNetworkError(Exception):
    may_have_reached_provider: bool
```

- [ ] **Step 1: Write RED transport tests with an injected opener**

Prove fixed `https://api.hunter.io/v2`, relative path allowlist
`/account`, `/domain-search`, `/email-verifier`, `X-API-KEY` header, no key in URL,
GET only, sorted/encoded query, no redirects, timeout `<=30`, body cap `524288`, JSON-object
only, bounded Retry-After, typed `claimed_email`, and fixed repr/errors.

Security canaries must scan `repr(request)`, raised exception, captured logs, and returned error
object for API key, email, Authorization text, response body, and URL query.

- [ ] **Step 2: Run RED**

```bash
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_hunter_transport.py
```

Expected: missing module/import failures.

- [ ] **Step 3: Implement transport using urllib + asyncio.to_thread**

The production constructor accepts only timeout and an optional injected opener; base host is a
module constant, not a caller argument. `_request` constructs
`Request(url, data=None, headers={"X-API-KEY": api_key}, method="GET")`; `HTTPError` parsing
extracts only bounded status, Retry-After, and the allowlisted error id, closes the error stream,
and discards raw content. Any ambiguous `URLError/TimeoutError/OSError` sets
`may_have_reached_provider=True`.

- [ ] **Step 4: Run GREEN and gates**

```bash
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_hunter_transport.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/ruff check \
  connectors/hunter tests/unit/test_hunter_transport.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/mypy connectors/hunter
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python scripts/check_boundaries.py
```

- [ ] **Step 5: Commit, push, and exact CI**

```bash
git add connectors/hunter tests/unit/test_hunter_transport.py
git commit -m "feat: add safe hunter http transport"
```

执行 Global Constraints 的 CI 关门命令。

---

### Task 5: Hunter Domain Search adapter

**Files:**
- Create: `connectors/hunter/client.py`
- Modify: `connectors/hunter/__init__.py`
- Create: `tests/unit/test_hunter_contact_enrichment.py`

**Interfaces:**

```python
MANIFEST = ConnectorManifest(
    connector_id="hunter",
    capabilities=("contact.enrich",),
    secret_refs=("HUNTER_API_KEY_REF",),
    rate_limit_note="Domain Search 15/s 500/min",
    compliance_note="PII 仅 typed 临时交接；Provider score 不进入业务数据",
)

class HunterConnector:
    async def configure(self, secret_resolver: HunterSecretResolver) -> None:
        raise NotImplementedError

    async def health_check(self) -> bool:
        raise NotImplementedError

    async def find_contacts(
        self, company_domain: str, role_hints: tuple[str, ...]
    ) -> ContactEnrichmentResult:
        raise NotImplementedError
```

- [ ] **Step 1: Write RED Domain Search tests**

Cover configure-before-use, resolver called exactly with `HUNTER_API_KEY_REF`, `/account` health,
canonical IDNA domain, fixed `limit=10/offset=0`, no pagination, empty result, linked-domain
allowlist, sorted unique canonical role hints, role match against position/department/seniority,
personal-before-generic, canonical-email
sort, max five, max twenty sources, invalid/missing sources dropped, and malformed top-level response
fixed failure.

Add a mutation test using response fields `confidence=92`, `score=100`, and
`decision_maker=True`; recursively inspect the result and assert none of those keys/values are
retained. Logs/repr/errors must not contain the API key or email canary.

- [ ] **Step 2: Run RED**

```bash
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_hunter_contact_enrichment.py
```

Expected: missing `HunterConnector` behavior.

- [ ] **Step 3: Implement deterministic parsing/filtering**

Parse only allowlisted fields. Build candidates only after canonical email-domain and source
validation. Role tokens use Unicode casefold + whitespace normalization but never fuzzy/model
scores. `meta.results > 0` selects `EnrichmentCostNote.COUNTED`; zero selects `NO_RESULT`.
Map 401 to auth-required connector error, 403/429 to bounded rate-limited error, 451 to permanent
policy refusal, 400/404/422/schema error to permanent, 5xx to transient, and ambiguous network
failure to an uncertainty error that the handler can map to reconciliation-required.

- [ ] **Step 4: Run GREEN and gates**

```bash
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_hunter_contact_enrichment.py \
  tests/unit/test_hunter_transport.py \
  tests/unit/test_contact_provider_contracts.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/ruff check \
  connectors/hunter tests/unit/test_hunter_contact_enrichment.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/mypy connectors/hunter
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python scripts/check_boundaries.py
```

- [ ] **Step 5: Commit, push, and exact CI**

```bash
git add connectors/hunter/client.py connectors/hunter/__init__.py \
  tests/unit/test_hunter_contact_enrichment.py
git commit -m "feat: adapt hunter domain search"
```

执行 Global Constraints 的 CI 关门命令。

---

### Task 6: Hunter Email Verifier adapter

**Files:**
- Modify: `connectors/hunter/client.py`
- Modify: `connectors/hunter/__init__.py`
- Create: `tests/unit/test_hunter_email_verification.py`

**Interfaces:**
- Consumes: Task 3 `EmailVerificationResult`, Task 4 transport, configured `HunterConnector`.
- Produces: `HunterConnector.verify(email: str) -> EmailVerificationResult` with bounded polling and privacy claim.
- Updates `MANIFEST.capabilities` to `("contact.enrich", "contact.verify")` and the rate note to
  include `Email Verifier 10/s 300/min`; the manifest never advertises an unimplemented method.

- [ ] **Step 1: Write RED status/poll/error tests**

Parametrize the exact mapping:

```python
@pytest.mark.parametrize(
    ("hunter_status", "expected"),
    [
        ("valid", EmailVerificationOutcome.VERIFIED),
        ("invalid", EmailVerificationOutcome.INVALID),
        ("accept_all", EmailVerificationOutcome.RISKY),
        ("webmail", EmailVerificationOutcome.RISKY),
        ("disposable", EmailVerificationOutcome.RISKY),
        ("unknown", EmailVerificationOutcome.UNVERIFIED),
    ],
)
async def test_verifier_status_mapping(
    hunter_status: str,
    expected: EmailVerificationOutcome,
    configured_connector: HunterConnector,
    transport: FakeHunterTransport,
) -> None:
    transport.responses.append(
        HunterHttpResponse(200, {"data": {"status": hunter_status, "score": 99}})
    )
    result = await configured_connector.verify("buyer@example.com")
    assert result.outcome is expected
    assert "99" not in repr(result)
```

Also prove: local bad email means transport call 0; 202 polls at most two additional calls with
injected sleeper and 30-second budget; 202 exhaustion and 222 return UNVERIFIED/UNKNOWN; 451
returns `privacy_claimed=True`/PRIVACY_REFUSED; score and raw flags/sources disappear; errors and
logs are canary-clean; checked time is the injected UTC clock at terminal outcome.

- [ ] **Step 2: Run RED**

```bash
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_hunter_email_verification.py
```

Expected: `verify` missing or NotImplemented.

- [ ] **Step 3: Implement verifier and bounded polling**

Use exact status enum mapping; never consult `score`. Poll sleeps only through injected
`Callable[[float], Awaitable[None]]`; terminal 202 after three total requests returns UNVERIFIED,
not an exception that a workflow would auto-retry. Retry-After is clamped to the remaining budget.

- [ ] **Step 4: Run GREEN and gates**

```bash
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_hunter_email_verification.py \
  tests/unit/test_hunter_contact_enrichment.py \
  tests/unit/test_hunter_transport.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/ruff check \
  connectors/hunter tests/unit/test_hunter_email_verification.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/mypy connectors/hunter
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python scripts/check_boundaries.py
```

- [ ] **Step 5: Commit, push, and exact CI**

```bash
git add connectors/hunter/client.py connectors/hunter/__init__.py \
  tests/unit/test_hunter_email_verification.py
git commit -m "feat: adapt hunter email verifier"
```

执行 Global Constraints 的 CI 关门命令。

---

### Task 7: Contact-provider Gateway safety stages and task-local slot

**Files:**
- Create: `tool_gateway/checks/contact_provider.py`
- Create: `tool_gateway/handlers/single_result_slot.py`
- Create: `tests/unit/test_contact_provider_gateway_support.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class ContactDiscoveryPreflight:
    tenant_id: TenantId
    hypothesis_id: NeedHypothesisId
    account_id: ProspectAccountId
    category: str
    country: str
    website_domain: str

@dataclass(frozen=True, repr=False)
class ContactVerificationPreflight:
    tenant_id: TenantId
    contact_point: ContactPointView = field(repr=False)
    cache_valid: bool

@runtime_checkable
class ContactDiscoveryPolicyReader(Protocol):
    async def preflight(
        self,
        tenant_id: TenantId,
        hypothesis_id: NeedHypothesisId,
        account_id: ProspectAccountId,
    ) -> ContactDiscoveryPreflight:
        raise NotImplementedError

@runtime_checkable
class ContactCountryPolicyReader(Protocol):
    async def allows_contact_enrichment(
        self, tenant_id: TenantId, country: str
    ) -> bool:
        raise NotImplementedError

@runtime_checkable
class ContactPointPolicyReader(Protocol):
    async def get_contact_point(
        self, tenant_id: TenantId, contact_point_id: ContactPointId
    ) -> ContactPointView:
        raise NotImplementedError

@runtime_checkable
class ProviderQuotaGuard(Protocol):
    async def reserve(
        self, tenant_id: TenantId, capability: str, now: datetime
    ) -> int | None:
        """允许返回 None；拒绝返回 1..86400 秒 Retry-After。"""
        raise NotImplementedError

class ContactResourceTenantCheck: name = "tenant"
class ContactEnrichmentPlaybookCheck: name = "playbook"
class ContactCountryPolicyCheck: name = "country_policy"
class ContactProviderSuppressionCheck: name = "suppression"
class ContactProviderRateLimitCheck: name = "rate_limit"

class ContextLocalSingleResultSlot[T]:
    def __init__(self, handle_prefix: str, id_factory: Callable[[str], str]) -> None:
        raise NotImplementedError

    def put(self, value: T) -> str:
        raise NotImplementedError

    def take(self, handle: str) -> T:
        raise NotImplementedError

    def discard_all(self) -> None:
        raise NotImplementedError
```

The slot constructor receives the exact handle prefix (`ceb` or `veb`) and an ID factory.
Policy/quota dependencies are narrow runtime-checkable Protocols; the check file must not import
domain models/repositories.

- [ ] **Step 1: Write RED stage and slot tests**

Prove ID shape rejects before DB/provider; preflight binds hypothesis/account/tenant and uses
authoritative category/country/domain; unknown country defaults deny; account/contact suppression
denies; provider quota returns fixed rate-limit category; verification cache hit skips quota
reservation; and check exceptions fail closed.

For the slot, prove same task second `put` rejects, wrong handle does not consume, `take` consumes,
`discard_all` clears, cancellation cleanup works, and 20 concurrent asyncio tasks never read another
task's value or tenant canary.

- [ ] **Step 2: Run RED**

```bash
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_contact_provider_gateway_support.py
```

Expected: missing support modules.

- [ ] **Step 3: Implement checks without changing pipeline**

`ContactEnrichmentPlaybookCheck` stores the returned typed preflight in
`ToolInvocationState.preflight`; country and suppression consume it. Verification suppression reads
the tenant-bound ContactPoint View through an injected public-service Protocol, stores
`ContactVerificationPreflight`, then checks Outreach
`SuppressionTarget(contact_point_id=preflight.contact_point.contact_point_id)`.
`ContactProviderRateLimitCheck` skips reservation only when that preflight's 30-day cache is valid.

Implement per-process fixed-window quota with an `asyncio.Lock` and exact Hunter limits
(enrich 15/s + 500/min, verify 10/s + 300/min). It is explicitly Phase 1 single-worker protection;
the class has no durable/multi-worker claims.

- [ ] **Step 4: Run GREEN and gates**

```bash
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_contact_provider_gateway_support.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/ruff check \
  tool_gateway/checks/contact_provider.py \
  tool_gateway/handlers/single_result_slot.py \
  tests/unit/test_contact_provider_gateway_support.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/mypy \
  tool_gateway/checks/contact_provider.py \
  tool_gateway/handlers/single_result_slot.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python scripts/check_boundaries.py
```

- [ ] **Step 5: Commit, push, and exact CI**

```bash
git add tool_gateway/checks/contact_provider.py \
  tool_gateway/handlers/single_result_slot.py \
  tests/unit/test_contact_provider_gateway_support.py
git commit -m "feat: guard contact provider gateway calls"
```

执行 Global Constraints 的 CI 关门命令。

---

### Task 8: `contact.enrich` Tool Gateway plugin

**Files:**
- Create: `tool_gateway/handlers/contact_provider_errors.py`
- Create: `tool_gateway/handlers/contact_enrichment.py`
- Modify: `tool_gateway/handlers/__init__.py`
- Create: `tests/unit/test_contact_enrichment_handler.py`
- Create: `tests/integration/test_tool_gateway_contact_enrichment.py`

**Interfaces:**

```python
MANIFEST = ToolManifest(
    tool_id="contact.enrich",
    version="v1",
    risk_level=RiskLevel.MEDIUM,
    cost_class=CostClass.MEDIUM,
    requires_approval=False,
    idempotency=IdempotencyRequirement.NONE,
    required_permissions=("contact:enrich",),
    checks=("tenant", "permission", "playbook", "country_policy", "suppression", "rate_limit"),
    input_schema={
        "type": "object",
        "required": ("hypothesis_id", "account_id", "role_hints"),
        "properties": {
            "hypothesis_id": {"type": "string"},
            "account_id": {"type": "string"},
            "role_hints": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 10,
            },
        },
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ("provider_ref",),
        "properties": {"provider_ref": {"type": "string"}},
        "additionalProperties": False,
    },
    redact_fields=(),
)

class ContactEnrichmentHandler:
    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        raise NotImplementedError

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> Mapping[str, SafeScalar]:
        raise NotImplementedError

class ToolGatewayContactEnricher:
    async def find_contacts(
        self,
        tenant_id: TenantId,
        hypothesis_id: NeedHypothesisId,
        account_id: ProspectAccountId,
        role_hints: tuple[str, ...],
    ) -> ContactEnrichmentResult:
        raise NotImplementedError
```

- [ ] **Step 1: Write RED handler/Gateway tests**

Assert exact manifest, strict params, max 10 role hints, HMAC fingerprint includes tenant + IDs +
sorted unique canonical hints, audit projection contains only IDs/count, prepare requires matching
`ContactDiscoveryPreflight`, connector configure happens after all checks, success ledger contains
only `ceb_` handle, trusted adapter take-once, and every rejection/exception/cancel/ledger-completion
failure clears the slot.

Integration test with PostgreSQL ledger must scan `tool_calls`, `tool_call_events`, and outbox text
for email/name/source/API-key canaries. Twenty concurrent calls must not cross results. Ambiguous
network error maps to `RECONCILIATION_REQUIRED` and the adapter never auto-reinvokes Gateway.

- [ ] **Step 2: Run RED**

```bash
TEST_DATABASE_URL="$TEST_DATABASE_URL" \
  /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_contact_enrichment_handler.py \
  tests/integration/test_tool_gateway_contact_enrichment.py
```

Expected: handler/manifest imports fail.

- [ ] **Step 3: Implement handler, late-configured provider reader, and trusted adapter**

`prepare` materializes only from typed preflight after checks. `_HunterProviderContactEnricher`
creates/configures a fresh connector via factory inside `execute`; secret resolver never enters
ToolCallContext. `contact_provider_errors.py` contains the single mapping from connector auth/rate/
permanent/transient/uncertain errors to fixed `ToolGatewayError` categories; Task 9 reuses it rather
than duplicating mappings. Handler output is only `{"provider_ref": slot.put(result)}`.

- [ ] **Step 4: Run GREEN and gates**

```bash
TEST_DATABASE_URL="$TEST_DATABASE_URL" \
  /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_contact_enrichment_handler.py \
  tests/integration/test_tool_gateway_contact_enrichment.py \
  tests/unit/test_tool_gateway_manifest.py \
  tests/unit/test_tool_gateway_pipeline.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/ruff check \
  tool_gateway/handlers tests/unit/test_contact_enrichment_handler.py \
  tests/integration/test_tool_gateway_contact_enrichment.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/mypy \
  tool_gateway/handlers/contact_enrichment.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python scripts/check_boundaries.py
```

- [ ] **Step 5: Commit, push, and exact CI**

```bash
git add tool_gateway/handlers/contact_enrichment.py \
  tool_gateway/handlers/contact_provider_errors.py \
  tool_gateway/handlers/__init__.py \
  tests/unit/test_contact_enrichment_handler.py \
  tests/integration/test_tool_gateway_contact_enrichment.py
git commit -m "feat: add contact enrichment gateway tool"
```

执行 Global Constraints 的 CI 关门命令。

---

### Task 9: `contact.verify` Tool Gateway plugin and final slice verification

**Files:**
- Create: `tool_gateway/handlers/contact_verification.py`
- Modify: `tool_gateway/handlers/__init__.py`
- Create: `tests/unit/test_contact_verification_handler.py`
- Create: `tests/integration/test_tool_gateway_contact_verification.py`
- Modify: `tool_gateway/AGENTS.md`
- Modify: `docs/architecture/04-tool-gateway.md`
- Modify: `docs/architecture/08-compliance.md`
- Modify: `docs/architecture/10-database.md`
- Modify: `HANDBOOK.md`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**

```python
MANIFEST = ToolManifest(
    tool_id="contact.verify",
    version="v1",
    risk_level=RiskLevel.MEDIUM,
    cost_class=CostClass.LOW,
    requires_approval=False,
    idempotency=IdempotencyRequirement.NONE,
    required_permissions=("contact:verify",),
    checks=("tenant", "permission", "suppression", "rate_limit"),
    input_schema={
        "type": "object",
        "required": ("contact_point_id",),
        "properties": {"contact_point_id": {"type": "string"}},
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ("provider_ref",),
        "properties": {"provider_ref": {"type": "string"}},
        "additionalProperties": False,
    },
    redact_fields=(),
)

class ContactVerificationHandler:
    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        raise NotImplementedError

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> Mapping[str, SafeScalar]:
        raise NotImplementedError

class ToolGatewayContactVerifier:
    async def verify(
        self,
        tenant_id: TenantId,
        contact_point_id: ContactPointId,
    ) -> EmailVerificationResult:
        raise NotImplementedError
```

- [ ] **Step 1: Write RED handler/cache/privacy tests**

Assert strict contact-point-only params, email absent from context/audit/fingerprint inputs visible to
callers, preflight tenant/kind binding, exact 30-day cache (`now < checked_at + 30 days`), expiry at
equality, cache hit connector/quota call 0, and all four cached outcomes.

For live calls, assert six Hunter statuses reach typed result, slot cleanup matches Task 8, and
privacy claim remains a typed fact for the workflow. Gateway/handler must not call
`record_verification` or erasure itself; add spy assertions proving business writes are zero.

Integration tests scan ledger/events/outbox/logs for email/raw/source/key/score canaries, prove
cross-tenant contact point is indistinguishable from missing, and prove a failed ledger completion
does not return success or leak the slot.

- [ ] **Step 2: Run RED**

```bash
TEST_DATABASE_URL="$TEST_DATABASE_URL" \
  /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_contact_verification_handler.py \
  tests/integration/test_tool_gateway_contact_verification.py
```

Expected: handler/manifest imports fail.

- [ ] **Step 3: Implement cache-aware handler and trusted adapter**

`prepare` consumes `ContactVerificationPreflight`; cached payload never constructs/configures Hunter.
Live payload keeps canonical email repr-disabled. Return the provider-neutral result unchanged to the
trusted adapter; the account-discovery workflow is solely responsible for constructing
`VerificationRecordRequest` and executing erasure on a privacy claim. Errors use only fixed
categories; `privacy_claimed` remains in typed slot, never in ledger output.

- [ ] **Step 4: Update truthful documentation**

Document exact implemented state:

- Hunter connector/handlers exist and tests use no real key/network;
- PII slot is task-local, take-once, non-durable;
- no automatic retry after ambiguous paid call;
- all verification outcomes carry checked time/cost note and cache 30 days;
- score/confidence discarded;
- production `contact.enrich` remains unregistered until real country-policy data and Playbook
  composition are configured;
- account-discovery persistence/workflow, Campaign wiring and UI remain incomplete.

Do not claim real Hunter connectivity, active production enrichment, multi-provider routing, or cost
wallet.

Extend the CI type-check step from
`mypy domains shared tool_gateway apps workflows notification_gateway infra` to
`mypy domains shared tool_gateway connectors apps workflows notification_gateway infra`, so the new
credential/PII boundary code cannot silently fall outside the remote type gate.

- [ ] **Step 5: Run focused GREEN gates**

```bash
TEST_DATABASE_URL="$TEST_DATABASE_URL" \
  /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q \
  tests/unit/test_contact_verification_handler.py \
  tests/integration/test_tool_gateway_contact_verification.py \
  tests/unit/test_contact_enrichment_handler.py \
  tests/integration/test_tool_gateway_contact_enrichment.py \
  tests/unit/test_hunter_email_verification.py \
  tests/unit/test_hunter_contact_enrichment.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/ruff check .
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/mypy \
  domains shared tool_gateway connectors apps workflows notification_gateway infra
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python scripts/check_boundaries.py
/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python scripts/scan_sensitive.py
```

- [ ] **Step 6: Run full no-Docker local verification**

```bash
TEST_DATABASE_URL="$TEST_DATABASE_URL" \
  /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/pytest -q -m 'not e2e' \
  --ignore=tests/integration/test_artifact_store_minio.py \
  --ignore=tests/integration/test_email_feedback_flow.py \
  --ignore=tests/integration/test_scheduler_reply_trigger.py \
  --ignore=tests/integration/test_message_content_reader.py
cd apps/web
npm run gen:api
git diff --exit-code -- src/api/api.d.ts
npm run typecheck
npm run lint
npm run test
npm run build
cd ../..
```

Expected: Python full suite, generated API zero-drift, Web typecheck/lint/tests/build all pass. Existing
non-failing lint warnings must be reported separately; no new warning is accepted in touched files.

- [ ] **Step 7: Characterization mutation audit**

Temporarily mutate each production rule one at a time and prove the named test turns RED, then restore
the production line immediately:

1. retain Hunter `score` → object-graph exclusion test fails;
2. remove tenant predicate in Prospecting read → cross-tenant test fails;
3. change cache comparison from `<` to `<=` → exact-expiry test fails;
4. remove slot discard on cancellation → cancellation cleanup test fails;
5. skip suppression → suppressed target/provider-call-zero test fails;
6. change valid mapping to risky → status matrix fails.

After restoring, rerun the focused GREEN command and `git diff --check`.

- [ ] **Step 8: Commit, push, and exact CI**

```bash
git add tool_gateway/handlers/contact_verification.py \
  tool_gateway/handlers/__init__.py \
  tests/unit/test_contact_verification_handler.py \
  tests/integration/test_tool_gateway_contact_verification.py \
  tool_gateway/AGENTS.md docs/architecture/04-tool-gateway.md \
  docs/architecture/08-compliance.md docs/architecture/10-database.md HANDBOOK.md \
  .github/workflows/ci.yml
git commit -m "feat: complete hunter contact provider slice"
```

执行 Global Constraints 的 CI 关门命令，并额外确认：

```bash
git status --short
git rev-parse HEAD
git rev-parse '@{u}'
```

Expected: clean status and identical local/upstream SHA.

---

## Completion Evidence Matrix

| Requirement | Authoritative evidence |
|---|---|
| 单 Hunter Provider、一个 secret ref | Hunter manifest/AGENTS contract tests |
| Key 不进 URL/日志/模型 | transport request capture + canary scans |
| confidence/score 不保存 | recursive DTO scan + mutation audit |
| typed enrichment + sources | connector contract and Domain Search matrix |
| 六状态确定性验证 | verifier mapping matrix |
| 202 bounded polling、451 privacy claim | verifier async tests |
| 全结果 30 天缓存 | Prospecting persistence + exact-expiry handler tests |
| risky/unknown 不可用 | Prospecting model/service regression tests |
| PII 不进 durable ledger | PostgreSQL ledger/outbox canary integration tests |
| 并发 slot 不串数据 | 20-task ContextVar + Gateway concurrency tests |
| 抑制/政策/配额 fail closed | stage unit tests and provider-call-zero assertions |
| 不自动重复不确定付费调用 | reconciliation mapping + invoker call-count test |
| 租户隔离 | service/repository/Gateway cross-tenant tests |
| 架构依赖边界 | `scripts/check_boundaries.py` + mypy |
| 每个小任务独立远端验证 | exact commit SHA GitHub run URL recorded per task |
| 整个切片无回归 | Task 9 full local gates + exact final HEAD CI |

## Next Slice After This Plan

只有上述矩阵全部有证据后，才为 `workflows/account_discovery` 单独写设计/计划，实现：

```text
NeedHypothesis → resolve_account → contact.enrich
→ persist contact + LegalBasis → contact.verify
→ record_verification / privacy erasure → assign_owner
```

该后续切片必须补齐真实 CountryPolicy 数据源与 production composition；在此之前，当前计划
只交付可测试的 Provider/Gateway 插件，不把未配置能力伪装成已上线功能。
