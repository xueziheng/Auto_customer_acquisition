from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from artifact_store.errors import (
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
)
from artifact_store.store import (
    GENERATED_ARTIFACT_MIME_TYPES,
    RAW_ARTIFACT_MIME_TYPES,
    GeneratedArtifactKind,
    GeneratedArtifactMeta,
    GeneratedArtifactStore,
    RawArtifactKind,
    RawArtifactMeta,
    RawArtifactStore,
)
from artifact_store.transport import ObjectBlobTransport
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ArtifactId,
    IdempotencyKey,
    RunId,
    TenantId,
    UserId,
)

NOW = datetime(2026, 8, 13, 6, 0, tzinfo=UTC)
HASH = "a" * 64
TENANT = TenantId("tn_00000000000000000000000000")
ARTIFACT = ArtifactId("art_00000000000000000000000000")
RUN = RunId("run_00000000000000000000000000")
USER = UserId("usr_00000000000000000000000000")
SUBJECT = "enr_00000000000000000000000000"
KEY = IdempotencyKey(f"{SUBJECT}:1:draft")
GENERATED_MIME = "application/vnd.tradeos.email-draft+json"

RAW_MIME_CASES = (
    (RawArtifactKind.EMAIL_RAW, "message/rfc822"),
    (RawArtifactKind.CHAT_SCREENSHOT, "image/png"),
    (RawArtifactKind.CHAT_SCREENSHOT, "image/jpeg"),
    (RawArtifactKind.CHAT_SCREENSHOT, "image/webp"),
    (RawArtifactKind.PDF, "application/pdf"),
    (
        RawArtifactKind.WORD,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    (
        RawArtifactKind.EXCEL,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    (RawArtifactKind.EXCEL, "text/csv"),
    (RawArtifactKind.WEB_SNAPSHOT, "text/html"),
    (RawArtifactKind.IMAGE, "image/png"),
    (RawArtifactKind.IMAGE, "image/jpeg"),
    (RawArtifactKind.IMAGE, "image/webp"),
    (RawArtifactKind.AUDIO, "audio/mpeg"),
    (RawArtifactKind.AUDIO, "audio/wav"),
    (RawArtifactKind.AUDIO, "audio/mp4"),
)


def _raw_meta(
    *,
    kind: object = RawArtifactKind.PDF,
    content_hash: object = HASH,
    size_bytes: object = 32,
    mime_type: object = "application/pdf",
    tenant_id: object = TENANT,
    artifact_id: object = ARTIFACT,
    uploaded_by: object = USER,
    uploaded_at: object = NOW,
) -> RawArtifactMeta:
    return RawArtifactMeta(
        tenant_id,  # type: ignore[arg-type]
        artifact_id,  # type: ignore[arg-type]
        kind,  # type: ignore[arg-type]
        content_hash,  # type: ignore[arg-type]
        size_bytes,  # type: ignore[arg-type]
        mime_type,  # type: ignore[arg-type]
        uploaded_by,  # type: ignore[arg-type]
        uploaded_at,  # type: ignore[arg-type]
    )


def _generated_meta(
    *,
    kind: object = GeneratedArtifactKind.EMAIL_DRAFT,
    content_hash: object = HASH,
    size_bytes: object = 32,
    mime_type: object = GENERATED_MIME,
    tenant_id: object = TENANT,
    artifact_id: object = ARTIFACT,
    workflow_run_id: object = RUN,
    subject_ref: object = SUBJECT,
    sequence_number: object = 1,
    idempotency_key: object = KEY,
    generated_by: object = "outreach_agent_v1",
    generated_at: object = NOW,
) -> GeneratedArtifactMeta:
    return GeneratedArtifactMeta(
        tenant_id=tenant_id,  # type: ignore[arg-type]
        artifact_id=artifact_id,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        content_hash=content_hash,  # type: ignore[arg-type]
        size_bytes=size_bytes,  # type: ignore[arg-type]
        mime_type=mime_type,  # type: ignore[arg-type]
        workflow_run_id=workflow_run_id,  # type: ignore[arg-type]
        subject_ref=subject_ref,  # type: ignore[arg-type]
        sequence_number=sequence_number,  # type: ignore[arg-type]
        idempotency_key=idempotency_key,  # type: ignore[arg-type]
        generated_by=generated_by,  # type: ignore[arg-type]
        generated_at=generated_at,  # type: ignore[arg-type]
    )


def test_raw_and_generated_kinds_are_disjoint() -> None:
    assert [kind.value for kind in RawArtifactKind] == [
        "email_raw",
        "chat_screenshot",
        "pdf",
        "word",
        "excel",
        "web_snapshot",
        "image",
        "audio",
    ]
    assert [kind.value for kind in GeneratedArtifactKind] == ["email_draft", "quote_pdf"]
    assert {kind.value for kind in RawArtifactKind}.isdisjoint(
        {kind.value for kind in GeneratedArtifactKind}
    )


@pytest.mark.parametrize(("kind", "mime_type"), RAW_MIME_CASES)
def test_each_raw_kind_accepts_only_its_canonical_mime(
    kind: RawArtifactKind, mime_type: str
) -> None:
    meta = _raw_meta(kind=kind, mime_type=mime_type)
    assert meta.kind is kind
    assert meta.mime_type == mime_type


def test_mime_policies_cannot_be_mutated_at_runtime() -> None:
    with pytest.raises(TypeError):
        RAW_ARTIFACT_MIME_TYPES[RawArtifactKind.PDF] = frozenset(  # type: ignore[index]
            {"text/plain"}
        )
    with pytest.raises(TypeError):
        GENERATED_ARTIFACT_MIME_TYPES[
            GeneratedArtifactKind.EMAIL_DRAFT
        ] = frozenset({"text/plain"})  # type: ignore[index]


@pytest.mark.parametrize(
    ("kind", "mime_type"),
    [
        (RawArtifactKind.EMAIL_RAW, "application/pdf"),
        (RawArtifactKind.CHAT_SCREENSHOT, "image/gif"),
        (RawArtifactKind.PDF, "text/html"),
        (RawArtifactKind.WORD, "application/msword"),
        (RawArtifactKind.EXCEL, "application/vnd.ms-excel"),
        (RawArtifactKind.WEB_SNAPSHOT, "text/plain"),
        (RawArtifactKind.IMAGE, "image/gif"),
        (RawArtifactKind.AUDIO, "audio/ogg"),
    ],
)
def test_raw_meta_rejects_wrong_kind_mime_pair(
    kind: RawArtifactKind, mime_type: str
) -> None:
    with pytest.raises(ValidationError, match="^artifact metadata 无效$"):
        _raw_meta(kind=kind, mime_type=mime_type)


def test_generated_meta_is_frozen_and_repr_hides_storage_and_content() -> None:
    meta = _generated_meta()
    rendered = repr(meta).lower()
    assert meta.content_hash == HASH
    assert "bucket" not in rendered
    assert "object" not in rendered
    assert "customer-body-marker" not in rendered
    assert "objects.example.invalid" not in rendered
    with pytest.raises((AttributeError, FrozenInstanceError)):
        meta.size_bytes = 33  # type: ignore[misc]


def test_generated_meta_accepts_only_email_draft_mime() -> None:
    assert _generated_meta().mime_type == GENERATED_MIME
    with pytest.raises(ValidationError, match="^artifact metadata 无效$"):
        _generated_meta(mime_type="message/rfc822")


@pytest.mark.parametrize("bad_hash", ["A" * 64, "a" * 63, "g" * 64, ""])
@pytest.mark.parametrize("factory", [_raw_meta, _generated_meta])
def test_meta_rejects_noncanonical_hash(bad_hash: str, factory: object) -> None:
    with pytest.raises(ValidationError, match="^artifact metadata 无效$"):
        factory(content_hash=bad_hash)  # type: ignore[operator]


@pytest.mark.parametrize("bad_kind", [GeneratedArtifactKind.EMAIL_DRAFT, "pdf", None])
def test_raw_meta_rejects_generated_or_untyped_kind(bad_kind: object) -> None:
    with pytest.raises(ValidationError, match="^artifact metadata 无效$"):
        _raw_meta(kind=bad_kind)


@pytest.mark.parametrize("bad_kind", [RawArtifactKind.PDF, "email_draft", None])
def test_generated_meta_rejects_raw_or_untyped_kind(bad_kind: object) -> None:
    with pytest.raises(ValidationError, match="^artifact metadata 无效$"):
        _generated_meta(kind=bad_kind)


@pytest.mark.parametrize(
    "bad_time",
    [
        NOW.replace(tzinfo=None),
        datetime(2026, 8, 13, 14, 0, tzinfo=timezone(timedelta(hours=8))),
        "2026-08-13T06:00:00Z",
        None,
    ],
)
@pytest.mark.parametrize(
    ("factory", "field"),
    [(_raw_meta, "uploaded_at"), (_generated_meta, "generated_at")],
)
def test_meta_requires_actual_utc_datetime(
    bad_time: object, factory: object, field: str
) -> None:
    with pytest.raises(ValidationError, match="^artifact metadata 无效$"):
        factory(**{field: bad_time})  # type: ignore[operator]


@pytest.mark.parametrize("bad_size", [0, -1, True, 1.0, "1", None])
@pytest.mark.parametrize("factory", [_raw_meta, _generated_meta])
def test_meta_requires_positive_integer_size(bad_size: object, factory: object) -> None:
    with pytest.raises(ValidationError, match="^artifact metadata 无效$"):
        factory(size_bytes=bad_size)  # type: ignore[operator]


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("tenant_id", "tenant-a"),
        ("tenant_id", 3),
        ("artifact_id", "art_bad"),
        ("artifact_id", None),
        ("uploaded_by", "user-a"),
        ("uploaded_by", 3),
    ],
)
def test_raw_meta_rejects_invalid_typed_identifiers(
    field: str, bad_value: object
) -> None:
    with pytest.raises(ValidationError, match="^artifact metadata 无效$"):
        _raw_meta(**{field: bad_value})


def test_raw_meta_allows_missing_uploader() -> None:
    assert _raw_meta(uploaded_by=None).uploaded_by is None


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("tenant_id", "tenant-a"),
        ("artifact_id", "art_bad"),
        ("workflow_run_id", "run_bad"),
        ("workflow_run_id", None),
        ("subject_ref", "bad subject"),
        ("subject_ref", "enr_00000000000000000000000000\nsecret"),
        ("subject_ref", 3),
        ("idempotency_key", "bad key"),
        ("idempotency_key", "enr_00000000000000000000000000:1:draft\x00"),
        ("idempotency_key", None),
        ("generated_by", ""),
        ("generated_by", " agent"),
        ("generated_by", "agent\nsecret"),
        ("generated_by", 3),
    ],
)
def test_generated_meta_rejects_invalid_binding_or_producer(
    field: str, bad_value: object
) -> None:
    with pytest.raises(ValidationError, match="^artifact metadata 无效$"):
        _generated_meta(**{field: bad_value})


@pytest.mark.parametrize("bad_sequence", [0, -1, True, 1.0, "1", None])
def test_generated_meta_requires_positive_integer_sequence(
    bad_sequence: object,
) -> None:
    with pytest.raises(ValidationError, match="^artifact metadata 无效$"):
        _generated_meta(sequence_number=bad_sequence)


class _RawStore:
    async def put(
        self,
        tenant_id: TenantId,
        kind: RawArtifactKind,
        content: bytes,
        mime_type: str,
        uploaded_by: UserId | None = None,
    ) -> RawArtifactMeta:
        del tenant_id, kind, content, mime_type, uploaded_by
        return _raw_meta()

    async def get(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> tuple[RawArtifactMeta, bytes]:
        del tenant_id, artifact_id
        return _raw_meta(), b"raw"

    async def get_meta(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> RawArtifactMeta:
        del tenant_id, artifact_id
        return _raw_meta()


class _GeneratedStore:
    async def put(
        self,
        tenant_id: TenantId,
        kind: GeneratedArtifactKind,
        content: bytes,
        mime_type: str,
        *,
        workflow_run_id: RunId,
        subject_ref: str,
        sequence_number: int,
        idempotency_key: IdempotencyKey,
        generated_by: str,
    ) -> GeneratedArtifactMeta:
        del (
            tenant_id,
            kind,
            content,
            mime_type,
            workflow_run_id,
            subject_ref,
            sequence_number,
            idempotency_key,
            generated_by,
        )
        return _generated_meta()

    async def get(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> tuple[GeneratedArtifactMeta, bytes]:
        del tenant_id, artifact_id
        return _generated_meta(), b"generated"

    async def get_meta(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> GeneratedArtifactMeta:
        del tenant_id, artifact_id
        return _generated_meta()


class _BlobTransport:
    async def put(self, object_key: str, content: bytes) -> None:
        del object_key, content

    async def get(self, object_key: str) -> bytes:
        del object_key
        return b"content"

    async def delete(self, object_key: str) -> None:
        del object_key


def test_protocols_accept_their_runtime_shapes_and_remain_separate_contracts() -> None:
    assert isinstance(_RawStore(), RawArtifactStore)
    assert isinstance(_GeneratedStore(), GeneratedArtifactStore)
    assert isinstance(_BlobTransport(), ObjectBlobTransport)
    assert RawArtifactStore is not GeneratedArtifactStore


def test_protocol_put_signatures_keep_generated_bindings_keyword_only() -> None:
    raw_parameters = list(inspect.signature(RawArtifactStore.put).parameters.values())
    generated_parameters = list(
        inspect.signature(GeneratedArtifactStore.put).parameters.values()
    )
    transport_parameters = list(
        inspect.signature(ObjectBlobTransport.put).parameters.values()
    )
    assert [parameter.name for parameter in raw_parameters] == [
        "self",
        "tenant_id",
        "kind",
        "content",
        "mime_type",
        "uploaded_by",
    ]
    assert [parameter.name for parameter in generated_parameters] == [
        "self",
        "tenant_id",
        "kind",
        "content",
        "mime_type",
        "workflow_run_id",
        "subject_ref",
        "sequence_number",
        "idempotency_key",
        "generated_by",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in generated_parameters[5:]
    )
    assert [parameter.name for parameter in transport_parameters] == [
        "self",
        "object_key",
        "content",
    ]


def test_artifact_errors_have_fixed_safe_messages() -> None:
    assert str(ArtifactConflictError()) == "Artifact 幂等记录冲突"
    assert str(ArtifactNotFoundError()) == "Artifact 不存在"
    assert str(ArtifactIntegrityError()) == "Artifact 完整性校验失败"
