"""入站技术账本；每次查询显式tenant，外部IO不在事务内。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.tables import (
    EmailInboundCursorRow,
    EmailInboundReceiptRow,
    EmailInboundReviewRow,
)
from shared.schemas.email_inbound import (
    ArchivedInboundPage,
    ArchivedInboundRaw,
    InboundRoute,
)
from shared.schemas.identifiers import ArtifactId, SendingIdentityId, TenantId
from workflows.reply_qualification.inbound_contracts import (
    InboundCursor,
    InboundPageError,
    InboundReviewView,
)


async def lock_tenant(session: AsyncSession, tenant_id: TenantId) -> None:
    key = int.from_bytes(
        hashlib.sha256(f"email-inbound:{tenant_id}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def cursor_fact(row: EmailInboundCursorRow) -> InboundCursor:
    return InboundCursor(
        route=InboundRoute(
            tenant_id=TenantId(row.tenant_id),
            mailbox_alias=row.mailbox_alias,
            configured_identity_id=SendingIdentityId(row.configured_identity_id),
            route_id=row.route_id,
            config_version=row.config_version,
        ),
        cursor=row.provider_cursor,
        version=row.version,
        bootstrap_started_at=row.bootstrap_started_at,
        after_epoch=row.after_epoch,
        confirmed_by=row.confirmed_by,
        confirmed_at=row.confirmed_at,
        last_succeeded_at=row.last_succeeded_at,
        blocked_reason=row.blocked_reason,
        next_retry_at=row.next_retry_at,
    )


class InboundStore:
    """短事务读取与首次绑定；已有绑定不可重置或替换身份。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
        mailbox_alias: str,
        *,
        now: Callable[[], datetime],
    ):
        self.factory, self.tenant_id, self.mailbox_alias, self.now = (
            factory,
            tenant_id,
            mailbox_alias,
            now,
        )

    async def row(
        self, session: AsyncSession, *, lock: bool = False
    ) -> EmailInboundCursorRow | None:
        query = select(EmailInboundCursorRow).where(
            EmailInboundCursorRow.tenant_id == self.tenant_id,
            EmailInboundCursorRow.mailbox_alias == self.mailbox_alias,
        )
        if lock:
            query = query.with_for_update()
        return await session.scalar(query)

    async def read_cursor(self) -> InboundCursor | None:
        async with self.factory() as session:
            row = await self.row(session)
            return cursor_fact(row) if row is not None else None

    async def bind(
        self,
        route: InboundRoute,
        initial: str,
        started: datetime,
        after_epoch: int,
        confirmed_by: str,
    ) -> InboundCursor:
        if (
            route.tenant_id != self.tenant_id
            or route.mailbox_alias != self.mailbox_alias
        ):
            raise InboundPageError("binding_conflict")
        async with self.factory.begin() as session:
            await lock_tenant(session, self.tenant_id)
            row = await self.row(session, lock=True)
            if row is not None:
                fact = cursor_fact(row)
                if fact.route != route:
                    raise InboundPageError("binding_conflict")
                return fact
            row = EmailInboundCursorRow(
                tenant_id=self.tenant_id,
                mailbox_alias=self.mailbox_alias,
                configured_identity_id=route.configured_identity_id,
                route_id=route.route_id,
                config_version=route.config_version,
                provider_cursor=initial,
                version=1,
                bootstrap_started_at=started,
                after_epoch=after_epoch,
                confirmed_by=confirmed_by,
                confirmed_at=self.now(),
            )
            session.add(row)
            await session.flush()
            return cursor_fact(row)

    async def verify_page(
        self,
        expected: InboundCursor,
        page: ArchivedInboundPage,
        fingerprints: dict[str, str],
    ) -> bool:
        """全新事务获取同tenant锁，避免提交未知与在途commit竞争；只认耐久事实。"""
        async with self.factory() as session:
            await lock_tenant(session, self.tenant_id)
            row = await self.row(session, lock=True)
            if (
                row is None
                or cursor_fact(row).route != expected.route
                or row.provider_cursor != page.next_cursor
                or row.version != expected.version + 1
            ):
                return False
            for digest in sorted(fingerprints):
                actual = await session.scalar(
                    select(EmailInboundReceiptRow.item_fingerprint).where(
                        EmailInboundReceiptRow.tenant_id == self.tenant_id,
                        EmailInboundReceiptRow.mailbox_alias == self.mailbox_alias,
                        EmailInboundReceiptRow.provider_ref_digest == digest,
                    )
                )
                if actual != fingerprints[digest]:
                    return False
            return True

    async def mark_failure(
        self, expected: InboundCursor, reason: str, next_retry_at: datetime | None
    ) -> bool:
        """旧cursor与version匹配才记固定阻断，不能覆盖并发成功。"""
        if reason not in {
            "provider_transient",
            "rate_limited",
            "provider_permanent",
            "provider_auth_required",
            "page_integrity",
            "receipt_conflict",
            "domain_rejected",
            "storage_unavailable",
        }:
            raise InboundPageError("page_integrity")
        async with self.factory.begin() as session:
            await lock_tenant(session, self.tenant_id)
            row = await self.row(session, lock=True)
            if row is None or cursor_fact(row) != expected:
                return False
            row.blocked_reason = reason
            row.next_retry_at = next_retry_at
            row.version += 1
            return True

    async def retry(self, expected_version: int) -> InboundCursor:
        """显式原位恢复，未到期Provider期限及旧版本请求均不可越过。"""
        async with self.factory.begin() as session:
            await lock_tenant(session, self.tenant_id)
            row = await self.row(session, lock=True)
            if (
                row is None
                or type(expected_version) is not int
                or row.version != expected_version
            ):
                raise InboundPageError("cursor_conflict")
            if row.next_retry_at is not None and self.now() < row.next_retry_at:
                return cursor_fact(row)
            if row.blocked_reason is not None or row.next_retry_at is not None:
                row.blocked_reason = None
                row.next_retry_at = None
                row.version += 1
            return cursor_fact(row)

    async def list_reviews(
        self, *, limit: int, after: str | None
    ) -> tuple[InboundReviewView, ...]:
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or (
                after is not None
                and re.fullmatch(r"irv_[0-7][0-9A-HJKMNP-TV-Z]{25}", after) is None
            )
        ):
            raise InboundPageError("page_integrity")
        async with self.factory() as session:
            query = select(EmailInboundReviewRow).where(
                EmailInboundReviewRow.tenant_id == self.tenant_id,
                EmailInboundReviewRow.mailbox_alias == self.mailbox_alias,
            )
            if after is not None:
                query = query.where(EmailInboundReviewRow.review_id > after)
            rows = (
                await session.scalars(
                    query.order_by(EmailInboundReviewRow.review_id).limit(limit)
                )
            ).all()
            return tuple(
                InboundReviewView.model_validate(
                    {
                        "review_id": r.review_id,
                        "reason": r.reason,
                        "created_at": r.created_at,
                        "archived": r.raw_artifact_id is not None,
                    }
                )
                for r in rows
            )

    async def review_raw(self, review_id: str) -> ArchivedInboundRaw:
        if re.fullmatch(r"irv_[0-7][0-9A-HJKMNP-TV-Z]{25}", review_id) is None:
            raise InboundPageError("not_found")
        async with self.factory() as session:
            row = (
                await session.execute(
                    select(EmailInboundReviewRow, EmailInboundReceiptRow)
                    .join(
                        EmailInboundReceiptRow,
                        (
                            EmailInboundReceiptRow.tenant_id
                            == EmailInboundReviewRow.tenant_id
                        )
                        & (
                            EmailInboundReceiptRow.mailbox_alias
                            == EmailInboundReviewRow.mailbox_alias
                        )
                        & (
                            EmailInboundReceiptRow.provider_ref_digest
                            == EmailInboundReviewRow.provider_ref_digest
                        ),
                    )
                    .where(
                        EmailInboundReviewRow.tenant_id == self.tenant_id,
                        EmailInboundReceiptRow.tenant_id == self.tenant_id,
                        EmailInboundReviewRow.mailbox_alias == self.mailbox_alias,
                        EmailInboundReviewRow.review_id == review_id,
                    )
                )
            ).one_or_none()
            if row is None:
                raise InboundPageError("not_found")
            review, receipt = row
            if review.raw_artifact_id is None:
                raise InboundPageError("not_archived")
            if (
                review.raw_artifact_id != receipt.raw_artifact_id
                or receipt.raw_hash is None
                or receipt.raw_size is None
            ):
                raise InboundPageError("page_integrity")
            return ArchivedInboundRaw(
                tenant_id=self.tenant_id,
                artifact_id=ArtifactId(review.raw_artifact_id),
                content_hash=receipt.raw_hash,
                size_bytes=receipt.raw_size,
            )
