"""prospecting 服务输入 canonicalization 与安全失败规则。"""

from __future__ import annotations

import importlib
import inspect
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Self

import pytest

from domains.prospecting.errors import (
    ContactPointNotFoundError,
    ProspectingConflictError,
)
from domains.prospecting.schemas import (
    VerificationRecordRequest,
    VerificationStatus,
)
from domains.prospecting.service import ProspectingService
from shared.errors import InvalidStateTransition, ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    ProspectAccountId,
    ProspectContactId,
    TenantId,
)

CHECKED_AT = datetime(2026, 8, 21, 9, tzinfo=UTC)
TENANT = TenantId("tenant-verification")
POINT_ID = ContactPointId("cp-verification")
CONTACT_ID = ProspectContactId("pc-verification")
ACCOUNT_ID = ProspectAccountId("acc-verification")

_models = importlib.import_module("domains.prospecting.models")
ContactPoint = _models.ContactPoint
ContactPointKind = _models.ContactPointKind
ContactType = _models.ContactType
LegalBasisRecord = _models.LegalBasisRecord
LegalBasisType = _models.LegalBasisType
ProspectContact = _models.ProspectContact
SubjectType = _models.SubjectType


def _module() -> ModuleType:
    return importlib.import_module("domains.prospecting.service_impl")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme.Example.", "acme.example"),
        ("例子.测试", "xn--fsqu00a.xn--0zwm56d"),
    ],
)
def test_canonical_domain(raw: str, expected: str) -> None:
    assert _module()._canonical_domain(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["https://example.com", "example.com/path", "example.com:443", "bad domain"],
)
def test_domain_requires_plain_host(raw: str) -> None:
    with pytest.raises(ValidationError, match="企业网站域名无效"):
        _module()._canonical_domain(raw)


def test_email_preserves_local_part_and_normalizes_domain() -> None:
    assert _module()._canonical_contact_value(
        _module().ContactPointKind.EMAIL, "Buyer.Name@Acme.Example"
    ) == "Buyer.Name@acme.example"


@pytest.mark.parametrize("raw", ["buyer example.com", "a@@example.com", " @example.com"])
def test_invalid_email_fails_closed(raw: str) -> None:
    with pytest.raises(ValidationError, match="邮箱地址无效"):
        _module()._canonical_contact_value(_module().ContactPointKind.EMAIL, raw)


def test_phone_requires_e164_shape() -> None:
    assert _module()._canonical_contact_value(
        _module().ContactPointKind.PHONE, "+491234567890"
    ) == "+491234567890"
    with pytest.raises(ValidationError, match="电话号码无效"):
        _module()._canonical_contact_value(_module().ContactPointKind.PHONE, "00491234")


class _StableHasher:
    def fingerprint(self, canonical_value: str) -> str:
        del canonical_value
        return "a" * 64


def _never_uow(_tenant_id: TenantId):
    raise AssertionError("输入校验失败时不得打开事务")


def _assert_new_verification_contract() -> None:
    parameters = tuple(
        inspect.signature(ProspectingService.record_verification).parameters
    )
    assert parameters == ("self", "tenant_id", "request")


@pytest.mark.parametrize(
    ("result", "provider", "message"),
    [
        ("verified", "provider-v1", "验证结果无效"),
        (VerificationStatus.VERIFIED, "", "验证服务标识无效"),
        (VerificationStatus.VERIFIED, " padded ", "验证服务标识无效"),
    ],
)
async def test_record_verification_rejects_invalid_input_before_uow(
    result: object, provider: str, message: str
) -> None:
    _assert_new_verification_contract()
    service = _module().ProspectingServiceImpl(_never_uow, _StableHasher())
    request = VerificationRecordRequest(
        POINT_ID,
        result,  # type: ignore[arg-type]
        provider,
        CHECKED_AT,
        "hunter.email_verifier.counted",
    )
    with pytest.raises(ValidationError, match=message):
        await service.record_verification(TenantId("tenant-verify-validation"), request)


async def test_record_verification_rejects_non_utc_checked_at_before_uow() -> None:
    _assert_new_verification_contract()
    naive_now = datetime(2026, 8, 20, 12)  # noqa: DTZ001 - 验证拒绝 naive 时钟
    service = _module().ProspectingServiceImpl(_never_uow, _StableHasher())
    request = VerificationRecordRequest(
        POINT_ID,
        VerificationStatus.VERIFIED,
        "provider-v1",
        naive_now,
        "hunter.email_verifier.counted",
    )
    with pytest.raises(ValidationError, match="验证观察时间必须为 UTC"):
        await service.record_verification(TenantId("tenant-verify-clock"), request)


def _verification_point(**overrides: object) -> ContactPoint:
    values: dict[str, object] = {
        "contact_point_id": POINT_ID,
        "tenant_id": TENANT,
        "contact_id": CONTACT_ID,
        "kind": ContactPointKind.EMAIL,
        "value": "buyer@example.com",
        "value_hash": "a" * 64,
        "legal_basis": LegalBasisRecord(
            basis=LegalBasisType.LEGITIMATE_INTEREST,
            subject_type=SubjectType.LEGAL_ENTITY,
            contact_type=ContactType.PERSONAL_BUSINESS,
            source="company_website",
            collected_at=CHECKED_AT - timedelta(days=10),
            assessment_ref="lia-verification",
        ),
        "created_at": CHECKED_AT - timedelta(days=10),
    }
    values.update(overrides)
    return ContactPoint(**values)  # type: ignore[arg-type]


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)


class _FakeContacts:
    def __init__(self, point: ContactPoint | None) -> None:
        self.point = point
        self.contact = ProspectContact(
            contact_id=CONTACT_ID,
            tenant_id=TENANT,
            account_id=ACCOUNT_ID,
            created_at=CHECKED_AT - timedelta(days=10),
        )
        self.update_count = 0

    async def get_contact_point_for_update(
        self, tenant_id: TenantId, contact_point_id: ContactPointId
    ) -> ContactPoint | None:
        return await self.get_contact_point(tenant_id, contact_point_id)

    async def get_contact_point(
        self, tenant_id: TenantId, contact_point_id: ContactPointId
    ) -> ContactPoint | None:
        if (
            self.point is None
            or tenant_id != self.point.tenant_id
            or contact_point_id != self.point.contact_point_id
        ):
            return None
        return self.point

    async def get_contact(
        self, tenant_id: TenantId, contact_id: ProspectContactId
    ) -> ProspectContact | None:
        if tenant_id != self.contact.tenant_id or contact_id != self.contact.contact_id:
            return None
        return self.contact

    async def update_contact_point(self, point: ContactPoint) -> None:
        self.update_count += 1
        self.point = point


class _FakeUow:
    def __init__(self, point: ContactPoint | None) -> None:
        self.contacts = _FakeContacts(point)
        self.bus = _FakeBus()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        del exc_type, exc, tb


def _verification_service(point: ContactPoint | None):
    uow = _FakeUow(point)
    service = _module().ProspectingServiceImpl(
        lambda _tenant: uow,  # type: ignore[arg-type]
        _StableHasher(),
    )
    return service, uow


async def test_record_unknown_persists_complete_observation() -> None:
    _assert_new_verification_contract()
    service, _uow = _verification_service(_verification_point())
    await service.record_verification(
        TENANT,
        VerificationRecordRequest(
            POINT_ID,
            VerificationStatus.UNVERIFIED,
            "hunter",
            CHECKED_AT,
            "hunter.email_verifier.unknown",
        ),
    )
    view = await service.get_contact_point(TENANT, POINT_ID)
    assert (
        view.verification,
        view.verification_provider,
        view.verification_checked_at,
        view.verification_cost_note,
    ) == (
        VerificationStatus.UNVERIFIED,
        "hunter",
        CHECKED_AT,
        "hunter.email_verifier.unknown",
    )
    assert view.account_id == ACCOUNT_ID


async def test_older_verification_result_cannot_overwrite_newer() -> None:
    _assert_new_verification_contract()
    current = _verification_point(
        verification=VerificationStatus.INVALID,
        verification_provider="hunter",
        verification_checked_at=CHECKED_AT,
        verification_cost_note="hunter.email_verifier.counted",
    )
    service, uow = _verification_service(current)
    with pytest.raises(InvalidStateTransition, match="验证结果早于当前观察"):
        await service.record_verification(
            TENANT,
            VerificationRecordRequest(
                POINT_ID,
                VerificationStatus.VERIFIED,
                "hunter",
                CHECKED_AT - timedelta(seconds=1),
                "hunter.email_verifier.counted",
            ),
        )
    assert uow.contacts.point == current
    assert uow.contacts.update_count == 0
    assert uow.bus.events == []


async def test_same_timestamp_different_result_is_conflict() -> None:
    _assert_new_verification_contract()
    current = _verification_point(
        verification=VerificationStatus.INVALID,
        verification_provider="hunter",
        verification_checked_at=CHECKED_AT,
        verification_cost_note="hunter.email_verifier.counted",
    )
    service, uow = _verification_service(current)
    with pytest.raises(ProspectingConflictError, match="验证观察时间冲突"):
        await service.record_verification(
            TENANT,
            VerificationRecordRequest(
                POINT_ID,
                VerificationStatus.RISKY,
                "hunter",
                CHECKED_AT,
                "hunter.email_verifier.counted",
            ),
        )
    assert uow.contacts.point == current
    assert uow.contacts.update_count == 0
    assert uow.bus.events == []


async def test_same_complete_request_is_idempotent() -> None:
    _assert_new_verification_contract()
    current = _verification_point(
        verification=VerificationStatus.RISKY,
        verification_provider="hunter",
        verification_checked_at=CHECKED_AT,
        verification_cost_note="hunter.email_verifier.counted",
    )
    service, uow = _verification_service(current)
    await service.record_verification(
        TENANT,
        VerificationRecordRequest(
            POINT_ID,
            VerificationStatus.RISKY,
            "hunter",
            CHECKED_AT,
            "hunter.email_verifier.counted",
        ),
    )
    assert uow.contacts.update_count == 0
    assert uow.bus.events == []


async def test_reverification_of_verified_point_updates_checked_at_without_second_event() -> None:
    _assert_new_verification_contract()
    first_verified_at = CHECKED_AT - timedelta(days=2)
    current = _verification_point(
        verification=VerificationStatus.VERIFIED,
        verified_at=first_verified_at,
        verification_provider="hunter",
        verification_checked_at=first_verified_at,
        verification_cost_note="hunter.email_verifier.counted",
    )
    service, uow = _verification_service(current)
    await service.record_verification(
        TENANT,
        VerificationRecordRequest(
            POINT_ID,
            VerificationStatus.VERIFIED,
            "hunter",
            CHECKED_AT,
            "hunter.email_verifier.counted",
        ),
    )
    assert uow.contacts.point is not None
    assert uow.contacts.point.verified_at == first_verified_at
    assert uow.contacts.point.verification_checked_at == CHECKED_AT
    assert uow.contacts.update_count == 1
    assert uow.bus.events == []


async def test_cross_tenant_get_contact_point_is_not_found() -> None:
    _assert_new_verification_contract()
    point = _verification_point()
    service, _uow = _verification_service(point)
    errors: list[str] = []
    for tenant_id, point_id in (
        (TenantId("tenant-other"), POINT_ID),
        (TENANT, ContactPointId("cp-missing")),
    ):
        with pytest.raises(
            ContactPointNotFoundError, match="潜在联系方式不存在"
        ) as error:
            await service.get_contact_point(tenant_id, point_id)
        errors.append(str(error.value))
    assert errors == ["潜在联系方式不存在", "潜在联系方式不存在"]


@pytest.mark.parametrize(
    "raw",
    [" padded@example.com ", "not-an-address", "004912345678"],
)
async def test_erasure_rejects_invalid_value_before_uow(raw: str) -> None:
    service = _module().ProspectingServiceImpl(_never_uow, _StableHasher())
    with pytest.raises(ValidationError):
        await service.handle_erasure_request(TenantId("tenant-erase-invalid"), raw)


class _InvalidHasher:
    def fingerprint(self, canonical_value: str) -> str:
        del canonical_value
        return "not-a-safe-hash"


async def test_erasure_rejects_invalid_hash_before_uow() -> None:
    service = _module().ProspectingServiceImpl(_never_uow, _InvalidHasher())
    with pytest.raises(ValidationError, match="联系方式指纹无效"):
        await service.handle_erasure_request(
            TenantId("tenant-erase-hash"), "privacy@example.com"
        )


class _LeakyFailingHasher:
    def fingerprint(self, canonical_value: str) -> str:
        raise RuntimeError(canonical_value)


async def test_erasure_redacts_hasher_failure() -> None:
    raw_value = "private.marker@example.com"
    service = _module().ProspectingServiceImpl(_never_uow, _LeakyFailingHasher())
    with pytest.raises(ValidationError, match="联系方式指纹计算失败") as error:
        await service.handle_erasure_request(
            TenantId("tenant-erase-hasher-failure"), raw_value
        )
    assert raw_value not in str(error.value)
