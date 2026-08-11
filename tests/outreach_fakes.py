"""触达服务行为测试共用的轻量事务 fake；不复制领域规则。"""

from __future__ import annotations

import copy
import importlib
from dataclasses import dataclass, field
from datetime import date

from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    SendingIdentityEligibilitySnapshot,
)
from shared.errors import PermissionDenied
from shared.schemas.identifiers import CampaignId, SendingIdentityId, TenantId

_models = importlib.import_module("domains.outreach.models")
ActionRecord = _models.ActionRecord
Campaign = _models.Campaign
CampaignVersion = _models.CampaignVersion
DailyQuotaUsage = _models.DailyQuotaUsage


@dataclass
class Trace:
    calls: list[tuple[object, ...]] = field(default_factory=list)


@dataclass
class FakeStore:
    campaigns: dict[CampaignId, Campaign] = field(default_factory=dict)
    versions: dict[tuple[CampaignId, int], CampaignVersion] = field(default_factory=dict)
    actions: dict[str, ActionRecord] = field(default_factory=dict)
    quotas: dict[tuple[CampaignId, date], tuple[int, int]] = field(default_factory=dict)


class FakeAudit:
    def __init__(self, trace: Trace) -> None:
        self.trace = trace
        self.records: list[dict[str, str]] = []

    def log(self, *, actor: str, action: str, tenant_id: TenantId, scope: str, rule: str) -> None:
        self.trace.calls.append(("audit", action, rule))
        self.records.append(
            {"actor": actor, "action": action, "tenant_id": str(tenant_id), "scope": scope, "rule": rule}
        )


class FakeAuthorizer:
    def __init__(self, trace: Trace, *, preauthorize_error: Exception | None = None) -> None:
        self.trace = trace
        self.preauthorize_error = preauthorize_error

    def preauthorize(self, actor, action, scope, tenant_id):
        self.trace.calls.append(("preauthorize", action.value))
        if self.preauthorize_error is not None:
            raise self.preauthorize_error
        return f"pre:{action.value}"

    def require(self, actor, action, scope, tenant_id, **resources):
        self.trace.calls.append(("require", action.value, resources))
        return f"allow:{action.value}"


class FakeCampaignRepository:
    def __init__(self, store: FakeStore, tenant_id: TenantId, trace: Trace) -> None:
        self.store, self.tenant_id, self.trace = store, tenant_id, trace

    async def add(self, campaign: Campaign, version: CampaignVersion) -> None:
        self.trace.calls.append(("campaign_add", campaign.campaign_id))
        self.store.campaigns[campaign.campaign_id] = copy.deepcopy(campaign)
        self.store.versions[(campaign.campaign_id, version.version)] = copy.deepcopy(version)

    async def get(self, tenant_id, campaign_id):
        value = self.store.campaigns.get(campaign_id)
        return copy.deepcopy(value) if value is not None and tenant_id == self.tenant_id else None

    async def get_for_update(self, tenant_id, campaign_id):
        self.trace.calls.append(("campaign_lock", campaign_id))
        return await self.get(tenant_id, campaign_id)

    async def get_version(self, tenant_id, campaign_id, version):
        value = self.store.versions.get((campaign_id, version))
        return copy.deepcopy(value) if value is not None and tenant_id == self.tenant_id else None

    async def append_version(self, version):
        self.store.versions[(version.campaign_id, version.version)] = copy.deepcopy(version)

    async def update(self, campaign):
        self.store.campaigns[campaign.campaign_id] = copy.deepcopy(campaign)

    async def list_scoped(self, tenant_id, scope, limit):
        values = [copy.deepcopy(v) for v in self.store.campaigns.values() if v.tenant_id == tenant_id]
        return sorted(values, key=lambda value: (value.created_at, value.campaign_id))[:limit]


class FakeQuotaRepository:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def get_usage(self, tenant_id, campaign_id, on_day):
        new_contacts, messages = self.store.quotas.get((campaign_id, on_day), (0, 0))
        return DailyQuotaUsage(tenant_id, campaign_id, on_day, new_contacts, messages)


class FakeActionRepository:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def append(self, action: ActionRecord) -> bool:
        if action.action_key in self.store.actions:
            return False
        self.store.actions[action.action_key] = copy.deepcopy(action)
        return True


class FakeBus:
    async def publish(self, event) -> None:
        return None


class FakeUow:
    def __init__(self, store: FakeStore, tenant_id: TenantId, trace: Trace) -> None:
        self.store, self.tenant_id, self.trace = store, tenant_id, trace
        self.campaigns = FakeCampaignRepository(store, tenant_id, trace)
        self.quotas = FakeQuotaRepository(store)
        self.actions = FakeActionRepository(store)
        self.bus = FakeBus()

    async def __aenter__(self):
        self.trace.calls.append(("uow_enter",))
        self._snapshot = copy.deepcopy(self.store)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.store.campaigns = self._snapshot.campaigns
            self.store.versions = self._snapshot.versions
            self.store.actions = self._snapshot.actions
            self.store.quotas = self._snapshot.quotas
        self.trace.calls.append(("uow_exit", exc_type))


class FakeUowFactory:
    def __init__(self, store: FakeStore, trace: Trace) -> None:
        self.store, self.trace = store, trace

    def __call__(self, tenant_id: TenantId) -> FakeUow:
        return FakeUow(self.store, tenant_id, self.trace)


class FakeSenders:
    def __init__(self, snapshots: dict[SendingIdentityId, SendingIdentityEligibilitySnapshot], trace: Trace) -> None:
        self.snapshots, self.trace = snapshots, trace

    async def get_sending_identity_eligibility(self, tenant_id, identity_id):
        self.trace.calls.append(("sender", identity_id))
        return self.snapshots[identity_id]


class FakeApprovals:
    def __init__(self, trace: Trace) -> None:
        self.trace = trace
        self.values: dict[tuple[CampaignId, int], CampaignApprovalSnapshot] = {}

    async def get_campaign_approval(self, tenant_id, campaign_id, version):
        self.trace.calls.append(("approval", campaign_id, version))
        return self.values.get((campaign_id, version))


class UnusedProvider:
    def __getattr__(self, name):
        async def _unused(*args, **kwargs):
            raise AssertionError(f"不应调用 provider: {name}")

        return _unused


def denied_authorizer(trace: Trace) -> FakeAuthorizer:
    return FakeAuthorizer(trace, preauthorize_error=PermissionDenied("拒绝"))
