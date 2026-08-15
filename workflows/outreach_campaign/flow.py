"""outreach_campaign 流程定义与注册：一次性注册全部 handler 与定义。

每步一条 run（draft → prepare → send → record → complete）：步骤间的
等待由 ``Enrollment.next_send_at`` + scheduler 驱动扫描负责，不在这条短
链里表达——静态定义表达不了逐 enrollment 的 wait_days，reminder handler
又不能 advance（引擎契约），故无 ``wait_for_reply`` 等待步骤。
"""

from __future__ import annotations

from datetime import timedelta
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
)

WORKFLOW_TYPE = "outreach_campaign"

#: prepare_send 的拒绝（额度满/暂停/身份不可用）是瞬态条件：退避重试直到
#: 条件清除。上限只需覆盖驱动扫描间隔——驱动在暂停/取消时 cancel 未终态
#: run，run 不需要靠耗尽重试来自我了结。
_PREPARE_SEND_MAX_RETRIES = 100


def build_outreach_campaign_definition(
    retry_interval: timedelta,
) -> WorkflowDefinition:
    """每步一条 run：draft → prepare → send → record → complete。"""
    if not isinstance(retry_interval, timedelta) or retry_interval <= timedelta(0):
        raise ValueError("触达流程重试间隔必须为正")
    return WorkflowDefinition(
        workflow_type=WORKFLOW_TYPE,
        version=1,
        steps=(
            StepDefinition("draft_content", "outreach_campaign.draft_content"),
            StepDefinition(
                "prepare_send",
                "outreach_campaign.prepare_send",
                max_retries=_PREPARE_SEND_MAX_RETRIES,
                retry_backoff=retry_interval,
            ),
            StepDefinition("send", "outreach_campaign.send"),
            StepDefinition("record_sent", "outreach_campaign.record_sent"),
        ),
        transitions={
            "draft_content": ("prepare_send",),
            "prepare_send": ("send",),
            "send": ("record_sent",),
            # record_sent 完成即 run 完成；无后继步骤
            "record_sent": (),
        },
    )


def build_outreach_campaign_handlers(
    outreach: OutreachService,
    sender: CampaignEmailSender,
) -> dict[str, StepHandler]:
    """按 handler_ref 装配全部步骤 handler；定义与 handler 同步注册。"""
    return {
        "outreach_campaign.draft_content": DraftContentStep(outreach),
        "outreach_campaign.prepare_send": PrepareSendStep(outreach),
        "outreach_campaign.send": SendStep(sender),
        "outreach_campaign.record_sent": RecordSentStep(outreach),
    }


def register_outreach_campaign(
    engine: Any, retry_interval: timedelta
) -> None:
    """引擎侧注册：定义必须先于任何 run 存在。"""
    engine.register(build_outreach_campaign_definition(retry_interval))
