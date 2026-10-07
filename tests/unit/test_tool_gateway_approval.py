"""真实审批阶段与Gateway账本终态的受控回归，不调用真实Provider。"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from shared.schemas.identifiers import new_id
from tests.unit import test_tool_gateway_pipeline as gateway_fakes
from tool_gateway.checks.approval import ApprovalCheck
from tool_gateway.errors import ToolCallStatus
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import ToolGateway


def _gateway() -> tuple[ToolGateway, list[str], gateway_fakes._Ledger]:
    trace: list[str] = []
    ledger = gateway_fakes._Ledger(trace)
    registry = ToolRegistry()
    registry.register(gateway_fakes._tool_manifest(), gateway_fakes._Handler(trace))
    checks = {
        name: gateway_fakes._Stage(name, trace)
        for name in ("tenant", "permission", "suppression")
    }
    checks.update(
        approval=ApprovalCheck(),
        idempotency=gateway_fakes._IdempotencyStage("idempotency", trace),
        rate_limit=gateway_fakes._RateStage("rate_limit", trace),
    )
    return (
        ToolGateway(
            registry,
            checks,
            lambda tenant: gateway_fakes._Uow(ledger),
            lease_duration=timedelta(minutes=5),
            lease_owner="approval-test",
            now=lambda: gateway_fakes.NOW,
            id_factory=new_id,
        ),
        trace,
        ledger,
    )


@pytest.mark.parametrize("field", ["subject", "body"])
@pytest.mark.parametrize("text", ["Price: 12.50 per unit.", "We can ship tomorrow."])
async def test_unapproved_commitment_is_rejected_before_provider(
    field: str, text: str
) -> None:
    """把商业承诺当安全文案会让真实管线执行发送。"""
    gateway, trace, ledger = _gateway()
    ctx = gateway_fakes._context()
    result = await gateway.invoke(replace(ctx, params={**ctx.params, field: text}))

    assert result.status is ToolCallStatus.REJECTED
    assert result.rejected is not None
    assert result.rejected.stage == "approval"
    assert result.rejected.rule == "approval:commercial_commitment"
    assert "handler.execute" not in trace
    assert "idempotency.claim" not in trace
    assert ledger.records[result.tool_call_id].status is ToolCallStatus.REJECTED


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("padding", ["", " ", "\t", "\n", "\r\n"])
async def test_normal_multiline_body_reaches_provider_and_completes_ledger(
    newline: str, padding: str,
) -> None:
    """正常多段正文应通过真实审批，不能裸异常后只留下received。"""
    gateway, trace, ledger = _gateway()
    ctx = gateway_fakes._context()
    body = f"{padding}Hello,{newline}{newline}Could you share your current sourcing needs?{padding}"
    result = await gateway.invoke(replace(ctx, params={**ctx.params, "body": body}))

    assert result.status is ToolCallStatus.SUCCEEDED
    assert trace.count("handler.execute") == 1
    assert ledger.records[result.tool_call_id].status is ToolCallStatus.SUCCEEDED


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("subject", "Hello\nBcc: other@example.test"),
        ("subject", "Hello\r\nBcc: other@example.test"),
        ("body", "Hello\rCanary-private-content"),
        ("body", "Hello\x00Canary-private-content"),
        ("body", "Hello\x1bCanary-private-content"),
        ("body", "Hello\x7fCanary-private-content"),
        ("body", "Hello\x85Canary-private-content"),
        ("body", ""),
        ("body", " \t\r\n "),
        ("body", "x" * 100_001),
        ("body", "x" * 100_000 + "\n"),
        ("body", None),
    ],
    ids=["subject-lf", "subject-crlf", "bare-cr", "nul", "escape", "del", "c1",
         "empty", "blank", "oversize", "oversize-with-whitespace", "wrong-type"],
)
async def test_invalid_material_is_structured_rejection_with_terminal_ledger(
    field: str, text: object
) -> None:
    """输入问题须拒绝并落终态，不能泄漏原文或变成received上的裸异常。"""
    gateway, trace, ledger = _gateway()
    ctx = gateway_fakes._context()
    result = await gateway.invoke(replace(ctx, params={**ctx.params, field: text}))

    assert result.status is ToolCallStatus.REJECTED
    assert result.rejected is not None
    assert result.rejected.stage == "approval"
    assert result.rejected.rule == "approval:material_invalid"
    assert "Canary-private-content" not in repr(result)
    assert "handler.execute" not in trace
    assert "idempotency.claim" not in trace
    assert ledger.records[result.tool_call_id].status is ToolCallStatus.REJECTED
