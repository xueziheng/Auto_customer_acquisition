"""Email send handler 的 ephemeral material 与指纹合同。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from domains.outreach.schemas import MessageSendPreflight
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    EnrollmentId,
    IdempotencyKey,
    MessageAttemptId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    UserId,
)
from tool_gateway.errors import DeliveryCertainty
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.pipeline import ToolCallContext


def _email_send_module():
    return __import__("tool_gateway.handlers.email_send", fromlist=["EmailSendHandler"])


def _preflight(**changes: object) -> MessageSendPreflight:
    values: dict[str, object] = {
        "tenant_id": TenantId("tn_01HZX3S5Y3V9BAS7X9C04S0A00"),
        "attempt_id": MessageAttemptId("mat_01HZX3S5Y3V9BAS7X9C04S0A00"),
        "campaign_id": CampaignId("cmp_01HZX3S5Y3V9BAS7X9C04S0A00"),
        "enrollment_id": EnrollmentId("enr_01HZX3S5Y3V9BAS7X9C04S0A00"),
        "account_id": ProspectAccountId("acc_01HZX3S5Y3V9BAS7X9C04S0A00"),
        "contact_point_id": ContactPointId("cp_01HZX3S5Y3V9BAS7X9C04S0A00"),
        "sending_identity_id": SendingIdentityId("sid_01HZX3S5Y3V9BAS7X9C04S0A00"),
        "campaign_version": 2,
        "step_number": 1,
        "idempotency_key": IdempotencyKey("send-key-01"),
    }
    values.update(changes)
    return MessageSendPreflight(**values)  # type: ignore[arg-type]


class _Materials:
    def __init__(self, material) -> None:
        self.material = material
        self.calls: list[tuple[str, MessageSendPreflight]] = []

    async def resolve(self, tenant_id, preflight):
        self.calls.append((tenant_id, preflight))
        return self.material


class _Links:
    def __init__(self, links: list[str]) -> None:
        self.links = links
        self.calls = 0

    async def build(self, tenant_id, preflight):
        del tenant_id, preflight
        value = self.links[min(self.calls, len(self.links) - 1)]
        self.calls += 1
        return value


class _Gmail:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def send_once(self, request):
        self.calls.append(request)
        result_cls = __import__("connectors.gmail.client", fromlist=["GmailSendResult"]).GmailSendResult
        return result_cls("gmail_ref_01", DeliveryCertainty.SENT, False)


def _material(preflight: MessageSendPreflight, **changes: object):
    email_send = _email_send_module()
    cls = email_send.DeliveryMaterial
    values: dict[str, object] = {
        "tenant_id": preflight.tenant_id,
        "attempt_id": preflight.attempt_id,
        "account_id": preflight.account_id,
        "contact_point_id": preflight.contact_point_id,
        "sending_identity_id": preflight.sending_identity_id,
        "from_address": "sender@example.com",
        "recipient_address": "buyer@example.net",
    }
    values.update(changes)
    return cls(**values)


def _context(
    preflight: MessageSendPreflight,
    *,
    subject: str = "TradeOS subject",
    body: str = " Exact body with spaces \n",
) -> ToolCallContext:
    return ToolCallContext(
        tenant_id=preflight.tenant_id,
        user_id=UserId("usr_01HZX3S5Y3V9BAS7X9C04S0A00"),
        tool_id="email.send",
        params={
            "attempt_id": str(preflight.attempt_id),
            "subject": subject,
            "body": body,
        },
        idempotency_key=preflight.idempotency_key,
        campaign_ref=str(preflight.campaign_id),
    )


def _handler(preflight: MessageSendPreflight, *, links: list[str] | None = None, key: bytes = b"k" * 32):
    material = _material(preflight)
    materials = _Materials(material)
    link_provider = _Links(links or ["https://example.com/unsubscribe/ref_01"])
    gmail = _Gmail()
    cls = _email_send_module().EmailSendHandler
    handler = cls(
        gmail,
        materials,
        link_provider,
        HmacFingerprintProvider("fp-v1", key),
    )
    return handler, materials, link_provider, gmail


@pytest.mark.asyncio
async def test_prepare_resolves_material_once_and_persists_only_safe_projection() -> None:
    preflight = _preflight()
    handler, materials, links, gmail = _handler(preflight)
    prepared = await handler.prepare(_context(preflight), preflight)
    assert prepared.audit_projection == {
        "attempt_id": str(preflight.attempt_id),
        "subject_bytes": len(b"TradeOS subject"),
        "body_bytes": len(b" Exact body with spaces \n"),
        "has_unsubscribe": True,
    }
    assert len(materials.calls) == 1
    assert links.calls == 1
    assert gmail.calls == []
    for raw in (
        "sender@example.com",
        "buyer@example.net",
        "TradeOS subject",
        "Exact body",
        "https://example.com/unsubscribe/ref_01",
    ):
        assert raw not in repr(prepared)


@pytest.mark.asyncio
async def test_prepare_sends_exact_content_that_approval_stage_checked() -> None:
    preflight = _preflight()
    handler, _, _, gmail = _handler(preflight)
    subject = "Approved employee subject"
    body = "Approved employee body"

    prepared = await handler.prepare(
        _context(preflight, subject=subject, body=body), preflight
    )
    await handler.execute(preflight.tenant_id, prepared)

    assert len(gmail.calls) == 1
    assert gmail.calls[0].subject == subject
    assert gmail.calls[0].body == body


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", TenantId("tn_01HZX3S5Y3V9BAS7X9C04S0A01")),
        ("attempt_id", MessageAttemptId("mat_01HZX3S5Y3V9BAS7X9C04S0A01")),
        ("account_id", ProspectAccountId("acc_01HZX3S5Y3V9BAS7X9C04S0A01")),
        ("contact_point_id", ContactPointId("cp_01HZX3S5Y3V9BAS7X9C04S0A01")),
        ("sending_identity_id", SendingIdentityId("sid_01HZX3S5Y3V9BAS7X9C04S0A01")),
    ],
)
@pytest.mark.asyncio
async def test_prepare_rejects_material_binding_mismatch(field: str, value: object) -> None:
    preflight = _preflight()
    handler, materials, _, gmail = _handler(preflight)
    materials.material = replace(materials.material, **{field: value})
    with pytest.raises(ValidationError):
        await handler.prepare(_context(preflight), preflight)
    assert gmail.calls == []


@pytest.mark.parametrize("preflight", [None, object()])
@pytest.mark.asyncio
async def test_prepare_requires_typed_current_preflight(preflight: object) -> None:
    valid = _preflight()
    handler, _, _, gmail = _handler(valid)
    with pytest.raises(ValidationError):
        await handler.prepare(_context(valid), preflight)
    assert gmail.calls == []


@pytest.mark.asyncio
async def test_fingerprint_preserves_raw_whitespace_and_changes_on_one_byte_or_link() -> None:
    preflight = _preflight()
    handler, _, _, _ = _handler(preflight)
    first = await handler.prepare(_context(preflight), preflight)
    second = await handler.prepare(
        _context(preflight, body="Exact body with spaces \n"), preflight
    )
    assert first.request_fingerprint != second.request_fingerprint

    changed_link_handler, _, _, _ = _handler(preflight, links=["https://example.com/unsubscribe/ref_02"])
    changed_link = await changed_link_handler.prepare(_context(preflight), preflight)
    assert first.request_fingerprint != changed_link.request_fingerprint

    unstable_handler, _, _, _ = _handler(
        preflight,
        links=[
            "https://example.com/unsubscribe/ref_01",
            "https://example.com/unsubscribe/ref_02",
        ],
    )
    unstable_first = await unstable_handler.prepare(_context(preflight), preflight)
    unstable_second = await unstable_handler.prepare(_context(preflight), preflight)
    assert unstable_first.request_fingerprint != unstable_second.request_fingerprint


@pytest.mark.asyncio
async def test_key_rotation_changes_version_and_digest() -> None:
    preflight = _preflight()
    first, _, _, _ = _handler(preflight, key=b"a" * 32)
    second, _, _, _ = _handler(preflight, key=b"b" * 32)
    left = await first.prepare(_context(preflight), preflight)
    right = await second.prepare(_context(preflight), preflight)
    assert left.fingerprint_version == right.fingerprint_version == "fp-v1"
    assert left.request_fingerprint != right.request_fingerprint


@pytest.mark.asyncio
async def test_execute_calls_connector_once_and_returns_only_safe_metadata() -> None:
    preflight = _preflight()
    handler, _, _, gmail = _handler(preflight)
    prepared = await handler.prepare(_context(preflight), preflight)
    output = await handler.execute(preflight.tenant_id, prepared)
    assert output == {"provider_ref": "gmail_ref_01", "already_existed": False}
    assert len(gmail.calls) == 1
    assert "buyer@example.net" not in repr(output)


@pytest.mark.asyncio
async def test_message_id_and_custom_header_are_domain_separated_hmac_values() -> None:
    preflight = _preflight()
    handler, _, _, gmail = _handler(preflight)
    prepared = await handler.prepare(_context(preflight), preflight)
    await handler.execute(preflight.tenant_id, prepared)
    request = gmail.calls[0]
    message_id = request.deterministic_message_id
    header = request.idempotency_header
    assert str(preflight.attempt_id) not in message_id
    assert str(preflight.attempt_id) not in header
    assert message_id.endswith("@messages.tradeos.invalid")
    assert len(message_id.removesuffix("@messages.tradeos.invalid")) == 64
    assert len(header) == 64
    assert message_id.removesuffix("@messages.tradeos.invalid") != header


@pytest.mark.asyncio
async def test_execute_rejects_secret_like_provider_output() -> None:
    preflight = _preflight()
    handler, _, _, gmail = _handler(preflight)

    async def unsafe(_request):
        result_cls = __import__("connectors.gmail.client", fromlist=["GmailSendResult"]).GmailSendResult
        return result_cls("Bearer_secret", DeliveryCertainty.SENT, False)

    gmail.send_once = unsafe
    prepared = await handler.prepare(_context(preflight), preflight)
    with pytest.raises(ValidationError):
        await handler.execute(preflight.tenant_id, prepared)
