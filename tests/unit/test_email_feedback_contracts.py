"""Provider-neutral 邮件反馈 DTO 的严格安全合同。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from shared.errors import ValidationError

NOW = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)


def _module() -> object:
    return importlib.import_module("shared.schemas.email_feedback")


def _correlation(**changes: object) -> object:
    module = _module()
    values: dict[str, object] = {
        "route_id": "route-v1",
        "deterministic_message_id": (
            "route-v1." + "a" * 64 + "@messages.tradeos.invalid"
        ),
        "idempotency_header": "route-v1." + "b" * 64,
    }
    values.update(changes)
    return module.EmailFeedbackCorrelation(**values)


def _item(**changes: object) -> object:
    module = _module()
    values: dict[str, object] = {
        "provider_event_id": "a" * 64,
        "provider_ref_digest": "b" * 64,
        "ordinal": 0,
        "kind": module.EmailFeedbackKind.HARD_BOUNCE,
        "occurred_at": NOW,
        "correlation": _correlation(),
        "parse_issue": None,
    }
    values.update(changes)
    return module.EmailFeedbackItem(**values)


def test_email_feedback_contracts_accept_strict_valid_page() -> None:
    module = _module()
    item = _item()
    page = module.EmailFeedbackPage(None, "history-v1", (item,))
    assert page.items == (item,)
    assert module.EmailFeedbackPage("history-v1", "history-v1", ()).items == ()
    assert "messages.tradeos.invalid" not in repr(item)


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_event_id": "A" * 64},
        {"provider_ref_digest": "short"},
        {"ordinal": -1},
        {"ordinal": 100},
        {"ordinal": True},
        {"kind": "hard_bounce"},
        {"occurred_at": NOW.replace(tzinfo=None)},
        {"occurred_at": NOW.astimezone(timezone(timedelta(hours=8)))},
        {"correlation": object()},
        {"parse_issue": "malformed"},
    ],
)
def test_email_feedback_item_rejects_invalid_types_and_bounds(
    changes: dict[str, object],
) -> None:
    with pytest.raises((ValidationError, TypeError)):
        _item(**changes)


def test_kind_issue_and_correlation_combinations_are_closed() -> None:
    module = _module()
    malformed = module.EmailFeedbackParseIssue.MALFORMED
    for changes in (
        {"kind": module.EmailFeedbackKind.HARD_BOUNCE, "parse_issue": malformed},
        {"kind": module.EmailFeedbackKind.SOFT_BOUNCE, "correlation": None},
        {"kind": module.EmailFeedbackKind.COMPLAINT, "parse_issue": malformed},
        {"kind": module.EmailFeedbackKind.COMPLAINT, "correlation": None},
        {"kind": module.EmailFeedbackKind.UNPARSEABLE, "parse_issue": None},
        {
            "kind": module.EmailFeedbackKind.UNPARSEABLE,
            "parse_issue": malformed,
            "correlation": _correlation(),
        },
    ):
        with pytest.raises(ValidationError):
            _item(**changes)
    assert _item(
        kind=module.EmailFeedbackKind.UNPARSEABLE,
        correlation=None,
        parse_issue=malformed,
    ).parse_issue is malformed


def test_complaint_kind_is_typed_and_requires_correlation() -> None:
    """投诉与退信同级：必须是带精确 correlation 的已解析事实。"""
    module = _module()
    assert module.EmailFeedbackKind.COMPLAINT.value == "complaint"
    item = _item(kind=module.EmailFeedbackKind.COMPLAINT)
    assert item.kind is module.EmailFeedbackKind.COMPLAINT
    assert item.correlation is not None
    assert item.parse_issue is None


@pytest.mark.parametrize(
    "changes",
    [
        {"route_id": "Bearer-private"},
        {"deterministic_message_id": "customer@example.com"},
        {"idempotency_header": "token-secret"},
        {"route_id": "bad\nvalue"},
        {"idempotency_header": "other-route." + "b" * 64},
    ],
)
def test_correlation_rejects_secret_like_or_unapproved_values(
    changes: dict[str, object],
) -> None:
    raw = next(iter(changes.values()))
    with pytest.raises(ValidationError) as raised:
        _correlation(**changes)
    assert str(raw) not in str(raised.value)


def test_page_is_immutable_bounded_and_unique() -> None:
    module = _module()
    first = _item()
    second = _item(provider_event_id="c" * 64, ordinal=1)
    assert module.EmailFeedbackPage(None, "cursor-v1", (first, second)).items == (
        first,
        second,
    )
    same_ordinal_from_another_message = _item(
        provider_event_id="d" * 64,
        provider_ref_digest="e" * 64,
        ordinal=0,
    )
    assert module.EmailFeedbackPage(
        None,
        "cursor-v1",
        (first, same_ordinal_from_another_message),
    ).items == (first, same_ordinal_from_another_message)
    for items in (
        [first],
        tuple(first for _ in range(101)),
        (first, _item()),
        (first, _item(provider_event_id="a" * 64, ordinal=1)),
    ):
        with pytest.raises(ValidationError):
            module.EmailFeedbackPage(None, "cursor-v1", items)


@pytest.mark.parametrize(
    ("starting", "next_cursor"),
    [
        (object(), "cursor-v1"),
        (None, ""),
        (None, " private"),
        (None, "cursor\x00value"),
        (None, "x" * 32769),
        (None, "Bearer-private"),
    ],
)
def test_page_rejects_unsafe_cursor_values(
    starting: object, next_cursor: object
) -> None:
    with pytest.raises(ValidationError) as raised:
        _module().EmailFeedbackPage(starting, next_cursor, ())
    assert "Bearer-private" not in str(raised.value)


def test_feedback_dtos_do_not_offer_raw_payload_fields() -> None:
    module = _module()
    forbidden = {
        "address",
        "body",
        "diagnostic_code",
        "final_recipient",
        "headers",
        "payload",
        "raw_message",
        "subject",
    }
    for record in (
        module.EmailFeedbackCorrelation,
        module.EmailFeedbackItem,
        module.EmailFeedbackPage,
    ):
        assert forbidden.isdisjoint(record.__dataclass_fields__)
