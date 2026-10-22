"""真实租户Artifact与Account持久化验证，模型不能自报正文替代原件。"""
from datetime import timedelta

import pytest

from apps.scheduler_worker.customer_profile_evidence import (
    ArchivedCustomerProfileEvidence,
)
from artifact_store.service_impl import RawArtifactStoreImpl
from artifact_store.store import RawArtifactKind
from connectors.web_search.client import PageSnapshot
from domains.prospecting.agent_profile import AgentAccountProfilePatch
from domains.prospecting.service_impl import ProspectingServiceImpl
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork
from shared.schemas.identifiers import TenantId, new_id
from tests.integration.test_customer_preparation import NOW, TestHasher, setup

pytest_plugins = ("tests.integration.test_customer_assignment_bridge",)


class Objects:
    def __init__(self):
        self.values = {}
    async def put(self, key, content):
        self.values[key] = content
    async def get(self, key):
        return self.values[key]
    async def get_bounded(self, key, *, maximum_bytes):
        value = self.values[key]
        assert len(value) <= maximum_bytes
        return value
    async def delete(self, key):
        self.values.pop(key, None)


@pytest.mark.parametrize("other_tenant", [False, True])
async def test_actual_artifact_profile_roundtrip(bridge_inventory, other_tenant):
    _, _, allocation, _ = await setup(bridge_inventory)
    _, _, factory, tenant, *_ = bridge_inventory
    objects = Objects()
    store = RawArtifactStoreImpl(lambda t: SqlAlchemyArtifactUnitOfWork(factory, t), objects, 2097152, lambda: NOW, new_id, bounded_transport=objects)
    artifact_tenant = TenantId(new_id("tn")) if other_tenant else tenant
    text = "We distribute industrial hardware."
    meta = await store.put(artifact_tenant, RawArtifactKind.WEB_SNAPSHOT, f"<p>{text}</p>".encode(), "text/html")
    account = await ProspectingServiceImpl(lambda t: SqlAlchemyProspectingUnitOfWork(factory, t), TestHasher()).get_account(tenant, allocation.account_id)
    snapshot = PageSnapshot(text, f"https://{account.website_domain}/about", NOW, meta.content_hash, meta.artifact_id)
    reader = ArchivedCustomerProfileEvidence(tenant, snapshot, store)
    prospecting = ProspectingServiceImpl(lambda t: SqlAlchemyProspectingUnitOfWork(factory, t), TestHasher(), now=lambda: NOW + timedelta(seconds=1))
    patch = AgentAccountProfilePatch(entity_type={"value": "distributor", "quote": text}, industry={"value": "hardware", "quote": "industrial hardware"}, artifact_id=meta.artifact_id, source_url=snapshot.url, page_hash=meta.content_hash, extracted_by="agent:codex/test", extracted_at=NOW)
    if other_tenant:
        from artifact_store.errors import ArtifactNotFoundError
        with pytest.raises(ArtifactNotFoundError):
            await prospecting.fill_account_profile_from_evidence(tenant, allocation.account_id, patch, evidence=reader)
        assert (await prospecting.get_account(tenant, allocation.account_id)).entity_type is None
    else:
        result = await prospecting.fill_account_profile_from_evidence(tenant, allocation.account_id, patch, evidence=reader)
        again = await prospecting.fill_account_profile_from_evidence(tenant, allocation.account_id, patch, evidence=reader)
        assert result == again
        assert result.entity_type == "distributor"
        assert result.field_provenance["entity_type"].confirmed_by is None
        assert result.field_provenance["entity_type"].source_id == meta.artifact_id
