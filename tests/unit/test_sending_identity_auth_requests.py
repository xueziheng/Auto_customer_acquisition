"""认证检查请求的权限、幂等、事务与状态机合同。"""

from __future__ import annotations

import importlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from domains.sending_identity.permissions import (
    Actor,
    Phase1SendingIdentityAuthorizer,
    ScopeLevel,
    SendingIdentityScope,
)
from shared.errors import (
    IdempotencyConflict,
    InvalidStateTransition,
    PermissionDenied,
    TenantIsolationViolation,
    ValidationError,
)
from shared.schemas.identifiers import IdempotencyKey, SendingIdentityId, TenantId

_NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
_TENANT = TenantId("tn_01K2C5R6J7ABCDEFGHJKMNPQRS")
_OTHER = TenantId("tn_01K2C5R6J7ABCDEFGHJKMNPQRT")
_IDENTITY = SendingIdentityId("sid_01K2C5R6J7ABCDEFGHJKMNPQRS")
_OTHER_IDENTITY = SendingIdentityId("sid_01K2C5R6J7ABCDEFGHJKMNPQRT")
_models = importlib.import_module("domains.sending_identity.models")
DomainRole = _models.DomainRole
IdentityState = _models.IdentityState
SendingIdentity = _models.SendingIdentity
SuspensionCategory = _models.SuspensionCategory


def _repository():
    return importlib.import_module("domains.sending_identity.repository")


def _service_class():
    module = importlib.import_module("domains.sending_identity.service_impl")
    if not hasattr(module.SendingIdentityServiceImpl, "request_authentication_check"):
        pytest.fail("RED：request_authentication_check 尚未实现")
    return module.SendingIdentityServiceImpl


def _boss() -> Actor:
    return Actor(
        "boss_1",
        SendingIdentityScope(level=ScopeLevel.TENANT),
        "boss",
    )


def _manager() -> Actor:
    return Actor(
        "manager_1",
        SendingIdentityScope(
            level=ScopeLevel.MANAGER,
            allowed_identity_ids=frozenset({_IDENTITY}),
        ),
        "manager",
    )


def _self_actor() -> Actor:
    return Actor("sales_1", SendingIdentityScope(level=ScopeLevel.SELF), "sales")


def _system(identity_id: SendingIdentityId = _IDENTITY) -> Actor:
    return Actor(
        "system_auth",
        SendingIdentityScope(
            level=ScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({identity_id}),
        ),
        "system",
    )


class _Audit:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.records: list[dict[str, object]] = []

    def log(self, **record: object) -> None:
        self.order.append(f"audit:{record['rule']}")
        self.records.append(record)


class _Identities:
    def __init__(self, factory: _Factory) -> None:
        self.factory = factory

    async def get(self, tenant_id, identity_id, *, for_update=False):
        self.factory.order.append("identity:lock" if for_update else "identity:get")
        return self.factory.identities.get(str(identity_id))

    async def update(self, identity):
        self.factory.order.append("identity:update")
        identity.version += 1
        self.factory.identities[str(identity.identity_id)] = identity


class _Actions:
    def __init__(self) -> None:
        self.records: list[object] = []

    async def exists_by_key(self, tenant_id, identity_id, action_key):
        return any(
            getattr(item, "action_key", None) == action_key for item in self.records
        )

    async def add(self, record):
        self.records.append(record)


class _Requests:
    def __init__(self, factory: _Factory) -> None:
        self.factory = factory

    async def create_or_get(self, request):
        self.factory.order.append("request:create_or_get")
        existing = next(
            (
                item
                for item in self.factory.requests.values()
                if item.tenant_id == request.tenant_id
                and item.request_key == request.request_key
            ),
            None,
        )
        if existing is not None:
            return SimpleNamespace(created=False, winner=existing)
        self.factory.requests[str(request.request_id)] = request
        return SimpleNamespace(created=True, winner=request)

    async def get(self, tenant_id, request_id, *, for_update=False):
        self.factory.order.append("request:lock" if for_update else "request:get")
        request = self.factory.requests.get(str(request_id))
        return (
            request if request is not None and request.tenant_id == tenant_id else None
        )

    async def transition(self, tenant_id, request_id, target, completed_at):
        repository = _repository()
        current = await self.get(tenant_id, request_id, for_update=True)
        if current is None:
            return None
        allowed = {
            repository.AuthenticationCheckRequestStatus.REQUESTED: {
                repository.AuthenticationCheckRequestStatus.RUNNING
            },
            repository.AuthenticationCheckRequestStatus.RUNNING: {
                repository.AuthenticationCheckRequestStatus.SUCCEEDED,
                repository.AuthenticationCheckRequestStatus.FAILED,
            },
        }
        if target not in allowed.get(current.status, set()):
            raise InvalidStateTransition("认证检查请求状态转换无效")
        updated = repository.AuthenticationCheckRequestView(
            current.request_id,
            current.tenant_id,
            current.sending_identity_id,
            current.request_key,
            target,
            current.requested_at,
            completed_at,
        )
        self.factory.requests[str(request_id)] = updated
        return updated


class _Bus:
    def __init__(self, factory: _Factory) -> None:
        self.factory = factory

    async def publish(self, event):
        self.factory.order.append("outbox:publish")
        self.factory.events.append(event)


class _Uow:
    def __init__(self, factory: _Factory) -> None:
        self.factory = factory
        self.identities = _Identities(factory)
        self.auth_check_requests = _Requests(factory)
        self.actions = factory.actions
        self.bus = _Bus(factory)

    async def __aenter__(self):
        self.factory.order.append("uow:enter")
        self.snapshot = deepcopy(
            (self.factory.identities, self.factory.requests, self.factory.events)
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None or self.factory.commit_error is not None:
            (
                self.factory.identities,
                self.factory.requests,
                self.factory.events,
            ) = self.snapshot
            self.factory.order.append("uow:rollback")
            if exc_type is None:
                raise self.factory.commit_error  # type: ignore[misc]
            return
        self.factory.order.append("uow:commit")
        return


class _Factory:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.identities: dict[str, Any] = {}
        self.requests: dict[str, object] = {}
        self.events: list[object] = []
        self.actions = _Actions()
        self.commit_error: BaseException | None = None

    def __call__(self, tenant_id):
        return _Uow(self)


def _build(state: Any = IdentityState.CREATED):
    order: list[str] = []
    factory = _Factory(order)
    initial_state = (
        IdentityState.WARMING
        if state in {IdentityState.THROTTLED, IdentityState.SUSPENDED}
        else state
    )
    identity = SendingIdentity(
        _IDENTITY,
        _TENANT,
        "sales@cold.example.com",
        "cold.example.com",
        DomainRole.COLD_OUTREACH,
        _NOW - timedelta(days=1),
        state=initial_state,
    )
    if state is IdentityState.THROTTLED:
        identity.transition_to(IdentityState.THROTTLED)
    elif state is IdentityState.SUSPENDED:
        identity.transition_to(
            IdentityState.SUSPENDED,
            suspension_category=SuspensionCategory.AUTHENTICATION_REGRESSION,
        )
    factory.identities[str(_IDENTITY)] = identity
    audit = _Audit(order)
    service = _service_class()(
        factory,
        Phase1SendingIdentityAuthorizer(_TENANT),
        audit,
        now=lambda: _NOW,
    )
    return service, factory, audit, order


async def test_boss_tenant_request_is_atomic_and_allow_audit_is_post_commit() -> None:
    """缺少 request/state/event 原子性或 commit 前 allow 都会留下虚假授权。"""
    service, factory, audit, order = _build()
    view = await service.request_authentication_check(
        _TENANT,
        _IDENTITY,
        IdempotencyKey("auth-request-1"),
        actor=_boss(),
    )
    repository = _repository()
    assert view.status is repository.AuthenticationCheckRequestStatus.REQUESTED
    assert factory.identities[str(_IDENTITY)].state is IdentityState.AUTH_PENDING
    assert len(factory.requests) == 1
    assert len(factory.events) == 1
    assert type(factory.events[0]).__name__ == "AuthenticationCheckRequested"
    assert order.index("request:create_or_get") < order.index("outbox:publish")
    assert order.index("outbox:publish") < order.index("uow:commit")
    assert order.index("uow:commit") < order.index(
        next(item for item in order if item.startswith("audit:phase1:"))
    )
    assert len(audit.records) == 1


@pytest.mark.parametrize("actor", [_manager(), _self_actor(), _system()])
async def test_manager_self_and_system_are_denied_before_writes(actor: Actor) -> None:
    """认证请求在 Phase 1 只能 boss/TENANT，拒绝审计必须立即且固定。"""
    service, factory, audit, order = _build()
    with pytest.raises(PermissionDenied):
        await service.request_authentication_check(
            _TENANT,
            _IDENTITY,
            IdempotencyKey("auth-request-denied"),
            actor=actor,
        )
    assert factory.requests == {}
    assert factory.events == []
    assert order == ["audit:deny:authorization"]
    assert audit.records[0]["rule"] == "deny:authorization"


async def test_duplicate_key_returns_existing_but_different_identity_conflicts() -> (
    None
):
    """API 重试是幂等成功，同 key 换 identity 必须固定冲突。"""
    service, factory, _audit, _order = _build()
    first = await service.request_authentication_check(
        _TENANT, _IDENTITY, IdempotencyKey("auth-request-duplicate"), actor=_boss()
    )
    again = await service.request_authentication_check(
        _TENANT, _IDENTITY, IdempotencyKey("auth-request-duplicate"), actor=_boss()
    )
    assert again == first
    assert len(factory.requests) == 1
    assert len(factory.events) == 1

    factory.identities[str(_OTHER_IDENTITY)] = SendingIdentity(
        _OTHER_IDENTITY,
        _TENANT,
        "other@cold.example.com",
        "cold.example.com",
        DomainRole.COLD_OUTREACH,
        _NOW - timedelta(days=1),
    )
    with pytest.raises(IdempotencyConflict) as caught:
        await service.request_authentication_check(
            _TENANT,
            _OTHER_IDENTITY,
            IdempotencyKey("auth-request-duplicate"),
            actor=_boss(),
        )
    assert str(caught.value) == "认证检查请求幂等冲突"


@pytest.mark.parametrize(
    "state",
    [
        IdentityState.WARMING,
        IdentityState.ACTIVE,
        IdentityState.THROTTLED,
        IdentityState.SUSPENDED,
    ],
)
async def test_recheck_preserves_send_lifecycle_state(state: Any) -> None:
    """重查认证不得在结果产生前破坏既有 send lifecycle 状态。"""
    service, factory, _audit, _order = _build(state)
    await service.request_authentication_check(
        _TENANT, _IDENTITY, IdempotencyKey(f"recheck-{state.value}"), actor=_boss()
    )
    assert factory.identities[str(_IDENTITY)].state is state


async def test_retired_wrong_tenant_and_noncanonical_inputs_fail_closed() -> None:
    """非法资源输入不得创建 request、状态或 outbox。"""
    service, factory, audit, _order = _build(IdentityState.RETIRED)
    with pytest.raises(InvalidStateTransition):
        await service.request_authentication_check(
            _TENANT, _IDENTITY, IdempotencyKey("retired-check"), actor=_boss()
        )
    for identity_id, key in (
        (SendingIdentityId("sid-bad"), IdempotencyKey("valid-key")),
        (_IDENTITY, IdempotencyKey("Bearer-secret")),
        (_IDENTITY, IdempotencyKey("authorization-ref")),
        (_IDENTITY, IdempotencyKey("contains space")),
        (_IDENTITY, IdempotencyKey("unicode-测试")),
        (_IDENTITY, IdempotencyKey("bad\x00key")),
    ):
        with pytest.raises(ValidationError):
            await service.request_authentication_check(
                _TENANT, identity_id, key, actor=_boss()
            )
    factory.identities[str(_IDENTITY)].tenant_id = _OTHER
    with pytest.raises(TenantIsolationViolation):
        await service.request_authentication_check(
            _TENANT,
            _IDENTITY,
            IdempotencyKey("wrong-tenant"),
            actor=_boss(),
        )
    assert factory.requests == {}
    assert factory.events == []
    assert not any(
        str(item.get("rule", "")).startswith("phase1:") for item in audit.records
    )


async def test_commit_failure_rolls_back_request_state_event_and_zero_allow() -> None:
    """commit 失败不能遗留请求、AUTH_PENDING、event 或 allow 审计。"""
    service, factory, audit, _order = _build()
    factory.commit_error = RuntimeError("private database secret")
    with pytest.raises(RuntimeError):
        await service.request_authentication_check(
            _TENANT,
            _IDENTITY,
            IdempotencyKey("commit-failure"),
            actor=_boss(),
        )
    assert factory.identities[str(_IDENTITY)].state is IdentityState.CREATED
    assert factory.requests == {}
    assert factory.events == []
    assert audit.records == []


async def test_request_status_transitions_are_tenant_scoped_and_terminal_safe() -> None:
    """只有 requested→running→terminal，且终态不可复活或跨租户更新。"""
    service, _factory, _audit, _order = _build()
    requested = await service.request_authentication_check(
        _TENANT, _IDENTITY, IdempotencyKey("status-check"), actor=_boss()
    )
    repository = _repository()
    running = await service.transition_authentication_check_request(
        _TENANT,
        requested.request_id,
        repository.AuthenticationCheckRequestStatus.RUNNING,
        actor=_system(),
    )
    assert running.completed_at is None
    retried_running = await service.transition_authentication_check_request(
        _TENANT,
        requested.request_id,
        repository.AuthenticationCheckRequestStatus.RUNNING,
        actor=_system(),
    )
    assert retried_running == running
    succeeded = await service.transition_authentication_check_request(
        _TENANT,
        requested.request_id,
        repository.AuthenticationCheckRequestStatus.SUCCEEDED,
        actor=_system(),
    )
    assert succeeded.completed_at == _NOW
    fetched = await service.get_authentication_check_request(
        _TENANT,
        requested.request_id,
        actor=_system(),
    )
    assert fetched == succeeded
    with pytest.raises(InvalidStateTransition):
        await service.transition_authentication_check_request(
            _TENANT,
            requested.request_id,
            repository.AuthenticationCheckRequestStatus.RUNNING,
            actor=_system(),
        )
    with pytest.raises(PermissionDenied):
        await service.transition_authentication_check_request(
            _OTHER,
            requested.request_id,
            repository.AuthenticationCheckRequestStatus.RUNNING,
            actor=_system(),
        )
