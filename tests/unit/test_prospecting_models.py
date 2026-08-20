"""prospecting 纯域门禁。"""

from __future__ import annotations

import importlib
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from domains.prospecting.errors import MissingAssessmentRefError
from shared.errors import ValidationError
from shared.schemas.identifiers import ContactPointId, ProspectContactId, TenantId

_models = importlib.import_module("domains.prospecting.models")
ContactPoint = _models.ContactPoint
ContactPointKind = _models.ContactPointKind
ContactType = _models.ContactType
LegalBasisRecord = _models.LegalBasisRecord
LegalBasisType = _models.LegalBasisType
SubjectType = _models.SubjectType
VerificationStatus = _models.VerificationStatus

NOW = datetime(2026, 8, 20, tzinfo=UTC)


def _basis(**overrides: object) -> Any:
    values: dict[str, object] = {
        "basis": LegalBasisType.LEGITIMATE_INTEREST,
        "subject_type": SubjectType.LEGAL_ENTITY,
        "contact_type": ContactType.ROLE_BASED,
        "source": "company_website",
        "source_url": "https://example.com/contact",
        "collected_at": NOW,
        "assessment_ref": "lia_2026_001",
    }
    values.update(overrides)
    return LegalBasisRecord(**values)  # type: ignore[arg-type]


def _point(**overrides: object) -> Any:
    values: dict[str, object] = {
        "contact_point_id": ContactPointId("cp_01K00000000000000000000000"),
        "tenant_id": TenantId("tenant-1"),
        "contact_id": ProspectContactId("pc_01K00000000000000000000000"),
        "kind": ContactPointKind.EMAIL,
        "value": "sales@example.com",
        "value_hash": "a" * 64,
        "legal_basis": _basis(),
        "created_at": NOW,
    }
    values.update(overrides)
    return ContactPoint(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (VerificationStatus.UNVERIFIED, False),
        (VerificationStatus.VERIFIED, True),
        (VerificationStatus.RISKY, False),
        (VerificationStatus.INVALID, False),
    ],
)
def test_only_verified_contact_point_may_enter_sequence(
    status: object, expected: bool
) -> None:
    point = _point(
        verification=status,
        verified_at=NOW if status is VerificationStatus.VERIFIED else None,
        verification_provider=(
            None if status is VerificationStatus.UNVERIFIED else "provider-v1"
        ),
    )
    assert point.may_enter_sequence() is expected


def test_legitimate_interest_requires_assessment_reference() -> None:
    with pytest.raises(MissingAssessmentRefError, match="正当利益依据缺少评估引用"):
        _basis(assessment_ref=None)


@pytest.mark.parametrize("field", ["source", "source_url", "assessment_ref"])
def test_legal_basis_rejects_blank_or_padded_fields(field: str) -> None:
    with pytest.raises(ValidationError, match="法律依据字段无效"):
        _basis(**{field: " padded "})


def test_legal_basis_requires_utc_collected_at() -> None:
    with pytest.raises(ValidationError, match="法律依据时间必须为 UTC"):
        _basis(collected_at=NOW.replace(tzinfo=None))


def test_verification_shape_is_fail_closed() -> None:
    with pytest.raises(ValidationError, match="联系方式验证状态无效"):
        _point(
            verification=VerificationStatus.VERIFIED,
            verified_at=None,
            verification_provider="provider-v1",
        )
    with pytest.raises(ValidationError, match="联系方式验证状态无效"):
        _point(
            verification=VerificationStatus.RISKY,
            verified_at=NOW,
            verification_provider="provider-v1",
        )


def test_contact_point_rejects_invalid_kind_and_hash() -> None:
    with pytest.raises(ValidationError, match="联系方式类型无效"):
        _point(kind="social")
    with pytest.raises(ValidationError, match="联系方式指纹无效"):
        _point(value_hash="address@example.com")


def test_dataclass_replace_cannot_bypass_verification_gate() -> None:
    point = _point()
    with pytest.raises(ValidationError, match="联系方式验证状态无效"):
        replace(point, verification=VerificationStatus.VERIFIED)
