"""Company Playbook 审批类型与安全公共视图。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
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
CATALOG_EVIDENCE_REF = (
    "catalog-evidence-v1:conversation:msg_01K00000000000000000000000:"
    + "c" * 64
)


class _Bus:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def publish(self, event: object) -> None:
        self._store.events.append(event)


class _Store:
    def __init__(self) -> None:
        self.packages: dict[ApprovalId, object] = {}
        self.application_keys: dict[ApprovalId, str] = {}
        self.events: list[object] = []


class _Approvals:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def add(self, package: object) -> None:
        self._store.packages[package.approval_id] = package  # type: ignore[attr-defined]

    async def get(self, tenant_id: TenantId, approval_id: ApprovalId) -> object | None:
        package = self._store.packages.get(approval_id)
        return (
            package
            if package is not None and package.tenant_id == tenant_id  # type: ignore[attr-defined]
            else None
        )

    async def get_for_update(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> object | None:
        return await self.get(tenant_id, approval_id)

    async def update(self, package: object) -> None:
        self._store.packages[package.approval_id] = package  # type: ignore[attr-defined]

    async def find_pending_by_change_set(
        self, tenant_id: TenantId, change_set_ref: str
    ) -> object | None:
        return next(
            (
                package
                for package in self._store.packages.values()
                if package.tenant_id == tenant_id  # type: ignore[attr-defined]
                and package.change_set_ref == change_set_ref  # type: ignore[attr-defined]
                and package.state.value == "pending"  # type: ignore[attr-defined]
            ),
            None,
        )

    async def find_by_change_set(
        self, tenant_id: TenantId, change_set_ref: str
    ) -> object | None:
        return next(
            (
                package
                for package in self._store.packages.values()
                if package.tenant_id == tenant_id  # type: ignore[attr-defined]
                and package.change_set_ref == change_set_ref  # type: ignore[attr-defined]
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

    async def lock_catalog_change_set(self, tenant_id, change_set_ref):
        del tenant_id, change_set_ref

    async def find_catalog_by_change_set(self, tenant_id, change_set_ref):
        package = await self.find_by_change_set(tenant_id, change_set_ref)
        return (
            package
            if package is not None
            and package.contract_namespace
            in {"catalog-policy-v1", "catalog-cultivation-v1"}
            else None
        )

    async def list_catalog_pending_candidates(
        self, tenant_id, *, scan_started_at, after, limit
    ):
        values = sorted(
            (
                p
                for p in self._store.packages.values()
                if p.tenant_id == tenant_id
                and p.contract_namespace
                in {"catalog-policy-v1", "catalog-cultivation-v1"}
                and p.created_at <= scan_started_at
                and p.state.value == "pending"
                and (after is None or (p.expires_at, p.approval_id) > after)
            ),
            key=lambda p: (p.expires_at, p.approval_id),
        )
        return tuple(values[:limit])

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
        self.bus = _Bus(store)

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

    def display(self, fact):
        from domains.quotations.service import (
            parse_quote_approval_payload,
            project_quote_approval_display,
        )

        return project_quote_approval_display(parse_quote_approval_payload(fact.proposed_change))

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


@pytest.mark.parametrize("prepared,owner", [("boss-runtime", "sales-owner"), (PROPOSER, "sales-owner"), ("boss-runtime", APPROVER)])
async def test_quote_namespace_submission_preserves_persisted_short_employee_ids(prepared, owner):
    from domains.quotations.service import quote_approval_payloads, quote_change_set_ref
    from tests.unit.test_quote_approval_contracts import RUN, quote_case

    svc, _, _, _ = quote_service_case()
    payload = quote_approval_payloads(quote_case(), None)[0].model_dump(mode="json")
    payload.update(prepared_by=prepared, submitted_owner_id=owner)
    kwargs = {"proposed_by_run": RUN, "proposed_by_employee": prepared, "owner_employee": owner,
        "change_set_ref": quote_change_set_ref(payload["quote_id"], payload["content_hash"], "quote_send"),
        "expires_at_limit": quote_case().content.valid_until}
    args = (payload["tenant_id"], ApprovalType.QUOTE_SEND, "正式报价审批", payload,
        "人工核实商业条款", BlastRadius([payload["quote_id"]], "仅批准本版本", "关闭本轮", False))
    approval_id = await svc.submit(*args, **kwargs)
    assert await svc.submit(*args, **kwargs) == approval_id
    fact = await svc.read_fact(payload["tenant_id"], approval_id)
    assert fact.proposed_by_employee == prepared and fact.owner_employee == owner


async def test_quote_namespace_decision_accepts_short_id_under_original_guard():
    from domains.approvals.schemas import ApprovalAccessResult

    svc, _, guard, _ = quote_service_case()
    payload, approval_id = await submit_quote(svc)
    actors = []

    @asynccontextmanager
    async def allowed(subject, *, actor_id, action):
        actors.append((actor_id, action))
        yield ApprovalAccessResult(can_decide=True, current_role="boss")

    guard.guard = allowed
    await svc.decide(payload.tenant_id, approval_id, True, "independent-boss")
    assert actors == [("independent-boss", "decide")]
    assert (await svc.read_fact(payload.tenant_id, approval_id)).decided_by_employee == "independent-boss"


@pytest.mark.parametrize("employee", [None, True, "", " leading", "bad\nidentity", "a" * 41])
async def test_quote_namespace_rejects_invalid_decider_before_guard_and_writes(employee):
    from shared.errors import ValidationError

    svc, factory, guard, _ = quote_service_case()
    payload, approval_id = await submit_quote(svc)

    @asynccontextmanager
    async def forbidden(*args, **kwargs):
        pytest.fail("非法员工不得进入当前授权lease")
        yield

    guard.guard = forbidden
    with pytest.raises(ValidationError, match="审批决定员工无效"):
        await svc.decide(payload.tenant_id, approval_id, True, employee)
    assert factory.store.packages[approval_id].state.value == "pending"


@pytest.mark.parametrize("field", ["prepared_by", "submitted_owner_id"])
@pytest.mark.parametrize("employee", [None, True, "", " trailing ", "bad\nidentity", "a" * 41])
async def test_quote_namespace_rejects_invalid_proposer_or_owner_before_write(field, employee):
    from domains.approvals.errors import QuoteContractError
    from domains.quotations.service import quote_approval_payloads, quote_change_set_ref
    from tests.unit.test_quote_approval_contracts import quote_case

    svc, factory, _, _ = quote_service_case()
    payload = quote_approval_payloads(quote_case(), None)[0].model_dump(mode="json")
    payload[field] = employee
    with pytest.raises(QuoteContractError):
        await svc.submit(payload["tenant_id"], ApprovalType.QUOTE_SEND, "报价", payload, "人工核对",
            BlastRadius([payload["quote_id"]], "批准", "拒绝", False),
            proposed_by_employee=payload["prepared_by"], owner_employee=payload["submitted_owner_id"],
            change_set_ref=quote_change_set_ref(payload["quote_id"], payload["content_hash"], "quote_send"),
            expires_at_limit=quote_case().content.valid_until)
    assert not factory.store.packages


async def test_legacy_submission_and_decision_keep_original_short_id_rejection():
    from shared.errors import ValidationError

    svc = _service()
    with pytest.raises(ValidationError):
        await svc.submit(TENANT, ApprovalType.PLAYBOOK_CHANGE, "旧审批", {"change": "old"}, "人工核对",
            BlastRadius(["old"], "批准", "拒绝", False), proposed_by_employee="boss-runtime")
    approval_id = await _submit(svc)
    with pytest.raises(ValidationError):
        await svc.decide(TENANT, approval_id, True, "independent-boss")


async def test_short_quote_decider_does_not_bypass_current_guard():
    from shared.errors import PermissionDenied

    svc, factory, guard, _ = quote_service_case()
    payload, approval_id = await submit_quote(svc)
    guard.allowed = False
    with pytest.raises(PermissionDenied):
        await svc.decide(payload.tenant_id, approval_id, True, "independent-boss")
    assert factory.store.packages[approval_id].state.value == "pending"


@pytest.mark.parametrize("operation", ["get", "get_for_reader", "list_pending_for", "list_for_reader"])
async def test_quote_display_runs_only_inside_existing_read_lease(operation):
    from domains.approvals.schemas import ApprovalReaderIdentity

    svc, _, guard, _ = quote_service_case()
    payload, approval_id = await submit_quote(svc)
    seen = []

    def display(fact):
        assert guard.held, "展示必须在现有read租约内"
        seen.append(fact.approval_id)
        return {"受控中文标签": "纯文本"}

    guard.display = display
    reader = ApprovalReaderIdentity(employee_id=APPROVER, role=guard.role)
    if operation == "get":
        view = await svc.get(payload.tenant_id, approval_id, current_employee=APPROVER)
    elif operation == "get_for_reader":
        view = await svc.get_for_reader(payload.tenant_id, approval_id, reader=reader)
    elif operation == "list_pending_for":
        view = (await svc.list_pending_for(payload.tenant_id, APPROVER))[0]
    else:
        view = (await svc.list_for_reader(payload.tenant_id, reader=reader))[0]
    assert view.proposed_change_display == {"受控中文标签": "纯文本"}
    assert seen == [approval_id] and not guard.held


@pytest.mark.parametrize("failure", ["guard", "role", "corrupt", "half_namespace", "missing_guard"])
@pytest.mark.parametrize("operation", ["get", "list"])
async def test_quote_display_never_runs_for_denied_or_corrupt_read(failure, operation):
    from domains.approvals.schemas import ApprovalReaderIdentity
    from shared.errors import TradeOSError

    svc, factory, guard, clock = quote_service_case()
    payload, approval_id = await submit_quote(svc)
    calls = []
    guard.display = lambda fact: calls.append(fact) or {"禁止": "不能到达"}
    role = guard.role
    if failure == "guard":
        guard.allowed = False
    elif failure == "role":
        role = "product"
    elif failure == "corrupt":
        factory.store.packages[approval_id].title = "changed"
    elif failure == "half_namespace":
        factory.store.packages[approval_id].proposed_change["schema_version"] = "quote-approval-incomplete"
    else:
        svc = ApprovalServiceImpl(factory, now=lambda: clock[0])
    reader = ApprovalReaderIdentity(employee_id=APPROVER, role=role)
    if failure == "guard" and operation == "list":
        assert await svc.list_for_reader(payload.tenant_id, reader=reader) == []
    else:
        with pytest.raises(TradeOSError):
            if operation == "get":
                await svc.get_for_reader(payload.tenant_id, approval_id, reader=reader)
            else:
                await svc.list_for_reader(payload.tenant_id, reader=reader)
    assert calls == []


async def test_quote_display_missing_dependency_fails_closed_without_json_fallback(monkeypatch):
    monkeypatch.delattr(QuoteAccessCase, "display")
    svc, _, _, _ = quote_service_case()
    payload, approval_id = await submit_quote(svc)
    with pytest.raises(AttributeError):
        await svc.get(payload.tenant_id, approval_id, current_employee=APPROVER)


@pytest.mark.parametrize("kind", [ApprovalType.PLAYBOOK_CHANGE, ApprovalType.COUNTRY_POLICY_CHANGE])
async def test_legacy_display_remains_exact_original_nested_json_strings(kind):
    import json

    from workflows.country_policy_change.steps import _approval_display

    svc = _service()
    change = {"文本": "原值", "before": {"list": ["one", "two"], "value": None}, "after": [1, False]}
    approval_id = await svc.submit(TENANT, kind, "旧审批", change, "人工确认",
        BlastRadius(["old"], "批准", "拒绝", False), proposed_by_employee=PROPOSER,
        owner_employee=PROPOSER, change_set_ref="legacy:display")
    view = await svc.get(TENANT, approval_id)
    expected = {key: value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        for key, value in change.items()}
    assert view.proposed_change_display == expected == _approval_display(change)


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
    ("approval_value", "expected_validity", "expected_label"),
    [
        (
            "catalog_proposal_policy_change",
            timedelta(days=7),
            "目录产品提案策略变更",
        ),
        (
            "catalog_product_cultivation",
            timedelta(days=3),
            "目录产品培养审批",
        ),
    ],
)
async def test_catalog_approval_types_have_exact_validity_label_and_self_approval_guard(
    approval_value: str,
    expected_validity: timedelta,
    expected_label: str,
) -> None:
    actors = _CatalogActorReader()
    service = ApprovalServiceImpl(
        _Factory(), catalog_actor_reader=actors, now=lambda: NOW
    )
    command = (
        _catalog_policy_command()
        if approval_value == "catalog_proposal_policy_change"
        else _catalog_cultivation_command()
    )
    approval_id = await service.submit_catalog(command)

    view = await service.get(TENANT, approval_id)
    assert view.expires_at - view.created_at == expected_validity
    assert view.type_label == expected_label
    actors.employee_id = PROPOSER
    with pytest.raises(SelfApprovalError):
        await service.decide(TENANT, approval_id, True, PROPOSER)
    assert (await service.get(TENANT, approval_id)).state == "pending"


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


def _catalog_policy_command(*, expires_at_limit=NOW + timedelta(days=7)):
    from domains.approvals.catalog_contract import (
        CatalogPolicyApprovalCommand,
        CatalogPolicyContentFact,
        catalog_policy_content_hash,
        catalog_policy_request_hash,
    )
    from shared.schemas.identifiers import CatalogProposalPolicyVersionId

    content = CatalogPolicyContentFact(
        minimum_distinct_accounts=3,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=None,
        minimum_quantity_unit_accounts=None,
        require_unified_unit=False,
    )
    content_hash = catalog_policy_content_hash(content)
    policy_id = CatalogProposalPolicyVersionId(
        "cpv_01K00000000000000000000000"
    )
    return CatalogPolicyApprovalCommand(
        tenant_id=TENANT,
        policy_version_id=policy_id,
        content=content,
        content_hash=content_hash,
        base_active_version=None,
        proposed_by_employee=PROPOSER,
        owner_employee=PROPOSER,
        change_set_ref=f"catalog-policy:{policy_id}:{content_hash}",
        request_hash=catalog_policy_request_hash(content, PROPOSER, None),
        expires_at_limit=expires_at_limit,
    )


def _catalog_base_policy():
    from domains.approvals.catalog_contract import (
        CatalogPolicyVersionFact,
        catalog_policy_content_hash,
    )

    content = _catalog_policy_command().content
    return CatalogPolicyVersionFact(
        policy_version_id="cpv_01K00000000000000000000009",
        content=content,
        content_hash=catalog_policy_content_hash(content),
    )


def _catalog_rules():
    from domains.approvals.catalog_contract import CatalogRuleResultFact

    values = (
        ("membership_integrity", "passed", True, True, "成员关系与品类完整一致"),
        ("distinct_accounts", "passed", 3, 3, "去重客户数达到策略门槛"),
        ("recurring_accounts", "not_required", 1, None, "策略不要求复购客户数"),
        ("distinct_countries", "not_required", 2, None, "策略不要求已知国家数"),
        ("quantity_unit_coverage", "not_required", 3, None, "策略不要求数量单位覆盖"),
        ("unified_unit", "not_required", "pcs", None, "策略不要求统一单位"),
    )
    return tuple(
        CatalogRuleResultFact(
            rule=rule,
            status=status,
            actual_value=actual,
            required_value=required,
            explanation_code=code,
        )
        for rule, status, actual, required, code in values
    )


def _catalog_cultivation_command(*, expires_at_limit=NOW + timedelta(days=3)):
    from domains.approvals.catalog_contract import (
        CATALOG_CULTIVATION_WARNING,
        CatalogCultivationApprovalCommand,
        catalog_cultivation_request_hash,
    )

    values = {
        "tenant_id": TENANT,
        "proposal_id": "cpr_01K00000000000000000000000",
        "cluster_id": "ncl_01K00000000000000000000000",
        "policy_version_id": "cpv_01K00000000000000000000000",
        "policy_content_hash": "a" * 64,
        "facts_hash": "b" * 64,
        "rule_results": _catalog_rules(),
        "evidence_refs": (CATALOG_EVIDENCE_REF,),
        "proposed_by_run": "run_01K00000000000000000000000",
        "owner_employee": PROPOSER,
        "change_set_ref": (
            "catalog-cultivation:cpr_01K00000000000000000000000:"
            "cpv_01K00000000000000000000000:" + "b" * 64
        ),
        "expires_at_limit": expires_at_limit,
        "warning": CATALOG_CULTIVATION_WARNING,
    }
    return CatalogCultivationApprovalCommand(
        **values,
        request_hash=catalog_cultivation_request_hash(**values),
    )


class _CatalogActorReader:
    def __init__(self) -> None:
        self.role = "boss"
        self.active = True
        self.eligible = True
        self.tenant_id = TENANT
        self.employee_id = APPROVER
        self.error: Exception | None = None

    async def read_actor(self, tenant_id, employee_id):
        from domains.approvals.catalog_contract import CatalogApprovalActorFact

        if self.error is not None:
            raise self.error
        del tenant_id, employee_id
        return CatalogApprovalActorFact(
            tenant_id=self.tenant_id,
            employee_id=self.employee_id,
            current_role=self.role,
            active=self.active,
            eligible=self.eligible,
        )


@pytest.mark.asyncio
async def test_catalog_link_state_is_a_strict_four_role_projection() -> None:
    from domains.approvals.schemas import (
        ApprovalReaderIdentity,
        CatalogApprovalLinkState,
    )
    from shared.errors import PermissionDenied

    factory = _Factory()
    actors = _CatalogActorReader()
    service = ApprovalServiceImpl(
        factory, catalog_actor_reader=actors, now=lambda: NOW
    )
    approval_id = await service.submit_catalog(_catalog_policy_command())

    for role in ("boss", "product", "sourcing", "finance"):
        actors.role = role
        linked = await service.get_catalog_link_state_for_reader(
            TENANT,
            approval_id,
            reader=ApprovalReaderIdentity(employee_id=APPROVER, role=role),
        )
        assert type(linked) is CatalogApprovalLinkState
        assert linked.model_dump(mode="json") == {
            "approval_id": str(approval_id),
            "approval_type": "catalog_proposal_policy_change",
            "state": "pending",
        }

    for role in ("manager", "sales", "viewer"):
        actors.role = role
        with pytest.raises(PermissionDenied):
            await service.get_catalog_link_state_for_reader(
                TENANT,
                approval_id,
                reader=ApprovalReaderIdentity(employee_id=APPROVER, role=role),
            )

    actors.role = "product"
    product = ApprovalReaderIdentity(employee_id=APPROVER, role="product")
    with pytest.raises(PermissionDenied):
        await service.get_for_reader(TENANT, approval_id, reader=product)
    assert await service.list_for_reader(TENANT, reader=product) == []


@pytest.mark.asyncio
async def test_catalog_link_state_revalidates_current_employee_and_catalog_subject() -> None:
    from domains.approvals.catalog_contract import CatalogApprovalContractError
    from domains.approvals.schemas import ApprovalReaderIdentity
    from shared.errors import PermissionDenied

    factory = _Factory()
    actors = _CatalogActorReader()
    actors.role = "product"
    service = ApprovalServiceImpl(
        factory, catalog_actor_reader=actors, now=lambda: NOW
    )
    approval_id = await service.submit_catalog(_catalog_policy_command())
    reader = ApprovalReaderIdentity(employee_id=APPROVER, role="product")

    for mutation in (
        {"error": RuntimeError("postgres://secret-password")},
        {"tenant_id": TenantId("tn_other")},
        {"employee_id": EmployeeId("emp_other")},
        {"role": "finance"},
        {"active": False},
        {"eligible": False},
    ):
        current = _CatalogActorReader()
        current.role = "product"
        for field, value in mutation.items():
            setattr(current, field, value)
        guarded = ApprovalServiceImpl(
            factory, catalog_actor_reader=current, now=lambda: NOW
        )
        with pytest.raises(PermissionDenied) as denied:
            await guarded.get_catalog_link_state_for_reader(
                TENANT, approval_id, reader=reader
            )
        assert "password" not in str(denied.value)

    without_reader = ApprovalServiceImpl(factory, now=lambda: NOW)
    with pytest.raises(PermissionDenied):
        await without_reader.get_catalog_link_state_for_reader(
            TENANT, approval_id, reader=reader
        )

    missing = ApprovalId("apr_01K00000000000000000000009")
    with pytest.raises(CatalogApprovalContractError) as missing_error:
        await service.get_catalog_link_state_for_reader(TENANT, missing, reader=reader)
    assert missing_error.value.code == "catalog_approval_not_found"

    legacy_id = await service.submit(
        TENANT,
        ApprovalType.PLAYBOOK_CHANGE,
        "legacy",
        {"version": "one"},
        "legacy",
        BlastRadius(["playbook"], "apply", "keep", True),
        proposed_by_employee=PROPOSER,
        owner_employee=PROPOSER,
    )
    with pytest.raises(CatalogApprovalContractError) as type_error:
        await service.get_catalog_link_state_for_reader(
            TENANT, legacy_id, reader=reader
        )
    assert type_error.value.code == "catalog_contract_invalid"


@pytest.mark.asyncio
async def test_catalog_submit_uses_strict_command_and_exact_fact_reads() -> None:
    from domains.approvals.catalog_contract import CatalogApprovalContractError

    factory = _Factory()
    actors = _CatalogActorReader()
    service = ApprovalServiceImpl(
        factory, catalog_actor_reader=actors, now=lambda: NOW
    )
    policy = _catalog_policy_command()
    cultivation = _catalog_cultivation_command()

    policy_id = await service.submit_catalog(policy)
    cultivation_id = await service.submit_catalog(cultivation)
    assert await service.submit_catalog(policy) == policy_id
    assert await service.submit_catalog(cultivation) == cultivation_id
    policy_fact = await service.read_catalog_fact(TENANT, policy_id)
    cultivation_fact = await service.find_catalog_fact(
        TENANT, cultivation.change_set_ref
    )
    assert policy_fact.contract_namespace == "catalog-policy-v1"
    assert policy_fact.proposed_change.policy_version_id == policy.policy_version_id
    assert policy_fact.expires_at - policy_fact.created_at == timedelta(days=7)
    assert cultivation_fact is not None
    assert cultivation_fact.contract_namespace == "catalog-cultivation-v1"
    assert cultivation_fact.request_hash == cultivation.request_hash
    assert cultivation_fact.expires_at - cultivation_fact.created_at == timedelta(days=3)
    assert await service.find_catalog_fact(TenantId("tn_other"), cultivation.change_set_ref) is None
    with pytest.raises(CatalogApprovalContractError) as error:
        await service.read_catalog_fact(TenantId("tn_other"), cultivation_id)
    assert error.value.code == "catalog_approval_not_found"


@pytest.mark.asyncio
async def test_catalog_trusted_reads_hide_storage_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domains.approvals.catalog_contract import CatalogApprovalContractError

    service = ApprovalServiceImpl(_Factory(), now=lambda: NOW)
    command = _catalog_policy_command()
    approval_id = await service.submit_catalog(command)

    async def broken_get(self, tenant_id, requested_id):
        del self, tenant_id, requested_id
        raise RuntimeError("postgres://secret-password")

    monkeypatch.setattr(_Approvals, "get", broken_get)
    with pytest.raises(CatalogApprovalContractError) as read_error:
        await service.read_catalog_fact(TENANT, approval_id)
    assert read_error.value.code == "catalog_storage_unavailable"
    assert "password" not in str(read_error.value)

    monkeypatch.undo()

    async def broken_find(self, tenant_id, change_set_ref):
        del self, tenant_id, change_set_ref
        raise RuntimeError("postgres://secret-password")

    monkeypatch.setattr(_Approvals, "find_catalog_by_change_set", broken_find)
    with pytest.raises(CatalogApprovalContractError) as find_error:
        await service.find_catalog_fact(TENANT, command.change_set_ref)
    assert find_error.value.code == "catalog_storage_unavailable"
    assert "password" not in str(find_error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "mutation"),
    [
        ("policy", {"policy_version_id": "cpv_other"}),
        (
            "policy",
            {
                "content": _catalog_policy_command().content.model_copy(
                    update={"minimum_distinct_accounts": 4}
                )
            },
        ),
        ("policy", {"content_hash": "c" * 64}),
        ("policy", {"base_active_version": _catalog_base_policy()}),
        ("policy", {"proposed_by_employee": APPROVER}),
        ("policy", {"owner_employee": APPROVER}),
        ("policy", {"request_hash": "d" * 64}),
        ("policy", {"expires_at_limit": NOW + timedelta(days=6)}),
        ("cultivation", {"proposal_id": "cpr_other"}),
        ("cultivation", {"cluster_id": "ncl_other"}),
        ("cultivation", {"policy_version_id": "cpv_other"}),
        ("cultivation", {"policy_content_hash": "c" * 64}),
        ("cultivation", {"facts_hash": "d" * 64}),
        ("cultivation", {"rule_results": tuple(reversed(_catalog_rules()))}),
        ("cultivation", {"evidence_refs": ("msg_other",)}),
        ("cultivation", {"proposed_by_run": "run_other"}),
        ("cultivation", {"owner_employee": APPROVER}),
        ("cultivation", {"request_hash": "e" * 64}),
        ("cultivation", {"expires_at_limit": NOW + timedelta(days=2)}),
        ("cultivation", {"warning": "不是完整的固定风险提示"}),
    ],
)
async def test_catalog_same_reference_never_reuses_a_mutated_subject(
    kind: str, mutation: dict[str, object]
) -> None:
    from domains.approvals.catalog_contract import CatalogApprovalContractError

    service = ApprovalServiceImpl(
        _Factory(), catalog_actor_reader=_CatalogActorReader(), now=lambda: NOW
    )
    command = (
        _catalog_policy_command()
        if kind == "policy"
        else _catalog_cultivation_command()
    )
    await service.submit_catalog(command)
    changed = command.model_copy(update=mutation)

    with pytest.raises(CatalogApprovalContractError) as error:
        await service.submit_catalog(changed)
    assert error.value.code == "catalog_request_conflict"


@pytest.mark.asyncio
async def test_catalog_full_persisted_package_is_checked_before_exact_replay() -> None:
    from domains.approvals.catalog_contract import (
        CatalogApprovalContractError,
        catalog_package_fact,
        same_catalog_request,
    )

    factory = _Factory()
    service = ApprovalServiceImpl(factory, now=lambda: NOW)
    command = _catalog_policy_command()
    approval_id = await service.submit_catalog(command)
    package = factory.store.packages[approval_id]
    mutations = (
        {"approval_type": ApprovalType.PLAYBOOK_CHANGE},
        {"contract_namespace": "catalog-cultivation-v1"},
        {"title": "被替换的标题"},
        {
            "proposed_change": package.proposed_change
            | {"external_action": "外部发送"}
        },
        {"reason": "被替换的理由"},
        {
            "blast_radius": BlastRadius(
                ["other"], "被替换的批准效果", "被替换的拒绝效果", False
            )
        },
        {"proposed_by_run": "run_01K00000000000000000000009"},
        {"proposed_by_employee": APPROVER},
        {"owner_employee": APPROVER},
        {"evidence_refs": ["msg_01K00000000000000000000009"]},
        {
            "change_set_ref": (
                f"catalog-policy:{command.policy_version_id}:" + "f" * 64
            )
        },
        {"request_hash": "f" * 64},
        {"expires_at_limit": command.expires_at_limit + timedelta(days=1)},
        {"expires_at": package.expires_at - timedelta(seconds=1)},
    )

    for mutation in mutations:
        changed = replace(package, **mutation)
        try:
            catalog_package_fact(changed)
        except CatalogApprovalContractError:
            continue
        assert not same_catalog_request(changed, package), mutation


@pytest.mark.asyncio
async def test_catalog_decision_requires_current_eligible_independent_boss() -> None:
    from shared.errors import PermissionDenied

    factory = _Factory()
    command = _catalog_policy_command()
    without_reader = ApprovalServiceImpl(factory, now=lambda: NOW)
    approval_id = await without_reader.submit_catalog(command)
    with pytest.raises(PermissionDenied, match="目录审批决定人事实不可用"):
        await without_reader.decide(TENANT, approval_id, True, APPROVER)
    assert factory.store.events == []
    assert (await without_reader.read_catalog_fact(TENANT, approval_id)).state.value == "pending"

    for mutation in (
        {"error": RuntimeError("database password must not escape")},
        {"tenant_id": TenantId("tn_other")},
        {"employee_id": EmployeeId("emp_other")},
        {"role": "manager"},
        {"active": False},
        {"eligible": False},
    ):
        actors = _CatalogActorReader()
        for field, value in mutation.items():
            setattr(actors, field, value)
        service = ApprovalServiceImpl(
            factory, catalog_actor_reader=actors, now=lambda: NOW
        )
        with pytest.raises(PermissionDenied) as denied:
            await service.decide(TENANT, approval_id, True, APPROVER)
        assert "password" not in str(denied.value)
        assert factory.store.events == []
        assert (await service.read_catalog_fact(TENANT, approval_id)).state.value == "pending"

    actors = _CatalogActorReader()
    service = ApprovalServiceImpl(factory, catalog_actor_reader=actors, now=lambda: NOW)
    actors.employee_id = PROPOSER
    with pytest.raises(SelfApprovalError):
        await service.decide(TENANT, approval_id, True, PROPOSER)
    assert factory.store.events == []
    actors.employee_id = APPROVER
    await service.decide(TENANT, approval_id, True, APPROVER)
    assert len(factory.store.events) == 1
    assert (await service.read_catalog_fact(TENANT, approval_id)).state.value == "approved"


@pytest.mark.asyncio
async def test_catalog_pending_list_and_detail_share_the_current_boss_guard() -> None:
    from domains.approvals.schemas import ApprovalReaderIdentity
    from shared.errors import PermissionDenied

    factory = _Factory()
    actors = _CatalogActorReader()
    service = ApprovalServiceImpl(
        factory, catalog_actor_reader=actors, now=lambda: NOW
    )
    policy_id = await service.submit_catalog(_catalog_policy_command())
    cultivation_id = await service.submit_catalog(_catalog_cultivation_command())
    boss = ApprovalReaderIdentity(employee_id=APPROVER, role="boss")

    pending = await service.list_for_reader(TENANT, reader=boss)
    assert {item.approval_id for item in pending} == {
        str(policy_id),
        str(cultivation_id),
    }
    assert all(item.can_current_user_decide for item in pending)
    assert (
        await service.get_for_reader(TENANT, policy_id, reader=boss)
    ).can_current_user_decide

    actors.active = False
    with pytest.raises(PermissionDenied):
        await service.list_for_reader(TENANT, reader=boss)
    with pytest.raises(PermissionDenied):
        await service.get_for_reader(TENANT, policy_id, reader=boss)

    actors.active = True
    actors.role = "manager"
    manager = ApprovalReaderIdentity(employee_id=APPROVER, role="manager")
    assert await service.list_for_reader(TENANT, reader=manager) == []
    with pytest.raises(PermissionDenied):
        await service.get_for_reader(TENANT, policy_id, reader=manager)


@pytest.mark.asyncio
async def test_catalog_pending_reader_without_actor_source_fails_closed() -> None:
    from domains.approvals.schemas import ApprovalReaderIdentity
    from shared.errors import PermissionDenied

    factory = _Factory()
    service = ApprovalServiceImpl(factory, now=lambda: NOW)
    approval_id = await service.submit_catalog(_catalog_policy_command())
    boss = ApprovalReaderIdentity(employee_id=APPROVER, role="boss")

    with pytest.raises(PermissionDenied, match="目录审批决定人事实不可用"):
        await service.list_for_reader(TENANT, reader=boss)
    with pytest.raises(PermissionDenied, match="目录审批决定人事实不可用"):
        await service.get_for_reader(TENANT, approval_id, reader=boss)


@pytest.mark.asyncio
async def test_catalog_owner_can_read_but_never_sees_a_decide_button() -> None:
    from domains.approvals.schemas import ApprovalReaderIdentity

    factory = _Factory()
    actors = _CatalogActorReader()
    actors.employee_id = PROPOSER
    service = ApprovalServiceImpl(
        factory, catalog_actor_reader=actors, now=lambda: NOW
    )
    approval_id = await service.submit_catalog(_catalog_policy_command())
    owner = ApprovalReaderIdentity(employee_id=PROPOSER, role="boss")

    pending = await service.list_for_reader(TENANT, reader=owner)
    assert [item.approval_id for item in pending] == [str(approval_id)]
    assert pending[0].can_current_user_decide is False
    assert (
        await service.get_for_reader(TENANT, approval_id, reader=owner)
    ).can_current_user_decide is False


@pytest.mark.asyncio
async def test_generic_submit_cannot_create_a_catalog_approval_shape() -> None:
    from domains.approvals.catalog_contract import CatalogApprovalContractError

    service = _service()
    with pytest.raises(CatalogApprovalContractError) as error:
        await service.submit(
            TENANT,
            ApprovalType.CATALOG_PROPOSAL_POLICY_CHANGE,
            "unsafe",
            {"change": "untyped"},
            "unsafe",
            BlastRadius(["unsafe"], "unsafe", "unsafe", False),
            proposed_by_employee=PROPOSER,
            owner_employee=PROPOSER,
        )
    assert error.value.code == "catalog_contract_invalid"
