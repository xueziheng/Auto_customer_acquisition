"""OpportunityService 实现（S2-10 核心 + S2-11 handoff/查询 + S3-5 全服务授权）。

依赖注入：``uow_factory``（返回绑定同租户的 ``OpportunityUnitOfWork``）、``scorer``
（策略注入的 ``OpportunityScorer``）、``handoff_policy``（接管 SLA 与积压阈值，
``get_queue_stats`` 使用）、``authorizer``（``OpportunityAuthorizer``，所有公开读写
先判权，默认拒绝）、``audit``（``AuditLogger``，仅 actor/action/tenant/scope/rule，
无敏感值）、``now``（时钟，默认 ``datetime.now``）。

授权契约：每个公开读写入口在**任何仓储读取或业务副作用之前**调用 ``authorizer.require``
（拒绝抛 ``PermissionDenied`` 且不进入 UoW）；允许与拒绝都写授权审计（拒绝
``rule="deny"``）。查询按 ``actor.scope`` 做 ABAC 判权：

- ``list_for_employee`` 查询参数先受 ``scope.allowed_owners`` 限制（``None``
  放行、集合不含目标即拒绝），取回行后在 build/return 前再按 owner/country/
  product_category 三维做**行级 ABAC**（受限维度缺资源值 fail closed）；allow
  延迟到全部行通过后只写一条；
- ``get`` / ``get_handoff_packet`` 在返回前按资源的 owner/country/product_category
  三维做资源级 ABAC（受限维度缺资源值一律 fail closed；``get_handoff_packet``
  经关联机会判权，防按 ID 绕过）；
- ``get_queue_stats`` / ``loss_reason_breakdown`` 是租户级聚合：Phase 1 仓储不支持
  scope 过滤聚合，窄作用域（SELF/MANAGER/无级别）一律显式拒绝，仅 TENANT 或
  authorizer 判权的 SYSTEM 可读未过滤聚合。

硬边界：领域层不 import 外部 SDK。唯一并发恢复**仅限** ``sqlalchemy.exc.IntegrityError``
且 ``orig`` SQLSTATE 为 23505（unique_violation）时重查既有记录；其余异常（领域错误、
编程错误、其他 DB 错误、同名冒充异常）一律原样上抛——不吞、不当作幂等成功。
判定函数只查异常类名/模块与 SQLSTATE，不在领域层引用 sqlalchemy 异常类型。
"""
from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import datetime

from domains.opportunities.errors import (
    AgentInferenceProvenanceError,
    HandoffAlreadyAcceptedError,
    IncompleteHandoffPacketError,
    MissingFieldProvenanceError,
    MissingLossReasonError,
)
from domains.opportunities.models import (
    ALLOWED_TRANSITIONS,
    CRITICAL_FIELDS,
    HandoffPacket,
    HandoffPolicy,
    HandoffState,
    HandoffTrigger,
    LossReason,
    LossRecord,
    Opportunity,
    OpportunityState,
    ScoreSnapshot,
)
from domains.opportunities.permissions import (
    Actor,
    AuditLogger,
    OpportunityAction,
    OpportunityAuthorizer,
    OpportunityScope,
    ScopeLevel,
)
from domains.opportunities.repository import OpportunityUnitOfWork
from domains.opportunities.schemas import (
    HandoffCreateRequest,
    HandoffPacketView,
    HandoffQueueItemView,
    HandoffQueueStats,
    OpportunityCreateRequest,
    OpportunityView,
    ProvenanceSummary,
    ScoreExplanation,
    ValidatedNeedEvidence,
)
from domains.opportunities.scoring import OpportunityScorer, ScoringInput
from shared.errors import (
    InvalidStateTransition,
    PermissionDenied,
    TenantIsolationViolation,
    TradeOSError,
    ValidationError,
)
from shared.events.catalog import (
    HandoffAccepted,
    HandoffQueueBacklogged,
    HandoffRequested,
    OpportunityLost,
    OpportunityQualified,
    OpportunityWon,
)
from shared.schemas.evidence import (
    ConfidenceTier,
    EvidenceItem,
    EvidenceLevel,
    derive_confidence,
)
from shared.schemas.identifiers import (
    EmployeeId,
    HandoffId,
    LossRecordId,
    OpportunityId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.provenance import Provenance, SourceType

_tenant_isolation_alert_logger = logging.getLogger("security.tenant_isolation")


def _is_unique_violation(exc: BaseException) -> bool:
    """是否 SQLAlchemy 唯一约束冲突（``sqlalchemy.exc.IntegrityError`` + SQLSTATE 23505）。

    领域层不 import 外部 SDK：只检查异常**类型名与模块**（防同名自定义异常冒充）、
    ``orig.sqlstate``/``orig.pgcode`` 是否等于 ``"23505"``。**不按错误字符串猜**，
    也不对任意非领域异常无条件重查——只有确认为唯一约束冲突才进入幂等恢复。
    """
    if exc.__class__.__name__ != "IntegrityError":
        return False
    if exc.__class__.__module__ != "sqlalchemy.exc":
        return False  # 非 SQLAlchemy 模块的同名异常不得冒充唯一冲突
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    state = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return state == "23505"


def _is_handoff_escalation_duplicate(exc: BaseException) -> bool:
    """仅识别升级审计精确唯一约束；不靠异常文本猜测。"""
    if exc.__class__.__name__ != "IntegrityError":
        return False
    if exc.__class__.__module__ != "sqlalchemy.exc":
        return False
    pending: list[object] = [getattr(exc, "orig", None)]
    seen: set[int] = set()
    sqlstate: str | None = None
    constraint: str | None = None
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        sqlstate = sqlstate or getattr(current, "sqlstate", None) or getattr(
            current, "pgcode", None
        )
        diag = getattr(current, "diag", None)
        constraint = constraint or getattr(current, "constraint_name", None) or getattr(
            diag, "constraint_name", None
        )
        pending.extend(
            [
                getattr(current, "__cause__", None),
                getattr(current, "__context__", None),
            ]
        )
    return (
        sqlstate == "23505"
        and constraint == "uq_handoff_escalations_tenant_handoff_level"
    )


def validate_present_critical_provenance(request: OpportunityCreateRequest) -> None:
    """校验 request 中 **present** 的关键字段都有 Provenance（硬边界 4），且无 Agent 推断（硬边界 5）。

    ``account_name``/``country`` 必然 present；其余 CRITICAL_FIELDS 只在值非 None 时要求来源。
    缺失抛 ``MissingFieldProvenanceError``；来源为 ``AGENT_INFERENCE`` 抛
    ``AgentInferenceProvenanceError``。纯函数，无 IO。
    """
    for field in CRITICAL_FIELDS:
        if getattr(request, field, None) is not None:
            prov = request.field_provenance.get(field)
            if prov is None:
                raise MissingFieldProvenanceError(f"关键字段 {field} 缺来源（硬边界 4）")
            if prov.source_type == SourceType.AGENT_INFERENCE:
                raise AgentInferenceProvenanceError(
                    f"关键字段 {field} 来源是 Agent 推断（硬边界 5）：机会只持久化事实"
                )


# S3-6 R5/F6：已验证需求证据等级门槛——「客户明确表达过」从这一档起。
_MIN_VALIDATED_EVIDENCE_LEVEL = EvidenceLevel.CUSTOMER_INTEREST_REPLY
_VALIDATED_LEVELS = frozenset(
    {
        EvidenceLevel.CUSTOMER_INTEREST_REPLY,
        EvidenceLevel.CUSTOMER_SPECIFICATION,
        EvidenceLevel.CUSTOMER_QUANTITY_AND_TIMING,
        EvidenceLevel.CUSTOMER_SAMPLE_OR_QUOTE_REQUEST,
    }
)
# S3-6 R5/F6：已验证需求来源白名单（硬边界 4/5：必须能追到客户消息/上传/员工确认）。
_VALIDATED_SOURCE_TYPES = frozenset(
    {SourceType.CONVERSATION, SourceType.UPLOAD, SourceType.EMPLOYEE_INPUT}
)


def validate_validated_need_evidence(evidence: ValidatedNeedEvidence) -> None:
    """校验已验证需求证据（S3-6 R5/F6）。

    「已验证需求」是**门槛不是标签**：公开企业事件、员工猜测、Agent 推断
    都不是客户本人明确表达过。任何带标签的输入都不能仅凭标签进入机会创建。

    校验（全部失败抛 ``shared.errors.ValidationError``）：
    - ``level`` 必须 ≥ ``CUSTOMER_INTEREST_REPLY``（LOW_MID/公开企业事件不够格）；
    - ``provenance.source_type`` 仅允许 conversation/upload/employee_input；
    - ``EMPLOYEE_INPUT`` 必须带真实人工确认对（``confirmed_by``/``confirmed_at``）；
      conversation/upload 可直接指向具体来源 ID，无需确认对。
    - ``source_id`` 非空由 ``Provenance`` 自身不变量保证，此处不重复。
    """
    if evidence.level not in _VALIDATED_LEVELS:
        raise ValidationError(
            f"已验证需求证据等级必须 ≥ {_MIN_VALIDATED_EVIDENCE_LEVEL.value}；"
            f"当前 {evidence.level.value} 是推断，不是客户明确表达"
        )
    source_type = evidence.provenance.source_type
    if source_type not in _VALIDATED_SOURCE_TYPES:
        raise ValidationError(
            f"已验证需求来源必须是 conversation/upload/employee_input；"
            f"当前 {source_type.value}"
        )
    if (
        source_type == SourceType.EMPLOYEE_INPUT
        and (
            evidence.provenance.confirmed_by is None
            or evidence.provenance.confirmed_at is None
        )
    ):
        raise ValidationError(
            "EMPLOYEE_INPUT 证据必须带真实人工确认对"
            "（confirmed_by/confirmed_at）：员工录入本身不算客户明确表达"
        )


def _derive_validated_need_confidence(
    request: OpportunityCreateRequest,
    evidence: ValidatedNeedEvidence,
    *,
    now: datetime,
) -> ConfidenceTier:
    """由已验证需求证据确定性推导打分档位。

    ``request.evidence_tier`` 只是兼容一致性声明，不参与推导；它只能重申
    原始 EvidenceLevel 或确定性推导后的 ConfidenceTier，否则在进入
    UoW 前失败关闭（硬边界 3）。
    """
    provenance = evidence.provenance
    result = derive_confidence(
        [
            EvidenceItem(
                level=evidence.level,
                source_type=provenance.source_type.value,
                source_id=provenance.source_id,
                observed_at=provenance.extracted_at,
                summary="客户明确表达采购需求",
            )
        ],
        now=now,
    )
    if request.evidence_tier not in {evidence.level.value, result.tier.value}:
        raise ValidationError("证据档位声明与已验证需求证据不一致")
    return result.tier


# customer_verbatim 来源白名单（硬边界 4：原话必须能追到证据）。
_VERBATIM_ALLOWED_SOURCES = frozenset(
    {SourceType.CONVERSATION, SourceType.UPLOAD, SourceType.EMPLOYEE_INPUT}
)


def _validate_handoff_packet(request: HandoffCreateRequest) -> None:
    """接管包关键字段（str）空串或纯空白 → IncompleteHandoffPacketError。

    不完整的接管包会被员工忽略，被忽略的接管会导致客户失联。
    """
    for field in ("account_name", "why_valuable", "customer_verbatim"):
        value = getattr(request, field)
        if not value or not value.strip():
            raise IncompleteHandoffPacketError(
                f"接管包缺关键字段 {field}：不完整的接管包会被员工忽略"
            )


def _validate_verbatim_provenance(prov: Provenance) -> None:
    """customer_verbatim 来源只允许 conversation/upload/employee_input。"""
    if prov.source_type not in _VERBATIM_ALLOWED_SOURCES:
        raise ValidationError(
            f"customer_verbatim 来源必须是 conversation/upload/employee_input；"
            f"当前 {prov.source_type.value}"
        )


def _parse_trigger(raw: str) -> HandoffTrigger:
    """解析接管触发条件；非法值转成 ValidationError（不泄漏内置 ValueError）。"""
    try:
        return HandoffTrigger(raw)
    except ValueError as exc:
        raise ValidationError(f"非法接管触发条件：{raw}") from exc


def _explanation(snapshot: ScoreSnapshot) -> ScoreExplanation:
    """把最新打分快照完整复制成 ScoreExplanation（列表/字典复制避免别名）。"""
    return ScoreExplanation(
        sort_key=snapshot.sort_key,
        rank_bucket=snapshot.rank_bucket,
        passed_gates=list(snapshot.passed_gates),
        failed_gates=list(snapshot.failed_gates),
        gate_reasons=dict(snapshot.gate_reasons),
        scored_at=snapshot.scored_at,
        scorer_version=snapshot.scorer_version,
    )


def _provenance_summary(
    field_name: str, provenance: Provenance
) -> ProvenanceSummary:
    """内部 Provenance 转公共稳定字符串 DTO；不丢任何只增历史字段。"""
    return ProvenanceSummary(
        field_name=field_name,
        source_type=provenance.source_type.value,
        source_id=provenance.source_id,
        extracted_by=provenance.extracted_by,
        extracted_at=provenance.extracted_at,
        confirmed_by=(
            str(provenance.confirmed_by)
            if provenance.confirmed_by is not None
            else None
        ),
        confirmed_at=provenance.confirmed_at,
        source_url=provenance.source_url,
        page_hash=provenance.page_hash,
    )


class OpportunityServiceImpl:
    """``OpportunityService`` 的完整实现（S2-10 核心 + S2-11 handoff/查询 + S3-5 授权）。"""

    def __init__(
        self,
        uow_factory: Callable[[], OpportunityUnitOfWork],
        scorer: OpportunityScorer,
        handoff_policy: HandoffPolicy,
        *,
        authorizer: OpportunityAuthorizer,
        audit: AuditLogger,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._uow_factory = uow_factory
        self._scorer = scorer
        self._handoff_policy = handoff_policy  # 接管 SLA/积压阈值（get_queue_stats 使用）
        self._authorizer = authorizer
        self._audit = audit
        self._now = now

    # --- 授权与审计 -----------------------------------------------------------

    def _authorize(
        self, actor: Actor, action: OpportunityAction, tenant_id: TenantId
    ) -> str:
        """判权（必须在任何仓储读取/副作用之前调用）；返回判权所用规则标识。

        拒绝（``PermissionDenied``）时写 ``rule="deny"`` 审计并抛出；放行**不写
        allow 审计**——allow 由 ``_audit_allow`` 在所有 ABAC 检查通过后只写一次，
        避免"先 allow 再 ABAC deny"的双条审计。不记录任何业务/异常内容。
        """
        return self._authorize_scope(actor, action, actor.scope, tenant_id)

    def _authorize_scope(
        self,
        actor: Actor,
        action: OpportunityAction,
        scope: OpportunityScope,
        tenant_id: TenantId,
    ) -> str:
        """对显式请求 scope 判权；拒绝审计不含业务 payload。"""
        try:
            rule = self._authorizer.require(actor, action, scope, tenant_id)
        except PermissionDenied:
            self._audit.log(
                actor=actor.actor_id,
                action=action.value,
                tenant_id=tenant_id,
                scope=scope.label,
                rule="deny",
            )
            raise
        return rule

    def _audit_allow(
        self,
        actor: Actor,
        action: OpportunityAction,
        tenant_id: TenantId,
        rule: str,
    ) -> None:
        """所有判权/ABAC 检查通过后，写**唯一一条** allow 审计。"""
        self._audit.log(
            actor=actor.actor_id,
            action=action.value,
            tenant_id=tenant_id,
            scope=actor.scope.label,
            rule=rule,
        )

    def _enforce_owner_abac(
        self, actor: Actor, employee_id: EmployeeId, tenant_id: TenantId
    ) -> None:
        """``list_for_employee`` 的 ABAC 维度判权（查询同样做 scope 判权）。

        ``scope.allowed_owners`` 为 ``None`` = 该维度不限制；限制集合不含目标
        owner → 拒绝（fail closed）。owner 是强类型 ``EmployeeId``，直接成员比较，
        不在字符串上做转换（避免 S3-13 SQL scope 翻译时丢类型）。
        """
        allowed = actor.scope.allowed_owners
        if allowed is not None and employee_id not in allowed:
            self._deny_abac(
                actor,
                OpportunityAction.OPPORTUNITY_LIST,
                tenant_id,
                rule="deny:abac:owner",
                message="无权查看该负责人的机会列表",
            )

    def _deny_abac(
        self,
        actor: Actor,
        action: OpportunityAction,
        tenant_id: TenantId,
        *,
        rule: str,
        message: str,
        audit_scope: OpportunityScope | None = None,
    ) -> None:
        """写一条无业务 payload 的 ABAC 拒绝审计并抛 PermissionDenied。"""
        self._audit.log(
            actor=actor.actor_id,
            action=action.value,
            tenant_id=tenant_id,
            scope=(audit_scope or actor.scope).label,
            rule=rule,
        )
        raise PermissionDenied(f"ABAC 拒绝：{message}")

    def _deny_tenant_isolation(
        self,
        actor: Actor,
        action: OpportunityAction,
        tenant_id: TenantId,
        *,
        message: str,
    ) -> None:
        """跨租户数据固定审计并拒绝；不记录实体 ID 或业务内容。"""
        self._audit.log(
            actor=actor.actor_id,
            action=action.value,
            tenant_id=tenant_id,
            scope=actor.scope.label,
            rule="deny:tenant_isolation",
        )
        _tenant_isolation_alert_logger.critical(
            "检测到跨租户数据隔离违规",
            extra={
                "actor": actor.actor_id,
                "action": action.value,
                "tenant_id": str(tenant_id),
                "scope": actor.scope.label,
                "rule": "deny:tenant_isolation",
            },
        )
        raise TenantIsolationViolation(message)

    def _enforce_resource_abac(
        self,
        actor: Actor,
        *,
        owner: EmployeeId | None,
        country: str | None,
        product_category: str | None,
        tenant_id: TenantId,
        action: OpportunityAction,
    ) -> None:
        """按资源的 owner/country/product_category 三维做 ABAC 判权（fail closed）。

        维度限制为 ``None`` = 该维度不限制；限制集合不含资源值、或受限维度缺
        资源值（如 ``owner=None``）→ 拒绝。owner 是强类型 ``EmployeeId``，直接
        成员比较，不做 str 转换。必须在返回/构造视图前调用。
        """
        scope = actor.scope
        if scope.allowed_owners is not None and (
            owner is None or owner not in scope.allowed_owners
        ):
            self._deny_abac(
                actor,
                action,
                tenant_id,
                rule="deny:abac:owner",
                message="无权访问该负责人的机会",
            )
        if scope.allowed_countries is not None and (
            country is None or country not in scope.allowed_countries
        ):
            self._deny_abac(
                actor,
                action,
                tenant_id,
                rule="deny:abac:country",
                message="无权访问该国家/地区的机会",
            )
        if scope.allowed_categories is not None and (
            product_category is None or product_category not in scope.allowed_categories
        ):
            self._deny_abac(
                actor,
                action,
                tenant_id,
                rule="deny:abac:category",
                message="无权访问该品类的机会",
            )

    def _enforce_aggregate_abac(
        self,
        actor: Actor,
        action: OpportunityAction,
        tenant_id: TenantId,
    ) -> None:
        """租户级聚合必须显式宽作用域：SELF/MANAGER/无级别一律拒绝。

        Phase 1 仓储不支持 scope 过滤聚合，宁可拒绝窄作用域，也不返回租户级
        全量数据（避免 SELF/MANAGER 读到超权限聚合）。SYSTEM 已由
        ``authorizer.require`` 判权放行，这里只拦窄作用域。
        """
        if actor.scope.level in (ScopeLevel.SELF, ScopeLevel.MANAGER, None):
            self._deny_abac(
                actor,
                action,
                tenant_id,
                rule="deny:abac:aggregate",
                message="窄作用域无权读取租户级聚合数据",
            )

    def _enforce_safe_query_scope(
        self,
        actor: Actor,
        action: OpportunityAction,
        tenant_id: TenantId,
        scope: OpportunityScope,
    ) -> None:
        """拒绝无级别与无限制 SYSTEM，防最小系统身份意外扩成全租户。"""
        unrestricted_system = (
            scope.level is ScopeLevel.SYSTEM
            and scope.allowed_owners is None
            and scope.allowed_countries is None
            and scope.allowed_categories is None
        )
        if scope.level is None or unrestricted_system:
            self._deny_abac(
                actor,
                action,
                tenant_id,
                rule="deny:unsafe_scope",
                message="查询作用域未明确收窄",
            )

    @staticmethod
    def _validate_states(states: list[OpportunityState] | None) -> None:
        """运行时拒绝非 OpportunityState，避免边界输入泄漏为 AttributeError。"""
        if states is not None and any(
            not isinstance(state, OpportunityState) for state in states
        ):
            raise ValidationError("states 必须是 OpportunityState 列表")

    async def create_from_need(
        self,
        tenant_id: TenantId,
        request: OpportunityCreateRequest,
        evidence: ValidatedNeedEvidence,
        *,
        actor: Actor,
    ) -> OpportunityId | None:
        """从已验证需求创建机会。

        - 授权与证据门禁先于一切副作用：先 ``authorizer.require``，再
          ``validate_validated_need_evidence``——invalid evidence 在进入 UoW /
          幂等查询 / 打分 / 事件之前抛 ``ValidationError``，即使同一 need 已存在。
        - 幂等：同一 ``need_id`` 已有机会 → 返回既有 ID。
        - 门槛失败：只保留失败快照（scorer 已落库），返回 None，不建机会、不发事件。
        - 通过：建机会、保存 present 关键字段 provenance、发布 ``OpportunityQualified``。
        - 唯一并发：commit 阶段的 DB 异常（``IntegrityError`` 属非领域异常）→ 新 UoW
          重查既有；只有确实找到才返回既有，否则原异常重抛。
        """
        rule = self._authorize(actor, OpportunityAction.OPPORTUNITY_CREATE, tenant_id)
        self._audit_allow(actor, OpportunityAction.OPPORTUNITY_CREATE, tenant_id, rule)
        validate_validated_need_evidence(evidence)
        evidence_tier = _derive_validated_need_confidence(
            request,
            evidence,
            now=self._now(),
        )
        try:
            async with self._uow_factory() as uow:
                existing = await uow.opportunities.find_by_need(
                    tenant_id, ValidatedNeedId(request.need_id)
                )
                if existing is not None:
                    return existing.opportunity_id

                validate_present_critical_provenance(request)

                opportunity_id = OpportunityId(new_id("opp"))
                scoring_input = ScoringInput(
                    has_verified_contact=request.has_verified_contact,
                    evidence_tier=evidence_tier,
                    category_allowed=request.category_allowed,
                    minimum_order_value=request.minimum_order_value,
                    estimated_order_value=request.estimated_order_value,
                    supply_available=request.supply_available,
                    is_repeat_buyer_likely=request.is_repeat_buyer_likely,
                )
                snapshot = await self._scorer.score(
                    uow.snapshots, tenant_id, opportunity_id, scoring_input
                )
                if snapshot.failed_gates:
                    return None  # 门槛失败：失败快照已入库，不建机会/不发事件

                opp = Opportunity(
                    opportunity_id=opportunity_id,
                    tenant_id=tenant_id,
                    account_id=ProspectAccountId(request.account_id),
                    need_id=ValidatedNeedId(request.need_id),
                    product_category=request.product_category,
                    created_at=self._now(),
                    account_name=request.account_name,
                    country=request.country,
                    quantity=request.quantity,
                    spec_summary=request.spec_summary,
                    application=request.application,
                    destination=request.destination,
                    required_by=request.required_by,
                    target_price=request.target_price,
                    current_supply_solution=request.current_supply_solution,
                    current_supply_problem=request.current_supply_problem,
                )
                await uow.opportunities.add(opp)
                # 只保存 CRITICAL_FIELDS 中在 request 上实际 present（值非 None）的字段；
                # account_name/country 必然 present。不保存任何未知/多余键。
                for field in CRITICAL_FIELDS:
                    if getattr(request, field, None) is not None:
                        await uow.provenance.save(
                            tenant_id,
                            "opportunity",
                            opp.opportunity_id,
                            field,
                            request.field_provenance[field],
                        )
                await uow.bus.publish(
                    OpportunityQualified(
                        tenant_id=tenant_id,
                        occurred_at=self._now(),
                        run_id=None,
                        opportunity_id=opp.opportunity_id,
                        rank_bucket=snapshot.rank_bucket,
                    )
                )
                return opp.opportunity_id
        except TradeOSError:
            # 领域错误（校验/状态/币种）不重试，直接上抛。
            raise
        except Exception as exc:
            # 唯一并发：仅当确认为 DB 唯一约束冲突（SQLSTATE 23505）才进入幂等恢复；
            # 编程错误/其他 DB 错误一律原样抛出（不吞异常、不当幂等成功）。
            if not _is_unique_violation(exc):
                raise
            async with self._uow_factory() as uow:
                existing = await uow.opportunities.find_by_need(
                    tenant_id, ValidatedNeedId(request.need_id)
                )
            if existing is not None:
                return existing.opportunity_id
            raise

    async def assign(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        owner: EmployeeId,
        assigned_by: EmployeeId,
        *,
        actor: Actor,
    ) -> None:
        """分配负责人并落审计字段（``assigned_at`` 用注入时钟）；失败不静默。

        ``actor`` 授权身份，``assigned_by`` 业务审计主体（两者分离）。
        """
        action = OpportunityAction.OPPORTUNITY_ASSIGN
        rule = self._authorize(actor, action, tenant_id)
        async with self._uow_factory() as uow:
            opp = await uow.opportunities.get(tenant_id, opportunity_id)
            if opp is None:
                raise ValidationError("机会不存在或不属于该租户")
            self._enforce_resource_abac(
                actor,
                owner=owner,
                country=opp.country,
                product_category=opp.product_category,
                tenant_id=tenant_id,
                action=action,
            )
            ok = await uow.opportunities.assign_owner(
                tenant_id, opportunity_id, owner, assigned_by, self._now()
            )
            if not ok:
                raise InvalidStateTransition("机会已变更，无法分配")
        self._audit_allow(actor, action, tenant_id, rule)

    async def transition(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        target: OpportunityState,
        *,
        actor: Actor,
    ) -> None:
        """状态推进：拒绝 WON/LOST 走普通转换；读当前态→状态机校验→原子 advance_state。"""
        action = OpportunityAction.OPPORTUNITY_TRANSITION
        rule = self._authorize(actor, action, tenant_id)
        async with self._uow_factory() as uow:
            opp = await uow.opportunities.get(tenant_id, opportunity_id)
            if opp is None:
                raise ValidationError("机会不存在或不属于该租户")
            self._enforce_resource_abac(
                actor,
                owner=opp.owner,
                country=opp.country,
                product_category=opp.product_category,
                tenant_id=tenant_id,
                action=action,
            )
            if target in (OpportunityState.WON, OpportunityState.LOST):
                raise InvalidStateTransition("终态不能走普通 transition")
            if not opp.can_transition_to(target):
                allowed = sorted(ALLOWED_TRANSITIONS[opp.state], key=lambda s: s.value)
                raise InvalidStateTransition(
                    f"非法转换：{opp.state.value} → {target.value}；"
                    f"允许：{', '.join(s.value for s in allowed)}"
                )
            ok = await uow.opportunities.advance_state(
                tenant_id, opportunity_id, opp.state, target
            )
            if not ok:
                raise InvalidStateTransition("机会状态已被并发修改")
        self._audit_allow(actor, action, tenant_id, rule)

    async def mark_lost(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        reason: LossReason | None,
        *,
        actor: Actor,
        confirmed_by: EmployeeId,
        confirmed_at: datetime,
        detail: str | None = None,
    ) -> None:
        """终结机会（**人工确认动作**）：close_lost_if_state → 只增 LossRecord → 发布 OpportunityLost。

        ``actor`` 授权身份，``confirmed_by``/``confirmed_at`` 业务确认主体（两者分离）。
        ``reason=None`` 先抛 ``MissingLossReasonError``（反馈闭环）。``died_at_state``
        记录关闭前状态；``recorded_at`` 用注入时钟。终态只能走 close_*，禁止普通 update。
        """
        action = OpportunityAction.OPPORTUNITY_MARK_LOST
        rule = self._authorize(actor, action, tenant_id)
        async with self._uow_factory() as uow:
            opp = await uow.opportunities.get(tenant_id, opportunity_id)
            if opp is None:
                raise ValidationError("机会不存在或不属于该租户")
            self._enforce_resource_abac(
                actor,
                owner=opp.owner,
                country=opp.country,
                product_category=opp.product_category,
                tenant_id=tenant_id,
                action=action,
            )
            if reason is None:
                raise MissingLossReasonError("终结机会必须带 LossReason（反馈闭环）")
            if opp.state in (OpportunityState.WON, OpportunityState.LOST):
                raise InvalidStateTransition("机会已处于终态，不能重复终结")
            ok = await uow.opportunities.close_lost_if_state(
                tenant_id, opportunity_id, opp.state, reason, detail, confirmed_by, confirmed_at
            )
            if not ok:
                raise InvalidStateTransition("机会状态已被并发修改")
            record = LossRecord(
                loss_record_id=LossRecordId(new_id("loss")),
                tenant_id=tenant_id,
                opportunity_id=opportunity_id,
                loss_reason=reason,
                died_at_state=opp.state,  # 关闭前状态
                confirmed_by=confirmed_by,
                confirmed_at=confirmed_at,
                recorded_at=self._now(),
                detail=detail,
            )
            await uow.loss_records.add(tenant_id, record)
            await uow.bus.publish(
                OpportunityLost(
                    tenant_id=tenant_id,
                    occurred_at=confirmed_at,
                    run_id=None,
                    opportunity_id=opportunity_id,
                    loss_reason=reason.value,
                    died_at_state=opp.state.value,
                )
            )
        self._audit_allow(actor, action, tenant_id, rule)

    async def mark_won(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        *,
        actor: Actor,
        confirmed_by: EmployeeId,
        confirmed_at: datetime,
    ) -> None:
        """终结为成交（**人工确认动作**）：仅 NEGOTIATING → close_won_if_state → 发布 OpportunityWon。

        ``actor`` 授权身份，``confirmed_by`` 业务确认主体（两者分离）。
        """
        action = OpportunityAction.OPPORTUNITY_MARK_WON
        rule = self._authorize(actor, action, tenant_id)
        async with self._uow_factory() as uow:
            opp = await uow.opportunities.get(tenant_id, opportunity_id)
            if opp is None:
                raise ValidationError("机会不存在或不属于该租户")
            self._enforce_resource_abac(
                actor,
                owner=opp.owner,
                country=opp.country,
                product_category=opp.product_category,
                tenant_id=tenant_id,
                action=action,
            )
            if opp.state != OpportunityState.NEGOTIATING:
                raise InvalidStateTransition("mark_won 仅能从 negotiating 转入")
            ok = await uow.opportunities.close_won_if_state(
                tenant_id, opportunity_id, confirmed_by, confirmed_at
            )
            if not ok:
                raise InvalidStateTransition("机会状态已被并发修改")
            await uow.bus.publish(
                OpportunityWon(
                    tenant_id=tenant_id,
                    occurred_at=confirmed_at,
                    run_id=None,
                    opportunity_id=opportunity_id,
                    closed_by=confirmed_by,
                )
            )
        self._audit_allow(actor, action, tenant_id, rule)

    # --- 人工接管 / 查询（S2-11） ------------------------------------------------

    async def request_handoff(
        self, tenant_id: TenantId, request: HandoffCreateRequest, *, actor: Actor
    ) -> HandoffId:
        """请求人工接管。

        校验（incomplete/verbatim 来源/trigger）失败不保存、不发事件；tenant+opportunity
        已有 pending 幂等返回；新建前确认机会存在且属于租户（避免 FK 错误推迟到 commit）。
        顺序：``provenance.save`` → ``handoffs.add`` → ``bus.publish(HandoffRequested)``。
        """
        action = OpportunityAction.HANDOFF_REQUEST
        rule = self._authorize(actor, action, tenant_id)
        async with self._uow_factory() as uow:
            opp = await uow.opportunities.get(
                tenant_id, OpportunityId(request.opportunity_id)
            )
            if opp is None:
                raise ValidationError("机会不存在或不属于该租户")
            self._enforce_resource_abac(
                actor,
                owner=opp.owner,
                country=opp.country,
                product_category=opp.product_category,
                tenant_id=tenant_id,
                action=action,
            )
            existing = await uow.handoffs.find_pending_for_opportunity(
                tenant_id, OpportunityId(request.opportunity_id)
            )
            if existing is not None:
                handoff_id = existing.handoff_id
            else:
                _validate_handoff_packet(request)
                _validate_verbatim_provenance(request.customer_verbatim_provenance)
                trigger = _parse_trigger(request.trigger)
                handoff_id = HandoffId(new_id("hand"))
                if opp.owner is None:
                    raise ValidationError("接管请求缺少机会负责人")
                requested_at = self._now()
                packet = HandoffPacket(
                    handoff_id=handoff_id,
                    tenant_id=tenant_id,
                    opportunity_id=OpportunityId(request.opportunity_id),
                    trigger=trigger,
                    requested_at=requested_at,
                    account_name=request.account_name,
                    country=request.country,
                    why_valuable=request.why_valuable,
                    customer_verbatim=request.customer_verbatim,
                    how_we_found_them=request.how_we_found_them,
                    validated_need_summary=request.validated_need_summary,
                    missing_information=list(request.missing_information),
                    conversation_summary=request.conversation_summary,
                    already_sent=list(request.already_sent),
                    commitments_made=list(request.commitments_made),
                    suggested_next_step=request.suggested_next_step,
                    evidence_links=list(request.evidence_links),
                    assigned_to=opp.owner,
                )
                await uow.provenance.save(
                    tenant_id,
                    "handoff",
                    str(handoff_id),
                    "customer_verbatim",
                    request.customer_verbatim_provenance,
                )
                await uow.handoffs.add(packet)
                await uow.bus.publish(
                    HandoffRequested(
                        tenant_id=tenant_id,
                        occurred_at=requested_at,
                        run_id=None,
                        handoff_id=handoff_id,
                        opportunity_id=OpportunityId(request.opportunity_id),
                        assigned_to=opp.owner,
                        trigger=trigger.value,
                    )
                )
        self._audit_allow(actor, action, tenant_id, rule)
        return handoff_id

    async def record_handoff_escalation(
        self,
        tenant_id: TenantId,
        handoff_id: HandoffId,
        level: int,
        escalated_at: datetime,
        *,
        actor: Actor,
    ) -> None:
        """判权后追加升级审计；仅精确重复唯一键视为幂等成功。"""
        action = OpportunityAction.HANDOFF_ESCALATION_RECORD
        rule = self._authorize(actor, action, tenant_id)
        self._audit_allow(actor, action, tenant_id, rule)
        try:
            async with self._uow_factory() as uow:
                await uow.handoffs.record_escalation(
                    tenant_id, handoff_id, level, escalated_at
                )
        except Exception as exc:
            if not _is_handoff_escalation_duplicate(exc):
                raise

    async def accept_handoff(
        self,
        tenant_id: TenantId,
        handoff_id: HandoffId,
        accepted_by: EmployeeId,
        *,
        actor: Actor,
    ) -> None:
        """员工接受接管：原子 ``accept_if_requested``；并发已被接受抛 HandoffAlreadyAcceptedError。

        ``actor`` 授权身份，``accepted_by`` 业务审计主体（事件 accepted_by，两者分离）。
        """
        action = OpportunityAction.HANDOFF_ACCEPT
        rule = self._authorize(actor, action, tenant_id)
        async with self._uow_factory() as uow:
            packet = await uow.handoffs.get(tenant_id, handoff_id)
            if packet is None:
                raise ValidationError("接管不存在或不属于该租户")
            linked = await uow.opportunities.get(tenant_id, packet.opportunity_id)
            if linked is None:
                raise ValidationError("接管关联的机会或负责人无效")
            self._enforce_resource_abac(
                actor,
                owner=linked.owner,
                country=linked.country,
                product_category=linked.product_category,
                tenant_id=tenant_id,
                action=action,
            )
            if packet.assigned_to != linked.owner:
                raise ValidationError("接管关联的机会或负责人无效")
            if packet.state is not HandoffState.REQUESTED:
                raise HandoffAlreadyAcceptedError("接管已被接受")
            if (
                actor.scope.level is not ScopeLevel.SYSTEM
                and accepted_by != EmployeeId(actor.actor_id)
            ):
                self._deny_abac(
                    actor,
                    action,
                    tenant_id,
                    rule="deny:abac:accepted_by",
                    message="接管接受人必须等于当前身份",
                )
            accepted_at = self._now()
            ok = await uow.handoffs.accept_if_requested(
                tenant_id, handoff_id, accepted_by, accepted_at
            )
            if not ok:
                raise HandoffAlreadyAcceptedError("接管已被接受")
            await uow.bus.publish(
                HandoffAccepted(
                    tenant_id=tenant_id,
                    occurred_at=accepted_at,
                    run_id=None,
                    handoff_id=handoff_id,
                    accepted_by=accepted_by,
                )
            )
        self._audit_allow(actor, action, tenant_id, rule)

    async def get_handoff_packet(
        self, tenant_id: TenantId, handoff_id: HandoffId, *, actor: Actor
    ) -> HandoffPacketView:
        """读取接管包：完整映射、list 字段复制、wait_seconds 用注入 now。

        owner/category 不在接管包上，资源 ABAC 经**关联机会**判权——防止只按
        handoff ID 就绕过 owner/country/category 限制读取别人负责的接管包。
        """
        rule = self._authorize(actor, OpportunityAction.HANDOFF_READ, tenant_id)
        async with self._uow_factory() as uow:
            packet = await uow.handoffs.get(tenant_id, handoff_id)
            if packet is None:
                raise ValidationError(f"接管包 {handoff_id} 不存在")
            linked = await uow.opportunities.get(tenant_id, packet.opportunity_id)
            self._enforce_resource_abac(
                actor,
                owner=linked.owner if linked is not None else None,
                country=linked.country if linked is not None else None,
                product_category=(
                    linked.product_category if linked is not None else None
                ),
                tenant_id=tenant_id,
                action=OpportunityAction.HANDOFF_READ,
            )
            self._audit_allow(actor, OpportunityAction.HANDOFF_READ, tenant_id, rule)
            return HandoffPacketView(
                handoff_id=packet.handoff_id,
                opportunity_id=packet.opportunity_id,
                trigger=packet.trigger.value,
                account_name=packet.account_name,
                country=packet.country,
                why_valuable=packet.why_valuable,
                customer_verbatim=packet.customer_verbatim,
                requested_at=packet.requested_at,
                state=packet.state.value,
                how_we_found_them=packet.how_we_found_them,
                validated_need_summary=packet.validated_need_summary,
                missing_information=list(packet.missing_information),
                conversation_summary=packet.conversation_summary,
                already_sent=list(packet.already_sent),
                commitments_made=list(packet.commitments_made),
                suggested_next_step=packet.suggested_next_step,
                evidence_links=list(packet.evidence_links),
                wait_seconds=packet.wait_seconds(self._now()),
                assigned_to_name=None,  # 无员工域数据
            )

    async def get_queue_stats(
        self, tenant_id: TenantId, *, actor: Actor
    ) -> HandoffQueueStats:
        """待接管队列统计：全部基于同一 now 调 wait_seconds。

        ``by_employee`` 用仓储聚合接口 ``count_pending_by_employee``（不受扫描列表截断）。
        ``list_pending`` 的 limit 用 ``sys.maxsize`` 技术上限（Protocol 无总数 aggregate，
        Phase 1 在现有接口下尽可能完整扫描；**非业务阈值**）。
        policy 语义「超过」：``wait_seconds > sla_seconds`` 才 breached、``queue_depth >
        backlog_threshold`` 才 backlogged（等于不算）。超阈值发布 ``HandoffQueueBacklogged``。
        """
        rule = self._authorize(actor, OpportunityAction.HANDOFF_QUEUE_READ, tenant_id)
        self._enforce_aggregate_abac(
            actor, OpportunityAction.HANDOFF_QUEUE_READ, tenant_id
        )
        self._audit_allow(actor, OpportunityAction.HANDOFF_QUEUE_READ, tenant_id, rule)
        async with self._uow_factory() as uow:
            pending = await uow.handoffs.list_pending(tenant_id, sys.maxsize)
            by_employee = await uow.handoffs.count_pending_by_employee(tenant_id)
            now = self._now()
            waits = [p.wait_seconds(now) for p in pending]
            waited = [w for w in waits if w is not None]
            queue_depth = len(pending)
            oldest = max(waited) if waited else 0
            breached_count = sum(
                1 for w in waited if w > self._handoff_policy.sla_seconds
            )
            is_backlogged = queue_depth > self._handoff_policy.backlog_threshold
            if is_backlogged:
                await uow.bus.publish(
                    HandoffQueueBacklogged(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        run_id=None,
                        queue_depth=queue_depth,
                        oldest_wait_seconds=oldest,
                    )
                )
            return HandoffQueueStats(
                queue_depth=queue_depth,
                oldest_wait_seconds=oldest,
                by_employee=by_employee,
                breached_count=breached_count,
                is_backlogged=is_backlogged,
            )

    async def get(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        *,
        actor: Actor,
    ) -> OpportunityView:
        rule = self._authorize(actor, OpportunityAction.OPPORTUNITY_READ, tenant_id)
        async with self._uow_factory() as uow:
            opp = await uow.opportunities.get(tenant_id, opportunity_id)
            if opp is None:
                raise ValidationError(f"机会 {opportunity_id} 不存在")
            self._enforce_resource_abac(
                actor,
                owner=opp.owner,
                country=opp.country,
                product_category=opp.product_category,
                tenant_id=tenant_id,
                action=OpportunityAction.OPPORTUNITY_READ,
            )
            view = await self._build_view(uow, tenant_id, opp)
        self._audit_allow(actor, OpportunityAction.OPPORTUNITY_READ, tenant_id, rule)
        return view

    async def _build_view(
        self,
        uow: OpportunityUnitOfWork,
        tenant_id: TenantId,
        opp: Opportunity,
    ) -> OpportunityView:
        """同一 UoW 内构造视图，含 newest-first 的完整只增来源历史。"""
        snapshot = await uow.snapshots.latest_for_opportunity(
            tenant_id, opp.opportunity_id
        )
        score = _explanation(snapshot) if snapshot is not None else None
        pending = await uow.handoffs.find_pending_for_opportunity(
            tenant_id, opp.opportunity_id
        )
        provenance_rows = await uow.provenance.list_for_entity(
            tenant_id,
            "opportunity",
            str(opp.opportunity_id),
        )
        return OpportunityView(
            opportunity_id=opp.opportunity_id,
            account_id=opp.account_id,
            account_name=opp.account_name,
            country=opp.country,
            need_id=opp.need_id,
            product_category=opp.product_category,
            state=opp.state.value,
            created_at=opp.created_at,
            quantity=opp.quantity,
            spec_summary=opp.spec_summary,
            destination=opp.destination,
            required_by=opp.required_by,
            target_price=opp.target_price,
            current_supply_problem=opp.current_supply_problem,
            can_source=opp.can_source,
            estimated_cost=opp.estimated_cost,
            estimated_profit=opp.estimated_profit,
            owner=str(opp.owner) if opp.owner is not None else None,
            owner_name=None,  # 无员工域数据
            next_action=opp.next_action,
            next_action_due=opp.next_action_due,
            score=score,
            loss_reason=opp.loss_reason.value if opp.loss_reason is not None else None,
            died_at_state=(
                opp.died_at_state.value if opp.died_at_state is not None else None
            ),
            has_pending_handoff=pending is not None,
            provenance=[
                _provenance_summary(field_name, provenance)
                for field_name, provenance in provenance_rows
            ],
        )

    async def list_for_employee(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        *,
        actor: Actor,
        states: list[OpportunityState] | None = None,
        limit: int = 50,
    ) -> list[OpportunityView]:
        """某员工负责的机会；单 UoW 内构造全部 View（不逐项另开 UoW）。

        ABAC 判权顺序：``authorizer.require``（进入 UoW 前）→ ``allowed_owners``
        查询参数预检（查询前）→ 取回行后按 owner/country/product_category 三维
        逐行判权（build/return 前）。任一越界行（含受限维度缺资源值）即整单拒绝，
        只写一条 deny 审计；全部行通过才写一条 allow 审计——避免"先 allow 再
        行级 ABAC 拒绝"的双条审计。
        """
        rule = self._authorize(actor, OpportunityAction.OPPORTUNITY_LIST, tenant_id)
        self._enforce_owner_abac(actor, employee_id, tenant_id)
        if limit <= 0:
            raise ValidationError("limit 必须 > 0")
        async with self._uow_factory() as uow:
            opps = await uow.opportunities.list_by_owner(
                tenant_id, employee_id, states, limit
            )
            for opp in opps:
                self._enforce_resource_abac(
                    actor,
                    owner=opp.owner,
                    country=opp.country,
                    product_category=opp.product_category,
                    tenant_id=tenant_id,
                    action=OpportunityAction.OPPORTUNITY_LIST,
                )
            views = [await self._build_view(uow, tenant_id, opp) for opp in opps]
        self._audit_allow(actor, OpportunityAction.OPPORTUNITY_LIST, tenant_id, rule)
        return views

    async def list_opportunities(
        self,
        tenant_id: TenantId,
        actor: Actor,
        *,
        scope: OpportunityScope,
        states: list[OpportunityState] | None = None,
        limit: int = 50,
    ) -> list[OpportunityView]:
        """按完整 scope 在 SQL 过滤；返回前对每行再次做资源 ABAC。"""
        action = OpportunityAction.OPPORTUNITY_LIST
        rule = self._authorize_scope(actor, action, scope, tenant_id)
        if scope != actor.scope:
            self._deny_abac(
                actor,
                action,
                tenant_id,
                rule="deny:scope_mismatch",
                message="请求作用域与身份作用域不一致",
                audit_scope=scope,
            )
        self._enforce_safe_query_scope(actor, action, tenant_id, scope)
        if limit <= 0:
            raise ValidationError("limit 必须 > 0")
        self._validate_states(states)
        if states == []:
            self._audit_allow(actor, action, tenant_id, rule)
            return []

        async with self._uow_factory() as uow:
            opportunities = await uow.opportunities.list_scoped(
                tenant_id,
                scope,
                states,
                limit,
            )
            for opportunity in opportunities:
                if opportunity.tenant_id != tenant_id:
                    self._deny_tenant_isolation(
                        actor,
                        action,
                        tenant_id,
                        message="机会列表数据租户不一致",
                    )
                self._enforce_resource_abac(
                    actor,
                    owner=opportunity.owner,
                    country=opportunity.country,
                    product_category=opportunity.product_category,
                    tenant_id=tenant_id,
                    action=action,
                )
            views = [
                await self._build_view(uow, tenant_id, opportunity)
                for opportunity in opportunities
            ]
        self._audit_allow(actor, action, tenant_id, rule)
        return views

    async def list_pending_handoffs(
        self,
        tenant_id: TenantId,
        actor: Actor,
        *,
        limit: int = 50,
    ) -> list[HandoffQueueItemView]:
        """按 SQL scope 返回 REQUESTED 队列，保持仓储的最久等待优先顺序。"""
        action = OpportunityAction.HANDOFF_QUEUE_READ
        scope = actor.scope
        rule = self._authorize_scope(actor, action, scope, tenant_id)
        self._enforce_safe_query_scope(actor, action, tenant_id, scope)
        if limit <= 0:
            raise ValidationError("limit 必须 > 0")

        async with self._uow_factory() as uow:
            packets = await uow.handoffs.list_pending_scoped(
                tenant_id,
                scope,
                limit,
            )
            linked: list[tuple[HandoffPacket, Opportunity]] = []
            for packet in packets:
                if packet.tenant_id != tenant_id:
                    self._deny_tenant_isolation(
                        actor,
                        action,
                        tenant_id,
                        message="待接管列表数据租户不一致",
                    )
                if packet.state is not HandoffState.REQUESTED:
                    raise ValidationError("待接管列表包含非 requested 数据")
                opportunity = await uow.opportunities.get(
                    tenant_id,
                    packet.opportunity_id,
                )
                if opportunity is None:
                    raise ValidationError("接管数据关联机会缺失")
                if opportunity.tenant_id != tenant_id:
                    self._deny_tenant_isolation(
                        actor,
                        action,
                        tenant_id,
                        message="接管关联机会租户不一致",
                    )
                if opportunity.opportunity_id != packet.opportunity_id:
                    raise ValidationError("接管关联机会标识不一致")
                self._enforce_resource_abac(
                    actor,
                    owner=packet.assigned_to,
                    country=opportunity.country,
                    product_category=opportunity.product_category,
                    tenant_id=tenant_id,
                    action=action,
                )
                linked.append((packet, opportunity))

            now = self._now()
            items: list[HandoffQueueItemView] = []
            for packet, _ in linked:
                wait_seconds = packet.wait_seconds(now)
                if wait_seconds is None:
                    raise ValidationError("待接管数据缺等待时长")
                items.append(
                    HandoffQueueItemView(
                        handoff_id=str(packet.handoff_id),
                        opportunity_id=str(packet.opportunity_id),
                        trigger=packet.trigger.value,
                        account_name=packet.account_name,
                        country=packet.country,
                        why_valuable=packet.why_valuable,
                        customer_verbatim=packet.customer_verbatim,
                        requested_at=packet.requested_at,
                        wait_seconds=wait_seconds,
                        state=packet.state.value,
                        assigned_to=(
                            str(packet.assigned_to)
                            if packet.assigned_to is not None
                            else None
                        ),
                        suggested_next_step=packet.suggested_next_step,
                        missing_information=list(packet.missing_information),
                        evidence_links=list(packet.evidence_links),
                    )
                )
        self._audit_allow(actor, action, tenant_id, rule)
        return items

    async def loss_reason_breakdown(
        self, tenant_id: TenantId, *, actor: Actor, since_days: int = 30
    ) -> dict[str, dict[str, int]]:
        """按 ``(loss_reason, died_at_state)`` 二维交叉统计；同键安全累加。"""
        rule = self._authorize(actor, OpportunityAction.LOSS_REASON_READ, tenant_id)
        self._enforce_aggregate_abac(actor, OpportunityAction.LOSS_REASON_READ, tenant_id)
        self._audit_allow(actor, OpportunityAction.LOSS_REASON_READ, tenant_id, rule)
        if since_days <= 0:
            raise ValidationError("since_days 必须 > 0")
        async with self._uow_factory() as uow:
            rows = await uow.loss_records.count_by_reason_and_state(
                tenant_id, since_days
            )
        breakdown: dict[str, dict[str, int]] = {}
        for reason, state, count in rows:
            bucket = breakdown.setdefault(reason, {})
            bucket[state] = bucket.get(state, 0) + count
        return breakdown
