"""发件身份生命周期 service 的 fake-UoW 行为契约。"""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from domains.sending_identity.errors import (
    AuthenticationNotVerifiedError,
    ColdOutreachDomainViolation,
    DomainRoleConflictError,
    IdentityRetiredError,
    IdentitySuspendedError,
    InvalidDeliveryEventError,
    SendingIdentityNotFoundError,
    WarmupLimitExceededError,
)
from domains.sending_identity.permissions import (
    Actor,
    Phase1SendingIdentityAuthorizer,
    ScopeLevel,
    SendingIdentityAction,
    SendingIdentityScope,
)
from domains.sending_identity.schemas import (
    AuthenticationFailure,
    AuthenticationResult,
    DeliveryEventRecord,
    IdentityRegisterRequest,
    SendReservation,
)
from shared.errors import (
    InvalidStateTransition,
    PermissionDenied,
    TenantIsolationViolation,
    ValidationError,
)
from shared.schemas.identifiers import (
    IdempotencyKey,
    SendingIdentityId,
    TenantId,
    new_id,
)

_models = importlib.import_module("domains.sending_identity.models")
AuthCheck = _models.AuthCheck
AuthenticationFailureCategory = _models.AuthenticationFailureCategory
AuthenticationFixInstruction = _models.AuthenticationFixInstruction
DomainRole = _models.DomainRole
DeliveryEventType = _models.DeliveryEventType
IdentityState = _models.IdentityState
ReputationWindow = _models.ReputationWindow
SendingIdentity = _models.SendingIdentity
SuspensionCategory = _models.SuspensionCategory

_repository = importlib.import_module("domains.sending_identity.repository")
AuthenticationCheckRecord = _repository.AuthenticationCheckRecord
IdentityActionRecord = _repository.IdentityActionRecord
ReservationOutcome = _repository.ReservationOutcome
ReservationResult = _repository.ReservationResult
SendingDomain = _repository.SendingDomain

_NOW = datetime(2026, 8, 10, 12, tzinfo=UTC)
_TENANT = TenantId("tService")


def _service_class():
    try:
        return importlib.import_module(
            "domains.sending_identity.service_impl"
        ).SendingIdentityServiceImpl
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：SendingIdentityServiceImpl 尚未创建（{exc}）")


def _boss() -> Actor:
    return Actor(
        actor_id="boss_1",
        role="boss",
        scope=SendingIdentityScope(level=ScopeLevel.TENANT),
    )


def _system(identity_id: SendingIdentityId) -> Actor:
    return Actor(
        actor_id="system_1",
        role="system",
        scope=SendingIdentityScope(
            level=ScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({identity_id}),
        ),
    )


def _manager(
    *,
    identity_ids: frozenset[SendingIdentityId] | None = None,
    domains: frozenset[str] | None = None,
) -> Actor:
    return Actor(
        actor_id="manager_1",
        role="manager",
        scope=SendingIdentityScope(
            level=ScopeLevel.MANAGER,
            allowed_identity_ids=identity_ids,
            allowed_domains=domains,
        ),
    )


def _failure() -> tuple[AuthenticationFailure, ...]:
    return (
        AuthenticationFailure(
            check=AuthCheck.SPF,
            category=AuthenticationFailureCategory.RECORD_MISSING,
            instruction=AuthenticationFixInstruction.CONFIGURE_SPF,
        ),
    )


def _auth(
    *,
    checked_at: datetime = _NOW,
    passed: bool = True,
    check_ref: str = "dns_check_1",
) -> AuthenticationResult:
    return AuthenticationResult(
        checked_at=checked_at,
        spf_passed=passed,
        dkim_passed=True,
        dmarc_passed=True,
        failures=() if passed else _failure(),
        check_ref=check_ref,
    )


class _Authorizer:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    def preauthorize(self, actor, action, scope, tenant_id):
        self.order.append(f"preauthorize:{action.value}")
        assert scope == actor.scope
        return f"provisional:{action.value}"

    def require(self, actor, action, scope, tenant_id, **kwargs):
        self.order.append(f"require:{action.value}")
        assert scope == actor.scope
        if action is not SendingIdentityAction.IDENTITY_LIST:
            assert kwargs.get("identity_id") is not None or kwargs.get("domain") is not None
        return f"allow-full:{action.value}"


class _DenyingAuthorizer(_Authorizer):
    def __init__(self, order: list[str], stage: str) -> None:
        super().__init__(order)
        self.stage = stage

    def preauthorize(self, actor, action, scope, tenant_id):
        if self.stage == "preauthorize":
            self.order.append(f"preauthorize:{action.value}")
            raise PermissionDenied("private authorization detail")
        return super().preauthorize(actor, action, scope, tenant_id)

    def require(self, actor, action, scope, tenant_id, **kwargs):
        if self.stage == "require":
            self.order.append(f"require:{action.value}")
            raise PermissionDenied("private authorization detail")
        return super().require(actor, action, scope, tenant_id, **kwargs)


class _TracingPhase1Authorizer:
    def __init__(self, tenant_id: TenantId, order: list[str]) -> None:
        self._delegate = Phase1SendingIdentityAuthorizer(tenant_id)
        self.order = order

    def preauthorize(self, actor, action, scope, tenant_id):
        self.order.append(f"preauthorize:{action.value}")
        return self._delegate.preauthorize(actor, action, scope, tenant_id)

    def require(self, actor, action, scope, tenant_id, **kwargs):
        self.order.append(
            f"require:{action.value}:{kwargs.get('identity_id')}:{kwargs.get('domain')}"
        )
        return self._delegate.require(actor, action, scope, tenant_id, **kwargs)


class _Audit:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.records: list[dict[str, object]] = []

    def log(self, **record) -> None:
        self.order.append(f"audit:{record['rule']}")
        self.records.append(record)


class _Domains:
    def __init__(self, store: dict[str, SendingDomain], order: list[str]) -> None:
        self.store = store
        self.order = order

    async def ensure(self, domain: SendingDomain) -> SendingDomain:
        self.order.append("domain:ensure")
        return self.store.setdefault(domain.domain, domain)

    async def add(self, domain: SendingDomain) -> None:
        self.store[domain.domain] = domain

    async def get(self, tenant_id, domain):
        self.order.append("domain:get")
        return self.store.get(domain)


class _Identities:
    def __init__(self, store: dict[str, SendingIdentity], order: list[str]) -> None:
        self.store = store
        self.order = order

    async def register_if_address_absent(self, identity: SendingIdentity):
        self.order.append("identity:register")
        winner = next(
            (item for item in self.store.values() if item.address == identity.address),
            None,
        )
        if winner is None:
            self.store[str(identity.identity_id)] = identity
            return SimpleNamespace(created=True, winner=identity)
        return SimpleNamespace(created=False, winner=winner)

    async def add(self, identity):
        self.store[str(identity.identity_id)] = identity

    async def get(self, tenant_id, identity_id, *, for_update=False):
        self.order.append("identity:get")
        if for_update:
            self.order.append("identity:lock")
        return self.store.get(str(identity_id))

    async def update(self, identity: SendingIdentity) -> None:
        self.order.append("identity:update")
        identity.version += 1
        self.store[str(identity.identity_id)] = identity

    async def find_by_address(self, tenant_id, address):
        return next((item for item in self.store.values() if item.address == address), None)

    async def list_domain_for_update(self, tenant_id, domain):
        self.order.append("identity:list_domain")
        return sorted(
            (item for item in self.store.values() if item.domain == domain),
            key=lambda item: item.identity_id,
        )

    async def find_domain_role(self, tenant_id, domain):
        for item in self.store.values():
            if item.domain == domain:
                return item.role
        return None

    async def list_available_for_campaign(self, tenant_id, scope, limit):
        self.order.append("identity:list_available")
        # 故意忽略 scope，证明 service/authorizer 不可信任 repository 过滤。
        return list(self.store.values())[:limit]


class _AuthChecks:
    def __init__(self, records: list[AuthenticationCheckRecord]) -> None:
        self.records = records

    async def append_if_ref_absent(self, record: AuthenticationCheckRecord):
        winner = next(
            (
                item
                for item in self.records
                if item.identity_id == record.identity_id
                and item.result.check_ref == record.result.check_ref
            ),
            None,
        )
        if winner is None:
            self.records.append(record)
            return SimpleNamespace(created=True, winner=record)
        return SimpleNamespace(created=False, winner=winner)

    async def add(self, record):
        self.records.append(record)

    async def latest_for_identity(self, tenant_id, identity_id):
        matches = [item for item in self.records if item.identity_id == identity_id]
        return max(
            matches,
            key=lambda item: (item.result.checked_at, item.auth_check_id),
            default=None,
        )


class _Actions:
    def __init__(self) -> None:
        self.records: list[IdentityActionRecord] = []

    async def add(self, record):
        self.records.append(record)

    async def exists_by_key(self, tenant_id, identity_id, action_key):
        return any(item.action_key == action_key for item in self.records)


class _Counters:
    def __init__(self, factory: _UowFactory) -> None:
        self._factory = factory

    async def get_count(self, tenant_id, identity_id, on_day):
        return self._factory.counter_values.get((str(identity_id), on_day), 0)


class _Reputation:
    def __init__(self, factory: _UowFactory) -> None:
        self._factory = factory

    async def record_event(self, event: DeliveryEventRecord) -> bool:
        self._factory.order.append("reputation:record_event")
        key = (str(event.tenant_id), str(event.dedup_key))
        if key in self._factory.delivery_event_keys:
            return False
        self._factory.delivery_event_keys.add(key)
        self._factory.delivery_events.append(event)
        return True

    async def compute_window(self, tenant_id, identity_id, window_days, computed_at):
        return self._factory.identity_windows.get(
            str(identity_id),
            ReputationWindow(window_days, computed_at, 0, 0, 0, 0, 0, 0),
        )

    async def compute_domain_window(self, tenant_id, domain, window_days, computed_at):
        return self._factory.domain_windows.get(
            domain,
            ReputationWindow(window_days, computed_at, 0, 0, 0, 0, 0, 0),
        )


class _Reservations:
    def __init__(self, factory: _UowFactory) -> None:
        self._factory = factory

    async def reserve_if_below(
        self,
        tenant_id,
        identity_id,
        reservation_key,
        on_day,
        daily_limit,
        created_at,
    ):
        self._factory.order.append("reservation:reserve")
        if self._factory.reservation_error is not None:
            raise self._factory.reservation_error
        key = (str(identity_id), str(reservation_key))
        existing = self._factory.reservation_values.get(key)
        if existing is not None:
            count = self._factory.counter_values.get(
                (str(identity_id), existing.on_day), 0
            )
            return ReservationResult(ReservationOutcome.EXISTING, existing, count)
        counter_key = (str(identity_id), on_day)
        count = self._factory.counter_values.get(counter_key, 0)
        if count >= daily_limit:
            return ReservationResult(ReservationOutcome.CAP_REACHED, None, count)
        sequence = count + 1
        reservation = SendReservation(
            reservation_id=new_id("res"),
            identity_id=identity_id,
            reservation_key=reservation_key,
            on_day=on_day,
            sequence=sequence,
            daily_limit=daily_limit,
            remaining_today=daily_limit - sequence,
        )
        self._factory.counter_values[counter_key] = sequence
        self._factory.reservation_values[key] = reservation
        return ReservationResult(ReservationOutcome.CREATED, reservation, sequence)


class _Bus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event):
        self.events.append(event)

    async def publish_many(self, events):
        self.events.extend(events)


class _Uow:
    def __init__(self, factory: _UowFactory) -> None:
        self._factory = factory
        self.domains = _Domains(factory.domains, factory.order)
        self.identities = _Identities(factory.identities, factory.order)
        self.auth_checks = _AuthChecks(factory.auth_records)
        self.actions = factory.actions
        self.counters = _Counters(factory)
        self.reputation = _Reputation(factory)
        self.reservations = _Reservations(factory)
        self.bus = factory.bus

    async def __aenter__(self):
        self._factory.order.append("uow:enter")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._factory.order.append("uow:commit" if exc_type is None else "uow:rollback")
        if exc_type is None and self._factory.commit_error is not None:
            raise self._factory.commit_error


class _UowFactory:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.domains: dict[str, SendingDomain] = {}
        self.identities: dict[str, SendingIdentity] = {}
        self.auth_records: list[AuthenticationCheckRecord] = []
        self.actions = _Actions()
        self.bus = _Bus()
        self.counter_values: dict[tuple[str, object], int] = {}
        self.reservation_values: dict[tuple[str, str], SendReservation] = {}
        self.identity_windows: dict[str, ReputationWindow] = {}
        self.domain_windows: dict[str, ReputationWindow] = {}
        self.delivery_events: list[DeliveryEventRecord] = []
        self.delivery_event_keys: set[tuple[str, str]] = set()
        self.commit_error: BaseException | None = None
        self.reservation_error: BaseException | None = None

    def __call__(self, tenant_id):
        return _Uow(self)


def _build(now: datetime = _NOW, *, authorizer: object | None = None):
    order: list[str] = []
    factory = _UowFactory(order)
    audit = _Audit(order)
    service = _service_class()(
        factory,
        authorizer if authorizer is not None else _Authorizer(order),
        audit,
        now=lambda: now,
    )
    return service, factory, audit, order


def _seed(
    factory: _UowFactory,
    *,
    state: IdentityState = IdentityState.CREATED,
    version: int = 0,
) -> SendingIdentity:
    identity_id = SendingIdentityId("sid_01K27XZA00ABCDEFGHJKMNPQRS")
    identity = SendingIdentity(
        identity_id=identity_id,
        tenant_id=_TENANT,
        address="sales@cold.service.example",
        domain="cold.service.example",
        role=DomainRole.COLD_OUTREACH,
        created_at=_NOW - timedelta(days=30),
        state=state,
        version=version,
    )
    factory.identities[str(identity_id)] = identity
    factory.domains[identity.domain] = SendingDomain(
        _TENANT, identity.domain, identity.role, identity.created_at
    )
    return identity


def _seed_available(
    factory: _UowFactory,
    *,
    identity_id: SendingIdentityId,
    domain: str,
) -> SendingIdentity:
    identity = SendingIdentity(
        identity_id=identity_id,
        tenant_id=_TENANT,
        address=f"sales@{domain}",
        domain=domain,
        role=DomainRole.COLD_OUTREACH,
        created_at=_NOW - timedelta(days=30),
        state=IdentityState.WARMING,
        warmup_plan=importlib.import_module(
            "domains.sending_identity.models"
        ).WarmupPlan(_NOW.date(), 50),
    )
    factory.identities[str(identity_id)] = identity
    factory.domains[domain] = SendingDomain(
        _TENANT, domain, identity.role, identity.created_at
    )
    factory.auth_records.append(
        AuthenticationCheckRecord(
            auth_check_id=f"auth_{identity_id}",
            tenant_id=_TENANT,
            identity_id=identity_id,
            result=_auth(check_ref=f"check_{identity_id}"),
            created_at=_NOW,
        )
    )
    return identity


def _seed_sendable(
    factory: _UowFactory,
    *,
    state: IdentityState = IdentityState.WARMING,
    role: DomainRole = DomainRole.COLD_OUTREACH,
    started_on=None,
    target: int = 5,
    auth_passed: bool = True,
) -> SendingIdentity:
    """构造手算发送门禁场景；expected limit 不复用 service 决策。"""
    identity = _seed(factory, state=IdentityState.WARMING)
    identity.role = role
    identity.warmup_plan = _models.WarmupPlan(
        started_on if started_on is not None else _NOW.date(), target
    )
    if state is IdentityState.ACTIVE:
        identity.state = IdentityState.ACTIVE
        identity.activated_at = _NOW - timedelta(days=1)
    elif state is IdentityState.THROTTLED:
        identity.transition_to(IdentityState.THROTTLED)
    elif state is IdentityState.SUSPENDED:
        identity.transition_to(
            IdentityState.SUSPENDED,
            suspension_category=SuspensionCategory.AUTHENTICATION_REGRESSION,
        )
    elif state is not IdentityState.WARMING:
        identity.state = state
        identity.warmup_plan = None
    factory.domains[identity.domain] = SendingDomain(
        _TENANT, identity.domain, role, identity.created_at
    )
    factory.auth_records.append(
        AuthenticationCheckRecord(
            auth_check_id=f"auth_send_{state.value}",
            tenant_id=_TENANT,
            identity_id=identity.identity_id,
            result=_auth(
                passed=auth_passed,
                check_ref=f"send_{state.value}_{'pass' if auth_passed else 'fail'}",
            ),
            created_at=_NOW,
        )
    )
    return identity


async def _invoke_successful_public_method(
    method_name: str,
    service: object,
    factory: _UowFactory,
) -> SendingIdentityAction:
    """为公共方法顺序矩阵提供一个手工确定的合法资源场景。"""
    if method_name == "register":
        await service.register(
            _TENANT,
            IdentityRegisterRequest(
                address="trace@cold.trace.example",
                domain="cold.trace.example",
                role=DomainRole.COLD_OUTREACH,
            ),
            actor=_boss(),
        )
        return SendingIdentityAction.IDENTITY_REGISTER

    state = {
        "begin_authentication": IdentityState.CREATED,
        "record_authentication_result": IdentityState.CREATED,
        "start_warmup": IdentityState.AUTH_PENDING,
        "advance_warmup": IdentityState.WARMING,
        "resume_from_suspension": IdentityState.WARMING,
        "retire": IdentityState.CREATED,
        "get": IdentityState.WARMING,
        "list_available_for_campaign": IdentityState.WARMING,
        "get_domain_reputation": IdentityState.ACTIVE,
        "get_warmup_progress": IdentityState.WARMING,
        "check_send_permission": IdentityState.WARMING,
        "reserve_send_slot": IdentityState.WARMING,
        "record_delivery_event": IdentityState.WARMING,
        "evaluate_reputation": IdentityState.WARMING,
        "resume_from_throttle": IdentityState.WARMING,
    }[method_name]
    identity = _seed(factory, state=state)
    if method_name in {
        "advance_warmup",
        "resume_from_suspension",
        "get",
        "list_available_for_campaign",
        "get_warmup_progress",
        "check_send_permission",
        "reserve_send_slot",
        "record_delivery_event",
        "evaluate_reputation",
        "resume_from_throttle",
    }:
        identity.warmup_plan = importlib.import_module(
            "domains.sending_identity.models"
        ).WarmupPlan(_NOW.date() - timedelta(days=28), 50)
    if method_name in {
        "start_warmup",
        "resume_from_suspension",
        "get",
        "list_available_for_campaign",
        "check_send_permission",
        "reserve_send_slot",
        "resume_from_throttle",
    }:
        factory.auth_records.append(
            AuthenticationCheckRecord(
                auth_check_id=f"auth_trace_{method_name}",
                tenant_id=_TENANT,
                identity_id=identity.identity_id,
                result=_auth(check_ref=f"trace_{method_name}"),
                created_at=_NOW,
            )
        )
    if method_name == "resume_from_suspension":
        identity.transition_to(
            IdentityState.SUSPENDED,
            suspension_category=SuspensionCategory.AUTHENTICATION_REGRESSION,
        )
    if method_name == "resume_from_throttle":
        identity.transition_to(IdentityState.THROTTLED)

    if method_name == "begin_authentication":
        await service.begin_authentication(_TENANT, identity.identity_id, actor=_boss())
        return SendingIdentityAction.AUTH_CHECK_BEGIN
    if method_name == "record_authentication_result":
        await service.record_authentication_result(
            _TENANT, identity.identity_id, _auth(), actor=_system(identity.identity_id)
        )
        return SendingIdentityAction.AUTH_RESULT_RECORD
    if method_name == "start_warmup":
        await service.start_warmup(_TENANT, identity.identity_id, 50, actor=_boss())
        return SendingIdentityAction.WARMUP_START
    if method_name == "advance_warmup":
        await service.advance_warmup(
            _TENANT, identity.identity_id, actor=_system(identity.identity_id)
        )
        return SendingIdentityAction.WARMUP_ADVANCE
    if method_name == "resume_from_suspension":
        await service.resume_from_suspension(
            _TENANT, identity.identity_id, "reviewed", actor=_boss()
        )
        return SendingIdentityAction.SUSPENSION_RESUME
    if method_name == "retire":
        await service.retire(_TENANT, identity.identity_id, "retired", actor=_boss())
        return SendingIdentityAction.IDENTITY_RETIRE
    if method_name == "get":
        await service.get(_TENANT, identity.identity_id, actor=_boss())
        return SendingIdentityAction.IDENTITY_READ
    if method_name == "list_available_for_campaign":
        await service.list_available_for_campaign(_TENANT, limit=10, actor=_boss())
        return SendingIdentityAction.IDENTITY_LIST
    if method_name == "get_domain_reputation":
        await service.get_domain_reputation(_TENANT, identity.domain, actor=_boss())
        return SendingIdentityAction.REPUTATION_READ
    if method_name == "get_warmup_progress":
        await service.get_warmup_progress(
            _TENANT, identity.identity_id, actor=_boss()
        )
        return SendingIdentityAction.IDENTITY_READ
    if method_name == "check_send_permission":
        await service.check_send_permission(
            _TENANT, identity.identity_id, True, actor=_boss()
        )
        return SendingIdentityAction.SEND_PERMISSION_READ
    if method_name == "reserve_send_slot":
        await service.reserve_send_slot(
            _TENANT,
            identity.identity_id,
            IdempotencyKey("trace-reservation"),
            True,
            actor=_system(identity.identity_id),
        )
        return SendingIdentityAction.SEND_SLOT_RESERVE
    if method_name == "record_delivery_event":
        await service.record_delivery_event(
            _TENANT,
            identity.identity_id,
            DeliveryEventRecord(
                tenant_id=_TENANT,
                identity_id=identity.identity_id,
                event_type=DeliveryEventType.DELIVERED,
                occurred_at=_NOW,
                dedup_key=IdempotencyKey("trace-delivery-event"),
                source_ref="trace-event-ref",
            ),
            actor=_system(identity.identity_id),
        )
        return SendingIdentityAction.DELIVERY_EVENT_RECORD
    if method_name == "evaluate_reputation":
        await service.evaluate_reputation(
            _TENANT, identity.identity_id, actor=_system(identity.identity_id)
        )
        return SendingIdentityAction.REPUTATION_EVALUATE
    if method_name == "resume_from_throttle":
        await service.resume_from_throttle(
            _TENANT, identity.identity_id, actor=_system(identity.identity_id)
        )
        return SendingIdentityAction.THROTTLE_RESUME
    raise AssertionError(f"未覆盖 public method: {method_name}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name,resource_marker",
    [
        ("register", None),
        ("begin_authentication", "identity:get"),
        ("record_authentication_result", "identity:get"),
        ("start_warmup", "identity:get"),
        ("advance_warmup", "identity:get"),
        ("resume_from_suspension", "identity:get"),
        ("retire", "identity:get"),
        ("get", "identity:get"),
        ("list_available_for_campaign", "identity:list_available"),
        ("get_domain_reputation", "domain:get"),
        ("get_warmup_progress", "identity:get"),
        ("check_send_permission", "identity:get"),
        ("reserve_send_slot", "identity:get"),
        ("record_delivery_event", "identity:list_domain"),
        ("evaluate_reputation", "identity:list_domain"),
        ("resume_from_throttle", "identity:list_domain"),
    ],
)
async def test_every_public_method_uses_two_phase_authorization_and_post_commit_allow(
    method_name: str,
    resource_marker: str | None,
) -> None:
    """删掉 preauthorize、资源后 require 或 commit 后 allow 任一环都必须失败。"""
    service, factory, audit, order = _build()
    action = await _invoke_successful_public_method(method_name, service, factory)
    assert order[0] == f"preauthorize:{action.value}"
    if resource_marker is None:
        assert order.index(f"require:{action.value}") < order.index("uow:enter")
    else:
        assert order.index(resource_marker) < order.index(f"require:{action.value}")
    assert order.count(f"require:{action.value}") == 1
    assert order.index(f"require:{action.value}") < order.index("uow:commit")
    assert order[-1] == f"audit:allow-full:{action.value}"
    assert audit.records == [
        {
            "actor": audit.records[0]["actor"],
            "action": action.value,
            "tenant_id": _TENANT,
            "scope": audit.records[0]["scope"],
            "rule": f"allow-full:{action.value}",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["preauthorize", "require"])
async def test_either_authorization_phase_denial_emits_one_safe_fixed_audit(
    stage: str,
) -> None:
    """任一授权阶段拒绝都只能产生一条脱敏 deny，不能产生 provisional allow。"""
    order: list[str] = []
    authorizer = _DenyingAuthorizer(order, stage)
    service, factory, audit, service_order = _build(authorizer=authorizer)
    authorizer.order = service_order
    identity = _seed(factory)
    with pytest.raises(PermissionDenied, match="private authorization detail"):
        await service.get(_TENANT, identity.identity_id, actor=_boss())
    assert audit.records == [
        {
            "actor": "boss_1",
            "action": SendingIdentityAction.IDENTITY_READ.value,
            "tenant_id": _TENANT,
            "scope": ScopeLevel.TENANT.value,
            "rule": "deny:authorization",
        }
    ]
    assert all(
        secret not in str(audit.records)
        for secret in (
            str(identity.identity_id),
            identity.domain,
            identity.address,
            "private authorization detail",
        )
    )
    assert not any(str(record["rule"]).startswith("allow") for record in audit.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["get", "get_warmup_progress"])
@pytest.mark.parametrize(
    "scope_kind,allowed",
    [
        ("domain", True),
        ("domain", False),
        ("identity", True),
        ("identity", False),
        ("both", True),
        ("both", False),
    ],
)
async def test_manager_identity_queries_apply_full_domain_and_identity_abac(
    method_name: str,
    scope_kind: str,
    allowed: bool,
) -> None:
    """domain-only、identity-only 与双维 manager 都必须在真实 row load 后判定。"""
    authorizer = Phase1SendingIdentityAuthorizer(_TENANT)
    service, factory, audit, _ = _build(authorizer=authorizer)
    identity = _seed(factory, state=IdentityState.WARMING)
    identity.warmup_plan = importlib.import_module(
        "domains.sending_identity.models"
    ).WarmupPlan(_NOW.date(), 50)
    ids = frozenset(
        {
            identity.identity_id
            if allowed
            else SendingIdentityId("sid_01K27XZA00ABCDEFGHJKMNPQRT")
        }
    )
    domains = frozenset({identity.domain if allowed else "other.service.example"})
    actor = _manager(
        identity_ids=ids if scope_kind in {"identity", "both"} else None,
        domains=domains if scope_kind in {"domain", "both"} else None,
    )
    call = getattr(service, method_name)
    if allowed:
        result = await call(_TENANT, identity.identity_id, actor=actor)
        assert result is not None
        assert len(audit.records) == 1
        assert audit.records[0]["rule"] == "phase1:manager:manager:identity:read"
    else:
        with pytest.raises(PermissionDenied):
            await call(_TENANT, identity.identity_id, actor=actor)
        assert len(audit.records) == 1
        assert audit.records[0]["rule"] == "deny:authorization"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope_kind,mismatch_dimension,allowed",
    [
        ("identity", None, True),
        ("identity", "identity", False),
        ("domain", None, True),
        ("domain", "domain", False),
        ("both", None, True),
        ("both", "identity", False),
        ("both", "domain", False),
    ],
)
async def test_manager_list_rechecks_every_scope_dimension_against_ignored_repo_scope(
    scope_kind: str,
    mismatch_dimension: str | None,
    allowed: bool,
) -> None:
    """IDENTITY_LIST action 不能把故意忽略 scope 的 fake repo 结果视为可信。"""
    service, factory, audit, _ = _build(
        authorizer=Phase1SendingIdentityAuthorizer(_TENANT)
    )
    row = _seed_available(
        factory,
        identity_id=SendingIdentityId("sid_01K27XZA00ABCDEFGHJKMNPQRS"),
        domain="cold.list-abac.example",
    )
    allowed_identity = (
        SendingIdentityId("sid_01K27XZA00ABCDEFGHJKMNPQRT")
        if mismatch_dimension == "identity"
        else row.identity_id
    )
    allowed_domain = (
        "other.list-abac.example"
        if mismatch_dimension == "domain"
        else row.domain
    )
    actor = _manager(
        identity_ids=(
            frozenset({allowed_identity})
            if scope_kind in {"identity", "both"}
            else None
        ),
        domains=(
            frozenset({allowed_domain})
            if scope_kind in {"domain", "both"}
            else None
        ),
    )
    if allowed:
        views = await service.list_available_for_campaign(
            _TENANT, limit=10, actor=actor
        )
        assert [view.identity_id for view in views] == [row.identity_id]
        assert audit.records == [
            {
                "actor": "manager_1",
                "action": SendingIdentityAction.IDENTITY_LIST.value,
                "tenant_id": _TENANT,
                "scope": ScopeLevel.MANAGER.value,
                "rule": "phase1:manager:manager:identity:list",
            }
        ]
    else:
        with pytest.raises(PermissionDenied, match="Phase 1 发件身份授权拒绝"):
            await service.list_available_for_campaign(_TENANT, limit=10, actor=actor)
        assert audit.records == [
            {
                "actor": "manager_1",
                "action": SendingIdentityAction.IDENTITY_LIST.value,
                "tenant_id": _TENANT,
                "scope": ScopeLevel.MANAGER.value,
                "rule": "deny:authorization",
            }
        ]


@pytest.mark.asyncio
async def test_manager_list_denies_whole_multirow_result_on_any_unauthorized_row() -> None:
    """第二行越权时不得返回第一行或留下 allow audit。"""
    service, factory, audit, _ = _build(
        authorizer=Phase1SendingIdentityAuthorizer(_TENANT)
    )
    allowed_row = _seed_available(
        factory,
        identity_id=SendingIdentityId("sid_01K27XZA00ABCDEFGHJKMNPQRS"),
        domain="allowed.list-abac.example",
    )
    _seed_available(
        factory,
        identity_id=SendingIdentityId("sid_01K27XZA00ABCDEFGHJKMNPQRT"),
        domain="denied.list-abac.example",
    )
    actor = _manager(identity_ids=frozenset({allowed_row.identity_id}))
    with pytest.raises(PermissionDenied, match="Phase 1 发件身份授权拒绝"):
        await service.list_available_for_campaign(_TENANT, limit=10, actor=actor)
    assert audit.records == [
        {
            "actor": "manager_1",
            "action": SendingIdentityAction.IDENTITY_LIST.value,
            "tenant_id": _TENANT,
            "scope": ScopeLevel.MANAGER.value,
            "rule": "deny:authorization",
        }
    ]
    assert not any(str(record["rule"]).startswith("phase1:") for record in audit.records)


@pytest.mark.asyncio
async def test_manager_empty_list_uses_one_targetless_full_require_then_allows() -> None:
    """空结果必须显式 full authorize，且 allow 只能发生在 UoW 成功退出后。"""
    tracing = _TracingPhase1Authorizer(_TENANT, [])
    service, _, audit, order = _build(authorizer=tracing)
    tracing.order = order
    actor = _manager(
        identity_ids=frozenset(
            {SendingIdentityId("sid_01K27XZA00ABCDEFGHJKMNPQRS")}
        )
    )
    assert await service.list_available_for_campaign(
        _TENANT, limit=10, actor=actor
    ) == []
    targetless = f"require:{SendingIdentityAction.IDENTITY_LIST.value}:None:None"
    assert order.count(targetless) == 1
    assert order[0] == f"preauthorize:{SendingIdentityAction.IDENTITY_LIST.value}"
    assert order.index("identity:list_available") < order.index(targetless)
    assert order.index(targetless) < order.index("uow:commit")
    assert order[-1] == "audit:phase1:manager:manager:identity:list"
    assert len(audit.records) == 1


@pytest.mark.asyncio
async def test_register_is_authorized_first_commits_action_v0_then_audits() -> None:
    """register 的安全顺序、sid_ ID 和创建 action v0 不得漂移。"""
    service, factory, audit, order = _build()
    request = IdentityRegisterRequest(
        address=" Sales@COLD.SERVICE.EXAMPLE ",
        domain="COLD.SERVICE.EXAMPLE",
        role=DomainRole.COLD_OUTREACH,
        display_name="Sales",
        connector_ref="gmail_sales",
    )
    identity_id = await service.register(_TENANT, request, actor=_boss())
    assert str(identity_id).startswith("sid_") and len(str(identity_id)) == 30
    stored = factory.identities[str(identity_id)]
    assert stored.state is IdentityState.CREATED
    assert factory.actions.records[0].action_key == (
        f"identity:{identity_id}:v0:{SendingIdentityAction.IDENTITY_REGISTER.value}"
    )
    assert order[0] == "preauthorize:identity:register"
    assert order.index("require:identity:register") < order.index("uow:enter")
    assert order.index("uow:commit") < order.index("audit:allow-full:identity:register")
    assert len(audit.records) == 1


@pytest.mark.asyncio
async def test_register_idempotency_requires_all_safe_fields_and_domain_role() -> None:
    """同地址仅完整同值幂等；domain/address winner 的 typed 差异必须固定拒绝。"""
    service, factory, audit, _ = _build()
    request = IdentityRegisterRequest(
        address="sales@cold.service.example",
        domain="cold.service.example",
        role=DomainRole.COLD_OUTREACH,
        display_name="Sales",
        connector_ref="gmail_sales",
    )
    first = await service.register(_TENANT, request, actor=_boss())
    assert await service.register(_TENANT, request, actor=_boss()) == first
    assert len(factory.identities) == 1
    assert len(factory.actions.records) == 1
    assert len(audit.records) == 2

    with pytest.raises(ValidationError):
        await service.register(
            _TENANT,
            IdentityRegisterRequest(
                address=request.address,
                domain=request.domain,
                role=request.role,
                display_name="Different",
                connector_ref=request.connector_ref,
            ),
            actor=_boss(),
        )
    factory.domains[request.domain] = SendingDomain(
        _TENANT, request.domain, DomainRole.PRIMARY_BUSINESS, _NOW
    )
    with pytest.raises(DomainRoleConflictError):
        await service.register(_TENANT, request, actor=_boss())


@pytest.mark.asyncio
async def test_lifecycle_auth_does_not_auto_warm_and_activation_occurs_on_day_29_once() -> None:
    """认证全过仍须显式预热，且 day28 不能提前激活。"""
    service, factory, audit, _ = _build(now=_NOW)
    identity = _seed(factory)
    boss = _boss()
    system = _system(identity.identity_id)
    await service.begin_authentication(_TENANT, identity.identity_id, actor=boss)
    assert identity.state is IdentityState.AUTH_PENDING
    assert factory.actions.records[-1].action_key.endswith("v1:auth:check_begin")

    await service.record_authentication_result(
        _TENANT, identity.identity_id, _auth(), actor=system
    )
    assert identity.state is IdentityState.AUTH_PENDING
    await service.start_warmup(
        _TENANT, identity.identity_id, 100, actor=boss
    )
    assert identity.state is IdentityState.WARMING
    assert identity.warmup_plan is not None
    assert identity.warmup_plan.started_on == _NOW.date()

    day28_service, _, _, _ = _build(now=_NOW + timedelta(days=27))
    day28_service._uow_factory = factory
    await day28_service.advance_warmup(
        _TENANT, identity.identity_id, actor=system
    )
    assert identity.state is IdentityState.WARMING
    assert factory.bus.events == []

    day29_service, _, _, _ = _build(now=_NOW + timedelta(days=28))
    day29_service._uow_factory = factory
    await day29_service.advance_warmup(
        _TENANT, identity.identity_id, actor=system
    )
    assert identity.state is IdentityState.ACTIVE
    assert len(factory.bus.events) == 1
    await day29_service.advance_warmup(
        _TENANT, identity.identity_id, actor=system
    )
    assert len(factory.bus.events) == 1
    assert len(audit.records) >= 3


@pytest.mark.asyncio
async def test_auth_ref_is_typed_idempotent_and_only_latest_regression_suspends() -> None:
    """乱序旧检查只进 history；相同 ref 异内容拒绝；最新退化才熔断。"""
    service, factory, audit, _ = _build()
    identity = _seed(factory, state=IdentityState.ACTIVE, version=4)
    identity.activated_at = _NOW - timedelta(days=1)
    system = _system(identity.identity_id)
    newest_pass = _auth(checked_at=_NOW, check_ref="newest_pass")
    await service.record_authentication_result(
        _TENANT, identity.identity_id, newest_pass, actor=system
    )
    await service.record_authentication_result(
        _TENANT,
        identity.identity_id,
        _auth(
            checked_at=_NOW - timedelta(hours=1),
            passed=False,
            check_ref="old_failure",
        ),
        actor=system,
    )
    assert identity.state is IdentityState.ACTIVE

    await service.record_authentication_result(
        _TENANT, identity.identity_id, newest_pass, actor=system
    )
    assert len(factory.auth_records) == 2
    with pytest.raises(ValidationError):
        await service.record_authentication_result(
            _TENANT,
            identity.identity_id,
            _auth(checked_at=_NOW, passed=False, check_ref="newest_pass"),
            actor=system,
        )

    await service.record_authentication_result(
        _TENANT,
        identity.identity_id,
        _auth(checked_at=_NOW + timedelta(minutes=1), passed=False, check_ref="regress"),
        actor=system,
    )
    assert identity.state is IdentityState.SUSPENDED
    assert identity.suspension_category is SuspensionCategory.AUTHENTICATION_REGRESSION
    assert identity.sendable_state_before_restriction is IdentityState.ACTIVE
    assert len(factory.bus.events) == 1
    assert factory.actions.records[-1].action_key.endswith(
        "v5:auth:result_record"
    )
    assert len(audit.records) == 4


@pytest.mark.asyncio
async def test_auth_time_bounds_and_retired_state_fail_without_allow_audit() -> None:
    """时间边界闭区间接受，边界外一微秒拒绝，retired 不新增 history。"""
    service, factory, audit, _ = _build()
    identity = _seed(factory)
    system = _system(identity.identity_id)
    for index, checked_at in enumerate(
        (identity.created_at, _NOW + timedelta(minutes=5))
    ):
        await service.record_authentication_result(
            _TENANT,
            identity.identity_id,
            _auth(checked_at=checked_at, check_ref=f"accepted_{index}"),
            actor=system,
        )
    assert len(factory.auth_records) == 2
    assert len(audit.records) == 2
    for checked_at in (
        identity.created_at - timedelta(microseconds=1),
        _NOW + timedelta(minutes=5, microseconds=1),
    ):
        with pytest.raises(ValidationError):
            await service.record_authentication_result(
                _TENANT,
                identity.identity_id,
                _auth(checked_at=checked_at, check_ref=f"ref_{checked_at.minute}"),
                actor=system,
            )
    assert len(audit.records) == 2
    identity.state = IdentityState.RETIRED
    with pytest.raises(InvalidStateTransition):
        await service.record_authentication_result(
            _TENANT, identity.identity_id, _auth(), actor=system
        )
    assert len(factory.auth_records) == 2


@pytest.mark.asyncio
async def test_throttled_auth_regression_preserves_original_sendable_state() -> None:
    """THROTTLED→SUSPENDED 若重写 saved state，后续恢复将无法回到 ACTIVE。"""
    service, factory, _, _ = _build()
    identity = _seed(factory, state=IdentityState.ACTIVE, version=7)
    identity.activated_at = _NOW - timedelta(days=1)
    identity.transition_to(IdentityState.THROTTLED)
    assert identity.sendable_state_before_restriction is IdentityState.ACTIVE
    await service.record_authentication_result(
        _TENANT,
        identity.identity_id,
        _auth(passed=False, check_ref="throttled_regression"),
        actor=_system(identity.identity_id),
    )
    assert identity.state is IdentityState.SUSPENDED
    assert identity.sendable_state_before_restriction is IdentityState.ACTIVE
    assert identity.suspension_category is SuspensionCategory.AUTHENTICATION_REGRESSION


@pytest.mark.asyncio
async def test_resume_rejects_latest_auth_failure_without_allow_audit() -> None:
    """历史曾通过不能绕过 latest-fail 恢复受限身份。"""
    service, factory, audit, _ = _build()
    identity = _seed(factory, state=IdentityState.ACTIVE, version=5)
    identity.transition_to(
        IdentityState.SUSPENDED,
        suspension_category=SuspensionCategory.AUTHENTICATION_REGRESSION,
    )
    for auth_id, result in (
        ("auth_old_pass", _auth(checked_at=_NOW - timedelta(minutes=1), check_ref="old_pass")),
        ("auth_latest_fail", _auth(passed=False, check_ref="latest_fail")),
    ):
        factory.auth_records.append(
            AuthenticationCheckRecord(
                auth_check_id=auth_id,
                tenant_id=_TENANT,
                identity_id=identity.identity_id,
                result=result,
                created_at=_NOW,
            )
        )
    with pytest.raises(AuthenticationNotVerifiedError):
        await service.resume_from_suspension(
            _TENANT, identity.identity_id, "reviewed", actor=_boss()
        )
    assert identity.state is IdentityState.SUSPENDED
    assert audit.records == []


@pytest.mark.asyncio
async def test_stateful_write_failures_never_emit_allow_audit() -> None:
    """validation/not-found/state/concurrency/commit 任一失败都不能记录成功授权。"""
    # validation
    service, factory, audit, _ = _build()
    identity = _seed(factory)
    with pytest.raises(ValidationError):
        await service.record_authentication_result(
            _TENANT,
            identity.identity_id,
            _auth(
                checked_at=_NOW + timedelta(minutes=5, microseconds=1),
                check_ref="invalid_time",
            ),
            actor=_system(identity.identity_id),
        )
    assert audit.records == []

    # not found
    service, _, audit, _ = _build()
    with pytest.raises(SendingIdentityNotFoundError):
        await service.begin_authentication(
            _TENANT,
            SendingIdentityId("sid_01K27XZA00ABCDEFGHJKMNPQRT"),
            actor=_boss(),
        )
    assert audit.records == []

    # invalid state
    service, factory, audit, _ = _build()
    identity = _seed(factory, state=IdentityState.AUTH_PENDING)
    with pytest.raises(InvalidStateTransition):
        await service.begin_authentication(_TENANT, identity.identity_id, actor=_boss())
    assert audit.records == []

    # concurrent address winner differs in a safe field
    service, factory, audit, _ = _build()
    identity = _seed(factory)
    identity.display_name = "Winner"
    with pytest.raises(ValidationError, match="发件身份登记冲突"):
        await service.register(
            _TENANT,
            IdentityRegisterRequest(
                address=identity.address,
                domain=identity.domain,
                role=identity.role,
                display_name="Loser",
            ),
            actor=_boss(),
        )
    assert audit.records == []

    # commit
    service, factory, audit, _ = _build()
    factory.commit_error = RuntimeError("private commit failure")
    with pytest.raises(RuntimeError, match="private commit failure"):
        await service.register(
            _TENANT,
            IdentityRegisterRequest(
                address="commit@cold.failure-matrix.example",
                domain="cold.failure-matrix.example",
                role=DomainRole.COLD_OUTREACH,
            ),
            actor=_boss(),
        )
    assert audit.records == []


@pytest.mark.asyncio
async def test_suspension_recovery_preserves_warming_note_privacy_and_retire_is_idempotent() -> None:
    """boss 恢复只能回 saved state；note 仅进 action；重复 retire 无副作用。"""
    service, factory, audit, _ = _build()
    identity = _seed(factory, state=IdentityState.WARMING, version=3)
    identity.warmup_plan = importlib.import_module(
        "domains.sending_identity.models"
    ).WarmupPlan(_NOW.date() - timedelta(days=5), 50)
    identity.transition_to(
        IdentityState.SUSPENDED,
        suspension_category=SuspensionCategory.AUTHENTICATION_REGRESSION,
    )
    factory.auth_records.append(
        AuthenticationCheckRecord(
            auth_check_id="auth_resume",
            tenant_id=_TENANT,
            identity_id=identity.identity_id,
            result=_auth(),
            created_at=_NOW,
        )
    )
    note = "  reviewed configuration  "
    await service.resume_from_suspension(
        _TENANT, identity.identity_id, note, actor=_boss()
    )
    assert identity.state is IdentityState.WARMING
    assert factory.actions.records[-1].note == "reviewed configuration"
    assert all(note.strip() not in str(record) for record in audit.records)

    await service.retire(_TENANT, identity.identity_id, " retired ", actor=_boss())
    counts = (len(factory.actions.records), len(factory.bus.events), len(audit.records))
    await service.retire(_TENANT, identity.identity_id, "ignored", actor=_boss())
    assert (len(factory.actions.records), len(factory.bus.events), len(audit.records)) == (
        counts[0], counts[1], counts[2] + 1
    )


@pytest.mark.asyncio
async def test_public_methods_authorize_before_validation_and_commit_failure_has_no_allow() -> None:
    """authorizer 必须是首调用；commit/validation 失败都没有 allow 审计。"""
    service, factory, audit, order = _build()
    with pytest.raises(ValidationError):
        await service.list_available_for_campaign(
            _TENANT, limit=True, actor=_boss()
        )
    assert order == ["preauthorize:identity:list"]
    factory.commit_error = RuntimeError("private commit failure")
    with pytest.raises(RuntimeError):
        await service.register(
            _TENANT,
            IdentityRegisterRequest(
                address="sales@cold.failure.example",
                domain="cold.failure.example",
                role=DomainRole.COLD_OUTREACH,
            ),
            actor=_boss(),
        )
    assert audit.records == []


@pytest.mark.asyncio
async def test_query_row_tenant_corruption_denies_whole_result_without_payload_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """repo 损坏行必须整单 fail closed，并仅写固定安全 tenant 告警。"""
    service, factory, audit, _ = _build()
    identity = _seed(factory, state=IdentityState.ACTIVE)
    identity.activated_at = _NOW
    identity.tenant_id = TenantId("tCorrupted")
    with (
        caplog.at_level("CRITICAL", logger="security.tenant_isolation"),
        pytest.raises(TenantIsolationViolation),
    ):
        await service.get(_TENANT, identity.identity_id, actor=_boss())
    assert [record.getMessage() for record in caplog.records] == [
        "检测到跨租户数据隔离违规"
    ]
    assert "cold.service.example" not in caplog.text
    assert "sales@" not in caplog.text
    assert len(audit.records) == 1
    assert audit.records[0]["rule"] == "deny:tenant_isolation"


@pytest.mark.asyncio
async def test_queries_build_safe_views_and_recheck_campaign_rows() -> None:
    """查询只返回安全 DTO；campaign 行须复核角色、认证和可发送状态。"""
    service, factory, audit, _ = _build()
    identity = _seed(factory, state=IdentityState.WARMING)
    model_module = importlib.import_module("domains.sending_identity.models")
    identity.warmup_plan = model_module.WarmupPlan(_NOW.date(), 50)
    factory.auth_records.append(
        AuthenticationCheckRecord(
            auth_check_id="auth_view",
            tenant_id=_TENANT,
            identity_id=identity.identity_id,
            result=_auth(),
            created_at=_NOW,
        )
    )
    view = await service.get(_TENANT, identity.identity_id, actor=_boss())
    assert view.identity_id == identity.identity_id
    assert view.auth is not None and view.auth.all_passed
    assert not hasattr(view, "connector_ref")
    assert "dns_check_1" not in repr(view)

    listed = await service.list_available_for_campaign(
        _TENANT, limit=10, actor=_boss()
    )
    assert [item.identity_id for item in listed] == [identity.identity_id]
    identity.role = DomainRole.PRIMARY_BUSINESS
    with pytest.raises(ValidationError):
        await service.list_available_for_campaign(
            _TENANT, limit=10, actor=_boss()
        )
    assert len(audit.records) == 2


@pytest.mark.asyncio
async def test_missing_and_unverified_warmup_paths_are_closed() -> None:
    """不存在或 latest auth 未全过均不得启动预热。"""
    service, factory, audit, _ = _build()
    missing = SendingIdentityId("sid_01K27XZA00ABCDEFGHJKMNPQRT")
    with pytest.raises(SendingIdentityNotFoundError):
        await service.get(_TENANT, missing, actor=_boss())
    identity = _seed(factory, state=IdentityState.AUTH_PENDING)
    with pytest.raises(AuthenticationNotVerifiedError):
        await service.start_warmup(
            _TENANT, identity.identity_id, 50, actor=_boss()
        )
    assert len(audit.records) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "allowed", "reason", "daily_limit"),
    [
        ("created", False, "发件身份尚未进入可发送预热", 0),
        ("auth_pending", False, "发件身份尚未进入可发送预热", 0),
        ("throttled", False, "发件身份当前已限流", 5),
        ("suspended", False, "发件身份当前已停用", 5),
        ("retired", False, "发件身份已退役", 0),
        ("latest_auth_failed", False, "发件身份认证未全部通过", 5),
        ("before_start", False, "发件身份预热尚未开始", 0),
        ("warming_day_1", True, None, 5),
        ("warming_day_28", True, None, 100),
        ("active", True, None, 100),
        ("cap", False, "当日发送额度已用尽", 5),
        ("identity_below_minimum", True, None, 5),
        ("domain_below_minimum", True, None, 5),
        ("identity_rate", False, "身份信誉窗口已触发发送限制", 5),
        ("domain_rate", False, "域名信誉窗口已触发发送限制", 5),
        ("identity_spam_trap", False, "身份信誉窗口已触发发送限制", 5),
        ("identity_blocklist", False, "身份信誉窗口已触发发送限制", 5),
        ("domain_spam_trap", False, "域名信誉窗口已触发发送限制", 5),
        ("domain_blocklist", False, "域名信誉窗口已触发发送限制", 5),
    ],
)
async def test_check_send_permission_gate_matrix_has_fixed_safe_reasons(
    scenario: str,
    allowed: bool,
    reason: str | None,
    daily_limit: int,
) -> None:
    """删掉任一 state/auth/date/cap/window gate 都会改变独立写死矩阵。"""
    service, factory, audit, _ = _build()
    if scenario in {"created", "auth_pending", "retired"}:
        state = {
            "created": IdentityState.CREATED,
            "auth_pending": IdentityState.AUTH_PENDING,
            "retired": IdentityState.RETIRED,
        }[scenario]
        identity = _seed(factory, state=state)
    else:
        identity = _seed_sendable(
            factory,
            state={
                "throttled": IdentityState.THROTTLED,
                "suspended": IdentityState.SUSPENDED,
                "active": IdentityState.ACTIVE,
            }.get(scenario, IdentityState.WARMING),
            started_on=(
                _NOW.date() + timedelta(days=1)
                if scenario == "before_start"
                else _NOW.date() - timedelta(days=27)
                if scenario == "warming_day_28"
                else _NOW.date() - timedelta(days=28)
                if scenario == "active"
                else _NOW.date()
            ),
            target=100 if scenario in {"warming_day_28", "active"} else 5,
            auth_passed=scenario != "latest_auth_failed",
        )
    if scenario == "cap":
        factory.counter_values[(str(identity.identity_id), _NOW.date())] = 5
    if scenario == "identity_below_minimum":
        factory.identity_windows[str(identity.identity_id)] = ReputationWindow(
            7, _NOW, 49, 0, 49, 0, 0, 0
        )
    if scenario == "domain_below_minimum":
        factory.domain_windows[identity.domain] = ReputationWindow(
            7, _NOW, 49, 0, 49, 0, 0, 0
        )
    if scenario == "identity_rate":
        factory.identity_windows[str(identity.identity_id)] = ReputationWindow(
            7, _NOW, 50, 48, 2, 0, 0, 0
        )
    if scenario == "domain_rate":
        factory.domain_windows[identity.domain] = ReputationWindow(
            7, _NOW, 50, 49, 0, 0, 1, 0
        )
    if scenario == "identity_spam_trap":
        factory.identity_windows[str(identity.identity_id)] = ReputationWindow(
            7, _NOW, 0, 0, 0, 0, 0, 0, spam_trap_hits=1
        )
    if scenario == "identity_blocklist":
        factory.identity_windows[str(identity.identity_id)] = ReputationWindow(
            7, _NOW, 0, 0, 0, 0, 0, 0, blocklist_hits=1
        )
    if scenario == "domain_spam_trap":
        factory.domain_windows[identity.domain] = ReputationWindow(
            7, _NOW, 0, 0, 0, 0, 0, 0, spam_trap_hits=1
        )
    if scenario == "domain_blocklist":
        factory.domain_windows[identity.domain] = ReputationWindow(
            7, _NOW, 0, 0, 0, 0, 0, 0, blocklist_hits=1
        )

    permission = await service.check_send_permission(
        _TENANT, identity.identity_id, True, actor=_boss()
    )

    assert (permission.allowed, permission.reason, permission.daily_limit) == (
        allowed,
        reason,
        daily_limit,
    )
    assert permission.remaining_today == (daily_limit if allowed else 0)
    warmup_boundaries = {
        "before_start": (0, True),
        "warming_day_1": (1, True),
        "warming_day_28": (28, True),
        "active": (29, False),
    }
    if scenario in warmup_boundaries:
        assert (permission.warmup_day, permission.is_warming) == warmup_boundaries[
            scenario
        ]
    if reason is not None:
        assert identity.address not in reason
        assert identity.domain not in reason
    assert len(audit.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    [DomainRole.PRIMARY_BUSINESS, DomainRole.TRANSACTIONAL],
)
async def test_cold_outreach_role_is_never_bypassed(role: DomainRole) -> None:
    """非冷开发域即使状态、认证、额度全合格也必须抛 typed role error。"""
    service, factory, audit, _ = _build()
    identity = _seed_sendable(factory, role=role)
    with pytest.raises(ColdOutreachDomainViolation) as caught:
        await service.check_send_permission(
            _TENANT, identity.identity_id, True, actor=_boss()
        )
    assert identity.address not in str(caught.value)
    assert identity.domain not in str(caught.value)
    assert audit.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role",
    [DomainRole.PRIMARY_BUSINESS, DomainRole.TRANSACTIONAL],
)
async def test_reserve_never_bypasses_cold_outreach_role(role: DomainRole) -> None:
    """authoritative reserve 必须独立执行 cold-role gate。"""
    service, factory, audit, _ = _build()
    identity = _seed_sendable(factory, role=role)
    key = IdempotencyKey("reserve-role-denial")
    with pytest.raises(ColdOutreachDomainViolation) as caught:
        await service.reserve_send_slot(
            _TENANT,
            identity.identity_id,
            key,
            True,
            actor=_system(identity.identity_id),
        )
    assert all(
        value not in str(caught.value)
        for value in (identity.address, identity.domain, str(key))
    )
    assert factory.counter_values == {}
    assert factory.reservation_values == {}
    assert audit.records == []


@pytest.mark.asyncio
async def test_send_permission_enforces_resource_scope_before_returning_diagnostics() -> None:
    """SYSTEM scope 不含目标 identity 时不得泄漏 gate 结果。"""
    service, factory, audit, _ = _build(
        authorizer=Phase1SendingIdentityAuthorizer(_TENANT)
    )
    identity = _seed_sendable(factory)
    other = SendingIdentityId("sid_01K27XZA00ABCDEFGHJKMNPQRT")
    with pytest.raises(PermissionDenied):
        await service.check_send_permission(
            _TENANT, identity.identity_id, True, actor=_system(other)
        )
    assert audit.records == [
        {
            "actor": "system_1",
            "action": SendingIdentityAction.SEND_PERMISSION_READ.value,
            "tenant_id": _TENANT,
            "scope": ScopeLevel.SYSTEM.value,
            "rule": "deny:authorization",
        }
    ]


@pytest.mark.asyncio
async def test_reserve_scope_denial_emits_one_deny_and_writes_nothing() -> None:
    """reserve 的 resource ABAC 失败恰一 deny，零 allow/业务写。"""
    service, factory, audit, _ = _build(
        authorizer=Phase1SendingIdentityAuthorizer(_TENANT)
    )
    identity = _seed_sendable(factory)
    other = SendingIdentityId("sid_01K27XZA00ABCDEFGHJKMNPQRT")
    with pytest.raises(PermissionDenied):
        await service.reserve_send_slot(
            _TENANT,
            identity.identity_id,
            IdempotencyKey("reserve-scope-denial"),
            True,
            actor=_system(other),
        )
    assert factory.counter_values == {}
    assert factory.reservation_values == {}
    assert audit.records == [
        {
            "actor": "system_1",
            "action": SendingIdentityAction.SEND_SLOT_RESERVE.value,
            "tenant_id": _TENANT,
            "scope": ScopeLevel.SYSTEM.value,
            "rule": "deny:authorization",
        }
    ]


@pytest.mark.asyncio
async def test_reservation_is_authoritative_idempotent_and_preserves_original_snapshot() -> None:
    """同 key 在后来计数变化后仍返回原快照；不同 key 精确填满 cap。"""
    service, factory, audit, order = _build()
    identity = _seed_sendable(factory, target=100)
    system = _system(identity.identity_id)

    diagnostic = await service.check_send_permission(
        _TENANT, identity.identity_id, True, actor=_boss()
    )
    assert diagnostic.allowed is True
    first = await service.reserve_send_slot(
        _TENANT,
        identity.identity_id,
        IdempotencyKey("  reservation-one  "),
        True,
        actor=system,
    )
    assert (
        first.reservation_key,
        first.on_day,
        first.sequence,
        first.daily_limit,
        first.remaining_today,
    ) == ("reservation-one", _NOW.date(), 1, 5, 4)
    for index in range(2, 6):
        await service.reserve_send_slot(
            _TENANT,
            identity.identity_id,
            IdempotencyKey(f"reservation-{index}"),
            True,
            actor=system,
        )
    retried = await service.reserve_send_slot(
        _TENANT,
        identity.identity_id,
        IdempotencyKey("reservation-one"),
        True,
        actor=system,
    )
    assert retried == first
    future_service = _service_class()(
        factory,
        _Authorizer(order),
        audit,
        now=lambda: _NOW + timedelta(days=3),
    )
    assert (
        await future_service.reserve_send_slot(
            _TENANT,
            identity.identity_id,
            IdempotencyKey("reservation-one"),
            True,
            actor=system,
        )
    ) == first
    with pytest.raises(WarmupLimitExceededError):
        await service.reserve_send_slot(
            _TENANT,
            identity.identity_id,
            IdempotencyKey("reservation-over-cap"),
            True,
            actor=system,
        )
    assert factory.counter_values[(str(identity.identity_id), _NOW.date())] == 5
    assert len(factory.reservation_values) == 5
    assert len(audit.records) == 8  # diagnostic + 5 creates + two idempotent retries
    assert factory.actions.records == []
    assert factory.bus.events == []
    assert order.index("domain:ensure") < order.index("identity:lock")
    assert order.index("identity:lock") < order.index("reservation:reserve")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_key", "expected"),
    [
        ("k", "k"),
        ("k" * 65, "k" * 65),
        ("k" * 200, "k" * 200),
        ("  " + "z" * 200 + "  ", "z" * 200),
    ],
)
async def test_reservation_key_accepts_full_post_strip_length_contract(
    raw_key: str,
    expected: str,
) -> None:
    """1/65/200 与 strip 后 200 均必须成功，不得回归 64 字符限制。"""
    service, factory, audit, _ = _build()
    identity = _seed_sendable(factory, target=100)
    reservation = await service.reserve_send_slot(
        _TENANT,
        identity.identity_id,
        IdempotencyKey(raw_key),
        True,
        actor=_system(identity.identity_id),
    )
    assert reservation.reservation_key == expected
    assert len(factory.reservation_values) == 1
    assert len(audit.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_key",
    [
        "",
        "   ",
        "x" * 201,
        "line\nbreak",
        "control\x00value",
        "control\x1fvalue",
        "control\x7fvalue",
        "control\x85value",
        "control\tvalue",
        "BearerValue",
        "api_TOKEN_value",
        "client-secret-value",
        "pass" + "word=value",
    ],
)
async def test_reservation_key_validation_is_fixed_and_does_not_echo_input(
    raw_key: str,
) -> None:
    """删除 strip/长度/control/secret-like 任一规则都必须使本矩阵变红。"""
    service, factory, audit, _ = _build()
    identity = _seed_sendable(factory)
    with pytest.raises(ValidationError) as caught:
        await service.reserve_send_slot(
            _TENANT,
            identity.identity_id,
            IdempotencyKey(raw_key),
            True,
            actor=_system(identity.identity_id),
        )
    assert str(caught.value) == "发送预留幂等键无效"
    if raw_key:
        assert raw_key not in str(caught.value)
    assert audit.records == []


@pytest.mark.asyncio
async def test_reservation_rechecks_capacity_after_a_stale_diagnostic() -> None:
    """check 的允许结果不能被 reserve 当作授权缓存。"""
    service, factory, audit, _ = _build()
    identity = _seed_sendable(factory)
    assert (
        await service.check_send_permission(
            _TENANT, identity.identity_id, True, actor=_boss()
        )
    ).allowed
    factory.counter_values[(str(identity.identity_id), _NOW.date())] = 5
    with pytest.raises(WarmupLimitExceededError):
        await service.reserve_send_slot(
            _TENANT,
            identity.identity_id,
            IdempotencyKey("stale-check"),
            True,
            actor=_system(identity.identity_id),
        )
    assert len(audit.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "error_type"),
    [
        ("created", AuthenticationNotVerifiedError),
        ("auth_pending", AuthenticationNotVerifiedError),
        ("latest_auth_failed", AuthenticationNotVerifiedError),
        ("before_start", WarmupLimitExceededError),
        ("active_incomplete", WarmupLimitExceededError),
        ("throttled", WarmupLimitExceededError),
        ("cap", WarmupLimitExceededError),
        ("identity_reputation", WarmupLimitExceededError),
        ("domain_reputation", WarmupLimitExceededError),
        ("suspended", IdentitySuspendedError),
        ("retired", IdentityRetiredError),
    ],
)
async def test_reserve_maps_denied_states_to_frozen_typed_errors(
    scenario: str,
    error_type: type[Exception],
) -> None:
    """reserve 的独立 gate 矩阵固定 typed error 且失败零写。"""
    service, factory, audit, _ = _build()
    if scenario in {"created", "auth_pending", "retired"}:
        identity = _seed(
            factory,
            state={
                "created": IdentityState.CREATED,
                "auth_pending": IdentityState.AUTH_PENDING,
                "retired": IdentityState.RETIRED,
            }[scenario],
        )
    else:
        identity = _seed_sendable(
            factory,
            state={
                "active_incomplete": IdentityState.ACTIVE,
                "throttled": IdentityState.THROTTLED,
                "suspended": IdentityState.SUSPENDED,
            }.get(scenario, IdentityState.WARMING),
            started_on=(
                _NOW.date() + timedelta(days=1)
                if scenario == "before_start"
                else _NOW.date()
            ),
            auth_passed=scenario != "latest_auth_failed",
        )
    if scenario == "cap":
        factory.counter_values[(str(identity.identity_id), _NOW.date())] = 5
    if scenario == "identity_reputation":
        factory.identity_windows[str(identity.identity_id)] = ReputationWindow(
            7, _NOW, 50, 48, 2, 0, 0, 0
        )
    if scenario == "domain_reputation":
        factory.domain_windows[identity.domain] = ReputationWindow(
            7, _NOW, 50, 49, 0, 0, 1, 0
        )
    key = IdempotencyKey(f"typed-{scenario}")
    counters_before = dict(factory.counter_values)
    reservations_before = dict(factory.reservation_values)
    with pytest.raises(error_type) as caught:
        await service.reserve_send_slot(
            _TENANT,
            identity.identity_id,
            key,
            True,
            actor=_system(identity.identity_id),
        )
    assert type(caught.value) is error_type
    assert all(
        value not in str(caught.value)
        for value in (identity.address, identity.domain, str(key))
    )
    assert factory.counter_values == counters_before
    assert factory.reservation_values == reservations_before
    assert audit.records == []


@pytest.mark.asyncio
async def test_reservation_propagates_serialization_deadlock_sentinel_unchanged() -> None:
    """operational DB sentinel 必须保留原对象，不能被转换为业务 cap。"""
    service, factory, audit, _ = _build()
    identity = _seed_sendable(factory)
    sentinel = RuntimeError("private serialization/deadlock sentinel")
    factory.reservation_error = sentinel
    with pytest.raises(RuntimeError) as caught:
        await service.reserve_send_slot(
            _TENANT,
            identity.identity_id,
            IdempotencyKey("operational-failure"),
            True,
            actor=_system(identity.identity_id),
        )
    assert caught.value is sentinel
    assert not isinstance(caught.value, WarmupLimitExceededError)
    assert factory.counter_values == {}
    assert factory.reservation_values == {}
    assert audit.records == []


@pytest.mark.asyncio
async def test_reservation_commit_and_corruption_failures_have_zero_allow() -> None:
    """commit 与 tenant/domain corruption 均不能留下 allow 或调用 reservation。"""
    service, factory, audit, _ = _build()
    identity = _seed_sendable(factory)
    factory.commit_error = RuntimeError("private commit failure")
    with pytest.raises(RuntimeError, match="private commit failure"):
        await service.reserve_send_slot(
            _TENANT,
            identity.identity_id,
            IdempotencyKey("commit-failure"),
            True,
            actor=_system(identity.identity_id),
        )
    assert audit.records == []

    service, factory, audit, order = _build()
    identity = _seed_sendable(factory)
    factory.domains[identity.domain] = SendingDomain(
        TenantId("tCorrupted"), identity.domain, identity.role, identity.created_at
    )
    with pytest.raises(TenantIsolationViolation):
        await service.reserve_send_slot(
            _TENANT,
            identity.identity_id,
            IdempotencyKey("domain-corruption"),
            True,
            actor=_system(identity.identity_id),
        )
    assert not any(str(record["rule"]).startswith("allow") for record in audit.records)
    assert "reservation:reserve" not in order

    service, factory, audit, order = _build()
    identity = _seed_sendable(factory)
    identity.tenant_id = TenantId("tCorrupted")
    with pytest.raises(TenantIsolationViolation):
        await service.reserve_send_slot(
            _TENANT,
            identity.identity_id,
            IdempotencyKey("identity-corruption"),
            True,
            actor=_system(identity.identity_id),
        )
    assert not any(str(record["rule"]).startswith("allow") for record in audit.records)
    assert "reservation:reserve" not in order


@pytest.mark.asyncio
async def test_delivery_event_tenant_mismatch_is_critical_and_never_appended(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """event 自报其他 tenant 属安全违规，必须在 append 前固定告警并零 allow。"""
    service, factory, audit, _ = _build()
    identity = _seed_sendable(factory)
    event = DeliveryEventRecord(
        tenant_id=TenantId("tOther"),
        identity_id=identity.identity_id,
        event_type=DeliveryEventType.DELIVERED,
        occurred_at=_NOW,
        dedup_key=IdempotencyKey("provider:tenant-mismatch"),
        source_ref="provider-event-tenant",
    )
    with (
        caplog.at_level("CRITICAL", logger="security.tenant_isolation"),
        pytest.raises(TenantIsolationViolation),
    ):
        await service.record_delivery_event(
            _TENANT,
            identity.identity_id,
            event,
            actor=_system(identity.identity_id),
        )
    assert [record.getMessage() for record in caplog.records] == [
        "检测到跨租户数据隔离违规"
    ]
    assert factory.delivery_events == []
    assert factory.actions.records == []
    assert factory.bus.events == []
    assert audit.records == [
        {
            "actor": "system_1",
            "action": SendingIdentityAction.DELIVERY_EVENT_RECORD.value,
            "tenant_id": _TENANT,
            "scope": ScopeLevel.SYSTEM.value,
            "rule": "deny:tenant_isolation",
        }
    ]


@pytest.mark.asyncio
async def test_delivery_event_identity_mismatch_is_fixed_and_never_appended() -> None:
    """参数 identity 与 event identity 不一致不得借错误文本泄漏任一标识。"""
    service, factory, audit, _ = _build()
    identity = _seed_sendable(factory)
    event = DeliveryEventRecord(
        tenant_id=_TENANT,
        identity_id=SendingIdentityId("sid_01K27XZA00ABCDEFGHJKMNPQRT"),
        event_type=DeliveryEventType.DELIVERED,
        occurred_at=_NOW,
        dedup_key=IdempotencyKey("provider:identity-mismatch"),
        source_ref="provider-event-identity",
    )
    with pytest.raises(InvalidDeliveryEventError) as caught:
        await service.record_delivery_event(
            _TENANT,
            identity.identity_id,
            event,
            actor=_system(identity.identity_id),
        )
    assert str(caught.value) == "投递事件无效"
    assert factory.delivery_events == []
    assert factory.actions.records == []
    assert factory.bus.events == []
    assert audit.records == []


def test_decimal_reputation_fixture_stays_exact() -> None:
    """service fake 的信誉窗口仍以 Decimal 派生，不向 float 漂移。"""
    window = ReputationWindow(7, _NOW, 100, 95, 3, 2, 1, 0)
    assert window.hard_bounce_rate == Decimal("0.03")
