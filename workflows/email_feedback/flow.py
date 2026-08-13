"""Provider-neutral 邮件反馈整页原子编排。"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from domains.outreach.service import (
    Actor as OutreachActor,
)
from domains.outreach.service import (
    DeliveryCorrelationLookup,
    DeliveryFeedbackTarget,
    MessageAttemptConflictError,
)
from domains.sending_identity.service import (
    Actor as SendingIdentityActor,
)
from domains.sending_identity.service import (
    DeliveryEventRecord,
    DeliveryEventType,
)
from shared.errors import ValidationError
from shared.schemas.email_feedback import (
    EmailFeedbackItem,
    EmailFeedbackKind,
    EmailFeedbackPage,
    EmailFeedbackParseIssue,
    EmailFeedbackQuarantineReason,
    EmailFeedbackResult,
)
from shared.schemas.identifiers import IdempotencyKey, SendingIdentityId, TenantId
from workflows.email_feedback.repository import (
    FeedbackPageUnitOfWork,
    FeedbackQuarantine,
    FeedbackReceipt,
    FeedbackReceiptAppendStatus,
)

__all__ = ["FeedbackPageProcessor", "FeedbackPageResult"]

_ROUTE_RE = re.compile(r"[a-z0-9-]{1,32}")
_MAILBOX_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_ID_RE = re.compile(r"(?:tn|sid)_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_logger = logging.getLogger("workflows.email_feedback")


class FeedbackPageUnitOfWorkFactory(Protocol):
    def __call__(self, tenant_id: TenantId) -> FeedbackPageUnitOfWork: ...


class OutreachActorFactory(Protocol):
    def __call__(self, identity_id: SendingIdentityId) -> OutreachActor: ...


class SendingIdentityActorFactory(Protocol):
    def __call__(self, identity_id: SendingIdentityId) -> SendingIdentityActor: ...


class CriticalSink(Protocol):
    def __call__(
        self,
        *,
        tenant_id: TenantId,
        mailbox_alias: str,
        reason: str,
    ) -> None: ...


@dataclass(frozen=True)
class FeedbackPageResult:
    processed: int
    duplicates: int
    hard_bounces: int
    soft_bounces: int
    quarantined: int
    next_cursor: str = field(repr=False)


@dataclass(frozen=True)
class _HardEffect:
    item: EmailFeedbackItem
    target: DeliveryFeedbackTarget
    domain: str


def _default_critical_sink(
    *, tenant_id: TenantId, mailbox_alias: str, reason: str
) -> None:
    _logger.critical(
        "检测到跨路由邮件反馈",
        extra={
            "tenant_id": str(tenant_id),
            "mailbox_alias": mailbox_alias,
            "reason": reason,
        },
    )


def _source_ref(provider_event_id: str) -> str:
    encoded = base64.b32encode(bytes.fromhex(provider_event_id)).decode("ascii")
    return f"feedback_{encoded.rstrip('=')}"


def _outreach_message_id(value: str | None) -> str | None:
    return f"<{value}>" if value is not None else None


def _quarantine_reason(
    item: EmailFeedbackItem, route_id: str
) -> EmailFeedbackQuarantineReason | None:
    if item.kind is EmailFeedbackKind.UNPARSEABLE:
        return (
            EmailFeedbackQuarantineReason.MALFORMED
            if item.parse_issue is EmailFeedbackParseIssue.MALFORMED
            else EmailFeedbackQuarantineReason.UNSUPPORTED
        )
    correlation = item.correlation
    if correlation is None or correlation.route_id is None:
        return EmailFeedbackQuarantineReason.MISSING_CORRELATION
    if correlation.route_id != route_id:
        return EmailFeedbackQuarantineReason.CROSS_TENANT_CORRELATION
    return None


def _item_fingerprint(
    item: EmailFeedbackItem, configured_identity_id: SendingIdentityId
) -> str:
    correlation = item.correlation
    canonical = json.dumps(
        {
            "configured_identity_id": str(configured_identity_id),
            "correlation": (
                None
                if correlation is None
                else {
                    "route_id": correlation.route_id,
                    "deterministic_message_id": correlation.deterministic_message_id,
                    "idempotency_header": correlation.idempotency_header,
                }
            ),
            "kind": item.kind.value,
            "occurred_at": item.occurred_at.isoformat(),
            "ordinal": item.ordinal,
            "parse_issue": item.parse_issue.value if item.parse_issue else None,
            "provider_event_id": item.provider_event_id,
            "provider_ref_digest": item.provider_ref_digest,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


class FeedbackPageProcessor:
    """整页提交 receipt、隔离、两域 effect 与 cursor。"""

    def __init__(
        self,
        uow_factory: FeedbackPageUnitOfWorkFactory,
        *,
        route_id: str,
        outreach_actor_factory: OutreachActorFactory,
        sending_identity_actor_factory: SendingIdentityActorFactory,
        now: Callable[[], datetime],
        critical_sink: CriticalSink = _default_critical_sink,
    ) -> None:
        if not isinstance(route_id, str) or _ROUTE_RE.fullmatch(route_id) is None:
            raise ValidationError("feedback route 无效")
        self._uow_factory = uow_factory
        self._route_id = route_id
        self._outreach_actor = outreach_actor_factory
        self._sending_actor = sending_identity_actor_factory
        self._now = now
        self._critical_sink = critical_sink

    @staticmethod
    def _validate_request(
        tenant_id: TenantId,
        mailbox_alias: str,
        configured_identity_id: SendingIdentityId,
        expected_cursor: str | None,
        page: EmailFeedbackPage,
    ) -> None:
        if (
            not isinstance(tenant_id, str)
            or _ID_RE.fullmatch(tenant_id) is None
            or not tenant_id.startswith("tn_")
            or not isinstance(mailbox_alias, str)
            or _MAILBOX_RE.fullmatch(mailbox_alias) is None
            or not isinstance(configured_identity_id, str)
            or _ID_RE.fullmatch(configured_identity_id) is None
            or not configured_identity_id.startswith("sid_")
            or not isinstance(page, EmailFeedbackPage)
            or page.starting_cursor != expected_cursor
        ):
            raise ValidationError("feedback page request 无效")

    @staticmethod
    def _quarantine_receipt(
        tenant_id: TenantId,
        mailbox_alias: str,
        item: EmailFeedbackItem,
        item_fingerprint: str,
        created_at: datetime,
    ) -> FeedbackReceipt:
        return FeedbackReceipt(
            tenant_id=tenant_id,
            mailbox_alias=mailbox_alias,
            provider_event_id=item.provider_event_id,
            item_fingerprint=item_fingerprint,
            ordinal=item.ordinal,
            kind=EmailFeedbackKind.UNPARSEABLE,
            occurred_at=item.occurred_at,
            result=EmailFeedbackResult.QUARANTINED,
            attempt_id=None,
            enrollment_id=None,
            account_id=None,
            contact_point_id=None,
            sending_identity_id=None,
            created_at=created_at,
        )

    @staticmethod
    def _correlated_receipt(
        tenant_id: TenantId,
        mailbox_alias: str,
        item: EmailFeedbackItem,
        target: DeliveryFeedbackTarget,
        item_fingerprint: str,
        created_at: datetime,
    ) -> FeedbackReceipt:
        return FeedbackReceipt(
            tenant_id=tenant_id,
            mailbox_alias=mailbox_alias,
            provider_event_id=item.provider_event_id,
            item_fingerprint=item_fingerprint,
            ordinal=item.ordinal,
            kind=item.kind,
            occurred_at=item.occurred_at,
            result=(
                EmailFeedbackResult.APPLIED
                if item.kind is EmailFeedbackKind.HARD_BOUNCE
                else EmailFeedbackResult.RECORDED
            ),
            attempt_id=target.attempt_id,
            enrollment_id=target.enrollment_id,
            account_id=target.account_id,
            contact_point_id=target.contact_point_id,
            sending_identity_id=target.sending_identity_id,
            created_at=created_at,
        )

    async def _append_quarantine(
        self,
        uow: FeedbackPageUnitOfWork,
        tenant_id: TenantId,
        mailbox_alias: str,
        item: EmailFeedbackItem,
        item_fingerprint: str,
        reason: EmailFeedbackQuarantineReason,
        created_at: datetime,
    ) -> FeedbackReceiptAppendStatus:
        receipt = self._quarantine_receipt(
            tenant_id, mailbox_alias, item, item_fingerprint, created_at
        )
        result = await uow.receipts.append_if_absent(receipt)
        if result.status is FeedbackReceiptAppendStatus.CONFLICT:
            raise ValidationError("邮件反馈 receipt 冲突")
        if result.status is FeedbackReceiptAppendStatus.CREATED:
            await uow.quarantines.append_if_absent(
                FeedbackQuarantine(
                    tenant_id=tenant_id,
                    mailbox_alias=mailbox_alias,
                    provider_event_id=item.provider_event_id,
                    reason=reason,
                    provider_ref_digest=item.provider_ref_digest,
                    created_at=created_at,
                )
            )
        return result.status

    async def process(
        self,
        tenant_id: TenantId,
        mailbox_alias: str,
        configured_identity_id: SendingIdentityId,
        expected_cursor: str | None,
        page: EmailFeedbackPage,
    ) -> FeedbackPageResult:
        self._validate_request(
            tenant_id,
            mailbox_alias,
            configured_identity_id,
            expected_cursor,
            page,
        )
        if not page.items and page.next_cursor == expected_cursor:
            return FeedbackPageResult(0, 0, 0, 0, 0, page.next_cursor)

        processed = duplicates = hard = soft = quarantined = 0
        critical_reasons: list[EmailFeedbackQuarantineReason] = []
        hard_effects: list[_HardEffect] = []
        created_at = self._now()
        async with self._uow_factory(tenant_id) as uow:
            cursor = await uow.cursors.lock_expected(
                tenant_id, mailbox_alias, expected_cursor
            )
            new_items: list[tuple[EmailFeedbackItem, str]] = []
            page_fingerprints: dict[str, str] = {}
            for item in page.items:
                item_fingerprint = _item_fingerprint(item, configured_identity_id)
                page_fingerprint = page_fingerprints.get(item.provider_event_id)
                if page_fingerprint is not None:
                    if page_fingerprint != item_fingerprint:
                        raise ValidationError("邮件反馈 receipt 冲突")
                    duplicates += 1
                    continue
                page_fingerprints[item.provider_event_id] = item_fingerprint
                existing = await uow.receipts.get(
                    tenant_id, mailbox_alias, item.provider_event_id
                )
                if existing is not None:
                    if existing.item_fingerprint != item_fingerprint:
                        raise ValidationError("邮件反馈 receipt 冲突")
                    duplicates += 1
                    continue
                new_items.append((item, item_fingerprint))

            for item, item_fingerprint in new_items:
                reason = _quarantine_reason(item, self._route_id)
                target: DeliveryFeedbackTarget | None = None
                if reason is None:
                    correlation = item.correlation
                    if correlation is None:
                        raise ValidationError("邮件反馈 correlation 无效")
                    try:
                        target = await uow.outreach.resolve_delivery_feedback(
                            tenant_id,
                            DeliveryCorrelationLookup(
                                _outreach_message_id(
                                    correlation.deterministic_message_id
                                ),
                                correlation.idempotency_header,
                            ),
                            actor=self._outreach_actor(configured_identity_id),
                        )
                    except MessageAttemptConflictError:
                        reason = EmailFeedbackQuarantineReason.AMBIGUOUS_CORRELATION
                    if target is None and reason is None:
                        reason = EmailFeedbackQuarantineReason.MISSING_CORRELATION
                    elif target is not None and (
                        not isinstance(target, DeliveryFeedbackTarget)
                        or target.tenant_id != tenant_id
                        or target.sending_identity_id != configured_identity_id
                    ):
                        reason = EmailFeedbackQuarantineReason.CROSS_TENANT_CORRELATION

                if reason is not None:
                    status = await self._append_quarantine(
                        uow,
                        tenant_id,
                        mailbox_alias,
                        item,
                        item_fingerprint,
                        reason,
                        created_at,
                    )
                    if status is FeedbackReceiptAppendStatus.EXISTING:
                        duplicates += 1
                        continue
                    processed += 1
                    quarantined += 1
                    if reason is EmailFeedbackQuarantineReason.CROSS_TENANT_CORRELATION:
                        critical_reasons.append(reason)
                    continue

                if target is None:
                    raise ValidationError("邮件反馈 target 无效")
                receipt = self._correlated_receipt(
                    tenant_id,
                    mailbox_alias,
                    item,
                    target,
                    item_fingerprint,
                    created_at,
                )
                appended = await uow.receipts.append_if_absent(receipt)
                if appended.status is FeedbackReceiptAppendStatus.CONFLICT:
                    raise ValidationError("邮件反馈 receipt 冲突")
                if appended.status is FeedbackReceiptAppendStatus.EXISTING:
                    duplicates += 1
                    continue
                processed += 1
                if item.kind is EmailFeedbackKind.SOFT_BOUNCE:
                    soft += 1
                    continue
                identity = await uow.sending_identities.get(
                    tenant_id,
                    target.sending_identity_id,
                    actor=self._sending_actor(target.sending_identity_id),
                )
                if (
                    getattr(identity, "identity_id", None) != target.sending_identity_id
                    or not isinstance(getattr(identity, "domain", None), str)
                    or not identity.domain
                ):
                    raise ValidationError("邮件反馈发件身份查询结果损坏")
                hard += 1
                hard_effects.append(_HardEffect(item, target, identity.domain))

            for effect in sorted(
                hard_effects,
                key=lambda value: (
                    value.domain,
                    value.target.sending_identity_id,
                    value.item.provider_event_id,
                ),
            ):
                event = DeliveryEventRecord(
                    tenant_id=tenant_id,
                    identity_id=effect.target.sending_identity_id,
                    event_type=DeliveryEventType.HARD_BOUNCED,
                    occurred_at=effect.item.occurred_at,
                    dedup_key=IdempotencyKey(effect.item.provider_event_id),
                    source_ref=_source_ref(effect.item.provider_event_id),
                )
                await uow.sending_identities.record_delivery_event(
                    tenant_id,
                    effect.target.sending_identity_id,
                    event,
                    actor=self._sending_actor(effect.target.sending_identity_id),
                )
            for effect in sorted(
                hard_effects,
                key=lambda value: (
                    value.target.contact_point_id,
                    value.target.enrollment_id,
                    value.item.provider_event_id,
                ),
            ):
                await uow.outreach.apply_hard_bounce(
                    tenant_id,
                    effect.target,
                    effect.item.provider_event_id,
                    effect.item.occurred_at,
                    actor=self._outreach_actor(effect.target.sending_identity_id),
                )
            if page.next_cursor != expected_cursor:
                await uow.cursors.advance(cursor, page.next_cursor, created_at)

        for reason in critical_reasons:
            try:
                self._critical_sink(
                    tenant_id=tenant_id,
                    mailbox_alias=mailbox_alias,
                    reason=reason.value,
                )
            except BaseException:  # noqa: BLE001 - 已提交隔离不能成为重放信号
                _logger.error("邮件反馈安全告警写入失败")
        return FeedbackPageResult(
            processed,
            duplicates,
            hard,
            soft,
            quarantined,
            page.next_cursor,
        )
