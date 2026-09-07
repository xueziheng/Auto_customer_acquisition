"""reply_qualification 流程定义与注册。

每个入站回复一条 run（subject_ref = message_id）：classify → apply_actions →
complete。本模块只提供定义与步骤 handler；生产触发经 ``InboundMessageStored``
（入站原文已存 artifact + Message 已持久化的 metadata-only 事件）由 scheduler
消费启动，本仓库已提供可选 ``ReplyQualificationComposition`` 组合 seam——
scheduler 提供组合时装配定义/handler/outbox 订阅，但 production main 尚未
注入真实模型 provider（``ReplyModelPort`` 适配器未决），因此生产进程 reply
flow 不会自动启用。

触发纪律（事件时序）：
- ``ReplyReceived`` 是 ``ConversationService.record_classification`` 在
  分类落库**之后**才发布的结果事件，不能作为本流程的前置触发——否则形成
  「先分类才触发分类」的循环。订阅方是 outreach（停序列）与 campaign
  （唤醒等待步骤）。
- 正确的生产触发是「入站原文已存 artifact + Message 已持久化」的
  metadata-only 事件（无正文/对象键，tenant-bound）：``InboundMessageStored``
  已存在并进入 scheduler 消费（本切片接线），事件结构零改动。
- 本流程内部不消费任何事件，不发布任何事件；分类结果事件由 conversations
  域经 outbox 发布。同一 message_id 重复触发由 run 幂等键
  ``reply:{message_id}`` 兜底为 no-op。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from agent_runtime.qualification_agent.agent import ReplyClassifier
from domains.conversations.service import ConversationService
from domains.outreach.service import OutreachService
from shared.schemas.identifiers import TenantId
from workflows.engine.runner import (
    StepDefinition,
    StepHandler,
    WorkflowDefinition,
    WorkflowEngine,
)
from workflows.reply_qualification.ports import (
    InputContentGuard,
    MessageContentReader,
    ReplyActionPorts,
    ReplyClassificationAccess,
)
from workflows.reply_qualification.steps import ApplyActionsStep, ClassifyStep

WORKFLOW_TYPE = "reply_qualification"


def build_reply_qualification_definition() -> WorkflowDefinition:
    """classify → apply_actions → complete（AUTO_REPLY 在 classify 短路）。"""
    return WorkflowDefinition(
        workflow_type=WORKFLOW_TYPE,
        version=1,
        steps=(
            StepDefinition("classify", "reply_qualification.classify"),
            StepDefinition("apply_actions", "reply_qualification.apply_actions"),
        ),
        transitions={
            "classify": ("apply_actions",),
            "apply_actions": (),
        },
    )


def build_reply_qualification_handlers(
    *,
    classifier: ReplyClassifier,
    content_reader: MessageContentReader,
    input_guard: InputContentGuard,
    conversations: ConversationService,
    outreach: OutreachService,
    tenant_id: TenantId,
    now: Callable[[], datetime],
    action_ports: ReplyActionPorts | None = None,
    classification_access: ReplyClassificationAccess | None = None,
) -> dict[str, StepHandler]:
    """按 handler_ref 装配步骤 handler；定义与 handler 由调用方同步注册。

    组合侧只注入端口：``input_guard`` 是窄 ``InputContentGuard``，具体实现
    （agent_runtime/guardrails/input_guard.CredentialMarkerGuard）由调用方
    装配，本模块不依赖具体护栏。
    """
    return {
        "reply_qualification.classify": ClassifyStep(
            classifier,
            content_reader,
            input_guard,
            conversations,
            classification_access,
        ),
        "reply_qualification.apply_actions": ApplyActionsStep(
            outreach, tenant_id, now, action_ports
        ),
    }


def register_reply_qualification(engine: WorkflowEngine) -> None:
    """引擎侧注册：定义必须先于任何 run 存在；handler 由调用方装配。"""
    engine.register(build_reply_qualification_definition())
