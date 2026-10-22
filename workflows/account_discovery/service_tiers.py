"""在联系人自动处理前校验服务期与持久预留，不改变发送和验证规则。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from domains.prospecting.service import (
    ProspectingService,
    ProspectingServicePlanStore,
    ServicePlanReservation,
    ServicePlanState,
    ServiceTierPolicy,
    contact_mission_key,
    tier_policy,
)
from shared.errors import TenantIsolationViolation, ValidationError
from workflows.account_discovery.ports import ContactEnricher, ContactVerifier
from workflows.account_discovery.steps import (
    FindContactsStep,
    VerifyContactsStep,
    _account_id,
    _base_context,
)
from workflows.engine.runner import WorkflowRun


class _ContactServiceGate:
    def __init__(
        self, service_plans: ProspectingServicePlanStore, now: Callable[[], datetime]
    ) -> None:
        self.store = service_plans
        self.now = now

    async def current(
        self, run: WorkflowRun
    ) -> tuple[ServicePlanState, ServiceTierPolicy] | None:
        """每次重读当前服务期和档位；人工证据不作为联系人处理前提。"""
        _base_context(run)
        _account_id(run)
        state = await self.store.get(run.tenant_id)
        if state is None:
            return None
        if state.tenant_id != run.tenant_id:
            raise TenantIsolationViolation("contact_service_plan_tenant_mismatch")
        now = self.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValidationError("contact_screening_clock_invalid")
        if not state.period_started_at <= now < state.period_ends_at:
            raise ValidationError("service_plan_period_inactive")
        return state, tier_policy(state.tier)

    async def receipt(self, run: WorkflowRun) -> ServicePlanReservation | None:
        legacy = await self.store.reservation(run.tenant_id, key=f"{run.run_id}:contacts", now=self.now())
        return legacy or await self.store.reservation(run.tenant_id, key=_contact_key(run), now=self.now())


def _contact_key(run: WorkflowRun) -> str:
    return contact_mission_key(str(_base_context(run)[1]), str(_account_id(run)))


def _screened_result(
    result: tuple[str, str | None, dict[str, Any]],
) -> tuple[str, str | None, dict[str, Any]]:
    action, next_step, patch = result
    return action, next_step, {
        **patch,
        "contact_screening_status": "ready_for_enrichment",
        "contact_screening_missing": [],
    }


def _receipt_limit(
    run: WorkflowRun,
    state: ServicePlanState,
    policy: ServiceTierPolicy,
    receipt: ServicePlanReservation | None,
) -> int:
    if receipt is None:
        raise ValidationError("contact_reservation_missing")
    if receipt.period_started_at != state.period_started_at:
        raise ValidationError("contact_reservation_period_mismatch")
    if (
        receipt.key not in {f"{run.run_id}:contacts", _contact_key(run)}
        or type(receipt.contacts) is not int
        or receipt.contacts < 1
        or receipt.candidates != 0
        or receipt.searches != 0
    ):
        raise ValidationError("contact_reservation_invalid")
    return min(policy.max_contacts_per_company, receipt.contacts)


class ServiceTierFindContactsStep:
    """自动预留本档联系人处理额度；历史联系人也使用同一额度上限。"""

    def __init__(
        self,
        prospecting: ProspectingService,
        enricher: ContactEnricher,
        service_plans: ProspectingServicePlanStore,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._prospecting = prospecting
        self._enricher = enricher
        self._gate = _ContactServiceGate(service_plans, now)

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        current = await self._gate.current(run)
        if current is None:
            return await FindContactsStep(
                self._prospecting,
                self._enricher,
                now=self._gate.now,
            ).execute(run)
        _state, policy = current
        receipt = await self._gate.receipt(run)
        if receipt is None:
            await self._gate.store.reserve(
                run.tenant_id,
                key=_contact_key(run),
                candidates=0,
                searches=0,
                contacts=policy.max_contacts_per_company,
                now=self._gate.now(),
            )
        # 预留期间可能切档或跨过服务期边界；成本前重读，旧预留不能被升档扩大。
        current = await self._gate.current(run)
        if current is None:
            raise ValidationError("contact_service_plan_missing")
        state, policy = current
        limit = _receipt_limit(run, state, policy, await self._gate.receipt(run))
        return _screened_result(
            await FindContactsStep(
                self._prospecting,
                self._enricher,
                now=self._gate.now,
                max_contacts=limit,
            ).execute(run)
        )


class ServiceTierVerifyContactsStep:
    """重查服务期并自动验证邮箱，只使用原预留，不产生第二次扣额。"""

    def __init__(
        self,
        prospecting: ProspectingService,
        verifier: ContactVerifier,
        service_plans: ProspectingServicePlanStore,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._prospecting = prospecting
        self._verifier = verifier
        self._gate = _ContactServiceGate(service_plans, now)

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        current = await self._gate.current(run)
        limit = None
        if current is not None:
            state, policy = current
            limit = _receipt_limit(run, state, policy, await self._gate.receipt(run))
        result = await VerifyContactsStep(
            self._prospecting,
            self._verifier,
            max_contacts=limit,
        ).execute(run)
        return _screened_result(result) if current is not None else result


__all__ = ("ServiceTierFindContactsStep", "ServiceTierVerifyContactsStep")
