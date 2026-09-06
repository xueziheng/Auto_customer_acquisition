"""回复读取只用有界原件；完整候选先检查，超限/隐私不改写证据。"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

import pytest

from apps.scheduler_worker.adapters.message_content_reader import (
    ArtifactMessageContentReader,
)
from artifact_store.store import RawArtifactKind, RawArtifactMeta
from shared.errors import ValidationError
from shared.schemas.identifiers import ArtifactId, MessageId, TenantId, new_id


class Raw:
    def __init__(self, body, subject="Inquiry", mime="text/plain", tenant=None):
        self.tenant = tenant or TenantId(new_id("tn"))
        self.artifact = ArtifactId(new_id("art"))
        self.raw = f"Subject: {subject}\r\nContent-Type: {mime}; charset=utf-8\r\n\r\n{body}".encode()
        self.meta = RawArtifactMeta(
            self.tenant,
            self.artifact,
            RawArtifactKind.EMAIL_RAW,
            sha256(self.raw).hexdigest(),
            len(self.raw),
            "message/rfc822",
            None,
            datetime.now(UTC),
        )

    async def get_meta(self, tenant, artifact):
        assert (tenant, artifact) == (self.tenant, self.artifact)
        return self.meta

    async def get_bounded(self, tenant, artifact, *, maximum_bytes):
        assert len(self.raw) <= maximum_bytes
        return await self.get_meta(tenant, artifact), self.raw


def reader(raw, **limits):
    message = SimpleNamespace(
        tenant_id=raw.tenant,
        message_id=MessageId(new_id("msg")),
        direction=SimpleNamespace(value="inbound"),
        raw_artifact_ref=raw.artifact,
    )

    class Messages:
        async def get(self, tenant, mid):
            assert (tenant, mid) == (message.tenant_id, message.message_id)
            return message

    @asynccontextmanager
    async def uow(tenant):
        assert tenant == raw.tenant
        yield SimpleNamespace(messages=Messages())

    return ArtifactMessageContentReader(
        uow,
        raw,
        max_raw_bytes=10000,
        max_subject_chars=limits.get("subject", 100),
        max_body_chars=limits.get("body", 1000),
    ), message


async def test_bounded_only_store_parses_html_without_attachment_or_headers():
    raw = Raw("<p>We need <b>hinges</b>.</p>", mime="text/html")
    consumer, message = reader(raw)
    content = await consumer.load(raw.tenant, message.message_id)
    assert content.body == "\nWe need hinges."
    assert content.subject == "(current reply)"


@pytest.mark.parametrize(
    "body,subject,mime,limits",
    [
        ("Useful " * 20 + "password", "Inquiry", "text/plain", {"body": 10}),
        ("We need hinges", "Long " * 20 + "password", "text/plain", {"subject": 10}),
        (
            '<p>hinges</p><span data-x="password">details</span>',
            "Inquiry",
            "text/html",
            {},
        ),
        ("<p>pass<b>word</b></p>", "Inquiry", "text/html", {}),
        ("We need hinges " * 20, "Inquiry", "text/plain", {"body": 10}),
    ],
)
async def test_unsafe_or_overbudget_content_is_rejected_before_projection(
    body, subject, mime, limits
):
    raw = Raw(body, subject, mime)
    consumer, message = reader(raw, **limits)
    with pytest.raises(ValidationError):
        await consumer.load(raw.tenant, message.message_id)


async def test_signature_projection_retains_exact_original_for_evidence():
    raw = Raw(
        "We need hinges.\nContact buyer@example.test or https://example.test/account. www.example.test"
    )
    consumer, message = reader(raw)
    content = await consumer.load(raw.tenant, message.message_id)
    assert (
        "buyer@example.test" not in content.body
        and "https://" not in content.body
        and "www.example.test" not in content.body
    )
    assert (
        content.original_body
        == "We need hinges.\nContact buyer@example.test or https://example.test/account. www.example.test"
    )
    assert content.projected is True
    assert "buyer@example.test" not in repr(content)


async def test_classifier_receives_only_current_segments_and_safe_subject():
    raw = Raw(
        "Thanks.\n> Please unsubscribe me.\nWe will review.", subject="Unsubscribe"
    )
    consumer, message = reader(raw)
    content = await consumer.load(raw.tenant, message.message_id)
    assert content.subject == "(current reply)"
    assert "unsubscribe" not in content.body
    assert content.body == "Thanks.\n\n[current expression boundary]\nWe will review."
    assert "unsubscribe" in content.original_body


async def test_only_history_has_no_current_expression_to_classify():
    raw = Raw("> Please unsubscribe me.")
    consumer, message = reader(raw)
    with pytest.raises(ValidationError):
        await consumer.load(raw.tenant, message.message_id)
