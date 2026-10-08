"""资料任务的身份、配置撤销和解析边界；不读取线上凭证。"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.scheduler_worker.knowledge import (
    KnowledgeModelAuthority,
    _Analyzer,
    _KnowledgeInputBudget,
    _Parser,
)
from connectors.codex.client import encode_codex_input
from connectors.files.knowledge import KnowledgeParseFailure
from shared.errors import InvalidStateTransition, PermissionDenied
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from tests.unit.test_enterprise_knowledge_analysis import identity
from tests.unit.test_enterprise_knowledge_processor import claim
from workflows.enterprise_knowledge.processor import (
    KnowledgeInput,
    KnowledgeProcessingFailure,
)


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "other_tenant"), ("employee_id", "other_employee"),
    ("user_id", "other_user"), ("run_id", "other_job"),
    ("configuration_version", "changed"), ("sequence", 1),
    ("capability", "assistant"),
])
async def test_model_identity_changes_fail_before_configuration(field, value):
    trusted = identity()
    service = SimpleNamespace(authorize_processing=AsyncMock())
    configuration = SimpleNamespace(authorize=AsyncMock())
    authority = KnowledgeModelAuthority(service, claim(), configuration, trusted)
    with pytest.raises(ModelGenerationError) as caught:
        await authority.check(trusted.model_copy(update={field: value}))
    assert caught.value.code == "permission"
    service.authorize_processing.assert_not_called()
    configuration.authorize.assert_not_called()


@pytest.mark.parametrize("error", [PermissionDenied("revoked"), InvalidStateTransition("expired")])
async def test_revoked_claim_fails_closed(error):
    trusted = identity()
    service = SimpleNamespace(authorize_processing=AsyncMock(side_effect=error))
    configuration = SimpleNamespace(authorize=AsyncMock())
    authority = KnowledgeModelAuthority(service, claim(), configuration, trusted)
    with pytest.raises(ModelGenerationError) as caught:
        await authority.check(trusted)
    assert caught.value.code == "permission"
    configuration.authorize.assert_not_called()


async def test_configuration_is_rechecked_each_time():
    trusted = identity()
    service = SimpleNamespace(authorize_processing=AsyncMock())
    configuration = SimpleNamespace(authorize=AsyncMock(side_effect=[None, ModelGenerationError("configuration")]))
    authority = KnowledgeModelAuthority(service, claim(), configuration, trusted)
    await authority.check(trusted)
    with pytest.raises(ModelGenerationError) as caught:
        await authority.check(trusted)
    assert caught.value.code == "configuration"
    assert service.authorize_processing.await_count == 2
    assert configuration.authorize.await_count == 2
    assert configuration.authorize.await_args.kwargs == {"probe": False}


@pytest.mark.parametrize("reason", ["source_limit_exceeded", "size_limit", "text_limit", "page_limit", "archive_limit"])
async def test_all_parser_capacity_failures_are_actionable(reason):
    parser = SimpleNamespace(extract=AsyncMock(side_effect=KnowledgeParseFailure(reason)))
    with pytest.raises(KnowledgeProcessingFailure) as caught:
        await _Parser(parser).parse(b"synthetic", "text/plain")
    assert caught.value.reason == "source_limit_exceeded"


async def test_parser_warnings_are_not_appended_to_source():
    parser = SimpleNamespace(extract=AsyncMock(return_value=SimpleNamespace(
        text="Model: Valve-A.", images=(), image_source_pages=(),
        warnings=("formulas_not_evaluated",),
    )))
    result = await _Parser(parser).parse(b"synthetic", "text/plain")
    assert result.text == "Model: Valve-A."
    assert result.warnings == ("formulas_not_evaluated",)


class _RecordingModelGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[InvocationIdentity, ModelRequest]] = []
        self.response = ModelResponse(
            text='{"synthetic": true}', model="deepseek-flash",
            usage=ModelUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1),
        )

    async def generate(
        self, identity: InvocationIdentity, request: ModelRequest,
    ) -> ModelResponse:
        self.calls.append((identity, request))
        return self.response


def _request(document: str = "产品规格") -> ModelRequest:
    return ModelRequest(
        model="deepseek-flash", system_prompt="资料整理",
        payload={"document": document}, max_output_tokens=2048,
    )


def test_codex_input_encoding_preserves_utf8_and_default_separators() -> None:
    expected = '{"instructions": "资料整理", "input": {"document": "产品规格"}}'
    assert encode_codex_input(_request()) == expected.encode("utf-8")


async def test_chinese_capacity_is_utf8_bytes_and_never_reaches_gateway() -> None:
    gateway = _RecordingModelGateway()
    request = _request("规" * 22000)
    budget = _KnowledgeInputBudget(gateway, max_input_bytes=65536)
    assert len(encode_codex_input(request).decode("utf-8")) < 65536
    with pytest.raises(KnowledgeProcessingFailure) as caught:
        await budget.generate(identity(), request)
    assert caught.value.reason == "source_limit_exceeded"
    assert caught.value.uncertain is False
    assert gateway.calls == []


async def test_codex_envelope_overhead_is_rejected_before_gateway() -> None:
    gateway = _RecordingModelGateway()
    request = _request()
    gateway_text_bytes = (
        len(json.dumps(request.payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        + len(request.system_prompt.encode("utf-8"))
    )
    budget = _KnowledgeInputBudget(gateway, max_input_bytes=gateway_text_bytes)
    with pytest.raises(KnowledgeProcessingFailure) as caught:
        await budget.generate(identity(), request)
    assert caught.value.reason == "source_limit_exceeded"
    assert gateway.calls == []


@pytest.mark.parametrize("extra_bytes", [-1, 0, 1])
async def test_exact_envelope_boundary_and_unchanged_request(extra_bytes: int) -> None:
    gateway = _RecordingModelGateway()
    request = _request()
    trusted = identity()
    expected = '{"instructions": "资料整理", "input": {"document": "产品规格"}}'
    budget = _KnowledgeInputBudget(
        gateway, max_input_bytes=len(expected.encode("utf-8")) + extra_bytes,
    )
    assert gateway.calls == []
    if extra_bytes < 0:
        with pytest.raises(KnowledgeProcessingFailure) as caught:
            await budget.generate(trusted, request)
        assert caught.value.reason == "source_limit_exceeded"
        assert gateway.calls == []
    else:
        assert await budget.generate(trusted, request) is gateway.response
        assert len(gateway.calls) == 1
        passed_identity, passed_request = gateway.calls[0]
        assert passed_identity is trusted
        assert passed_request is request


@pytest.mark.parametrize("source,expected_calls", [("Model: Valve-A.", 1), ("规" * 22000, 0)])
async def test_analyzer_capacity_failures_are_actionable(
    monkeypatch: pytest.MonkeyPatch, source: str, expected_calls: int,
) -> None:
    gateway = SimpleNamespace(generate=AsyncMock(side_effect=ModelGenerationError("output_limit")))
    monkeypatch.setattr(
        "apps.scheduler_worker.knowledge.build_model_composition",
        lambda **kwargs: SimpleNamespace(generator=gateway),
    )
    settings = SimpleNamespace(
        model="deepseek-flash", configuration_version="v1", secret_ref="synthetic/model",
        limits=SimpleNamespace(timeout_seconds=60, max_input_bytes=65536, max_output_tokens=2048),
    )
    knowledge = SimpleNamespace(
        root=Path("/synthetic"), codex_binary=Path("/fixed/codex"), timeout_seconds=60,
    )
    analyzer = _Analyzer(
        service=SimpleNamespace(), sessions=object(), settings=settings, knowledge=knowledge,
        resolver=object(), fingerprints=object(), instance_id="synthetic-worker",
    )
    with pytest.raises(KnowledgeProcessingFailure) as caught:
        await analyzer.analyze(claim(), KnowledgeInput(source))
    assert caught.value.reason == "source_limit_exceeded"
    assert caught.value.uncertain is False
    assert gateway.generate.await_count == expected_calls
