"""prospecting 浅域服务：确定性消歧、录入与合规门禁。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from domains.prospecting.errors import (
    ContactPointNotFoundError,
    ErasedContactPointError,
    ProspectAccountNotFoundError,
    ProspectContactNotFoundError,
    ProspectingConflictError,
)
from domains.prospecting.models import (
    ContactPoint,
    ContactPointKind,
    LegalBasisRecord,
    ProspectAccount,
    ProspectContact,
    VerificationStatus,
)
from domains.prospecting.repository import ProspectingUnitOfWork
from domains.prospecting.schemas import (
    AccountResolveRequest,
    ContactCreateRequest,
    ContactPointCreateRequest,
    ContactPointDetailView,
    ContactPointView,
    DiscoveredContactRequest,
    DiscoveredContactResult,
    ProspectAccountDetailView,
    ProspectAccountView,
    ProspectContactDetailView,
    ProspectContactView,
    VerificationRecordRequest,
)
from domains.prospecting.service import ContactValueHasher
from shared.errors import InvalidStateTransition, TransientError, ValidationError
from shared.events.catalog import ContactPointVerified
from shared.schemas.identifiers import (
    ContactPointId,
    ProspectAccountId,
    ProspectContactId,
    TenantId,
    new_id,
)

_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_HASH = re.compile(r"[0-9a-f]{64}")
_PHONE = re.compile(r"\+[1-9][0-9]{7,14}")


def _require_text(value: str, message: str) -> None:
    if not value or value != value.strip():
        raise ValidationError(message)


def _optional_text(value: str | None, message: str) -> None:
    if value is not None:
        _require_text(value, message)


def _utc_now(now: Callable[[], datetime]) -> datetime:
    value = now()
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValidationError("服务时钟必须为 UTC")
    return value


def _canonical_domain(raw: str) -> str:
    _require_text(raw, "企业网站域名无效")
    if any(marker in raw for marker in (":", "/", "?", "#", "@")):
        raise ValidationError("企业网站域名无效")
    host = raw.removesuffix(".")
    try:
        canonical = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValidationError("企业网站域名无效") from exc
    labels = canonical.split(".")
    if (
        len(canonical) > 253
        or len(labels) < 2
        or any(_DOMAIN_LABEL.fullmatch(label) is None for label in labels)
    ):
        raise ValidationError("企业网站域名无效")
    return canonical


def _canonical_contact_value(kind: ContactPointKind, raw: str) -> str:
    if kind is ContactPointKind.PHONE:
        _require_text(raw, "电话号码无效")
        if _PHONE.fullmatch(raw) is None:
            raise ValidationError("电话号码无效")
        return raw
    _require_text(raw, "邮箱地址无效")
    if kind is not ContactPointKind.EMAIL or raw.count("@") != 1:
        raise ValidationError("邮箱地址无效")
    local, domain = raw.rsplit("@", 1)
    if (
        not local
        or len(local) > 64
        or any(char.isspace() for char in local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
    ):
        raise ValidationError("邮箱地址无效")
    try:
        canonical_domain = _canonical_domain(domain)
    except ValidationError as exc:
        raise ValidationError("邮箱地址无效") from exc
    value = f"{local}@{canonical_domain}"
    if len(value) > 320:
        raise ValidationError("邮箱地址无效")
    return value


def _fingerprint_contact_value(
    hasher: ContactValueHasher, canonical_value: str
) -> str:
    try:
        value_hash = hasher.fingerprint(canonical_value)
    except Exception:  # noqa: BLE001 - 阻断底层异常回显原始个人数据
        raise ValidationError("联系方式指纹计算失败") from None
    if not isinstance(value_hash, str) or _HASH.fullmatch(value_hash) is None:
        raise ValidationError("联系方式指纹无效")
    return value_hash


def _account_view(account: ProspectAccount) -> ProspectAccountView:
    return ProspectAccountView(
        account_id=account.account_id,
        tenant_id=account.tenant_id,
        name=account.name,
        country=account.country,
        created_at=account.created_at,
        website_domain=account.website_domain,
        entity_type=account.entity_type,
        industry=account.industry,
        size_hint=account.size_hint,
        source_signal_refs=tuple(account.source_signal_refs),
    )


def _contact_point_view(
    point: ContactPoint, account_id: ProspectAccountId
) -> ContactPointView:
    return ContactPointView(
        contact_point_id=point.contact_point_id,
        tenant_id=point.tenant_id,
        contact_id=point.contact_id,
        account_id=account_id,
        kind=point.kind,
        value=point.value,
        verification=point.verification,
        created_at=point.created_at,
        verified_at=point.verified_at,
        verification_provider=point.verification_provider,
        verification_checked_at=point.verification_checked_at,
        verification_cost_note=point.verification_cost_note,
        enrichment_cost_note=point.enrichment_cost_note,
    )


def _contact_view(contact: ProspectContact) -> ProspectContactView:
    return ProspectContactView(
        contact_id=contact.contact_id,
        tenant_id=contact.tenant_id,
        account_id=contact.account_id,
        created_at=contact.created_at,
        full_name=contact.full_name,
        role_title=contact.role_title,
        language=contact.language,
    )


def _contact_point_detail(
    point: ContactPoint, account_id: ProspectAccountId
) -> ContactPointDetailView:
    basis = point.legal_basis
    return ContactPointDetailView(
        contact_point=_contact_point_view(point, account_id),
        legal_basis=basis.basis,
        subject_type=basis.subject_type,
        contact_type=basis.contact_type,
        legal_basis_source=basis.source,
        collected_at=basis.collected_at,
        source_url=basis.source_url,
        assessment_ref=basis.assessment_ref,
    )


class ProspectingServiceImpl:
    def __init__(
        self,
        uow_factory: Callable[[TenantId], ProspectingUnitOfWork],
        contact_value_hasher: ContactValueHasher,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._hasher = contact_value_hasher
        self._now = now or (lambda: datetime.now(UTC))

    async def resolve_account(
        self, tenant_id: TenantId, request: AccountResolveRequest
    ) -> ProspectAccountId:
        _require_text(str(tenant_id), "租户标识无效")
        _require_text(request.entity_name, "潜在企业字段无效")
        _require_text(request.country, "潜在企业字段无效")
        _optional_text(request.entity_type, "潜在企业字段无效")
        _optional_text(request.industry, "潜在企业字段无效")
        _optional_text(request.size_hint, "潜在企业字段无效")
        if any(not item or item != item.strip() for item in request.source_signal_refs):
            raise ValidationError("潜在企业来源引用无效")
        domain = (
            _canonical_domain(request.website_domain)
            if request.website_domain is not None
            else None
        )
        source_refs = list(dict.fromkeys(request.source_signal_refs))
        account = ProspectAccount(
            account_id=ProspectAccountId(new_id("acc")),
            tenant_id=tenant_id,
            name=request.entity_name,
            country=request.country,
            created_at=_utc_now(self._now),
            website_domain=domain,
            entity_type=request.entity_type,
            industry=request.industry,
            size_hint=request.size_hint,
            source_signal_refs=source_refs,
        )
        async with self._uow_factory(tenant_id) as uow:
            if await uow.accounts.add(account):
                return account.account_id
            if domain is None:
                raise ProspectingConflictError("潜在企业唯一身份冲突")
            winner = await uow.accounts.find_by_domain(tenant_id, domain)
            if winner is None:
                raise ProspectingConflictError("潜在企业消歧冲突")
            merged = await uow.accounts.merge_source_signal_refs(
                tenant_id, winner.account_id, tuple(source_refs)
            )
            if merged is None:
                raise ProspectingConflictError("潜在企业消歧冲突")
            return winner.account_id

    async def create_contact(
        self, tenant_id: TenantId, request: ContactCreateRequest
    ) -> ProspectContactId:
        _optional_text(request.full_name, "潜在联系人字段无效")
        _optional_text(request.role_title, "潜在联系人字段无效")
        _optional_text(request.language, "潜在联系人字段无效")
        contact = ProspectContact(
            contact_id=ProspectContactId(new_id("pc")),
            tenant_id=tenant_id,
            account_id=request.account_id,
            created_at=_utc_now(self._now),
            full_name=request.full_name,
            role_title=request.role_title,
            language=request.language,
        )
        async with self._uow_factory(tenant_id) as uow:
            if await uow.accounts.get(tenant_id, request.account_id) is None:
                raise ProspectAccountNotFoundError("潜在企业不存在")
            await uow.contacts.add_contact(contact)
        return contact.contact_id

    async def add_contact_point(
        self, tenant_id: TenantId, request: ContactPointCreateRequest
    ) -> ContactPointId:
        canonical = _canonical_contact_value(request.kind, request.value)
        _optional_text(request.enrichment_cost_note, "联系方式字段无效")
        basis_input = request.legal_basis
        basis = LegalBasisRecord(
            basis=basis_input.basis,
            subject_type=basis_input.subject_type,
            contact_type=basis_input.contact_type,
            source=basis_input.source,
            source_url=basis_input.source_url,
            collected_at=basis_input.collected_at,
            assessment_ref=basis_input.assessment_ref,
        )
        value_hash = _fingerprint_contact_value(self._hasher, canonical)
        point = ContactPoint(
            contact_point_id=ContactPointId(new_id("cp")),
            tenant_id=tenant_id,
            contact_id=request.contact_id,
            kind=request.kind,
            value=canonical,
            value_hash=value_hash,
            legal_basis=basis,
            created_at=_utc_now(self._now),
            enrichment_cost_note=request.enrichment_cost_note,
        )
        async with self._uow_factory(tenant_id) as uow:
            if await uow.contacts.is_erasure_suppressed(tenant_id, value_hash):
                raise ErasedContactPointError("联系方式已被删除或反对处理")
            if await uow.contacts.get_contact(tenant_id, request.contact_id) is None:
                raise ProspectContactNotFoundError("潜在联系人不存在")
            if await uow.contacts.add_contact_point(point):
                return point.contact_point_id
            existing = await uow.contacts.find_by_value_hash(
                tenant_id, request.kind, value_hash
            )
            if existing is not None and (
                existing.contact_id == point.contact_id
                and existing.value == point.value
                and existing.legal_basis == point.legal_basis
                and existing.enrichment_cost_note == point.enrichment_cost_note
            ):
                return existing.contact_point_id
            raise ProspectingConflictError("联系方式唯一身份冲突")

    async def record_discovered_contact(
        self, tenant_id: TenantId, request: DiscoveredContactRequest
    ) -> DiscoveredContactResult:
        """原子录入 Provider 候选；联系方式指纹是幂等身份。"""
        canonical = _canonical_contact_value(request.kind, request.value)
        _optional_text(request.full_name, "潜在联系人字段无效")
        _optional_text(request.role_title, "潜在联系人字段无效")
        _optional_text(request.language, "潜在联系人字段无效")
        _optional_text(request.enrichment_cost_note, "联系方式字段无效")
        basis_input = request.legal_basis
        basis = LegalBasisRecord(
            basis=basis_input.basis,
            subject_type=basis_input.subject_type,
            contact_type=basis_input.contact_type,
            source=basis_input.source,
            source_url=basis_input.source_url,
            collected_at=basis_input.collected_at,
            assessment_ref=basis_input.assessment_ref,
        )
        value_hash = _fingerprint_contact_value(self._hasher, canonical)
        now = _utc_now(self._now)
        contact = ProspectContact(
            contact_id=ProspectContactId(new_id("pc")),
            tenant_id=tenant_id,
            account_id=request.account_id,
            created_at=now,
            full_name=request.full_name,
            role_title=request.role_title,
            language=request.language,
        )
        point = ContactPoint(
            contact_point_id=ContactPointId(new_id("cp")),
            tenant_id=tenant_id,
            contact_id=contact.contact_id,
            kind=request.kind,
            value=canonical,
            value_hash=value_hash,
            legal_basis=basis,
            created_at=now,
            enrichment_cost_note=request.enrichment_cost_note,
        )
        async with self._uow_factory(tenant_id) as uow:
            if await uow.contacts.is_erasure_suppressed(tenant_id, value_hash):
                raise ErasedContactPointError("联系方式已被删除或反对处理")
            if await uow.accounts.get(tenant_id, request.account_id) is None:
                raise ProspectAccountNotFoundError("潜在企业不存在")
            existing = await uow.contacts.find_by_value_hash(
                tenant_id, request.kind, value_hash
            )
            if existing is not None:
                existing_contact = await uow.contacts.get_contact(
                    tenant_id, existing.contact_id
                )
                if (
                    existing_contact is None
                    or existing_contact.account_id != request.account_id
                ):
                    raise ProspectingConflictError("联系方式已归属于其他潜在企业")
                return DiscoveredContactResult(
                    account_id=request.account_id,
                    contact_id=existing.contact_id,
                    contact_point_id=existing.contact_point_id,
                    created=False,
                )
            await uow.contacts.add_contact(contact)
            if not await uow.contacts.add_contact_point(point):
                # 抛错使 UoW 回滚刚插入的联系人；scheduler 重试后会读取胜者。
                raise TransientError("联系方式并发录入冲突")
            return DiscoveredContactResult(
                account_id=request.account_id,
                contact_id=contact.contact_id,
                contact_point_id=point.contact_point_id,
                created=True,
            )

    async def get_account(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> ProspectAccountView:
        async with self._uow_factory(tenant_id) as uow:
            account = await uow.accounts.get(tenant_id, account_id)
        if account is None:
            raise ProspectAccountNotFoundError("潜在企业不存在")
        return _account_view(account)

    async def list_accounts(
        self, tenant_id: TenantId, *, limit: int = 50
    ) -> list[ProspectAccountView]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 200
        ):
            raise ValidationError("潜在企业查询数量无效")
        async with self._uow_factory(tenant_id) as uow:
            accounts = await uow.accounts.list_accounts(tenant_id, limit=limit)
        return [_account_view(account) for account in accounts]

    async def list_contacts_for_account(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> list[ProspectContactDetailView]:
        async with self._uow_factory(tenant_id) as uow:
            account = await uow.accounts.get(tenant_id, account_id)
            if account is None:
                raise ProspectAccountNotFoundError("潜在企业不存在")
            contacts = await uow.contacts.list_for_account(tenant_id, account_id)
            details: list[ProspectContactDetailView] = []
            for contact in contacts:
                points = await uow.contacts.list_for_contact(
                    tenant_id, contact.contact_id
                )
                details.append(
                    ProspectContactDetailView(
                        contact=_contact_view(contact),
                        contact_points=tuple(
                            _contact_point_detail(point, account_id)
                            for point in points
                        ),
                    )
                )
        return details

    async def get_account_detail(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> ProspectAccountDetailView:
        account = await self.get_account(tenant_id, account_id)
        contacts = await self.list_contacts_for_account(tenant_id, account_id)
        return ProspectAccountDetailView(account=account, contacts=tuple(contacts))

    async def get_contact_point(
        self, tenant_id: TenantId, contact_point_id: ContactPointId
    ) -> ContactPointView:
        async with self._uow_factory(tenant_id) as uow:
            point = await uow.contacts.get_contact_point(tenant_id, contact_point_id)
            if point is None:
                raise ContactPointNotFoundError("潜在联系方式不存在")
            contact = await uow.contacts.get_contact(tenant_id, point.contact_id)
            if contact is None:
                raise ContactPointNotFoundError("潜在联系方式不存在")
            return _contact_point_view(point, contact.account_id)

    async def list_verified_contact_points(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> list[ContactPointView]:
        async with self._uow_factory(tenant_id) as uow:
            points = await uow.contacts.list_verified_for_account(tenant_id, account_id)
        return [_contact_point_view(point, account_id) for point in points]

    async def record_verification(
        self,
        tenant_id: TenantId,
        request: VerificationRecordRequest,
    ) -> None:
        """行锁下转换验证状态，并与 metadata-only outbox 原子提交。"""
        if not isinstance(request.result, VerificationStatus):
            raise ValidationError("验证结果无效")
        _require_text(request.provider, "验证服务标识无效")
        _require_text(request.cost_note, "验证成本说明无效")
        if (
            request.checked_at.tzinfo is None
            or request.checked_at.utcoffset() != timedelta(0)
        ):
            raise ValidationError("验证观察时间必须为 UTC")
        async with self._uow_factory(tenant_id) as uow:
            current = await uow.contacts.get_contact_point_for_update(
                tenant_id, request.contact_point_id
            )
            if current is None:
                raise ContactPointNotFoundError("潜在联系方式不存在")
            if current.verification_checked_at is not None:
                if request.checked_at < current.verification_checked_at:
                    raise InvalidStateTransition("验证结果早于当前观察")
                if request.checked_at == current.verification_checked_at:
                    same_observation = (
                        current.verification is request.result
                        and current.verification_provider == request.provider
                        and current.verification_cost_note == request.cost_note
                    )
                    if same_observation:
                        return
                    raise ProspectingConflictError("验证观察时间冲突")
            transitioned_to_verified = (
                current.verification is not VerificationStatus.VERIFIED
                and request.result is VerificationStatus.VERIFIED
            )
            updated = replace(
                current,
                verification=request.result,
                verified_at=(
                    (
                        current.verified_at
                        if current.verification is VerificationStatus.VERIFIED
                        else request.checked_at
                    )
                    if request.result is VerificationStatus.VERIFIED
                    else None
                ),
                verification_provider=request.provider,
                verification_checked_at=request.checked_at,
                verification_cost_note=request.cost_note,
            )
            await uow.contacts.update_contact_point(updated)
            if transitioned_to_verified:
                await uow.bus.publish(
                    ContactPointVerified(
                        tenant_id=tenant_id,
                        occurred_at=request.checked_at,
                        run_id=None,
                        contact_point_id=request.contact_point_id,
                        verification_result=request.result.value,
                    )
                )

    async def handle_erasure_request(
        self, tenant_id: TenantId, contact_point_value: str
    ) -> int:
        """清除个人数据，并只保留不可恢复的 canonical value 指纹。"""
        kind = (
            ContactPointKind.PHONE
            if contact_point_value.startswith("+")
            else ContactPointKind.EMAIL
        )
        canonical = _canonical_contact_value(kind, contact_point_value)
        value_hash = _fingerprint_contact_value(self._hasher, canonical)
        async with self._uow_factory(tenant_id) as uow:
            return await uow.contacts.erase_personal_data(tenant_id, value_hash)
