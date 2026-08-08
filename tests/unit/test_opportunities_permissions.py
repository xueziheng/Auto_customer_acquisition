"""S3-5 opportunities 全服务授权单测（typed 契约 + 服务集成）。

契约（``domains/opportunities/permissions``）：
- typed ``OpportunityAction`` / ``ScopeLevel`` / ``OpportunityScope`` / ``Actor``。
- ``OpportunityScope`` 是可扩展的 ABAC 作用域：不可变（frozen dataclass + frozenset）、
  无最高权限默认（``level=None`` = 无授权）、为 S3-13 的 SQL ABAC 留出
  ``allowed_owners`` / ``allowed_countries`` / ``allowed_categories`` 维度限制。
- ``OpportunityAuthorizer`` Protocol：``require(actor, action, scope, tenant_id)``；
  ``DefaultDenyAuthorizer`` 未知 action / 未知 scope / 一切未放行动作一律默认拒绝。
- ``AuditLogger`` / ``StandardAuditLogger``：仅 actor/action/tenant_id/scope/rule，
  无敏感值与业务 payload。

服务集成（``domains/opportunities.service_impl``）：
- 每个公开读写方法都带显式 ``actor: Actor`` 并经 ``authorizer.require``；
  判权失败（``PermissionDenied``）发生在任何仓储读取 / 业务副作用之前（不创建 UoW）。
- 允许与拒绝都写授权审计（拒绝 ``rule="deny"``）。
- 查询 ABAC：``list_for_employee`` 按 ``scope.allowed_owners`` 判权——
  ``None`` 放行；限制集合不含目标 owner 即拒绝（fail closed）。
- 不 import 其他域（employees 等）；worker/system 用显式最小 actor/scope，无旁路。

RED：``domains.opportunities.permissions`` 尚未创建 → import 即 ``ModuleNotFoundError``。
"""
from __future__ import annotations

import importlib
import inspect
import logging
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Self

import pytest

from domains.opportunities import models
from domains.opportunities.schemas import HandoffCreateRequest, OpportunityCreateRequest
from shared.errors import PermissionDenied
from shared.schemas.identifiers import (
    EmployeeId,
    HandoffId,
    OpportunityId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.provenance import Provenance, SourceType

OpportunityState = models.OpportunityState
LossReason = models.LossReason
HandoffPolicy = models.HandoffPolicy

_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
_USD = CurrencyCode("USD")

_MODULE_BY_SYMBOL = {
    "Actor": "domains.opportunities.permissions",
    "AuditLogger": "domains.opportunities.permissions",
    "OpportunityAction": "domains.opportunities.permissions",
    "OpportunityAuthorizer": "domains.opportunities.permissions",
    "OpportunityScope": "domains.opportunities.permissions",
    "ScopeLevel": "domains.opportunities.permissions",
    "StandardAuditLogger": "domains.opportunities.permissions",
    "DefaultDenyAuthorizer": "domains.opportunities.permissions",
    "OpportunityServiceImpl": "domains.opportunities.service_impl",
}


def _load(symbol: str):
    """按模块字符串导入符号；缺失转行为失败（RED 阶段 permissions 未创建）。"""
    try:
        return getattr(importlib.import_module(_MODULE_BY_SYMBOL[symbol]), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{symbol} 尚未定义（{exc}）")


Actor = _load("Actor")
AuditLogger = _load("AuditLogger")
OpportunityAction = _load("OpportunityAction")
OpportunityAuthorizer = _load("OpportunityAuthorizer")
OpportunityScope = _load("OpportunityScope")
ScopeLevel = _load("ScopeLevel")
StandardAuditLogger = _load("StandardAuditLogger")
DefaultDenyAuthorizer = _load("DefaultDenyAuthorizer")
OpportunityServiceImpl = _load("OpportunityServiceImpl")


def _prov() -> Provenance:
    return Provenance(
        source_type=SourceType.CONVERSATION,
        source_id="m1",
        extracted_by="model_v3",
        extracted_at=_NOW,
    )


def _create_request() -> OpportunityCreateRequest:
    return OpportunityCreateRequest(
        need_id="need-1",
        account_id="acc-1",
        account_name="Acme",
        country="US",
        product_category="hinges",
        evidence_tier="high",
        has_verified_contact=True,
        category_allowed=True,
        minimum_order_value=Money(Decimal(100), _USD),
        field_provenance={"account_name": _prov(), "country": _prov()},
    )


def _handoff_request() -> HandoffCreateRequest:
    return HandoffCreateRequest(
        opportunity_id="opp-1",
        trigger="quote_requested",
        account_name="Acme",
        country="US",
        why_valuable="扩建第二座工厂",
        customer_verbatim="we need hinges",
        customer_verbatim_provenance=_prov(),
    )


def _actor(actor_id: str = "e1", **scope_overrides) -> Actor:
    return Actor(
        actor_id=actor_id,
        scope=OpportunityScope(level=ScopeLevel.TENANT, **scope_overrides),
    )


# --- 契约：Action / Scope / Actor ---------------------------------------------


def test_opportunity_action_typed() -> None:
    """typed action：新增操作必须登记，否则默认拒绝；值形如 ``entity:verb``。"""
    values = {a.value for a in OpportunityAction}
    assert "opportunity:create" in values
    assert "opportunity:assign" in values
    assert "opportunity:transition" in values
    assert "opportunity:mark_lost" in values
    assert "opportunity:mark_won" in values
    assert "opportunity:read" in values
    assert "opportunity:list" in values
    assert "handoff:request" in values
    assert "handoff:accept" in values
    assert "handoff:read" in values
    assert "handoff:queue_read" in values
    assert "loss_reason:read" in values
    assert all(":" in v for v in values)


def test_scope_level_typed() -> None:
    """作用域级别：system/self/manager/tenant。"""
    assert {s.value for s in ScopeLevel} == {"system", "self", "manager", "tenant"}


def test_scope_immutable_no_mutable_defaults() -> None:
    """ABAC scope 不可变、无最高权限默认、无可变默认值。"""
    scope = OpportunityScope(
        level=ScopeLevel.SELF, allowed_owners=frozenset({EmployeeId("e1")})
    )
    assert scope.level == ScopeLevel.SELF
    assert scope.allowed_owners == frozenset({EmployeeId("e1")})
    with pytest.raises(AttributeError):  # frozen dataclass 拒绝赋值
        scope.allowed_owners = frozenset({EmployeeId("e2")})  # type: ignore[misc]
    default = OpportunityScope()
    assert default.level is None  # 无授权默认（fail closed，不默认成最高权限）
    assert default.allowed_owners is None
    assert default.allowed_countries is None
    assert default.allowed_categories is None


def test_scope_exposes_abac_dimensions() -> None:
    """ABAC 三个维度可表达（S3-13 SQL ABAC 挂载点），``None``=不限、空集=拒绝。"""
    scope = OpportunityScope(
        level=ScopeLevel.SELF,
        allowed_owners=frozenset({EmployeeId("e1")}),
        allowed_countries=frozenset({"US"}),
        allowed_categories=frozenset({"hinges"}),
    )
    assert scope.allowed_owners == frozenset({EmployeeId("e1")})
    assert scope.allowed_countries == frozenset({"US"})
    assert scope.allowed_categories == frozenset({"hinges"})
    deny_all = OpportunityScope(level=ScopeLevel.SELF, allowed_owners=frozenset())
    assert deny_all.allowed_owners == frozenset()  # 空集 = 该维度任何值都不授权


def test_actor_requires_explicit_scope() -> None:
    """Actor.scope 无默认值：必须显式传，避免默认成最高权限。"""
    with pytest.raises(TypeError):
        Actor(actor_id="e1")  # type: ignore[call-arg]


def test_scope_label_for_audit() -> None:
    """审计用的 scope 标签：级别名；无级别 = unprivileged。"""
    assert OpportunityScope(level=ScopeLevel.TENANT).label == "tenant"
    assert OpportunityScope().label == "unprivileged"


# --- 契约：Authorizer Protocol + 默认拒绝 -------------------------------------


def test_authorizer_protocol_signature() -> None:
    """OpportunityAuthorizer.require(actor, action, scope, tenant_id) 契约形状。"""
    params = list(inspect.signature(OpportunityAuthorizer.require).parameters)
    assert params[1:] == ["actor", "action", "scope", "tenant_id"]  # self 之后四个


def test_default_deny_unknown_action() -> None:
    """未知 action（非 OpportunityAction）一律默认拒绝。"""
    actor = _actor()
    with pytest.raises(PermissionDenied):
        DefaultDenyAuthorizer().require(
            actor, "opportunity:brand_new", actor.scope, TenantId("t1")  # type: ignore[arg-type]
        )


def test_default_deny_rejects_all_known_actions() -> None:
    """默认拒绝基座：没有放行规则，已知 action 也一律拒绝（fail closed）。"""
    auth = DefaultDenyAuthorizer()
    actor = _actor()
    for action in OpportunityAction:
        with pytest.raises(PermissionDenied):
            auth.require(actor, action, actor.scope, TenantId("t1"))


# --- 契约：AuditLogger ---------------------------------------------------------


def test_audit_logger_protocol_signature() -> None:
    """授权审计仅 actor/action/tenant_id/scope/rule，全部 keyword-only。"""
    params = inspect.signature(AuditLogger.log).parameters
    assert {"actor", "action", "tenant_id", "scope", "rule"} <= set(params)
    for name in ("actor", "action", "tenant_id", "scope", "rule"):
        assert params[name].kind == inspect.Parameter.KEYWORD_ONLY


def test_standard_audit_logger_records_fields(caplog) -> None:
    """StandardAuditLogger 结构化记录 5 字段；消息体不含业务内容。"""
    logger = StandardAuditLogger(logger_name="security.authorization.test")
    with caplog.at_level(logging.INFO, logger="security.authorization.test"):
        logger.log(
            actor="e1",
            action="opportunity:read",
            tenant_id=TenantId("t1"),
            scope="tenant",
            rule="test:allow",
        )
    records = [r for r in caplog.records if r.name == "security.authorization.test"]
    assert records
    rec = records[-1]
    assert rec.__dict__["actor"] == "e1"
    assert rec.__dict__["action"] == "opportunity:read"
    assert rec.__dict__["tenant_id"] == "t1"
    assert rec.__dict__["scope"] == "tenant"
    assert rec.__dict__["rule"] == "test:allow"
    assert rec.getMessage() == "authorization"  # 消息体无任何业务 payload


# --- 服务集成：全方法带 actor + 判权先于仓储/副作用 ----------------------------

_ACTION_BY_METHOD = {
    "create_from_need": OpportunityAction.OPPORTUNITY_CREATE.value,
    "assign": OpportunityAction.OPPORTUNITY_ASSIGN.value,
    "transition": OpportunityAction.OPPORTUNITY_TRANSITION.value,
    "mark_lost": OpportunityAction.OPPORTUNITY_MARK_LOST.value,
    "mark_won": OpportunityAction.OPPORTUNITY_MARK_WON.value,
    "request_handoff": OpportunityAction.HANDOFF_REQUEST.value,
    "accept_handoff": OpportunityAction.HANDOFF_ACCEPT.value,
    "get_handoff_packet": OpportunityAction.HANDOFF_READ.value,
    "get_queue_stats": OpportunityAction.HANDOFF_QUEUE_READ.value,
    "get": OpportunityAction.OPPORTUNITY_READ.value,
    "list_for_employee": OpportunityAction.OPPORTUNITY_LIST.value,
    "loss_reason_breakdown": OpportunityAction.LOSS_REASON_READ.value,
}


class _DenyAuthorizer:
    """判权一律拒绝；模拟未放行 / 越权调用。"""

    def require(self, actor, action, scope, tenant_id) -> str:
        raise PermissionDenied(f"deny:{action.value}")


class _AllowAuthorizer:
    """判权一律放行并记录调用（recorded action 供精确断言）。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def require(self, actor, action, scope, tenant_id) -> str:
        self.calls.append((actor, action, scope, tenant_id))
        return "test:allow"


class _RecordingAudit:
    """记录审计条目（actor/action/tenant_id/scope/rule）。"""

    def __init__(self) -> None:
        self.entries: list[dict[str, str]] = []

    def log(self, *, actor, action, tenant_id, scope, rule) -> None:
        self.entries.append(
            {
                "actor": actor,
                "action": action,
                "tenant_id": str(tenant_id),
                "scope": scope,
                "rule": rule,
            }
        )


class _NeverScorer:
    async def score(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("判权必须在打分前")


class _NeverUoWFactory:
    def __call__(self) -> object:
        raise AssertionError("authorizer 判权必须在进入 UoW 前；不应创建 UoW")


def _make_service(*, authorizer, audit, factory=None):
    return OpportunityServiceImpl(
        factory if factory is not None else _NeverUoWFactory(),
        _NeverScorer(),
        HandoffPolicy(sla_seconds=3600, backlog_threshold=40),
        authorizer=authorizer,
        audit=audit,
        now=lambda: _NOW,
    )


@pytest.mark.parametrize(
    "method_name, invoke",
    [
        ("create_from_need", lambda s: s.create_from_need(TenantId("t1"), _create_request(), actor=_actor())),
        ("assign", lambda s: s.assign(TenantId("t1"), OpportunityId("opp-1"), EmployeeId("emp-1"), EmployeeId("mgr-1"), actor=_actor())),
        ("transition", lambda s: s.transition(TenantId("t1"), OpportunityId("opp-1"), OpportunityState.ASSIGNED, actor=_actor())),
        ("mark_lost", lambda s: s.mark_lost(TenantId("t1"), OpportunityId("opp-1"), LossReason.PRICE_TOO_HIGH, actor=_actor(), confirmed_by=EmployeeId("e1"), confirmed_at=_NOW)),
        ("mark_won", lambda s: s.mark_won(TenantId("t1"), OpportunityId("opp-1"), actor=_actor(), confirmed_by=EmployeeId("e1"), confirmed_at=_NOW)),
        ("request_handoff", lambda s: s.request_handoff(TenantId("t1"), _handoff_request(), actor=_actor())),
        ("accept_handoff", lambda s: s.accept_handoff(TenantId("t1"), HandoffId("ho-1"), EmployeeId("e1"), actor=_actor())),
        ("get_handoff_packet", lambda s: s.get_handoff_packet(TenantId("t1"), HandoffId("ho-1"), actor=_actor())),
        ("get_queue_stats", lambda s: s.get_queue_stats(TenantId("t1"), actor=_actor())),
        ("get", lambda s: s.get(TenantId("t1"), OpportunityId("opp-1"), actor=_actor())),
        ("list_for_employee", lambda s: s.list_for_employee(TenantId("t1"), EmployeeId("e1"), actor=_actor())),
        ("loss_reason_breakdown", lambda s: s.loss_reason_breakdown(TenantId("t1"), actor=_actor())),
    ],
)
async def test_deny_before_any_uow(method_name: str, invoke) -> None:
    """判权拒绝发生在任何仓储读取/副作用之前：不创建 UoW，且拒绝写审计。"""
    audit = _RecordingAudit()
    service = _make_service(authorizer=_DenyAuthorizer(), audit=audit)
    with pytest.raises(PermissionDenied):
        await invoke(service)
    assert audit.entries, "拒绝必须写授权审计"
    assert audit.entries[-1]["rule"] == "deny"
    assert audit.entries[-1]["action"] == _ACTION_BY_METHOD[method_name]


async def test_allow_logs_rule_and_passes() -> None:
    """放行路径：authorizer 被调用并返回 rule；允许也写审计（rule=返回值）。"""
    factory = _FakeUoWFactory()
    authorizer = _AllowAuthorizer()
    audit = _RecordingAudit()
    service = _make_service(authorizer=authorizer, audit=audit, factory=factory)

    result = await service.loss_reason_breakdown(TenantId("t1"), actor=_actor())

    assert result == {}
    assert authorizer.calls[0][1] == OpportunityAction.LOSS_REASON_READ
    assert audit.entries[-1]["rule"] == "test:allow"
    assert audit.entries[-1]["action"] == "loss_reason:read"


# --- 查询 ABAC scope 判权 -------------------------------------------------------


async def test_list_for_employee_abac_owner_denied() -> None:
    """查询 ABAC：scope.allowed_owners 不含目标 owner → 拒绝，且不查仓储。"""
    audit = _RecordingAudit()
    service = _make_service(authorizer=_AllowAuthorizer(), audit=audit)
    actor = Actor(
        actor_id="e1",
        scope=OpportunityScope(
            level=ScopeLevel.SELF, allowed_owners=frozenset({EmployeeId("e2")})
        ),
    )
    with pytest.raises(PermissionDenied):
        await service.list_for_employee(TenantId("t1"), EmployeeId("e1"), actor=actor)
    assert audit.entries[-1]["rule"] == "deny:abac:owner"
    assert audit.entries[-1]["action"] == "opportunity:list"


async def test_list_for_employee_abac_unrestricted_allowed() -> None:
    """查询 ABAC：scope 未限制 owner（None）→ 放行并进入仓储（tenant 原样传）。"""
    factory = _FakeUoWFactory()
    authorizer = _AllowAuthorizer()
    audit = _RecordingAudit()
    service = _make_service(authorizer=authorizer, audit=audit, factory=factory)
    actor = Actor(actor_id="m1", scope=OpportunityScope(level=ScopeLevel.MANAGER))

    result = await service.list_for_employee(
        TenantId("t1"), EmployeeId("e1"), actor=actor, limit=10
    )

    assert result == []
    assert authorizer.calls[0][1] == OpportunityAction.OPPORTUNITY_LIST
    assert audit.entries[-1]["rule"] == "test:allow"
    call = factory.created[0].opportunities.list_by_owner_calls[0]
    assert call[0] == TenantId("t1")
    assert call[1] == EmployeeId("e1")
    assert call[3] == 10


# --- worker/system：显式最小 actor/scope，不旁路 --------------------------------


def test_system_actor_explicit_minimal_scope() -> None:
    """worker/system 显式最小 actor/scope；默认拒绝基座不放行（fail closed）。"""
    actor = Actor(actor_id="system:outbox", scope=OpportunityScope(level=ScopeLevel.SYSTEM))
    assert actor.scope.level == ScopeLevel.SYSTEM
    assert actor.scope.allowed_owners is None  # 最小权限：无 ABAC 放宽
    with pytest.raises(PermissionDenied):
        DefaultDenyAuthorizer().require(
            actor, OpportunityAction.OPPORTUNITY_CREATE, actor.scope, TenantId("t1")
        )


# --- 独立契约：不 import 其他域 --------------------------------------------------


def test_no_cross_domain_import() -> None:
    """permissions / service_impl 独立契约：不得 import 任何其他业务域。"""
    for mod_name in (
        "domains.opportunities.permissions",
        "domains.opportunities.service_impl",
    ):
        src = inspect.getsource(importlib.import_module(mod_name))
        for line in src.splitlines():
            if "domains." in line and "domains.opportunities" not in line:
                pytest.fail(f"{mod_name} 存在跨域导入：{line.strip()}")


# --- 允许路径的最小 fake（list_for_employee / loss_reason_breakdown）-----------


class _FakeOpportunityRepo:
    def __init__(self) -> None:
        self.list_by_owner_calls: list[tuple] = []

    async def list_by_owner(self, tenant_id, owner, states, limit):
        self.list_by_owner_calls.append((tenant_id, owner, states, limit))
        return []


class _FakeLossRepo:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def count_by_reason_and_state(self, tenant_id, since_days):
        self.calls.append((tenant_id, since_days))
        return []


class _FakeUoW:
    def __init__(self) -> None:
        self.opportunities = _FakeOpportunityRepo()
        self.loss_records = _FakeLossRepo()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class _FakeUoWFactory:
    def __init__(self) -> None:
        self.created: list[_FakeUoW] = []

    def __call__(self) -> _FakeUoW:
        uow = _FakeUoW()
        self.created.append(uow)
        return uow
