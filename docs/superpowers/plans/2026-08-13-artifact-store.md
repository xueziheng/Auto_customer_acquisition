# Artifact Store 持久化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立真实 PostgreSQL + S3/MinIO 的不可变 Artifact Store，使原始证据与系统生成邮件草稿严格分层、租户隔离、可重启恢复、可校验且不泄漏内容或凭证。

**Architecture:** `artifact_store` 定义 typed DTO、Store/Repository/UoW 与 blob transport Protocol，并在 `service_impl.py` 编排外部对象写入、数据库 winner 和补偿；`infra/db` 只实现 tenant-bound PostgreSQL metadata；唯一直接使用 boto3 的 concrete adapter 位于 `connectors/object_store`。原始资料按 tenant+kind+hash 去重，派生产物按 tenant+idempotency key 判定幂等/冲突，读取时重新校验长度与 SHA-256。

**Tech Stack:** Python 3.12、SQLAlchemy 2.x async、PostgreSQL 16、Alembic、boto3、MinIO、pytest/testcontainers、ruff、mypy。

## Global Constraints

- 权威设计：`docs/superpowers/specs/2026-08-13-artifact-store-design.md`，基线至少包含边界修正 commit `e428758dc0edb23bf05d02d3af651da795bcd0cc`。
- 所有 Python 命令使用 `PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH`；不得依赖系统 Python。
- `artifact_store`、`infra/db`、domains、workflows 与 agent_runtime 不得 import boto3；唯一 boto3 adapter 是 `connectors/object_store/s3.py`。
- S3 credential 原值只能在 connector adapter 内解析和持有；不得进入参数 DTO、返回值、`repr`、日志、异常、PostgreSQL、workflow context、outbox 或 Tool Gateway ledger。
- PostgreSQL 不保存 artifact bytes、邮件 subject/body、原始 MIME、S3 endpoint/bucket/object key 以外的对象传输元数据；公共 DTO 不暴露 object key。
- `raw_artifacts` 与 `artifacts` 是不同表、不同 typed kind、不同 Store；生成草稿不能作为原始证据。
- 所有表与查询显式 `tenant_id`；跨租户读取与不存在使用同一固定错误；同 hash 跨 tenant 不共享 metadata 或 object key。
- 无 update/delete/list-all 公共 API；原始资料同租户 `(kind, hash)` 去重；派生产物同租户幂等键异内容或异绑定固定冲突。
- S3 调用失败不提交 metadata；数据库/commit 失败只补偿本次随机 artifact object；cleanup 的 `BaseException` 不覆盖 primary。
- 外部 cancellation 必须等待在途同步 boto3 调用达到确定结果、完成必要补偿，再传播原 `CancelledError`。
- 新测试先取得 genuine RED；不得把 fixture、Docker、PATH、import、warning 或测试自身错误冒充产品 RED。
- 每个 Task 独立普通 commit、push `codex/phase1-implementation`，等待精确 HEAD SHA 的 GitHub Actions `success` 后才能进入下一 Task；禁止 amend、force push。
- 每个 Task 新文件最终 Git mode `100644`；只删除该 Task 产生的 AppleDouble、`dist`、coverage、容器或 `/tmp` 产物。

---

## File Map

### Artifact contract and orchestration

- `artifact_store/store.py`: public frozen DTO、typed kind、Raw/Generated Store Protocol。
- `artifact_store/errors.py`: 固定安全错误。
- `artifact_store/transport.py`: `ObjectBlobTransport` 与内部 blob-not-found contract。
- `artifact_store/repository.py`: metadata records、repository/UoW Protocol、typed insert outcome。
- `artifact_store/service_impl.py`: hash、object key、put/get、winner、补偿与完整性校验。

### Concrete infrastructure

- `connectors/object_store/AGENTS.md`: SDK/凭证/错误分类约束。
- `connectors/object_store/config.py`: 严格环境配置。
- `connectors/object_store/s3.py`: 唯一 boto3 adapter。
- `migrations/versions/0014_artifact_store.py`: 两张 metadata 表。
- `infra/db/tables.py`: ORM parity。
- `infra/db/repositories/artifacts.py`: tenant-bound PostgreSQL repositories。
- `infra/db/artifact_uow.py`: per-operation async session/commit/rollback/close。

### Tests and delivery

- `tests/unit/test_artifact_store_contracts.py`: DTO/kind/errors/Protocol 边界。
- `tests/unit/test_artifact_store_config.py`: endpoint、secret ref、limit、repr。
- `tests/unit/test_artifact_store_service.py`: orchestration、failure/cancellation、mutation matrix。
- `tests/integration/test_artifact_store_persistence.py`: migration/ORM/repository/DB guards。
- `tests/integration/test_artifact_store_minio.py`: real PostgreSQL + MinIO store behavior/concurrency。
- `tests/integration/test_demo_artifact_store.py`: subprocess demo + DB/S3 readback。
- `scripts/demo_artifact_store.py`: offline acceptance demo。

---

### Task 1: Artifact contracts and strict object-store configuration

**Files:**
- Modify: `artifact_store/store.py`
- Modify: `artifact_store/AGENTS.md`
- Create: `artifact_store/errors.py`
- Create: `artifact_store/transport.py`
- Create: `connectors/object_store/__init__.py`
- Create: `connectors/object_store/AGENTS.md`
- Create: `connectors/object_store/config.py`
- Modify: `infra/.env.example`
- Create: `tests/unit/test_artifact_store_contracts.py`
- Create: `tests/unit/test_artifact_store_config.py`

**Interfaces:**
- Produces `RawArtifactKind`, `GeneratedArtifactKind`, `RawArtifactMeta`, `GeneratedArtifactMeta`, `RawArtifactStore`, `GeneratedArtifactStore`.
- Produces `ObjectBlobTransport.put/get/delete` and `BlobObjectNotFoundError`.
- Produces `S3ObjectStoreSettings.from_environ(environ)` containing endpoint, bucket, region, access/secret refs and two explicit byte limits.
- Later tasks consume these exact names; Task 1 does not import SQLAlchemy or boto3.

- [ ] **Step 1: Write contract RED**

Create `tests/unit/test_artifact_store_contracts.py`. Load the public module normally and assert real construction/Protocol behavior. The wished-for contract is:

```python
from datetime import UTC, datetime

import pytest

from artifact_store.errors import (
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
)
from artifact_store.store import (
    GeneratedArtifactKind,
    GeneratedArtifactMeta,
    GeneratedArtifactStore,
    RawArtifactKind,
    RawArtifactMeta,
    RawArtifactStore,
)
from artifact_store.transport import ObjectBlobTransport
from shared.schemas.identifiers import ArtifactId, IdempotencyKey, RunId, TenantId

NOW = datetime(2026, 8, 13, 6, 0, tzinfo=UTC)
HASH = "a" * 64


def test_raw_and_generated_kinds_are_disjoint() -> None:
    assert RawArtifactKind.EMAIL_RAW.value == "email_raw"
    assert GeneratedArtifactKind.EMAIL_DRAFT.value == "email_draft"
    assert set(RawArtifactKind).isdisjoint(set(GeneratedArtifactKind))


def test_generated_meta_is_frozen_and_repr_hides_storage_details() -> None:
    meta = GeneratedArtifactMeta(
        tenant_id=TenantId("tn_00000000000000000000000000"),
        artifact_id=ArtifactId("art_00000000000000000000000000"),
        kind=GeneratedArtifactKind.EMAIL_DRAFT,
        content_hash=HASH,
        size_bytes=32,
        mime_type="application/vnd.tradeos.email-draft+json",
        workflow_run_id=RunId("run_00000000000000000000000000"),
        subject_ref="enr_00000000000000000000000000",
        sequence_number=1,
        idempotency_key=IdempotencyKey(
            "enr_00000000000000000000000000:1:draft"
        ),
        generated_by="outreach_agent_v1",
        generated_at=NOW,
    )
    assert meta.content_hash == HASH
    assert "bucket" not in repr(meta).lower()
    assert "object" not in repr(meta).lower()
    with pytest.raises(AttributeError):
        meta.size_bytes = 33  # type: ignore[misc]


@pytest.mark.parametrize("bad_hash", ["A" * 64, "a" * 63, "g" * 64, ""])
def test_meta_rejects_noncanonical_hash(bad_hash: str) -> None:
    with pytest.raises(Exception, match="artifact metadata 无效"):
        RawArtifactMeta(
            TenantId("tn_00000000000000000000000000"),
            ArtifactId("art_00000000000000000000000000"),
            RawArtifactKind.PDF,
            bad_hash,
            1,
            "application/pdf",
            None,
            NOW,
        )


def test_protocols_are_runtime_checkable() -> None:
    assert RawArtifactStore is not GeneratedArtifactStore
    assert hasattr(ObjectBlobTransport, "put")


def test_artifact_errors_have_fixed_safe_messages() -> None:
    assert str(ArtifactConflictError()) == "Artifact 幂等记录冲突"
    assert str(ArtifactNotFoundError()) == "Artifact 不存在"
    assert str(ArtifactIntegrityError()) == "Artifact 完整性校验失败"
```

Add mutation-strength cases for:

- all raw kinds and exact MIME mapping;
- generated kind passed to raw meta and vice versa;
- non-UTC/naive/string timestamps;
- invalid tenant/artifact/run/subject/idempotency/generated-by types and control characters;
- zero/negative/bool/float size and sequence number;
- `repr` not containing a test body marker, endpoint, bucket or object key;
- Protocol implementations with the exact async signatures.

- [ ] **Step 2: Write strict configuration RED**

Create `tests/unit/test_artifact_store_config.py` around this exact API:

```python
from connectors.object_store.config import S3ObjectStoreSettings


def _production() -> dict[str, str]:
    return {
        "TRADEOS_DEV_MODE": "false",
        "S3_ENDPOINT": "https://objects.example.invalid",
        "S3_BUCKET_ARTIFACTS": "tradeos-artifacts",
        "S3_ACCESS_KEY_REF": "ARTIFACT_S3_ACCESS_KEY",
        "S3_SECRET_KEY_REF": "ARTIFACT_S3_SECRET_KEY",
        "S3_REGION": "us-east-1",
        "RAW_ARTIFACT_MAX_BYTES": "10485760",
        "GENERATED_ARTIFACT_MAX_BYTES": "1048576",
    }


def test_production_config_is_explicit_and_frozen() -> None:
    settings = S3ObjectStoreSettings.from_environ(_production())
    assert settings.endpoint == "https://objects.example.invalid"
    assert settings.raw_max_bytes == 10_485_760
    assert settings.generated_max_bytes == 1_048_576
    assert "objects.example.invalid" not in repr(settings)
    assert "tradeos-artifacts" not in repr(settings)


def test_dev_mode_allows_only_loopback_http_with_explicit_port() -> None:
    values = _production() | {
        "TRADEOS_DEV_MODE": "true",
        "S3_ENDPOINT": "http://127.0.0.1:19000",
    }
    assert S3ObjectStoreSettings.from_environ(values).dev_mode is True
```

Parameterize missing/blank values, `TRADEOS_DEV_MODE` casing, production HTTP, dev non-loopback, missing/leading-zero/out-of-range port, credentials/query/fragment/path, noncanonical bucket/region, invalid secret refs, size `0/-1/+1/01/1.0/true/NaN/Infinity`, unknown object types, and input mapping mutation after construction. Assert every failure is the fixed `PolicyViolation("Artifact Store 配置无效")` and no raw input appears in the exception or `repr`.

- [ ] **Step 3: Run genuine RED**

Run:

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_artifact_store_contracts.py \
         tests/unit/test_artifact_store_config.py -q -W error
```

Expected: collection succeeds and failures are missing typed contracts/config, not environment or import corruption. Record exact failure composition in `.superpowers/sdd/2026-08-13-artifact-store/task-1-report.md`.

- [ ] **Step 4: Implement minimal contracts**

Replace the ambiguous single `ArtifactMeta/ArtifactStore` contract in `artifact_store/store.py`; there are no current production callers, so do not retain a second legacy interface. Use frozen dataclasses and runtime-checkable Protocols:

```python
class RawArtifactKind(str, Enum):
    EMAIL_RAW = "email_raw"
    CHAT_SCREENSHOT = "chat_screenshot"
    PDF = "pdf"
    WORD = "word"
    EXCEL = "excel"
    WEB_SNAPSHOT = "web_snapshot"
    IMAGE = "image"
    AUDIO = "audio"


class GeneratedArtifactKind(str, Enum):
    EMAIL_DRAFT = "email_draft"


@runtime_checkable
class RawArtifactStore(Protocol):
    async def put(
        self,
        tenant_id: TenantId,
        kind: RawArtifactKind,
        content: bytes,
        mime_type: str,
        uploaded_by: UserId | None = None,
    ) -> RawArtifactMeta: ...

    async def get(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> tuple[RawArtifactMeta, bytes]: ...

    async def get_meta(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> RawArtifactMeta: ...


@runtime_checkable
class GeneratedArtifactStore(Protocol):
    async def put(
        self,
        tenant_id: TenantId,
        kind: GeneratedArtifactKind,
        content: bytes,
        mime_type: str,
        *,
        workflow_run_id: RunId,
        subject_ref: str,
        sequence_number: int,
        idempotency_key: IdempotencyKey,
        generated_by: str,
    ) -> GeneratedArtifactMeta: ...

    async def get(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> tuple[GeneratedArtifactMeta, bytes]: ...

    async def get_meta(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> GeneratedArtifactMeta: ...
```

`ObjectBlobTransport` is async and bucket-bound:

```python
@runtime_checkable
class ObjectBlobTransport(Protocol):
    async def put(self, object_key: str, content: bytes) -> None: ...
    async def get(self, object_key: str) -> bytes: ...
    async def delete(self, object_key: str) -> None: ...
```

Fix MIME policy exactly:

```text
email_raw       message/rfc822
chat_screenshot image/png | image/jpeg | image/webp
pdf             application/pdf
word            application/vnd.openxmlformats-officedocument.wordprocessingml.document
excel           application/vnd.openxmlformats-officedocument.spreadsheetml.sheet | text/csv
web_snapshot    text/html
image           image/png | image/jpeg | image/webp
audio           audio/mpeg | audio/wav | audio/mp4
email_draft     application/vnd.tradeos.email-draft+json
```

Errors have constructors with no caller-controlled message. `ArtifactConflictError`, `ArtifactNotFoundError`, and `ArtifactIntegrityError` subclass `TradeOSError`; `BlobObjectNotFoundError` remains transport-internal and also uses a fixed message.

- [ ] **Step 5: Implement strict settings and connector rules**

Create `connectors/object_store/AGENTS.md` documenting: boto3 is allowed only here; connector contains no dedup/business rules; secret refs are resolved only during adapter construction; endpoint/bucket/key/credentials are forbidden in logs/errors/repr; `NoSuchKey` is typed, all other SDK/network failures become fixed `TransientError`.

Update `artifact_store/AGENTS.md` in the same commit so it no longer advertises one ambiguous `ArtifactStore`: state the Raw/Generated split, immutable/no-delete API, hash verification, tenant isolation and the rule that generated artifacts are not original evidence. Keep S3 SDK/credential ownership explicitly in `connectors/object_store`.

Implement a frozen `S3ObjectStoreSettings` whose `from_environ` copies only the eight named settings. Use `urllib.parse.urlsplit`, `ipaddress.ip_address`, and `validate_environment_secret_reference`; never resolve credentials here. Environment integers must match `[1-9][0-9]*` and fit signed 64-bit. `__repr__` returns exactly `S3ObjectStoreSettings(dev_mode=False)` or `S3ObjectStoreSettings(dev_mode=True)`.

Update `infra/.env.example` by replacing direct `S3_ACCESS_KEY/S3_SECRET_KEY` entries with non-runnable references and all explicit settings:

```text
S3_ENDPOINT=REQUIRED_CANONICAL_HTTPS_ENDPOINT
S3_BUCKET_ARTIFACTS=REQUIRED_ARTIFACT_BUCKET
S3_ACCESS_KEY_REF=REQUIRED_S3_ACCESS_KEY_ENV_NAME
S3_SECRET_KEY_REF=REQUIRED_S3_SECRET_KEY_ENV_NAME
S3_REGION=REQUIRED_AWS_REGION
RAW_ARTIFACT_MAX_BYTES=REQUIRED_POSITIVE_INTEGER
GENERATED_ARTIFACT_MAX_BYTES=REQUIRED_POSITIVE_INTEGER
```

- [ ] **Step 6: Verify GREEN and scoped gates**

Run:

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_artifact_store_contracts.py \
         tests/unit/test_artifact_store_config.py -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  ruff check artifact_store connectors/object_store \
             tests/unit/test_artifact_store_contracts.py \
             tests/unit/test_artifact_store_config.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  mypy artifact_store connectors/object_store
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python3 scripts/check_boundaries.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python3 scripts/scan_sensitive.py
git diff --check
```

Expected: all exit 0, no warning, no AppleDouble or NUL.

- [ ] **Step 7: Full gate, review, commit, push and exact CI**

Run `make check` and `pytest tests/integration -q -W error`. Inspect the exact allowlist and staged patch. Stage new files with `git add --chmod=-x`; require mode `100644`, cached diff-check and staged sensitive scan. Commit:

```bash
git commit -m "feat(artifacts): define immutable storage contracts"
git push origin codex/phase1-implementation
```

Wait for the GitHub run whose `headSha` exactly equals `git rev-parse HEAD`; require every job/step success and branch `0 ahead / 0 behind`. Do not begin Task 2 before this gate.

---

### Task 2: PostgreSQL metadata, migration and tenant-bound UoW

**Files:**
- Create: `artifact_store/repository.py`
- Create: `migrations/versions/0014_artifact_store.py`
- Modify: `infra/db/tables.py`
- Create: `infra/db/repositories/artifacts.py`
- Create: `infra/db/artifact_uow.py`
- Modify: `tests/integration/test_migrations.py`
- Modify: `tests/integration/test_repositories.py`
- Create: `tests/integration/test_artifact_store_persistence.py`

**Interfaces:**
- Produces `RawArtifactRecord`, `GeneratedArtifactRecord`, `ArtifactInsertStatus`, `RawArtifactInsertResult`, `GeneratedArtifactInsertResult`.
- Produces `RawArtifactRepository`, `GeneratedArtifactRepository`, `ArtifactUnitOfWork`, `ArtifactUnitOfWorkFactory` Protocols.
- Produces `RawArtifactRepositoryImpl`, `GeneratedArtifactRepositoryImpl`, `SqlAlchemyArtifactUnitOfWork`.
- Task 3 consumes the UoW factory; this task performs no S3 operation.

- [ ] **Step 1: Write repository/UoW RED**

Create `tests/integration/test_artifact_store_persistence.py` using the real migrated PostgreSQL fixture and wished-for APIs:

```python
from sqlalchemy.ext.asyncio import async_sessionmaker

from artifact_store.repository import ArtifactInsertStatus, RawArtifactRecord
from artifact_store.store import RawArtifactKind, RawArtifactMeta
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork


async def test_raw_repository_is_tenant_bound_and_idempotent(
    integration_engine,
) -> None:
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    record = _raw_record(tenant, content_hash="a" * 64)
    async with SqlAlchemyArtifactUnitOfWork(factory, tenant) as uow:
        first = await uow.raw.insert_if_absent(record)
    async with SqlAlchemyArtifactUnitOfWork(factory, tenant) as uow:
        second = await uow.raw.insert_if_absent(record)
    assert first.status is ArtifactInsertStatus.CREATED
    assert second.status is ArtifactInsertStatus.EXISTING
    assert second.winner == first.winner
```

Add real tests for generated same-key same-record, same-key different record returns existing winner for service-level comparison, get-by-id/get-by-hash/get-by-key, cross-tenant miss, tenant argument mismatch raises `TenantIsolationViolation` plus fixed CRITICAL, commit failure rollback, rollback/close BaseException primary preservation, and no public update/delete/list methods.

- [ ] **Step 2: Write migration/schema RED**

Extend `tests/integration/test_migrations.py` with `test_artifact_store_0014_roundtrip_and_guards`. It must:

1. start at head and assert `alembic_version == '0014'`;
2. inspect exact two-table columns, PK/UNIQUE/CHECK/index names;
3. insert valid raw/generated rows;
4. prove DB rejects uppercase/bad hash, size zero, bad kind/MIME pair, unsafe key, key not matching class/tenant/artifact, invalid generated binding, and duplicate tenant idempotency key;
5. downgrade to `0013`, assert only these two tables disappear and existing `0013` tables remain;
6. upgrade to `0014`, re-run guards;
7. restore head even when an assertion fails.

Extend ORM parity in `tests/integration/test_repositories.py` so `Base.metadata` exactly matches head, including the two new tables and constraints.

- [ ] **Step 3: Run genuine PostgreSQL RED**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/integration/test_artifact_store_persistence.py \
         tests/integration/test_migrations.py \
         tests/integration/test_repositories.py -q -W error
```

Expected: existing tests pass; new failures are missing `0014`, ORM rows, repository/UoW contracts. Docker/Alembic/fixture/path must be healthy.

- [ ] **Step 4: Define repository contracts**

Create `artifact_store/repository.py` with internal records that hide object keys from repr:

```python
@dataclass(frozen=True)
class RawArtifactRecord:
    meta: RawArtifactMeta
    object_key: str = field(repr=False)


class ArtifactInsertStatus(str, Enum):
    CREATED = "created"
    EXISTING = "existing"


@runtime_checkable
class RawArtifactRepository(Protocol):
    async def get_by_id(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> RawArtifactRecord | None: ...
    async def get_by_hash(
        self, tenant_id: TenantId, kind: RawArtifactKind, content_hash: str
    ) -> RawArtifactRecord | None: ...
    async def insert_if_absent(
        self, record: RawArtifactRecord
    ) -> RawArtifactInsertResult: ...
```

Define equivalent generated methods keyed by id and idempotency key. `ArtifactUnitOfWork` exposes only `raw` and `generated`; context exit owns commit/rollback/close.

- [ ] **Step 5: Implement migration and ORM parity**

Create revision `0014`, `down_revision='0013'`. Exact SQL invariants:

```text
raw_artifacts PK             tenant_id, artifact_id
raw_artifacts UNIQUE         tenant_id, kind, content_hash
artifacts PK                 tenant_id, artifact_id
artifacts UNIQUE             tenant_id, idempotency_key
artifact_id                  ^art_[0-7][0-9A-HJKMNP-TV-Z]{25}$
content_hash                 ^[0-9a-f]{64}$
size_bytes                   > 0
object_key                   raw/generated || '/' || tenant_id || '/' || artifact_id
timestamps                   TIMESTAMPTZ NOT NULL
```

Use `String(32)` tenant/ID/binding fields, `String(64)` hash/producer, `String(200)` idempotency key, `String(128)` object key, `String(100)` MIME and `BigInteger` size. Add exact kind/MIME pair CHECKs copied from Task 1. No content, JSON, endpoint, bucket, credential or free exception columns. Downgrade removes only `artifacts`, then `raw_artifacts`.

Add `RawArtifactRow` and `GeneratedArtifactRow` to `infra/db/tables.py`; do not use `Base.metadata.create_all` in production.

- [ ] **Step 6: Implement tenant-bound repositories and UoW**

Repositories subclass `TenantScopedRepository`, bind one session and tenant, and use `_require_tenant` with fixed `security.tenant_isolation` CRITICAL before `TenantIsolationViolation`. Inserts use PostgreSQL:

```python
insert(Row)
.values(...)
.on_conflict_do_nothing(constraint="uq_...")
.returning(Row)
```

If no row is returned, perform a tenant-scoped canonical winner query. Do not catch broad `IntegrityError`, parse constraint names from exception strings, commit in repository, or select a cross-tenant winner.

`SqlAlchemyArtifactUnitOfWork` follows the hardened Outreach/Sending Identity pattern: new session per enter, commit on clean exit, rollback on body or commit failure, close always; rollback/close `BaseException` never replaces a primary, while a lone close cancellation propagates.

- [ ] **Step 7: Verify GREEN, migration roundtrip and full gates**

Run the exact three-file PostgreSQL command again, then scoped ruff/mypy, boundary, sensitive and diff-check. Run:

```bash
make check
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/integration -q -W error
```

Inspect real schema and query `information_schema.columns` to prove neither table has content/body/subject/endpoint/bucket/credential columns.

- [ ] **Step 8: Review, commit, push and exact CI**

Stage only Task 2 files, modes `100644`, cached diff/sensitive clean. Commit and push:

```bash
git commit -m "feat(artifacts): persist tenant metadata"
git push origin codex/phase1-implementation
```

Wait for exact SHA CI success and `0 ahead / 0 behind` before Task 3.

---

### Task 3: S3/MinIO adapter and immutable store orchestration

**Files:**
- Create: `connectors/object_store/s3.py`
- Create: `artifact_store/service_impl.py`
- Modify: `infra/docker-compose.yml`
- Create: `tests/unit/test_artifact_store_service.py`
- Create: `tests/integration/test_artifact_store_minio.py`

**Interfaces:**
- Produces `S3ObjectBlobTransport` implementing `ObjectBlobTransport`.
- Produces local structural `ObjectStoreSecretResolver.resolve(secret_ref: str) -> str`; `EnvironmentSecretResolver` satisfies it without connector-to-infra imports.
- Produces `RawArtifactStoreImpl` and `GeneratedArtifactStoreImpl` implementing public Store Protocols.
- Constructors consume `ArtifactUnitOfWorkFactory`, bucket-bound transport, explicit maximum bytes, UTC clock and optional ID generator for deterministic tests.

- [ ] **Step 1: Write orchestration RED**

Create fake repository/UoW and deterministic transport in `tests/unit/test_artifact_store_service.py`, but assert public Store results and durable order rather than mock call counts alone. Cover:

```python
async def test_generated_put_never_persists_body_in_metadata() -> None:
    marker = b"customer-body-marker"
    store, fake_db, blob = _generated_store()
    meta = await store.put(
        TENANT,
        GeneratedArtifactKind.EMAIL_DRAFT,
        marker,
        "application/vnd.tradeos.email-draft+json",
        workflow_run_id=RUN,
        subject_ref=ENROLLMENT,
        sequence_number=1,
        idempotency_key=KEY,
        generated_by="outreach_agent_v1",
    )
    assert blob.objects[f"generated/{TENANT}/{meta.artifact_id}"] == marker
    assert marker not in repr(fake_db.records).encode()
```

Add tests for existing raw short-circuit before blob, generated same-key idempotency, different hash/binding conflict, MIME mismatch, configured byte cap, S3 failure no repository insert, commit failure cleanup, loser cleanup then winner, cleanup failure with/without primary, blob missing read mapping, size/hash corruption + one CRITICAL, cross-tenant miss, cancellation during put/get/delete, primary RuntimeError/CancelledError identity preservation, and no content/object key in logs/errors/repr.

- [ ] **Step 2: Write real MinIO RED**

Create `tests/integration/test_artifact_store_minio.py` with a session-scoped `DockerContainer("minio/minio:RELEASE.2025-04-22T22-12-26Z")`; this immutable tag was verified through the registry while writing the plan. Pin the same image in `infra/docker-compose.yml`; do not permit an unpinned CI dependency. Generate test credentials at runtime with `secrets.token_urlsafe`, never print them, create one random bucket, and build a real `S3ObjectBlobTransport` through secret refs.

Test real PostgreSQL + MinIO:

- raw put/get/meta and byte/hash agreement;
- raw same tenant dedup without second object;
- generated same key idempotency and different payload conflict;
- 20 concurrent raw puts yield one metadata row and one object;
- 20 concurrent generated puts yield one winner;
- same bytes in two tenants produce different IDs/keys/rows;
- cross-tenant get equals nonexistent;
- mutate object bytes through a test-only boto3 client, then fixed integrity error + safe CRITICAL;
- use a dedicated transport pointed at an unused loopback port: put fails and creates no metadata, without stopping or contaminating the shared MinIO fixture;
- injected commit failure: object removed;
- cleanup failure: primary preserved;
- SQL scans prove a unique body marker is absent from every textual artifact column.

- [ ] **Step 3: Run genuine RED**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_artifact_store_service.py \
         tests/integration/test_artifact_store_minio.py -q -W error
```

Expected: dependencies/containers collect correctly; failures are missing adapter/store behavior.

- [ ] **Step 4: Implement the S3 adapter**

`S3ObjectBlobTransport` accepts `S3ObjectStoreSettings` and the local structural `ObjectStoreSecretResolver`, resolves only the two configured refs inside its constructor, creates a bucket-bound boto3 client, then discards local raw credential variables. Do not import the Gmail-specific resolver or `infra.secrets` from the connector; application/demo composition injects `EnvironmentSecretResolver` structurally. Its repr is exactly `S3ObjectBlobTransport()`.

All boto3 operations run in `asyncio.to_thread`. Use one helper that creates a task, awaits it through `asyncio.shield`, and on caller cancellation awaits the same task to completion before re-raising the original cancellation object. Map only S3 `NoSuchKey/404` to `BlobObjectNotFoundError`; all SDK/network failures become `TransientError("Artifact 对象存储暂不可用")` from `None`. Never log or rethrow a boto exception.

`put_object`, `get_object` (read and close streaming body), and `delete_object` use the settings-bound bucket. The adapter accepts object keys only after the Store has generated them; it still rejects unsafe shape defensively.

- [ ] **Step 5: Implement immutable Store services**

Use `hashlib.sha256(content).hexdigest()` and `ArtifactId(new_id("art"))`. The exact write order is:

```text
validate → pre-read canonical winner → mark put_attempted
→ blob.put → repository insert-if-absent → UoW commit
→ if loser, delete own object → compare and return winner
```

Set `put_attempted=True` before awaiting transport so a cancellation that completes the underlying put still triggers delete. Cleanup helper catches `BaseException`; with primary it logs only `Artifact 补偿清理失败` and preserves primary, without primary it raises fixed `TransientError`.

On generated existing/winner, compare every caller-controlled canonical field: kind, hash, size, MIME, run, subject, sequence, key and generated-by. `generated_at` is assigned once by the Store and is never compared to a retry because the caller does not supply it; return the winner's persisted time. Raw dedup winner returns existing uploader/time without overwrite.

Read metadata first; map blob-not-found to fixed artifact-not-found; compare length and hash before returning. Integrity logger name is `security.artifact_integrity`, level CRITICAL, message `检测到 Artifact 完整性违规`, and extra contains only tenant/artifact/kind/rule.

- [ ] **Step 6: Verify GREEN and mutation strength**

Run unit + real MinIO focused tests. Temporarily apply and revert these test-only mutations, proving RED each time:

1. omit hash recheck;
2. remove tenant predicate from get;
3. return generated winner without field comparison;
4. mark `put_attempted` only after await;
5. let cleanup exception replace primary;
6. import boto3 from artifact_store instead of connector (boundary must fail).

Do not leave mutations in the worktree.

- [ ] **Step 7: Full gates, review, commit, push and exact CI**

Run scoped ruff/mypy, boundary, explicit sensitive, `make check`, full integration `-W error`, diff-check and filesystem cleanup. Stage exact Task 3 files as `100644`. Commit:

```bash
git commit -m "feat(artifacts): store immutable objects in minio"
git push origin codex/phase1-implementation
```

Require exact SHA CI success and branch sync before Task 4.

---

### Task 4: Offline acceptance demo, runbook and final audit

**Files:**
- Create: `scripts/demo_artifact_store.py`
- Create: `tests/integration/test_demo_artifact_store.py`
- Modify: `artifact_store/AGENTS.md`
- Modify: `connectors/object_store/AGENTS.md`
- Modify: `docs/architecture/05-data-plane.md`
- Modify: `docs/architecture/10-database.md`

**Interfaces:**
- Produces `async def run_demo(environ: Mapping[str, str]) -> dict[str, object]` for integration tests and `def main() -> int` for CLI.
- No production composition factory is introduced; the script is explicitly demo-only and uses the same concrete stores/UoW/connector as production composition will later consume.

- [ ] **Step 1: Write demo/subprocess RED**

Create `tests/integration/test_demo_artifact_store.py`. Reuse real PostgreSQL and a pinned MinIO container. Invoke `sys.executable scripts/demo_artifact_store.py` with an exact environment containing only `DATABASE_URL`, public S3 settings, two secret ref names and their generated values. Run twice against the same databases.

Assert each success has return code 0, empty stderr and exactly one JSON line. Parse only safe fields:

```json
{
  "tenant_id": "tn_...",
  "raw_artifact_id": "art_...",
  "generated_artifact_id": "art_...",
  "raw_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "generated_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "raw_put_count": 2,
  "raw_row_count": 1,
  "generated_put_count": 2,
  "generated_row_count": 1
}
```

The two runs must have different tenants and artifact IDs. Independent SQL/S3 readback must prove each tenant has exactly one raw + one generated row/object, hashes match bytes, no content marker is in stdout/stderr/DB textual columns, and cross-tenant reads fail identically. Add invalid DSN, invalid endpoint, missing secret and unavailable MinIO subprocess cases: nonzero, stdout empty, stderr exactly `Artifact Store 演示运行失败\n`, injected DSN/endpoint/credential/content markers absent.

- [ ] **Step 2: Run genuine RED**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/integration/test_demo_artifact_store.py -q -W error
```

Expected: file-missing or missing demo API only; PG/MinIO fixture healthy.

- [ ] **Step 3: Implement the demo**

`main()` copies `os.environ`, calls `asyncio.run`, prints canonical JSON only after every readback succeeds, and catches ordinary exceptions at the process boundary. It never loads `.env`, uses a default URL/bucket/limit, or prints traceback/exception.

`run_demo`:

1. parses `S3ObjectStoreSettings`;
2. builds and owns one async engine/session factory;
3. constructs `SqlAlchemyArtifactUnitOfWork`, `S3ObjectBlobTransport`, Raw/Generated stores;
4. creates `TenantId(new_id("tn"))`;
5. writes the same safe raw fixture twice and same generated draft bytes twice;
6. reads both back and validates hash/bytes/idempotent IDs;
7. uses a separate SQL session to count only the random tenant rows;
8. disposes engine in `finally`, preserving primary errors from cleanup.

Success output never contains MIME, generated_by, subject_ref, endpoint, bucket, object key or content. The demo must not create/delete tables or clean data across tenants.

- [ ] **Step 4: Update docs and pin MinIO**

Pin `infra/docker-compose.yml` MinIO to the same immutable version used in integration tests. Update:

- `artifact_store/AGENTS.md`: Raw vs Generated, no legacy single-store wording, object integrity, tenant isolation, no update/delete, S3 adapter boundary;
- `connectors/object_store/AGENTS.md`: exact supported operations, config, credential and error rules;
- `docs/architecture/05-data-plane.md`: raw evidence and generated artifacts are separate layers/indexes;
- `docs/architecture/10-database.md`: exact two table purposes and forbidden content columns.

Add narrow documentation assertions to the demo test based on stable semantic headings/links, not brittle full sentences or whitespace order.

- [ ] **Step 5: Run final acceptance**

```bash
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/unit/test_artifact_store_contracts.py \
         tests/unit/test_artifact_store_config.py \
         tests/unit/test_artifact_store_service.py \
         tests/integration/test_artifact_store_persistence.py \
         tests/integration/test_artifact_store_minio.py \
         tests/integration/test_demo_artifact_store.py -q -W error
make check
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  pytest tests/integration -q -W error
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python3 scripts/check_boundaries.py
env PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH" \
  python3 scripts/scan_sensitive.py
git diff --check
```

Also run migration `0014 → 0013 → 0014`, inspect real DB forbidden columns/markers, inspect captured logs/stdout/stderr for injected endpoint/bucket/object-key/credential/body/DSN markers, verify UTF-8/no NUL, modes `100644`, no task-owned containers/processes/temporary files/AppleDouble.

- [ ] **Step 6: Final review, commit, push and exact CI**

Write `.superpowers/sdd/2026-08-13-artifact-store/task-4-report.md` with RED/GREEN, failure composition, real versions, command outputs, security inspection and remaining explicit non-goals. Stage only Task 4 files, cached diff/sensitive/mode clean. Commit:

```bash
git commit -m "docs(artifacts): add postgres minio acceptance demo"
git push origin codex/phase1-implementation
```

Wait for exact HEAD SHA CI `completed/success`, verify every step including Browser E2E, fetch remote, require `0 ahead / 0 behind` and clean tracked/index. Only then mark Artifact Store slice complete and begin the separate Campaign automatic sequence design.

---

## Plan Self-Review Checklist

- [ ] Design Sections 1–12 each map to at least one task and test.
- [ ] No task puts boto3 outside `connectors/object_store`.
- [ ] Raw and Generated contracts never collapse into one legacy interface.
- [ ] No test treats a fake/fixture/Docker/PATH failure as RED.
- [ ] Repository conflict handling uses typed ON CONFLICT result, not exception-string parsing.
- [ ] Cancellation and cleanup `BaseException` matrices are explicit.
- [ ] PostgreSQL forbidden content columns/markers and log/stdout/stderr leakage are queried, not inferred.
- [ ] Every task has focused RED/GREEN, full local gates, ordinary commit, push and exact SHA CI.
- [ ] No TODO/TBD/placeholders or undefined cross-task symbols remain.
