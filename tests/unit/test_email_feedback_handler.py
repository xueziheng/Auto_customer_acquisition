"""邮件反馈 Tool Handler 的 ephemeral page 与安全投影合同。"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime

import pytest

from connectors.gmail.client import GmailConnector
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId, UserId, new_id
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.pipeline import CheckRejection, ToolCallContext, ToolCallResult

NOW = datetime(2026, 8, 13, 10, tzinfo=UTC)


def _page() -> object:
    schemas = importlib.import_module("shared.schemas.email_feedback")
    correlation = schemas.EmailFeedbackCorrelation(
        None,
        "a" * 64 + "@messages.tradeos.invalid",
        "b" * 64,
    )
    item = schemas.EmailFeedbackItem(
        "c" * 64,
        "d" * 64,
        0,
        schemas.EmailFeedbackKind.HARD_BOUNCE,
        NOW,
        correlation,
        None,
    )
    return schemas.EmailFeedbackPage(None, "gfc1.safe-cursor", (item,))


class Reader:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.error: BaseException | None = None

    async def fetch(
        self,
        tenant_id: TenantId,
        mailbox_alias: str,
        cursor: str | None,
        page_limit: int,
    ) -> object:
        self.calls.append((tenant_id, mailbox_alias, cursor, page_limit))
        if self.error is not None:
            raise self.error
        return _page()


class _Secrets:
    def __init__(self) -> None:
        self.refs: list[str] = []

    def resolve(self, secret_ref: str) -> str:
        self.refs.append(secret_ref)
        return "oauth-private-marker"


class _Transport:
    async def search(self, *, token: str, message_id: str, header: str) -> None:
        del token, message_id, header

    async def send(self, *, token: str, raw_message: bytes) -> str:
        del token, raw_message
        return "unused"

    async def get_profile_history_id(self, *, token: str) -> str:
        assert token == "oauth-private-marker"
        return "100"

    async def list_feedback_messages(
        self, *, token: str, after_epoch: int, page_token: str | None
    ) -> tuple[tuple[str, ...], str | None]:
        del token, after_epoch, page_token
        return (), None

    async def list_feedback_history(
        self, *, token: str, start_history_id: str, page_token: str | None
    ) -> tuple[tuple[str, ...], str | None, str]:
        del token, start_history_id, page_token
        return (), None, "101"

    async def get_raw_message(self, *, token: str, message_ref: str) -> bytes:
        del token, message_ref
        raise AssertionError("no message expected")


def _context(tenant: TenantId, **params: object) -> ToolCallContext:
    values: dict[str, object] = {
        "mailbox_alias": "feedback-primary",
        "cursor": "gfc1.private-cursor-marker",
        "page_limit": 25,
    }
    values.update(params)
    return ToolCallContext(
        tenant_id=tenant,
        user_id=UserId(new_id("usr")),
        tool_id="email.feedback.fetch",
        params=values,
    )


def _handler(reader: Reader) -> tuple[object, object]:
    module = importlib.import_module("tool_gateway.handlers.email_feedback")
    slot = module.FeedbackPageSlot()
    handler = module.EmailFeedbackFetchHandler(
        reader,
        slot,
        HmacFingerprintProvider("fp-v1", b"f" * 32),
    )
    return handler, slot


@pytest.mark.asyncio
async def test_prepare_fingerprint_covers_raw_cursor_but_projection_does_not() -> None:
    tenant = TenantId(new_id("tn"))
    reader = Reader()
    handler, _slot = _handler(reader)
    first = await handler.prepare(_context(tenant), None)
    second = await handler.prepare(
        _context(tenant, cursor="gfc1.different-private-cursor"), None
    )
    assert first.request_fingerprint != second.request_fingerprint
    assert first.audit_projection == {
        "mailbox_alias": "feedback-primary",
        "page_limit": 25,
        "has_cursor": True,
    }
    rendered = repr(first)
    for forbidden in (
        "private-cursor-marker",
        "different-private-cursor",
        "@messages.tradeos.invalid",
    ):
        assert forbidden not in rendered
    assert reader.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"mailbox_alias": "private@example.com"},
        {"mailbox_alias": "Bearer-private"},
        {"cursor": 1},
        {"cursor": ""},
        {"page_limit": 0},
        {"page_limit": 101},
        {"page_limit": True},
        {"extra": "value"},
    ],
)
@pytest.mark.asyncio
async def test_prepare_rejects_malformed_params_without_raw_echo(
    changes: dict[str, object],
) -> None:
    tenant = TenantId(new_id("tn"))
    handler, _slot = _handler(Reader())
    with pytest.raises(ValidationError) as raised:
        await handler.prepare(_context(tenant, **changes), None)
    assert "private@example.com" not in str(raised.value)
    assert "Bearer-private" not in str(raised.value)


@pytest.mark.asyncio
async def test_execute_returns_only_one_shot_handle_and_take_deletes() -> None:
    tenant = TenantId(new_id("tn"))
    reader = Reader()
    handler, slot = _handler(reader)
    prepared = await handler.prepare(_context(tenant, cursor=None), None)
    result = await handler.execute(tenant, prepared)
    assert set(result) == {"provider_ref"}
    handle = result["provider_ref"]
    assert isinstance(handle, str) and handle.startswith("fpg_")
    page = slot.take(handle)
    assert page == _page()
    with pytest.raises(ValidationError):
        slot.take(handle)
    with pytest.raises(ValidationError):
        slot.take(new_id("fpg"))
    assert reader.calls == [(tenant, "feedback-primary", None, 25)]


@pytest.mark.asyncio
async def test_slot_capacity_is_one_and_reader_failure_or_cancellation_clears() -> None:
    tenant = TenantId(new_id("tn"))
    reader = Reader()
    handler, slot = _handler(reader)
    prepared = await handler.prepare(_context(tenant), None)
    first = await handler.execute(tenant, prepared)
    with pytest.raises(ValidationError):
        await handler.execute(tenant, prepared)
    with pytest.raises(ValidationError):
        slot.take(first["provider_ref"])

    for error in (RuntimeError("private reader marker"), asyncio.CancelledError()):
        reader.error = error
        with pytest.raises(type(error)):
            await handler.execute(tenant, prepared)
        assert slot.is_empty
    reader.error = None
    assert (await handler.execute(tenant, prepared))["provider_ref"].startswith(
        "fpg_"
    )


@pytest.mark.asyncio
async def test_execute_rejects_tenant_or_payload_mismatch_before_reader() -> None:
    tenant = TenantId(new_id("tn"))
    reader = Reader()
    handler, _slot = _handler(reader)
    prepared = await handler.prepare(_context(tenant), None)
    with pytest.raises(ValidationError):
        await handler.execute(TenantId(new_id("tn")), prepared)
    assert reader.calls == []


@pytest.mark.asyncio
async def test_trusted_reader_configures_connector_only_when_fetch_is_called() -> None:
    module = importlib.import_module("tool_gateway.handlers.email_feedback")
    tenant = TenantId(new_id("tn"))
    secrets = _Secrets()
    factory_calls: list[TenantId] = []

    def factory(requested_tenant: TenantId) -> GmailConnector:
        factory_calls.append(requested_tenant)
        return GmailConnector(_Transport(), now=lambda: NOW)

    reader = module._GmailProviderEmailFeedbackReader(factory, secrets)
    assert factory_calls == [] and secrets.refs == []
    page = await reader.fetch(tenant, "feedback-primary", None, 10)
    assert page.items == ()
    assert factory_calls == [tenant]
    assert secrets.refs == ["GMAIL_OAUTH_TOKEN_REF"]


class _Gateway:
    def __init__(self, slot: object) -> None:
        self.slot = slot
        self.calls: list[ToolCallContext] = []
        self.status = ToolCallStatus.SUCCEEDED
        self.error: BaseException | None = None

    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult:
        self.calls.append(ctx)
        handle = self.slot.put(_page())
        if self.error is not None:
            raise self.error
        if self.status is ToolCallStatus.SUCCEEDED:
            return ToolCallResult(
                tool_id=ctx.tool_id,
                status=self.status,
                output={"provider_ref": handle},
                tool_call_id=new_id("tcl"),
                cost_note="low",
            )
        return ToolCallResult(
            tool_id=ctx.tool_id,
            status=self.status,
            rejected=CheckRejection(
                "permission",
                "permission:denied",
                "当前操作者无权读取反馈",
            ),
            tool_call_id=new_id("tcl"),
            error_category=ToolErrorCategory.PERMISSION_DENIED,
        )


@pytest.mark.asyncio
async def test_tool_reader_invokes_gateway_then_takes_page_once() -> None:
    module = importlib.import_module("tool_gateway.handlers.email_feedback")
    tenant = TenantId(new_id("tn"))
    user = UserId(new_id("usr"))
    slot = module.FeedbackPageSlot()
    gateway = _Gateway(slot)
    reader = module.ToolGatewayEmailFeedbackReader(gateway, slot, user)
    page = await reader.fetch(tenant, "feedback-primary", None, 25)
    assert page == _page()
    assert slot.is_empty
    assert len(gateway.calls) == 1
    assert gateway.calls[0].params == {
        "mailbox_alias": "feedback-primary",
        "cursor": None,
        "page_limit": 25,
    }


@pytest.mark.asyncio
async def test_tool_reader_clears_unclaimed_page_on_failure_or_cancellation() -> None:
    module = importlib.import_module("tool_gateway.handlers.email_feedback")
    tenant = TenantId(new_id("tn"))
    slot = module.FeedbackPageSlot()
    gateway = _Gateway(slot)
    reader = module.ToolGatewayEmailFeedbackReader(
        gateway,
        slot,
        UserId(new_id("usr")),
    )

    gateway.status = ToolCallStatus.REJECTED
    with pytest.raises(ToolGatewayError) as rejected:
        await reader.fetch(tenant, "feedback-primary", None, 25)
    assert rejected.value.category is ToolErrorCategory.PERMISSION_DENIED
    assert slot.is_empty

    gateway.status = ToolCallStatus.SUCCEEDED
    gateway.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await reader.fetch(tenant, "feedback-primary", None, 25)
    assert slot.is_empty
