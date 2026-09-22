"""ADR0070：只装配显式模型端口，不读取配置环境或判断业务权限。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from connectors.deepseek.client import DeepSeekClient
from connectors.gmail.client import SecretResolver
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.model_invocation import ModelGenerationPort
from tool_gateway.checks.model import CurrentModelAuthority
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.model_generate import GatewayModelGenerator, ModelProvider
from tool_gateway.model_usage import ModelUsageRepository
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
) -> ModelComposition:
    return ModelComposition(
        GatewayModelGenerator(
            authority=authority,
            usage=usage,
            provider_factory=provider_factory
            or (
                lambda: DeepSeekClient(
                    settings.secret_ref,
                    resolver,
                    timeout_seconds=settings.limits.timeout_seconds,
                )
            ),
            limits=settings.limits,
            model=settings.model,
            configuration_version=settings.configuration_version,
            ledger_factory=ledger_factory,
            fingerprints=fingerprints,
            lease_owner=lease_owner,
            lease_duration=lease_duration,
        )
    )
