from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from artifact_store.store import (
    GeneratedArtifactKind,
    GeneratedArtifactMeta,
    RawArtifactKind,
    RawArtifactMeta,
)
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import (
    ArtifactId,
    IdempotencyKey,
    RunId,
    TenantId,
    UserId,
    new_id,
)

NOW = datetime(2026, 8, 13, 6, 0, tzinfo=UTC)
GENERATED_MIME = "application/vnd.tradeos.email-draft+json"


@pytest_asyncio.fixture
async def artifact_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _raw_record(
    tenant_id: TenantId,
    *,
    artifact_id: ArtifactId | None = None,
    content_hash: str = "a" * 64,
) -> Any:
    from artifact_store.repository import RawArtifactRecord

    artifact_id = artifact_id or ArtifactId(new_id("art"))
    return RawArtifactRecord(
        RawArtifactMeta(
            tenant_id,
            artifact_id,
            RawArtifactKind.PDF,
            content_hash,
            32,
            "application/pdf",
            UserId(new_id("usr")),
            NOW,
        ),
        f"raw/{tenant_id}/{artifact_id}",
    )


def _generated_record(
    tenant_id: TenantId,
    *,
    artifact_id: ArtifactId | None = None,
    content_hash: str = "b" * 64,
    sequence_number: int = 1,
) -> Any:
    from artifact_store.repository import GeneratedArtifactRecord

    artifact_id = artifact_id or ArtifactId(new_id("art"))
    subject_ref = f"enr_{str(new_id('enr')).split('_', 1)[1]}"
    return GeneratedArtifactRecord(
        GeneratedArtifactMeta(
            tenant_id=tenant_id,
            artifact_id=artifact_id,
            kind=GeneratedArtifactKind.EMAIL_DRAFT,
            content_hash=content_hash,
            size_bytes=64,
            mime_type=GENERATED_MIME,
            workflow_run_id=RunId(new_id("run")),
            subject_ref=subject_ref,
            sequence_number=sequence_number,
            idempotency_key=IdempotencyKey(
                f"{subject_ref}:{sequence_number}:draft"
            ),
            generated_by="outreach_agent_v1",
            generated_at=NOW,
        ),
        f"generated/{tenant_id}/{artifact_id}",
    )


async def test_raw_repository_is_tenant_bound_and_idempotent(
    artifact_engine: AsyncEngine,
) -> None:
    from artifact_store.repository import ArtifactInsertStatus
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork

    factory = async_sessionmaker(artifact_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    record = _raw_record(tenant)
    async with SqlAlchemyArtifactUnitOfWork(factory, tenant) as uow:
        first = await uow.raw.insert_if_absent(record)
    async with SqlAlchemyArtifactUnitOfWork(factory, tenant) as uow:
        second = await uow.raw.insert_if_absent(record)
    assert first.status is ArtifactInsertStatus.CREATED
    assert second.status is ArtifactInsertStatus.EXISTING
    assert second.winner == first.winner == record


async def test_raw_repository_gets_by_id_and_hash_without_cross_tenant_leak(
    artifact_engine: AsyncEngine,
) -> None:
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork

    factory = async_sessionmaker(artifact_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    other = TenantId(new_id("tn"))
    record = _raw_record(tenant)
    async with SqlAlchemyArtifactUnitOfWork(factory, tenant) as uow:
        await uow.raw.insert_if_absent(record)
    async with SqlAlchemyArtifactUnitOfWork(factory, tenant) as uow:
        assert await uow.raw.get_by_id(tenant, record.meta.artifact_id) == record
        assert (
            await uow.raw.get_by_hash(
                tenant, RawArtifactKind.PDF, record.meta.content_hash
            )
            == record
        )
    async with SqlAlchemyArtifactUnitOfWork(factory, other) as uow:
        assert await uow.raw.get_by_id(other, record.meta.artifact_id) is None
        assert (
            await uow.raw.get_by_hash(
                other, RawArtifactKind.PDF, record.meta.content_hash
            )
            is None
        )


async def test_generated_repository_returns_canonical_existing_winner(
    artifact_engine: AsyncEngine,
) -> None:
    from artifact_store.repository import ArtifactInsertStatus
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork

    factory = async_sessionmaker(artifact_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    winner = _generated_record(tenant)
    conflicting = _generated_record(
        tenant,
        content_hash="c" * 64,
        sequence_number=winner.meta.sequence_number,
    )
    conflicting = type(conflicting)(
        type(conflicting.meta)(
            **{
                **conflicting.meta.__dict__,
                "subject_ref": winner.meta.subject_ref,
                "idempotency_key": winner.meta.idempotency_key,
            }
        ),
        conflicting.object_key,
    )
    async with SqlAlchemyArtifactUnitOfWork(factory, tenant) as uow:
        first = await uow.generated.insert_if_absent(winner)
    async with SqlAlchemyArtifactUnitOfWork(factory, tenant) as uow:
        same = await uow.generated.insert_if_absent(winner)
    async with SqlAlchemyArtifactUnitOfWork(factory, tenant) as uow:
        conflict = await uow.generated.insert_if_absent(conflicting)
    assert first.status is ArtifactInsertStatus.CREATED
    assert same.status is ArtifactInsertStatus.EXISTING
    assert same.winner == winner
    assert conflict.status is ArtifactInsertStatus.EXISTING
    assert conflict.winner == winner


async def test_generated_repository_gets_by_id_and_idempotency_key(
    artifact_engine: AsyncEngine,
) -> None:
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork

    factory = async_sessionmaker(artifact_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    other = TenantId(new_id("tn"))
    record = _generated_record(tenant)
    async with SqlAlchemyArtifactUnitOfWork(factory, tenant) as uow:
        await uow.generated.insert_if_absent(record)
    async with SqlAlchemyArtifactUnitOfWork(factory, tenant) as uow:
        assert await uow.generated.get_by_id(tenant, record.meta.artifact_id) == record
        assert (
            await uow.generated.get_by_idempotency_key(
                tenant, record.meta.idempotency_key
            )
            == record
        )
    async with SqlAlchemyArtifactUnitOfWork(factory, other) as uow:
        assert await uow.generated.get_by_id(other, record.meta.artifact_id) is None
        assert (
            await uow.generated.get_by_idempotency_key(
                other, record.meta.idempotency_key
            )
            is None
        )


async def test_repository_write_tenant_mismatch_is_typed_and_critical(
    artifact_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork

    factory = async_sessionmaker(artifact_engine, expire_on_commit=False)
    bound = TenantId(new_id("tn"))
    other = TenantId(new_id("tn"))
    with caplog.at_level(
        logging.CRITICAL, logger="security.tenant_isolation"
    ), pytest.raises(TenantIsolationViolation, match="^跨租户数据隔离违规$"):
        async with SqlAlchemyArtifactUnitOfWork(factory, bound) as uow:
            await uow.raw.insert_if_absent(_raw_record(other))
    records = [
        record
        for record in caplog.records
        if record.name == "security.tenant_isolation"
    ]
    assert len(records) == 1
    assert records[0].getMessage() == "检测到跨租户数据隔离违规"
    assert records[0].levelno == logging.CRITICAL
    assert records[0].tenant_id == str(bound)  # type: ignore[attr-defined]
    assert records[0].action == "artifact_raw_insert"  # type: ignore[attr-defined]


async def test_repository_method_tenant_mismatch_is_typed_and_critical(
    artifact_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork

    factory = async_sessionmaker(artifact_engine, expire_on_commit=False)
    bound = TenantId(new_id("tn"))
    other = TenantId(new_id("tn"))
    record = _raw_record(bound)
    async with SqlAlchemyArtifactUnitOfWork(factory, bound) as uow:
        await uow.raw.insert_if_absent(record)
    with caplog.at_level(
        logging.CRITICAL, logger="security.tenant_isolation"
    ), pytest.raises(TenantIsolationViolation):
        async with SqlAlchemyArtifactUnitOfWork(factory, bound) as uow:
            await uow.raw.get_by_id(other, record.meta.artifact_id)
    records = [
        record
        for record in caplog.records
        if record.name == "security.tenant_isolation"
    ]
    assert len(records) == 1
    assert records[0].action == "artifact_raw_get"  # type: ignore[attr-defined]


async def test_uow_commit_failure_rolls_back_uncommitted_metadata(
    artifact_engine: AsyncEngine,
) -> None:
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
    from infra.db.tables import RawArtifactRow

    class CommitFailureSession(AsyncSession):
        rolled_back = False

        async def commit(self) -> None:
            raise RuntimeError("commit-marker")

        async def rollback(self) -> None:
            type(self).rolled_back = True
            await super().rollback()

    factory = async_sessionmaker(
        artifact_engine,
        expire_on_commit=False,
        class_=CommitFailureSession,
    )
    tenant = TenantId(new_id("tn"))
    record = _raw_record(tenant)
    with pytest.raises(RuntimeError, match="^commit-marker$"):
        async with SqlAlchemyArtifactUnitOfWork(factory, tenant) as uow:
            await uow.raw.insert_if_absent(record)
    assert CommitFailureSession.rolled_back is True
    async with AsyncSession(artifact_engine) as session:
        stored = await session.scalar(
            select(RawArtifactRow).where(
                RawArtifactRow.tenant_id == str(tenant),
                RawArtifactRow.artifact_id == str(record.meta.artifact_id),
            )
        )
    assert stored is None


@pytest.mark.parametrize(
    ("primary", "rollback_error", "close_error"),
    [
        (RuntimeError("body-primary"), BaseException("rollback-secondary"), None),
        (RuntimeError("body-primary"), None, BaseException("close-secondary")),
        (
            RuntimeError("commit-primary"),
            BaseException("rollback-secondary"),
            BaseException("close-secondary"),
        ),
    ],
)
async def test_uow_cleanup_baseexception_never_replaces_primary(
    primary: RuntimeError,
    rollback_error: BaseException | None,
    close_error: BaseException | None,
) -> None:
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork

    class CleanupSession:
        async def commit(self) -> None:
            if str(primary) == "commit-primary":
                raise primary

        async def rollback(self) -> None:
            if rollback_error is not None:
                raise rollback_error

        async def close(self) -> None:
            if close_error is not None:
                raise close_error

    uow = SqlAlchemyArtifactUnitOfWork(lambda: CleanupSession(), TenantId(new_id("tn")))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError) as exc:
        async with uow:
            if str(primary) == "body-primary":
                raise primary
    assert exc.value is primary


async def test_uow_lone_close_cancellation_propagates() -> None:
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork

    cancellation = BaseException("close-cancellation")

    class CloseFailureSession:
        async def commit(self) -> None: ...

        async def rollback(self) -> None: ...

        async def close(self) -> None:
            raise cancellation

    uow = SqlAlchemyArtifactUnitOfWork(
        lambda: CloseFailureSession(), TenantId(new_id("tn"))  # type: ignore[arg-type]
    )
    with pytest.raises(BaseException) as exc:
        async with uow:
            pass
    assert exc.value is cancellation


def test_repositories_expose_no_public_mutation_or_cross_tenant_listing() -> None:
    from artifact_store.repository import (
        GeneratedArtifactRepository,
        RawArtifactRepository,
    )

    forbidden = {"update", "delete", "list", "list_all", "unsafe_cross_tenant_query"}
    assert forbidden.isdisjoint(vars(RawArtifactRepository))
    assert forbidden.isdisjoint(vars(GeneratedArtifactRepository))
