"""reply_qualification 步骤 handler：只编排，业务规则全在域服务。

classify：经只读 ``MessageContentReader`` 端口按 message_id 加载
subject/body（仅内存，不落库/事件/日志——artifact_store 硬边界 4），先过
注入的 ``InputContentGuard``（硬边界 1），再交给注入的 ``ReplyClassifier``
分类；随后 conversations.record_classification 落库（带 outbound_message_id
关联 + 模型版本）并返回 REPLY_ACTIONS 动作序列——动作决定在域，模型/
ChangeSet 永不携带动作。AUTO_REPLY 短路 complete。

**workflow context 携带的 category 一律 fail-closed 拒绝**：context 自报类别
无 Provenance、可被内部调用方伪造（"unsubscribe" 直接触发停序列/抑制），
违反事实/推断分离与关键结论 provenance。预分类只允许来自 tenant-bound 的
持久化分类查询 + 显式 provenance（后续切片），绝不信任 context。

apply_actions：按动作序列经域服务逐个幂等执行；未接线动作显式
fail-closed（run FAILED 可观测，分类不丢）。handler 绑定单一 tenant：
``run.tenant_id`` 与绑定 tenant 不一致即 ``TenantIsolationViolation``
fail-closed（硬边界 8）。

subject 契约：内容视图允许无主题（真实邮件常缺），进模型前归一为固定
非敏感占位 ``(no subject)``，模型侧不会因缺主题运行时失败。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from agent_runtime.qualification_agent.agent import ReplyClassifier
from domains.conversations.schemas import ReplyCategory, ReplyFieldEvidence
from domains.conversations.service import ConversationService
from domains.outreach.permissions import (
    Actor as OutreachActor,
)
from domains.outreach.permissions import (
    OutreachScope,
)
from domains.outreach.permissions import (
    ScopeLevel as OutreachScopeLevel,
)
from domains.outreach.schemas import (
    EnrollmentStopReason,
    SuppressionReason,
    SuppressionRequest,
    SuppressionTarget,
)
from domains.outreach.service import OutreachService
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    EnrollmentId,
    IdempotencyKey,
    MessageId,
    OutboundMessageId,
    ProspectAccountId,
    TenantId,
)
from workflows.engine.runner import WorkflowRun
from workflows.reply_qualification.ports import (
    InputContentGuard,
    MessageContentReader,
    ReplyActionContext,
    ReplyActionPorts,
)

_SYSTEM_ACTOR_ID = "system:reply-qualification"


class ReplyActionNotWiredError(ValidationError):
    """REPLY_ACTIONS 中的动作尚无域服务接线：显式失败，绝不静默跳过。"""


def _enrollment_actor(enrollment_id: EnrollmentId) -> OutreachActor:
    return OutreachActor(
        _SYSTEM_ACTOR_ID,
        OutreachScope(
            level=OutreachScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({enrollment_id}),
        ),
        "system",
    )


def _suppression_actor(target: SuppressionTarget) -> OutreachActor:
    canonical = target.canonical_id
    return OutreachActor(
        _SYSTEM_ACTOR_ID,
        OutreachScope(
            level=OutreachScopeLevel.SYSTEM,
            allowed_suppression_targets=frozenset({canonical}),
        ),
        "system",
    )


class ClassifyStep:
    """分类（agent）→ conversations 落库 → 动作进 context。

    上下文只携带 typed ID；正文/主题经 ``MessageContentReader`` 端口只读
    加载，只存在进程内存，绝不写入 workflow 表、事件或日志。context 中的
    ``category`` 键视为伪造尝试，fail-closed 拒绝（见模块 docstring）。
    """

    def __init__(
        self,
        classifier: ReplyClassifier,
        content_reader: MessageContentReader,
        input_guard: InputContentGuard,
        conversations: ConversationService,
    ) -> None:
        self._classifier = classifier
        self._content_reader = content_reader
        self._input_guard = input_guard
        self._conversations = conversations

    @staticmethod
    def _context(run: WorkflowRun) -> tuple[MessageId, OutboundMessageId | None]:
        raw = run.context
        raw_message_id = raw.get("message_id")
        raw_outbound = raw.get("outbound_message_id")
        if not isinstance(raw_message_id, str) or not raw_message_id:
            raise ValidationError("回复 workflow 缺少 message_id")
        if raw_outbound is not None and (
            not isinstance(raw_outbound, str) or not raw_outbound.strip()
        ):
            raise ValidationError("回复 workflow outbound 关联无效")
        return (
            MessageId(raw_message_id),
            OutboundMessageId(raw_outbound) if raw_outbound is not None else None,
        )

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        message_id, outbound_message_id = self._context(run)
        if run.context.get("category") is not None:
            # fail-closed：context 自报类别可被内部调用方伪造，且无 Provenance，
            # 绝不信任；预分类只允许来自 tenant-bound 持久化查询（后续切片）
            raise ValidationError("workflow context 不得携带自报类别")
        content = await self._content_reader.load(run.tenant_id, message_id)
        if content is None:
            raise ValidationError("回复消息原文不可读")
        # 硬边界 1：凭证/API key/token-like 文本绝不进模型——先过输入护栏
        self._input_guard.check(subject=content.subject, body=content.body)
        result = await self._classifier.classify(
            message={
                "message_id": str(message_id),
                # 无主题 → 固定非敏感占位，模型侧永不因缺主题失败
                "subject": content.subject or "(no subject)",
                "body": content.body,
            }
        )
        category = result.category
        classified_by = self._classifier.model
        actions = await self._conversations.record_classification(
            run.tenant_id,
            message_id,
            category,
            classified_by=classified_by,
            outbound_message_id=outbound_message_id,
            candidate_fields=tuple(
                ReplyFieldEvidence(item.field, item.value, item.quote)
                for item in result.candidate_fields
            ),
        )
        if category is ReplyCategory.AUTO_REPLY:
            # 自动回复不算回复：停序列/动作一律不触发
            return ("complete", None, {})
        return (
            "advance",
            "apply_actions",
            {"category": category.value, "actions": list(actions)},
        )


class ApplyActionsStep:
    """按 REPLY_ACTIONS 经域服务逐个幂等执行；未接线动作显式 fail-closed。"""

    def __init__(
        self,
        outreach: OutreachService,
        tenant_id: TenantId,
        now: Callable[[], datetime],
        action_ports: ReplyActionPorts | None = None,
    ) -> None:
        self._outreach = outreach
        self._tenant_id = tenant_id
        self._now = now
        self._action_ports = action_ports

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        if run.tenant_id != self._tenant_id:
            # 硬边界 8：handler 绑定单一 tenant，跨租户执行一律 fail-closed
            raise TenantIsolationViolation("回复动作跨租户执行")
        actions = run.context.get("actions")
        category = run.context.get("category")
        if not isinstance(actions, list) or not isinstance(category, str):
            raise ValidationError("回复动作上下文缺失")
        for action in actions:
            if not isinstance(action, str):
                raise ValidationError("回复动作无效")
            await self._apply(run, action, category)
        return ("complete", None, {})

    async def _apply(self, run: WorkflowRun, action: str, category: str) -> None:
        if action == "stop_sequence":
            await self._stop_sequence(run)
            return
        if action == "suppress":
            await self._suppress(run, category)
            return
        methods = {
            "route_bounce": "route_bounce",
            "record_complaint": "record_complaint",
            "handoff": "request_handoff",
            "start_qualification": "start_qualification",
            "extract_need_fields": "extract_need_fields",
            "mark_future_restart": "mark_future_restart",
            "create_follow_up": "create_follow_up",
            "intake_new_contact": "intake_new_contact",
        }
        method_name = methods.get(action)
        if method_name is None or self._action_ports is None:
            raise ReplyActionNotWiredError(f"回复动作未接线：{action}")
        method = getattr(self._action_ports, method_name, None)
        if not callable(method):
            raise ReplyActionNotWiredError(f"回复动作未接线：{action}")
        context = self._action_context(run)
        await method(
            run.tenant_id,
            context,
            f"reply:{action}:{context.message_id}",
        )

    @staticmethod
    def _action_context(run: WorkflowRun) -> ReplyActionContext:
        values = {
            "message_id": run.context.get("message_id"),
            "outbound_message_id": run.context.get("outbound_message_id"),
            "enrollment_id": run.context.get("enrollment_id"),
            "account_id": run.context.get("account_id"),
            "contact_point_id": run.context.get("contact_point_id"),
        }
        prefixes = {
            "message_id": "msg_",
            "enrollment_id": "enr_",
            "account_id": "acc_",
            "contact_point_id": "cp_",
        }
        if any(
            not isinstance(value, str) or not value.strip()
            for value in values.values()
        ) or any(
            not str(values[name]).startswith(prefix)
            for name, prefix in prefixes.items()
        ):
            raise ValidationError("回复动作关联上下文无效")
        return ReplyActionContext(
            message_id=MessageId(str(values["message_id"])),
            outbound_message_id=OutboundMessageId(
                str(values["outbound_message_id"])
            ),
            enrollment_id=EnrollmentId(str(values["enrollment_id"])),
            account_id=ProspectAccountId(str(values["account_id"])),
            contact_point_id=ContactPointId(str(values["contact_point_id"])),
        )

    async def _stop_sequence(self, run: WorkflowRun) -> None:
        raw_enrollment = run.context.get("enrollment_id")
        if not isinstance(raw_enrollment, str) or not raw_enrollment.startswith("enr_"):
            raise ValidationError("回复 stop_sequence 缺少 enrollment")
        enrollment_id = EnrollmentId(raw_enrollment)
        await self._outreach.stop_enrollment(
            run.tenant_id,
            enrollment_id,
            EnrollmentStopReason.REPLY,
            actor=_enrollment_actor(enrollment_id),
        )

    async def _suppress(self, run: WorkflowRun, category: str) -> None:
        raw_contact = run.context.get("contact_point_id")
        raw_account = run.context.get("account_id")
        if not isinstance(raw_contact, str) or not raw_contact.startswith("cp_"):
            raise ValidationError("回复 suppress 缺少 contact")
        if not isinstance(raw_account, str) or not raw_account.startswith("acc_"):
            raise ValidationError("回复 suppress 缺少 account")
        reason = {
            "unsubscribe": SuppressionReason.UNSUBSCRIBE,
            "complaint": SuppressionReason.COMPLAINT,
            "bounce": SuppressionReason.HARD_BOUNCE,
        }.get(category)
        if reason is None:
            raise ReplyActionNotWiredError(f"suppress 无类别映射：{category}")
        message_id = run.context.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise ValidationError("回复 suppress 缺少 message_id")
        # SuppressionTarget 必须且只能指定一个资源：退订/投诉/硬退信来自该
        # 回复地址，确定性取 contact 粒度（公司级抑制需更高阶证据，不在此
        # 切片推断）。
        target = SuppressionTarget(contact_point_id=ContactPointId(raw_contact))
        del raw_account
        await self._outreach.add_suppression(
            run.tenant_id,
            SuppressionRequest(
                target=target,
                reason=reason,
                occurred_at=self._now(),
                source_ref=f"reply:{message_id}",
                idempotency_key=IdempotencyKey(f"reply-suppress:{message_id}"),
            ),
            actor=_suppression_actor(target),
        )
