"""outreach_campaign 流程定义与注册：一次性注册全部 handler 与定义。

严格按 workflows/outreach_campaign/AGENTS.md：每个 enrollment 一条 run
（subject_ref = enrollment_id），5 步 draft_content → prepare_send → send →
record_sent → wait_for_reply。wait_for_reply 是真实 WAITING_EVENT(ReplyReceived)，
超时 = 该 enrollment 当前步骤 wait_days（RecordSentStep 从域 next_send_at
推导写入 run.context["timeout_seconds"]，引擎动态超时）；prepare_send 的
非终态拒绝（身份熔断/额度/暂停/未到期）→ wait_event(SendingIdentityActivated)，
事件到达立即唤醒，wait 时排定 1 天确定性兜底复查，绝不指数退避。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from domains.outreach.service import OutreachService
from workflows.engine.runner import (
    StepDefinition,
    StepHandler,
    WorkflowDefinition,
)
from workflows.outreach_campaign.steps import (
    CampaignEmailSender,
    DraftContentStep,
    PrepareSendStep,
    RecordSentStep,
    SendStep,
    WaitForReplyStep,
)

WORKFLOW_TYPE = "outreach_campaign"

#: prepare_send 非终态拒绝的确定性复查周期（兜底）；事件唤醒是快路径。
_PREPARE_RECHECK_TIMEOUT = timedelta(days=1)


def build_outreach_campaign_definition(
    retry_interval: timedelta,
) -> WorkflowDefinition:
    """每 enrollment 一条 run：draft → prepare → send → record → wait 循环。"""
    if not isinstance(retry_interval, timedelta) or retry_interval <= timedelta(0):
        raise ValueError("触达流程重试间隔必须为正")
    return WorkflowDefinition(
        workflow_type=WORKFLOW_TYPE,
        version=2,
        steps=(
            StepDefinition("draft_content", "outreach_campaign.draft_content"),
            StepDefinition(
                "prepare_send",
                "outreach_campaign.prepare_send",
                wait_event_type="SendingIdentityActivated",
                timeout=_PREPARE_RECHECK_TIMEOUT,
                on_timeout="prepare_send",
                run_on_entry=True,
                retry_backoff=retry_interval,
            ),
            StepDefinition(
                "send",
                "outreach_campaign.send",
                retry_backoff=retry_interval,
            ),
            StepDefinition("record_sent", "outreach_campaign.record_sent"),
            StepDefinition(
                "wait_for_reply",
                "outreach_campaign.wait_for_reply",
                wait_event_type="ReplyReceived",
                timeout_context_key="timeout_seconds",
                on_timeout="draft_content",
            ),
        ),
        transitions={
            "draft_content": ("prepare_send",),
            "prepare_send": ("send", "prepare_send"),
            "send": ("record_sent",),
            "record_sent": ("wait_for_reply",),
            "wait_for_reply": ("draft_content",),
        },
    )


def build_outreach_campaign_handlers(
    outreach: OutreachService,
    sender: CampaignEmailSender,
    now: Callable[[], datetime],
) -> dict[str, StepHandler]:
    """按 handler_ref 装配全部步骤 handler；定义与 handler 同步注册。"""
    return {
        "outreach_campaign.draft_content": DraftContentStep(outreach),
        "outreach_campaign.prepare_send": PrepareSendStep(outreach),
        "outreach_campaign.send": SendStep(sender),
        "outreach_campaign.record_sent": RecordSentStep(outreach, now),
        "outreach_campaign.wait_for_reply": WaitForReplyStep(outreach),
    }


def register_outreach_campaign(
    engine: Any, retry_interval: timedelta
) -> None:
    """引擎侧注册：定义必须先于任何 run 存在。"""
    engine.register(build_outreach_campaign_definition(retry_interval))
