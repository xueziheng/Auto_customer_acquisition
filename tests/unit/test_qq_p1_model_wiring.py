"""原租户模型选择与 Codex provider 接线；只捕获参数，零凭证和外部调用。"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from apps.composition_support import model as composition
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.model_invocation import InvocationIdentity, ModelGenerationError
from shared.schemas.qq_trial import QQTrialLimits
from tests.integration.test_model_usage import limits
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.qq_trial_model import TrialMessageGenerators


@pytest.mark.parametrize("mismatch",[None,"model","version"])
def test_original_catalog_to_codex_receives_same_round_budget(monkeypatch,mismatch):
    settings=StandaloneModelSettings(provider="deepseek",model="synthetic-p1-model",
        secret_ref="SYNTHETIC_ONLY",configuration_version="synthetic-p1-v1",model_data_export_enabled=True,
        limits=limits(timeout_seconds=120,max_input_bytes=65536,max_output_tokens=1024))
    selected=settings.model_copy(update={"model":"different" if mismatch=="model" else settings.model,
        "configuration_version":"different" if mismatch=="version" else settings.configuration_version})
    gate=SimpleNamespace(scope=SimpleNamespace(model=settings.model,
        model_configuration_version=settings.configuration_version,
        limits=QQTrialLimits(model_timeout_seconds=30)))
    identity=InvocationIdentity(tenant_id="synthetic-tenant",user_id="synthetic-user",employee_id="synthetic-owner",
        run_id="synthetic-run",capability="reply_qualification",configuration_version=settings.configuration_version,sequence=0)
    calls=[]
    provider=object()
    def resolve(config,actual):
        assert config is settings and actual is identity
        calls.append("catalog")
        return selected
    def codex(config,actual,*,round_budget):
        calls.append((config,actual,round_budget))
        return provider
    monkeypatch.setattr(composition,"resolve_task_model",resolve)
    monkeypatch.setattr(composition,"configured_codex_provider",codex)
    result=composition.build_model_composition(settings=settings,resolver=object(),authority=object(),usage=object(),
        ledger_factory=lambda tenant:pytest.fail("unit must not open ledger"),
        fingerprints=HmacFingerprintProvider("v1",b"synthetic-local-only-fingerprint!"),lease_owner="synthetic-p1",lease_duration=timedelta(seconds=90),
        trial_gate=gate)
    assert isinstance(result.generator,TrialMessageGenerators) and calls==[]
    budget=object()
    if mismatch is not None:
        with pytest.raises(ModelGenerationError):
            result.generator.provider_factory(budget,identity)
        assert calls==["catalog"]
    else:
        assert result.generator.provider_factory(budget,identity) is provider
        config,actual,bound=calls[1]
        assert actual is identity and bound is budget
        assert (config.limits.timeout_seconds,config.limits.max_input_bytes,config.limits.max_output_tokens)==(30,32768,512)


def test_unactivated_gate_rejects_scope_and_rebinding():
    from infra.db.qq_trial import SqlQQTrialGate
    from shared.schemas.qq_trial import QQTrialStopped
    async def allowed():
        return True
    gate=SqlQQTrialGate(None,None,allowed)
    with pytest.raises(QQTrialStopped):
        _=gate.scope
    scope=object()
    gate.bind_scope(scope)
    assert gate.scope is scope
    with pytest.raises(QQTrialStopped):
        gate.bind_scope(scope)
