"""受控原件契约：拒绝伪造绑定、隐式转换和非canonical定位。"""

from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256

import pytest
from pydantic import ValidationError

from shared.schemas import evidence_read as e

ULID = "01KZXT00000000000000000001"
TENANT = "tn_" + ULID
ARTIFACT = "art_" + ULID


def raw_meta(**changes):
    return e.EvidenceRawMeta(
        **{
            "tenant_id": TENANT,
            "artifact_id": ARTIFACT,
            "kind": "pdf",
            "mime_type": "application/pdf",
            "content_hash": sha256(b"abc").hexdigest(),
            "size_bytes": 3,
            "observed_at": datetime(2026, 8, 28, tzinfo=UTC),
            **changes,
        }
    )


def test_locator_uses_codepoints_and_utf8_excerpt_budget():
    parsed = e.ParsedEvidenceText(profile="pdf-text-v1", page=1, text="A😀B")
    selected, excerpt = e.select_evidence_text(parsed, 1, 2, maximum_excerpt_bytes=4)
    assert excerpt == "😀"
    assert selected.start == 1 and selected.end == 2
    assert e.parse_evidence_locator(e.make_evidence_locator(selected)) == selected
    with pytest.raises(e.QuoteEvidenceError) as caught:
        e.select_evidence_text(parsed, 1, 2, maximum_excerpt_bytes=3)
    assert caught.value.code == "parse_limit_exceeded"


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant_id": "tn_bad"},
        {"artifact_id": "gen_" + ULID},
        {"size_bytes": True},
        {"size_bytes": 0},
        {"size_bytes": "3"},
        {"kind": "email_raw"},
        {"mime_type": "message/rfc822"},
        {"content_hash": "A" * 64},
        {"observed_at": datetime(2026, 8, 28)},  # noqa: DTZ001 - 非法naive时间夹具
    ],
)
def test_meta_rejects_invalid_binding(changes):
    with pytest.raises(ValidationError):
        raw_meta(**changes)


def test_raw_and_text_are_never_in_repr_or_dump():
    raw = e.EvidenceRawContent(meta=raw_meta(), content=b"abc")
    parsed = e.ParsedEvidenceText(
        profile="rfc822-plain-v1", page=None, text="private-sentinel"
    )
    assert "content" not in raw.model_dump()
    assert "text" not in parsed.model_dump()
    assert "private-sentinel" not in repr(parsed)
    assert "abc" not in repr(raw)
    with pytest.raises(ValidationError):
        e.EvidenceRawContent(meta=raw_meta(), content=b"abd")
    assert (
        raw_meta(
            observed_at=datetime(2026, 8, 28, tzinfo=timezone(timedelta(hours=8)))
        ).observed_at.tzinfo
        is UTC
    )


@pytest.mark.parametrize(
    "locator",
    [
        "",
        "$.text",
        "pdf-text-v1:p=01;c=0:2;h=" + "a" * 64,
        "pdf-text-v1:p=0;c=0:2;h=" + "a" * 64,
        "rfc822-plain-v1:c=+0:2;h=" + "a" * 64,
        "rfc822-plain-v1:c=2:2;h=" + "a" * 64,
        "rfc822-plain-v1:c=0:2;h=" + "A" * 64,
    ],
)
def test_locator_rejects_noncanonical_shapes(locator):
    with pytest.raises(e.QuoteEvidenceError) as caught:
        e.parse_evidence_locator(locator)
    assert caught.value.code == "invalid_input"


@pytest.mark.parametrize("actor", ["", " boss", "boss ", True, "x" * 41, "a\x80"])
def test_bad_employee_identity_is_not_coerced(actor):
    with pytest.raises(ValidationError):
        reference(actor)


def reference(actor="legacy-boss"):
    return e.AuthorizedEvidenceReference(
        tenant_id=TENANT,
        actor_id=actor,
        source_ref="upload:upl_" + ULID,
        scope=e.PricingEvidenceScope(purpose="pricing"),
        raw=raw_meta(),
        message_id=None,
        conversation_id=None,
        account_id=None,
    )


def test_legacy_employee_is_valid_but_scope_source_pair_is_strict():
    assert reference().actor_id == "legacy-boss"
    with pytest.raises(ValidationError):
        e.EvidenceVerifyRequest(
            operation="verify",
            source_ref="message:msg_" + ULID,
            scope=e.PricingEvidenceScope(purpose="pricing"),
            locator="$",
        )
    with pytest.raises(ValidationError):
        e.EvidenceVerifyRequest(
            operation="verify",
            source_ref="message:msg_" + ULID,
            scope=e.NeedUnitEvidenceScope(
                purpose="need_unit", need_id="need_" + ULID, action="read"
            ),
            locator="$",
        )


def test_errors_are_fixed_and_not_retryable():
    error = e.QuoteEvidenceError("source_unavailable")
    assert str(error) == "来源暂不可用" and not error.is_retryable
    assert error.context == {}
    with pytest.raises(ValueError):
        e.QuoteEvidenceError("arbitrary")


@pytest.mark.parametrize("value", [True, 0, -1, "1", 1.0])
def test_object_limits_require_positive_strict_integers(value):
    with pytest.raises(ValidationError):
        e.ObjectReadLimits(
            connect_timeout_ms=value,
            read_timeout_ms=1000,
            total_timeout_ms=5000,
            chunk_bytes=65536,
            maximum_attempts=1,
        )
