"""离线profile字节规则；仅测试可直接接触纯profile函数。"""

import io

import pytest
from pypdf import PdfWriter
from reportlab.pdfgen.canvas import Canvas

from shared.schemas.evidence_read import EvidenceParseLimits, QuoteEvidenceError


def parse_limits(**changes):
    return EvidenceParseLimits(
        **{
            "maximum_input_bytes": 2097152,
            "maximum_pages": 8,
            "maximum_text_bytes": 262144,
            "maximum_excerpt_bytes": 8192,
            "cpu_seconds": 8,
            "address_space_bytes": 268435456,
            "wall_timeout_ms": 8000,
            "maximum_result_bytes": 2097152,
            "maximum_concurrency": 2,
            "queue_timeout_ms": 2000,
            "termination_grace_ms": 200,
            **changes,
        }
    )


def pdf_bytes(text="Controlled supplier statement", pages=1):
    buffer = io.BytesIO()
    canvas = Canvas(buffer, invariant=True)
    for _ in range(pages):
        if text:
            canvas.drawString(40, 700, text)
        canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def parse(content, profile="rfc822-plain-v1", page=None, **changes):
    from connectors.evidence_text.profiles import parse_profile

    return parse_profile(
        content, profile=profile, page=page, limits=parse_limits(**changes)
    )


@pytest.mark.parametrize(
    "body,want",
    [
        (
            b"Content-Type: text/plain; charset=utf-8\r\n\r\nWe need 50 pieces.\r\n",
            "We need 50 pieces.\n",
        ),
        (
            b"Content-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: base64\n\nSGVsbG8g8J+YgA==",
            "Hello 😀",
        ),
        (
            b"Content-Type: text/plain; charset=iso-8859-1\nContent-Transfer-Encoding: quoted-printable\n\ncaf=E9",
            "café",
        ),
    ],
)
def test_plain_decoding_preserves_exact_body(body, want):
    assert parse(body).text == want


@pytest.mark.parametrize(
    "content",
    [
        b"Content-Type: text/plain; charset=utf-8\n\n\xff",
        b"Content-Type: text/plain; charset=unknown-charset\n\nhello",
        b"Content-Type: text/html\n\n<p>hello</p>",
        b"Content-Type: text/plain\nContent-Disposition: attachment\n\nhello",
        b"Content-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: base64\n\n@@@@",
        b"Content-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: base64\n\nSGVsbG8@",
        b"Content-Type: text/plain\n\n \t\r\n",
    ],
)
def test_unsupported_email_is_never_repaired(content):
    with pytest.raises(QuoteEvidenceError) as caught:
        parse(content)
    assert caught.value.code == "parse_unsupported"


def test_first_non_attachment_plain_and_full_body_no_trim():
    message = b'Content-Type: multipart/mixed; boundary="bound"\n\n--bound\nContent-Type: text/plain\nContent-Disposition: attachment\n\nIGNORE\n--bound\nContent-Type: text/plain; charset=utf-8\n\n  first\rsecond\n--bound\nContent-Type: text/plain\n\nlast\n--bound--\n'
    assert parse(message).text == "  first\nsecond"
    with pytest.raises(QuoteEvidenceError) as caught:
        parse(
            b"Content-Type: text/plain\n\nabcdef",
            maximum_text_bytes=5,
            maximum_excerpt_bytes=5,
        )
    assert caught.value.code == "parse_limit_exceeded"


def test_pdf_extracts_specified_page_and_rejects_page_budget():
    assert (
        parse(pdf_bytes(), "pdf-text-v1", 1).text == "Controlled supplier statement\n"
    )
    with pytest.raises(QuoteEvidenceError) as caught:
        parse(pdf_bytes(pages=2), "pdf-text-v1", 1, maximum_pages=1)
    assert caught.value.code == "parse_limit_exceeded"


@pytest.mark.parametrize("kind", ["encrypted", "corrupt", "blank", "scan"])
def test_pdf_unsupported_types(kind):
    if kind == "corrupt":
        content = b"not-a-pdf"
    elif kind == "encrypted":
        writer = PdfWriter(io.BytesIO(pdf_bytes()))
        writer.encrypt("controlled")
        buffer = io.BytesIO()
        writer.write(buffer)
        content = buffer.getvalue()
    elif kind == "scan":
        from PIL import Image

        buffer = io.BytesIO()
        canvas = Canvas(buffer, invariant=True)
        canvas.drawInlineImage(
            Image.new("RGB", (8, 8), "black"), 40, 600, width=100, height=100
        )
        canvas.showPage()
        canvas.save()
        content = buffer.getvalue()
    else:
        content = pdf_bytes("")
    with pytest.raises(QuoteEvidenceError) as caught:
        parse(content, "pdf-text-v1", 1)
    assert caught.value.code == "parse_unsupported"
