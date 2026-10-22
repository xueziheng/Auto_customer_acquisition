"""Agent资料与人工入口分开，租户/快照/逐字证据/已有事实均失败关闭。"""
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from domains.prospecting.agent_profile import (
    AgentAccountProfilePatch,
    AgentProfileSnapshot,
)
from domains.prospecting.service_impl import ProspectingServiceImpl
from shared.errors import ValidationError
from shared.schemas.identifiers import ArtifactId, ProspectAccountId, TenantId

NOW = datetime(2026, 10, 22, tzinfo=UTC)
TENANT = TenantId("tenant-profile")
ACCOUNT = ProspectAccountId("account-profile")


@pytest.mark.parametrize("failure", [None, "tenant", "hash", "url", "quote", "domain", "conflict"])
async def test_agent_evidence_updates_only_matching_unconfirmed_facts(failure):
    from types import SimpleNamespace

    from domains.prospecting.errors import ProspectingConflictError

    account = SimpleNamespace(account_id=ACCOUNT, tenant_id=TENANT, name="Example", country="US", created_at=NOW, website_domain="wrong.test" if failure == "domain" else "company.test", entity_type=None, industry=None, size_hint=None, source_signal_refs=(), field_provenance={})
    if failure == "conflict":
        account.entity_type = "manufacturer"
    class Accounts:
        async def get_for_update(self, tenant, account_id):
            return account
        async def update_profile(self, value):
            assert value is account
    @asynccontextmanager
    async def uow(tenant):
        yield SimpleNamespace(accounts=Accounts())
    class Reader:
        async def read(self, tenant, artifact_id):
            return AgentProfileSnapshot(tenant_id="other" if failure == "tenant" else tenant, artifact_id=artifact_id,
                source_url="https://other.test" if failure == "url" else "https://company.test/about",
                page_hash="b" * 64 if failure == "hash" else "a" * 64, observed_at=NOW,
                text="We distribute industrial hardware.")
    patch = AgentAccountProfilePatch(entity_type={"value": "distributor", "quote": "Not on page" if failure == "quote" else "We distribute industrial hardware."},
        industry={"value": "hardware", "quote": "industrial hardware"}, artifact_id=ArtifactId("art-test"), source_url="https://company.test/about", page_hash="a" * 64, extracted_by="agent:codex/run", extracted_at=NOW)
    service = ProspectingServiceImpl(uow, object(), now=lambda: NOW)
    if failure:
        with pytest.raises((ValidationError, ProspectingConflictError)):
            await service.fill_account_profile_from_evidence(TENANT, ACCOUNT, patch, evidence=Reader())
        assert account.industry is None
    else:
        result = await service.fill_account_profile_from_evidence(TENANT, ACCOUNT, patch, evidence=Reader())
        again = await service.fill_account_profile_from_evidence(TENANT, ACCOUNT, patch, evidence=Reader())
        assert again == result
        assert result.entity_type == "distributor"
        assert result.field_provenance["entity_type"].confirmed_by is None
        assert result.field_provenance["entity_type"].extracted_by == "agent:codex/run"
        assert result.field_provenance["industry"].source_quote == "industrial hardware"


@pytest.mark.parametrize("failure", [None, "tenant", "hash", "text"])
async def test_archived_reader_checks_actual_bytes(failure):
    import hashlib
    from types import SimpleNamespace

    from apps.scheduler_worker.customer_profile_evidence import (
        ArchivedCustomerProfileEvidence,
    )
    from artifact_store.store import RawArtifactKind
    from connectors.web_search.client import PageSnapshot
    from shared.errors import PermissionDenied
    from shared.schemas.identifiers import new_id

    artifact = ArtifactId(new_id("art"))
    body = b"<p>We distribute industrial hardware.</p>"
    digest = hashlib.sha256(body).hexdigest()
    page = PageSnapshot("forged" if failure == "text" else "We distribute industrial hardware.", "https://company.test/about", NOW, digest, artifact)
    class Store:
        async def get_bounded(self, tenant, artifact_id, *, maximum_bytes):
            return SimpleNamespace(tenant_id=TenantId("other") if failure == "tenant" else tenant,
                artifact_id=artifact_id, kind=RawArtifactKind.WEB_SNAPSHOT,
                content_hash="b" * 64 if failure == "hash" else digest), body
    reader = ArchivedCustomerProfileEvidence(TENANT, page, Store())
    if failure:
        with pytest.raises((ValidationError, PermissionDenied)):
            await reader.read(TENANT, artifact)
    else:
        value = await reader.read(TENANT, artifact)
        assert value.text == page.text
        assert value.page_hash == digest
