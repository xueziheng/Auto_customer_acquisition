"""承诺账本的幂等记录、人工确认、履约和逾期升级实现。"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime

from domains.commitments.errors import RelativeDueTimeError
from domains.commitments.models import (
    Commitment,
    CommitmentStatus,
    CommitmentType,
)
from domains.commitments.repository import CommitmentUnitOfWorkFactory
from shared.errors import InvalidStateTransition, ValidationError
from shared.events.catalog import CommitmentCreated, CommitmentOverdue, DomainEvent
from shared.schemas.identifiers import CommitmentId, EmployeeId, TenantId

_SCAN_LIMIT = 500
_COMMITMENT_ID = re.compile(r"com_[0-7][0-9A-HJKMNP-TV-Z]{25}")


def _text(value: object, field: str, maximum: int = 4_000) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(f"{field} 无效")
    return value


def _aware(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field}必须含时区")
    return value


def _parse_due_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise RelativeDueTimeError("承诺到期时间必须是带时区的绝对时间") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RelativeDueTimeError("承诺到期时间必须是带时区的绝对时间")
    return parsed


class CommitmentServiceImpl:
    """只通过 tenant-bound UoW 修改承诺并把事件写入同一 outbox 事务。"""

    def __init__(
        self,
        uow_factory: CommitmentUnitOfWorkFactory,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(uow_factory, CommitmentUnitOfWorkFactory):
            raise ValidationError("承诺事务依赖无效")
        self._uow_factory = uow_factory
        self._now = now or (lambda: datetime.now(UTC))

    @staticmethod
    def _tenant(tenant_id: TenantId) -> TenantId:
        _text(str(tenant_id), "tenant_id", 64)
        return tenant_id

    def _clock(self) -> datetime:
        return _aware(self._now(), "承诺服务时间")

    @staticmethod
    def _validate_new(tenant_id: TenantId, commitment: Commitment) -> None:
        if not isinstance(commitment, Commitment):
            raise ValidationError("承诺提取结果无效")
        if commitment.tenant_id != tenant_id:
            raise ValidationError("承诺租户不一致")
        commitment_id = _text(str(commitment.commitment_id), "承诺标识", 64)
        if _COMMITMENT_ID.fullmatch(commitment_id) is None:
            raise ValidationError("承诺标识无效")
        _text(str(commitment.owner), "承诺负责人", 64)
        _text(str(commitment.source_message_id), "承诺来源消息", 64)
        _text(commitment.action, "承诺事项")
        _text(commitment.verbatim, "承诺原话", 8_000)
        _aware(commitment.due_at, "承诺到期时间")
        _aware(commitment.created_at, "承诺创建时间")
        if not isinstance(commitment.commitment_type, CommitmentType):
            raise ValidationError("承诺类型无效")
        expected_status = (
            CommitmentStatus.WAITING_CUSTOMER
            if commitment.commitment_type is CommitmentType.CUSTOMER
            else CommitmentStatus.PENDING
        )
        if commitment.status is not expected_status:
            raise ValidationError("承诺初始状态与类型不一致")
        if not isinstance(commitment.due_at_uncertain, bool):
            raise ValidationError("承诺到期时间不确定标记无效")
        if commitment.extracted_by is not None:
            _text(commitment.extracted_by, "承诺提取者", 128)
        if (
            commitment.confirmed_by is not None
            or commitment.fulfilled_at is not None
            or commitment.escalated_at is not None
        ):
            raise ValidationError("新提取承诺必须处于未确认态")

    async def record_extracted(
        self, tenant_id: TenantId, commitment: Commitment
    ) -> CommitmentId:
        """原子去重保存未确认提取，并仅为 winner 发布创建事件。"""
        self._tenant(tenant_id)
        self._validate_new(tenant_id, commitment)
        async with self._uow_factory(tenant_id) as uow:
            winner, created = await uow.commitments.add_if_absent(commitment)
            if not created:
                return winner.commitment_id
            await uow.bus.publish(
                CommitmentCreated(
                    tenant_id=tenant_id,
                    occurred_at=commitment.created_at,
                    commitment_id=str(commitment.commitment_id),
                    commitment_type=commitment.commitment_type.value,
                    due_at=commitment.due_at,
                )
            )
        return commitment.commitment_id

    async def confirm(
        self,
        tenant_id: TenantId,
        commitment_id: CommitmentId,
        confirmed_by: EmployeeId,
        corrected_due_at: str | None = None,
    ) -> None:
        """确认提取；首个确认人与时间事实不可被后续调用覆盖。"""
        self._tenant(tenant_id)
        _text(str(commitment_id), "承诺标识", 64)
        _text(str(confirmed_by), "承诺确认人", 64)
        corrected = (
            _parse_due_at(corrected_due_at)
            if corrected_due_at is not None
            else None
        )
        async with self._uow_factory(tenant_id) as uow:
            commitment = await uow.commitments.get_for_update(
                tenant_id, commitment_id
            )
            if commitment is None:
                raise ValidationError("承诺不存在")
            if commitment.status in {
                CommitmentStatus.FULFILLED,
                CommitmentStatus.CANCELLED,
            }:
                raise InvalidStateTransition("终态承诺不可确认")
            if commitment.confirmed_by is not None:
                if commitment.confirmed_by == confirmed_by and corrected is None:
                    return
                raise InvalidStateTransition("承诺已确认，不可覆盖确认记录")
            commitment.confirmed_by = confirmed_by
            if corrected is not None:
                commitment.due_at = corrected
                commitment.due_at_uncertain = False
            await uow.commitments.update(commitment)

    async def fulfill(
        self, tenant_id: TenantId, commitment_id: CommitmentId
    ) -> None:
        """把已确认的开放承诺标记完成；重复完成为幂等 no-op。"""
        self._tenant(tenant_id)
        _text(str(commitment_id), "承诺标识", 64)
        now = self._clock()
        async with self._uow_factory(tenant_id) as uow:
            commitment = await uow.commitments.get_for_update(
                tenant_id, commitment_id
            )
            if commitment is None:
                raise ValidationError("承诺不存在")
            if commitment.status is CommitmentStatus.FULFILLED:
                return
            if commitment.status is CommitmentStatus.CANCELLED:
                raise InvalidStateTransition("已取消承诺不可完成")
            if not commitment.is_confirmed:
                raise InvalidStateTransition("未确认承诺不可完成")
            commitment.status = CommitmentStatus.FULFILLED
            commitment.fulfilled_at = now
            await uow.commitments.update(commitment)

    async def scan_overdue(self, tenant_id: TenantId) -> int:
        """首次逾期通知负责人；员工承诺下一轮仍逾期时只升级一次。"""
        self._tenant(tenant_id)
        now = self._clock()
        new_overdue = 0
        async with self._uow_factory(tenant_id) as uow:
            commitments = await uow.commitments.list_due(
                tenant_id, now, _SCAN_LIMIT
            )
            events: list[DomainEvent] = []
            for commitment in commitments:
                publish = False
                if commitment.status is not CommitmentStatus.OVERDUE:
                    commitment.status = CommitmentStatus.OVERDUE
                    new_overdue += 1
                    publish = True
                elif (
                    commitment.commitment_type is CommitmentType.EMPLOYEE
                    and commitment.escalated_at is None
                ):
                    commitment.escalated_at = now
                    publish = True
                if not publish:
                    continue
                await uow.commitments.update(commitment)
                events.append(
                    CommitmentOverdue(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        commitment_id=str(commitment.commitment_id),
                        overdue_seconds=max(
                            0, int((now - commitment.due_at).total_seconds())
                        ),
                    )
                )
            if events:
                await uow.bus.publish_many(events)
        return new_overdue

    async def list_for_employee(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        include_fulfilled: bool,
    ) -> list[Commitment]:
        """按负责人列出承诺，默认只返回仍需处理的状态。"""
        self._tenant(tenant_id)
        _text(str(employee_id), "员工标识", 64)
        if not isinstance(include_fulfilled, bool):
            raise ValidationError("承诺历史开关无效")
        statuses = (
            list(CommitmentStatus)
            if include_fulfilled
            else [
                CommitmentStatus.PENDING,
                CommitmentStatus.WAITING_CUSTOMER,
                CommitmentStatus.OVERDUE,
            ]
        )
        async with self._uow_factory(tenant_id) as uow:
            return await uow.commitments.list_for_employee(
                tenant_id, employee_id, statuses
            )


__all__ = ("CommitmentServiceImpl",)
