"""平台模型库：复用平台授权、模型测试 Gateway 和独立公司绑定。"""
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.platform_model_api import PlatformModelApi
from connectors.codex.client import CodexRunnerSettings
from connectors.codex_agent.client import AgentTransport, codex_agent_provider
from connectors.obsidian.workspace import KnowledgeTaskScope
from domains.organization.data_sources import DataSourceUnavailable
from domains.organization.model_api import ModelApiCommand
from domains.organization.model_catalog import (
    ModelCatalogEntry,
    ModelCatalogSave,
    ModelCatalogView,
    ModelCompanyBinding,
    ModelCompanyUpdate,
)
from domains.organization.platform_access import PlatformAccessService
from infra.model_catalog import FileModelCatalog
from shared.authentication import AuthPrincipal
from shared.schemas.identifiers import TenantId, new_id
from tool_gateway.fingerprint import HmacFingerprintProvider


class PlatformModelCatalog:
    def __init__(self, tenant: TenantId, access: PlatformAccessService, store: FileModelCatalog, sessions: async_sessionmaker | None, fingerprints: HmacFingerprintProvider | None, runner_settings: CodexRunnerSettings | None = None) -> None:
        self.tenant, self.access, self.store = tenant, access, store
        self.sessions, self.fingerprints, self.runner = sessions, fingerprints, runner_settings
        self._probes = {}

    async def get(self, principal: AuthPrincipal) -> ModelCatalogView:
        overview = await self.access.overview(principal)
        bindings = self.store.bindings(self.tenant)
        companies = []
        for company in overview.enterprises:
            binding = bindings.get(str(company.tenant_id), {})
            companies.append(ModelCompanyBinding(tenant_id=str(company.tenant_id), name=company.name,
                enabled=company.enabled, runtime_available=company.available, revision=binding.get("revision", 0),
                model_id=binding.get("model_id"), configuration_version=binding.get("version"),
                model=binding.get("model"), configuration_name=binding.get("name")))
        return ModelCatalogView(models=self.store.entries(self.tenant), companies=tuple(companies))

    async def save(self, principal: AuthPrincipal, entry_id: str | None, command: ModelCatalogSave) -> ModelCatalogEntry:
        await self.access.authorize(principal)
        return self.store.save(self.tenant, str(principal.user_id), entry_id, command)

    async def delete(self, principal: AuthPrincipal, entry_id: str, command: ModelApiCommand) -> ModelCatalogView:
        await self.access.authorize(principal)
        self.store.delete(self.tenant, str(principal.user_id), entry_id, command.expected_revision)
        return await self.get(principal)

    async def bind(self, principal: AuthPrincipal, company_id: str, command: ModelCompanyUpdate) -> ModelCatalogView:
        current = await self.get(principal)
        company = next((c for c in current.companies if c.tenant_id == company_id and c.enabled), None)
        if company is None:
            raise DataSourceUnavailable("公司不存在或未启用")
        self.store.bind(self.tenant, str(principal.user_id), company_id, command.model_id, command.model_revision, command.expected_revision)
        return await self.get(principal)

    async def test(self, principal: AuthPrincipal, entry_id: str, command: ModelApiCommand, *, agent: bool = False) -> ModelCatalogEntry:
        await self.access.authorize(principal)
        current = self.store.get(self.tenant, entry_id)
        if agent and current.agent_tested_at and datetime.now(UTC)-current.agent_tested_at < timedelta(seconds=60) and current.revision == command.expected_revision:
            return current
        probe = self._probes.setdefault(entry_id, PlatformModelApi(self.tenant, self.access,
            self.store.model_store(entry_id), self.sessions, self.fingerprints))
        if agent:
            if self.runner is None:
                raise DataSourceUnavailable("Codex 运行环境尚未配置")
            scope = KnowledgeTaskScope(str(self.tenant), str(principal.user_id), new_id("probe"))
            def factory(config, resolver):
                return lambda: codex_agent_provider(AgentTransport("MODEL_API_KEY", resolver,
                    base_url=config.base_url, protocol=config.protocol, timeout_seconds=60), settings=self.runner, scope=scope)
            await probe.test(principal, command, factory_builder=factory, kind="agent",
                record_result=lambda revision, status, failure: self.store.record_agent_test(self.tenant, entry_id, revision, status, failure))
        else:
            await probe.test(principal, command)
        return self.store.get(self.tenant, entry_id)
