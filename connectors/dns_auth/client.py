"""唯一 DNS 认证外部适配器；原始响应和 provider 异常止于本模块。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Protocol

import dns.asyncresolver
import dns.exception
import dns.resolver
import tldextract

from shared.errors import TransientError, ValidationError
from shared.schemas.dns_auth import (
    DnsAuthenticationFacts,
    DnsAuthenticationFailure,
    DnsAuthenticationFailureCategory,
    DnsAuthenticationRequest,
)

_MAX_RECORDS = 16
_MAX_RECORD_BYTES = 4096
_MAX_TOTAL_BYTES = 8192
_SPF_MACRO_EXPAND = r"%\{(?i:[slodipvh][0-9]*r?[.\-+,/_=]*)\}"
_PUBLIC_SUFFIX = tldextract.TLDExtract(
    suffix_list_urls=(),
    cache_dir=None,
    include_psl_private_domains=True,
)


class AsyncTxtResolver(Protocol):
    async def resolve(self, name: str, rdtype: str) -> object: ...


class DnsPythonAsyncResolver:
    """生产 dnspython async resolver；provider 对象不越过 Connector。"""

    def __init__(self) -> None:
        self._resolver = dns.asyncresolver.Resolver()

    async def resolve(self, name: str, rdtype: str) -> object:
        return await self._resolver.resolve(name, rdtype)


def _failure(
    check: str, category: DnsAuthenticationFailureCategory
) -> DnsAuthenticationFailure:
    return DnsAuthenticationFailure(check, category, f"configure_{check}")


def _txt_records(answer: object) -> list[str] | None:
    if not isinstance(answer, Iterable):
        return None
    try:
        items = list(answer)
    except (TypeError, ValueError):
        return None
    if len(items) > _MAX_RECORDS:
        return None
    total = 0
    records: list[str] = []
    for item in items:
        chunks = getattr(item, "strings", None)
        if not isinstance(chunks, tuple):
            return None
        raw = bytearray()
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                return None
            raw.extend(chunk)
            if len(raw) > _MAX_RECORD_BYTES:
                return None
        total += len(raw)
        if total > _MAX_TOTAL_BYTES:
            return None
        try:
            records.append(bytes(raw).decode("ascii"))
        except UnicodeDecodeError:
            return None
    return records


def _valid_macro_string(value: str) -> bool:
    index = 0
    while index < len(value):
        character = value[index]
        if character == "%":
            if index + 1 >= len(value):
                return False
            marker = value[index + 1]
            if marker in {"%", "_", "-"}:
                index += 2
                continue
            if marker != "{":
                return False
            closing = value.find("}", index + 2)
            if closing < 0 or re.fullmatch(
                r"(?i:[slodipvh][0-9]*r?[.\-+,/_=]*)",
                value[index + 2 : closing],
            ) is None:
                return False
            index = closing + 1
            continue
        if character in "{}" or re.fullmatch(r"[A-Za-z0-9_.+-]", character) is None:
            return False
        index += 1
    return True


def _valid_domain_spec(value: str) -> bool:
    if not 1 <= len(value) <= 253 or ".." in value or not _valid_macro_string(value):
        return False
    if re.search(rf"{_SPF_MACRO_EXPAND}\Z", value) is not None:
        return True
    without_final_dot = value.removesuffix(".")
    _, separator, top_label = without_final_dot.rpartition(".")
    if not separator or not 1 <= len(top_label) <= 63:
        return False
    if "-" not in top_label:
        return (
            top_label.isascii()
            and top_label.isalnum()
            and any(character.isalpha() for character in top_label)
        )
    return re.fullmatch(r"[A-Za-z0-9]+-[A-Za-z0-9-]*[A-Za-z0-9]", top_label) is not None


def _valid_cidr_token(value: str, maximum: int) -> bool:
    return (
        re.fullmatch(r"(?:0|[1-9][0-9]{0,2})", value) is not None
        and int(value) <= maximum
    )


def _valid_spf(policy: str) -> bool:
    if (
        not policy
        or policy.startswith(" ")
        or policy.endswith(" ")
        or any(character != " " and not 33 <= ord(character) <= 126 for character in policy)
    ):
        return False
    terms = [term for term in policy.split(" ") if term]
    if not terms or terms[0].lower() != "v=spf1":
        return False
    modifiers: set[str] = set()
    for raw_term in terms[1:]:
        qualifier = raw_term[:1] if raw_term[:1] in "+-~?" else ""
        term = raw_term[1:] if qualifier else raw_term
        if not term:
            return False
        modifier_match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_.-]*)=(.*)", term)
        if modifier_match is not None:
            if qualifier:
                return False
            name, value = modifier_match.groups()
            name = name.lower()
            if name in {"redirect", "exp"}:
                if not _valid_domain_spec(value) or name in modifiers:
                    return False
                modifiers.add(name)
            elif not _valid_macro_string(value):
                return False
            continue
        lowered = term.lower()
        if lowered == "all":
            continue
        a_or_mx = re.match(r"(?i:(a|mx))(?=[:/]|$)", term)
        if a_or_mx is not None:
            remainder = term[a_or_mx.end() :]
            domain: str | None = None
            if remainder.startswith(":"):
                domain_and_cidr = remainder[1:]
                cidr_start = len(domain_and_cidr)
                index = 0
                while index < len(domain_and_cidr):
                    if domain_and_cidr.startswith("%{", index):
                        closing = domain_and_cidr.find("}", index + 2)
                        if closing < 0:
                            break
                        index = closing + 1
                        continue
                    if domain_and_cidr[index] == "/":
                        cidr_start = index
                        break
                    index += 1
                domain = domain_and_cidr[:cidr_start]
                remainder = domain_and_cidr[cidr_start:]
            cidrs = re.fullmatch(
                r"(?:/([0-9]{1,3}))?(?://([0-9]{1,3}))?", remainder
            )
            if cidrs is None:
                return False
            ipv4_cidr, ipv6_cidr = cidrs.groups()
            if (
                (domain is not None and not _valid_domain_spec(domain))
                or (
                    ipv4_cidr is not None
                    and not _valid_cidr_token(ipv4_cidr, 32)
                )
                or (
                    ipv6_cidr is not None
                    and not _valid_cidr_token(ipv6_cidr, 128)
                )
            ):
                return False
            continue
        mechanism, separator, argument = term.partition(":")
        mechanism = mechanism.lower()
        if mechanism in {"include", "exists"}:
            if not separator or not _valid_domain_spec(argument):
                return False
        elif mechanism == "ip4":
            if not separator:
                return False
            if "/" in argument and not _valid_cidr_token(
                argument.rsplit("/", 1)[1], 32
            ):
                return False
            try:
                if ipaddress.ip_network(argument, strict=False).version != 4:
                    return False
            except ValueError:
                return False
        elif mechanism == "ip6":
            if not separator:
                return False
            if "/" in argument and not _valid_cidr_token(
                argument.rsplit("/", 1)[1], 128
            ):
                return False
            try:
                if ipaddress.ip_network(argument, strict=False).version != 6:
                    return False
            except ValueError:
                return False
        elif mechanism == "ptr":
            if separator and not _valid_domain_spec(argument):
                return False
        else:
            return False
    return True


def _tag_items(
    value: str,
    *,
    allow_fws_in: frozenset[str] = frozenset(),
    case_sensitive_names: bool = False,
) -> tuple[tuple[str, str], ...] | None:
    parts = [part.strip() for part in value.split(";")]
    if parts and not parts[-1]:
        parts.pop()
    if not parts:
        return None
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in parts:
        if "=" not in part:
            return None
        name, tag_value = (item.strip() for item in part.split("=", 1))
        if not case_sensitive_names:
            name = name.lower()
        invalid_character = any(
            not (
                (name in allow_fws_in and character in " \t")
                or 33 <= ord(character) <= 126
            )
            for character in tag_value
        )
        if (
            re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", name) is None
            or not tag_value
            or len(tag_value) > 2048
            or invalid_character
            or name in seen
        ):
            return None
        seen.add(name)
        items.append((name, tag_value))
    return tuple(items)


def _classify(check: str, records: list[str] | None) -> DnsAuthenticationFailure | None:
    if records is None:
        return _failure(check, DnsAuthenticationFailureCategory.MALFORMED)
    if check == "spf":
        candidates = [
            record
            for record in records
            if re.match(r"(?i:v=spf1)(?: |$)", record) is not None
        ]
        if not candidates:
            return _failure(check, DnsAuthenticationFailureCategory.MISSING)
        if len(candidates) != 1 or not _valid_spf(candidates[0]):
            return _failure(check, DnsAuthenticationFailureCategory.MALFORMED)
        value = candidates[0]
        if re.search(r"(?:^| )[+?]?all(?: |$)", value, re.IGNORECASE):
            return _failure(check, DnsAuthenticationFailureCategory.POLICY_UNSAFE)
    elif check == "dkim":
        if len(records) != 1:
            return _failure(check, DnsAuthenticationFailureCategory.MALFORMED)
        items = _tag_items(
            records[0],
            allow_fws_in=frozenset({"p"}),
            case_sensitive_names=True,
        )
        if items is None:
            return _failure(check, DnsAuthenticationFailureCategory.MALFORMED)
        if items[0][0] == "v":
            if items[0][1] != "DKIM1":
                return _failure(check, DnsAuthenticationFailureCategory.MALFORMED)
            items = items[1:]
        elif any(name == "v" for name, _ in items):
            return _failure(check, DnsAuthenticationFailureCategory.MALFORMED)
        tags = dict(items)
        if tags.get("k", "rsa").lower() != "rsa" or "p" not in tags:
            return _failure(check, DnsAuthenticationFailureCategory.MALFORMED)
        try:
            public_key = base64.b64decode(
                tags["p"].replace(" ", "").replace("\t", ""), validate=True
            )
        except (binascii.Error, ValueError):
            return _failure(check, DnsAuthenticationFailureCategory.MALFORMED)
        if len(public_key) < 16:
            return _failure(check, DnsAuthenticationFailureCategory.MALFORMED)
    else:
        if len(records) != 1:
            return _failure(check, DnsAuthenticationFailureCategory.MALFORMED)
        items = _tag_items(records[0])
        if (
            items is None
            or len(items) < 2
            or items[0] != ("v", "DMARC1")
            or items[1][0] != "p"
        ):
            return _failure(check, DnsAuthenticationFailureCategory.MALFORMED)
        tags = dict(items)
        if tags["p"] not in {"none", "quarantine", "reject"}:
            return _failure(check, DnsAuthenticationFailureCategory.MALFORMED)
        if tags["p"] == "none":
            return _failure(check, DnsAuthenticationFailureCategory.POLICY_UNSAFE)
    return None


class DnsAuthenticationConnector:
    """用注入 resolver 查询三个固定 TXT 名称并输出安全事实。"""

    def __init__(
        self,
        resolver: AsyncTxtResolver,
        *,
        now: Callable[[], datetime],
    ) -> None:
        if not callable(getattr(resolver, "resolve", None)) or not callable(now):
            raise ValidationError("DNS Connector 依赖无效")
        self._resolver = resolver
        self._now = now

    def validate_request(self, request: DnsAuthenticationRequest) -> None:
        if not isinstance(request, DnsAuthenticationRequest):
            raise ValidationError("DNS 认证请求无效")
        extracted = _PUBLIC_SUFFIX(request.domain)
        if not extracted.suffix or not extracted.domain:
            raise ValidationError("DNS 认证请求无效")

    async def check(self, request: DnsAuthenticationRequest) -> DnsAuthenticationFacts:
        self.validate_request(request)
        names = (
            ("spf", request.domain),
            ("dkim", f"{request.dkim_selector}._domainkey.{request.domain}"),
            ("dmarc", f"_dmarc.{request.domain}"),
        )
        failures: list[DnsAuthenticationFailure] = []
        for check, name in names:
            try:
                answer = await self._resolver.resolve(name, "TXT")
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
                failures.append(
                    _failure(check, DnsAuthenticationFailureCategory.MISSING)
                )
                continue
            except (dns.exception.Timeout, OSError):
                raise TransientError("DNS resolver 暂时不可用") from None
            except Exception:  # noqa: BLE001 - provider 异常必须在 Connector 内脱敏
                raise TransientError("DNS resolver 暂时不可用") from None
            failure = _classify(check, _txt_records(answer))
            if failure is not None:
                failures.append(failure)
        now = self._now()
        if not isinstance(now, datetime) or now.tzinfo is not UTC:
            raise ValidationError("DNS Connector 时钟无效")
        failed = {item.check for item in failures}
        digest = hashlib.sha256(
            f"{request.domain}\0{request.dkim_selector}\0{now.isoformat()}\0"
            f"{','.join(f'{item.check}:{item.category.value}' for item in failures)}".encode()
        ).hexdigest()
        return DnsAuthenticationFacts(
            now,
            "spf" not in failed,
            "dkim" not in failed,
            "dmarc" not in failed,
            tuple(failures),
            digest,
        )
