"""outreach_campaign 步骤 handler：只编排，业务规则全在 outreach 域服务。

LLM/IO 全部在 handler 内；handler 必须幂等（scheduler 重扫、崩溃恢复会
重复调用）。草稿当前用确定性模板（切片 6 才引入模型草稿），存 run.context。
"""

from __future__ import annotations

from typing import Any, Protocol

from domains.outreach.permissions import Actor as OutreachActor
from domains.outreach.permissions import OutreachScope
from domains.outreach.permissions import ScopeLevel as OutreachScopeLevel
from domains.outreach.schemas import (
    DraftContent,
    MessageAttemptId,
    SendAuthorization,
    SendDecision,
    SendDenialReason,
)
from domains.outreach.service import OutreachService
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    CampaignId,
    EnrollmentId,
    IdempotencyKey,
    SendingIdentityId,
    TenantId,
)
from workflows.engine.runner import WorkflowRun

_DISCOVERY_SUBJECT = "供应与采购需求沟通"
_DISCOVERY_BODY = (
    "你们目前在哪些产品、零部件、包装或供应方面最难找到合适的选择？"
)
_PRESENTATION_SUBJECT = "关于上次沟通的跟进"
_PRESENTATION_BODY = (
    "上次聊到的需求方向，我们整理了一些思路，想进一步了解你们的优先事项。"
)
_FOLLOW_UP_SUBJECT = "关于上次沟通的跟进"
_FOLLOW_UP_BODY = (
    "上次提到的需求，不知你们近期是否有新的进展？想保持同步。"
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
    """按当前步骤意图生成确定性草稿；产出进 run.context['draft']。"""

    def __init__(self, outreach: OutreachService) -> None:
        self._outreach = outreach

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        enrollment_id, _campaign_id = _context(run)
        spec = await self._outreach.get_sequence_step_spec(
            run.tenant_id, enrollment_id, actor=_enrollment_actor(enrollment_id)
        )
        subject, body = _intent_templates(spec.intent.value)
        return (
            "advance",
            "prepare_send",
            {"draft": {"subject": subject, "body": body}},
        )


class PrepareSendStep:
    """域综合检查；回复/终态 → complete，其余拒绝 → 引擎退避重试。"""

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
        # 额度/未到期/Campaign 非活跃/身份不可用：引擎退避重试，恢复后继续
        raise TransientError("序列发送暂不可执行")


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
    """域内幂等记录已发送并推进步数（gateway 记账后调用为 no-op 级安全）。"""

    def __init__(self, outreach: OutreachService) -> None:
        self._outreach = outreach

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
        # 每步一个 run：本步完成，等待由 Enrollment.next_send_at + 驱动负责
        return ("complete", None, {})
