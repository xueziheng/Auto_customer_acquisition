"""回复动作的生产组合：metadata-only 上下文 → 业务事实/公共域服务。

workflow 只传 ``ReplyActionContext`` 的五个 typed ID；本组合按 message_id
经 tenant-bound reader 重读分类证据与原文，再调用 demand/opportunities 公共
服务。客户原话仅以有界逐字摘录进入业务 Provenance/接管包，完整正文只由
artifact 引用定位，不进入 workflow/outbox/log。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from domains.conversations.schemas import (
    MAX_REPLY_FIELD_QUOTE_CODEPOINTS,
    ReplyWorkAction,
    ReplyWorkActionRequest,
)
from domains.conversations.service import ConversationService
from domains.demand.schemas import CustomerReplyEvidenceClaim
from domains.demand.service import DemandService
from domains.opportunities.permissions import (
    Actor as OpportunityActor,
)
from domains.opportunities.permissions import (
    OpportunityScope,
)
from domains.opportunities.permissions import (
    ScopeLevel as OpportunityScopeLevel,
)
from domains.opportunities.schemas import HandoffCreateRequest
from domains.opportunities.service import OpportunityService
from domains.outreach.permissions import Actor as OutreachActor
from domains.outreach.permissions import OutreachScope
from domains.outreach.permissions import ScopeLevel as OutreachScopeLevel
from domains.outreach.schemas import (
    DeliveryCorrelationLookup,
    DeliveryFeedbackTarget,
)
from domains.outreach.service import OutreachService
from domains.sending_identity.service import (
    Actor as SendingIdentityActor,
)
from domains.sending_identity.service import (
    DeliveryEventRecord,
    DeliveryEventType,
    SendingIdentityScope,
    SendingIdentityService,
)
from domains.sending_identity.service import (
    ScopeLevel as SendingIdentityScopeLevel,
)
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    EnrollmentId,
    IdempotencyKey,
    MessageId,
    NeedHypothesisId,
    OpportunityId,
    OutboundMessageId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import Provenance, SourceType
from workflows.reply_qualification.ports import (
    MessageContentReader,
    ReplyActionContext,
)

_SYSTEM_ACTOR = OpportunityActor(
    "system:reply-qualification",
    OpportunityScope(level=OpportunityScopeLevel.SYSTEM),
    "system",
)


def _bounded_verbatim_excerpt(
    body: str,
    candidate_fields: tuple[ReplyFieldSnapshot, ...],
) -> str:
    """返回至多 500 个 Unicode code point 的原文子串。

    字段 quote 已由分类护栏验证；这里仍复核它存在于当前重读正文，避免原件与
    分类快照漂移。按字段稳定顺序取首个有效 quote，否则从正文首个非空字符起
    截取。只裁边、不改写内容，因此结果仍是可核对的逐字证据。
    """
    if not isinstance(body, str) or not body.strip():
        raise ValidationError("回复消息正文无效")
    for field in candidate_fields:
        quote = field.quote
        if len(quote) > MAX_REPLY_FIELD_QUOTE_CODEPOINTS:
            raise ValidationError("回复字段逐字证据超长")
        if quote not in body:
            continue
        bounded_quote = quote.rstrip()
        if bounded_quote.strip():
            return bounded_quote
    start = next(index for index, char in enumerate(body) if not char.isspace())
    excerpt = body[start : start + MAX_REPLY_FIELD_QUOTE_CODEPOINTS].rstrip()
    if not excerpt:
        raise ValidationError("回复消息摘录无效")
    return excerpt


def _enrollment_actor(enrollment_id: EnrollmentId) -> OutreachActor:
    return OutreachActor(
        "system:reply-qualification",
        OutreachScope(
            level=OutreachScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({enrollment_id}),
        ),
        "system",
    )


def _identity_actor(identity_id: SendingIdentityId) -> OutreachActor:
    return OutreachActor(
        "system:reply-qualification",
        OutreachScope(
            level=OutreachScopeLevel.SYSTEM,
            allowed_sending_identity_ids=frozenset({identity_id}),
        ),
        "system",
    )


def _sending_identity_actor(identity_id: SendingIdentityId) -> SendingIdentityActor:
    return SendingIdentityActor(
        "system:reply-qualification",
        SendingIdentityScope(
            level=SendingIdentityScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({identity_id}),
        ),
        "system",
    )


@dataclass(frozen=True)
class ReplyFieldSnapshot:
    """持久化分类中的字段候选；quote 已由模型输出护栏逐字验证。"""

    field: str
    value: str
    quote: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.field, self.value, self.quote)
        ):
            raise ValidationError("回复字段证据无效")
        if len(self.quote) > MAX_REPLY_FIELD_QUOTE_CODEPOINTS:
            raise ValidationError("回复字段逐字证据超长")


@dataclass(frozen=True)
class ReplyEvidenceSnapshot:
    """按 message_id 重读的 tenant-bound 分类证据，不进入 workflow context。"""

    message_id: MessageId
    account_id: ProspectAccountId
    category: str
    classified_by: str
    classified_at: datetime
    raw_artifact_ref: str
    outbound_message_id: OutboundMessageId
    candidate_fields: tuple[ReplyFieldSnapshot, ...]


@dataclass(frozen=True)
class ReplyBusinessFacts:
    """回复动作需要的业务关联；由 tenant-bound 上层投影提供，不做猜测。"""

    need_id: ValidatedNeedId | str | None
    hypothesis_id: NeedHypothesisId | str | None
    opportunity_id: OpportunityId | str | None
    account_name: str
    country: str
    why_valuable: str
    how_we_found_them: str | None = None
    validated_need_summary: str | None = None
    missing_information: tuple[str, ...] = ()
    already_sent: tuple[str, ...] = ()
    commitments_made: tuple[str, ...] = ()
    suggested_next_step: str | None = None


@runtime_checkable
class ReplyEvidenceReader(Protocol):
    """分类证据公共读缝；实现必须强制 tenant + message 过滤。"""

    async def load(
        self, tenant_id: TenantId, message_id: MessageId
    ) -> ReplyEvidenceSnapshot | None: ...


@runtime_checkable
class ReplyBusinessFactsReader(Protocol):
    """需求/机会/账户关联读缝；缺任何必要映射返回 None，调用方 fail-closed。"""

    async def load(
        self, tenant_id: TenantId, context: ReplyActionContext
    ) -> ReplyBusinessFacts | None: ...


@runtime_checkable
class ReplyOpportunityIntake(Protocol):
    """首次需求晋升后的机会创建与真实 owner 分配端口。"""

    async def create_and_assign(
        self,
        tenant_id: TenantId,
        context: ReplyActionContext,
        hypothesis_id: NeedHypothesisId,
        need_id: ValidatedNeedId,
        evidence: ReplyEvidenceSnapshot,
    ) -> OpportunityId | None: ...


def _candidate_business_value(field: ReplyFieldSnapshot) -> object:
    """把模型字符串边界中的结构化金额恢复为确定性业务形状。"""
    if field.field != "target_price":
        return field.value
    try:
        value = json.loads(field.value)
    except json.JSONDecodeError as exc:
        raise ValidationError("回复目标价格式无效") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"amount", "currency"}
        or not isinstance(value["amount"], str)
        or not isinstance(value["currency"], str)
    ):
        raise ValidationError("回复目标价格式无效")
    return value


class ComposedReplyActionPorts:
    """把字段、接管与投递反馈动作接到真实公共域服务。

    qualification/future/follow-up/referral 通过 conversations 公共 API 写入
    metadata-only owner queue；referral 只排队核验，不自动 enrollment。
    """

    def __init__(
        self,
        *,
        tenant_id: TenantId,
        evidence: ReplyEvidenceReader,
        business: ReplyBusinessFactsReader,
        content: MessageContentReader,
        demand: DemandService,
        opportunities: OpportunityService,
        outreach: OutreachService,
        sending_identities: SendingIdentityService,
        conversations: ConversationService,
        opportunity_intake: ReplyOpportunityIntake | None = None,
    ) -> None:
        if (
            not isinstance(tenant_id, str)
            or not tenant_id.strip()
            or not isinstance(evidence, ReplyEvidenceReader)
            or not isinstance(business, ReplyBusinessFactsReader)
            or not isinstance(content, MessageContentReader)
            or not callable(getattr(demand, "update_need_fields", None))
            or not callable(getattr(demand, "promote_to_validated", None))
            or not callable(getattr(opportunities, "request_handoff", None))
            or any(
                not callable(getattr(outreach, method, None))
                for method in (
                    "get_enrollment",
                    "resolve_delivery_feedback",
                    "apply_hard_bounce",
                    "apply_complaint",
                )
            )
            or not callable(getattr(sending_identities, "record_delivery_event", None))
            or not callable(getattr(conversations, "enqueue_reply_work_action", None))
        ):
            raise ValidationError("回复动作生产组合依赖未完整配置")
        self._tenant_id = tenant_id
        self._evidence = evidence
        self._business = business
        self._content = content
        self._demand = demand
        self._opportunities = opportunities
        self._outreach = outreach
        self._sending_identities = sending_identities
        self._conversations = conversations
        self._opportunity_intake = opportunity_intake

    def _require_call(
        self,
        tenant_id: TenantId,
        context: ReplyActionContext,
        idempotency_key: str,
        action: str,
    ) -> None:
        if tenant_id != self._tenant_id:
            raise TenantIsolationViolation("回复动作跨租户执行")
        if not isinstance(context, ReplyActionContext):
            raise ValidationError("回复动作关联上下文无效")
        expected = f"reply:{action}:{context.message_id}"
        if idempotency_key != expected:
            raise ValidationError("回复动作幂等键无效")

    async def _facts(
        self, tenant_id: TenantId, context: ReplyActionContext
    ) -> tuple[ReplyEvidenceSnapshot, ReplyBusinessFacts]:
        evidence = await self._evidence.load(tenant_id, context.message_id)
        business = await self._business.load(tenant_id, context)
        if evidence is None or evidence.message_id != context.message_id:
            raise ValidationError("回复分类证据不存在")
        if evidence.outbound_message_id != context.outbound_message_id:
            raise ValidationError("回复出站消息关联不匹配")
        if evidence.account_id != context.account_id:
            raise ValidationError("回复企业关联不匹配")
        if business is None:
            raise ValidationError("回复业务关联不存在")
        return evidence, business

    async def _feedback_target(
        self,
        tenant_id: TenantId,
        context: ReplyActionContext,
        *,
        category: str,
    ) -> tuple[ReplyEvidenceSnapshot, DeliveryFeedbackTarget]:
        evidence = await self._evidence.load(tenant_id, context.message_id)
        if (
            evidence is None
            or evidence.message_id != context.message_id
            or evidence.category != category
            or not isinstance(evidence.classified_at, datetime)
            or evidence.classified_at.tzinfo is None
            or evidence.classified_at.utcoffset()
            != UTC.utcoffset(evidence.classified_at)
        ):
            raise ValidationError("回复投递反馈证据无效")
        enrollment = await self._outreach.get_enrollment(
            tenant_id,
            context.enrollment_id,
            actor=_enrollment_actor(context.enrollment_id),
        )
        identity_id = getattr(enrollment, "sending_identity_id", None)
        if (
            getattr(enrollment, "tenant_id", None) != tenant_id
            or getattr(enrollment, "enrollment_id", None) != context.enrollment_id
            or getattr(enrollment, "account_id", None) != context.account_id
            or getattr(enrollment, "contact_point_id", None) != context.contact_point_id
            or not isinstance(identity_id, str)
            or not identity_id
        ):
            raise ValidationError("回复 Enrollment 关联不匹配")
        target = await self._outreach.resolve_delivery_feedback(
            tenant_id,
            DeliveryCorrelationLookup(
                deterministic_message_id=str(context.outbound_message_id)
            ),
            actor=_identity_actor(SendingIdentityId(identity_id)),
        )
        if (
            not isinstance(target, DeliveryFeedbackTarget)
            or target.tenant_id != tenant_id
            or target.enrollment_id != context.enrollment_id
            or target.account_id != context.account_id
            or target.contact_point_id != context.contact_point_id
            or target.sending_identity_id != identity_id
        ):
            raise ValidationError("回复投递反馈关联不匹配")
        return evidence, target

    @staticmethod
    def _feedback_event_id(
        tenant_id: TenantId,
        context: ReplyActionContext,
        action: str,
    ) -> str:
        material = (
            f"reply-action-v1\x00{tenant_id}\x00{action}\x00"
            f"{context.message_id}\x00{context.outbound_message_id}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    async def _record_reputation(
        self,
        tenant_id: TenantId,
        target: DeliveryFeedbackTarget,
        evidence: ReplyEvidenceSnapshot,
        event_id: str,
        event_type: DeliveryEventType,
    ) -> None:
        await self._sending_identities.record_delivery_event(
            tenant_id,
            target.sending_identity_id,
            DeliveryEventRecord(
                tenant_id=tenant_id,
                identity_id=target.sending_identity_id,
                event_type=event_type,
                occurred_at=evidence.classified_at,
                dedup_key=IdempotencyKey(event_id),
                source_ref=f"replyfb_{event_id[:56]}",
            ),
            actor=_sending_identity_actor(target.sending_identity_id),
        )

    async def extract_need_fields(
        self,
        tenant_id: TenantId,
        context: ReplyActionContext,
        idempotency_key: str,
    ) -> None:
        self._require_call(tenant_id, context, idempotency_key, "extract_need_fields")
        evidence, business = await self._facts(tenant_id, context)
        if not evidence.candidate_fields:
            raise ValidationError("回复没有可应用的字段证据")
        fields = {
            item.field: {
                "value": _candidate_business_value(item),
                "quote": item.quote,
                "extracted_by": evidence.classified_by,
            }
            for item in evidence.candidate_fields
        }
        hypothesis_id = (
            NeedHypothesisId(str(business.hypothesis_id))
            if business.hypothesis_id is not None
            else None
        )
        if business.need_id is not None:
            need_id = ValidatedNeedId(str(business.need_id))
            current_need = await self._demand.get_need(tenant_id, need_id)
            if (
                current_need.need_id != need_id
                or current_need.account_id != context.account_id
            ):
                raise ValidationError("回复需求关联不匹配")
            category_field = fields.pop("product_category", None)
            if (
                category_field is not None
                and category_field["value"] != current_need.product_category
            ):
                raise ValidationError("回复产品类别与不可变需求不一致，需人工核对")
            if fields:
                await self._demand.update_need_fields(
                    tenant_id,
                    need_id,
                    fields,
                    context.message_id,
                    None,
                )
            if self._opportunity_intake is None:
                return
        else:
            if hypothesis_id is None:
                raise ValidationError("回复需求关联不存在")
            record_reply_evidence = getattr(
                self._demand, "record_customer_reply_evidence", None
            )
            if not callable(record_reply_evidence):
                raise ValidationError("回复客户证据组合未配置")
            await record_reply_evidence(
                tenant_id,
                CustomerReplyEvidenceClaim(
                    hypothesis_id=hypothesis_id,
                    source_message_id=context.message_id,
                    outbound_message_id=context.outbound_message_id,
                    enrollment_id=context.enrollment_id,
                    account_id=context.account_id,
                    contact_point_id=context.contact_point_id,
                ),
            )
            need_id = await self._demand.promote_to_validated(
                tenant_id,
                hypothesis_id,
                context.message_id,
                fields,
                None,
            )
        if business.hypothesis_id is None:
            raise ValidationError("回复需求关联不存在")
        assert hypothesis_id is not None
        if self._opportunity_intake is None:
            raise ValidationError("回复机会创建组合未配置")
        await self._opportunity_intake.create_and_assign(
            tenant_id,
            context,
            hypothesis_id,
            ValidatedNeedId(str(need_id)),
            evidence,
        )

    async def request_handoff(
        self,
        tenant_id: TenantId,
        context: ReplyActionContext,
        idempotency_key: str,
    ) -> None:
        self._require_call(tenant_id, context, idempotency_key, "handoff")
        evidence, business = await self._facts(tenant_id, context)
        if business.opportunity_id is None:
            if business.need_id is not None and business.missing_information:
                await self.create_follow_up(
                    tenant_id, context, f"reply:create_follow_up:{context.message_id}"
                )
                return
            raise ValidationError("回复机会关联不存在")
        content = await self._content.load(tenant_id, context.message_id)
        if content is None:
            raise ValidationError("回复消息原文不可读")
        segments = content.evidence_segments if content.projected else (content.body,)
        if not segments:
            raise ValidationError("回复当前表达不可读")
        source = next(
            (
                segment
                for candidate in evidence.candidate_fields
                for segment in segments
                if candidate.quote in segment
            ),
            next((segment for segment in segments if segment.strip()), ""),
        )
        verbatim = _bounded_verbatim_excerpt(source, evidence.candidate_fields)
        trigger = {
            "requests_materials": "materials_requested",
            "requests_quote": "quote_requested",
            "requests_sample": "sample_requested",
            "provides_specification": "specification_file_received",
            "complaint": "complaint",
        }.get(evidence.category, "agent_low_confidence")
        await self._opportunities.request_handoff(
            tenant_id,
            HandoffCreateRequest(
                opportunity_id=str(business.opportunity_id),
                trigger=trigger,
                account_name=business.account_name,
                country=business.country,
                why_valuable=business.why_valuable,
                customer_verbatim=verbatim,
                customer_verbatim_provenance=Provenance(
                    source_type=SourceType.CONVERSATION,
                    source_id=str(context.message_id),
                    extracted_by=evidence.classified_by,
                    extracted_at=evidence.classified_at,
                    source_quote=verbatim,
                ),
                how_we_found_them=business.how_we_found_them,
                validated_need_summary=business.validated_need_summary,
                missing_information=list(business.missing_information),
                already_sent=list(business.already_sent),
                commitments_made=list(business.commitments_made),
                suggested_next_step=business.suggested_next_step,
                evidence_links=[evidence.raw_artifact_ref],
            ),
            actor=_SYSTEM_ACTOR,
        )

    async def route_bounce(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None:
        self._require_call(tenant_id, context, idempotency_key, "route_bounce")
        evidence, target = await self._feedback_target(
            tenant_id, context, category="bounce"
        )
        event_id = self._feedback_event_id(tenant_id, context, "route_bounce")
        await self._record_reputation(
            tenant_id,
            target,
            evidence,
            event_id,
            DeliveryEventType.HARD_BOUNCED,
        )
        await self._outreach.apply_hard_bounce(
            tenant_id,
            target,
            event_id,
            evidence.classified_at,
            actor=_identity_actor(target.sending_identity_id),
        )

    async def record_complaint(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None:
        self._require_call(tenant_id, context, idempotency_key, "record_complaint")
        evidence, target = await self._feedback_target(
            tenant_id, context, category="complaint"
        )
        event_id = self._feedback_event_id(tenant_id, context, "record_complaint")
        await self._record_reputation(
            tenant_id,
            target,
            evidence,
            event_id,
            DeliveryEventType.COMPLAINT,
        )
        await self._outreach.apply_complaint(
            tenant_id,
            target,
            event_id,
            evidence.classified_at,
            actor=_identity_actor(target.sending_identity_id),
        )

    async def start_qualification(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None:
        self._require_call(tenant_id, context, idempotency_key, "start_qualification")
        await self._enqueue_work(
            tenant_id,
            context,
            idempotency_key,
            ReplyWorkAction.START_QUALIFICATION,
        )

    async def mark_future_restart(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None:
        self._require_call(tenant_id, context, idempotency_key, "mark_future_restart")
        await self._enqueue_work(
            tenant_id,
            context,
            idempotency_key,
            ReplyWorkAction.MARK_FUTURE_RESTART,
        )

    async def create_follow_up(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None:
        self._require_call(tenant_id, context, idempotency_key, "create_follow_up")
        await self._enqueue_work(
            tenant_id,
            context,
            idempotency_key,
            ReplyWorkAction.CREATE_FOLLOW_UP,
        )

    async def intake_new_contact(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None:
        self._require_call(tenant_id, context, idempotency_key, "intake_new_contact")
        await self._enqueue_work(
            tenant_id,
            context,
            idempotency_key,
            ReplyWorkAction.INTAKE_NEW_CONTACT,
        )

    async def _enqueue_work(
        self,
        tenant_id: TenantId,
        context: ReplyActionContext,
        idempotency_key: str,
        action: ReplyWorkAction,
    ) -> None:
        await self._conversations.enqueue_reply_work_action(
            tenant_id,
            ReplyWorkActionRequest(
                message_id=context.message_id,
                outbound_message_id=context.outbound_message_id,
                enrollment_id=context.enrollment_id,
                account_id=context.account_id,
                contact_point_id=context.contact_point_id,
                action=action,
                idempotency_key=IdempotencyKey(idempotency_key),
            ),
        )
