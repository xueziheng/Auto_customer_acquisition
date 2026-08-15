"""触达域六类 tenant-bound PostgreSQL repository。"""

from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domains.outreach.models import (
    ActionRecord,
    Campaign,
    CampaignBoundary,
    CampaignState,
    CampaignVersion,
    DailyQuotaUsage,
    Enrollment,
    EnrollmentState,
    EnrollmentStopReason,
    MessageAttempt,
    MessageAttemptState,
    SendFailureCategory,
    SequenceStepSpec,
    StepIntent,
    SuppressionEntry,
    SuppressionReason,
)
from domains.outreach.permissions import OutreachScope, ScopeLevel
from domains.outreach.repository import (
    AppendStatus,
    DeliveryCorrelationBindResult,
    DeliveryCorrelationBindStatus,
    EnrollmentInsertResult,
    EnrollmentInsertStatus,
    MessageAttemptCreateResult,
    QuotaReservationResult,
    QuotaReservationStatus,
    SuppressionAppendResult,
)
from domains.outreach.schemas import SuppressionTarget
from infra.db.base import TenantScopedRepository
from infra.db.tables import (
    OutreachActionRow,
    OutreachCampaignRow,
    OutreachCampaignVersionRow,
    OutreachDailyQuotaRow,
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    OutreachSequenceStepRow,
    OutreachSuppressionRow,
)
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    IdempotencyKey,
    MessageAttemptId,
    MessageId,
    ProspectAccountId,
    SendingIdentityId,
    SuppressionId,
    TenantId,
)

_tenant_logger = logging.getLogger("security.tenant_isolation")


class _OutreachRepository(TenantScopedRepository):
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        super().__init__(tenant_id)
        self._session = session

    def _tenant_matches(self, tenant_id: TenantId, action: str) -> bool:
        if tenant_id == self._tenant_id:
            return True
        _tenant_logger.critical(
            "检测到跨租户数据隔离违规",
            extra={"action": action, "tenant_id": str(self._tenant_id)},
        )
        return False

    def _require_tenant(self, tenant_id: TenantId, action: str) -> None:
        if not self._tenant_matches(tenant_id, action):
            raise TenantIsolationViolation("跨租户数据隔离违规")


def _campaign_to_row(campaign: Campaign) -> OutreachCampaignRow:
    return OutreachCampaignRow(
        tenant_id=str(campaign.tenant_id),
        campaign_id=str(campaign.campaign_id),
        state=campaign.state.value,
        current_version=campaign.current_version,
        created_by=str(campaign.created_by),
        created_at=campaign.created_at,
        round_robin_cursor=campaign.round_robin_cursor,
        approval_id=campaign.approval_id,
        approved_by=str(campaign.approved_by) if campaign.approved_by else None,
        approved_at=campaign.approved_at,
        paused_reason=campaign.paused_reason,
    )


def _row_to_campaign(row: OutreachCampaignRow) -> Campaign:
    return Campaign(
        tenant_id=TenantId(row.tenant_id),
        campaign_id=CampaignId(row.campaign_id),
        state=CampaignState(row.state),
        current_version=row.current_version,
        created_by=EmployeeId(row.created_by),
        created_at=row.created_at,
        round_robin_cursor=row.round_robin_cursor,
        approval_id=row.approval_id,
        approved_by=EmployeeId(row.approved_by) if row.approved_by else None,
        approved_at=row.approved_at,
        paused_reason=row.paused_reason,
    )


def _version_row(version: CampaignVersion) -> OutreachCampaignVersionRow:
    boundary = version.boundary
    return OutreachCampaignVersionRow(
        tenant_id=str(version.tenant_id),
        campaign_id=str(version.campaign_id),
        version=version.version,
        name=version.name,
        markets=list(boundary.markets),
        target_entity_types=list(boundary.target_entity_types),
        allowed_categories=list(boundary.allowed_categories),
        sender_identity_ids=[str(value) for value in boundary.sender_identity_ids],
        daily_new_contact_limit=boundary.daily_new_contact_limit,
        daily_total_message_limit=boundary.daily_total_message_limit,
        handoff_triggers=list(boundary.handoff_triggers),
        stop_on_reply=boundary.stop_on_reply,
        created_by=str(version.created_by),
        created_at=version.created_at,
    )


def _row_to_version(
    row: OutreachCampaignVersionRow,
    step_rows: list[OutreachSequenceStepRow],
) -> CampaignVersion:
    return CampaignVersion(
        tenant_id=TenantId(row.tenant_id),
        campaign_id=CampaignId(row.campaign_id),
        version=row.version,
        name=row.name,
        boundary=CampaignBoundary(
            markets=tuple(row.markets),
            target_entity_types=tuple(row.target_entity_types),
            allowed_categories=tuple(row.allowed_categories),
            sender_identity_ids=tuple(
                SendingIdentityId(value) for value in row.sender_identity_ids
            ),
            steps=tuple(
                SequenceStepSpec(
                    step.step_number, StepIntent(step.intent), step.wait_days
                )
                for step in step_rows
            ),
            daily_new_contact_limit=row.daily_new_contact_limit,
            daily_total_message_limit=row.daily_total_message_limit,
            handoff_triggers=tuple(row.handoff_triggers),
            stop_on_reply=row.stop_on_reply,
        ),
        created_by=EmployeeId(row.created_by),
        created_at=row.created_at,
    )


def _enrollment_row(enrollment: Enrollment) -> OutreachEnrollmentRow:
    return OutreachEnrollmentRow(
        tenant_id=str(enrollment.tenant_id),
        enrollment_id=str(enrollment.enrollment_id),
        campaign_id=str(enrollment.campaign_id),
        campaign_version=enrollment.campaign_version,
        account_id=str(enrollment.account_id),
        contact_point_id=str(enrollment.contact_point_id),
        sending_identity_id=str(enrollment.sending_identity_id),
        state=enrollment.state.value,
        current_step=enrollment.current_step,
        next_send_at=enrollment.next_send_at,
        enrolled_at=enrollment.enrolled_at,
        stopped_at=enrollment.stopped_at,
        stop_reason=enrollment.stop_reason.value if enrollment.stop_reason else None,
        idempotency_key=str(enrollment.idempotency_key),
    )


def _row_to_enrollment(row: OutreachEnrollmentRow) -> Enrollment:
    return Enrollment(
        tenant_id=TenantId(row.tenant_id),
        enrollment_id=EnrollmentId(row.enrollment_id),
        campaign_id=CampaignId(row.campaign_id),
        campaign_version=row.campaign_version,
        account_id=ProspectAccountId(row.account_id),
        contact_point_id=ContactPointId(row.contact_point_id),
        sending_identity_id=SendingIdentityId(row.sending_identity_id),
        state=EnrollmentState(row.state),
        current_step=row.current_step,
        next_send_at=row.next_send_at,
        enrolled_at=row.enrolled_at,
        stopped_at=row.stopped_at,
        stop_reason=EnrollmentStopReason(row.stop_reason) if row.stop_reason else None,
        idempotency_key=IdempotencyKey(row.idempotency_key),
    )


def _suppression_row(entry: SuppressionEntry) -> OutreachSuppressionRow:
    return OutreachSuppressionRow(
        tenant_id=str(entry.tenant_id),
        suppression_id=str(entry.suppression_id),
        contact_point_id=(
            str(entry.target.contact_point_id)
            if entry.target.contact_point_id is not None
            else None
        ),
        account_id=(
            str(entry.target.account_id)
            if entry.target.account_id is not None
            else None
        ),
        reason=entry.reason.value,
        occurred_at=entry.occurred_at,
        source_ref=entry.source_ref,
        idempotency_key=str(entry.idempotency_key),
        created_at=entry.created_at,
    )


def _row_to_suppression(row: OutreachSuppressionRow) -> SuppressionEntry:
    return SuppressionEntry(
        tenant_id=TenantId(row.tenant_id),
        suppression_id=SuppressionId(row.suppression_id),
        target=SuppressionTarget(
            contact_point_id=(
                ContactPointId(row.contact_point_id) if row.contact_point_id else None
            ),
            account_id=ProspectAccountId(row.account_id) if row.account_id else None,
        ),
        reason=SuppressionReason(row.reason),
        occurred_at=row.occurred_at,
        source_ref=row.source_ref,
        idempotency_key=IdempotencyKey(row.idempotency_key),
        created_at=row.created_at,
    )


def _attempt_row(attempt: MessageAttempt) -> OutreachMessageAttemptRow:
    return OutreachMessageAttemptRow(
        tenant_id=str(attempt.tenant_id),
        attempt_id=str(attempt.attempt_id),
        message_id=str(attempt.message_id),
        campaign_id=str(attempt.campaign_id),
        enrollment_id=str(attempt.enrollment_id),
        campaign_version=attempt.campaign_version,
        step_number=attempt.step_number,
        sending_identity_id=str(attempt.sending_identity_id),
        idempotency_key=str(attempt.idempotency_key),
        state=attempt.state.value,
        provider_ref=attempt.provider_ref,
        failure_category=(
            attempt.failure_category.value if attempt.failure_category else None
        ),
        created_at=attempt.created_at,
        updated_at=attempt.updated_at,
        send_claimed_at=attempt.send_claimed_at,
        deterministic_message_id=attempt.deterministic_message_id,
        idempotency_header=attempt.idempotency_header,
    )


def _row_to_attempt(row: OutreachMessageAttemptRow) -> MessageAttempt:
    return MessageAttempt(
        tenant_id=TenantId(row.tenant_id),
        attempt_id=MessageAttemptId(row.attempt_id),
        message_id=MessageId(row.message_id),
        campaign_id=CampaignId(row.campaign_id),
        enrollment_id=EnrollmentId(row.enrollment_id),
        campaign_version=row.campaign_version,
        step_number=row.step_number,
        sending_identity_id=SendingIdentityId(row.sending_identity_id),
        idempotency_key=IdempotencyKey(row.idempotency_key),
        state=MessageAttemptState(row.state),
        provider_ref=row.provider_ref,
        failure_category=(
            SendFailureCategory(row.failure_category) if row.failure_category else None
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
        send_claimed_at=row.send_claimed_at,
        deterministic_message_id=row.deterministic_message_id,
        idempotency_header=row.idempotency_header,
    )


class CampaignRepositoryImpl(_OutreachRepository):
    async def add(self, campaign: Campaign, version: CampaignVersion) -> None:
        self._require_tenant(campaign.tenant_id, "outreach_campaign_add")
        self._require_tenant(version.tenant_id, "outreach_version_add")
        self._session.add(_campaign_to_row(campaign))
        await self._session.flush()
        await self.append_version(version)

    async def append_version(self, version: CampaignVersion) -> None:
        self._require_tenant(version.tenant_id, "outreach_version_append")
        self._session.add(_version_row(version))
        await self._session.flush()
        self._session.add_all(
            [
                OutreachSequenceStepRow(
                    tenant_id=str(version.tenant_id),
                    campaign_id=str(version.campaign_id),
                    version=version.version,
                    step_number=step.step_number,
                    intent=step.intent.value,
                    wait_days=step.wait_days,
                )
                for step in version.boundary.steps
            ]
        )
        await self._session.flush()

    async def get(
        self, tenant_id: TenantId, campaign_id: CampaignId
    ) -> Campaign | None:
        if not self._tenant_matches(tenant_id, "outreach_campaign_get"):
            return None
        row = await self._session.get(
            OutreachCampaignRow, (str(self._tenant_id), str(campaign_id))
        )
        return _row_to_campaign(row) if row else None

    async def get_for_update(
        self, tenant_id: TenantId, campaign_id: CampaignId
    ) -> Campaign | None:
        if not self._tenant_matches(tenant_id, "outreach_campaign_lock"):
            return None
        row = (
            await self._session.execute(
                select(OutreachCampaignRow)
                .where(
                    OutreachCampaignRow.tenant_id == self._tenant_id,
                    OutreachCampaignRow.campaign_id == campaign_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        return _row_to_campaign(row) if row else None

    async def get_version(
        self, tenant_id: TenantId, campaign_id: CampaignId, version: int
    ) -> CampaignVersion | None:
        if not self._tenant_matches(tenant_id, "outreach_version_get"):
            return None
        row = await self._session.get(
            OutreachCampaignVersionRow,
            (str(self._tenant_id), str(campaign_id), version),
        )
        if row is None:
            return None
        steps = (
            (
                await self._session.execute(
                    select(OutreachSequenceStepRow)
                    .where(
                        OutreachSequenceStepRow.tenant_id == self._tenant_id,
                        OutreachSequenceStepRow.campaign_id == campaign_id,
                        OutreachSequenceStepRow.version == version,
                    )
                    .order_by(OutreachSequenceStepRow.step_number.asc())
                )
            )
            .scalars()
            .all()
        )
        return _row_to_version(row, list(steps))

    async def update(self, campaign: Campaign) -> None:
        self._require_tenant(campaign.tenant_id, "outreach_campaign_update")
        await self._session.execute(
            update(OutreachCampaignRow)
            .where(
                OutreachCampaignRow.tenant_id == self._tenant_id,
                OutreachCampaignRow.campaign_id == campaign.campaign_id,
            )
            .values(
                state=campaign.state.value,
                current_version=campaign.current_version,
                round_robin_cursor=campaign.round_robin_cursor,
                approval_id=campaign.approval_id,
                approved_by=campaign.approved_by,
                approved_at=campaign.approved_at,
                paused_reason=campaign.paused_reason,
            )
        )

    async def list_scoped(
        self, tenant_id: TenantId, scope: OutreachScope, limit: int
    ) -> list[Campaign]:
        if not self._tenant_matches(tenant_id, "outreach_campaign_list"):
            return []
        if scope.level not in {ScopeLevel.TENANT, ScopeLevel.MANAGER, ScopeLevel.SELF}:
            return []
        query = select(OutreachCampaignRow).where(
            OutreachCampaignRow.tenant_id == self._tenant_id
        )
        if scope.level is not ScopeLevel.TENANT:
            if not scope.allowed_campaign_ids:
                return []
            query = query.where(
                OutreachCampaignRow.campaign_id.in_(scope.allowed_campaign_ids)
            )
        rows = (
            (
                await self._session.execute(
                    query.order_by(
                        OutreachCampaignRow.created_at.asc(),
                        OutreachCampaignRow.campaign_id.asc(),
                    ).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_row_to_campaign(row) for row in rows]


class EnrollmentRepositoryImpl(_OutreachRepository):
    async def insert_if_absent(self, enrollment: Enrollment) -> EnrollmentInsertResult:
        self._require_tenant(enrollment.tenant_id, "outreach_enrollment_insert")
        key_lock = f"{self._tenant_id}:enrollment-key:{enrollment.idempotency_key}"
        account_lock = f"{self._tenant_id}:account:{enrollment.account_id}"
        for lock_key in (key_lock, account_lock):
            await self._session.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
            )
        existing = (
            await self._session.execute(
                select(OutreachEnrollmentRow).where(
                    OutreachEnrollmentRow.tenant_id == self._tenant_id,
                    OutreachEnrollmentRow.idempotency_key == enrollment.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            winner = _row_to_enrollment(existing)
            status = (
                EnrollmentInsertStatus.EXISTING
                if winner == enrollment
                else EnrollmentInsertStatus.IDEMPOTENCY_CONFLICT
            )
            return EnrollmentInsertResult(
                status,
                winner if status is EnrollmentInsertStatus.EXISTING else None,
            )
        active = await self.find_active_for_account(
            self._tenant_id, enrollment.account_id
        )
        if active is not None:
            return EnrollmentInsertResult(EnrollmentInsertStatus.ACCOUNT_CONFLICT, None)
        self._session.add(_enrollment_row(enrollment))
        await self._session.flush()
        return EnrollmentInsertResult(EnrollmentInsertStatus.CREATED, enrollment)

    async def get(
        self, tenant_id: TenantId, enrollment_id: EnrollmentId
    ) -> Enrollment | None:
        if not self._tenant_matches(tenant_id, "outreach_enrollment_get"):
            return None
        row = await self._session.get(
            OutreachEnrollmentRow, (str(self._tenant_id), str(enrollment_id))
        )
        return _row_to_enrollment(row) if row else None

    async def get_by_key(
        self, tenant_id: TenantId, key: IdempotencyKey
    ) -> Enrollment | None:
        if not self._tenant_matches(tenant_id, "outreach_enrollment_key"):
            return None
        row = (
            await self._session.execute(
                select(OutreachEnrollmentRow).where(
                    OutreachEnrollmentRow.tenant_id == self._tenant_id,
                    OutreachEnrollmentRow.idempotency_key == key,
                )
            )
        ).scalar_one_or_none()
        return _row_to_enrollment(row) if row else None

    async def get_for_update(
        self, tenant_id: TenantId, enrollment_id: EnrollmentId
    ) -> Enrollment | None:
        if not self._tenant_matches(tenant_id, "outreach_enrollment_lock"):
            return None
        row = (
            await self._session.execute(
                select(OutreachEnrollmentRow)
                .where(
                    OutreachEnrollmentRow.tenant_id == self._tenant_id,
                    OutreachEnrollmentRow.enrollment_id == enrollment_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        return _row_to_enrollment(row) if row else None

    async def find_active_for_account(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> Enrollment | None:
        if not self._tenant_matches(tenant_id, "outreach_enrollment_account"):
            return None
        row = (
            await self._session.execute(
                select(OutreachEnrollmentRow).where(
                    OutreachEnrollmentRow.tenant_id == self._tenant_id,
                    OutreachEnrollmentRow.account_id == account_id,
                    OutreachEnrollmentRow.state.in_(("enrolled", "in_sequence")),
                )
            )
        ).scalar_one_or_none()
        return _row_to_enrollment(row) if row else None

    async def lock_matching_active(
        self, tenant_id: TenantId, target: SuppressionTarget
    ) -> list[Enrollment]:
        if not self._tenant_matches(tenant_id, "outreach_enrollment_target_lock"):
            return []
        query = select(OutreachEnrollmentRow).where(
            OutreachEnrollmentRow.tenant_id == self._tenant_id,
            OutreachEnrollmentRow.state.in_(("enrolled", "in_sequence")),
        )
        if target.contact_point_id is not None:
            query = query.where(
                OutreachEnrollmentRow.contact_point_id == target.contact_point_id
            )
        else:
            query = query.where(OutreachEnrollmentRow.account_id == target.account_id)
        campaign_ids = (
            (
                await self._session.execute(
                    query.with_only_columns(
                        OutreachEnrollmentRow.campaign_id
                    ).distinct()
                )
            )
            .scalars()
            .all()
        )
        if campaign_ids:
            await self._session.execute(
                select(OutreachCampaignRow)
                .where(
                    OutreachCampaignRow.tenant_id == self._tenant_id,
                    OutreachCampaignRow.campaign_id.in_(campaign_ids),
                )
                .order_by(OutreachCampaignRow.campaign_id.asc())
                .with_for_update()
            )
        rows = (
            (
                await self._session.execute(
                    query.order_by(
                        OutreachEnrollmentRow.enrollment_id.asc()
                    ).with_for_update()
                )
            )
            .scalars()
            .all()
        )
        return [_row_to_enrollment(row) for row in rows]

    async def update(self, enrollment: Enrollment) -> None:
        self._require_tenant(enrollment.tenant_id, "outreach_enrollment_update")
        await self._session.execute(
            update(OutreachEnrollmentRow)
            .where(
                OutreachEnrollmentRow.tenant_id == self._tenant_id,
                OutreachEnrollmentRow.enrollment_id == enrollment.enrollment_id,
            )
            .values(
                state=enrollment.state.value,
                current_step=enrollment.current_step,
                next_send_at=enrollment.next_send_at,
                stopped_at=enrollment.stopped_at,
                stop_reason=(
                    enrollment.stop_reason.value if enrollment.stop_reason else None
                ),
            )
        )

    async def list_scoped(
        self, tenant_id: TenantId, scope: OutreachScope, limit: int
    ) -> list[Enrollment]:
        if not self._tenant_matches(tenant_id, "outreach_enrollment_list"):
            return []
        if scope.level not in {ScopeLevel.TENANT, ScopeLevel.MANAGER, ScopeLevel.SELF}:
            return []
        query = select(OutreachEnrollmentRow).where(
            OutreachEnrollmentRow.tenant_id == self._tenant_id
        )
        for column, allowed in (
            (OutreachEnrollmentRow.campaign_id, scope.allowed_campaign_ids),
            (OutreachEnrollmentRow.account_id, scope.allowed_account_ids),
            (OutreachEnrollmentRow.enrollment_id, scope.allowed_enrollment_ids),
        ):
            if allowed is not None:
                if not allowed:
                    return []
                query = query.where(column.in_(allowed))
        rows = (
            (
                await self._session.execute(
                    query.order_by(
                        OutreachEnrollmentRow.enrolled_at.asc(),
                        OutreachEnrollmentRow.enrollment_id.asc(),
                    ).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_row_to_enrollment(row) for row in rows]


    async def list_due_for_sequence(
        self, tenant_id: TenantId, *, limit: int, now: datetime
    ) -> list[Enrollment]:
        """活跃 Campaign 中到期可推进的 Enrollment（按到期时间排序）。"""
        if not self._tenant_matches(tenant_id, "outreach_enrollment_due_list"):
            return []
        rows = (
            (
                await self._session.execute(
                    select(OutreachEnrollmentRow)
                    .join(
                        OutreachCampaignRow,
                        and_(
                            OutreachCampaignRow.tenant_id
                            == OutreachEnrollmentRow.tenant_id,
                            OutreachCampaignRow.campaign_id
                            == OutreachEnrollmentRow.campaign_id,
                        ),
                    )
                    .where(
                        OutreachEnrollmentRow.tenant_id == self._tenant_id,
                        OutreachCampaignRow.state == CampaignState.ACTIVE.value,
                        OutreachEnrollmentRow.state.in_(
                            [
                                EnrollmentState.ENROLLED.value,
                                EnrollmentState.IN_SEQUENCE.value,
                            ]
                        ),
                        OutreachEnrollmentRow.next_send_at.is_not(None),
                        OutreachEnrollmentRow.next_send_at <= now,
                    )
                    .order_by(
                        OutreachEnrollmentRow.next_send_at.asc(),
                        OutreachEnrollmentRow.enrollment_id.asc(),
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_row_to_enrollment(row) for row in rows]


class SuppressionRepositoryImpl(_OutreachRepository):
    async def append_if_absent(
        self, entry: SuppressionEntry
    ) -> SuppressionAppendResult:
        self._require_tenant(entry.tenant_id, "outreach_suppression_append")
        statement = (
            insert(OutreachSuppressionRow)
            .values(
                tenant_id=str(entry.tenant_id),
                suppression_id=str(entry.suppression_id),
                contact_point_id=entry.target.contact_point_id,
                account_id=entry.target.account_id,
                reason=entry.reason.value,
                occurred_at=entry.occurred_at,
                source_ref=entry.source_ref,
                idempotency_key=str(entry.idempotency_key),
                created_at=entry.created_at,
            )
            .on_conflict_do_nothing(constraint="uq_outreach_suppressions_tenant_key")
            .returning(OutreachSuppressionRow.suppression_id)
        )
        created = (await self._session.execute(statement)).scalar_one_or_none()
        if created is not None:
            return SuppressionAppendResult(AppendStatus.CREATED, entry)
        row = (
            await self._session.execute(
                select(OutreachSuppressionRow).where(
                    OutreachSuppressionRow.tenant_id == self._tenant_id,
                    OutreachSuppressionRow.idempotency_key == entry.idempotency_key,
                )
            )
        ).scalar_one()
        winner = _row_to_suppression(row)
        same_payload = (
            winner.tenant_id == entry.tenant_id
            and winner.target == entry.target
            and winner.reason is entry.reason
            and winner.occurred_at == entry.occurred_at
            and winner.source_ref == entry.source_ref
            and winner.idempotency_key == entry.idempotency_key
        )
        status = AppendStatus.EXISTING if same_payload else AppendStatus.CONFLICT
        return SuppressionAppendResult(
            status, winner if status is AppendStatus.EXISTING else None
        )

    async def find_current(
        self, tenant_id: TenantId, target: SuppressionTarget
    ) -> SuppressionEntry | None:
        if not self._tenant_matches(tenant_id, "outreach_suppression_get"):
            return None
        query = select(OutreachSuppressionRow).where(
            OutreachSuppressionRow.tenant_id == self._tenant_id
        )
        if target.contact_point_id is not None:
            query = query.where(
                OutreachSuppressionRow.contact_point_id == target.contact_point_id
            )
        else:
            query = query.where(OutreachSuppressionRow.account_id == target.account_id)
        row = (
            await self._session.execute(
                query.order_by(
                    OutreachSuppressionRow.occurred_at.desc(),
                    OutreachSuppressionRow.suppression_id.desc(),
                ).limit(1)
            )
        ).scalar_one_or_none()
        return _row_to_suppression(row) if row else None

    async def list_scoped(
        self, tenant_id: TenantId, scope: OutreachScope, limit: int
    ) -> list[SuppressionEntry]:
        if not self._tenant_matches(tenant_id, "outreach_suppression_list"):
            return []
        if scope.level not in {ScopeLevel.TENANT, ScopeLevel.MANAGER}:
            return []
        query = select(OutreachSuppressionRow).where(
            OutreachSuppressionRow.tenant_id == self._tenant_id
        )
        if scope.level is ScopeLevel.MANAGER:
            targets = scope.allowed_suppression_targets
            if not targets:
                return []
            contacts = [value for value in targets if value.startswith("cp_")]
            accounts = [value for value in targets if value.startswith("acc_")]
            query = query.where(
                or_(
                    OutreachSuppressionRow.contact_point_id.in_(contacts),
                    OutreachSuppressionRow.account_id.in_(accounts),
                )
            )
        rows = (
            (
                await self._session.execute(
                    query.order_by(
                        OutreachSuppressionRow.occurred_at.desc(),
                        OutreachSuppressionRow.suppression_id.desc(),
                    ).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_row_to_suppression(row) for row in rows]


class QuotaRepositoryImpl(_OutreachRepository):
    async def _reserve(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        on_day: date,
        limit: int,
        column: str,
    ) -> QuotaReservationResult:
        self._require_tenant(tenant_id, "outreach_quota_reserve")
        excluded = insert(OutreachDailyQuotaRow).excluded
        target = getattr(OutreachDailyQuotaRow, column)
        statement = (
            insert(OutreachDailyQuotaRow)
            .values(
                tenant_id=str(self._tenant_id),
                campaign_id=str(campaign_id),
                on_day=on_day,
                new_contacts_reserved=1 if column == "new_contacts_reserved" else 0,
                messages_reserved=1 if column == "messages_reserved" else 0,
            )
            .on_conflict_do_update(
                constraint="pk_outreach_daily_quotas",
                set_={column: target + getattr(excluded, column)},
                where=target < limit,
            )
            .returning(OutreachDailyQuotaRow)
        )
        row = (await self._session.execute(statement)).scalar_one_or_none()
        if row is None:
            return QuotaReservationResult(QuotaReservationStatus.CAP_REACHED, None)
        winner = DailyQuotaUsage(
            TenantId(row.tenant_id),
            CampaignId(row.campaign_id),
            row.on_day,
            row.new_contacts_reserved,
            row.messages_reserved,
        )
        return QuotaReservationResult(QuotaReservationStatus.RESERVED, winner)

    async def reserve_new_contact(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        on_day: date,
        limit: int,
    ) -> QuotaReservationResult:
        return await self._reserve(
            tenant_id, campaign_id, on_day, limit, "new_contacts_reserved"
        )

    async def reserve_message(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        on_day: date,
        limit: int,
    ) -> QuotaReservationResult:
        return await self._reserve(
            tenant_id, campaign_id, on_day, limit, "messages_reserved"
        )

    async def get_usage(
        self, tenant_id: TenantId, campaign_id: CampaignId, on_day: date
    ) -> DailyQuotaUsage:
        self._require_tenant(tenant_id, "outreach_quota_get")
        row = await self._session.get(
            OutreachDailyQuotaRow,
            (str(self._tenant_id), str(campaign_id), on_day),
        )
        if row is None:
            return DailyQuotaUsage(tenant_id, campaign_id, on_day, 0, 0)
        return DailyQuotaUsage(
            TenantId(row.tenant_id),
            CampaignId(row.campaign_id),
            row.on_day,
            row.new_contacts_reserved,
            row.messages_reserved,
        )


class MessageAttemptRepositoryImpl(_OutreachRepository):
    async def create_if_absent(
        self, attempt: MessageAttempt
    ) -> MessageAttemptCreateResult:
        self._require_tenant(attempt.tenant_id, "outreach_attempt_create")
        values = {
            column.name: getattr(_attempt_row(attempt), column.name)
            for column in OutreachMessageAttemptRow.__table__.columns
        }
        statement = (
            insert(OutreachMessageAttemptRow)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_outreach_attempts_tenant_key")
            .returning(OutreachMessageAttemptRow.attempt_id)
        )
        created = (await self._session.execute(statement)).scalar_one_or_none()
        if created is not None:
            return MessageAttemptCreateResult(AppendStatus.CREATED, attempt)
        row = (
            await self._session.execute(
                select(OutreachMessageAttemptRow).where(
                    OutreachMessageAttemptRow.tenant_id == self._tenant_id,
                    OutreachMessageAttemptRow.idempotency_key
                    == attempt.idempotency_key,
                )
            )
        ).scalar_one()
        winner = _row_to_attempt(row)
        status = AppendStatus.EXISTING if winner == attempt else AppendStatus.CONFLICT
        return MessageAttemptCreateResult(
            status, winner if status is AppendStatus.EXISTING else None
        )

    async def get_for_update(
        self, tenant_id: TenantId, attempt_id: MessageAttemptId
    ) -> MessageAttempt | None:
        if not self._tenant_matches(tenant_id, "outreach_attempt_lock"):
            return None
        row = (
            await self._session.execute(
                select(OutreachMessageAttemptRow)
                .where(
                    OutreachMessageAttemptRow.tenant_id == self._tenant_id,
                    OutreachMessageAttemptRow.attempt_id == attempt_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        return _row_to_attempt(row) if row else None

    async def get_by_key(
        self, tenant_id: TenantId, key: IdempotencyKey
    ) -> MessageAttempt | None:
        if not self._tenant_matches(tenant_id, "outreach_attempt_key"):
            return None
        row = (
            await self._session.execute(
                select(OutreachMessageAttemptRow).where(
                    OutreachMessageAttemptRow.tenant_id == self._tenant_id,
                    OutreachMessageAttemptRow.idempotency_key == key,
                )
            )
        ).scalar_one_or_none()
        return _row_to_attempt(row) if row else None

    async def update(self, attempt: MessageAttempt) -> None:
        self._require_tenant(attempt.tenant_id, "outreach_attempt_update")
        await self._session.execute(
            update(OutreachMessageAttemptRow)
            .where(
                OutreachMessageAttemptRow.tenant_id == self._tenant_id,
                OutreachMessageAttemptRow.attempt_id == attempt.attempt_id,
            )
            .values(
                state=attempt.state.value,
                provider_ref=attempt.provider_ref,
                failure_category=(
                    attempt.failure_category.value if attempt.failure_category else None
                ),
                updated_at=attempt.updated_at,
                send_claimed_at=attempt.send_claimed_at,
            )
        )

    async def bind_delivery_correlation(
        self, attempt: MessageAttempt
    ) -> DeliveryCorrelationBindResult:
        self._require_tenant(attempt.tenant_id, "outreach_attempt_delivery_bind")
        if (
            attempt.deterministic_message_id is None
            or attempt.idempotency_header is None
        ):
            return DeliveryCorrelationBindResult(
                DeliveryCorrelationBindStatus.CONFLICT, None
            )
        lock_keys = sorted(
            (
                f"{self._tenant_id}:message-id:{attempt.deterministic_message_id}",
                f"{self._tenant_id}:idempotency-header:{attempt.idempotency_header}",
            )
        )
        for lock_key in lock_keys:
            await self._session.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
            )
        message_winner = await self.find_by_deterministic_message_id(
            self._tenant_id, attempt.deterministic_message_id
        )
        header_winner = await self.find_by_idempotency_header(
            self._tenant_id, attempt.idempotency_header
        )
        winners = tuple(
            winner
            for winner in (message_winner, header_winner)
            if winner is not None
        )
        if any(winner.attempt_id != attempt.attempt_id for winner in winners):
            return DeliveryCorrelationBindResult(
                DeliveryCorrelationBindStatus.CONFLICT, None
            )
        row = (
            await self._session.execute(
                select(OutreachMessageAttemptRow)
                .where(
                    OutreachMessageAttemptRow.tenant_id == self._tenant_id,
                    OutreachMessageAttemptRow.attempt_id == attempt.attempt_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return DeliveryCorrelationBindResult(
                DeliveryCorrelationBindStatus.CONFLICT, None
            )
        current = (row.deterministic_message_id, row.idempotency_header)
        desired = (
            attempt.deterministic_message_id,
            attempt.idempotency_header,
        )
        if current == desired:
            return DeliveryCorrelationBindResult(
                DeliveryCorrelationBindStatus.EXISTING, _row_to_attempt(row)
            )
        if current != (None, None):
            return DeliveryCorrelationBindResult(
                DeliveryCorrelationBindStatus.CONFLICT, None
            )
        row.deterministic_message_id = attempt.deterministic_message_id
        row.idempotency_header = attempt.idempotency_header
        await self._session.flush()
        return DeliveryCorrelationBindResult(
            DeliveryCorrelationBindStatus.BOUND, _row_to_attempt(row)
        )

    async def find_by_deterministic_message_id(
        self, tenant_id: TenantId, deterministic_message_id: str
    ) -> MessageAttempt | None:
        if not self._tenant_matches(tenant_id, "outreach_attempt_message_id"):
            return None
        row = (
            await self._session.execute(
                select(OutreachMessageAttemptRow).where(
                    OutreachMessageAttemptRow.tenant_id == self._tenant_id,
                    OutreachMessageAttemptRow.deterministic_message_id
                    == deterministic_message_id,
                )
            )
        ).scalar_one_or_none()
        return _row_to_attempt(row) if row is not None else None

    async def find_by_idempotency_header(
        self, tenant_id: TenantId, idempotency_header: str
    ) -> MessageAttempt | None:
        if not self._tenant_matches(tenant_id, "outreach_attempt_idempotency_header"):
            return None
        row = (
            await self._session.execute(
                select(OutreachMessageAttemptRow).where(
                    OutreachMessageAttemptRow.tenant_id == self._tenant_id,
                    OutreachMessageAttemptRow.idempotency_header
                    == idempotency_header,
                )
            )
        ).scalar_one_or_none()
        return _row_to_attempt(row) if row is not None else None


class ActionRepositoryImpl(_OutreachRepository):
    async def append(self, action: ActionRecord) -> bool:
        self._require_tenant(action.tenant_id, "outreach_action_append")
        result = await self._session.execute(
            insert(OutreachActionRow)
            .values(
                tenant_id=str(action.tenant_id),
                action_id=action.action_id,
                action_key=action.action_key,
                action=action.action,
                entity_id=action.entity_id,
                actor_id=action.actor_id,
                occurred_at=action.occurred_at,
            )
            .on_conflict_do_nothing(constraint="uq_outreach_actions_tenant_key")
            .returning(OutreachActionRow.action_id)
        )
        return result.scalar_one_or_none() is not None
