"""上下文依赖的窄契约；只允许受信装配提供身份和策略。"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from shared.schemas.identifiers import EmployeeId, RunId, TenantId, UserId

Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
Role = Literal["sales", "manager", "boss"]
Scope = Literal["self", "manager", "tenant"]
ApprovalAction = Literal[
    "initial_price",
    "discount",
    "delivery_commitment",
    "formal_quote",
    "inventory_commitment",
    "certification_commitment",
    "payment_terms",
    "contract_terms",
    "exclusive_agency",
    "quality_guarantee",
    "off_catalog_reference_price",
    "price_email",
    "campaign_start",
    "sending_identity_change",
    "opportunity_won",
    "opportunity_lost",
]
REQUIRED_APPROVAL_ACTIONS: tuple[ApprovalAction, ...] = (
    "initial_price",
    "discount",
    "delivery_commitment",
    "formal_quote",
    "inventory_commitment",
    "certification_commitment",
    "payment_terms",
    "contract_terms",
    "exclusive_agency",
    "quality_guarantee",
    "off_catalog_reference_price",
    "price_email",
    "campaign_start",
    "sending_identity_change",
    "opportunity_won",
    "opportunity_lost",
)


class ContextDTO(BaseModel):
    """冻结容器且拒绝宽松转换；不允许未知授权字段或错误回显原文。"""

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
        revalidate_instances="always",
    )


class ContextIdentity(ContextDTO):
    """员工公开记录映射的主体；空 owners 永不代表无限制。"""

    tenant_id: TenantId
    user_id: UserId
    employee_id: EmployeeId
    role: Role
    scope_level: Scope
    allowed_owner_ids: tuple[EmployeeId, ...] = Field(min_length=1)
    territory_status: Literal["unavailable"] = "unavailable"

    @model_validator(mode="after")
    def coherent_scope(self) -> Self:
        """严格保持角色范围，禁止 sales 夹带他人归属。"""
        if (
            self.scope_level
            != {"sales": "self", "manager": "manager", "boss": "tenant"}[self.role]
        ):
            raise ValueError("上下文身份范围无效")
        if self.employee_id not in self.allowed_owner_ids or len(
            set(self.allowed_owner_ids)
        ) != len(self.allowed_owner_ids):
            raise ValueError("上下文归属范围无效")
        if self.role == "sales" and self.allowed_owner_ids != (self.employee_id,):
            raise ValueError("上下文归属范围无效")
        if any(
            not value.strip()
            for value in (
                self.tenant_id,
                self.user_id,
                self.employee_id,
                *self.allowed_owner_ids,
            )
        ):
            raise ValueError("上下文身份无效")
        return self


class ContextRules(ContextDTO):
    """必需租户规则及完整审批底线；空排除集也必须显式提供。"""

    playbook_ref: Text
    approval_actions: tuple[ApprovalAction, ...]
    exclusions: tuple[Text, ...]
    directives: tuple[Text, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def complete_approvals(self) -> Self:
        """所有全局逐次审批动作必须在规则集中显式存在。"""
        if set(self.approval_actions) != set(REQUIRED_APPROVAL_ACTIONS):
            raise ValueError("上下文审批规则不完整")
        return self


class ContextPolicy(ContextDTO):
    """受信 provider 的权限与规则版本快照；不是模型返回值。"""

    tenant_id: TenantId
    employee_id: EmployeeId
    policy_ref: Text
    user_allowed_tools: tuple[Text, ...]
    run_allowed_tools: tuple[Text, ...]
    explicitly_blocked_tools: tuple[Text, ...]
    required_rules: ContextRules
    skill_rules: tuple[Text, ...]


class ContextEntityRef(ContextDTO):
    """只接受精确机会引用；引用本身不授予读取权限。"""

    kind: Literal["opportunity"]
    entity_id: Text


class ContextSource(ContextDTO):
    """来源安全投影；URL、对象地址和摘录留在原始证据存储中。"""

    source_type: Literal[
        "conversation", "web_page", "upload", "employee_input", "external_api"
    ]
    source_id: Text
    extracted_by: Text
    extracted_at: AwareDatetime
    confirmed_by: EmployeeId | None
    confirmed_at: AwareDatetime | None

    @model_validator(mode="after")
    def confirmed_pair(self) -> Self:
        """确认人和确认时间必须同时存在或同时缺失。"""
        if (self.confirmed_by is None) != (self.confirmed_at is None):
            raise ValueError("上下文来源确认信息不完整")
        return self


class ContextFact(ContextDTO):
    """关键事实只允许窄白名单，完整来源随事实整段保留。"""

    name: Literal["quantity", "spec_summary", "destination", "required_by"]
    value: Text | int
    provenance: ContextSource

    @model_validator(mode="after")
    def value_matches_field(self) -> Self:
        """数量为正整数，其余字段必须为非空文本，不接收价格字段。"""
        if self.name == "quantity":
            if type(self.value) is not int or self.value <= 0:
                raise ValueError("上下文事实值无效")
        elif not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("上下文事实值无效")
        return self


class ContextInference(ContextDTO):
    """推断与事实分离，并关联该次返回的真实来源 ID。"""

    value: Text
    based_on: tuple[Text, ...] = Field(min_length=1)
    inferred_by: Text
    inferred_at: AwareDatetime


class ContextFactSection(ContextDTO):
    """有租户、归属和来源的可选背景；每段必须至少有一项事实。"""

    tenant_id: TenantId
    section_id: Text
    entity_ref: ContextEntityRef
    kind: Literal[
        "current_entity_summary",
        "relevant_history_evidence",
        "matching_supply_capability",
    ]
    source_ref: Text
    owner_employee_id: EmployeeId
    facts: tuple[ContextFact, ...] = Field(min_length=1)
    inferences: tuple[ContextInference, ...]

    @model_validator(mode="after")
    def backed_inferences(self) -> Self:
        """证据与推断同段，使裁剪不会留下无依据的推断。"""
        sources = {fact.provenance.source_id for fact in self.facts}
        if any(not set(item.based_on) <= sources for item in self.inferences):
            raise ValueError("上下文推断缺少事实依据")
        return self


class ContextIdentityReader(Protocol):
    async def resolve(
        self, tenant_id: TenantId, acting_user: UserId
    ) -> ContextIdentity:
        """经公开员工记录唯一解析主体；不得使用同串身份回退。"""
        ...


class ContextPolicyReader(Protocol):
    async def read(self, identity: ContextIdentity) -> ContextPolicy:
        """读取可信且完整的策略；未配置必须拒绝，不能从任务输入自授权。"""
        ...


class ContextFactReader(Protocol):
    async def load(
        self,
        identity: ContextIdentity,
        refs: tuple[ContextEntityRef, ...],
        *,
        history_limit: int,
    ) -> tuple[ContextFactSection, ...]:
        """查询前传递合法主体和 ABAC，仅返回指定租户及归属的窄事实。"""
        ...


class LockedSkill(ContextDTO):
    """受信选择快照必须固定版本与 prompt；reader 对资产快照正确性负责。"""

    skill_id: Text
    version: Text
    prompt_ref: Text
    prompt: Text


class TaskContextDescriptor(ContextDTO):
    """由已授权任务生产者绑定的选择记录；不接受 task.inputs 替代。"""

    tenant_id: TenantId
    run_id: RunId
    user_id: UserId
    source_ref: Text
    run_allowed_tools: tuple[Text, ...]
    run_blocked_tools: tuple[Text, ...]
    skills: tuple[LockedSkill, ...] = Field(min_length=1)
    entity_refs: tuple[ContextEntityRef, ...]


class TaskContextDescriptorReader(Protocol):
    async def read(
        self, tenant_id: TenantId, run_id: RunId, acting_user: UserId
    ) -> TaskContextDescriptor:
        """读取已锁定、正确绑定 tenant/run/user 的选择和资产快照。"""
        ...
