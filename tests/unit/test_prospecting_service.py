"""prospecting 服务输入 canonicalization 与安全失败规则。"""

from __future__ import annotations

import importlib
from types import ModuleType

import pytest

from domains.prospecting.schemas import VerificationStatus
from shared.errors import ValidationError
from shared.schemas.identifiers import ContactPointId, TenantId


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
    service = _module().ProspectingServiceImpl(_never_uow, _StableHasher())
    with pytest.raises(ValidationError, match=message):
        await service.record_verification(
            TenantId("tenant-verify-validation"),
            ContactPointId("cp-verify-validation"),
            result,  # type: ignore[arg-type]
            provider,
        )


async def test_record_verification_rejects_non_utc_clock_before_uow() -> None:
    from datetime import datetime

    naive_now = datetime(2026, 8, 20, 12)  # noqa: DTZ001 - 验证拒绝 naive 时钟
    service = _module().ProspectingServiceImpl(
        _never_uow,
        _StableHasher(),
        now=lambda: naive_now,
    )
    with pytest.raises(ValidationError, match="服务时钟必须为 UTC"):
        await service.record_verification(
            TenantId("tenant-verify-clock"),
            ContactPointId("cp-verify-clock"),
            VerificationStatus.VERIFIED,
            "provider-v1",
        )


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
