"""内联历史引用不能成为本次客户证据；候选保持连续，不跨排除区拼句。"""

import pytest

from connectors.gmail.inbound_mime import parse_inbound_content


@pytest.mark.parametrize(
    "body,mime,current,excluded",
    [
        (
            "Thanks.\n> We offered 100 units.\nNo current need.",
            "text/plain",
            "Thanks.",
            "100 units",
        ),
        (
            "Thanks.\nOn Friday Supplier wrote:\nWe offered 100 units.",
            "text/plain",
            "Thanks.",
            "100 units",
        ),
        (
            "<p>Thanks.</p><blockquote>We offered 100 units.</blockquote><p>No current need.</p>",
            "text/html",
            "Thanks.",
            "100 units",
        ),
        (
            '<p>We need 200 units.</p><div class="gmail_quote">We offered 100 units.</div>',
            "text/html",
            "200 units",
            "100 units",
        ),
    ],
)
def test_historical_quotes_are_excluded_from_current_contiguous_segments(
    body, mime, current, excluded
):
    parsed = parse_inbound_content(
        f"Content-Type: {mime}; charset=utf-8\r\n\r\n{body}".encode()
    )
    segments = getattr(parsed, "evidence_segments", None)
    assert segments is not None, "缺少当前表达的连续候选"
    assert any(current in segment for segment in segments)
    assert all(excluded not in segment for segment in segments)
    assert excluded in parsed.body
    assert all(segment in parsed.body for segment in segments)
    assert all("Thanks.\nNo current need." not in segment for segment in segments)


def test_unclosed_quotes_and_segment_budget_never_become_current_evidence():
    malformed = parse_inbound_content(
        b"Content-Type: text/html\r\n\r\n<p>Thanks.</p><blockquote>100 units"
    )
    assert malformed.evidence_available is False
    many = parse_inbound_content(
        ("Content-Type: text/plain\r\n\r\n" + "current\n> history\n" * 205).encode()
    )
    assert many.evidence_available is False and many.evidence_segments == ()
    assert "history" in many.body
