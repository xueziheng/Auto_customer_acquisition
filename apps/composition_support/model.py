"""ADR0070：只装配显式模型端口，不读取配置环境或判断业务权限。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from connectors.codex_agent.discovery import DiscoverySession
from connectors.deepseek.client import DeepSeekClient
from connectors.gmail.client import SecretResolver
from connectors.model_api.client import CompatibleModelClient
from infra.model_api import ModelKeyFileResolver
from infra.standalone.settings import StandaloneModelSettings
from infra.tenant_model import resolve_task_model
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationError,
    ModelGenerationPort,
    ModelRequest,
    ModelResponse,
)
from shared.schemas.qq_trial import QQProviderRoundBudget
from tool_gateway.checks.model import CurrentModelAuthority
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.model_generate import GatewayModelGenerator, ModelProvider
from tool_gateway.model_usage import ModelUsageRepository
from tool_gateway.qq_trial_model import (
    TrialMessageGenerators,
    TrialModelBudget,
    TrialModelPorts,
    TrialProviderBudget,
)
from tool_gateway.repository import ToolGatewayUnitOfWorkFactory


@dataclass(frozen=True)
class ModelComposition:
    generator: ModelGenerationPort

    async def aclose(self) -> None:
        """每次调用的 Connector 已在 handler finally 关闭，没有共享 SDK。"""


def build_model_composition(
    *,
    settings: StandaloneModelSettings,
    resolver: SecretResolver,
    authority: CurrentModelAuthority,
    usage: ModelUsageRepository,
    ledger_factory: ToolGatewayUnitOfWorkFactory,
    fingerprints: HmacFingerprintProvider,
    lease_owner: str,
    lease_duration: timedelta,
    provider_factory: Callable[[], ModelProvider] | None = None,
    trial_gate: TrialModelBudget | None = None,
    trial_provider_factory: Callable[
        [TrialProviderBudget, InvocationIdentity], ModelProvider
    ]
    | None = None,
) -> ModelComposition:
    if trial_gate is not None:
        if provider_factory is not None:
            raise ModelGenerationError("configuration")

        def trial_provider(
            budget: TrialProviderBudget, identity: InvocationIdentity
        ) -> ModelProvider:
            selected = resolve_task_model(settings, identity)
            if (
                selected.model != trial_gate.scope.model
                or selected.configuration_version
                != trial_gate.scope.model_configuration_version
            ):
                raise ModelGenerationError("configuration")
            bounded = selected.model_copy(
                update={
                    "limits": selected.limits.model_copy(
                        update={
                            "timeout_seconds": min(
                                selected.limits.timeout_seconds,
                                trial_gate.scope.limits.model_timeout_seconds,
                            ),
                            "max_input_bytes": min(
                                selected.limits.max_input_bytes,
                                trial_gate.scope.limits.model_input_bytes,
                            ),
                            "max_output_tokens": min(
                                selected.limits.max_output_tokens,
                                trial_gate.scope.limits.model_output_tokens,
                            ),
                        }
                    )
                }
            )
            return (
                trial_provider_factory(budget, identity)
                if trial_provider_factory is not None
                else configured_codex_provider(bounded, identity, round_budget=budget)
            )

        return ModelComposition(
            TrialMessageGenerators(
                trial_gate,
                TrialModelPorts(
                    authority,
                    usage,
                    ledger_factory,
                    fingerprints,
                    settings.limits,
                    lease_owner,
                    lease_duration,
                    settings.provider,
                ),
                trial_provider,
            )
        )
    if trial_provider_factory is not None:
        raise ModelGenerationError("configuration")
    if settings.model_catalog_directory is not None and provider_factory is None:

        def generator(selected, identity):
            return GatewayModelGenerator(
                authority=authority,
                usage=usage,
                provider_factory=lambda: configured_codex_provider(selected, identity),
                limits=selected.limits,
                model=selected.model,
                provider=selected.provider,
                configuration_version=selected.configuration_version,
                ledger_factory=ledger_factory,
                fingerprints=fingerprints,
                lease_owner=lease_owner,
                lease_duration=lease_duration,
            )

        return ModelComposition(TenantModelGenerator(settings, authority, generator))
    return ModelComposition(
        GatewayModelGenerator(
            authority=authority,
            usage=usage,
            provider_factory=provider_factory
            or (lambda: configured_model_provider(settings, resolver)),
            limits=settings.limits,
            model=settings.model,
            provider=settings.provider,
            configuration_version=settings.configuration_version,
            ledger_factory=ledger_factory,
            fingerprints=fingerprints,
            lease_owner=lease_owner,
            lease_duration=lease_duration,
        )
    )


def configured_model_provider(
    settings: StandaloneModelSettings, resolver: SecretResolver
) -> ModelProvider:
    """显式部署版本决定模型与凭证，不改变 Gateway 授权和限额。"""
    if settings.api_key_file is not None:
        if settings.api_control_tenant is None:
            raise ModelGenerationError("configuration")
        resolver = ModelKeyFileResolver(
            settings.api_key_file,
            settings.api_control_tenant,
            (settings.api_connection_version or settings.configuration_version),
            settings.secret_ref,
        )
    if (
        settings.api_key_file is None
        and settings.provider == "deepseek"
        and settings.api_protocol == "responses"
        and settings.api_base_url == "https://api.deepseek.com"
    ):
        return DeepSeekClient(
            settings.secret_ref,
            resolver,
            timeout_seconds=settings.limits.timeout_seconds,
        )
    return CompatibleModelClient(
        settings.secret_ref,
        resolver,
        base_url=settings.api_base_url,
        protocol=settings.api_protocol,
        timeout_seconds=settings.limits.timeout_seconds,
    )


class TenantModelGenerator:
    """每次从受信身份解析同一任务的连接快照，再进入原 Gateway。"""

    def __init__(
        self,
        settings: StandaloneModelSettings,
        authority: CurrentModelAuthority,
        factory: Callable[
            [StandaloneModelSettings, InvocationIdentity], ModelGenerationPort
        ],
    ) -> None:
        self._settings, self._authority, self._factory = settings, authority, factory

    async def generate(
        self, identity: InvocationIdentity, request: ModelRequest
    ) -> ModelResponse:
        await self._authority.check(identity)
        selected = resolve_task_model(self._settings, identity)
        return await self._factory(selected, identity).generate(
            identity, request.model_copy(update={"model": selected.model})
        )


def configured_codex_provider(
    settings: StandaloneModelSettings,
    identity: InvocationIdentity,
    *,
    discovery: DiscoverySession | None = None,
    round_budget: QQProviderRoundBudget | None = None,
) -> ModelProvider:
    from connectors.codex.client import CodexRunnerSettings
    from connectors.codex_agent.client import AgentTransport, codex_agent_provider
    from connectors.obsidian.workspace import KnowledgeTaskScope

    if (
        settings.api_key_file is None
        or settings.api_control_tenant is None
        or settings.codex_job_root is None
    ):
        raise ModelGenerationError("configuration")
    resolver = ModelKeyFileResolver(
        settings.api_key_file,
        settings.api_control_tenant,
        settings.api_connection_version or settings.configuration_version,
        "MODEL_API_KEY",
    )
    return codex_agent_provider(
        AgentTransport(
            "MODEL_API_KEY",
            resolver,
            base_url=settings.api_base_url,
            protocol=settings.api_protocol,
            timeout_seconds=settings.limits.timeout_seconds,
            round_budget=round_budget,
        ),
        settings=CodexRunnerSettings(
            codex_binary=settings.codex_binary,
            bwrap_binary=Path("/usr/bin/bwrap"),
            python_binary=Path("/usr/bin/python3"),
            job_root=settings.codex_job_root,
            timeout_seconds=settings.limits.timeout_seconds,
            max_input_bytes=min(settings.limits.max_input_bytes, 1024 * 1024),
            max_output_bytes=1024 * 1024,
            reply_classification=(
                identity.capability
                in {"business_mail_triage", "business_mail_followup"}
                or (
                    identity.capability == "reply_qualification"
                    and (
                        round_budget is not None
                        or bool(settings.qq_reply and settings.qq_reply.continuous)
                    )
                )
            ),
        ),
        scope=KnowledgeTaskScope(
            str(identity.tenant_id), str(identity.employee_id), str(identity.run_id)
        ),
        discovery=discovery,
    )
