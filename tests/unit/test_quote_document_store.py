"""中立适配严格映射技术metadata与固定故障，不扩大为对象删除能力。"""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from artifact_store.errors import ArtifactIntegrityError, ArtifactReadLimitExceeded
from infra.quote_document_store import (
    GeneratedStoreDocumentAdapter,
    GeneratedStoreDocumentMetadataReader,
)
from shared.schemas.generated_documents import (
    GeneratedDocumentError,
    QuotePdfWriteLimits,
)
from tests.unit.test_quote_pdf_artifacts import pdf_meta


@pytest.mark.parametrize(
    "field,value",
    [
        ("maximum_attempts", True),
        ("maximum_attempts", 2),
        ("connect_timeout_ms", 0),
        ("read_timeout_ms", True),
        ("total_timeout_ms", "10"),
    ],
)
def test_write_limits_do_not_admit_retries_or_coercion(field, value):
    with pytest.raises(ValidationError):
        QuotePdfWriteLimits(
            **(
                {
                    "connect_timeout_ms": 1,
                    "read_timeout_ms": 2,
                    "total_timeout_ms": 3,
                    "maximum_attempts": 1,
                }
                | {field: value}
            )
        )


@pytest.mark.parametrize(
    "failure,code",
    [
        (ArtifactIntegrityError(), "corrupt"),
        (ArtifactReadLimitExceeded(), "read_limit"),
        (RuntimeError("private"), "unavailable"),
    ],
)
async def test_bounded_adapter_preserves_fixed_classification(failure, code):
    class Store:
        async def get_bounded(self, *args, **kwargs):
            raise failure

    store = Store()
    adapter = GeneratedStoreDocumentAdapter(store, store)
    with pytest.raises(GeneratedDocumentError) as error:
        await adapter.get_bounded(
            pdf_meta().tenant_id, pdf_meta().artifact_id, maximum_bytes=10
        )
    assert error.value.code == code and "private" not in str(error.value)


async def test_metadata_only_adapter_has_no_wide_storage_methods():
    class Store:
        async def get_meta_by_key(self, tenant, key):
            return pdf_meta()

    adapter = GeneratedStoreDocumentMetadataReader(Store())
    meta = await adapter.get_meta_by_key(
        pdf_meta().tenant_id, pdf_meta().idempotency_key
    )
    assert meta.artifact_hash == pdf_meta().content_hash
    assert not any(
        hasattr(adapter, name) for name in ("put_pdf", "get_bounded", "get", "delete")
    )


async def test_unknown_write_stays_unknown_without_bottom_text():
    class Store:
        async def put(self, *args, **kwargs):
            raise RuntimeError("private write outcome")

    adapter = GeneratedStoreDocumentAdapter(Store(), None)
    with pytest.raises(GeneratedDocumentError) as error:
        await adapter.put_pdf(
            pdf_meta().tenant_id,
            b"pdf",
            workflow_run_id=pdf_meta().workflow_run_id,
            subject_ref=pdf_meta().subject_ref,
            sequence_number=1,
            idempotency_key=pdf_meta().idempotency_key,
            generated_by="quote_pdf_v1",
        )
    assert error.value.code == "commit_unknown" and "private" not in str(error.value)


@pytest.mark.parametrize("change", ["tenant", "key", "kind"])
async def test_metadata_adapter_rejects_wrong_branch_or_binding(change):
    from artifact_store.store import GeneratedArtifactKind

    value = pdf_meta()
    if change == "tenant":
        value = replace(value, tenant_id="tn_00000000000000000000000001")
    elif change == "key":
        subject = "quo_00000000000000000000000001"
        value = replace(
            value,
            subject_ref=subject,
            idempotency_key=f"{subject}:1:quote_pdf:quote_pdf_v1",
        )
    else:
        subject = "enr_00000000000000000000000001"
        value = replace(
            value,
            kind=GeneratedArtifactKind.EMAIL_DRAFT,
            mime_type="application/vnd.tradeos.email-draft+json",
            subject_ref=subject,
            idempotency_key=f"{subject}:1:draft",
            generated_by="legacy",
        )

    class Store:
        async def get_meta_by_key(self, *args):
            return value

    with pytest.raises(GeneratedDocumentError) as error:
        await GeneratedStoreDocumentMetadataReader(Store()).get_meta_by_key(
            pdf_meta().tenant_id, pdf_meta().idempotency_key
        )
    assert error.value.code == "invalid_binding"
