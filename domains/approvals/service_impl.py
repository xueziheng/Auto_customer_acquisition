"""审批包生命周期、过期、自批禁止与幂等应用实现。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from domains.approvals.errors import (
    ApprovalExpiredError,
    ConflictingDecisionError,
    SelfApprovalError,
)
from domains.approvals.models import (
    DEFAULT_VALIDITY,
    ApprovalPackage,
    ApprovalState,
    ApprovalType,
    BlastRadius,
)
from domains.approvals.repository import ApprovalUnitOfWorkFactory
from domains.approvals.schemas import ApprovalView
from shared.errors import InvalidStateTransition, ValidationError
from shared.events.catalog import ApprovalDecided
from shared.schemas.identifiers import ApprovalId, EmployeeId, RunId, TenantId, new_id

_TYPE_LABELS: dict[ApprovalType, str] = {
    ApprovalType.QUOTE_SEND: "正式报价发送",
    ApprovalType.PRICE_COMMUNICATION: "价格沟通",
    ApprovalType.DISCOUNT: "折扣",
    ApprovalType.DELIVERY_COMMITMENT: "交货期承诺",
    ApprovalType.CERTIFICATION_COMMITMENT: "认证承诺",
    ApprovalType.PAYMENT_TERMS: "付款条件",
    ApprovalType.EXCLUSIVE_DISTRIBUTION: "独家代理",
    ApprovalType.OFF_CAMPAIGN_SEND: "Campaign 外发送",
    ApprovalType.SPEND_ABOVE_THRESHOLD: "超阈值支出",
    ApprovalType.SUPPRESSION_REMOVAL: "移除抑制",
    ApprovalType.CAMPAIGN_BOUNDARY_CHANGE: "Campaign 边界",
    ApprovalType.SENDING_IDENTITY_CHANGE: "发件身份配置",
    ApprovalType.INDICATIVE_RISK_ACCEPTANCE: "指示价风险接受",
    ApprovalType.MARGIN_FLOOR_OVERRIDE: "最低利润覆盖",
    ApprovalType.PLAYBOOK_CHANGE: "Company Playbook 变更",
}

_SAFE_APPLICATION_ERROR_CODES = frozenset(
    {"PLAYBOOK_BASE_VERSION_CONFLICT", "PLAYBOOK_APPROVAL_FACT_INVALID"}
)


def _utc(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValidationError("审批时间必须是 UTC")
    return value


def _text(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(f"{field} 无效")
    return value


def _optional_id(value: object, field: str, prefix: str) -> str | None:
    if value is None:
        return None
    text = _text(str(value), field, 64)
    if not text.startswith(f"{prefix}_"):
        raise ValidationError(f"{field} 无效")
    return text


def _json_object(value: object, field: str) -> dict:
    if not isinstance(value, dict) or not value:
        raise ValidationError(f"{field} 必须为非空对象")
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} 不是可持久化 JSON") from exc
    if len(rendered.encode("utf-8")) > 64_000:
        raise ValidationError(f"{field} 过大")
    lowered = rendered.casefold()
    if any(
        marker in lowered
        for marker in ('"password"', '"secret"', '"token"', '"cookie"')
    ):
        raise ValidationError(f"{field} 不得包含凭证形态字段")
    return value


def _strings(
    values: object, field: str, *, required: bool, maximum_items: int
) -> list[str]:
    if not isinstance(values, list) or len(values) > maximum_items:
        raise ValidationError(f"{field} 无效")
    result = [_text(item, field, 2_000) for item in values]
    if required and not result:
        raise ValidationError(f"{field} 不能为空")
    return result


class ApprovalServiceImpl:
    def __init__(
        self,
        uow_factory: ApprovalUnitOfWorkFactory,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(uow_factory, ApprovalUnitOfWorkFactory):
            raise ValidationError("审批事务依赖无效")
        self._uow_factory = uow_factory
        self._now = now or (lambda: datetime.now(UTC))

    @staticmethod
    def _tenant(tenant_id: TenantId) -> TenantId:
        _text(str(tenant_id), "tenant_id", 64)
        return tenant_id

    @staticmethod
    def _view(
        package: ApprovalPackage,
        *,
        now: datetime,
        current_employee: EmployeeId | None = None,
    ) -> ApprovalView:
        display = {
            str(key): (
                value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False, sort_keys=True)
            )
            for key, value in package.proposed_change.items()
        }
        can_decide = (
            current_employee is not None
            and package.state is ApprovalState.PENDING
            and not package.is_expired_at(now)
            and package.can_be_decided_by(current_employee)
        )
        seconds = max(0, int((package.expires_at - now).total_seconds()))
        return ApprovalView(
            approval_id=str(package.approval_id),
            approval_type=package.approval_type.value,
            type_label=_TYPE_LABELS[package.approval_type],
            title=package.title,
            reason=package.reason,
            proposed_change_display=display,
            affected_entities=list(package.blast_radius.affected_entities),
            if_approved=package.blast_radius.if_approved,
            if_rejected=package.blast_radius.if_rejected,
            reversible=package.blast_radius.reversible,
            state=package.state.value,
            created_at=package.created_at,
            expires_at=package.expires_at,
            proposed_by=(
                str(package.proposed_by_employee)
                if package.proposed_by_employee
                else None
            ),
            evidence_links=list(package.evidence_refs),
            owner_name=(
                str(package.owner_employee) if package.owner_employee else None
            ),
            decided_by_name=(str(package.decided_by) if package.decided_by else None),
            decided_at=package.decided_at,
            decision_note=package.decision_note,
            seconds_until_expiry=seconds,
            can_current_user_decide=can_decide,
            change_set_ref=package.change_set_ref,
            decided_by_employee=package.decided_by,
            applied_at=package.applied_at,
            application_error_code=(
                package.apply_error
                if package.apply_error in _SAFE_APPLICATION_ERROR_CODES
                else None
            ),
        )

    async def submit(
        self,
        tenant_id: TenantId,
        approval_type: ApprovalType,
        title: str,
        proposed_change: dict,
        reason: str,
        blast_radius: BlastRadius,
        *,
        proposed_by_run: RunId | None = None,
        proposed_by_employee: EmployeeId | None = None,
        evidence_refs: list[str] | None = None,
        change_set_ref: str | None = None,
        owner_employee: EmployeeId | None = None,
    ) -> ApprovalId:
        self._tenant(tenant_id)
        if not isinstance(approval_type, ApprovalType):
            raise ValidationError("approval_type 无效")
        _text(title, "审批标题", 500)
        _json_object(proposed_change, "审批变更")
        _text(reason, "审批理由", 4_000)
        if not isinstance(blast_radius, BlastRadius):
            raise ValidationError("审批影响范围无效")
        _strings(
            blast_radius.affected_entities,
            "受影响对象",
            required=True,
            maximum_items=200,
        )
        _text(blast_radius.if_approved, "批准后影响", 4_000)
        _text(blast_radius.if_rejected, "拒绝后影响", 4_000)
        if not isinstance(blast_radius.reversible, bool):
            raise ValidationError("审批可逆性无效")
        evidence = _strings(
            evidence_refs or [], "审批证据引用", required=False, maximum_items=100
        )
        if change_set_ref is not None:
            _text(change_set_ref, "变更集引用", 200)
        _optional_id(proposed_by_run, "提议 Run", "run")
        _optional_id(proposed_by_employee, "提议员工", "emp")
        _optional_id(owner_employee, "业务负责人", "emp")
        now = _utc(self._now())
        async with self._uow_factory(tenant_id) as uow:
            if change_set_ref is not None:
                existing = await uow.approvals.find_pending_by_change_set(
                    tenant_id, change_set_ref
                )
                if existing is not None:
                    return existing.approval_id
            approval_id = ApprovalId(new_id("apr"))
            package = ApprovalPackage(
                approval_id=approval_id,
                tenant_id=tenant_id,
                approval_type=approval_type,
                title=title,
                proposed_change=proposed_change,
                reason=reason,
                blast_radius=blast_radius,
                created_at=now,
                expires_at=now + DEFAULT_VALIDITY.get(approval_type, timedelta(days=3)),
                proposed_by_run=proposed_by_run,
                proposed_by_employee=proposed_by_employee,
                evidence_refs=evidence,
                change_set_ref=change_set_ref,
                owner_employee=owner_employee,
            )
            await uow.approvals.add(package)
        return approval_id

    async def decide(
        self,
        tenant_id: TenantId,
        approval_id: ApprovalId,
        approved: bool,
        decided_by: EmployeeId,
        note: str | None = None,
    ) -> None:
        self._tenant(tenant_id)
        _optional_id(approval_id, "approval_id", "apr")
        _optional_id(decided_by, "decided_by", "emp")
        if type(approved) is not bool:
            raise ValidationError("审批决定无效")
        if note is not None:
            _text(note, "审批备注", 4_000)
        if not approved and note is None:
            raise ValidationError("拒绝审批必须填写原因")
        now = _utc(self._now())
        target = ApprovalState.APPROVED if approved else ApprovalState.REJECTED
        async with self._uow_factory(tenant_id) as uow:
            package = await uow.approvals.get_for_update(tenant_id, approval_id)
            if package is None:
                raise ValidationError("审批不存在")
            if package.state is not ApprovalState.PENDING:
                if (
                    package.state is target
                    and package.decided_by == decided_by
                    and package.decision_note == note
                ):
                    return
                raise ConflictingDecisionError("审批已有不同结果")
            if package.is_expired_at(now):
                raise ApprovalExpiredError("审批已过期，必须重新提交")
            if not package.can_be_decided_by(decided_by):
                raise SelfApprovalError("提议人或业务负责人不能审批自己的变更")
            package.state = target
            package.decided_at = now
            package.decided_by = decided_by
            package.decision_note = note
            await uow.approvals.update(package)
            await uow.bus.publish(
                ApprovalDecided(
                    tenant_id=tenant_id,
                    occurred_at=now,
                    approval_id=str(approval_id),
                    decision="approve" if approved else "reject",
                    decided_by=decided_by,
                )
            )

    async def mark_applied(
        self, tenant_id: TenantId, approval_id: ApprovalId, idempotency_key: str
    ) -> bool:
        self._tenant(tenant_id)
        _text(idempotency_key, "应用幂等键", 200)
        now = _utc(self._now())
        async with self._uow_factory(tenant_id) as uow:
            package = await uow.approvals.get_for_update(tenant_id, approval_id)
            if package is None:
                raise ValidationError("审批不存在")
            if package.state is ApprovalState.APPLIED:
                return await uow.approvals.record_application(
                    tenant_id, approval_id, idempotency_key
                )
            if package.state is not ApprovalState.APPROVED:
                raise InvalidStateTransition("只有已批准审批可以应用")
            first = await uow.approvals.record_application(
                tenant_id, approval_id, idempotency_key
            )
            if not first:
                return False
            package.state = ApprovalState.APPLIED
            package.applied_at = now
            await uow.approvals.update(package)
            return True

    async def mark_apply_failed(
        self, tenant_id: TenantId, approval_id: ApprovalId, error: str
    ) -> None:
        self._tenant(tenant_id)
        _text(error, "审批应用错误", 2_000)
        async with self._uow_factory(tenant_id) as uow:
            package = await uow.approvals.get_for_update(tenant_id, approval_id)
            if package is None:
                raise ValidationError("审批不存在")
            if (
                package.state is ApprovalState.APPLY_FAILED
                and package.apply_error == error
            ):
                return
            if package.state is not ApprovalState.APPROVED:
                raise InvalidStateTransition("只有已批准审批可以标记应用失败")
            package.state = ApprovalState.APPLY_FAILED
            package.apply_error = error
            await uow.approvals.update(package)

    async def expire_overdue(self, tenant_id: TenantId) -> int:
        self._tenant(tenant_id)
        now = _utc(self._now())
        async with self._uow_factory(tenant_id) as uow:
            packages = await uow.approvals.list_expired_candidates(tenant_id, now, 200)
            for package in packages:
                package.state = ApprovalState.EXPIRED
                await uow.approvals.update(package)
            return len(packages)

    async def get(
        self,
        tenant_id: TenantId,
        approval_id: ApprovalId,
        *,
        current_employee: EmployeeId | None = None,
    ) -> ApprovalView:
        self._tenant(tenant_id)
        now = _utc(self._now())
        async with self._uow_factory(tenant_id) as uow:
            package = await uow.approvals.get(tenant_id, approval_id)
            if package is None:
                raise ValidationError("审批不存在")
            return self._view(package, now=now, current_employee=current_employee)

    async def get_by_change_set(
        self, tenant_id: TenantId, change_set_ref: str
    ) -> ApprovalView | None:
        self._tenant(tenant_id)
        _text(change_set_ref, "变更集引用", 200)
        now = _utc(self._now())
        async with self._uow_factory(tenant_id) as uow:
            package = await uow.approvals.find_by_change_set(tenant_id, change_set_ref)
            return None if package is None else self._view(package, now=now)

    async def list_pending_for(
        self, tenant_id: TenantId, employee_id: EmployeeId, limit: int = 50
    ) -> list[ApprovalView]:
        self._tenant(tenant_id)
        _optional_id(employee_id, "employee_id", "emp")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValidationError("limit 无效")
        now = _utc(self._now())
        async with self._uow_factory(tenant_id) as uow:
            packages = await uow.approvals.list_pending_for_employee(
                tenant_id, employee_id, limit
            )
            return [
                self._view(item, now=now, current_employee=employee_id)
                for item in packages
            ]


__all__ = ("ApprovalServiceImpl",)
