"""审批域实体。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from shared.schemas.identifiers import ApprovalId, EmployeeId, RunId, TenantId


class ApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    APPLIED = "applied"
    APPLY_FAILED = "apply_failed"
    """批准了但应用失败。**必须人工介入**，不能自动重试到成功——
    反复失败通常说明目标对象状态已变，盲目重试会应用到错误状态上。"""

    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalType(str, Enum):
    """必须人工审批的变更类型 —— MUST_APPROVE 注册表。

    ``tool_gateway`` 每次执行高风险动作前对着这个枚举查。
    分散定义会漂移（新工具忘接审批就成漏洞），所以集中在这里。

    与 ``quotations.ForbiddenAutoCommitment`` 的关系：那边定义
    「什么内容算承诺」，这边定义「承诺类动作走什么审批」。
    值用字符串对应，不跨域 import。
    """

    QUOTE_SEND = "quote_send"
    PRICE_COMMUNICATION = "price_communication"
    """任何包含具体价格的对外消息（即使不是正式报价）。"""

    DISCOUNT = "discount"
    DELIVERY_COMMITMENT = "delivery_commitment"
    CERTIFICATION_COMMITMENT = "certification_commitment"
    PAYMENT_TERMS = "payment_terms"
    EXCLUSIVE_DISTRIBUTION = "exclusive_distribution"
    OFF_CAMPAIGN_SEND = "off_campaign_send"
    """已批准 Campaign 边界之外的对外发送。"""

    SPEND_ABOVE_THRESHOLD = "spend_above_threshold"
    SUPPRESSION_REMOVAL = "suppression_removal"
    """从抑制名单移除。几乎不该发生，发生就要有人签字。"""

    CAMPAIGN_BOUNDARY_CHANGE = "campaign_boundary_change"
    SENDING_IDENTITY_CHANGE = "sending_identity_change"
    INDICATIVE_RISK_ACCEPTANCE = "indicative_risk_acceptance"
    MARGIN_FLOOR_OVERRIDE = "margin_floor_override"
    PLAYBOOK_CHANGE = "playbook_change"
    COUNTRY_POLICY_CHANGE = "country_policy_change"


DEFAULT_VALIDITY: dict[ApprovalType, timedelta] = {
    ApprovalType.QUOTE_SEND: timedelta(days=2),
    ApprovalType.PRICE_COMMUNICATION: timedelta(days=2),
    ApprovalType.DISCOUNT: timedelta(days=2),
    ApprovalType.MARGIN_FLOOR_OVERRIDE: timedelta(days=2),
    ApprovalType.CAMPAIGN_BOUNDARY_CHANGE: timedelta(days=7),
    ApprovalType.SENDING_IDENTITY_CHANGE: timedelta(days=7),
    ApprovalType.SUPPRESSION_REMOVAL: timedelta(days=1),
    ApprovalType.PLAYBOOK_CHANGE: timedelta(days=7),
    ApprovalType.COUNTRY_POLICY_CHANGE: timedelta(days=7),
}
"""审批有效期，按类型。价格类短（市场变得快），配置类长。
未列出的类型用 3 天。

为什么要有效期：一周前批准的价格变更今天才应用，汇率和供应商价格
可能都变了——批准针对的是**当时的状态**。
"""


@dataclass(frozen=True)
class BlastRadius:
    """影响范围 —— 审批人最需要、最常缺失的信息。

    字段：
        affected_entities:  影响哪些对象（「Campaign us-2026q3 及其
                            47 个进行中序列」）
        if_approved:        批准会发生什么
        if_rejected:        否决会发生什么
        reversible:         批准后能否撤销
    """

    affected_entities: list[str]
    if_approved: str
    if_rejected: str
    reversible: bool


@dataclass
class ApprovalPackage:
    """审批包。

    目标：审批人打开通知 → 看完 → 决定，一分钟内完成。
    信息不足导致审批人要去翻别的页面，是审批被拖延的头号原因。

    字段：
        approval_id, tenant_id
        approval_type
        title:            一句话说明（通知里显示）
        proposed_change:  变更内容的结构化描述
        proposed_by_run:  提议的 Agent Run（若有）
        proposed_by_employee
        reason:           为什么提议
        evidence_refs:    依据的证据链接
        blast_radius
        change_set_ref:   待应用的变更集引用
        owner_employee:   相关业务的负责人（自批禁止的判断依据）
        state
        created_at, expires_at
        decided_at, decided_by, decision_note
        applied_at, apply_error
    """

    approval_id: ApprovalId
    tenant_id: TenantId
    approval_type: ApprovalType
    title: str
    proposed_change: dict
    reason: str
    blast_radius: BlastRadius
    created_at: datetime
    expires_at: datetime
    state: ApprovalState = ApprovalState.PENDING
    proposed_by_run: RunId | None = None
    proposed_by_employee: EmployeeId | None = None
    evidence_refs: list[str] = field(default_factory=list)
    change_set_ref: str | None = None
    owner_employee: EmployeeId | None = None
    decided_at: datetime | None = None
    decided_by: EmployeeId | None = None
    decision_note: str | None = None
    applied_at: datetime | None = None
    apply_error: str | None = None
    contract_namespace: str | None = None
    request_hash: str | None = None
    expires_at_limit: datetime | None = None

    def can_be_decided_by(self, employee: EmployeeId) -> bool:
        """自批禁止的判定。

        实现要求：``employee`` 等于 ``proposed_by_employee`` 或
        ``owner_employee`` 时返回 False。**没有豁免参数。**
        这不是不信任，是消除「赶指标时给自己开绿灯」的结构性诱惑。
        """
        return employee not in {self.proposed_by_employee, self.owner_employee}

    def is_expired_at(self, now: datetime) -> bool:
        return now >= self.expires_at
