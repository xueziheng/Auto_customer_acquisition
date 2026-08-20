"""prospecting 服务输入 canonicalization 与安全失败规则。"""

from __future__ import annotations

import importlib
from types import ModuleType

import pytest

from shared.errors import ValidationError


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
