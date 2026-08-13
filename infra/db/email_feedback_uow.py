"""邮件反馈整页外层 UoW：四仓储、两域服务与 outbox 共用一个 session。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.outreach.repository import (
    OutreachUnitOfWork,
    OutreachUnitOfWorkFactory,
)
from domains.outreach.service import OutreachService
from domains.sending_identity.repository import (
    SendingIdentityUnitOfWork,
    SendingIdentityUnitOfWorkFactory,
)
from domains.sending_identity.service import SendingIdentityService
from infra.db.outbox import PostgresEventBus
from infra.db.repositories.email_feedback import (
    FeedbackCursorRepositoryImpl,
    FeedbackQuarantineRepositoryImpl,
    FeedbackReceiptRepositoryImpl,
    UnsubscribeTokenRepositoryImpl,
)
from infra.db.repositories.outreach import (
    ActionRepositoryImpl,
    CampaignRepositoryImpl,
    EnrollmentRepositoryImpl,
    MessageAttemptRepositoryImpl,
    QuotaRepositoryImpl,
    SuppressionRepositoryImpl,
)
from infra.db.repositories.sending_identities import (
    AuthenticationCheckRepositoryImpl,
    IdentityActionRepositoryImpl,
    ReputationRepositoryImpl,
    SendCounterRepositoryImpl,
    SendingDomainRepositoryImpl,
    SendingIdentityRepositoryImpl,
    SendReservationRepositoryImpl,
)
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import TenantId

_logger = logging.getLogger("infra.db.email_feedback.uow")
_security_logger = logging.getLogger("infra.db.email_feedback.security")


class AuditSink(Protocol):
    def log(
        self,
        *,
        actor: str,
        action: str,
        tenant_id: TenantId,
        scope: str,
        rule: str,
    ) -> None: ...


class OutreachServiceBuilder(Protocol):
    def __call__(
        self,
        factory: OutreachUnitOfWorkFactory,
        audit: AuditSink,
    ) -> OutreachService: ...


class SendingIdentityServiceBuilder(Protocol):
    def __call__(
        self,
        factory: SendingIdentityUnitOfWorkFactory,
        audit: AuditSink,
    ) -> SendingIdentityService: ...


@dataclass(frozen=True)
class _AuditRecord:
    actor: str
    action: str
    tenant_id: TenantId
    scope: str
    rule: str


class _TransactionAwareAudit:
    """拒绝立即留痕；仅允许审计等待业务事务成功提交。"""

    def __init__(self, sink: AuditSink) -> None:
        self._sink = sink
        self.records: list[_AuditRecord] = []

    def log(
        self,
        *,
        actor: str,
        action: str,
        tenant_id: TenantId,
        scope: str,
        rule: str,
    ) -> None:
        record = _AuditRecord(actor, action, tenant_id, scope, rule)
        if rule.startswith("deny:"):
            self._sink.log(
                actor=record.actor,
                action=record.action,
                tenant_id=record.tenant_id,
                scope=record.scope,
                rule=record.rule,
            )
            return
        self.records.append(record)


class _BoundOutreachUnitOfWork:
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        now: Callable[[], datetime],
    ) -> None:
        self.campaigns = CampaignRepositoryImpl(session, tenant_id)
        self.enrollments = EnrollmentRepositoryImpl(session, tenant_id)
        self.suppressions = SuppressionRepositoryImpl(session, tenant_id)
        self.quotas = QuotaRepositoryImpl(session, tenant_id)
        self.attempts = MessageAttemptRepositoryImpl(session, tenant_id)
        self.actions = ActionRepositoryImpl(session, tenant_id)
        self.bus = PostgresEventBus(session, tenant_id, now=now)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class _BoundSendingIdentityUnitOfWork:
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        now: Callable[[], datetime],
    ) -> None:
        self.domains = SendingDomainRepositoryImpl(session, tenant_id)
        self.identities = SendingIdentityRepositoryImpl(session, tenant_id)
        self.auth_checks = AuthenticationCheckRepositoryImpl(session, tenant_id)
        self.reputation = ReputationRepositoryImpl(session, tenant_id)
        self.counters = SendCounterRepositoryImpl(session, tenant_id)
        self.reservations = SendReservationRepositoryImpl(session, tenant_id)
        self.actions = IdentityActionRepositoryImpl(session, tenant_id)
        self.bus = PostgresEventBus(session, tenant_id, now=now)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class _BoundFactory[UnitOfWorkT]:
    def __init__(
        self,
        tenant_id: TenantId,
        create: Callable[[TenantId], UnitOfWorkT],
    ) -> None:
        self._tenant_id = tenant_id
        self._create = create

    def __call__(self, tenant_id: TenantId) -> UnitOfWorkT:
        if tenant_id != self._tenant_id:
            _security_logger.critical(
                "检测到跨租户数据隔离违规",
                extra={"tenant_id": str(self._tenant_id), "rule": "bound_uow_factory"},
            )
            raise TenantIsolationViolation("跨租户数据隔离违规")
        return self._create(tenant_id)


class SqlAlchemyFeedbackPageUnitOfWork:
    """外层独占 session 生命周期；域内 bound UoW 绝不提交或关闭。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
        *,
        outreach_builder: OutreachServiceBuilder,
        sending_identity_builder: SendingIdentityServiceBuilder,
        audit_sink: AuditSink,
        now: Callable[[], datetime],
    ) -> None:
        self._factory = session_factory
        self._tenant_id = tenant_id
        self._outreach_builder = outreach_builder
        self._sending_identity_builder = sending_identity_builder
        self._audit_sink = audit_sink
        self._now = now

    async def __aenter__(self) -> Self:
        session = self._factory()
        self._session = session
        self._audit = _TransactionAwareAudit(self._audit_sink)
        try:
            self.cursors = FeedbackCursorRepositoryImpl(
                session, self._tenant_id, now=self._now
            )
            self.receipts = FeedbackReceiptRepositoryImpl(session, self._tenant_id)
            self.quarantines = FeedbackQuarantineRepositoryImpl(
                session, self._tenant_id
            )
            self.tokens = UnsubscribeTokenRepositoryImpl(session, self._tenant_id)
            outreach_factory: OutreachUnitOfWorkFactory = _BoundFactory[
                OutreachUnitOfWork
            ](
                self._tenant_id,
                lambda tenant: cast(
                    OutreachUnitOfWork,
                    _BoundOutreachUnitOfWork(session, tenant, self._now),
                ),
            )
            sending_factory: SendingIdentityUnitOfWorkFactory = _BoundFactory[
                SendingIdentityUnitOfWork
            ](
                self._tenant_id,
                lambda tenant: cast(
                    SendingIdentityUnitOfWork,
                    _BoundSendingIdentityUnitOfWork(session, tenant, self._now),
                ),
            )
            self.outreach = self._outreach_builder(outreach_factory, self._audit)
            self.sending_identities = self._sending_identity_builder(
                sending_factory, self._audit
            )
            return self
        except BaseException:
            try:
                await session.rollback()
            except BaseException:  # noqa: BLE001 - cleanup 不覆盖构造 primary
                _logger.error("邮件反馈事务回滚失败")
            try:
                await session.close()
            except BaseException:  # noqa: BLE001 - cleanup 不覆盖构造 primary
                _logger.error("邮件反馈事务关闭失败")
            raise

    def _flush_audit(self) -> None:
        for record in tuple(self._audit.records):
            try:
                self._audit_sink.log(
                    actor=record.actor,
                    action=record.action,
                    tenant_id=record.tenant_id,
                    scope=record.scope,
                    rule=record.rule,
                )
            except BaseException:  # noqa: BLE001 - 已提交审计失败不能成为重放信号
                _logger.error("邮件反馈提交后审计刷新失败")
        self._audit.records.clear()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        preserve_primary = exc_type is not None
        committed = False
        try:
            if exc_type is None:
                try:
                    await self._session.commit()
                    committed = True
                except BaseException:
                    preserve_primary = True
                    self._audit.records.clear()
                    try:
                        await self._session.rollback()
                    except BaseException:  # noqa: BLE001 - cleanup 不覆盖 primary
                        _logger.error("邮件反馈事务回滚失败")
                    raise
            else:
                self._audit.records.clear()
                try:
                    await self._session.rollback()
                except BaseException:  # noqa: BLE001 - cleanup 不覆盖 primary
                    _logger.error("邮件反馈事务回滚失败")
            if committed:
                self._flush_audit()
        finally:
            try:
                await self._session.close()
            except BaseException:
                if not preserve_primary:
                    raise
                _logger.error("邮件反馈事务关闭失败")
