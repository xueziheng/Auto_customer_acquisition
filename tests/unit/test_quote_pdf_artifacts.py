"""派生PDF契约与三hash：全为受控metadata，不渲染PDF。"""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError as PydanticValidationError

from artifact_store.store import GeneratedArtifactKind, GeneratedArtifactMeta
from shared.errors import ValidationError
from shared.schemas.quote_document import CustomerQuoteView

ULID = "00000000000000000000000000"
NOW = datetime(2026, 8, 28, tzinfo=UTC)


def customer_view() -> CustomerQuoteView:
    return CustomerQuoteView(
        quote_id=f"quo_{ULID}", version=1, issuer_name="公司", issuer_address="Address",
        issuer_contact="Contact", account_name="Buyer", description="Widget",
        specification="Steel", unit="pcs", quantity_display="10.00",
        unit_price_display=" 2.00 ", total_display="20.00", currency="USD",
        valid_until_display="2026-09-01", approved_terms=("First term", "Second term"),
    )


def pdf_meta() -> GeneratedArtifactMeta:
    return GeneratedArtifactMeta(
        tenant_id=f"tn_{ULID}", artifact_id=f"art_{ULID}",
        kind=GeneratedArtifactKind.QUOTE_PDF, content_hash="a" * 64, size_bytes=4,
        mime_type="application/pdf", workflow_run_id=f"run_{ULID}",
        subject_ref=f"quo_{ULID}", sequence_number=1,
        idempotency_key=f"quo_{ULID}:1:quote_pdf:quote_pdf_v1",
        generated_by="quote_pdf_v1", generated_at=NOW,
    )


def test_pdf_metadata_accepts_its_own_subject_and_key() -> None:
    assert pdf_meta().kind.value == "quote_pdf"


@pytest.mark.parametrize(("field", "value"), [
    ("subject_ref", f"enr_{ULID}"), ("mime_type", "text/plain"),
    ("generated_by", "arbitrary_template"), ("sequence_number", True),
    ("sequence_number", 0), ("idempotency_key", f"quo_{ULID}:2:quote_pdf:quote_pdf_v1"),
    ("idempotency_key", f"quo_{ULID}:1:draft"), ("workflow_run_id", "run_bad"),
    ("workflow_run_id", f"run_8{ULID[1:]}"), ("content_hash", "A" * 64),
    ("generated_at", NOW.replace(tzinfo=None)),
])
def test_pdf_rejects_cross_branch_or_malformed_metadata(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        replace(pdf_meta(), **{field: value})


@pytest.mark.parametrize("field", tuple(CustomerQuoteView.model_fields))
def test_customer_hash_binds_every_customer_field_and_order(field: str) -> None:
    from shared.schemas import quote_document

    hash_view = getattr(quote_document, "customer_quote_hash", None)
    assert callable(hash_view), "缺少客户规范hash"
    view = customer_view()
    value = getattr(view, field)
    changed = value + "!" if isinstance(value, str) else (
        value + 1 if isinstance(value, int) else tuple(reversed(value))
    )
    assert hash_view(view.model_copy(update={field: changed})) != hash_view(view)
    expected = hashlib.sha256(json.dumps(
        {"version": "customer-quote-v1", "customer": view.model_dump(mode="json")},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    assert hash_view(view) == expected


def file_values() -> dict[str, object]:
    return {"file_id": f"qfl_{ULID}", "quote_id": f"quo_{ULID}", "quote_version": 1,
        "artifact_id": f"art_{ULID}", "content_hash": "a" * 64,
        "quote_content_hash": "b" * 64, "customer_content_hash": "c" * 64,
        "template_version": "quote_pdf_v1", "size_bytes": 4, "generated_at": NOW}


def test_file_view_normalizes_time_and_is_safe_frozen() -> None:
    from domains.quotations import schemas

    view_type = getattr(schemas, "QuoteFileView", None)
    assert view_type is not None, "缺少安全文件DTO"
    values = file_values()
    values["generated_at"] = NOW.astimezone(timezone(timedelta(hours=8)))
    view = view_type(**values)
    assert view.generated_at.tzinfo is UTC
    assert set(view.model_dump()) == set(file_values())
    with pytest.raises(PydanticValidationError):
        view.size_bytes = 5


@pytest.mark.parametrize(("field", "value"), [
    ("quote_version", True), ("size_bytes", 0), ("size_bytes", "4"),
    ("content_hash", "A" * 64), ("quote_content_hash", "a" * 63),
    ("customer_content_hash", "g" * 64), ("file_id", f"qfl_8{ULID[1:]}"),
    ("generated_at", NOW.replace(tzinfo=None)), ("object_key", "secret"),
])
def test_file_view_rejects_invalid_values(field: str, value: object) -> None:
    from domains.quotations import schemas

    view_type = getattr(schemas, "QuoteFileView", None)
    assert view_type is not None, "缺少安全文件DTO"
    with pytest.raises(PydanticValidationError):
        view_type(**{**file_values(), field: value})
