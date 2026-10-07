"""发件身份认证 workflow 的安全编排合同。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from shared.errors import TransientError
from shared.schemas.identifiers import (
    AuthenticationCheckRequestId,
    IdempotencyKey,
    RunId,
    SendingIdentityId,
    TenantId,
)
from workflows.engine.runner import StepStatus, WorkflowRun

_NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
_TENANT = TenantId("tn_01K2C5R6J7ABCDEFGHJKMNPQRS")
_IDENTITY = SendingIdentityId("sid_01K2C5R6J7ABCDEFGHJKMNPQRS")
_REQUEST = AuthenticationCheckRequestId("acr_01K2C5R6J7ABCDEFGHJKMNPQRS")


def _flow():
    try:
        return importlib.import_module("workflows.sending_identity_auth.flow")
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED：认证 workflow 尚未创建（{exc.name}）")


def _contracts():
    try:
        return importlib.import_module("shared.schemas.dns_auth")
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED：DNS 合同尚未创建（{exc.name}）")


def _repository():
    return importlib.import_module("domains.sending_identity.repository")


def _run() -> WorkflowRun:
    return WorkflowRun(
        RunId("run_01K2C5R6J7ABCDEFGHJKMNPQRS"),
        _TENANT,
        "sending_identity_authentication",
        1,
        str(_REQUEST),
        "check",
        StepStatus.PENDING,
        _NOW,
        context={
            "request_id": str(_REQUEST),
            "sending_identity_id": str(_IDENTITY),
        },
    )


class _Sending:
    def __init__(self, request_status: object | None = None) -> None:
        self.transitions: list[tuple[object, object]] = []
        self.results: list[tuple[object, object, Any]] = []
        self.actors: list[Any] = []
        self.request_status = request_status

    async def get(self, tenant_id, identity_id, *, actor):
        self.actors.append(actor)
        return SimpleNamespace(
            tenant_id=tenant_id,
            identity_id=identity_id,
            domain="example.co.uk",
        )

    async def transition_authentication_check_request(
        self, tenant_id, request_id, target_status, *, actor
    ):
        self.actors.append(actor)
        self.transitions.append((request_id, target_status))
        self.request_status = target_status
        return SimpleNamespace(
            request_id=request_id,
            tenant_id=tenant_id,
            sending_identity_id=_IDENTITY,
            request_key=IdempotencyKey("request-key"),
            status=target_status,
            requested_at=_NOW,
            completed_at=None,
        )

    async def get_authentication_check_request(
        self, tenant_id, request_id, *, actor
    ):
        self.actors.append(actor)
        status = self.request_status or _repository().AuthenticationCheckRequestStatus.REQUESTED
        return SimpleNamespace(
            request_id=request_id,
            tenant_id=tenant_id,
            sending_identity_id=_IDENTITY,
            request_key=IdempotencyKey("request-key"),
            status=status,
            requested_at=_NOW,
            completed_at=(
                _NOW
                if status
                in {
                    _repository().AuthenticationCheckRequestStatus.SUCCEEDED,
                    _repository().AuthenticationCheckRequestStatus.FAILED,
                }
                else None
            ),
        )

    async def record_authentication_result(
        self, tenant_id, identity_id, result, *, actor
    ):
        self.actors.append(actor)
        self.results.append((tenant_id, identity_id, result))


class _Tool:
    def __init__(self, result: object | BaseException) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def check(self, **kwargs: object):
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _facts(*, passed: bool) -> Any:
    contracts = _contracts()
    failures: tuple[Any, ...] = ()
    if not passed:
        failures = (
            contracts.DnsAuthenticationFailure(
                "spf",
                contracts.DnsAuthenticationFailureCategory.MALFORMED,
                "configure_spf",
            ),
        )
    return contracts.DnsAuthenticationFacts(
        _NOW,
        passed,
        True,
        True,
        failures,
        "a" * 64,
    )


async def test_success_uses_safe_tool_inputs_exact_system_actor_and_completes() -> None:
    """workflow 只能传安全 ID/domain/selector，成功才记录认证并终结 request。"""
    flow = _flow()
    sending = _Sending()
    tool = _Tool(_facts(passed=True))
    handler = flow.DnsAuthenticationStep(sending, tool, dkim_selector="s1")

    outcome = await handler.execute(_run())

    repository = _repository()
    assert outcome == ("complete", None, {})
    assert tool.calls == [
        {
            "tenant_id": _TENANT,
            "run_id": RunId("run_01K2C5R6J7ABCDEFGHJKMNPQRS"),
            "request_id": _REQUEST,
            "sending_identity_id": _IDENTITY,
            "domain": "example.co.uk",
            "dkim_selector": "s1",
        }
    ]
    assert [status for _, status in sending.transitions] == [
        repository.AuthenticationCheckRequestStatus.RUNNING,
        repository.AuthenticationCheckRequestStatus.SUCCEEDED,
    ]
    assert len(sending.results) == 1
    assert sending.results[0][2].check_ref == f"dns_{'a' * 60}"
    assert all(actor.role == "system" for actor in sending.actors)
    assert all(
        actor.scope.allowed_identity_ids == frozenset({_IDENTITY})
        for actor in sending.actors
    )


async def test_permanent_typed_failure_records_result_and_fails_request_and_run() -> (
    None
):
    """DNS 永久失败是业务结果，必须落事实并进入 failed 而非重试。"""
    flow = _flow()
    sending = _Sending()
    handler = flow.DnsAuthenticationStep(
        sending, _Tool(_facts(passed=False)), dkim_selector="s1"
    )
    outcome = await handler.execute(_run())
    repository = _repository()
    assert outcome == ("fail", "authentication_failed", {})
    assert [status for _, status in sending.transitions] == [
        repository.AuthenticationCheckRequestStatus.RUNNING,
        repository.AuthenticationCheckRequestStatus.FAILED,
    ]
    assert len(sending.results) == 1
    failure = sending.results[0][2].failures[0]
    assert failure.check.value == "spf"
    assert failure.category.value == "record_invalid"


async def test_retryable_resolver_error_leaves_request_running_and_no_raw_context() -> (
    None
):
    """临时错误必须交给引擎退避，不能伪造失败事实或污染 context。"""
    flow = _flow()
    sending = _Sending()
    handler = flow.DnsAuthenticationStep(
        sending,
        _Tool(TransientError("raw resolver secret")),
        dkim_selector="s1",
    )
    run = _run()
    with pytest.raises(TransientError) as caught:
        await handler.execute(run)
    repository = _repository()
    assert [status for _, status in sending.transitions] == [
        repository.AuthenticationCheckRequestStatus.RUNNING
    ]
    assert sending.results == []
    assert run.context == {
        "request_id": str(_REQUEST),
        "sending_identity_id": str(_IDENTITY),
    }
    assert "raw resolver secret" not in repr(run.context)
    assert "raw resolver secret" not in str(caught.value.__context__)


@pytest.mark.parametrize(
    ("status_name", "expected"),
    [
        ("SUCCEEDED", ("complete", None, {})),
        ("FAILED", ("fail", "authentication_failed", {})),
    ],
)
async def test_terminal_domain_request_replay_does_not_repeat_dns_or_writes(
    status_name: str, expected: tuple[str, str | None, dict[str, object]]
) -> None:
    """域终态已提交但 step 未提交时，重放必须只收敛 workflow。"""
    repository = _repository()
    sending = _Sending(getattr(repository.AuthenticationCheckRequestStatus, status_name))
    tool = _Tool(_facts(passed=True))
    outcome = await _flow().DnsAuthenticationStep(
        sending, tool, dkim_selector="s1"
    ).execute(_run())
    assert outcome == expected
    assert tool.calls == []
    assert sending.transitions == []
    assert sending.results == []


async def test_duplicate_requested_event_starts_one_idempotent_workflow() -> None:
    """Outbox 至少一次投递不能创建第二个认证 run。"""
    flow = _flow()

    class Engine:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        async def start(self, *args, **kwargs):
            self.calls.append((*args, kwargs))
            return RunId("run_01K2C5R6J7ABCDEFGHJKMNPQRS")

    event_type = importlib.import_module(
        "shared.events.catalog"
    ).AuthenticationCheckRequested
    event = event_type(
        tenant_id=_TENANT,
        occurred_at=_NOW,
        request_id=_REQUEST,
        sending_identity_id=_IDENTITY,
    )
    engine = Engine()
    launcher = flow.AuthenticationCheckRequestedHandler(engine)
    await launcher.handle(event)
    await launcher.handle(event)
    assert len(engine.calls) == 2
    first = engine.calls[0]
    assert first[0:3] == (_TENANT, "sending_identity_authentication", str(_REQUEST))
    assert first[4] == f"auth:{_REQUEST}"
    assert engine.calls[1] == first


async def test_unconfigured_checker_finishes_request_without_authentication_facts() -> None:
    """未配置检查器不能把已失败的工作流留成永久 requested，也不能伪造认证事实。"""
    from apps.scheduler_worker.pilot import UnconfiguredDnsStep

    sending = _Sending()
    handler = _flow().FailedAuthenticationStep(sending, UnconfiguredDnsStep())
    assert await handler.execute(_run()) == ("fail", "dns_not_configured", {})
    status = _repository().AuthenticationCheckRequestStatus
    assert sending.request_status is status.FAILED
    assert [value for _, value in sending.transitions] == [status.RUNNING, status.FAILED]
    assert sending.results == []
    assert all(actor.scope.allowed_identity_ids == frozenset({_IDENTITY}) for actor in sending.actors)
    transitions = list(sending.transitions)
    assert await handler.execute(_run()) == ("fail", "authentication_failed", {})
    assert sending.transitions == transitions
    assert sending.results == []


async def test_unconfigured_checker_preserves_already_successful_request() -> None:
    """终态重放不应因当前部署缺少 DNS 而覆盖历史成功。"""
    from apps.scheduler_worker.pilot import UnconfiguredDnsStep

    status = _repository().AuthenticationCheckRequestStatus
    sending = _Sending(status.SUCCEEDED)
    handler = _flow().FailedAuthenticationStep(sending, UnconfiguredDnsStep())
    assert await handler.execute(_run()) == ("complete", None, {})
    assert sending.transitions == []
    assert sending.results == []


async def test_unconfigured_checker_rejects_mismatched_request_before_writing() -> None:
    """请求与发件身份不匹配时不得替另一身份修改状态。"""
    from apps.scheduler_worker.pilot import UnconfiguredDnsStep
    from shared.errors import ValidationError

    class MismatchedSending(_Sending):
        async def get_authentication_check_request(self, tenant_id, request_id, *, actor):
            request = await super().get_authentication_check_request(tenant_id, request_id, actor=actor)
            request.sending_identity_id = SendingIdentityId("sid_01K2C5R6J7ABCDEFGHJKMNPQRT")
            return request

    sending = MismatchedSending()
    handler = _flow().FailedAuthenticationStep(sending, UnconfiguredDnsStep())
    with pytest.raises(ValidationError):
        await handler.execute(_run())
    assert sending.transitions == []
    assert sending.results == []
