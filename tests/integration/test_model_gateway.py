"""真实 Tool Gateway/PG 配额，Provider 仅受控外部端口。"""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from connectors.deepseek.client import DeepSeekFailure
from infra.db.model_usage import SqlModelUsageRepository
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.schemas.identifiers import TenantId, new_id
from shared.schemas.model_invocation import ModelRequest, ModelResponse, ModelUsage
from tests.integration.test_model_usage import identity, limits
from tool_gateway.fingerprint import HmacFingerprintProvider


class Authority:
    def __init__(self):
        self.allowed = True

    async def check(self, identity):
        from shared.schemas.model_invocation import ModelGenerationError

        if not self.allowed:
            raise ModelGenerationError("permission")


class Provider:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    async def generate(self, request):
        self.calls += 1
        if self.fail:
            raise DeepSeekFailure("unknown")
        return ModelResponse(
            text='{"message":"PRIVATE_RESPONSE"}',
            model=request.model,
            usage=ModelUsage(input_tokens=10, cached_input_tokens=2, output_tokens=3),
        )

    async def aclose(self):
        pass


def assemble(engine, authority, provider):
    from tool_gateway.handlers.model_generate import GatewayModelGenerator

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return GatewayModelGenerator(
        authority=authority,
        usage=SqlModelUsageRepository(sessions),
        provider_factory=lambda: provider,
        limits=limits(),
        model="test-model",
        configuration_version="test-v1",
        ledger_factory=lambda tenant: SqlAlchemyToolGatewayUnitOfWork(sessions, tenant),
        fingerprints=HmacFingerprintProvider("v1", b"k" * 32),
        lease_owner="model-test",
        lease_duration=timedelta(seconds=30),
    ), sessions


def request():
    return ModelRequest(
        model="test-model",
        system_prompt="PRIVATE_PROMPT",
        payload={"message": "PRIVATE_INPUT"},
        max_output_tokens=64,
    )


async def test_call_replay_is_not_reexecuted_and_ledgers_have_no_content(
    integration_engine,
):
    from infra.db.tables import ModelInvocationRow, ToolCallRow
    from shared.schemas.model_invocation import ModelGenerationError

    tenant = TenantId(new_id("tn"))
    actor = identity(tenant)
    provider = Provider()
    generator, sessions = assemble(integration_engine, Authority(), provider)
    response = await generator.generate(actor, request())
    assert response.text == '{"message":"PRIVATE_RESPONSE"}'
    with pytest.raises(ModelGenerationError) as caught:
        await generator.generate(actor, request())
    assert caught.value.code == "unknown"
    assert provider.calls == 1
    async with sessions() as session:
        for table in (ModelInvocationRow, ToolCallRow):
            rows = (
                await session.scalars(select(table).where(table.tenant_id == tenant))
            ).all()
            assert rows
            serialized = str(
                [
                    {c.name: getattr(row, c.name) for c in table.__table__.columns}
                    for row in rows
                ]
            )
            for private in ("PRIVATE_PROMPT", "PRIVATE_INPUT", "PRIVATE_RESPONSE"):
                assert private not in serialized


async def test_permission_and_quota_refuse_before_provider(integration_engine):
    from shared.schemas.model_invocation import ModelGenerationError

    authority = Authority()
    authority.allowed = False
    provider = Provider()
    generator, _ = assemble(integration_engine, authority, provider)
    tenant = TenantId(new_id("tn"))
    with pytest.raises(ModelGenerationError):
        await generator.generate(identity(tenant), request())
    assert provider.calls == 0
    authority.allowed = True
    await generator.generate(identity(tenant), request())
    with pytest.raises(ModelGenerationError) as caught:
        await generator.generate(identity(tenant), request())
    assert caught.value.code == "quota"
    assert provider.calls == 1


async def test_unknown_is_not_retried_and_usage_stays_unknown(integration_engine):
    from infra.db.tables import ModelInvocationRow
    from shared.schemas.model_invocation import ModelGenerationError

    tenant = TenantId(new_id("tn"))
    actor = identity(tenant)
    provider = Provider(fail=True)
    generator, sessions = assemble(integration_engine, Authority(), provider)
    for _ in range(2):
        with pytest.raises(ModelGenerationError) as caught:
            await generator.generate(actor, request())
        assert caught.value.code == "unknown"
    assert provider.calls == 1
    async with sessions() as session:
        row = (
            await session.scalars(
                select(ModelInvocationRow).where(ModelInvocationRow.tenant_id == tenant)
            )
        ).one()
        assert (
            row.state == "unknown"
            and row.input_tokens is None
            and not row.slot_released
        )


async def test_wrong_config_and_input_limit_never_calls_provider(integration_engine):
    from shared.schemas.model_invocation import ModelGenerationError

    provider = Provider()
    generator, _ = assemble(integration_engine, Authority(), provider)
    tenant = TenantId(new_id("tn"))
    with pytest.raises(ModelGenerationError):
        await generator.generate(identity(tenant, version="v2"), request())
    large = ModelRequest(
        model="test-model",
        system_prompt="规则",
        payload={"text": "x" * 5000},
        max_output_tokens=64,
    )
    with pytest.raises(ModelGenerationError):
        await generator.generate(identity(tenant), large)
    assert provider.calls == 0
