"""Company Playbook 审批类型与安全公共视图。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self

import pytest

from domains.approvals.errors import SelfApprovalError
from domains.approvals.service import ApprovalType, BlastRadius
from domains.approvals.service_impl import ApprovalServiceImpl
from shared.schemas.identifiers import ApprovalId, EmployeeId, TenantId

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
TENANT = TenantId("tenant-playbook-approval")
PROPOSER = EmployeeId("emp_01K00000000000000000000000")
APPROVER = EmployeeId("emp_01K00000000000000000000001")
CHANGE_SET = "playbook:pbv_01K00000000000000000000000:" + "a" * 64
COUNTRY_POLICY_CHANGE_SET = (
    "country_policy:cpp_01K00000000000000000000000:" + "b" * 64
)


class _Bus:
    async def publish(self, event: object) -> None:
        del event


class _Store:
    def __init__(self) -> None:
        self.packages: dict[ApprovalId, object] = {}
        self.application_keys: dict[ApprovalId, str] = {}


class _Approvals:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def add(self, package: object) -> None:
        self._store.packages[package.approval_id] = package  # type: ignore[attr-defined]

    async def get(self, tenant_id: TenantId, approval_id: ApprovalId) -> object | None:
        del tenant_id
        return self._store.packages.get(approval_id)

    async def get_for_update(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> object | None:
        return await self.get(tenant_id, approval_id)

    async def update(self, package: object) -> None:
        self._store.packages[package.approval_id] = package  # type: ignore[attr-defined]

    async def find_pending_by_change_set(
        self, tenant_id: TenantId, change_set_ref: str
    ) -> object | None:
        del tenant_id
        return next(
            (
                package
                for package in self._store.packages.values()
                if package.change_set_ref == change_set_ref  # type: ignore[attr-defined]
                and package.state.value == "pending"  # type: ignore[attr-defined]
            ),
            None,
        )

    async def find_by_change_set(
        self, tenant_id: TenantId, change_set_ref: str
    ) -> object | None:
        del tenant_id
        return next(
            (
                package
                for package in self._store.packages.values()
                if package.change_set_ref == change_set_ref  # type: ignore[attr-defined]
            ),
            None,
        )

    async def record_application(
        self, tenant_id: TenantId, approval_id: ApprovalId, idempotency_key: str
    ) -> bool:
        del tenant_id
        if approval_id in self._store.application_keys:
            return False
        self._store.application_keys[approval_id] = idempotency_key
        return True


class _Uow:
    def __init__(self, store: _Store) -> None:
        self.approvals = _Approvals(store)
        self.bus = _Bus()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback


class _Factory:
    def __init__(self) -> None:
        self.store = _Store()

    def __call__(self, tenant_id: TenantId) -> _Uow:
        del tenant_id
        return _Uow(self.store)


def _service() -> ApprovalServiceImpl:
    return ApprovalServiceImpl(_Factory(), now=lambda: NOW)


async def _submit(
    service: ApprovalServiceImpl, change_set_ref: str = CHANGE_SET
) -> ApprovalId:
    return await service.submit(
        TENANT,
        ApprovalType.PLAYBOOK_CHANGE,
        "激活 Company Playbook v1",
        {"before": "未配置", "after": "候选版本"},
        "老板提交了新的公司经营边界。",
        BlastRadius(
            affected_entities=["Company Playbook pbv_01K00000000000000000000000"],
            if_approved="激活候选版本",
            if_rejected="保持当前版本",
            reversible=True,
        ),
        proposed_by_employee=PROPOSER,
        owner_employee=PROPOSER,
        change_set_ref=change_set_ref,
    )


async def _submit_country_policy(service: ApprovalServiceImpl) -> ApprovalId:
    return await service.submit(
        TENANT,
        ApprovalType.COUNTRY_POLICY_CHANGE,
        "激活 Synthetic Market 国家政策包 v1",
        {"before": "未配置", "after": "候选版本"},
        "老板提交了新的合成国家政策包。",
        BlastRadius(
            affected_entities=["国家政策包 cpp_01K00000000000000000000000"],
            if_approved="激活候选版本",
            if_rejected="保持默认拒绝",
            reversible=True,
        ),
        proposed_by_employee=PROPOSER,
        owner_employee=PROPOSER,
        change_set_ref=COUNTRY_POLICY_CHANGE_SET,
    )


@pytest.mark.asyncio
async def test_playbook_change_is_a_seven_day_mandatory_approval() -> None:
    service = _service()
    approval_id = await _submit(service)
    view = await service.get(TENANT, approval_id)

    assert ApprovalType.PLAYBOOK_CHANGE.value == "playbook_change"
    assert view.expires_at - view.created_at == timedelta(days=7)


@pytest.mark.asyncio
async def test_playbook_view_exposes_safe_correlation_and_application_facts() -> None:
    service = _service()
    approval_id = await _submit(service)
    await service.decide(TENANT, approval_id, True, APPROVER)
    await service.mark_applied(TENANT, approval_id, "playbook-application-1")
    view = await service.get(TENANT, approval_id)

    assert view.type_label == "Company Playbook 变更"
    assert view.change_set_ref == CHANGE_SET
    assert view.decided_by_employee == APPROVER
    assert view.applied_at == NOW
    assert view.application_error_code is None


@pytest.mark.asyncio
async def test_playbook_view_only_exposes_allowlisted_apply_error_codes() -> None:
    allowed_service = _service()
    allowed_id = await _submit(allowed_service)
    await allowed_service.decide(TENANT, allowed_id, True, APPROVER)
    await allowed_service.mark_apply_failed(
        TENANT, allowed_id, "PLAYBOOK_BASE_VERSION_CONFLICT"
    )

    hidden_service = _service()
    hidden_id = await _submit(hidden_service)
    await hidden_service.decide(TENANT, hidden_id, True, APPROVER)
    await hidden_service.mark_apply_failed(
        TENANT, hidden_id, "database password leaked in raw exception"
    )

    assert (
        await allowed_service.get(TENANT, allowed_id)
    ).application_error_code == "PLAYBOOK_BASE_VERSION_CONFLICT"
    assert (await hidden_service.get(TENANT, hidden_id)).application_error_code is None


@pytest.mark.asyncio
async def test_playbook_proposer_and_owner_cannot_self_approve() -> None:
    service = _service()
    approval_id = await _submit(service)

    with pytest.raises(SelfApprovalError):
        await service.decide(TENANT, approval_id, True, PROPOSER)
    await service.decide(TENANT, approval_id, True, APPROVER)
    assert (await service.get(TENANT, approval_id)).state == "approved"


@pytest.mark.asyncio
async def test_country_policy_change_has_exact_label_and_seven_day_validity() -> None:
    service = _service()
    approval_id = await _submit_country_policy(service)
    view = await service.get(TENANT, approval_id)

    assert view.type_label == "国家政策包变更"
    assert view.expires_at - view.created_at == timedelta(days=7)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "COUNTRY_POLICY_BASE_VERSION_CONFLICT",
        "COUNTRY_POLICY_APPROVAL_FACT_INVALID",
    ],
)
async def test_country_policy_public_apply_failure_codes_are_allowlisted(
    code: str,
) -> None:
    service = _service()
    approval_id = await _submit_country_policy(service)
    await service.decide(TENANT, approval_id, True, APPROVER)
    await service.mark_apply_failed(TENANT, approval_id, code)

    assert (await service.get(TENANT, approval_id)).application_error_code == code
