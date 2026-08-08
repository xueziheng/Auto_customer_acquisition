"""触达域实体。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    ProspectAccountId,
    SendingIdentityId,
    SequenceId,
    TenantId,
)


class CampaignState(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class StepIntent(str, Enum):
    """序列步骤意图。决定 ``outreach_agent`` 生成什么样的内容。"""

    DISCOVERY = "discovery"
    """需求发现：让客户说出他缺什么。**第一步必须是这个。**

    以卖货开场会把对话锁死在「要/不要」；以需求开场能获得
    「我真正缺什么」——那是整条链路的原料。"""

    PRESENTATION = "presentation"
    """产品呈现：客户表达需求后，展示对应的供应方案。"""

    FOLLOW_UP = "follow_up"
    """跟进提醒。"""


@dataclass(frozen=True)
class SequenceStepSpec:
    """序列步骤定义。

    字段：
        step_number:   第几步（从 1 起）
        intent
        wait_days:     距上一步的等待天数
    """

    step_number: int
    intent: StepIntent
    wait_days: int


@dataclass(frozen=True)
class CampaignBoundary:
    """Campaign 边界 —— 老板批准的自主活动范围。

    这不是配置项集合，是**授权书**：Agent 在这个范围内不需要逐次
    请示，越界动作被 ``tool_gateway`` 拒绝。所以每个字段都是一条
    边界，不设默认放宽。

    字段：
        markets:              允许的目标国家
        target_entity_types:  允许的企业类型（importer / manufacturer / …）
        allowed_categories:   允许探索的品类
        sender_identity_ids:  可用发件身份（登记时校验角色为 COLD_OUTREACH）
        steps:                序列步骤（长度即 max_messages）
        stop_on_reply:        有回复即停，默认 True 且不建议改
        daily_new_contacts:   每日新联系人上限
        daily_total_messages: 每日总发送上限
        handoff_triggers:     触发转人工的条件（字符串，值来自
                              opportunities.HandoffTrigger，不跨域 import）
    """

    markets: list[str]
    target_entity_types: list[str]
    allowed_categories: list[str]
    sender_identity_ids: list[SendingIdentityId]
    steps: list[SequenceStepSpec]
    daily_new_contacts: int
    daily_total_messages: int
    handoff_triggers: list[str]
    stop_on_reply: bool = True

    def validate(self) -> list[str]:
        """校验边界自洽，返回问题列表。

        必查项：
        - ``steps`` 非空且第一步 intent 为 ``DISCOVERY``
        - ``steps`` 长度 ≤ 5（超过 5 步的无回复序列只会增加投诉）
        - 每日上限为正且 new_contacts ≤ total_messages
        - markets 非空
        """
        raise NotImplementedError


@dataclass
class Campaign:
    """Campaign。

    **边界修改即新版本。** ``boundary`` 一经批准不可变；要改就创建
    新版本并重新走审批——否则「老板批准的」和「实际执行的」会悄悄
    分叉，审批就失去了意义。

    字段：
        campaign_id, tenant_id
        name
        boundary
        version:        边界版本号
        state
        created_by, approved_by, approved_at
        paused_reason
        created_at
    """

    campaign_id: CampaignId
    tenant_id: TenantId
    name: str
    boundary: CampaignBoundary
    created_at: datetime
    created_by: EmployeeId
    version: int = 1
    state: CampaignState = CampaignState.DRAFT
    approved_by: EmployeeId | None = None
    approved_at: datetime | None = None
    paused_reason: str | None = None


class EnrollmentState(str, Enum):
    ENROLLED = "enrolled"
    IN_SEQUENCE = "in_sequence"
    REPLIED = "replied"
    """客户回复，序列停止。**这是成功出口**，不是异常。"""

    COMPLETED = "completed"
    """序列走完无回复。"""

    STOPPED_SUPPRESSED = "stopped_suppressed"
    STOPPED_BOUNCED = "stopped_bounced"
    STOPPED_MANUAL = "stopped_manual"
    STOPPED_IDENTITY_UNAVAILABLE = "stopped_identity_unavailable"
    """发件身份被熔断，序列挂起。身份恢复后可人工决定是否续跑。"""


@dataclass
class Enrollment:
    """一个联系人在一个 Campaign 序列中的位置。

    字段：
        enrollment_id, tenant_id, campaign_id
        account_id, contact_point_id
        sending_identity_id:  分配的发件身份（一条序列固定一个身份，
                              中途换发件人会让客户觉得混乱）
        state
        current_step:         已发到第几步
        next_send_at:         下一步计划时间
        enrolled_at
        stopped_at, stopped_reason
        conversation_ref:     产生的会话引用
    """

    enrollment_id: EnrollmentId
    tenant_id: TenantId
    campaign_id: CampaignId
    account_id: ProspectAccountId
    contact_point_id: ContactPointId
    sending_identity_id: SendingIdentityId
    enrolled_at: datetime
    state: EnrollmentState = EnrollmentState.ENROLLED
    current_step: int = 0
    next_send_at: datetime | None = None
    stopped_at: datetime | None = None
    stopped_reason: str | None = None
    conversation_ref: str | None = None

    def can_send_next_step(self, max_steps: int) -> bool:
        """能否发下一步。

        实现要求：状态为 ENROLLED / IN_SEQUENCE、未到步数上限、
        到达计划时间。**这只是本地判断**，发送前还要过 service 层的
        综合检查（回复竞态、抑制名单、身份许可），见
        ``OutreachService.prepare_send``。
        """
        raise NotImplementedError


class SuppressionScope(str, Enum):
    CONTACT = "contact"
    ACCOUNT = "account"
    """企业级。客户说「不要再联系我们公司」时用这个——只抑制回信
    那个人然后换个联系人继续发，既失礼又有法律风险。"""


class SuppressionReason(str, Enum):
    UNSUBSCRIBE = "unsubscribe"
    COMPLAINT = "complaint"
    HARD_BOUNCE = "hard_bounce"
    MANUAL_BLOCK = "manual_block"
    COMPETITOR = "competitor"
    EXISTING_CUSTOMER_CONFLICT = "existing_customer_conflict"
    """已是公司客户，不应再被冷开发触达。被现有客户收到冷开发邮件
    是很尴尬的事故。"""


@dataclass(frozen=True)
class SuppressionEntry:
    """抑制记录。

    **只增不删。** 跨 Campaign、跨身份、跨员工全局生效。
    移除必须人工发起并走审批（几乎不应该发生）。

    字段：
        tenant_id
        scope, target_id
        reason
        source_ref:   触发来源（消息 ID / webhook 事件 ID / 操作人）
        created_at
    """

    tenant_id: TenantId
    scope: SuppressionScope
    target_id: str
    reason: SuppressionReason
    source_ref: str
    created_at: datetime


@dataclass(frozen=True)
class DailyQuotaUsage:
    """Campaign 每日额度使用。

    字段：
        campaign_id, on_day
        new_contacts_used, total_messages_used

    计数必须原子递增（同 ``sending_identity`` 的发送计数），
    并发发送下读-改-写会突破老板批准的上限。
    """

    campaign_id: CampaignId
    on_day: str
    new_contacts_used: int
    total_messages_used: int
