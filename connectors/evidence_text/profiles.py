"""纯profile实现，只由受限worker调用；测试可独立验证字节规则。"""

import io
from email import policy
from email.parser import BytesParser

from pypdf import PdfReader

from shared.schemas.evidence_read import (
    EvidenceParseLimits,
    EvidenceProfile,
    ParsedEvidenceText,
    QuoteEvidenceError,
)


def parse_profile(
    content: bytes,
    *,
    profile: EvidenceProfile,
    page: int | None,
    limits: EvidenceParseLimits,
) -> ParsedEvidenceText:
    """只统一换行，拒绝无文本/非法编码及任何超限。"""
    try:
        if len(content) > limits.maximum_input_bytes:
            raise QuoteEvidenceError("source_limit_exceeded")
        if profile == "pdf-text-v1":
            if type(page) is not int or page < 1:
                raise QuoteEvidenceError("invalid_input")
            pdf = PdfReader(io.BytesIO(content), strict=True)
            if pdf.is_encrypted:
                raise QuoteEvidenceError("parse_unsupported")
            count = len(pdf.pages)
            if count > limits.maximum_pages:
                raise QuoteEvidenceError("parse_limit_exceeded")
            if page > count:
                raise QuoteEvidenceError("locator_mismatch")
            text = pdf.pages[page - 1].extract_text(
                extraction_mode="plain", orientations=(0, 90, 180, 270)
            )
        elif profile == "rfc822-plain-v1" and page is None:
            message = BytesParser(policy=policy.default).parsebytes(content)
            text = ""
            for part in message.walk():
                if (
                    part.get_content_type() != "text/plain"
                    or part.get_content_disposition() == "attachment"
                ):
                    continue
                value = part.get_content(errors="strict")
                if not isinstance(value, str) or part.defects:
                    raise QuoteEvidenceError("parse_unsupported")
                if value.strip():
                    text = value
                    break
        else:
            raise QuoteEvidenceError("invalid_input")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            raise QuoteEvidenceError("parse_unsupported")
        if len(text.encode("utf-8", errors="strict")) > limits.maximum_text_bytes:
            raise QuoteEvidenceError("parse_limit_exceeded")
        return ParsedEvidenceText(profile=profile, page=page, text=text)
    except QuoteEvidenceError:
        raise
    except MemoryError:
        raise QuoteEvidenceError("parse_limit_exceeded") from None
    except Exception:  # noqa: BLE001 - 解析库与编码异常不携原文
        raise QuoteEvidenceError("parse_unsupported") from None
