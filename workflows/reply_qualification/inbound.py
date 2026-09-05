"""入站页纯编排；opaque cursor与显式关联指纹不进入日志或模型。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC

from shared.schemas.email_inbound import ArchivedInboundItem, InboundRoute


def item_fingerprint(route: InboundRoute, item: ArchivedInboundItem) -> str:
    """显式投影默认序列化隐藏的证据；只返回确定性摘要。"""
    raw = item.raw
    canonical = {
        "version": "inbound-receipt-v1",
        "guard_version": "credential-marker-v1",
        "route": [
            route.tenant_id,
            route.mailbox_alias,
            route.configured_identity_id,
            route.route_id,
            route.config_version,
        ],
        "provider": item.provider_ref_digest,
        "disposition": item.disposition.value,
        "parser": item.parser_version,
        "message_id": item.external_message_id,
        "in_reply_to": item.in_reply_to,
        "sent_at": item.sent_at.astimezone(UTC).isoformat() if item.sent_at else None,
        "raw": [raw.tenant_id, raw.artifact_id, raw.content_hash, raw.size_bytes]
        if raw
        else None,
    }
    return hashlib.sha256(
        json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


from collections.abc import Callable

from domains.outreach.service import Actor, DeliveryCorrelationLookup
from shared.schemas.email_inbound import ArchivedInboundPage, InboundDisposition
from shared.schemas.identifiers import OutboundMessageId, SendingIdentityId
from workflows.reply_qualification.inbound_contracts import (
    InboundCommitUnknown,
    InboundCommitVerifier,
    InboundCursor,
    InboundPageError,
    InboundPageUowFactory,
)


class InboundPageProcessor:
    """唯一整页事务调用域公开服务；错误页不得逐项吞掉。"""

    def __init__(
        self,
        verifier: InboundCommitVerifier,
        uow: InboundPageUowFactory,
        actor_factory: Callable[[SendingIdentityId], Actor],
    ):
        self._verifier, self._uow, self._actor_factory = verifier, uow, actor_factory

    async def process(self, expected: InboundCursor, page: ArchivedInboundPage) -> str:
        if page.route != expected.route or page.starting_cursor != expected.cursor:
            raise InboundPageError("page_integrity")
        items: dict[str, ArchivedInboundItem] = {}
        fingerprints: dict[str, str] = {}
        for item in page.items:
            digest = item.provider_ref_digest
            fingerprint = item_fingerprint(page.route, item)
            if digest in fingerprints and fingerprints[digest] != fingerprint:
                raise InboundPageError("receipt_conflict")
            if item.raw is not None and item.raw.tenant_id != page.route.tenant_id:
                raise InboundPageError("page_integrity")
            items[digest], fingerprints[digest] = item, fingerprint
        try:
            async with self._uow(expected) as tx:
                if tx.current != expected:
                    raise InboundPageError("cursor_conflict")
                fresh = []
                for digest in sorted(items):
                    old = await tx.receipt(digest)
                    if old is not None and old != fingerprints[digest]:
                        raise InboundPageError("receipt_conflict")
                    if old is None:
                        fresh.append(items[digest])
                for item in fresh:
                    message_id = None
                    reason = None
                    if item.disposition is InboundDisposition.CANDIDATE:
                        if (
                            item.raw is None
                            or item.external_message_id is None
                            or item.sent_at is None
                            or item.in_reply_to is None
                        ):
                            raise InboundPageError("page_integrity")
                        # 5a只准入精确自有Message-ID；route必须仍等于受信发送配置。
                        prefix = f"<{page.route.route_id}."
                        if not item.in_reply_to.startswith(prefix):
                            reason = "unknown_outbound"
                        else:
                            target = await tx.outreach.resolve_delivery_feedback(
                                page.route.tenant_id,
                                DeliveryCorrelationLookup(
                                    deterministic_message_id=item.in_reply_to
                                ),
                                actor=self._actor_factory(
                                    page.route.configured_identity_id
                                ),
                            )
                            if target is None:
                                reason = "unknown_outbound"
                            elif (
                                target.tenant_id != page.route.tenant_id
                                or target.sending_identity_id
                                != page.route.configured_identity_id
                            ):
                                raise InboundPageError("page_integrity")
                            else:
                                message_id = await tx.conversations.ingest_inbound(
                                    page.route.tenant_id,
                                    None,
                                    target.account_id,
                                    str(item.raw.artifact_id),
                                    item.external_message_id,
                                    item.sent_at,
                                    outbound_message_id=OutboundMessageId(
                                        item.in_reply_to
                                    ),
                                )
                    elif item.disposition not in {
                        InboundDisposition.SKIPPED_LABEL,
                        InboundDisposition.SKIPPED_DELIVERY_REPORT,
                    }:
                        reason = item.disposition.value
                    await tx.record(
                        item, fingerprints[item.provider_ref_digest], message_id, reason
                    )
                await tx.advance(page.next_cursor)
            return "committed"
        except (InboundCommitUnknown, InboundPageError) as error:
            if (
                isinstance(error, InboundPageError)
                and error.reason != "cursor_conflict"
            ):
                raise
            if await self._verifier.verify_page(expected, page, fingerprints):
                return "replayed"
            raise
