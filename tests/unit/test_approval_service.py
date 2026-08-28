"""Company Playbook 审批类型与安全公共视图。"""

from __future__ import annotations

from contextlib import asynccontextmanager
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
COUNTRY_POLICY_CHANGE_SET = "country_policy:cpp_01K00000000000000000000000:" + "b" * 64


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

    async def lock_quote_change_set(self, tenant_id, change_set_ref):
        """单线程纯服务测试；并发唯一由真实PG组覆盖。"""
        del tenant_id, change_set_ref

    async def find_quote_by_change_set(self, tenant_id, change_set_ref):
        return await self.find_by_change_set(tenant_id, change_set_ref)

    async def list_quote_pending_candidates(
        self, tenant_id, *, scan_started_at, after, limit
    ):
        values = sorted(
            (
                p
                for p in self._store.packages.values()
                if p.tenant_id == tenant_id
                and p.contract_namespace == "quote-approval-v1"
                and p.created_at <= scan_started_at
                and p.state.value == "pending"
                and (after is None or (p.expires_at, p.approval_id) > after)
            ),
            key=lambda p: (p.expires_at, p.approval_id),
        )
        return tuple(values[:limit])

    async def list_pending_for_employee(self, tenant_id, employee_id, limit, *, legacy_only=False):
        return [
            p
            for p in self._store.packages.values()
            if p.tenant_id == tenant_id
            and p.state.value == "pending"
            and (not legacy_only or p.contract_namespace is None)
            and employee_id not in {p.proposed_by_employee, p.owner_employee}
        ][:limit]


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


class QuoteAccessCase:
    """只替代跨域当前员工存储，完整载荷用真实解析器验证。"""

    def __init__(self):
        self.role = "finance"
        self.held = False
        self.allowed = True

    def subject(self, fact):
        from domains.approvals.quote_contract import quote_contract_subject
        from domains.quotations.service import parse_quote_approval_payload

        parse_quote_approval_payload(fact.proposed_change)
        return quote_contract_subject(
            tenant_id=fact.tenant_id,
            approval_id=fact.approval_id,
            approval_type=fact.approval_type,
            change_set_ref=fact.change_set_ref,
            proposed_change=fact.proposed_change,
            proposed_by_employee=fact.proposed_by_employee,
            owner_employee=fact.owner_employee,
        )

    @asynccontextmanager
    async def guard(self, subject, *, actor_id, action):
        from domains.approvals.schemas import ApprovalAccessResult
        from shared.errors import PermissionDenied

        del subject, actor_id, action
        if not self.allowed:
            raise PermissionDenied("拒绝当前授权")
        self.held = True
        try:
            yield ApprovalAccessResult(can_decide=False, current_role=self.role)
        finally:
            self.held = False


def quote_service_case():
    import inspect

    assert "quote_access" in inspect.signature(ApprovalServiceImpl).parameters, (
        "缺少报价guard依赖"
    )
    guard = QuoteAccessCase()
    factory = _Factory()
    from tests.unit.test_quotation_contracts import basis_case

    clock = [basis_case()[3]]
    svc = ApprovalServiceImpl(factory, quote_access=guard, now=lambda: clock[0])
    return svc, factory, guard, clock


async def submit_quote(svc, *, limit=None, title="正式报价审批"):
    import json

    from domains.quotations.service import quote_approval_payloads, quote_change_set_ref
    from tests.unit.test_quote_approval_contracts import RUN, quote_case

    payload = quote_approval_payloads(quote_case(), None)[0]
    approval_id = await svc.submit(
        payload.tenant_id,
        ApprovalType.QUOTE_SEND,
        title,
        json.loads(payload.model_dump_json()),
        "人工核实商业条款",
        BlastRadius([str(payload.quote_id)], "仅批准本版本", "关闭本轮", False),
        proposed_by_run=RUN,
        proposed_by_employee=payload.prepared_by,
        owner_employee=payload.submitted_owner_id,
        change_set_ref=quote_change_set_ref(
            payload.quote_id, payload.content_hash, "quote_send"
        ),
        expires_at_limit=limit or payload.customer.valid_until,
    )
    return payload, approval_id


@pytest.mark.parametrize("operation", ["get", "list"])
async def test_new_reader_role_change_between_readable_roles_is_rejected(operation):
    from domains.approvals.schemas import ApprovalReaderIdentity
    from shared.errors import PermissionDenied

    svc, _, guard, _ = quote_service_case()
    payload, approval_id = await submit_quote(svc)
    reader = ApprovalReaderIdentity(employee_id=payload.prepared_by, role="manager")
    guard.role = "finance"
    with pytest.raises(PermissionDenied):
        if operation == "get":
            await svc.get_for_reader(payload.tenant_id, approval_id, reader=reader)
        else:
            await svc.list_for_reader(payload.tenant_id, reader=reader)
    reader = ApprovalReaderIdentity(employee_id=payload.prepared_by, role="finance")
    if operation == "get":
        result = await svc.get_for_reader(payload.tenant_id, approval_id, reader=reader)
        assert not result.can_current_user_decide
    else:
        result = await svc.list_for_reader(payload.tenant_id, reader=reader)
        assert [item.approval_id for item in result] == [approval_id]


async def test_new_namespace_replay_uses_original_limit_after_clock_expiry():
    from domains.approvals.errors import QuoteContractError
    from domains.approvals.service import ApprovalState

    svc, factory, _, clock = quote_service_case()
    payload, approval_id = await submit_quote(svc)
    original = await svc.read_fact(payload.tenant_id, approval_id)
    factory.store.packages[approval_id].state = ApprovalState.APPROVED
    factory.store.packages[approval_id].decided_by = APPROVER
    factory.store.packages[approval_id].decided_at = clock[0]
    clock[0] += timedelta(days=20)
    assert (await submit_quote(svc))[1] == approval_id
    restored = await svc.read_fact(payload.tenant_id, approval_id)
    assert restored.request_hash == original.request_hash
    assert restored.expires_at_limit == payload.customer.valid_until
    with pytest.raises(QuoteContractError) as error:
        await submit_quote(svc, title="更改不可变请求")
    assert error.value.code == "quote_request_conflict"


async def test_new_namespace_does_not_trust_persisted_hash_column():
    from domains.approvals.errors import QuoteContractError

    svc, factory, _, _ = quote_service_case()
    payload, approval_id = await submit_quote(svc)
    factory.store.packages[approval_id].title = "tampered"
    with pytest.raises(QuoteContractError):
        await svc.read_fact(payload.tenant_id, approval_id)


async def test_new_namespace_without_access_dependency_fails_closed():
    from shared.errors import PermissionDenied

    svc, factory, _, clock = quote_service_case()
    payload, approval_id = await submit_quote(svc)
    missing = ApprovalServiceImpl(factory, now=lambda: clock[0])
    with pytest.raises(PermissionDenied):
        await missing.get(
            payload.tenant_id, approval_id, current_employee=payload.prepared_by
        )
    with pytest.raises(PermissionDenied):
        await missing.get(payload.tenant_id, approval_id)


async def test_new_decision_requires_current_guard_before_package_lock():
    from shared.errors import PermissionDenied

    svc, factory, guard, _ = quote_service_case()
    payload, approval_id = await submit_quote(svc)
    guard.allowed = False
    with pytest.raises(PermissionDenied):
        await svc.decide(payload.tenant_id, approval_id, True, APPROVER)
    assert factory.store.packages[approval_id].state.value == "pending"


async def test_new_decision_clock_is_sampled_after_access_wait():
    from domains.approvals.errors import ApprovalExpiredError
    from domains.approvals.schemas import ApprovalAccessResult

    svc, factory, guard, clock = quote_service_case()
    payload, approval_id = await submit_quote(svc)

    @asynccontextmanager
    async def delayed(subject, *, actor_id, action):
        del subject, actor_id, action
        clock[0] = payload.customer.valid_until
        yield ApprovalAccessResult(can_decide=True, current_role="boss")

    guard.guard = delayed
    with pytest.raises(ApprovalExpiredError):
        await svc.decide(payload.tenant_id, approval_id, True, APPROVER)
    assert factory.store.packages[approval_id].state.value == "pending"


async def test_legacy_wrappers_do_not_expand_visibility():
    from domains.approvals.schemas import ApprovalReaderIdentity
    from shared.errors import PermissionDenied

    svc = _service()
    approval_id = await _submit(svc)
    assert hasattr(svc, "get_for_reader"), "缺少新旧隔离读取端口"
    with pytest.raises(PermissionDenied):
        await svc.get_for_reader(
            TENANT,
            approval_id,
            reader=ApprovalReaderIdentity(employee_id=PROPOSER, role="finance"),
        )


async def test_router_own_quote_read_uses_new_wrapper_not_global_approver_gate():
    from types import SimpleNamespace

    from apps.api.routers.approvals import get_approval, list_pending_approvals

    svc, _, _, _ = quote_service_case()
    payload, approval_id = await submit_quote(svc)
    identity = SimpleNamespace(
        tenant_id=payload.tenant_id,
        employee=SimpleNamespace(employee_id=payload.prepared_by, role="finance"),
    )
    dependencies = SimpleNamespace(approvals=svc)
    view = await get_approval(str(approval_id), identity, dependencies)
    assert view.approval_id == approval_id
    assert not view.can_current_user_decide
    assert len(await list_pending_approvals(identity, dependencies, 50)) == 1


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


async def test_new_invisible_candidates_do_not_consume_legacy_list_limit():
    from domains.approvals.schemas import ApprovalReaderIdentity
    svc,_,guard,_ = quote_service_case()
    payload,_ = await submit_quote(svc)
    legacy = await svc.submit(payload.tenant_id,ApprovalType.PLAYBOOK_CHANGE,"旧审批",
        {"version":"old"},"旧申请",BlastRadius(["old"],"批准","不变",False),
        proposed_by_employee=payload.prepared_by,owner_employee=payload.prepared_by)
    guard.allowed = False
    result = await svc.list_for_reader(payload.tenant_id,
        reader=ApprovalReaderIdentity(employee_id=APPROVER,role="boss"),limit=1)
    assert [item.approval_id for item in result] == [legacy]


async def test_mixed_queue_scans_past_invisible_page_before_merging_legacy():
    from dataclasses import replace

    from domains.approvals.schemas import ApprovalAccessResult, ApprovalReaderIdentity
    from shared.errors import PermissionDenied
    from shared.schemas.identifiers import new_id

    svc,factory,guard,clock = quote_service_case()
    payload,visible_id = await submit_quote(svc)
    visible = factory.store.packages[visible_id]
    legacy = await svc.submit(payload.tenant_id,ApprovalType.PLAYBOOK_CHANGE,"旧审批",
        {"version":"old"},"旧申请",BlastRadius(["old"],"批准","不变",False),
        proposed_by_employee=payload.prepared_by,owner_employee=payload.prepared_by)
    # 完整原请求保持不变；201个不同实际ID模拟稳定游标的跨页候选。
    factory.store.packages[visible_id] = replace(visible,created_at=clock[0]+timedelta(hours=1),
        expires_at=visible.expires_at+timedelta(hours=1))
    for _ in range(200):
        invisible = replace(visible,approval_id=ApprovalId(new_id("apr")))
        factory.store.packages[invisible.approval_id] = invisible
    clock[0] += timedelta(hours=2)
    @asynccontextmanager
    async def selective_guard(subject,*,actor_id,action):
        del actor_id,action
        if subject.approval_id != visible_id:
            raise PermissionDenied("不在当前范围")
        yield ApprovalAccessResult(can_decide=True,current_role="boss")
    guard.guard = selective_guard
    result = await svc.list_for_reader(payload.tenant_id,
        reader=ApprovalReaderIdentity(employee_id=APPROVER,role="boss"),limit=1)
    assert factory.store.packages[visible_id].expires_at < factory.store.packages[legacy].expires_at
    assert [item.approval_id for item in result] == [visible_id]


async def test_quote_fact_lookup_is_strict_internal_and_revalidates_original_request():
    from domains.approvals.errors import QuoteContractError
    from shared.errors import PermissionDenied

    svc,factory,_,_ = quote_service_case()
    payload,approval_id = await submit_quote(svc)
    package = factory.store.packages[approval_id]
    fact = await svc.find_quote_fact(payload.tenant_id,package.change_set_ref)
    assert fact.approval_id == approval_id and fact.request_hash == package.request_hash
    assert await svc.find_quote_fact(payload.tenant_id,package.change_set_ref.replace("quote_send","discount")) is None
    for ref in ("legacy:whatever",package.change_set_ref.upper()," "+package.change_set_ref,"quote:invalid"):
        with pytest.raises(QuoteContractError):
            await svc.find_quote_fact(payload.tenant_id,ref)
    with pytest.raises(PermissionDenied):
        await ApprovalServiceImpl(factory).find_quote_fact(payload.tenant_id,package.change_set_ref)
    package.title = "篡改原请求"
    with pytest.raises(QuoteContractError):
        await svc.find_quote_fact(payload.tenant_id,package.change_set_ref)
