"""outreach_campaign 步骤 handler：只编排，业务规则全在 outreach 域服务。

LLM/IO 全部在 handler 内；handler 必须幂等（scheduler 重扫、崩溃恢复会
重复调用）。草稿当前用确定性模板（切片 6 才引入模型草稿），存 run.context。
客户可见 subject/body 全英文（内部日志/文档用中文）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from domains.outreach.permissions import Actor as OutreachActor
from domains.outreach.permissions import OutreachScope
from domains.outreach.permissions import ScopeLevel as OutreachScopeLevel
from domains.outreach.schemas import (
    DraftContent,
    EnrollmentState,
    MessageAttemptId,
    SendAuthorization,
    SendDecision,
    SendDenialReason,
)
from domains.outreach.service import OutreachService
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    CampaignId,
    EnrollmentId,
    IdempotencyKey,
    SendingIdentityId,
    TenantId,
)
from workflows.engine.runner import WorkflowRun

_DAYS_TO_SECONDS = 86_400

_DISCOVERY_SUBJECT = "Quick question about your sourcing needs"
_DISCOVERY_BODY = (
    "What products, components, packaging, or supply areas are currently the "
    "hardest for you to find the right options?"
)
_PRESENTATION_SUBJECT = "Following up on our last conversation"
_PRESENTATION_BODY = (
    "We have put together some thoughts on the needs you mentioned. We would "
    "like to better understand your priorities."
)
_FOLLOW_UP_SUBJECT = "Checking in on your sourcing priorities"
_FOLLOW_UP_BODY = (
    "I wanted to check whether there is any update on the needs we discussed. "
    "Happy to keep the conversation going."
)


class CampaignEmailSender(Protocol):
    """发送适配器：真实实现包住 tool_gateway email.send（claim→发送→记账）。"""

    async def send(
        self, *, tenant_id: TenantId, authorization: SendAuthorization
    ) -> str:
        """返回 provider reference；可重试失败抛 TransientError，永久失败抛其他。"""
        ...


def _context(run: WorkflowRun) -> tuple[EnrollmentId, CampaignId]:
    """必填键校验；流程上下文会随步骤累积 draft/authorization 等产物。"""
    enrollment_id = run.context.get("enrollment_id")
    campaign_id = run.context.get("campaign_id")
    if (
        not isinstance(enrollment_id, str)
        or not enrollment_id.startswith("enr_")
        or enrollment_id != run.subject_ref
        or not isinstance(campaign_id, str)
        or not campaign_id.startswith("cmp_")
    ):
        raise ValidationError("触达 workflow context 无效")
    return EnrollmentId(enrollment_id), CampaignId(campaign_id)


def _enrollment_actor(enrollment_id: EnrollmentId) -> OutreachActor:
    """SYSTEM 作用域必须收窄单一资源集：enrollment 侧调用用它。"""
    return OutreachActor(
        "system:outreach-campaign",
        OutreachScope(
            level=OutreachScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({enrollment_id}),
        ),
        "system",
    )


def _intent_templates(intent: str) -> tuple[str, str]:
    templates = {
        "discovery": (_DISCOVERY_SUBJECT, _DISCOVERY_BODY),
        "presentation": (_PRESENTATION_SUBJECT, _PRESENTATION_BODY),
        "follow_up": (_FOLLOW_UP_SUBJECT, _FOLLOW_UP_BODY),
    }
    if intent not in templates:
        raise ValidationError("未知序列意图")
    return templates[intent]


class DraftContentStep:
    """按当前步骤意图生成确定性英文草稿；终态 Enrollment 直接 complete。

    末步 wait_for_reply 超时后（或回复/抑制已被域终态化）会再次回到本步：
    域只回答「当前是否可发」，序列是否走完由 Enrollment 状态表达，这里只读
    不判断业务规则。
    """

    def __init__(self, outreach: OutreachService) -> None:
        self._outreach = outreach

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        enrollment_id, _campaign_id = _context(run)
        actor = _enrollment_actor(enrollment_id)
        enrollment = await self._outreach.get_enrollment(
            run.tenant_id, enrollment_id, actor=actor
        )
        if enrollment.state not in {
            EnrollmentState.ENROLLED,
            EnrollmentState.IN_SEQUENCE,
        }:
            # 序列已终态（completed/replied/suppressed…）：本 run 正常收束
            return ("complete", None, {})
        spec = await self._outreach.get_sequence_step_spec(
            run.tenant_id, enrollment_id, actor=actor
        )
        subject, body = _intent_templates(spec.intent.value)
        return (
            "advance",
            "prepare_send",
            {
                "draft": {"subject": subject, "body": body},
                "wait_days": spec.wait_days,
            },
        )


class PrepareSendStep:
    """域综合检查；终态原因 → complete，其余拒绝 → wait_event 或确定性超时复查。

    身份熔断/额度满/Campaign 非活跃/未到期都是瞬态条件：进入等待
    (waiting_event, SendingIdentityActivated)，事件到达立即唤醒重查；
    wait 时排定的确定性超时（1 天）只是兜底，绝不指数退避轮询。
    """

    def __init__(self, outreach: OutreachService) -> None:
        self._outreach = outreach

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        enrollment_id, _campaign_id = _context(run)
        actor = _enrollment_actor(enrollment_id)
        draft = run.context.get("draft")
        if (
            not isinstance(draft, dict)
            or not isinstance(draft.get("subject"), str)
            or not isinstance(draft.get("body"), str)
        ):
            raise ValidationError("触达草稿缺失")
        decision: SendDecision = await self._outreach.prepare_send(
            run.tenant_id,
            enrollment_id,
            DraftContent(subject=draft["subject"], body=draft["body"]),
            actor=actor,
        )
        if decision.authorized and decision.authorization is not None:
            authorization = decision.authorization
            return (
                "advance",
                "send",
                {
                    "authorization": {
                        "tenant_id": str(authorization.tenant_id),
                        "enrollment_id": str(authorization.enrollment_id),
                        "campaign_id": str(authorization.campaign_id),
                        "campaign_version": authorization.campaign_version,
                        "step_number": authorization.step_number,
                        "sending_identity_id": str(
                            authorization.sending_identity_id
                        ),
                        "attempt_id": str(authorization.attempt_id),
                        "idempotency_key": str(authorization.idempotency_key),
                        "subject": authorization.subject,
                        "body": authorization.body,
                    }
                },
            )
        if decision.denial_reason in {
            SendDenialReason.REPLY_RECEIVED,
            SendDenialReason.SUPPRESSED,
            SendDenialReason.ENROLLMENT_TERMINAL,
        }:
            return ("complete", None, {})
        return ("wait", None, {})


class SendStep:
    """经窄发送适配器执行；适配器包住 tool_gateway email.send。"""

    def __init__(self, sender: CampaignEmailSender) -> None:
        self._sender = sender

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        _enrollment_id, _campaign_id = _context(run)
        raw = run.context.get("authorization")
        if not isinstance(raw, dict):
            raise ValidationError("触达授权缺失")
        authorization = SendAuthorization(
            tenant_id=TenantId(raw["tenant_id"]),
            enrollment_id=EnrollmentId(raw["enrollment_id"]),
            campaign_id=CampaignId(raw["campaign_id"]),
            campaign_version=int(raw["campaign_version"]),
            step_number=int(raw["step_number"]),
            sending_identity_id=SendingIdentityId(raw["sending_identity_id"]),
            attempt_id=MessageAttemptId(raw["attempt_id"]),
            idempotency_key=IdempotencyKey(raw["idempotency_key"]),
            subject=raw["subject"],
            body=raw["body"],
        )
        provider_ref = await self._sender.send(
            tenant_id=run.tenant_id, authorization=authorization
        )
        return ("advance", "record_sent", {"provider_ref": provider_ref})


class RecordSentStep:
    """域内幂等记录已发送并推进步数；按域 next_send_at 排定 wait_for_reply 超时。

    超时 = 该 enrollment 当前步骤 wait_days（域在 record_sent 后写
    next_send_at = now + 下一步 wait_days）；末步无下一步 → 用本步
    wait_days 作为回复窗口，超时后由 draft 终态检查收束为 complete。
    """

    def __init__(
        self, outreach: OutreachService, now: Callable[[], datetime]
    ) -> None:
        self._outreach = outreach
        self._now = now

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        enrollment_id, _campaign_id = _context(run)
        actor = _enrollment_actor(enrollment_id)
        raw = run.context.get("authorization")
        provider_ref = run.context.get("provider_ref")
        if not isinstance(raw, dict) or not isinstance(provider_ref, str):
            raise ValidationError("触达发送结果缺失")
        attempt_id = MessageAttemptId(raw["attempt_id"])
        await self._outreach.record_sent(
            run.tenant_id, attempt_id, provider_ref, actor=actor
        )
        enrollment = await self._outreach.get_enrollment(
            run.tenant_id, enrollment_id, actor=actor
        )
        wait_days = run.context.get("wait_days")
        if enrollment.next_send_at is not None:
            delta = (enrollment.next_send_at - self._now()).total_seconds()
            wait_seconds = int(delta) + 1
        elif isinstance(wait_days, int) and not isinstance(wait_days, bool) and wait_days > 0:
            wait_seconds = wait_days * _DAYS_TO_SECONDS
        else:
            raise ValidationError("触达序列等待窗口缺失")
        return ("advance", "wait_for_reply", {"timeout_seconds": wait_seconds})


class WaitForReplyStep:
    """WAITING_EVENT(ReplyReceived)：回复到达 → complete（reply 路径接管）。

    引擎只在 ``deliver_event(ReplyReceived)`` 时调用本 handler；超时推进由
    on_timeout（draft_content）承担，本步不轮询。
    """

    def __init__(self, outreach: OutreachService) -> None:
        self._outreach = outreach

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        _enrollment_id, _campaign_id = _context(run)
        return ("complete", None, {})
