"""有界读取不得回退旧全量get，且元数据事务不得跨对象IO。"""

import pytest

from artifact_store import errors
from artifact_store.service_impl import RawArtifactStoreImpl
from artifact_store.store import RawArtifactKind
from artifact_store.transport import BlobObjectNotFoundError
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId, new_id
from tests.unit.test_artifact_store_service import (
    NOW,
    _MemoryDatabase,
    _Transport,
    _UnitOfWork,
)


class BoundedTransport(_Transport):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self.limits = []
        self.replacement = None

    async def get_bounded(self, key, *, maximum_bytes):
        assert self.db.events[-1] == "uow_exit"
        self.limits.append(maximum_bytes)
        if key not in self.objects:
            raise BlobObjectNotFoundError()
        data = self.objects[key] if self.replacement is None else self.replacement
        if len(data) > maximum_bytes:
            from artifact_store.transport import BlobReadLimitExceeded

            raise BlobReadLimitExceeded()
        return data


def case(bounded=True, maximum=32):
    db = _MemoryDatabase()
    transport = BoundedTransport(db)
    args = (
        lambda tenant: _UnitOfWork(db, tenant),
        transport,
        maximum,
        lambda: NOW,
        new_id,
    )
    store = RawArtifactStoreImpl(
        *args, **({"bounded_transport": transport} if bounded else {})
    )
    return store, transport


async def test_missing_bounded_transport_does_not_fall_back():
    store, transport = case(False)
    tenant = TenantId(new_id("tn"))
    meta = await store.put(tenant, RawArtifactKind.PDF, b"abc", "application/pdf")
    with pytest.raises(errors.ArtifactBoundedReadUnavailable):
        await store.get_bounded(tenant, meta.artifact_id, maximum_bytes=32)
    assert transport.events == ["put"]


async def test_metadata_precheck_and_actual_cap_are_bounded():
    store, transport = case()
    tenant = TenantId(new_id("tn"))
    meta = await store.put(tenant, RawArtifactKind.PDF, b"abc", "application/pdf")
    with pytest.raises(errors.ArtifactReadLimitExceeded):
        await store.get_bounded(tenant, meta.artifact_id, maximum_bytes=2)
    assert transport.limits == []
    assert await store.get_bounded(tenant, meta.artifact_id, maximum_bytes=64) == (
        meta,
        b"abc",
    )
    assert transport.limits == [3]
    with pytest.raises(errors.ArtifactNotFoundError):
        await store.get_bounded(
            TenantId(new_id("tn")), meta.artifact_id, maximum_bytes=32
        )
    assert transport.limits == [3]


@pytest.mark.parametrize(
    "changed,error_name",
    [
        (b"abcd", "ArtifactReadLimitExceeded"),
        (b"ab", "ArtifactIntegrityError"),
        (b"abd", "ArtifactIntegrityError"),
    ],
)
async def test_modified_objects_never_return_content(changed, error_name):
    store, transport = case()
    tenant = TenantId(new_id("tn"))
    meta = await store.put(tenant, RawArtifactKind.PDF, b"abc", "application/pdf")
    transport.replacement = changed
    with pytest.raises(getattr(errors, error_name)):
        await store.get_bounded(tenant, meta.artifact_id, maximum_bytes=32)


@pytest.mark.parametrize("value", [True, 0, -1, "2"])
async def test_bad_limit_fails_before_metadata_or_bytes(value):
    store, transport = case()
    with pytest.raises(ValidationError):
        await store.get_bounded(
            TenantId(new_id("tn")), new_id("art"), maximum_bytes=value
        )
    assert transport.db.events == [] and transport.limits == []
