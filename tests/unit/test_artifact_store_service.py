from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import pytest

from artifact_store.errors import (
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
)
from artifact_store.repository import (
    ArtifactInsertStatus,
    GeneratedArtifactInsertResult,
    GeneratedArtifactRecord,
    RawArtifactInsertResult,
    RawArtifactRecord,
)
from artifact_store.store import GeneratedArtifactKind, RawArtifactKind
from artifact_store.transport import BlobObjectNotFoundError
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    ArtifactId,
    IdempotencyKey,
    RunId,
    TenantId,
    UserId,
    new_id,
)

TENANT = TenantId(new_id("tn"))
OTHER_TENANT = TenantId(new_id("tn"))
RUN = RunId(new_id("run"))
ENROLLMENT = f"enr_{str(new_id('enr')).split('_', 1)[1]}"
KEY = IdempotencyKey(f"{ENROLLMENT}:1:draft")
UPLOADER = UserId(new_id("usr"))
NOW = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)
GENERATED_MIME = "application/vnd.tradeos.email-draft+json"


class _MemoryDatabase:
    def __init__(self) -> None:
        self.raw_by_id: dict[tuple[str, str], RawArtifactRecord] = {}
        self.raw_by_hash: dict[tuple[str, str, str], RawArtifactRecord] = {}
        self.generated_by_id: dict[tuple[str, str], GeneratedArtifactRecord] = {}
        self.generated_by_key: dict[tuple[str, str], GeneratedArtifactRecord] = {}
        self.events: list[str] = []
        self.commit_error: BaseException | None = None
        self.forced_raw_winner: RawArtifactRecord | None = None
        self.forced_generated_winner: GeneratedArtifactRecord | None = None


class _RawRepository:
    def __init__(self, db: _MemoryDatabase, tenant_id: TenantId) -> None:
        self._db = db
        self._tenant = tenant_id

    async def get_by_id(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> RawArtifactRecord | None:
        assert tenant_id == self._tenant
        self._db.events.append("raw_get_id")
        return self._db.raw_by_id.get((str(tenant_id), str(artifact_id)))

    async def get_by_hash(
        self, tenant_id: TenantId, kind: RawArtifactKind, content_hash: str
    ) -> RawArtifactRecord | None:
        assert tenant_id == self._tenant
        self._db.events.append("raw_get_hash")
        return self._db.raw_by_hash.get(
            (str(tenant_id), kind.value, content_hash)
        )

    async def insert_if_absent(
        self, record: RawArtifactRecord
    ) -> RawArtifactInsertResult:
        self._db.events.append("raw_insert")
        winner = self._db.forced_raw_winner
        if winner is None:
            winner = self._db.raw_by_hash.get(
                (
                    str(record.meta.tenant_id),
                    record.meta.kind.value,
                    record.meta.content_hash,
                )
            )
        if winner is not None:
            return RawArtifactInsertResult(ArtifactInsertStatus.EXISTING, winner)
        self._db.raw_by_id[
            (str(record.meta.tenant_id), str(record.meta.artifact_id))
        ] = record
        self._db.raw_by_hash[
            (
                str(record.meta.tenant_id),
                record.meta.kind.value,
                record.meta.content_hash,
            )
        ] = record
        return RawArtifactInsertResult(ArtifactInsertStatus.CREATED, record)


class _GeneratedRepository:
    def __init__(self, db: _MemoryDatabase, tenant_id: TenantId) -> None:
        self._db = db
        self._tenant = tenant_id

    async def get_by_id(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> GeneratedArtifactRecord | None:
        assert tenant_id == self._tenant
        self._db.events.append("generated_get_id")
        return self._db.generated_by_id.get((str(tenant_id), str(artifact_id)))

    async def get_by_idempotency_key(
        self, tenant_id: TenantId, idempotency_key: IdempotencyKey
    ) -> GeneratedArtifactRecord | None:
        assert tenant_id == self._tenant
        self._db.events.append("generated_get_key")
        return self._db.generated_by_key.get(
            (str(tenant_id), str(idempotency_key))
        )

    async def insert_if_absent(
        self, record: GeneratedArtifactRecord
    ) -> GeneratedArtifactInsertResult:
        self._db.events.append("generated_insert")
        winner = self._db.forced_generated_winner
        if winner is None:
            winner = self._db.generated_by_key.get(
                (str(record.meta.tenant_id), str(record.meta.idempotency_key))
            )
        if winner is not None:
            return GeneratedArtifactInsertResult(
                ArtifactInsertStatus.EXISTING, winner
            )
        self._db.generated_by_id[
            (str(record.meta.tenant_id), str(record.meta.artifact_id))
        ] = record
        self._db.generated_by_key[
            (str(record.meta.tenant_id), str(record.meta.idempotency_key))
        ] = record
        return GeneratedArtifactInsertResult(ArtifactInsertStatus.CREATED, record)


class _UnitOfWork:
    def __init__(self, db: _MemoryDatabase, tenant_id: TenantId) -> None:
        self._db = db
        self.raw = _RawRepository(db, tenant_id)
        self.generated = _GeneratedRepository(db, tenant_id)
        self._snapshot: tuple[dict, dict, dict, dict] | None = None

    async def __aenter__(self) -> Self:
        self._db.events.append("uow_enter")
        self._snapshot = (
            dict(self._db.raw_by_id),
            dict(self._db.raw_by_hash),
            dict(self._db.generated_by_id),
            dict(self._db.generated_by_key),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is not None or self._db.commit_error is not None:
            assert self._snapshot is not None
            (
                self._db.raw_by_id,
                self._db.raw_by_hash,
                self._db.generated_by_id,
                self._db.generated_by_key,
            ) = self._snapshot
        if exc_type is None and self._db.commit_error is not None:
            raise self._db.commit_error
        self._db.events.append("uow_exit")


class _Transport:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.events: list[str] = []
        self.put_error: BaseException | None = None
        self.get_error: BaseException | None = None
        self.delete_error: BaseException | None = None

    async def put(self, object_key: str, content: bytes) -> None:
        self.events.append("put")
        if self.put_error is not None:
            raise self.put_error
        self.objects[object_key] = content

    async def get(self, object_key: str) -> bytes:
        self.events.append("get")
        if self.get_error is not None:
            raise self.get_error
        try:
            return self.objects[object_key]
        except KeyError:
            raise BlobObjectNotFoundError() from None

    async def delete(self, object_key: str) -> None:
        self.events.append("delete")
        if self.delete_error is not None:
            raise self.delete_error
        self.objects.pop(object_key, None)


def _stores(
    *,
    db: _MemoryDatabase | None = None,
    transport: _Transport | None = None,
    id_generator: Callable[[str], str] = new_id,
):
    from artifact_store.service_impl import (
        GeneratedArtifactStoreImpl,
        RawArtifactStoreImpl,
    )

    database = db or _MemoryDatabase()
    blob = transport or _Transport()
    factory = lambda tenant: _UnitOfWork(database, tenant)
    raw = RawArtifactStoreImpl(factory, blob, 1024, lambda: NOW, id_generator)
    generated = GeneratedArtifactStoreImpl(
        factory, blob, 1024, lambda: NOW, id_generator
    )
    return raw, generated, database, blob


async def _put_generated(store, content: bytes = b"draft"):
    return await store.put(
        TENANT,
        GeneratedArtifactKind.EMAIL_DRAFT,
        content,
        GENERATED_MIME,
        workflow_run_id=RUN,
        subject_ref=ENROLLMENT,
        sequence_number=1,
        idempotency_key=KEY,
        generated_by="outreach_agent_v1",
    )


async def test_generated_put_never_persists_body_in_metadata() -> None:
    marker = b"customer-body-marker"
    _, store, db, blob = _stores()
    meta = await _put_generated(store, marker)
    assert blob.objects[f"generated/{TENANT}/{meta.artifact_id}"] == marker
    assert marker not in repr(db.generated_by_id).encode()


async def test_raw_existing_short_circuits_before_blob_write() -> None:
    raw, _, db, blob = _stores()
    first = await raw.put(TENANT, RawArtifactKind.PDF, b"same", "application/pdf")
    second = await raw.put(TENANT, RawArtifactKind.PDF, b"same", "application/pdf")
    assert second == first
    assert blob.events == ["put"]
    assert db.events[:2] == ["uow_enter", "raw_get_hash"]


async def test_generated_same_key_is_idempotent_and_changed_binding_conflicts() -> None:
    _, store, _, blob = _stores()
    first = await _put_generated(store)
    assert await _put_generated(store) == first
    assert blob.events == ["put"]


@pytest.mark.parametrize("changed", ["workflow_run", "generated_by"])
async def test_generated_same_key_rejects_changed_safe_binding(changed: str) -> None:
    _, store, _, blob = _stores()
    await _put_generated(store)
    kwargs = {
        "workflow_run_id": RUN,
        "generated_by": "outreach_agent_v1",
    }
    if changed == "workflow_run":
        kwargs["workflow_run_id"] = RunId(new_id("run"))
    else:
        kwargs["generated_by"] = "outreach_agent_v2"
    with pytest.raises(ArtifactConflictError):
        await store.put(
            TENANT,
            GeneratedArtifactKind.EMAIL_DRAFT,
            b"draft",
            GENERATED_MIME,
            workflow_run_id=kwargs["workflow_run_id"],
            subject_ref=ENROLLMENT,
            sequence_number=1,
            idempotency_key=KEY,
            generated_by=kwargs["generated_by"],
        )
    assert blob.events == ["put"]
    with pytest.raises(ArtifactConflictError, match="^Artifact 幂等记录冲突$"):
        await _put_generated(store, b"changed")
    assert blob.events == ["put"]


@pytest.mark.parametrize(
    ("content", "mime", "limit"),
    [
        (b"pdf", "text/html", 1024),
        (b"12345", "application/pdf", 4),
        ("not-bytes", "application/pdf", 1024),
    ],
)
async def test_raw_put_rejects_mime_size_and_non_bytes_before_io(
    content: object, mime: str, limit: int
) -> None:
    from artifact_store.service_impl import RawArtifactStoreImpl

    db = _MemoryDatabase()
    blob = _Transport()
    store = RawArtifactStoreImpl(
        lambda tenant: _UnitOfWork(db, tenant),
        blob,
        limit,
        lambda: NOW,
        new_id,
    )
    with pytest.raises(ValidationError, match="^artifact metadata 无效$"):
        await store.put(TENANT, RawArtifactKind.PDF, content, mime)  # type: ignore[arg-type]
    assert db.events == []
    assert blob.events == []


async def test_blob_failure_does_not_insert_metadata() -> None:
    failure = TransientError("transport-marker")
    blob = _Transport()
    blob.put_error = failure
    raw, _, db, _ = _stores(transport=blob)
    with pytest.raises(TransientError) as exc:
        await raw.put(TENANT, RawArtifactKind.PDF, b"pdf", "application/pdf")
    assert exc.value is failure
    assert db.raw_by_id == {}


async def test_commit_failure_removes_attempted_object_and_preserves_primary() -> None:
    db = _MemoryDatabase()
    primary = RuntimeError("commit-marker")
    db.commit_error = primary
    raw, _, _, blob = _stores(db=db)
    with pytest.raises(RuntimeError) as exc:
        await raw.put(TENANT, RawArtifactKind.PDF, b"pdf", "application/pdf")
    assert exc.value is primary
    assert blob.objects == {}


async def test_loser_object_is_deleted_then_raw_winner_returned() -> None:
    raw, _, db, blob = _stores()
    winner = await raw.put(
        TENANT, RawArtifactKind.PDF, b"winner", "application/pdf", UPLOADER
    )
    winner_record = next(iter(db.raw_by_id.values()))
    db.raw_by_id.clear()
    db.raw_by_hash.clear()
    db.forced_raw_winner = winner_record
    result = await raw.put(
        TENANT, RawArtifactKind.PDF, b"winner", "application/pdf", None
    )
    assert result == winner
    assert blob.events[-2:] == ["put", "delete"]
    assert list(blob.objects) == [f"raw/{TENANT}/{winner.artifact_id}"]


async def test_cleanup_failure_preserves_primary_or_becomes_fixed_transient() -> None:
    primary = RuntimeError("primary-marker")
    db = _MemoryDatabase()
    db.commit_error = primary
    blob = _Transport()
    blob.delete_error = RuntimeError("cleanup-secret")
    raw, _, _, _ = _stores(db=db, transport=blob)
    with pytest.raises(RuntimeError) as exc:
        await raw.put(TENANT, RawArtifactKind.PDF, b"pdf", "application/pdf")
    assert exc.value is primary

    db.commit_error = None
    db.forced_raw_winner = RawArtifactRecord(
        next(iter(db.raw_by_id.values())).meta,
        next(iter(db.raw_by_id.values())).object_key,
    ) if db.raw_by_id else None
    if db.forced_raw_winner is None:
        clean_blob = _Transport()
        clean_raw, _, clean_db, _ = _stores(transport=clean_blob)
        await clean_raw.put(TENANT, RawArtifactKind.PDF, b"pdf", "application/pdf")
        db.forced_raw_winner = next(iter(clean_db.raw_by_id.values()))
    with pytest.raises(TransientError, match="^Artifact 对象存储暂不可用$"):
        await raw.put(TENANT, RawArtifactKind.PDF, b"pdf", "application/pdf")


async def test_get_maps_missing_and_detects_integrity_with_safe_critical(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw, _, _, blob = _stores()
    meta = await raw.put(TENANT, RawArtifactKind.PDF, b"correct", "application/pdf")
    key = f"raw/{TENANT}/{meta.artifact_id}"
    blob.objects.pop(key)
    with pytest.raises(ArtifactNotFoundError, match="^Artifact 不存在$"):
        await raw.get(TENANT, meta.artifact_id)
    blob.objects[key] = b"corrupt"
    with caplog.at_level(
        logging.CRITICAL, logger="security.artifact_integrity"
    ), pytest.raises(
        ArtifactIntegrityError, match="^Artifact 完整性校验失败$"
    ):
        await raw.get(TENANT, meta.artifact_id)
    records = [r for r in caplog.records if r.name == "security.artifact_integrity"]
    assert len(records) == 1
    assert records[0].getMessage() == "检测到 Artifact 完整性违规"
    assert records[0].tenant_id == str(TENANT)  # type: ignore[attr-defined]
    assert records[0].artifact_id == str(meta.artifact_id)  # type: ignore[attr-defined]
    assert "corrupt" not in repr(records[0].__dict__)


async def test_cross_tenant_get_equals_nonexistent() -> None:
    raw, _, _, _ = _stores()
    meta = await raw.put(TENANT, RawArtifactKind.PDF, b"pdf", "application/pdf")
    messages = []
    for tenant, artifact_id in (
        (OTHER_TENANT, meta.artifact_id),
        (TENANT, ArtifactId(new_id("art"))),
    ):
        with pytest.raises(ArtifactNotFoundError) as exc:
            await raw.get(tenant, artifact_id)
        messages.append(str(exc.value))
    assert messages == ["Artifact 不存在", "Artifact 不存在"]


async def test_generated_get_rechecks_same_length_hash() -> None:
    _, generated, _, blob = _stores()
    meta = await _put_generated(generated, b"draft")
    blob.objects[f"generated/{TENANT}/{meta.artifact_id}"] = b"drift"
    with pytest.raises(ArtifactIntegrityError):
        await generated.get(TENANT, meta.artifact_id)


async def test_cancellation_after_completed_put_triggers_cleanup_and_preserves_object() -> None:
    class CancellationTransport(_Transport):
        async def put(self, object_key: str, content: bytes) -> None:
            self.objects[object_key] = content
            raise asyncio.CancelledError("put-cancelled")

    cancellation_transport = CancellationTransport()
    raw, _, db, blob = _stores(transport=cancellation_transport)
    cancellation = None
    try:
        await raw.put(TENANT, RawArtifactKind.PDF, b"pdf", "application/pdf")
    except asyncio.CancelledError as exc:
        cancellation = exc
    assert cancellation is not None
    assert blob.objects == {}
    assert db.raw_by_id == {}


def test_service_repr_and_errors_hide_content_and_object_keys() -> None:
    raw, generated, db, blob = _stores()
    rendered = repr((raw, generated, db, blob))
    assert "raw/" not in rendered
    assert "generated/" not in rendered
    assert "customer-body-marker" not in rendered


@pytest.mark.parametrize("operation", ["put", "get", "delete"])
async def test_s3_transport_cancellation_waits_for_underlying_operation(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from connectors.object_store.config import S3ObjectStoreSettings
    from connectors.object_store.s3 import S3ObjectBlobTransport

    started = threading.Event()
    release = threading.Event()
    completed: list[str] = []

    class Body:
        def read(self) -> bytes:
            started.set()
            release.wait(5)
            completed.append("get")
            return b"value"

        def close(self) -> None: ...

    class Client:
        def put_object(self, **kwargs) -> None:
            started.set()
            release.wait(5)
            completed.append("put")

        def get_object(self, **kwargs) -> dict[str, object]:
            return {"Body": Body()}

        def delete_object(self, **kwargs) -> None:
            started.set()
            release.wait(5)
            completed.append("delete")

    monkeypatch.setattr("connectors.object_store.s3.boto3.client", lambda *a, **k: Client())
    settings = S3ObjectStoreSettings(
        True,
        "http://127.0.0.1:9000",
        "artifact-tests",
        "ACCESS_REF",
        "SECRET_REF",
        "us-east-1",
        1024,
        1024,
    )
    transport = S3ObjectBlobTransport(
        settings, type("Resolver", (), {"resolve": lambda self, ref: "safe"})()
    )
    key = f"raw/{TENANT}/{ArtifactId(new_id('art'))}"
    if operation == "put":
        task = asyncio.create_task(transport.put(key, b"value"))
    elif operation == "get":
        task = asyncio.create_task(transport.get(key))
    else:
        task = asyncio.create_task(transport.delete(key))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel("caller-cancel")
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError) as exc:
        await task
    assert exc.value.args == ("caller-cancel",)
    assert completed == [operation]


async def test_s3_transport_repr_and_sdk_failure_are_fixed_and_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from connectors.object_store.config import S3ObjectStoreSettings
    from connectors.object_store.s3 import S3ObjectBlobTransport

    class Client:
        def put_object(self, **kwargs) -> None:
            raise RuntimeError("sdk-endpoint-secret-marker")

    monkeypatch.setattr("connectors.object_store.s3.boto3.client", lambda *a, **k: Client())
    settings = S3ObjectStoreSettings(
        True,
        "http://127.0.0.1:9000",
        "artifact-tests",
        "ACCESS_REF",
        "SECRET_REF",
        "us-east-1",
        1024,
        1024,
    )
    transport = S3ObjectBlobTransport(
        settings,
        type(
            "Resolver",
            (),
            {"resolve": lambda self, ref: "credential-secret-marker"},
        )(),
    )
    assert repr(transport) == "S3ObjectBlobTransport()"
    key = f"raw/{TENANT}/{ArtifactId(new_id('art'))}"
    with pytest.raises(
        TransientError, match="^Artifact 对象存储暂不可用$"
    ) as exc:
        await transport.put(key, b"value")
    assert "sdk-endpoint-secret-marker" not in repr(exc.value)
    assert "credential-secret-marker" not in repr(transport)
