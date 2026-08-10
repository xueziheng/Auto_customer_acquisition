"""发件身份生命周期 service 的 fake-UoW 行为契约。"""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from domains.sending_identity.errors import (
    AuthenticationNotVerifiedError,
    DomainRoleConflictError,
    SendingIdentityNotFoundError,
)
from domains.sending_identity.permissions import (
    Actor,
    ScopeLevel,
    SendingIdentityAction,
    SendingIdentityScope,
)
from domains.sending_identity.schemas import (
    AuthenticationFailure,
    AuthenticationResult,
    IdentityRegisterRequest,
)
from shared.errors import (
    InvalidStateTransition,
    TenantIsolationViolation,
    ValidationError,
)
from shared.schemas.identifiers import SendingIdentityId, TenantId

_models = importlib.import_module("domains.sending_identity.models")
AuthCheck = _models.AuthCheck
AuthenticationFailureCategory = _models.AuthenticationFailureCategory
AuthenticationFixInstruction = _models.AuthenticationFixInstruction
DomainRole = _models.DomainRole
IdentityState = _models.IdentityState
ReputationWindow = _models.ReputationWindow
SendingIdentity = _models.SendingIdentity
SuspensionCategory = _models.SuspensionCategory

_repository = importlib.import_module("domains.sending_identity.repository")
AuthenticationCheckRecord = _repository.AuthenticationCheckRecord
IdentityActionRecord = _repository.IdentityActionRecord
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

    def require(self, actor, action, scope, tenant_id, **kwargs):
        self.order.append(f"authorize:{action.value}")
        assert scope == actor.scope
        return f"allow:{action.value}"


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
        return self.store.get(str(identity_id))

    async def update(self, identity: SendingIdentity) -> None:
        self.order.append("identity:update")
        identity.version += 1
        self.store[str(identity.identity_id)] = identity

    async def find_by_address(self, tenant_id, address):
        return next((item for item in self.store.values() if item.address == address), None)

    async def list_domain_for_update(self, tenant_id, domain):
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
    async def get_count(self, tenant_id, identity_id, on_day):
        return 0


class _Reputation:
    async def compute_window(self, tenant_id, identity_id, window_days, computed_at):
        return ReputationWindow(window_days, computed_at, 0, 0, 0, 0, 0, 0)

    async def compute_domain_window(self, tenant_id, domain, window_days, computed_at):
        return ReputationWindow(window_days, computed_at, 0, 0, 0, 0, 0, 0)


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
        self.counters = _Counters()
        self.reputation = _Reputation()
        self.reservations = SimpleNamespace()
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
        self.commit_error: BaseException | None = None

    def __call__(self, tenant_id):
        return _Uow(self)


def _build(now: datetime = _NOW):
    order: list[str] = []
    factory = _UowFactory(order)
    audit = _Audit(order)
    service = _service_class()(
        factory,
        _Authorizer(order),
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
    assert order[0] == "authorize:identity:register"
    assert order.index("uow:commit") < order.index("audit:allow:identity:register")
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
    """外部时间不得回拨到创建前或未来五分钟外，retired 不新增 history。"""
    service, factory, audit, _ = _build()
    identity = _seed(factory)
    system = _system(identity.identity_id)
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
    assert audit.records == []
    identity.state = IdentityState.RETIRED
    with pytest.raises(InvalidStateTransition):
        await service.record_authentication_result(
            _TENANT, identity.identity_id, _auth(), actor=system
        )
    assert factory.auth_records == []


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
    assert order == ["authorize:identity:list"]
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


def test_decimal_reputation_fixture_stays_exact() -> None:
    """service fake 的信誉窗口仍以 Decimal 派生，不向 float 漂移。"""
    window = ReputationWindow(7, _NOW, 100, 95, 3, 2, 1, 0)
    assert window.hard_bounce_rate == Decimal("0.03")
