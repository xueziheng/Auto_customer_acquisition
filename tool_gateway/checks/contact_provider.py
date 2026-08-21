"""联系人 Provider 工具的 typed preflight、政策、抑制与配额检查。"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from domains.outreach.schemas import SuppressionTarget
from domains.prospecting.schemas import ContactPointView
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    NeedHypothesisId,
    ProspectAccountId,
    TenantId,
)
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.pipeline import (
    CheckRejection,
    ToolCallContext,
    ToolInvocationState,
)

_HYPOTHESIS_RE = re.compile(r"hyp_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_ACCOUNT_RE = re.compile(r"acc_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_CONTACT_POINT_RE = re.compile(r"cp_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_CAPABILITY_LIMITS = {
    "contact.enrich": (15, 500),
    "contact.verify": (10, 300),
}


@dataclass(frozen=True)
class ContactDiscoveryPreflight:
    tenant_id: TenantId
    hypothesis_id: NeedHypothesisId
    account_id: ProspectAccountId
    category: str
    country: str
    website_domain: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tenant_id, str)
            or not self.tenant_id
            or _HYPOTHESIS_RE.fullmatch(self.hypothesis_id) is None
            or _ACCOUNT_RE.fullmatch(self.account_id) is None
            or not _safe_text(self.category)
            or not _safe_text(self.country)
            or not _safe_text(self.website_domain)
        ):
            raise ValidationError("联系人发现 preflight 无效")


@dataclass(frozen=True, repr=False)
class ContactVerificationPreflight:
    tenant_id: TenantId
    contact_point: ContactPointView = field(repr=False)
    cache_valid: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tenant_id, str)
            or not self.tenant_id
            or not isinstance(self.contact_point, ContactPointView)
            or self.contact_point.tenant_id != self.tenant_id
            or not isinstance(self.cache_valid, bool)
        ):
            raise ValidationError("联系人验证 preflight 无效")


@runtime_checkable
class ContactDiscoveryPolicyReader(Protocol):
    async def preflight(
        self,
        tenant_id: TenantId,
        hypothesis_id: NeedHypothesisId,
        account_id: ProspectAccountId,
    ) -> ContactDiscoveryPreflight:
        raise NotImplementedError


@runtime_checkable
class ContactCountryPolicyReader(Protocol):
    async def allows_contact_enrichment(
        self,
        tenant_id: TenantId,
        country: str,
    ) -> bool:
        raise NotImplementedError


@runtime_checkable
class ContactPointPolicyReader(Protocol):
    async def get_contact_point(
        self,
        tenant_id: TenantId,
        contact_point_id: ContactPointId,
    ) -> ContactPointView:
        raise NotImplementedError


@runtime_checkable
class ContactSuppressionReader(Protocol):
    async def is_suppressed(
        self,
        tenant_id: TenantId,
        target: SuppressionTarget,
    ) -> bool:
        raise NotImplementedError


@runtime_checkable
class ProviderQuotaGuard(Protocol):
    async def reserve(
        self,
        tenant_id: TenantId,
        capability: str,
        now: datetime,
    ) -> int | None:
        """允许返回 None；拒绝返回 1..86400 秒 Retry-After。"""
        raise NotImplementedError


class ContactResourceTenantCheck:
    name = "tenant"

    async def check(
        self,
        ctx: ToolCallContext,
        state: ToolInvocationState,
    ) -> CheckRejection | None:
        del state
        valid = False
        if ctx.tool_id == "contact.enrich":
            hypothesis_id = ctx.params.get("hypothesis_id")
            account_id = ctx.params.get("account_id")
            valid = (
                isinstance(hypothesis_id, str)
                and _HYPOTHESIS_RE.fullmatch(hypothesis_id) is not None
                and isinstance(account_id, str)
                and _ACCOUNT_RE.fullmatch(account_id) is not None
            )
        elif ctx.tool_id == "contact.verify":
            contact_point_id = ctx.params.get("contact_point_id")
            valid = (
                isinstance(contact_point_id, str)
                and _CONTACT_POINT_RE.fullmatch(contact_point_id) is not None
            )
        if valid:
            return None
        return CheckRejection(
            self.name,
            "tenant:contact_resource_binding",
            "联系人工具资源绑定无效",
        )


class ContactEnrichmentPlaybookCheck:
    name = "playbook"

    def __init__(self, reader: ContactDiscoveryPolicyReader) -> None:
        if not isinstance(reader, ContactDiscoveryPolicyReader):
            raise ValidationError("联系人发现政策读取器无效")
        self._reader = reader

    async def check(
        self,
        ctx: ToolCallContext,
        state: ToolInvocationState,
    ) -> CheckRejection | None:
        hypothesis_value = ctx.params.get("hypothesis_id")
        account_value = ctx.params.get("account_id")
        if (
            not isinstance(hypothesis_value, str)
            or _HYPOTHESIS_RE.fullmatch(hypothesis_value) is None
            or not isinstance(account_value, str)
            or _ACCOUNT_RE.fullmatch(account_value) is None
        ):
            return _preflight_rejection(self.name)
        hypothesis_id = NeedHypothesisId(hypothesis_value)
        account_id = ProspectAccountId(account_value)
        try:
            preflight = await self._reader.preflight(
                ctx.tenant_id,
                hypothesis_id,
                account_id,
            )
        except Exception:  # noqa: BLE001 -- 外部读取异常必须脱敏并 fail closed
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT) from None
        if (
            not isinstance(preflight, ContactDiscoveryPreflight)
            or preflight.tenant_id != ctx.tenant_id
            or preflight.hypothesis_id != hypothesis_id
            or preflight.account_id != account_id
        ):
            return _preflight_rejection(self.name)
        state.preflight = preflight
        return None


class ContactCountryPolicyCheck:
    name = "country_policy"

    def __init__(self, reader: ContactCountryPolicyReader) -> None:
        if not isinstance(reader, ContactCountryPolicyReader):
            raise ValidationError("联系人国家政策读取器无效")
        self._reader = reader

    async def check(
        self,
        ctx: ToolCallContext,
        state: ToolInvocationState,
    ) -> CheckRejection | None:
        preflight = state.preflight
        if (
            not isinstance(preflight, ContactDiscoveryPreflight)
            or preflight.tenant_id != ctx.tenant_id
        ):
            return _preflight_rejection(self.name)
        try:
            allowed = await self._reader.allows_contact_enrichment(
                ctx.tenant_id,
                preflight.country,
            )
        except Exception:  # noqa: BLE001 -- 政策读取失败不得默认放行
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT) from None
        if allowed is True:
            return None
        if allowed is not False:
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT)
        return CheckRejection(
            self.name,
            "country_policy:contact_enrichment",
            "目标国家未配置允许联系人补全的政策包",
        )


class ContactProviderSuppressionCheck:
    name = "suppression"

    def __init__(
        self,
        point_reader: ContactPointPolicyReader,
        suppression_reader: ContactSuppressionReader,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            not isinstance(point_reader, ContactPointPolicyReader)
            or not isinstance(suppression_reader, ContactSuppressionReader)
            or not callable(now)
        ):
            raise ValidationError("联系人抑制检查依赖无效")
        self._point_reader = point_reader
        self._suppression_reader = suppression_reader
        self._now = now

    async def check(
        self,
        ctx: ToolCallContext,
        state: ToolInvocationState,
    ) -> CheckRejection | None:
        target: SuppressionTarget
        if ctx.tool_id == "contact.enrich":
            preflight = state.preflight
            if (
                not isinstance(preflight, ContactDiscoveryPreflight)
                or preflight.tenant_id != ctx.tenant_id
            ):
                return _preflight_rejection(self.name)
            target = SuppressionTarget(account_id=preflight.account_id)
        elif ctx.tool_id == "contact.verify":
            contact_value = ctx.params.get("contact_point_id")
            if (
                not isinstance(contact_value, str)
                or _CONTACT_POINT_RE.fullmatch(contact_value) is None
            ):
                return _preflight_rejection(self.name)
            contact_point_id = ContactPointId(contact_value)
            try:
                point = await self._point_reader.get_contact_point(
                    ctx.tenant_id,
                    contact_point_id,
                )
            except Exception:  # noqa: BLE001 -- 读取失败不得继续处理 PII
                raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT) from None
            if (
                not isinstance(point, ContactPointView)
                or point.tenant_id != ctx.tenant_id
                or point.contact_point_id != contact_point_id
                or getattr(point.kind, "value", None) != "email"
            ):
                return _preflight_rejection(self.name)
            now = self._now()
            if not _utc(now):
                raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT)
            checked_at = point.verification_checked_at
            cache_valid = (
                checked_at is not None
                and _utc(checked_at)
                and now < checked_at + timedelta(days=30)
            )
            state.preflight = ContactVerificationPreflight(
                ctx.tenant_id,
                point,
                cache_valid,
            )
            target = SuppressionTarget(contact_point_id=contact_point_id)
        else:
            return _preflight_rejection(self.name)
        try:
            suppressed = await self._suppression_reader.is_suppressed(
                ctx.tenant_id,
                target,
            )
        except Exception:  # noqa: BLE001 -- suppression 不可用时默认拒绝
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT) from None
        if suppressed is False:
            return None
        if suppressed is not True:
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT)
        return CheckRejection(
            self.name,
            "suppression:contact_provider",
            "企业或联系方式当前处于抑制状态",
        )


class ContactProviderRateLimitCheck:
    name = "rate_limit"

    def __init__(
        self,
        quota: ProviderQuotaGuard,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(quota, ProviderQuotaGuard) or not callable(now):
            raise ValidationError("联系人 Provider 配额依赖无效")
        self._quota = quota
        self._now = now

    async def check(
        self,
        ctx: ToolCallContext,
        state: ToolInvocationState,
    ) -> CheckRejection | None:
        if ctx.tool_id == "contact.verify":
            if not isinstance(state.preflight, ContactVerificationPreflight):
                return _preflight_rejection(self.name)
            if state.preflight.cache_valid:
                return None
        elif ctx.tool_id == "contact.enrich":
            if not isinstance(state.preflight, ContactDiscoveryPreflight):
                return _preflight_rejection(self.name)
        else:
            return _preflight_rejection(self.name)
        now = self._now()
        if not _utc(now):
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT)
        try:
            retry_after = await self._quota.reserve(
                ctx.tenant_id,
                ctx.tool_id,
                now,
            )
        except Exception:  # noqa: BLE001 -- 配额状态不明时不得调用付费 Provider
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT) from None
        if retry_after is None:
            return None
        if (
            not isinstance(retry_after, int)
            or isinstance(retry_after, bool)
            or not 1 <= retry_after <= 86_400
        ):
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT)
        raise ToolGatewayError(
            ToolErrorCategory.RATE_LIMITED,
            retry_after_seconds=retry_after,
        )


class InMemoryHunterQuotaGuard:
    """Phase 1 单 worker 的 Hunter 秒/分钟固定窗口保护，不声称跨进程一致。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._observations: dict[tuple[TenantId, str], list[datetime]] = {}

    async def reserve(
        self,
        tenant_id: TenantId,
        capability: str,
        now: datetime,
    ) -> int | None:
        if (
            not isinstance(tenant_id, str)
            or not tenant_id
            or capability not in _CAPABILITY_LIMITS
            or not _utc(now)
        ):
            raise ValidationError("Hunter 配额请求无效")
        second_limit, minute_limit = _CAPABILITY_LIMITS[capability]
        async with self._lock:
            key = (tenant_id, capability)
            observations = [
                observed
                for observed in self._observations.get(key, [])
                if observed > now - timedelta(minutes=1)
            ]
            recent_second = [
                observed
                for observed in observations
                if observed > now - timedelta(seconds=1)
            ]
            waits: list[int] = []
            if len(recent_second) >= second_limit:
                waits.append(_wait_seconds(recent_second[0], now, seconds=1))
            if len(observations) >= minute_limit:
                waits.append(_wait_seconds(observations[0], now, seconds=60))
            self._observations[key] = observations
            if waits:
                return max(waits)
            observations.append(now)
            return None


def _wait_seconds(observed: datetime, now: datetime, *, seconds: int) -> int:
    remaining = (observed + timedelta(seconds=seconds) - now).total_seconds()
    return max(1, math.ceil(remaining))


def _preflight_rejection(stage: str) -> CheckRejection:
    return CheckRejection(
        stage,
        f"{stage}:contact_preflight",
        "联系人工具前置事实未确认",
    )


def _safe_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value.encode("utf-8")) <= 500
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _utc(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
    )


__all__ = (
    "ContactCountryPolicyCheck",
    "ContactCountryPolicyReader",
    "ContactDiscoveryPolicyReader",
    "ContactDiscoveryPreflight",
    "ContactEnrichmentPlaybookCheck",
    "ContactPointPolicyReader",
    "ContactProviderRateLimitCheck",
    "ContactProviderSuppressionCheck",
    "ContactResourceTenantCheck",
    "ContactSuppressionReader",
    "ContactVerificationPreflight",
    "InMemoryHunterQuotaGuard",
    "ProviderQuotaGuard",
)
