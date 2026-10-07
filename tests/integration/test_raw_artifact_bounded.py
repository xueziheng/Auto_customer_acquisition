"""真实PG原件metadata与受控对象；完整性、租户和事务边界。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from artifact_store.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactReadLimitExceeded,
)
from artifact_store.service_impl import RawArtifactStoreImpl
from artifact_store.store import RawArtifactKind
from artifact_store.transport import BlobReadLimitExceeded
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.quote_evidence_artifacts import RawQuoteEvidenceAdapter
from shared.schemas.evidence_read import QuoteEvidenceError
from shared.schemas.identifiers import TenantId, new_id


class Transport:
    def __init__(self, factory):
        self.factory = factory
        self.values = {}
        self.limits = []
        self.corruption = None

    async def put(self, key, content):
        self.values[key] = content

    async def delete(self, key):
        self.values.pop(key, None)

    async def get(self, key):
        pytest.fail("bounded来源不得回退旧get")

    async def get_bounded(self, key, *, maximum_bytes):
        # 同engine第二连接可取得原件锁，且读事务已经全部退出。
        async with self.factory() as session, session.begin():
            await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
            tenant, artifact = key.split("/")[1:]
            await session.execute(
                text(
                    "SELECT artifact_id FROM raw_artifacts WHERE tenant_id=:tenant AND artifact_id=:artifact FOR UPDATE NOWAIT"
                ),
                {"tenant": tenant, "artifact": artifact},
            )
            idle = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND state='idle in transaction' AND pid<>pg_backend_pid()"
                )
            )
            assert idle == 0
        self.limits.append(maximum_bytes)
        data = self.values[key] if self.corruption is None else self.corruption
        if len(data) > maximum_bytes:
            raise BlobReadLimitExceeded()
        return data


def case(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    transport = Transport(factory)
    store = RawArtifactStoreImpl(
        lambda tenant: SqlAlchemyArtifactUnitOfWork(factory, tenant),
        transport,
        2097152,
        lambda: datetime.now(UTC),
        new_id,
        bounded_transport=transport,
    )
    return store, transport


async def test_pg_metadata_scope_integrity_and_transaction_exit(integration_engine):
    store, transport = case(integration_engine)
    tenant = TenantId(new_id("tn"))
    meta = await store.put(
        tenant, RawArtifactKind.PDF, b"controlled", "application/pdf"
    )
    with pytest.raises(ArtifactReadLimitExceeded):
        await store.get_bounded(tenant, meta.artifact_id, maximum_bytes=3)
    with pytest.raises(ArtifactNotFoundError):
        await store.get_bounded(
            TenantId(new_id("tn")), meta.artifact_id, maximum_bytes=2097152
        )
    assert transport.limits == []
    assert await store.get_bounded(tenant, meta.artifact_id, maximum_bytes=2097152) == (
        meta,
        b"controlled",
    )
    assert transport.limits == [10]
    for bad, failure in [
        (b"controlled-more", ArtifactReadLimitExceeded),
        (b"controllEd", ArtifactIntegrityError),
        (b"short", ArtifactIntegrityError),
    ]:
        transport.corruption = bad
        with pytest.raises(failure):
            await store.get_bounded(tenant, meta.artifact_id, maximum_bytes=2097152)


async def test_raw_adapter_maps_real_meta_and_refuses_unsupported(integration_engine):
    store, transport = case(integration_engine)
    tenant = TenantId(new_id("tn"))
    adapter = RawQuoteEvidenceAdapter(store)
    meta = await store.put(
        tenant, RawArtifactKind.EMAIL_RAW, b"controlled-email", "message/rfc822"
    )
    mapped = await adapter.get_meta(tenant, meta.artifact_id)
    assert mapped.observed_at == meta.uploaded_at and mapped.kind == "email_raw"
    assert transport.limits == []
    read = await adapter.read(tenant, meta.artifact_id, maximum_bytes=2097152)
    assert read.content == b"controlled-email" and "content" not in read.model_dump()
    unsupported = await store.put(tenant, RawArtifactKind.IMAGE, b"image", "image/png")
    with pytest.raises(QuoteEvidenceError) as caught:
        await adapter.get_meta(tenant, unsupported.artifact_id)
    assert caught.value.code == "source_unsupported"
    with pytest.raises(QuoteEvidenceError) as caught:
        await adapter.get_meta(TenantId(new_id("tn")), meta.artifact_id)
    assert caught.value.code == "permission_denied"
