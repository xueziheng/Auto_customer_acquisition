"""公开研究国家政策结构化决策的 fail-closed 行为。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from domains.compliance.schemas import CountryPolicyAction, CountryPolicyDecision
from shared.schemas.identifiers import (
    CountryPolicyVersionId,
    TenantId,
    UserId,
)
from tests.unit.country_policy_gateway_support import (
    build_country_policy_gateway,
    country_policy_manifest,
)
from tool_gateway.checks.web_discovery import (
    WebResearchCountryPolicyCheck,
    WebResearchPreflight,
)
from tool_gateway.errors import (
    ToolCallStatus,
    ToolErrorCategory,
    ToolGatewayError,
)
from tool_gateway.pipeline import ToolCallContext, ToolInvocationState

TENANT = TenantId("ten_01J00000000000000000000000")
USER = UserId("usr_01J00000000000000000000000")


def _state() -> ToolInvocationState:
    state = ToolInvocationState(
        manifest=SimpleNamespace(
            tool_id="web.search",
            version="v1",
            risk_level=SimpleNamespace(value="low"),
            cost_class=SimpleNamespace(value="low"),
            idempotency=SimpleNamespace(value="none"),
            checks=("country_policy",),
        ),
        tool_call_id="tcl_01J00000000000000000000000",
    )
    state.preflight = WebResearchPreflight(TENANT, "XZ", "synthetic-category")
    return state


def _ctx() -> ToolCallContext:
    return ToolCallContext(
        TENANT,
        USER,
        "web.search",
        {"country": "XZ", "category": "synthetic-category"},
    )


def _decision(
    *,
    country_key: str = "xz",
    action: CountryPolicyAction = CountryPolicyAction.PUBLIC_RESEARCH,
    configured: bool = True,
    allowed: bool = True,
) -> CountryPolicyDecision:
    return CountryPolicyDecision(
        country_key=country_key,
        action=action,
        configured=configured,
        allowed=allowed,
        active_version_id=(
            CountryPolicyVersionId("cpp_01J00000000000000000000000")
            if configured
            else None
        ),
        content_hash="b" * 64 if configured else None,
        requirements=("manual_review",) if configured else (),
    )


class _Reader:
    def __init__(self, result: object | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    async def decision(
        self,
        tenant_id: TenantId,
        country: str,
        action: CountryPolicyAction,
    ) -> CountryPolicyDecision:
        self.calls.append((tenant_id, country, action))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_unknown_country_rejects_not_configured_before_handler() -> None:
    reader = _Reader(_decision(configured=False, allowed=False))
    rejection = await WebResearchCountryPolicyCheck(reader).check(_ctx(), _state())
    assert rejection is not None
    assert rejection.stage == "country_policy"
    assert rejection.rule == "country_policy:not_configured"
    assert reader.calls == [(TENANT, "xz", CountryPolicyAction.PUBLIC_RESEARCH)]


@pytest.mark.asyncio
async def test_configured_denial_rejects_action_not_allowed_before_handler() -> None:
    rejection = await WebResearchCountryPolicyCheck(
        _Reader(_decision(allowed=False))
    ).check(_ctx(), _state())
    assert rejection is not None
    assert rejection.stage == "country_policy"
    assert rejection.rule == "country_policy:action_not_allowed"


@pytest.mark.asyncio
async def test_allowed_decision_with_exact_version_and_hash_continues() -> None:
    reader = _Reader(_decision())
    assert await WebResearchCountryPolicyCheck(reader).check(_ctx(), _state()) is None
    assert reader.calls == [(TENANT, "xz", CountryPolicyAction.PUBLIC_RESEARCH)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        _decision(country_key="xy"),
        _decision(action=CountryPolicyAction.CONTACT_ENRICHMENT),
        CountryPolicyDecision.model_construct(
            country_key="xz",
            action=CountryPolicyAction.PUBLIC_RESEARCH,
            configured=False,
            allowed=True,
            active_version_id=None,
            content_hash=None,
            requirements=(),
        ),
        CountryPolicyDecision.model_construct(
            country_key="xz",
            action=CountryPolicyAction.PUBLIC_RESEARCH,
            configured=True,
            allowed=True,
            active_version_id="not-a-policy-version",
            content_hash="b" * 64,
            requirements=(),
        ),
        object(),
    ],
)
async def test_mismatched_country_or_action_in_decision_is_transient_failure(
    decision: object,
) -> None:
    with pytest.raises(ToolGatewayError) as captured:
        await WebResearchCountryPolicyCheck(_Reader(decision)).check(_ctx(), _state())
    assert captured.value.category is ToolErrorCategory.PROVIDER_TRANSIENT


@pytest.mark.asyncio
async def test_noncanonical_active_version_fails_gateway_before_transport() -> None:
    decision = CountryPolicyDecision.model_construct(
        country_key="xz",
        action=CountryPolicyAction.PUBLIC_RESEARCH,
        configured=True,
        allowed=True,
        active_version_id="not-a-policy-version",
        content_hash="b" * 64,
        requirements=(),
    )
    gateway, handler, transport = build_country_policy_gateway(
        country_policy_manifest("web.search"),
        WebResearchPreflight(TENANT, "XZ", "synthetic-category"),
        WebResearchCountryPolicyCheck(_Reader(decision)),
    )

    result = await gateway.invoke(_ctx())

    assert handler.prepare_calls == 0
    assert handler.execute_calls == 0
    assert transport.calls == 0
    assert result.status is ToolCallStatus.REJECTED
    assert result.error_category is ToolErrorCategory.PROVIDER_TRANSIENT
    assert result.rejected is not None
    assert result.rejected.stage == "runtime"
    assert result.rejected.rule == "runtime:provider_transient"


@pytest.mark.asyncio
async def test_reader_exception_is_transient_failure_not_denial() -> None:
    with pytest.raises(ToolGatewayError) as captured:
        await WebResearchCountryPolicyCheck(
            _Reader(RuntimeError("private-provider-canary"))
        ).check(_ctx(), _state())
    assert captured.value.category is ToolErrorCategory.PROVIDER_TRANSIENT
    assert "private-provider-canary" not in repr(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        _decision(configured=False, allowed=False),
        _decision(allowed=False),
    ],
)
async def test_rejected_path_calls_provider_transport_zero_times(
    decision: CountryPolicyDecision,
) -> None:
    gateway, handler, transport = build_country_policy_gateway(
        country_policy_manifest("web.search"),
        WebResearchPreflight(TENANT, "XZ", "synthetic-category"),
        WebResearchCountryPolicyCheck(_Reader(decision)),
    )
    result = await gateway.invoke(_ctx())

    assert handler.prepare_calls == 0
    assert handler.execute_calls == 0
    assert transport.calls == 0
    assert result.status is ToolCallStatus.REJECTED
    assert result.rejected is not None
    assert result.rejected.stage == "country_policy"
    assert result.rejected.rule == (
        "country_policy:not_configured"
        if not decision.configured
        else "country_policy:action_not_allowed"
    )
