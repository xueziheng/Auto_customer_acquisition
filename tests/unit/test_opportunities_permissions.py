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
from domains.opportunities.schemas import (
    HandoffCreateRequest,
    OpportunityCreateRequest,
    ValidatedNeedEvidence,
)
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.evidence import EvidenceLevel
from shared.schemas.identifiers import (
    EmployeeId,
    HandoffId,
    OpportunityId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.provenance import Provenance, SourceType

Opportunity = models.Opportunity
OpportunityState = models.OpportunityState
LossReason = models.LossReason
HandoffPacket = models.HandoffPacket
HandoffState = models.HandoffState
HandoffTrigger = models.HandoffTrigger
ScoreSnapshot = models.ScoreSnapshot
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
    "Phase1OpportunityAuthorizer": "domains.opportunities.permissions",
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
Phase1OpportunityAuthorizer = _load("Phase1OpportunityAuthorizer")
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


def _create_evidence() -> ValidatedNeedEvidence:
    """S3-6：默认有效证据（客户明确表达 + 会话来源）。"""
    return ValidatedNeedEvidence(
        level=EvidenceLevel.CUSTOMER_INTEREST_REPLY,
        provenance=_prov(),
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
    assert "handoff:escalation_record" in values
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
    deny_all = OpportunityScope(level=ScopeLevel.MANAGER, allowed_owners=frozenset())
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


def test_phase1_authorizer_implements_public_authorizer_protocol() -> None:
    """正式 runtime authorizer 必须保持机会域公开 Protocol 形状。"""
    assert isinstance(
        Phase1OpportunityAuthorizer(TenantId("t1")),
        OpportunityAuthorizer,
    )


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
    "record_handoff_escalation": "handoff:escalation_record",
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
        ("create_from_need", lambda s: s.create_from_need(TenantId("t1"), _create_request(), _create_evidence(), actor=_actor())),
        ("assign", lambda s: s.assign(TenantId("t1"), OpportunityId("opp-1"), EmployeeId("emp-1"), EmployeeId("mgr-1"), actor=_actor())),
        ("transition", lambda s: s.transition(TenantId("t1"), OpportunityId("opp-1"), OpportunityState.ASSIGNED, actor=_actor())),
        ("mark_lost", lambda s: s.mark_lost(TenantId("t1"), OpportunityId("opp-1"), LossReason.PRICE_TOO_HIGH, actor=_actor(), confirmed_by=EmployeeId("e1"), confirmed_at=_NOW)),
        ("mark_won", lambda s: s.mark_won(TenantId("t1"), OpportunityId("opp-1"), actor=_actor(), confirmed_by=EmployeeId("e1"), confirmed_at=_NOW)),
        ("request_handoff", lambda s: s.request_handoff(TenantId("t1"), _handoff_request(), actor=_actor())),
        ("record_handoff_escalation", lambda s: s.record_handoff_escalation(TenantId("t1"), HandoffId("ho-1"), 1, _NOW, actor=_actor())),
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
    """查询 ABAC：scope.allowed_owners 不含目标 owner → 拒绝，且不查仓储。

    SELF actor 的 own-owner 限制必须含自身（构造期校验），故 actor 自身
    owner 为 e2，却查别人的机会列表 e1 → 拒绝。
    """
    audit = _RecordingAudit()
    service = _make_service(authorizer=_AllowAuthorizer(), audit=audit)
    actor = Actor(
        actor_id="e2",
        scope=OpportunityScope(
            level=ScopeLevel.SELF, allowed_owners=frozenset({EmployeeId("e2")})
        ),
    )
    with pytest.raises(PermissionDenied):
        await service.list_for_employee(TenantId("t1"), EmployeeId("e1"), actor=actor)
    assert len(audit.entries) == 1  # 拒绝路径仅一条审计
    assert audit.entries[0]["rule"] == "deny:abac:owner"
    assert audit.entries[0]["action"] == "opportunity:list"


async def test_list_for_employee_abac_unrestricted_allowed() -> None:
    """查询 ABAC：owner 维度未限制（None）→ 放行并进入仓储（tenant 原样传）。

    MANAGER 按构造校验必须带至少一个显式 ABAC 维度，此处用 country 维度
    限定；owner 维度仍为 None，故查询列表放行（country/category 过滤归
    S3-13 SQL 层）。
    """
    factory = _FakeUoWFactory()
    authorizer = _AllowAuthorizer()
    audit = _RecordingAudit()
    service = _make_service(authorizer=authorizer, audit=audit, factory=factory)
    actor = Actor(
        actor_id="m1",
        scope=OpportunityScope(
            level=ScopeLevel.MANAGER, allowed_countries=frozenset({"US"})
        ),
    )

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


# --- 修复轮2 · Important 2：list_for_employee 行级 owner/country/category ABAC ----


async def test_list_for_employee_abac_country_denied_row() -> None:
    """查询 ABAC：返回机会 country 越界（受限 country 维度）→ 拒绝，仅一条 deny。

    MANAGER 只限 allowed_countries={"US"}（owner 不限），返回行 country=CN 必须
    在 build/return 前被拦，不能只查 owner 维度就整单放行。
    """
    factory = _FakeUoWFactory()
    factory.seed_rows(
        [_opp(owner=EmployeeId("e1"), country="CN", category="hinges")]
    )
    audit = _RecordingAudit()
    service = _make_service(authorizer=_AllowAuthorizer(), audit=audit, factory=factory)
    actor = Actor(
        actor_id="m1",
        scope=OpportunityScope(
            level=ScopeLevel.MANAGER, allowed_countries=frozenset({"US"})
        ),
    )
    with pytest.raises(PermissionDenied):
        await service.list_for_employee(TenantId("t1"), EmployeeId("e1"), actor=actor)
    assert len(audit.entries) == 1  # 拒绝路径仅一条审计（无前置 allow）
    assert audit.entries[0]["rule"] == "deny:abac:country"
    assert audit.entries[0]["action"] == "opportunity:list"


async def test_list_for_employee_abac_category_denied_row() -> None:
    """查询 ABAC：SELF 受限 owner+category，返回机会 category 越界 → 拒绝。

    行 owner 匹配自身限制，但 category 越界仍必须整单拒绝（不能只查 owner）。
    """
    factory = _FakeUoWFactory()
    factory.seed_rows(
        [_opp(owner=EmployeeId("e1"), country="US", category="fasteners")]
    )
    audit = _RecordingAudit()
    service = _make_service(authorizer=_AllowAuthorizer(), audit=audit, factory=factory)
    actor = Actor(
        actor_id="e1",
        scope=OpportunityScope(
            level=ScopeLevel.SELF,
            allowed_owners=frozenset({EmployeeId("e1")}),
            allowed_categories=frozenset({"hinges"}),
        ),
    )
    with pytest.raises(PermissionDenied):
        await service.list_for_employee(TenantId("t1"), EmployeeId("e1"), actor=actor)
    assert len(audit.entries) == 1  # 拒绝路径仅一条审计
    assert audit.entries[0]["rule"] == "deny:abac:category"
    assert audit.entries[0]["action"] == "opportunity:list"


async def test_list_for_employee_abac_missing_owner_fails_closed() -> None:
    """查询 ABAC：owner 受限但返回行缺 owner（None）→ fail closed 拒绝。

    受限维度遇到缺失资源值一律拒绝，不能把 None 当"不限"放行。
    """
    factory = _FakeUoWFactory()
    factory.seed_rows(
        [_opp(owner=None, country="US", category="hinges")]
    )
    audit = _RecordingAudit()
    service = _make_service(authorizer=_AllowAuthorizer(), audit=audit, factory=factory)
    actor = Actor(
        actor_id="e1",
        scope=OpportunityScope(
            level=ScopeLevel.SELF,
            allowed_owners=frozenset({EmployeeId("e1")}),
            allowed_categories=frozenset({"hinges"}),
        ),
    )
    with pytest.raises(PermissionDenied):
        await service.list_for_employee(TenantId("t1"), EmployeeId("e1"), actor=actor)
    assert len(audit.entries) == 1  # 拒绝路径仅一条审计
    assert audit.entries[0]["rule"] == "deny:abac:owner"


async def test_list_for_employee_abac_rows_allowed_single_allow() -> None:
    """查询 ABAC：所有返回行通过三维判权 → 仅一条 allow，返回视图。

    allow 必须延迟到全部行通过后；任一越界行都不得产生 allow 审计。
    """
    factory = _FakeUoWFactory()
    factory.seed_rows(
        [
            _opp("opp-1", owner=EmployeeId("e1"), country="US", category="hinges"),
            _opp("opp-2", owner=EmployeeId("e1"), country="US", category="hinges"),
        ]
    )
    audit = _RecordingAudit()
    service = _make_service(authorizer=_AllowAuthorizer(), audit=audit, factory=factory)
    actor = Actor(
        actor_id="e1",
        scope=OpportunityScope(
            level=ScopeLevel.SELF,
            allowed_owners=frozenset({EmployeeId("e1")}),
            allowed_countries=frozenset({"US"}),
            allowed_categories=frozenset({"hinges"}),
        ),
    )
    views = await service.list_for_employee(
        TenantId("t1"), EmployeeId("e1"), actor=actor
    )
    assert [v.opportunity_id for v in views] == [
        OpportunityId("opp-1"),
        OpportunityId("opp-2"),
    ]
    assert len(audit.entries) == 1  # 全通过才写一条 allow
    assert audit.entries[0]["rule"] == "test:allow"
    assert audit.entries[0]["action"] == "opportunity:list"


# --- 修复轮1 · Important 2：SELF/MANAGER scope 构造 fail-closed -----------------


def _self_actor(actor_id: str = "e1") -> Actor:
    """SELF actor：own-owner 限制=自身（构造校验后仍合法）。"""
    return Actor(
        actor_id=actor_id,
        scope=OpportunityScope(
            level=ScopeLevel.SELF, allowed_owners=frozenset({EmployeeId(actor_id)})
        ),
    )


def _opp(
    opp_id: str = "opp-1",
    *,
    owner: EmployeeId | None = None,
    country: str = "US",
    category: str = "hinges",
) -> Opportunity:
    return Opportunity(
        opportunity_id=OpportunityId(opp_id),
        tenant_id=TenantId("t1"),
        account_id=ProspectAccountId("acc-1"),
        need_id=ValidatedNeedId("need-1"),
        product_category=category,
        created_at=_NOW,
        account_name="Acme",
        country=country,
        owner=owner,
    )


def _packet(
    handoff_id: str = "ho-1", *, opportunity_id: str = "opp-1"
) -> HandoffPacket:
    return HandoffPacket(
        handoff_id=HandoffId(handoff_id),
        tenant_id=TenantId("t1"),
        opportunity_id=OpportunityId(opportunity_id),
        trigger=HandoffTrigger.QUOTE_REQUESTED,
        requested_at=_NOW,
        account_name="Acme",
        country="US",
        why_valuable="扩建",
        customer_verbatim="we need hinges",
        state=HandoffState.REQUESTED,
    )


def test_self_scope_requires_nonempty_own_owner() -> None:
    """SELF 必须带非空 own-owner 限制；缺省/空集一律拒绝构造（fail closed）。"""
    with pytest.raises(ValidationError):
        OpportunityScope(level=ScopeLevel.SELF)
    with pytest.raises(ValidationError):
        OpportunityScope(level=ScopeLevel.SELF, allowed_owners=frozenset())


def test_manager_scope_requires_explicit_abac_dimension() -> None:
    """MANAGER 必须带至少一个显式 ABAC 维度；空集维度合法（=该维度全拒）。"""
    with pytest.raises(ValidationError):
        OpportunityScope(level=ScopeLevel.MANAGER)
    deny_all = OpportunityScope(level=ScopeLevel.MANAGER, allowed_owners=frozenset())
    assert deny_all.allowed_owners == frozenset()  # 显式空维度仍满足构造校验


def test_tenant_and_system_scope_may_be_unrestricted() -> None:
    """TENANT/SYSTEM 允许显式无限制；SYSTEM 判权仍由 authorizer 控制。"""
    assert OpportunityScope(level=ScopeLevel.TENANT).allowed_owners is None
    assert OpportunityScope(level=ScopeLevel.SYSTEM).allowed_owners is None


def test_self_actor_own_owner_must_include_actor_id() -> None:
    """SELF actor 的 own-owner 限制必须包含 actor_id 自身（构造期校验）。"""
    with pytest.raises(ValidationError):
        Actor(
            actor_id="e1",
            scope=OpportunityScope(
                level=ScopeLevel.SELF, allowed_owners=frozenset({EmployeeId("e2")})
            ),
        )
    actor = _self_actor("e1")
    assert actor.scope.allowed_owners == frozenset({EmployeeId("e1")})


# --- 修复轮2 · Important 1：SELF allowed_owners 必须精确等于自身单例 -------------


def test_self_actor_own_owner_must_be_exact_singleton() -> None:
    """SELF actor 的 allowed_owners 必须精确等于 {actor_id} 单例：self+other 拒绝。

    「只看自己的机会」语义：多带任何其他 owner（即使包含自身）也构成越权身份。
    """
    with pytest.raises(ValidationError):
        Actor(
            actor_id="e1",
            scope=OpportunityScope(
                level=ScopeLevel.SELF,
                allowed_owners=frozenset({EmployeeId("e1"), EmployeeId("e2")}),
            ),
        )


def test_self_actor_exact_singleton_accepted() -> None:
    """精确单例 {actor_id} 接受：SELF 身份只能覆盖自身一个 owner。"""
    actor = Actor(
        actor_id="e1",
        scope=OpportunityScope(
            level=ScopeLevel.SELF, allowed_owners=frozenset({EmployeeId("e1")})
        ),
    )
    assert actor.scope.allowed_owners == frozenset({EmployeeId("e1")})
    assert actor.scope.allowed_owners == frozenset({EmployeeId(actor.actor_id)})


# --- 修复轮1 · Important 1：按资源 ID 的 ABAC 覆盖（get / get_handoff_packet） ---


async def test_get_abac_owner_denied() -> None:
    """get：owner 维度受限且机会 owner 越界 → 拒绝，不返回视图。

    拒绝路径只允许**一条** deny 审计（无前置 allow 残留）。
    """
    factory = _FakeUoWFactory()
    factory.seed_opp(_opp(owner=EmployeeId("e2")))
    audit = _RecordingAudit()
    service = _make_service(authorizer=_AllowAuthorizer(), audit=audit, factory=factory)
    with pytest.raises(PermissionDenied):
        await service.get(TenantId("t1"), OpportunityId("opp-1"), actor=_self_actor("e1"))
    assert len(audit.entries) == 1  # 拒绝路径仅一条审计
    assert audit.entries[0]["rule"] == "deny:abac:owner"
    assert audit.entries[0]["action"] == "opportunity:read"


async def test_get_abac_missing_owner_fails_closed() -> None:
    """get：owner 受限但资源缺 owner（None）→ fail closed 拒绝。"""
    factory = _FakeUoWFactory()
    factory.seed_opp(_opp(owner=None))
    audit = _RecordingAudit()
    service = _make_service(authorizer=_AllowAuthorizer(), audit=audit, factory=factory)
    with pytest.raises(PermissionDenied):
        await service.get(TenantId("t1"), OpportunityId("opp-1"), actor=_self_actor("e1"))
    assert len(audit.entries) == 1  # 拒绝路径仅一条审计
    assert audit.entries[0]["rule"] == "deny:abac:owner"


async def test_get_abac_country_denied() -> None:
    """get：country 维度受限且机会 country 越界 → 拒绝。"""
    factory = _FakeUoWFactory()
    factory.seed_opp(_opp(country="CN", owner=EmployeeId("e1")))  # owner 匹配，仅 country 越界
    actor = Actor(
        actor_id="e1",
        scope=OpportunityScope(
            level=ScopeLevel.SELF,
            allowed_owners=frozenset({EmployeeId("e1")}),
            allowed_countries=frozenset({"US"}),
        ),
    )
    audit = _RecordingAudit()
    service = _make_service(authorizer=_AllowAuthorizer(), audit=audit, factory=factory)
    with pytest.raises(PermissionDenied):
        await service.get(TenantId("t1"), OpportunityId("opp-1"), actor=actor)
    assert len(audit.entries) == 1  # 拒绝路径仅一条审计
    assert audit.entries[0]["rule"] == "deny:abac:country"


async def test_get_abac_category_denied() -> None:
    """get：category 维度受限且机会品类越界 → 拒绝。"""
    factory = _FakeUoWFactory()
    factory.seed_opp(_opp(category="fasteners", owner=EmployeeId("e1")))  # owner 匹配，仅品类越界
    actor = Actor(
        actor_id="e1",
        scope=OpportunityScope(
            level=ScopeLevel.SELF,
            allowed_owners=frozenset({EmployeeId("e1")}),
            allowed_categories=frozenset({"hinges"}),
        ),
    )
    audit = _RecordingAudit()
    service = _make_service(authorizer=_AllowAuthorizer(), audit=audit, factory=factory)
    with pytest.raises(PermissionDenied):
        await service.get(TenantId("t1"), OpportunityId("opp-1"), actor=actor)
    assert len(audit.entries) == 1  # 拒绝路径仅一条审计
    assert audit.entries[0]["rule"] == "deny:abac:category"


async def test_get_abac_matching_scope_allowed() -> None:
    """get：owner 匹配自身限制 → 放行并返回视图。"""
    factory = _FakeUoWFactory()
    factory.seed_opp(_opp(owner=EmployeeId("e1")))
    service = _make_service(authorizer=_AllowAuthorizer(), audit=_RecordingAudit(), factory=factory)
    view = await service.get(TenantId("t1"), OpportunityId("opp-1"), actor=_self_actor("e1"))
    assert view.opportunity_id == OpportunityId("opp-1")
    assert view.owner == "e1"


async def test_get_handoff_packet_abac_uses_linked_opportunity() -> None:
    """get_handoff_packet：owner 限制经关联机会判权（不能靠 handoff ID 绕过）。"""
    factory = _FakeUoWFactory()
    factory.seed_packet(_packet("ho-1", opportunity_id="opp-1"))
    factory.seed_opp(_opp("opp-1", owner=EmployeeId("e2")))
    audit = _RecordingAudit()
    service = _make_service(authorizer=_AllowAuthorizer(), audit=audit, factory=factory)
    with pytest.raises(PermissionDenied):
        await service.get_handoff_packet(TenantId("t1"), HandoffId("ho-1"), actor=_self_actor("e1"))
    assert len(audit.entries) == 1  # 拒绝路径仅一条审计
    assert audit.entries[0]["rule"] == "deny:abac:owner"
    assert audit.entries[0]["action"] == "handoff:read"


async def test_get_handoff_packet_missing_opportunity_fails_closed() -> None:
    """get_handoff_packet：关联机会缺失 + owner 受限 → fail closed 拒绝。"""
    factory = _FakeUoWFactory()
    factory.seed_packet(_packet("ho-1", opportunity_id="opp-1"))
    # 不 seed opp：关联机会缺失，受限维度无法验证 → 拒绝
    audit = _RecordingAudit()
    service = _make_service(authorizer=_AllowAuthorizer(), audit=audit, factory=factory)
    with pytest.raises(PermissionDenied):
        await service.get_handoff_packet(TenantId("t1"), HandoffId("ho-1"), actor=_self_actor("e1"))
    assert len(audit.entries) == 1  # 拒绝路径仅一条审计
    assert audit.entries[0]["rule"] == "deny:abac:owner"


async def test_get_handoff_packet_abac_allowed_for_tenant() -> None:
    """get_handoff_packet：TENANT 无限制 → 放行，即使关联机会 owner 是别人。"""
    factory = _FakeUoWFactory()
    factory.seed_packet(_packet("ho-1", opportunity_id="opp-1"))
    factory.seed_opp(_opp("opp-1", owner=EmployeeId("e2")))
    service = _make_service(authorizer=_AllowAuthorizer(), audit=_RecordingAudit(), factory=factory)
    view = await service.get_handoff_packet(TenantId("t1"), HandoffId("ho-1"), actor=_actor())
    assert view.handoff_id == HandoffId("ho-1")


# --- 修复轮1 · Important 1：租户级聚合拒绝窄作用域 ------------------------------


async def test_get_queue_stats_aggregate_denied_for_self() -> None:
    """get_queue_stats：SELF 窄作用域读租户级聚合 → 拒绝，且不进入 UoW。"""
    audit = _RecordingAudit()
    service = _make_service(
        authorizer=_AllowAuthorizer(), audit=audit, factory=_NeverUoWFactory()
    )
    with pytest.raises(PermissionDenied):
        await service.get_queue_stats(TenantId("t1"), actor=_self_actor("e1"))
    assert len(audit.entries) == 1  # 拒绝路径仅一条审计
    assert audit.entries[0]["rule"] == "deny:abac:aggregate"
    assert audit.entries[0]["action"] == "handoff:queue_read"


async def test_loss_reason_breakdown_aggregate_denied_for_manager() -> None:
    """loss_reason_breakdown：MANAGER 窄作用域读租户级聚合 → 拒绝。"""
    audit = _RecordingAudit()
    actor = Actor(
        actor_id="m1",
        scope=OpportunityScope(
            level=ScopeLevel.MANAGER, allowed_countries=frozenset({"US"})
        ),
    )
    service = _make_service(
        authorizer=_AllowAuthorizer(), audit=audit, factory=_NeverUoWFactory()
    )
    with pytest.raises(PermissionDenied):
        await service.loss_reason_breakdown(TenantId("t1"), actor=actor)
    assert len(audit.entries) == 1  # 拒绝路径仅一条审计
    assert audit.entries[0]["rule"] == "deny:abac:aggregate"
    assert audit.entries[0]["action"] == "loss_reason:read"


async def test_aggregates_allowed_for_tenant_scope() -> None:
    """租户级聚合：显式 TENANT 宽作用域放行（返回空结果，不误伤）。"""
    factory = _FakeUoWFactory()
    service = _make_service(authorizer=_AllowAuthorizer(), audit=_RecordingAudit(), factory=factory)
    stats = await service.get_queue_stats(TenantId("t1"), actor=_actor())
    assert stats.queue_depth == 0
    breakdown = await service.loss_reason_breakdown(TenantId("t1"), actor=_actor())
    assert breakdown == {}


# --- 修复轮1 · Important 3：demo 最小策略 + 真实审计 ----------------------------


def test_demo_authorizer_minimal_policy() -> None:
    """演示 authorizer：仅放行本走查实际动作 + 显式 MANAGER scope；越权默认拒绝。"""
    demo = importlib.import_module("scripts.demo_opportunities")
    auth = demo._DemoAuthorizer()
    manager_scope = OpportunityScope(
        level=ScopeLevel.MANAGER, allowed_countries=frozenset({"US"})
    )
    actor = Actor(actor_id="e1", scope=manager_scope)
    for action in (
        OpportunityAction.OPPORTUNITY_CREATE,
        OpportunityAction.OPPORTUNITY_READ,
        OpportunityAction.OPPORTUNITY_TRANSITION,
        OpportunityAction.OPPORTUNITY_MARK_LOST,
    ):
        assert (
            auth.require(actor, action, manager_scope, TenantId("t1"))
            == "demo:manager:allow"
        )
    # 越权 action：未实际走查的动作默认拒绝
    with pytest.raises(PermissionDenied):
        auth.require(
            actor, OpportunityAction.OPPORTUNITY_ASSIGN, manager_scope, TenantId("t1")
        )
    # 越权 scope：非 MANAGER 一律拒绝
    tenant_actor = Actor(actor_id="e1", scope=OpportunityScope(level=ScopeLevel.TENANT))
    with pytest.raises(PermissionDenied):
        auth.require(
            tenant_actor,
            OpportunityAction.OPPORTUNITY_READ,
            tenant_actor.scope,
            TenantId("t1"),
        )


def test_demo_uses_real_standard_audit_logger() -> None:
    """演示脚本用真实五字段审计（StandardAuditLogger），不再丢弃授权审计。"""
    demo = importlib.import_module("scripts.demo_opportunities")
    assert not hasattr(demo, "_NoopAudit")  # 空审计实现已移除
    assert "StandardAuditLogger" in inspect.getsource(demo)  # 真实审计已装配


# --- 修复轮2 · Important 3：demo authorizer 仅放行精确 MANAGER+US scope ---------


def _demo_manager_us() -> OpportunityScope:
    """演示预期的精确 scope：MANAGER + allowed_countries={"US"}（owner/category 不限）。"""
    return OpportunityScope(level=ScopeLevel.MANAGER, allowed_countries=frozenset({"US"}))


def test_demo_authorizer_rejects_wrong_country_manager() -> None:
    """演示 authorizer：MANAGER 但 country 不是精确 US → 拒绝。"""
    demo = importlib.import_module("scripts.demo_opportunities")
    auth = demo._DemoAuthorizer()
    actor = Actor(
        actor_id="e1",
        scope=OpportunityScope(
            level=ScopeLevel.MANAGER, allowed_countries=frozenset({"CN"})
        ),
    )
    with pytest.raises(PermissionDenied):
        auth.require(actor, OpportunityAction.OPPORTUNITY_READ, actor.scope, TenantId("t1"))


def test_demo_authorizer_rejects_owner_only_manager() -> None:
    """演示 authorizer：MANAGER 只带 owner 维度（非精确 US scope）→ 拒绝。"""
    demo = importlib.import_module("scripts.demo_opportunities")
    auth = demo._DemoAuthorizer()
    actor = Actor(
        actor_id="e1",
        scope=OpportunityScope(
            level=ScopeLevel.MANAGER, allowed_owners=frozenset({EmployeeId("e1")})
        ),
    )
    with pytest.raises(PermissionDenied):
        auth.require(actor, OpportunityAction.OPPORTUNITY_READ, actor.scope, TenantId("t1"))


def test_demo_authorizer_rejects_actor_scope_mismatch() -> None:
    """演示 authorizer：传入 scope 参数与 actor.scope 不一致 → 拒绝。"""
    demo = importlib.import_module("scripts.demo_opportunities")
    auth = demo._DemoAuthorizer()
    actor = Actor(actor_id="e1", scope=OpportunityScope(level=ScopeLevel.TENANT))
    intended = _demo_manager_us()
    with pytest.raises(PermissionDenied):
        auth.require(actor, OpportunityAction.OPPORTUNITY_READ, intended, TenantId("t1"))


def test_demo_authorizer_accepts_exact_intended_scope() -> None:
    """演示 authorizer：精确 MANAGER+US scope 仍放行四动作（最小策略不旁路）。"""
    demo = importlib.import_module("scripts.demo_opportunities")
    auth = demo._DemoAuthorizer()
    scope = _demo_manager_us()
    actor = Actor(actor_id="e1", scope=scope)
    for action in (
        OpportunityAction.OPPORTUNITY_CREATE,
        OpportunityAction.OPPORTUNITY_READ,
        OpportunityAction.OPPORTUNITY_TRANSITION,
        OpportunityAction.OPPORTUNITY_MARK_LOST,
    ):
        assert auth.require(actor, action, scope, TenantId("t1")) == "demo:manager:allow"


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
        self.row: Opportunity | None = None
        self.rows: list[Opportunity] = []

    async def list_by_owner(self, tenant_id, owner, states, limit):
        self.list_by_owner_calls.append((tenant_id, owner, states, limit))
        return self.rows

    async def list_scoped(self, tenant_id, scope, states, limit):
        return self.rows

    async def get(self, tenant_id, opportunity_id) -> Opportunity | None:
        return self.row


class _FakeSnapshotRepo:
    def __init__(self) -> None:
        self.latest: ScoreSnapshot | None = None

    async def latest_for_opportunity(self, tenant_id, opportunity_id):
        return self.latest


class _FakeHandoffRepo:
    def __init__(self) -> None:
        self.row: HandoffPacket | None = None
        self.pending_list: list[HandoffPacket] = []
        self.count_rows: dict[str, int] = {}

    async def get(self, tenant_id, handoff_id) -> HandoffPacket | None:
        return self.row

    async def find_pending_for_opportunity(self, tenant_id, opportunity_id):
        return None

    async def list_pending(self, tenant_id, limit):
        return self.pending_list

    async def list_pending_scoped(self, tenant_id, scope, limit):
        return self.pending_list

    async def count_pending_by_employee(self, tenant_id):
        return self.count_rows


class _FakeLossRepo:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.rows: list[tuple[str, str, int]] = []

    async def count_by_reason_and_state(self, tenant_id, since_days):
        self.calls.append((tenant_id, since_days))
        return self.rows


class _FakeProvenanceRepo:
    def __init__(self) -> None:
        self.rows: list[tuple[str, Provenance]] = []

    async def list_for_entity(self, tenant_id, entity_type, entity_id):
        return list(self.rows)


class _FakeUoW:
    def __init__(self) -> None:
        self.opportunities = _FakeOpportunityRepo()
        self.snapshots = _FakeSnapshotRepo()
        self.handoffs = _FakeHandoffRepo()
        self.loss_records = _FakeLossRepo()
        self.provenance = _FakeProvenanceRepo()

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
        self.seed_opp_row: Opportunity | None = None
        self.seed_packet_row: HandoffPacket | None = None
        self.seed_opp_rows: list[Opportunity] = []

    def seed_opp(self, opp: Opportunity) -> None:
        self.seed_opp_row = opp

    def seed_packet(self, packet: HandoffPacket) -> None:
        self.seed_packet_row = packet

    def seed_rows(self, opps: list[Opportunity]) -> None:
        self.seed_opp_rows = opps

    def __call__(self) -> _FakeUoW:
        uow = _FakeUoW()
        uow.opportunities.row = self.seed_opp_row
        uow.opportunities.rows = self.seed_opp_rows
        uow.handoffs.row = self.seed_packet_row
        self.created.append(uow)
        return uow
