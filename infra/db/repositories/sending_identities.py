"""发件身份七类 tenant-bound PostgreSQL repository 实现。"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import cast

from sqlalchemy import Select, and_, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from domains.sending_identity.models import (
    AuthCheck,
    AuthenticationFailureCategory,
    AuthenticationFixInstruction,
    DeliveryEventType,
    DomainRole,
    IdentityState,
    ReputationThresholds,
    ReputationWindow,
    SendingIdentity,
    SuspensionCategory,
    WarmupPlan,
)
from domains.sending_identity.permissions import ScopeLevel, SendingIdentityScope
from domains.sending_identity.repository import (
    AuthenticationAppendResult,
    AuthenticationCheckRecord,
    AuthenticationCheckRequestCreateResult,
    AuthenticationCheckRequestStatus,
    AuthenticationCheckRequestView,
    IdentityActionRecord,
    IdentityRegistrationResult,
    ReservationOutcome,
    ReservationResult,
    SendingDomain,
)
from domains.sending_identity.schemas import (
    AuthenticationFailure,
    AuthenticationResult,
    DeliveryEventRecord,
    SendReservation,
)
from infra.db.base import TenantScopedRepository
from infra.db.tables import (
    AuthenticationCheckRequestRow,
    AuthenticationCheckRow,
    IdentityActionRow,
    ReputationEventRow,
    SendCounterRow,
    SendingDomainRow,
    SendingIdentityRow,
    SendReservationRow,
)
from shared.errors import InvalidStateTransition, TenantIsolationViolation
from shared.schemas.identifiers import (
    AuthenticationCheckRequestId,
    IdempotencyKey,
    SendingIdentityId,
    TenantId,
    new_id,
)

_security_logger = logging.getLogger("infra.db.sending_identity.security")


class _SendingRepository(TenantScopedRepository):
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        super().__init__(tenant_id)
        self._session = session

    def _tenant_matches(self, tenant_id: TenantId, rule: str) -> bool:
        if tenant_id == self._tenant_id:
            return True
        _security_logger.critical(
            "检测到跨租户数据隔离违规",
            extra={
                "repository": type(self).__name__,
                "tenant_id": str(self._tenant_id),
                "rule": rule,
            },
        )
        return False

    def _require_tenant(self, tenant_id: TenantId, rule: str) -> None:
        if not self._tenant_matches(tenant_id, rule):
            raise TenantIsolationViolation(
                "检测到跨租户数据隔离违规",
                context={
                    "repository": type(self).__name__,
                    "tenant_id": str(self._tenant_id),
                    "rule": rule,
                },
            )


def _domain_to_row(domain: SendingDomain) -> SendingDomainRow:
    return SendingDomainRow(
        tenant_id=domain.tenant_id,
        domain=domain.domain,
        role=domain.role.value,
        created_at=domain.created_at,
    )


def _row_to_domain(row: SendingDomainRow) -> SendingDomain:
    return SendingDomain(
        tenant_id=TenantId(row.tenant_id),
        domain=row.domain,
        role=DomainRole(row.role),
        created_at=row.created_at,
    )


def _identity_to_row(identity: SendingIdentity) -> SendingIdentityRow:
    warmup = identity.warmup_plan
    thresholds = identity.thresholds
    return SendingIdentityRow(
        tenant_id=identity.tenant_id,
        identity_id=identity.identity_id,
        domain=identity.domain,
        address=identity.address,
        display_name=identity.display_name,
        state=identity.state.value,
        connector_ref=identity.connector_ref,
        warmup_started_on=warmup.started_on if warmup is not None else None,
        target_daily_volume=warmup.target_daily_volume if warmup is not None else None,
        activated_at=identity.activated_at,
        suspended_at=identity.suspended_at,
        retired_at=identity.retired_at,
        sendable_state_before_restriction=(
            identity.sendable_state_before_restriction.value
            if identity.sendable_state_before_restriction is not None
            else None
        ),
        suspension_category=(
            identity.suspension_category.value
            if identity.suspension_category is not None
            else None
        ),
        version=identity.version,
        throttle_hard_bounce_rate=thresholds.throttle_hard_bounce_rate,
        suspend_hard_bounce_rate=thresholds.suspend_hard_bounce_rate,
        throttle_complaint_rate=thresholds.throttle_complaint_rate,
        suspend_complaint_rate=thresholds.suspend_complaint_rate,
        suspend_on_spam_trap=thresholds.suspend_on_spam_trap,
        suspend_on_blocklist=thresholds.suspend_on_blocklist,
        minimum_sample=thresholds.minimum_sample,
        created_at=identity.created_at,
    )


def _row_to_identity(row: SendingIdentityRow, role: str) -> SendingIdentity:
    warmup = None
    if row.warmup_started_on is not None and row.target_daily_volume is not None:
        warmup = WarmupPlan(row.warmup_started_on, row.target_daily_volume)
    return SendingIdentity(
        identity_id=SendingIdentityId(row.identity_id),
        tenant_id=TenantId(row.tenant_id),
        address=row.address,
        domain=row.domain,
        role=DomainRole(role),
        created_at=row.created_at,
        state=IdentityState(row.state),
        display_name=row.display_name,
        warmup_plan=warmup,
        thresholds=ReputationThresholds(
            throttle_hard_bounce_rate=row.throttle_hard_bounce_rate,
            suspend_hard_bounce_rate=row.suspend_hard_bounce_rate,
            throttle_complaint_rate=row.throttle_complaint_rate,
            suspend_complaint_rate=row.suspend_complaint_rate,
            suspend_on_spam_trap=row.suspend_on_spam_trap,
            suspend_on_blocklist=row.suspend_on_blocklist,
            minimum_sample=row.minimum_sample,
        ),
        activated_at=row.activated_at,
        suspended_at=row.suspended_at,
        retired_at=row.retired_at,
        sendable_state_before_restriction=(
            IdentityState(row.sendable_state_before_restriction)
            if row.sendable_state_before_restriction is not None
            else None
        ),
        suspension_category=(
            SuspensionCategory(row.suspension_category)
            if row.suspension_category is not None
            else None
        ),
        connector_ref=row.connector_ref,
        version=row.version,
    )


def _failure_to_json(failure: AuthenticationFailure) -> dict[str, str]:
    return {
        "check": failure.check.value,
        "category": failure.category.value,
        "instruction": failure.instruction.value,
    }


def _failure_from_json(value: dict[str, str]) -> AuthenticationFailure:
    return AuthenticationFailure(
        check=AuthCheck(value["check"]),
        category=AuthenticationFailureCategory(value["category"]),
        instruction=AuthenticationFixInstruction(value["instruction"]),
    )


def _auth_to_row(record: AuthenticationCheckRecord) -> AuthenticationCheckRow:
    result = record.result
    return AuthenticationCheckRow(
        tenant_id=record.tenant_id,
        auth_check_id=record.auth_check_id,
        identity_id=record.identity_id,
        checked_at=result.checked_at,
        spf_passed=result.spf_passed,
        dkim_passed=result.dkim_passed,
        dmarc_passed=result.dmarc_passed,
        failures=[_failure_to_json(item) for item in result.failures],
        check_ref=result.check_ref,
        created_at=record.created_at,
    )


def _row_to_auth(row: AuthenticationCheckRow) -> AuthenticationCheckRecord:
    return AuthenticationCheckRecord(
        auth_check_id=row.auth_check_id,
        tenant_id=TenantId(row.tenant_id),
        identity_id=SendingIdentityId(row.identity_id),
        result=AuthenticationResult(
            checked_at=row.checked_at,
            spf_passed=row.spf_passed,
            dkim_passed=row.dkim_passed,
            dmarc_passed=row.dmarc_passed,
            failures=tuple(_failure_from_json(item) for item in row.failures),
            check_ref=row.check_ref,
        ),
        created_at=row.created_at,
    )


def _row_to_auth_request(
    row: AuthenticationCheckRequestRow,
) -> AuthenticationCheckRequestView:
    return AuthenticationCheckRequestView(
        AuthenticationCheckRequestId(row.request_id),
        TenantId(row.tenant_id),
        SendingIdentityId(row.sending_identity_id),
        IdempotencyKey(row.request_key),
        AuthenticationCheckRequestStatus(row.status),
        row.requested_at,
        row.completed_at,
    )


class SendingDomainRepositoryImpl(_SendingRepository):
    """独立域名 repository；域角色由数据库 guard 强制不可变。"""

    async def add(self, domain: SendingDomain) -> None:
        self._require_tenant(domain.tenant_id, "sending_domain_write_tenant")
        self._session.add(_domain_to_row(domain))
        await self._session.flush()

    async def get(self, tenant_id: TenantId, domain: str) -> SendingDomain | None:
        if not self._tenant_matches(tenant_id, "sending_domain_read_tenant"):
            return None
        row = (
            await self._session.execute(
                self.scoped_query(SendingDomainRow).where(SendingDomainRow.domain == domain)
            )
        ).scalar_one_or_none()
        return _row_to_domain(row) if row is not None else None

    async def ensure(self, domain: SendingDomain) -> SendingDomain:
        """精确按 tenant/domain 创建或返回并锁定数据库 winner。"""
        self._require_tenant(domain.tenant_id, "sending_domain_ensure_tenant")
        result = await self._session.execute(
            insert(SendingDomainRow)
            .values(
                tenant_id=domain.tenant_id,
                domain=domain.domain,
                role=domain.role.value,
                created_at=domain.created_at,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "domain"])
            .returning(SendingDomainRow.domain)
        )
        if result.scalar_one_or_none() is not None:
            return domain
        row = (
            await self._session.execute(
                self.scoped_query(SendingDomainRow)
                .where(SendingDomainRow.domain == domain.domain)
                .with_for_update()
            )
        ).scalar_one()
        return _row_to_domain(row)


class SendingIdentityRepositoryImpl(_SendingRepository):
    """身份 repository；scope/auth/state 过滤全部发生在 SQL LIMIT 之前。"""

    def _joined(self) -> Select[tuple[SendingIdentityRow, str]]:
        return (
            select(SendingIdentityRow, SendingDomainRow.role)
            .join(
                SendingDomainRow,
                and_(
                    SendingDomainRow.tenant_id == SendingIdentityRow.tenant_id,
                    SendingDomainRow.domain == SendingIdentityRow.domain,
                ),
            )
            .where(SendingIdentityRow.tenant_id == self._tenant_id)
        )

    async def add(self, identity: SendingIdentity) -> None:
        self._require_tenant(identity.tenant_id, "sending_identity_write_tenant")
        self._session.add(_identity_to_row(identity))
        await self._session.flush()

    async def register_if_address_absent(
        self, identity: SendingIdentity
    ) -> IdentityRegistrationResult:
        """精确按 tenant/address 插入，返回创建行或数据库 winner。"""
        self._require_tenant(identity.tenant_id, "sending_identity_register_tenant")
        row = _identity_to_row(identity)
        values = {
            column.name: getattr(row, column.name)
            for column in SendingIdentityRow.__table__.columns
        }
        result = await self._session.execute(
            insert(SendingIdentityRow)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["tenant_id", "address"])
            .returning(SendingIdentityRow.identity_id)
        )
        if result.scalar_one_or_none() is not None:
            return IdentityRegistrationResult(created=True, winner=identity)
        winner = (
            await self._session.execute(
                self._joined()
                .where(SendingIdentityRow.address == identity.address)
                .with_for_update(of=SendingIdentityRow)
            )
        ).one()
        return IdentityRegistrationResult(
            created=False,
            winner=_row_to_identity(winner[0], winner[1]),
        )

    async def get(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        *,
        for_update: bool = False,
    ) -> SendingIdentity | None:
        if not self._tenant_matches(tenant_id, "sending_identity_read_tenant"):
            return None
        query = self._joined().where(SendingIdentityRow.identity_id == identity_id)
        if for_update:
            query = query.with_for_update(of=SendingIdentityRow)
        result = (await self._session.execute(query)).one_or_none()
        return _row_to_identity(result[0], result[1]) if result is not None else None

    async def update(self, identity: SendingIdentity) -> None:
        self._require_tenant(identity.tenant_id, "sending_identity_update_tenant")
        row = _identity_to_row(identity)
        values = {
            column.name: getattr(row, column.name)
            for column in SendingIdentityRow.__table__.columns
            if column.name
            not in {
                "tenant_id",
                "identity_id",
                "version",
            }
        }
        values["version"] = SendingIdentityRow.version + 1
        await self._session.execute(
            update(SendingIdentityRow)
            .where(
                SendingIdentityRow.tenant_id == self._tenant_id,
                SendingIdentityRow.identity_id == identity.identity_id,
            )
            .values(**values)
        )

    async def find_by_address(
        self, tenant_id: TenantId, address: str
    ) -> SendingIdentity | None:
        if not self._tenant_matches(tenant_id, "sending_identity_address_tenant"):
            return None
        result = (
            await self._session.execute(
                self._joined().where(SendingIdentityRow.address == address)
            )
        ).one_or_none()
        return _row_to_identity(result[0], result[1]) if result is not None else None

    async def list_domain_for_update(
        self, tenant_id: TenantId, domain: str
    ) -> list[SendingIdentity]:
        if not self._tenant_matches(tenant_id, "sending_identity_domain_tenant"):
            return []
        rows = (
            await self._session.execute(
                self._joined()
                .where(SendingIdentityRow.domain == domain)
                .order_by(SendingIdentityRow.identity_id.asc())
                .with_for_update(of=SendingIdentityRow)
            )
        ).all()
        return [_row_to_identity(row, role) for row, role in rows]

    async def find_domain_role(
        self, tenant_id: TenantId, domain: str
    ) -> DomainRole | None:
        if not self._tenant_matches(tenant_id, "sending_identity_role_tenant"):
            return None
        role = (
            await self._session.execute(
                select(SendingDomainRow.role).where(
                    SendingDomainRow.tenant_id == self._tenant_id,
                    SendingDomainRow.domain == domain,
                )
            )
        ).scalar_one_or_none()
        return DomainRole(role) if role is not None else None

    async def list_available_for_campaign(
        self,
        tenant_id: TenantId,
        scope: SendingIdentityScope,
        limit: int,
    ) -> list[SendingIdentity]:
        if not self._tenant_matches(tenant_id, "sending_identity_campaign_tenant"):
            return []
        if not isinstance(scope, SendingIdentityScope) or scope.level is None or limit <= 0:
            return []
        if scope.level is ScopeLevel.SELF:
            return []
        if (
            scope.level is ScopeLevel.SYSTEM
            and (
                scope.allowed_identity_ids is None
                or len(scope.allowed_identity_ids) != 1
            )
        ):
            return []
        if (
            scope.level is ScopeLevel.MANAGER
            and scope.allowed_identity_ids is None
            and scope.allowed_domains is None
        ):
            return []

        latest_auth_id = (
            select(AuthenticationCheckRow.auth_check_id)
            .where(
                AuthenticationCheckRow.tenant_id == SendingIdentityRow.tenant_id,
                AuthenticationCheckRow.identity_id == SendingIdentityRow.identity_id,
            )
            .order_by(
                AuthenticationCheckRow.checked_at.desc(),
                AuthenticationCheckRow.auth_check_id.desc(),
            )
            .limit(1)
            .correlate(SendingIdentityRow)
            .scalar_subquery()
        )
        latest_auth_passed = exists(
            select(1).where(
                AuthenticationCheckRow.tenant_id == SendingIdentityRow.tenant_id,
                AuthenticationCheckRow.identity_id == SendingIdentityRow.identity_id,
                AuthenticationCheckRow.auth_check_id == latest_auth_id,
                AuthenticationCheckRow.spf_passed.is_(True),
                AuthenticationCheckRow.dkim_passed.is_(True),
                AuthenticationCheckRow.dmarc_passed.is_(True),
            )
        )
        query = self._joined().where(
            SendingDomainRow.role == DomainRole.COLD_OUTREACH.value,
            SendingIdentityRow.state.in_(
                [IdentityState.WARMING.value, IdentityState.ACTIVE.value]
            ),
            latest_auth_passed,
        )
        if scope.allowed_identity_ids is not None:
            if not scope.allowed_identity_ids:
                return []
            query = query.where(
                SendingIdentityRow.identity_id.in_(
                    sorted(str(item) for item in scope.allowed_identity_ids)
                )
            )
        if scope.allowed_domains is not None:
            if not scope.allowed_domains:
                return []
            query = query.where(
                SendingIdentityRow.domain.in_(sorted(scope.allowed_domains))
            )
        rows = (
            await self._session.execute(
                query.order_by(
                    SendingIdentityRow.created_at.asc(),
                    SendingIdentityRow.identity_id.asc(),
                ).limit(limit)
            )
        ).all()
        return [_row_to_identity(row, role) for row, role in rows]


class AuthenticationCheckRepositoryImpl(_SendingRepository):
    """认证结果只增 repository。"""

    async def add(self, record: AuthenticationCheckRecord) -> None:
        self._require_tenant(record.tenant_id, "sending_auth_write_tenant")
        self._session.add(_auth_to_row(record))
        await self._session.flush()

    async def append_if_ref_absent(
        self, record: AuthenticationCheckRecord
    ) -> AuthenticationAppendResult:
        """精确按 tenant/identity/check_ref 追加或返回数据库 winner。"""
        self._require_tenant(record.tenant_id, "sending_auth_append_tenant")
        row = _auth_to_row(record)
        values = {
            column.name: getattr(row, column.name)
            for column in AuthenticationCheckRow.__table__.columns
        }
        result = await self._session.execute(
            insert(AuthenticationCheckRow)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "identity_id", "check_ref"]
            )
            .returning(AuthenticationCheckRow.auth_check_id)
        )
        if result.scalar_one_or_none() is not None:
            return AuthenticationAppendResult(created=True, winner=record)
        winner = (
            await self._session.execute(
                self.scoped_query(AuthenticationCheckRow).where(
                    AuthenticationCheckRow.identity_id == record.identity_id,
                    AuthenticationCheckRow.check_ref == record.result.check_ref,
                )
            )
        ).scalar_one()
        return AuthenticationAppendResult(
            created=False,
            winner=_row_to_auth(winner),
        )

    async def latest_for_identity(
        self, tenant_id: TenantId, identity_id: SendingIdentityId
    ) -> AuthenticationCheckRecord | None:
        if not self._tenant_matches(tenant_id, "sending_auth_read_tenant"):
            return None
        row = (
            await self._session.execute(
                self.scoped_query(AuthenticationCheckRow)
                .where(AuthenticationCheckRow.identity_id == identity_id)
                .order_by(
                    AuthenticationCheckRow.checked_at.desc(),
                    AuthenticationCheckRow.auth_check_id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return _row_to_auth(row) if row is not None else None


class AuthenticationCheckRequestRepositoryImpl(_SendingRepository):
    """认证请求的 tenant 幂等创建与严格状态转换。"""

    async def create_or_get(
        self, request: AuthenticationCheckRequestView
    ) -> AuthenticationCheckRequestCreateResult:
        self._require_tenant(request.tenant_id, "sending_auth_request_create_tenant")
        result = await self._session.execute(
            insert(AuthenticationCheckRequestRow)
            .values(
                tenant_id=request.tenant_id,
                request_id=request.request_id,
                sending_identity_id=request.sending_identity_id,
                request_key=request.request_key,
                status=request.status.value,
                requested_at=request.requested_at,
                completed_at=request.completed_at,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "request_key"])
            .returning(AuthenticationCheckRequestRow.request_id)
        )
        if result.scalar_one_or_none() is not None:
            return AuthenticationCheckRequestCreateResult(True, request)
        row = (
            await self._session.execute(
                self.scoped_query(AuthenticationCheckRequestRow)
                .where(AuthenticationCheckRequestRow.request_key == request.request_key)
                .with_for_update()
            )
        ).scalar_one()
        return AuthenticationCheckRequestCreateResult(False, _row_to_auth_request(row))

    async def get(
        self,
        tenant_id: TenantId,
        request_id: AuthenticationCheckRequestId,
        *,
        for_update: bool = False,
    ) -> AuthenticationCheckRequestView | None:
        if not self._tenant_matches(tenant_id, "sending_auth_request_read_tenant"):
            return None
        query = self.scoped_query(AuthenticationCheckRequestRow).where(
            AuthenticationCheckRequestRow.request_id == request_id
        )
        if for_update:
            query = query.with_for_update()
        row = (await self._session.execute(query)).scalar_one_or_none()
        return _row_to_auth_request(row) if row is not None else None

    async def transition(
        self,
        tenant_id: TenantId,
        request_id: AuthenticationCheckRequestId,
        target: AuthenticationCheckRequestStatus,
        completed_at: datetime | None,
    ) -> AuthenticationCheckRequestView:
        self._require_tenant(tenant_id, "sending_auth_request_transition_tenant")
        current = await self.get(tenant_id, request_id, for_update=True)
        allowed = {
            AuthenticationCheckRequestStatus.REQUESTED: {
                AuthenticationCheckRequestStatus.RUNNING
            },
            AuthenticationCheckRequestStatus.RUNNING: {
                AuthenticationCheckRequestStatus.SUCCEEDED,
                AuthenticationCheckRequestStatus.FAILED,
            },
        }
        if current is None or target not in allowed.get(current.status, set()):
            raise InvalidStateTransition("认证检查请求状态转换无效")
        terminal = target in {
            AuthenticationCheckRequestStatus.SUCCEEDED,
            AuthenticationCheckRequestStatus.FAILED,
        }
        if terminal != (completed_at is not None):
            raise InvalidStateTransition("认证检查请求完成时间无效")
        await self._session.execute(
            update(AuthenticationCheckRequestRow)
            .where(
                AuthenticationCheckRequestRow.tenant_id == self._tenant_id,
                AuthenticationCheckRequestRow.request_id == request_id,
                AuthenticationCheckRequestRow.status == current.status.value,
            )
            .values(status=target.value, completed_at=completed_at)
        )
        return AuthenticationCheckRequestView(
            current.request_id,
            current.tenant_id,
            current.sending_identity_id,
            current.request_key,
            target,
            current.requested_at,
            completed_at,
        )


def _empty_window(window_days: int, computed_at: datetime) -> ReputationWindow:
    return ReputationWindow(
        window_days=window_days,
        computed_at=computed_at,
        sent_attempts=0,
        delivered=0,
        hard_bounced=0,
        soft_bounced=0,
        complaints=0,
        unsubscribed=0,
        spam_trap_hits=0,
        blocklist_hits=0,
    )


def _event_count_columns() -> tuple[ColumnElement[int], ...]:
    event_id = ReputationEventRow.reputation_event_id
    return tuple(
        func.count(event_id).filter(ReputationEventRow.event_type == event.value)
        for event in DeliveryEventType
    )


def _window_from_counts(
    window_days: int,
    computed_at: datetime,
    sent_attempts: int,
    counts: tuple[int, ...],
) -> ReputationWindow:
    by_type = dict(zip(DeliveryEventType, counts, strict=True))
    return ReputationWindow(
        window_days=window_days,
        computed_at=computed_at,
        sent_attempts=sent_attempts,
        delivered=by_type[DeliveryEventType.DELIVERED],
        hard_bounced=by_type[DeliveryEventType.HARD_BOUNCED],
        soft_bounced=by_type[DeliveryEventType.SOFT_BOUNCED],
        complaints=by_type[DeliveryEventType.COMPLAINT],
        unsubscribed=by_type[DeliveryEventType.UNSUBSCRIBED],
        spam_trap_hits=by_type[DeliveryEventType.SPAM_TRAP],
        blocklist_hits=by_type[DeliveryEventType.BLOCKLISTED],
    )


class ReputationRepositoryImpl(_SendingRepository):
    """信誉事实写入与 immutable reservation 滚动窗口聚合。"""

    async def record_event(self, event: DeliveryEventRecord) -> bool:
        self._require_tenant(event.tenant_id, "sending_reputation_write_tenant")
        result = await self._session.execute(
            insert(ReputationEventRow)
            .values(
                tenant_id=event.tenant_id,
                reputation_event_id=new_id("rep"),
                identity_id=event.identity_id,
                event_type=event.event_type.value,
                occurred_at=event.occurred_at,
                dedup_key=event.dedup_key,
                source_ref=event.source_ref,
                created_at=event.occurred_at,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "dedup_key"])
            .returning(ReputationEventRow.reputation_event_id)
        )
        return result.scalar_one_or_none() is not None

    async def compute_window(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        window_days: int,
        computed_at: datetime,
    ) -> ReputationWindow:
        if not self._tenant_matches(tenant_id, "sending_reputation_read_tenant"):
            return _empty_window(window_days, computed_at)
        start = computed_at - timedelta(days=window_days)
        sent_attempts = cast(
            int,
            (
                await self._session.execute(
                    select(func.count(SendReservationRow.reservation_id)).where(
                        SendReservationRow.tenant_id == self._tenant_id,
                        SendReservationRow.identity_id == identity_id,
                        SendReservationRow.created_at >= start,
                        SendReservationRow.created_at <= computed_at,
                    )
                )
            ).scalar_one(),
        )
        counts = cast(
            tuple[int, ...],
            (
                await self._session.execute(
                    select(*_event_count_columns()).where(
                        ReputationEventRow.tenant_id == self._tenant_id,
                        ReputationEventRow.identity_id == identity_id,
                        ReputationEventRow.occurred_at >= start,
                        ReputationEventRow.occurred_at <= computed_at,
                    )
                )
            ).one(),
        )
        return _window_from_counts(window_days, computed_at, sent_attempts, counts)

    async def compute_domain_window(
        self,
        tenant_id: TenantId,
        domain: str,
        window_days: int,
        computed_at: datetime,
    ) -> ReputationWindow:
        if not self._tenant_matches(tenant_id, "sending_reputation_domain_tenant"):
            return _empty_window(window_days, computed_at)
        start = computed_at - timedelta(days=window_days)
        sent_attempts = cast(
            int,
            (
                await self._session.execute(
                    select(func.count(SendReservationRow.reservation_id))
                    .join(
                        SendingIdentityRow,
                        and_(
                            SendingIdentityRow.tenant_id == SendReservationRow.tenant_id,
                            SendingIdentityRow.identity_id == SendReservationRow.identity_id,
                        ),
                    )
                    .where(
                        SendingIdentityRow.tenant_id == self._tenant_id,
                        SendingIdentityRow.domain == domain,
                        SendReservationRow.created_at >= start,
                        SendReservationRow.created_at <= computed_at,
                    )
                )
            ).scalar_one(),
        )
        counts = cast(
            tuple[int, ...],
            (
                await self._session.execute(
                    select(*_event_count_columns())
                    .join(
                        SendingIdentityRow,
                        and_(
                            SendingIdentityRow.tenant_id == ReputationEventRow.tenant_id,
                            SendingIdentityRow.identity_id == ReputationEventRow.identity_id,
                        ),
                    )
                    .where(
                        SendingIdentityRow.tenant_id == self._tenant_id,
                        SendingIdentityRow.domain == domain,
                        ReputationEventRow.occurred_at >= start,
                        ReputationEventRow.occurred_at <= computed_at,
                    )
                )
            ).one(),
        )
        return _window_from_counts(window_days, computed_at, sent_attempts, counts)


class SendCounterRepositoryImpl(_SendingRepository):
    """每日发送计数只读入口；增量只允许 reservation repository 执行。"""

    async def get_count(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        on_day: date,
    ) -> int:
        if not self._tenant_matches(tenant_id, "sending_counter_read_tenant"):
            return 0
        value = (
            await self._session.execute(
                select(SendCounterRow.sent_attempts).where(
                    SendCounterRow.tenant_id == self._tenant_id,
                    SendCounterRow.identity_id == identity_id,
                    SendCounterRow.on_day == on_day,
                )
            )
        ).scalar_one_or_none()
        return int(value) if value is not None else 0


def _reservation_result(
    row: SendReservationRow,
    daily_limit: int,
    sent_attempts: int,
    outcome: ReservationOutcome,
) -> ReservationResult:
    return ReservationResult(
        outcome=outcome,
        reservation=SendReservation(
            reservation_id=row.reservation_id,
            identity_id=SendingIdentityId(row.identity_id),
            reservation_key=IdempotencyKey(row.reservation_key),
            on_day=row.on_day,
            sequence=row.sequence,
            daily_limit=daily_limit,
            remaining_today=max(0, daily_limit - sent_attempts),
        ),
        sent_attempts=sent_attempts,
    )


class SendReservationRepositoryImpl(_SendingRepository):
    """用 counter 行锁与精确 conflict target 原子预留发送名额。"""

    async def _existing(
        self, identity_id: SendingIdentityId, reservation_key: IdempotencyKey
    ) -> SendReservationRow | None:
        return (
            await self._session.execute(
                self.scoped_query(SendReservationRow).where(
                    SendReservationRow.identity_id == identity_id,
                    SendReservationRow.reservation_key == reservation_key,
                )
            )
        ).scalar_one_or_none()

    async def _count(
        self, identity_id: SendingIdentityId, on_day: date
    ) -> int:
        value = (
            await self._session.execute(
                select(SendCounterRow.sent_attempts).where(
                    SendCounterRow.tenant_id == self._tenant_id,
                    SendCounterRow.identity_id == identity_id,
                    SendCounterRow.on_day == on_day,
                )
            )
        ).scalar_one_or_none()
        return int(value) if value is not None else 0

    async def reserve_if_below(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        reservation_key: IdempotencyKey,
        on_day: date,
        daily_limit: int,
        created_at: datetime,
    ) -> ReservationResult:
        self._require_tenant(tenant_id, "sending_reservation_write_tenant")
        if isinstance(daily_limit, bool) or not isinstance(daily_limit, int) or daily_limit < 0:
            raise ValueError("daily_limit 必须为非负整数")
        existing = await self._existing(identity_id, reservation_key)
        if existing is not None:
            sent_attempts = await self._count(identity_id, existing.on_day)
            return _reservation_result(
                existing, daily_limit, sent_attempts, ReservationOutcome.EXISTING
            )

        await self._session.execute(
            insert(SendCounterRow)
            .values(
                tenant_id=self._tenant_id,
                identity_id=identity_id,
                on_day=on_day,
                sent_attempts=0,
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "identity_id", "on_day"]
            )
        )
        counter = (
            await self._session.execute(
                select(SendCounterRow)
                .where(
                    SendCounterRow.tenant_id == self._tenant_id,
                    SendCounterRow.identity_id == identity_id,
                    SendCounterRow.on_day == on_day,
                )
                .with_for_update()
            )
        ).scalar_one()

        existing = await self._existing(identity_id, reservation_key)
        if existing is not None:
            return _reservation_result(
                existing,
                daily_limit,
                counter.sent_attempts,
                ReservationOutcome.EXISTING,
            )
        if counter.sent_attempts >= daily_limit:
            return ReservationResult(
                outcome=ReservationOutcome.CAP_REACHED,
                reservation=None,
                sent_attempts=counter.sent_attempts,
            )

        sequence = counter.sent_attempts + 1
        reservation_id = new_id("res")
        result = await self._session.execute(
            insert(SendReservationRow)
            .values(
                tenant_id=self._tenant_id,
                reservation_id=reservation_id,
                identity_id=identity_id,
                reservation_key=reservation_key,
                on_day=on_day,
                sequence=sequence,
                created_at=created_at,
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "identity_id", "reservation_key"]
            )
            .returning(SendReservationRow.reservation_id)
        )
        if result.scalar_one_or_none() is None:
            existing = await self._existing(identity_id, reservation_key)
            if existing is None:
                raise RuntimeError("reservation 幂等冲突后未找到原记录")
            sent_attempts = (
                counter.sent_attempts
                if existing.on_day == on_day
                else await self._count(identity_id, existing.on_day)
            )
            return _reservation_result(
                existing,
                daily_limit,
                sent_attempts,
                ReservationOutcome.EXISTING,
            )
        counter.sent_attempts = sequence
        row = SendReservationRow(
            tenant_id=str(self._tenant_id),
            reservation_id=reservation_id,
            identity_id=str(identity_id),
            reservation_key=str(reservation_key),
            on_day=on_day,
            sequence=sequence,
            created_at=created_at,
        )
        return _reservation_result(row, daily_limit, sequence, ReservationOutcome.CREATED)


class IdentityActionRepositoryImpl(_SendingRepository):
    """身份动作只增审计 repository。"""

    async def add(self, record: IdentityActionRecord) -> None:
        self._require_tenant(record.tenant_id, "sending_action_write_tenant")
        self._session.add(
            IdentityActionRow(
                tenant_id=record.tenant_id,
                action_id=record.action_id,
                identity_id=record.identity_id,
                action_key=record.action_key,
                action=record.action.value,
                before_state=(
                    record.before_state.value if record.before_state is not None else None
                ),
                after_state=(
                    record.after_state.value if record.after_state is not None else None
                ),
                actor_id=record.actor_id,
                scope=record.scope,
                rule=record.rule,
                note=record.note,
                occurred_at=record.occurred_at,
            )
        )

    async def exists_by_key(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        action_key: str,
    ) -> bool:
        if not self._tenant_matches(tenant_id, "sending_action_read_tenant"):
            return False
        return bool(
            (
                await self._session.execute(
                    select(
                        exists().where(
                            IdentityActionRow.tenant_id == self._tenant_id,
                            IdentityActionRow.identity_id == identity_id,
                            IdentityActionRow.action_key == action_key,
                        )
                    )
                )
            ).scalar_one()
        )
