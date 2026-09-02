"""老板策略门禁后的 durable Sourcing Admission 启动与恢复驱动。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol
from uuid import uuid4

from domains.sourcing.permissions import SourcingActor, SourcingScope
from domains.sourcing.schemas import SourcingCaseReadView, SourcingNeedSnapshot
from domains.sourcing.service import (
    AdmissionBlockedReason,
    AdmissionState,
    SourcingAdmission,
    SourcingService,
)
from shared.errors import TradeOSError, ValidationError
from shared.schemas.identifiers import RunId, TenantId
from workflows.engine.runner import WorkflowEngine

from ._sourcing_context import _safe_context
from .directive_reader import SourcingAdmissionPolicyRead

logger = logging.getLogger(__name__)

_StopReason = Literal[
    "policy_not_configured",
    "automatic_admission_disabled",
    "policy_status_unknown",
    "admission_status_unknown",
    "batch_processed",
]
_AttemptOutcome = Literal["admitted", "waiting", "blocked", "pending_recovery"]


class SourcingAdmissionPolicyReader(Protocol):
    async def read(
        self, tenant_id: TenantId
    ) -> SourcingAdmissionPolicyRead | None: ...


@dataclass(frozen=True)
class SourcingAdmissionScanResult:
    """单轮固定结果；只含计数和可公开停止原因，不含业务 ID 或异常。"""

    stop_reason: _StopReason
    expired_released_count: int = 0
    claimed_count: int = 0
    admitted_count: int = 0
    returned_to_waiting_count: int = 0
    blocked_count: int = 0
    pending_recovery_count: int = 0


class SourcingAdmissionDriver:
    """按 Directive 精确取批，并用稳定业务键启动/绑定 canonical Run。"""

    def __init__(
        self,
        *,
        policy: SourcingAdmissionPolicyReader,
        sourcing: SourcingService,
        engine: WorkflowEngine,
        tenant_id: TenantId,
        sourcing_actor: SourcingActor,
        lease_duration: timedelta,
        now: Callable[[], datetime],
    ) -> None:
        required = (
            (policy, "read"),
            (sourcing, "release_expired_admission_claims"),
            (sourcing, "claim_admissions"),
            (sourcing, "get_admission_case_snapshot"),
            (sourcing, "complete_admission"),
            (sourcing, "release_admission_claim"),
            (sourcing, "block_admission"),
            (engine, "start"),
        )
        if any(not callable(getattr(value, name, None)) for value, name in required):
            raise ValidationError("寻源准入 driver 依赖无效")
        if (
            not isinstance(tenant_id, str)
            or not tenant_id
            or tenant_id != tenant_id.strip()
            or not isinstance(sourcing_actor, SourcingActor)
            or sourcing_actor.tenant_id != tenant_id
            or sourcing_actor.scope is not SourcingScope.SYSTEM
            or sourcing_actor.role != "system"
        ):
            raise ValidationError("寻源准入 driver tenant 或系统身份无效")
        if not isinstance(lease_duration, timedelta) or lease_duration <= timedelta(0):
            raise ValidationError("寻源准入 lease_duration 必须为正时长")
        if not callable(now):
            raise ValidationError("寻源准入时钟无效")
        self._policy = policy
        self._sourcing = sourcing
        self._engine = engine
        self._tenant_id = tenant_id
        self._sourcing_actor = sourcing_actor
        self._lease_duration = lease_duration
        self._now = now

    @property
    def tenant_id(self) -> TenantId:
        return self._tenant_id

    @property
    def sourcing_actor(self) -> SourcingActor:
        return self._sourcing_actor

    @property
    def lease_duration(self) -> timedelta:
        return self._lease_duration

    def _current_time(self) -> datetime:
        current = self._now()
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise ValidationError("寻源准入时钟必须返回 UTC 时间")
        offset = current.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValidationError("寻源准入时钟必须返回 UTC 时间")
        return current

    def _finish(self, result: SourcingAdmissionScanResult) -> SourcingAdmissionScanResult:
        logger.info(
            "寻源准入扫描完成",
            extra={
                "tenant_id": str(self._tenant_id),
                "stop_reason": result.stop_reason,
                "expired_released_count": result.expired_released_count,
                "claimed_count": result.claimed_count,
                "admitted_count": result.admitted_count,
                "returned_to_waiting_count": result.returned_to_waiting_count,
                "blocked_count": result.blocked_count,
                "pending_recovery_count": result.pending_recovery_count,
            },
        )
        return result

    async def scan_once(self) -> SourcingAdmissionScanResult:
        """读取当前策略后释放到期租约、精确 claim，并逐项独立启动。"""

        try:
            policy = await self._policy.read(self._tenant_id)
        except Exception:  # noqa: BLE001 - 策略不确定必须固定停止、零 claim/start
            return self._finish(
                SourcingAdmissionScanResult(stop_reason="policy_status_unknown")
            )
        if policy is None:
            return self._finish(
                SourcingAdmissionScanResult(stop_reason="policy_not_configured")
            )
        if not isinstance(policy, SourcingAdmissionPolicyRead):
            return self._finish(
                SourcingAdmissionScanResult(stop_reason="policy_status_unknown")
            )
        if not policy.enabled:
            return self._finish(
                SourcingAdmissionScanResult(
                    stop_reason="automatic_admission_disabled"
                )
            )

        now = self._current_time()
        released_count = 0
        try:
            released = await self._sourcing.release_expired_admission_claims(
                self._tenant_id,
                now=now,
                actor=self._sourcing_actor,
            )
            if not isinstance(released, list) or any(
                not isinstance(item, SourcingAdmission) for item in released
            ):
                raise ValidationError("寻源准入过期租约返回无效")
            released_count = len(released)
            claim_token = uuid4().hex
            claimed = await self._sourcing.claim_admissions(
                self._tenant_id,
                limit=policy.batch_limit,
                claim_token=claim_token,
                claim_expires_at=now + self._lease_duration,
                actor=self._sourcing_actor,
            )
            if (
                not isinstance(claimed, list)
                or len(claimed) > policy.batch_limit
                or any(not isinstance(item, SourcingAdmission) for item in claimed)
            ):
                raise ValidationError("寻源准入 claim 返回无效")
        except Exception:  # noqa: BLE001 - 存储结果不确定时不得继续 start
            return self._finish(
                SourcingAdmissionScanResult(
                    stop_reason="admission_status_unknown",
                    expired_released_count=released_count,
                )
            )

        admitted = waiting = blocked = pending = 0
        for admission in claimed:
            outcome = await self._admit_one(admission)
            if outcome == "admitted":
                admitted += 1
            elif outcome == "waiting":
                waiting += 1
            elif outcome == "blocked":
                blocked += 1
            else:
                pending += 1
        return self._finish(
            SourcingAdmissionScanResult(
                stop_reason="batch_processed",
                expired_released_count=released_count,
                claimed_count=len(claimed),
                admitted_count=admitted,
                returned_to_waiting_count=waiting,
                blocked_count=blocked,
                pending_recovery_count=pending,
            )
        )

    async def _admit_one(self, admission: SourcingAdmission) -> _AttemptOutcome:
        """处理一条已 claim admission；启动后的任何不确定性均等待租约恢复。"""

        if (
            not isinstance(admission, SourcingAdmission)
            or admission.tenant_id != self._tenant_id
            or admission.state is not AdmissionState.STARTING
            or admission.claim_token is None
        ):
            return "pending_recovery"
        claim_token = admission.claim_token
        try:
            case = await self._sourcing.get_admission_case_snapshot(
                self._tenant_id,
                admission.case_id,
                actor=self._sourcing_actor,
            )
        except Exception as error:  # noqa: BLE001 - 未启动前按已知语义处置
            return await self._handle_pre_start_error(admission, claim_token, error)
        if case is None:
            return await self._block(admission, claim_token)
        if (
            not isinstance(case, SourcingCaseReadView)
        ):
            return "pending_recovery"
        snapshot = case.need_snapshot
        if (
            case.case_id != admission.case_id
            or case.need_id != admission.need_id
            or type(case.workflow_version) is not int
            or case.workflow_version != 2
            or case.state != "opened"
            or not isinstance(snapshot, SourcingNeedSnapshot)
            or snapshot.need_id != admission.need_id
            or not isinstance(snapshot.snapshot_hash, str)
            or len(snapshot.snapshot_hash) != 64
            or any(
                character not in "0123456789abcdef"
                for character in snapshot.snapshot_hash
            )
        ):
            return await self._block(admission, claim_token)
        try:
            safe_context = _safe_context(snapshot, str(admission.case_id))
        except TradeOSError:
            return await self._block(admission, claim_token)

        try:
            run_id = await self._engine.start(
                self._tenant_id,
                "sourcing_case",
                str(admission.case_id),
                safe_context,
                f"sourcing-case:v2:{self._tenant_id}:{admission.need_id}",
            )
        except TradeOSError as error:
            if error.is_retryable:
                return await self._release(admission, claim_token)
            return await self._block(admission, claim_token)
        except Exception:  # noqa: BLE001 - start 结果未知，严禁即时重试或换键
            return "pending_recovery"
        if not isinstance(run_id, str) or not run_id or run_id != run_id.strip():
            return "pending_recovery"
        try:
            await self._sourcing.complete_admission(
                self._tenant_id,
                admission.admission_id,
                claim_token=claim_token,
                workflow_run_id=RunId(run_id),
                admitted_by=self._sourcing_actor.actor_id,
                admitted_at=self._current_time(),
                actor=self._sourcing_actor,
            )
        except Exception:  # noqa: BLE001 - Run 或 bind 可能已提交，等待同键恢复
            return "pending_recovery"
        return "admitted"

    async def _handle_pre_start_error(
        self,
        admission: SourcingAdmission,
        claim_token: str,
        error: Exception,
    ) -> _AttemptOutcome:
        if isinstance(error, TradeOSError):
            if error.is_retryable:
                return await self._release(admission, claim_token)
            return await self._block(admission, claim_token)
        return "pending_recovery"

    async def _release(
        self, admission: SourcingAdmission, claim_token: str
    ) -> _AttemptOutcome:
        try:
            await self._sourcing.release_admission_claim(
                self._tenant_id,
                admission.admission_id,
                claim_token=claim_token,
                released_at=self._current_time(),
                actor=self._sourcing_actor,
            )
        except Exception:  # noqa: BLE001 - cleanup 不确定仍保守等待 lease
            return "pending_recovery"
        return "waiting"

    async def _block(
        self, admission: SourcingAdmission, claim_token: str
    ) -> _AttemptOutcome:
        try:
            await self._sourcing.block_admission(
                self._tenant_id,
                admission.admission_id,
                reason=AdmissionBlockedReason.CASE_STATE_MISMATCH,
                blocked_at=self._current_time(),
                claim_token=claim_token,
                actor=self._sourcing_actor,
            )
        except Exception:  # noqa: BLE001 - cleanup 不确定仍保守等待 lease
            return "pending_recovery"
        return "blocked"


__all__ = (
    "SourcingAdmissionDriver",
    "SourcingAdmissionPolicyReader",
    "SourcingAdmissionScanResult",
)
