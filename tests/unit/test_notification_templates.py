"""固定通知模板只渲染 typed context。"""

from __future__ import annotations

import importlib

import pytest

from notification_gateway.jobs import (
    NotificationContext,
    NotificationJobClaim,
    NotificationKind,
)
from notification_gateway.models import NotificationPriority
from shared.errors import PolicyViolation, ValidationError
from shared.schemas.identifiers import EmployeeId, NotificationJobId, TenantId, new_id

_TENANT = TenantId(new_id("tn"))
_EMPLOYEE = EmployeeId(new_id("emp"))


def _renderer():
    try:
        return importlib.import_module(
            "notification_gateway.templates"
        ).FixedNotificationTemplateRenderer()
    except (AttributeError, ModuleNotFoundError) as exc:
        pytest.fail(f"RED：固定通知模板尚未实现（{exc}）")


def _claim(context: NotificationContext) -> NotificationJobClaim:
    return NotificationJobClaim(
        NotificationJobId(new_id("njb")),
        _TENANT,
        _EMPLOYEE,
        NotificationPriority.URGENT,
        context,
        "SafeEvent",
        "safe:dedup",
        "claim-token",
        1,
    )


@pytest.mark.parametrize(
    "context,title,next_step,link",
    [
        (
            NotificationContext(
                NotificationKind.HANDOFF_ESCALATION,
                "han_01",
                "opp_01",
                "manager",
                None,
            ),
            "人工接管提醒",
            "处理人工接管任务",
            "/crm/handoffs/han_01",
        ),
        (
            NotificationContext(
                NotificationKind.HANDOFF_QUEUE_BACKLOGGED,
                "handoff_queue",
                None,
                None,
                7,
            ),
            "接管队列积压",
            "查看并分配待接管任务",
            "/crm/handoffs",
        ),
        (
            NotificationContext(
                NotificationKind.SENDING_IDENTITY_SUSPENDED,
                "sid_01",
                None,
                "hard_bounce_rate",
                None,
            ),
            "发件身份已熔断",
            "检查信誉指标并人工处理",
            "/crm/sending-identities/sid_01",
        ),
        (
            NotificationContext(
                NotificationKind.REPUTATION_THRESHOLD_BREACHED,
                "sid_01",
                None,
                "complaint_rate:watch",
                None,
            ),
            "发件信誉指标预警",
            "检查发件身份信誉趋势",
            "/crm/sending-identities/sid_01",
        ),
        (
            NotificationContext(
                NotificationKind.COMMITMENT_OVERDUE,
                "com_01",
                None,
                None,
                None,
            ),
            "承诺已逾期",
            "处理逾期承诺并更新下一步",
            "/crm/commitments/com_01",
        ),
        (
            NotificationContext(
                NotificationKind.APPROVAL_DECIDED,
                "apr_01",
                "emp_01",
                "approved",
                None,
            ),
            "审批已有结果",
            "查看审批结果并继续处理",
            "/crm/approvals/apr_01",
        ),
    ],
)
def test_renderer_maps_every_kind_to_fixed_safe_template(
    context: NotificationContext, title: str, next_step: str, link: str
) -> None:
    """模板错配会把员工导航到错误业务对象。"""
    rendered = _renderer().render(_claim(context))
    assert (rendered.title, rendered.next_step, rendered.link) == (
        title,
        next_step,
        link,
    )
    assert rendered.context is context
    assert rendered.source_job_id is not None
    assert rendered.tenant_id == _TENANT and rendered.recipient == _EMPLOYEE


@pytest.mark.parametrize(
    "claim",
    [
        object(),
        _claim(
            NotificationContext(
                NotificationKind.HANDOFF_ESCALATION,
                "han_01",
                None,
                "owner",
                None,
            )
        ),
        _claim(
            NotificationContext(
                NotificationKind.APPROVAL_DECIDED,
                "apr_01",
                None,
                "approved",
                None,
            )
        ),
    ],
)
def test_renderer_rejects_malformed_claim_or_missing_required_ids(claim: object) -> None:
    """缺少关联 ID 的通知不能进入 router 后才失败。"""
    with pytest.raises(ValidationError, match="通知任务无法渲染"):
        _renderer().render(claim)


def test_renderer_rejects_unknown_kind_and_invalid_template_link() -> None:
    """新增 kind 未配模板或模板被误改成外链时必须失败关闭。"""
    module = importlib.import_module("notification_gateway.templates")
    renderer = _renderer()
    context = NotificationContext(
        NotificationKind.COMMITMENT_OVERDUE,
        "com_01",
        None,
        None,
        None,
    )
    original = module._TEMPLATES[NotificationKind.COMMITMENT_OVERDUE]
    module._TEMPLATES[NotificationKind.COMMITMENT_OVERDUE] = original.__class__(
        original.title, original.next_step, lambda _context: "https://evil.test/path"
    )
    try:
        with pytest.raises(PolicyViolation, match="相对深链"):
            renderer.render(_claim(context))
    finally:
        module._TEMPLATES[NotificationKind.COMMITMENT_OVERDUE] = original

    forged = object.__new__(NotificationContext)
    object.__setattr__(forged, "kind", "future_kind")
    object.__setattr__(forged, "primary_id", "id_01")
    object.__setattr__(forged, "secondary_id", None)
    object.__setattr__(forged, "reason_code", None)
    object.__setattr__(forged, "level", None)
    with pytest.raises(ValidationError, match="通知任务无法渲染"):
        renderer.render(_claim(forged))

    forged_level = object.__new__(NotificationContext)
    object.__setattr__(forged_level, "kind", NotificationKind.HANDOFF_QUEUE_BACKLOGGED)
    object.__setattr__(forged_level, "primary_id", "handoff_queue")
    object.__setattr__(forged_level, "secondary_id", None)
    object.__setattr__(forged_level, "reason_code", None)
    object.__setattr__(forged_level, "level", 100)
    with pytest.raises(ValidationError, match="通知任务无法渲染"):
        renderer.render(_claim(forged_level))
