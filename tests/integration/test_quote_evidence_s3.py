"""临时MinIO真实put/有限读取与真实RawStore错hash拒绝。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from artifact_store.errors import ArtifactIntegrityError
from artifact_store.service_impl import RawArtifactStoreImpl
from artifact_store.store import RawArtifactKind
from artifact_store.transport import BlobReadLimitExceeded
from connectors.object_store.bounded import S3BoundedObjectBlobTransport
from connectors.object_store.s3 import S3ObjectBlobTransport
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from shared.schemas.evidence_read import ObjectReadLimits
from shared.schemas.identifiers import TenantId, new_id
from tests.integration.test_artifact_store_minio import (
    minio_runtime,  # noqa: F401 - 显式复用受控MinIO fixture
)


async def test_real_minio_bounded_integrity(integration_engine, minio_runtime):  # noqa: F811 - pytest显式fixture参数
    runtime = minio_runtime
    limits = ObjectReadLimits(
        connect_timeout_ms=1000,
        read_timeout_ms=1000,
        total_timeout_ms=5000,
        chunk_bytes=65536,
        maximum_attempts=1,
    )
    bounded = S3BoundedObjectBlobTransport(
        runtime.settings, runtime.secrets, limits=limits
    )
    old = S3ObjectBlobTransport(runtime.settings, runtime.secrets)
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    raw = RawArtifactStoreImpl(
        lambda tenant: SqlAlchemyArtifactUnitOfWork(sessions, tenant),
        old,
        2097152,
        lambda: datetime.now(UTC),
        new_id,
        bounded_transport=bounded,
    )
    tenant = TenantId(new_id("tn"))
    meta = await raw.put(
        tenant, RawArtifactKind.PDF, b"controlled-pdf", "application/pdf"
    )
    assert await raw.get_bounded(tenant, meta.artifact_id, maximum_bytes=2097152) == (
        meta,
        b"controlled-pdf",
    )
    key = f"raw/{tenant}/{meta.artifact_id}"
    with pytest.raises(BlobReadLimitExceeded):
        await bounded.get_bounded(key, maximum_bytes=2)
    await old.put(key, b"controlled-bad")
    with pytest.raises(ArtifactIntegrityError):
        await raw.get_bounded(tenant, meta.artifact_id, maximum_bytes=2097152)
