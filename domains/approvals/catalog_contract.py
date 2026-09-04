"""目录产品提案审批的本域严格契约，不依赖 Products 或 Employees。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, Self, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from domains.approvals.models import ApprovalPackage, ApprovalState, BlastRadius
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogProductProposalId,
    CatalogProposalPolicyVersionId,
    EmployeeId,
    NeedClusterId,
    RunId,
    TenantId,
)

_LOWER_HASH = re.compile(r"[0-9a-f]{64}\Z")
_POLICY_REF = re.compile(r"catalog-policy:(cpv_[^:\s]{1,36}):([0-9a-f]{64})\Z")
_CULTIVATION_REF = re.compile(
    r"catalog-cultivation:(cpr_[^:\s]{1,36}):(cpv_[^:\s]{1,36}):([0-9a-f]{64})\Z"
)
_PG_INT_MAX = 2_147_483_647
_RULE_ORDER = (
    "membership_integrity",
    "distinct_accounts",
    "recurring_accounts",
    "distinct_countries",
    "quantity_unit_coverage",
    "unified_unit",
)
CATALOG_CULTIVATION_WARNING: Literal[
    "该提案不代表正式产品、已确认供应或客户可报价价格。"
] = "该提案不代表正式产品、已确认供应或客户可报价价格。"
CATALOG_POLICY_NAMESPACE: Literal["catalog-policy-v1"] = "catalog-policy-v1"
CATALOG_CULTIVATION_NAMESPACE: Literal["catalog-cultivation-v1"] = (
    "catalog-cultivation-v1"
)

_POLICY_TITLE = "目录产品提案策略变更"
_POLICY_REASON = "请人工核对当前策略基准与全部门槛。"
_POLICY_APPROVED = "激活该候选策略；基准版本变化时不得应用。"
_POLICY_REJECTED = "保持当前活动策略；无活动策略时功能继续关闭。"
_CULTIVATION_TITLE = "目录产品培养审批"
_CULTIVATION_REASON = "请人工核对策略、需求事实、规则结果与证据引用。"
_CULTIVATION_APPROVED = "仅进入培养队列；不创建正式产品、不确认供应、不生成客户报价。"
_CULTIVATION_REJECTED = "本事实与策略版本不再重复提议。"


class CatalogApprovalContractError(ValidationError):
    """Catalog 审批固定错误，不携带底层异常或原始事实。"""

    def __init__(
        self,
        code: Literal[
            "catalog_contract_invalid",
            "catalog_request_conflict",
            "catalog_approval_not_found",
            "catalog_storage_unavailable",
        ],
    ) -> None:
        self.code = code
        super().__init__(f"目录审批契约错误：{code}")


class _CatalogContractModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
    )


class CatalogPolicyContentFact(_CatalogContractModel):
    """审批域自有的策略事实白名单；字段与 Products 公共契约逐项映射。"""

    minimum_distinct_accounts: int
    minimum_recurring_accounts: int | None
    minimum_distinct_countries: int | None
    minimum_quantity_unit_accounts: int | None
    require_unified_unit: bool

    @model_validator(mode="after")
    def validate_thresholds(self) -> Self:
        values = (
            ("minimum_distinct_accounts", self.minimum_distinct_accounts, 2),
            ("minimum_recurring_accounts", self.minimum_recurring_accounts, 1),
            ("minimum_distinct_countries", self.minimum_distinct_countries, 2),
            (
                "minimum_quantity_unit_accounts",
                self.minimum_quantity_unit_accounts,
                1,
            ),
        )
        for field_name, value, minimum in values:
            if value is not None and (
                type(value) is not int or not minimum <= value <= _PG_INT_MAX
            ):
                raise ValueError(f"{field_name} 无效")
        for value in (
            self.minimum_recurring_accounts,
            self.minimum_quantity_unit_accounts,
        ):
            if value is not None and value > self.minimum_distinct_accounts:
                raise ValueError("策略子门槛不得大于 minimum_distinct_accounts")
        if self.require_unified_unit and self.minimum_quantity_unit_accounts is None:
            raise ValueError("统一单位要求缺少数量单位门槛")
        return self


def _identity(value: str, field: str, *, prefix: str | None = None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 200
        or any(not character.isprintable() for character in value)
        or (prefix is not None and not value.startswith(f"{prefix}_"))
    ):
        raise ValueError(f"{field} 无效")
    return value


def _hash(value: str, field: str) -> str:
    if not isinstance(value, str) or _LOWER_HASH.fullmatch(value) is None:
        raise ValueError(f"{field} 必须是 64 位小写 SHA-256")
    return value


def _utc(value: datetime, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValueError(f"{field} 必须是 UTC 时间")
    return value


class CatalogPolicyVersionFact(_CatalogContractModel):
    """审批人看到的既有策略版本；不含幂等键或生命周期写权限。"""

    policy_version_id: CatalogProposalPolicyVersionId
    content: CatalogPolicyContentFact
    content_hash: str

    @model_validator(mode="after")
    def validate_version(self) -> Self:
        _identity(self.policy_version_id, "policy_version_id", prefix="cpv")
        if self.content_hash != catalog_policy_content_hash(self.content):
            raise ValueError("content_hash 与策略内容不一致")
        return self


class CatalogRuleResultFact(_CatalogContractModel):
    """审批包只接收确定性规则代码，不接收模型解释或概率。"""

    rule: Literal[
        "membership_integrity",
        "distinct_accounts",
        "recurring_accounts",
        "distinct_countries",
        "quantity_unit_coverage",
        "unified_unit",
    ]
    status: Literal["passed", "failed", "unknown", "not_required"]
    actual_value: int | str | bool | None
    required_value: int | str | bool | None
    explanation_code: Literal[
        "目录事实损坏，评估已阻断",
        "成员关系与品类完整一致",
        "成员关系或品类不一致",
        "去重客户数达到策略门槛",
        "去重客户数未达到策略门槛",
        "复购客户数达到策略门槛",
        "复购客户数未达到策略门槛",
        "复购客户事实不完整",
        "策略不要求复购客户数",
        "已知国家数达到策略门槛",
        "已知国家数未达到策略门槛",
        "客户国家事实不完整",
        "策略不要求已知国家数",
        "数量单位覆盖达到策略门槛",
        "数量单位覆盖未达到策略门槛",
        "数量单位事实不完整",
        "策略不要求数量单位覆盖",
        "有效数量单位已经统一",
        "统一单位事实不完整",
        "有效数量单位不统一",
        "统一单位事实未知",
        "策略不要求统一单位",
    ]

    @model_validator(mode="after")
    def validate_semantics(self) -> Self:
        blocked_code = "目录事实损坏，评估已阻断"
        if self.explanation_code == blocked_code:
            if self.status != "unknown" or self.actual_value is not None:
                raise ValueError("损坏事实只能产生无 actual_value 的 unknown 结果")
        else:
            expected = {
                ("membership_integrity", "passed"): "成员关系与品类完整一致",
                ("membership_integrity", "failed"): "成员关系或品类不一致",
                ("distinct_accounts", "passed"): "去重客户数达到策略门槛",
                ("distinct_accounts", "failed"): "去重客户数未达到策略门槛",
                ("recurring_accounts", "passed"): "复购客户数达到策略门槛",
                ("recurring_accounts", "failed"): "复购客户数未达到策略门槛",
                ("recurring_accounts", "unknown"): "复购客户事实不完整",
                ("recurring_accounts", "not_required"): "策略不要求复购客户数",
                ("distinct_countries", "passed"): "已知国家数达到策略门槛",
                ("distinct_countries", "failed"): "已知国家数未达到策略门槛",
                ("distinct_countries", "unknown"): "客户国家事实不完整",
                ("distinct_countries", "not_required"): "策略不要求已知国家数",
                ("quantity_unit_coverage", "passed"): "数量单位覆盖达到策略门槛",
                ("quantity_unit_coverage", "failed"): "数量单位覆盖未达到策略门槛",
                ("quantity_unit_coverage", "unknown"): "数量单位事实不完整",
                ("quantity_unit_coverage", "not_required"): "策略不要求数量单位覆盖",
                ("unified_unit", "passed"): "有效数量单位已经统一",
                ("unified_unit", "failed"): "有效数量单位不统一",
                ("unified_unit", "unknown"): (
                    "统一单位事实不完整"
                    if self.required_value is True
                    else "统一单位事实未知"
                ),
                ("unified_unit", "not_required"): "策略不要求统一单位",
            }
            if self.explanation_code != expected.get((self.rule, self.status)):
                raise ValueError("explanation_code 与规则状态不一致")

        if self.rule == "membership_integrity":
            self._validate_membership()
        elif self.rule == "unified_unit":
            self._validate_unified_unit()
        else:
            self._validate_count_rule()
        return self

    def _validate_membership(self) -> None:
        if self.required_value is not True:
            raise ValueError("membership_integrity.required_value 必须固定为 true")
        expected_actual = {"passed": True, "failed": False, "unknown": None}
        if (
            self.status == "not_required"
            or self.actual_value is not expected_actual.get(self.status)
        ):
            raise ValueError("membership_integrity 状态与 actual_value 不一致")

    def _validate_count_rule(self) -> None:
        minimum = 2 if self.rule in {"distinct_accounts", "distinct_countries"} else 1
        if self.required_value is not None:
            if type(self.required_value) is not int:
                raise ValueError("count required_value 必须是整数")
            required = cast(int, self.required_value)
            if not minimum <= required <= _PG_INT_MAX:
                raise ValueError("count required_value 越界")
        else:
            required = None
        if self.actual_value is not None:
            if type(self.actual_value) is not int:
                raise ValueError("count actual_value 必须是整数")
            actual = cast(int, self.actual_value)
            if not 0 <= actual <= _PG_INT_MAX:
                raise ValueError("count actual_value 越界")
        else:
            actual = None
        if self.rule == "distinct_accounts" and required is None:
            raise ValueError("distinct_accounts.required_value 是固定必需门槛")
        if self.status == "unknown":
            if self.explanation_code != "目录事实损坏，评估已阻断" and actual is None:
                raise ValueError("合法 unknown count 必须携带已确认 actual_value")
            if actual is not None and required is not None and actual >= required:
                raise ValueError("required count 已达门槛时不得标记 unknown")
            return
        if self.status == "not_required":
            if required is not None or actual is None:
                raise ValueError("not_required count 规则形状无效")
            return
        if required is None or actual is None:
            raise ValueError("passed/failed count 规则必须携带 actual 与 required")
        if (self.status == "passed") != (actual >= required):
            raise ValueError("count 状态与 actual>=required 比较不一致")

    def _validate_unified_unit(self) -> None:
        if self.required_value is not None and self.required_value is not True:
            raise ValueError("unified_unit.required_value 只能是 true 或 None")
        if self.actual_value is not None:
            if type(self.actual_value) is not str:
                raise ValueError("unified_unit.actual_value 只能是 str 或 None")
            unit = cast(str, self.actual_value)
            if (
                not unit
                or unit != unit.strip()
                or len(unit) > 64
                or any(not character.isprintable() for character in unit)
                or "://" in unit
            ):
                raise ValueError("unified_unit.actual_value 无效")
        if self.status == "passed":
            valid = self.required_value is True and self.actual_value is not None
        elif self.status == "failed":
            valid = self.required_value is True and self.actual_value is None
        elif self.status == "unknown":
            valid = self.actual_value is None
        else:
            valid = self.required_value is None and self.actual_value is not None
        if not valid:
            raise ValueError("unified_unit 状态与规则值不一致")


class CatalogPolicyApprovalCommand(_CatalogContractModel):
    """策略审批提交命令；原始文本、价格与概率没有字段入口。"""

    tenant_id: TenantId
    policy_version_id: CatalogProposalPolicyVersionId
    content: CatalogPolicyContentFact
    content_hash: str
    base_active_version: CatalogPolicyVersionFact | None
    proposed_by_employee: EmployeeId
    owner_employee: EmployeeId
    change_set_ref: str
    request_hash: str
    expires_at_limit: datetime

    @model_validator(mode="after")
    def validate_subject(self) -> Self:
        _identity(self.tenant_id, "tenant_id")
        _identity(self.policy_version_id, "policy_version_id", prefix="cpv")
        _identity(self.proposed_by_employee, "proposed_by_employee", prefix="emp")
        _identity(self.owner_employee, "owner_employee", prefix="emp")
        _utc(self.expires_at_limit, "expires_at_limit")
        if self.owner_employee != self.proposed_by_employee:
            raise ValueError("策略 owner 必须是可信提交人")
        if self.content_hash != catalog_policy_content_hash(self.content):
            raise ValueError("content_hash 与策略内容不一致")
        base_id = (
            None
            if self.base_active_version is None
            else self.base_active_version.policy_version_id
        )
        if self.request_hash != catalog_policy_request_hash(
            self.content, self.proposed_by_employee, base_id
        ):
            raise ValueError("request_hash 与策略创建承诺不一致")
        expected_ref = f"catalog-policy:{self.policy_version_id}:{self.content_hash}"
        if self.change_set_ref != expected_ref:
            raise ValueError("策略 change_set_ref 无效")
        return self


def _safe_evidence_refs(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values or len(values) > 100 or len(set(values)) != len(values):
        raise ValueError("evidence_refs 无效")
    for value in values:
        _identity(value, "evidence_ref")
        if "://" in value or any(character.isspace() for character in value):
            raise ValueError("evidence_ref 不得承载 URL 或原文")
    return values


def catalog_cultivation_request_hash(
    *,
    tenant_id: TenantId,
    proposal_id: CatalogProductProposalId,
    cluster_id: NeedClusterId,
    policy_version_id: CatalogProposalPolicyVersionId,
    policy_content_hash: str,
    facts_hash: str,
    rule_results: tuple[CatalogRuleResultFact, ...],
    evidence_refs: tuple[str, ...],
    proposed_by_run: RunId,
    owner_employee: EmployeeId,
    change_set_ref: str,
    expires_at_limit: datetime,
    warning: str,
) -> str:
    """对完整培养审批 subject 作规范 JSON SHA-256。"""
    payload = {
        "schema_version": "catalog-cultivation-request-v1",
        "tenant_id": str(tenant_id),
        "proposal_id": str(proposal_id),
        "cluster_id": str(cluster_id),
        "policy_version_id": str(policy_version_id),
        "policy_content_hash": policy_content_hash,
        "facts_hash": facts_hash,
        "rule_results": [item.model_dump(mode="json") for item in rule_results],
        "evidence_refs": list(evidence_refs),
        "proposed_by_run": str(proposed_by_run),
        "owner_employee": str(owner_employee),
        "change_set_ref": change_set_ref,
        "expires_at_limit": expires_at_limit.isoformat(),
        "warning": warning,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CatalogCultivationApprovalCommand(_CatalogContractModel):
    """培养审批提交命令；只含安全定位、规则结果和 Evidence 引用。"""

    tenant_id: TenantId
    proposal_id: CatalogProductProposalId
    cluster_id: NeedClusterId
    policy_version_id: CatalogProposalPolicyVersionId
    policy_content_hash: str
    facts_hash: str
    rule_results: tuple[CatalogRuleResultFact, ...]
    evidence_refs: tuple[str, ...]
    proposed_by_run: RunId
    owner_employee: EmployeeId
    change_set_ref: str
    request_hash: str
    expires_at_limit: datetime
    warning: Literal["该提案不代表正式产品、已确认供应或客户可报价价格。"] = (
        CATALOG_CULTIVATION_WARNING
    )

    @model_validator(mode="after")
    def validate_subject(self) -> Self:
        for value, field, prefix in (
            (self.tenant_id, "tenant_id", None),
            (self.proposal_id, "proposal_id", "cpr"),
            (self.cluster_id, "cluster_id", None),
            (self.policy_version_id, "policy_version_id", "cpv"),
            (self.proposed_by_run, "proposed_by_run", "run"),
            (self.owner_employee, "owner_employee", "emp"),
        ):
            _identity(value, field, prefix=prefix)
        _hash(self.policy_content_hash, "policy_content_hash")
        _hash(self.facts_hash, "facts_hash")
        _utc(self.expires_at_limit, "expires_at_limit")
        _safe_evidence_refs(self.evidence_refs)
        if tuple(item.rule for item in self.rule_results) != _RULE_ORDER:
            raise ValueError("目录评估规则顺序无效")
        expected_ref = (
            f"catalog-cultivation:{self.proposal_id}:"
            f"{self.policy_version_id}:{self.facts_hash}"
        )
        if self.change_set_ref != expected_ref:
            raise ValueError("培养 change_set_ref 无效")
        expected_hash = catalog_cultivation_request_hash(
            **{
                name: getattr(self, name)
                for name in (
                    "tenant_id",
                    "proposal_id",
                    "cluster_id",
                    "policy_version_id",
                    "policy_content_hash",
                    "facts_hash",
                    "rule_results",
                    "evidence_refs",
                    "proposed_by_run",
                    "owner_employee",
                    "change_set_ref",
                    "expires_at_limit",
                    "warning",
                )
            }
        )
        if self.request_hash != expected_hash:
            raise ValueError("request_hash 与培养审批 subject 不一致")
        return self


class CatalogPolicyApprovalChange(_CatalogContractModel):
    """实际持久化的策略 proposed_change 精确白名单。"""

    schema_version: Literal["catalog-policy-v1"]
    tenant_id: TenantId
    approval_type: Literal["catalog_proposal_policy_change"]
    policy_version_id: CatalogProposalPolicyVersionId
    content: CatalogPolicyContentFact
    content_hash: str
    base_active_version_id: CatalogProposalPolicyVersionId | None
    before_policy: CatalogPolicyVersionFact | None
    request_hash: str
    configuration_when_missing: Literal["未配置即关闭"]
    external_action: Literal["无外部动作"]

    @model_validator(mode="after")
    def validate_change(self) -> Self:
        if self.content_hash != catalog_policy_content_hash(self.content):
            raise ValueError("content_hash 与策略内容不一致")
        _hash(self.request_hash, "request_hash")
        before_id = (
            None if self.before_policy is None else self.before_policy.policy_version_id
        )
        if self.base_active_version_id != before_id:
            raise ValueError("before_policy 与 base_active_version_id 不一致")
        return self


class CatalogCultivationApprovalChange(_CatalogContractModel):
    """实际持久化的培养 proposed_change 精确白名单。"""

    schema_version: Literal["catalog-cultivation-v1"]
    tenant_id: TenantId
    approval_type: Literal["catalog_product_cultivation"]
    proposal_id: CatalogProductProposalId
    cluster_id: NeedClusterId
    policy_version_id: CatalogProposalPolicyVersionId
    policy_content_hash: str
    facts_hash: str
    rule_results: tuple[CatalogRuleResultFact, ...]
    evidence_refs: tuple[str, ...]
    warning: Literal["该提案不代表正式产品、已确认供应或客户可报价价格。"]
    request_hash: str

    @field_validator("rule_results", "evidence_refs", mode="before")
    @classmethod
    def restore_json_arrays(cls, value: object) -> object:
        """JSONB 数组只在持久事实解码边界显式恢复为不可变 tuple。"""
        return tuple(value) if isinstance(value, list) else value


type CatalogApprovalChange = (
    CatalogPolicyApprovalChange | CatalogCultivationApprovalChange
)
type CatalogApprovalCommand = (
    CatalogPolicyApprovalCommand | CatalogCultivationApprovalCommand
)


class CatalogApprovalActorFact(_CatalogContractModel):
    """可信员工源返回的当前最小事实；不能作为可缓存授权 token。"""

    tenant_id: TenantId
    employee_id: EmployeeId
    current_role: Literal[
        "boss", "manager", "sales", "sourcing", "product", "finance", "viewer"
    ]
    active: bool
    eligible: bool

    @model_validator(mode="after")
    def validate_actor(self) -> Self:
        _identity(self.tenant_id, "tenant_id")
        _identity(self.employee_id, "employee_id", prefix="emp")
        return self


class CatalogApprovalFact(_CatalogContractModel):
    """受信 workflow 使用的完整持久事实；不得注册为 HTTP DTO。"""

    tenant_id: TenantId
    approval_id: ApprovalId
    approval_type: Literal[
        "catalog_proposal_policy_change", "catalog_product_cultivation"
    ]
    contract_namespace: Literal["catalog-policy-v1", "catalog-cultivation-v1"]
    title: str
    proposed_change: CatalogApprovalChange
    reason: str
    blast_radius: BlastRadius
    proposed_by_run: RunId | None
    proposed_by_employee: EmployeeId | None
    owner_employee: EmployeeId
    evidence_refs: tuple[str, ...]
    change_set_ref: str
    created_at: datetime
    expires_at: datetime
    expires_at_limit: datetime
    request_hash: str
    state: ApprovalState
    decided_by_employee: EmployeeId | None
    decided_at: datetime | None
    decision_note: str | None
    applied_at: datetime | None
    application_error_code: str | None

    @model_validator(mode="after")
    def validate_workflow_fact(self) -> Self:
        expected_type = {
            CATALOG_POLICY_NAMESPACE: "catalog_proposal_policy_change",
            CATALOG_CULTIVATION_NAMESPACE: "catalog_product_cultivation",
        }[self.contract_namespace]
        if (
            self.approval_type != expected_type
            or self.proposed_change.schema_version != self.contract_namespace
            or self.proposed_change.approval_type != self.approval_type
        ):
            raise ValueError("Catalog namespace、类型与 proposed_change 不一致")
        for value, field in (
            (self.created_at, "created_at"),
            (self.expires_at, "expires_at"),
            (self.expires_at_limit, "expires_at_limit"),
        ):
            _utc(value, field)
        if not self.created_at < self.expires_at <= self.expires_at_limit:
            raise ValueError("Catalog 审批期限无效")
        decided = self.state in {
            ApprovalState.APPROVED,
            ApprovalState.APPLIED,
            ApprovalState.APPLY_FAILED,
            ApprovalState.REJECTED,
        }
        if decided != (
            self.decided_by_employee is not None and self.decided_at is not None
        ):
            raise ValueError("Catalog 审批决定事实不完整")
        if self.decided_at is not None:
            _utc(self.decided_at, "decided_at")
            if self.decided_at > self.expires_at:
                raise ValueError("Catalog 审批决定不得晚于期限")
            if self.decided_by_employee in {
                self.proposed_by_employee,
                self.owner_employee,
            }:
                raise ValueError("Catalog 审批不得自批")
        if self.state is ApprovalState.APPLIED:
            if self.applied_at is None or self.application_error_code is not None:
                raise ValueError("Catalog applied 事实无效")
            _utc(self.applied_at, "applied_at")
        elif self.state is ApprovalState.APPLY_FAILED:
            if self.applied_at is not None or self.application_error_code is None:
                raise ValueError("Catalog apply_failed 事实无效")
        elif self.applied_at is not None or self.application_error_code is not None:
            raise ValueError("Catalog 未应用状态不得携带应用结果")
        return self


@runtime_checkable
class CatalogApprovalActorReader(Protocol):
    async def read_actor(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> CatalogApprovalActorFact:
        """读取当前租户员工事实；异常必须由审批服务脱敏。"""
        ...


@runtime_checkable
class CatalogApprovalFactReader(Protocol):
    async def read_catalog_fact(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> CatalogApprovalFact: ...

    async def find_catalog_fact(
        self, tenant_id: TenantId, change_set_ref: str
    ) -> CatalogApprovalFact | None: ...


def catalog_change_set_ref(value: object) -> tuple[TenantId, str]:
    """只提取加锁所需 locator；完整命令在锁内重新校验。"""
    try:
        if not isinstance(
            value, (CatalogPolicyApprovalCommand, CatalogCultivationApprovalCommand)
        ):
            raise TypeError
        tenant_id = value.tenant_id
        change_set_ref = value.change_set_ref
        _identity(tenant_id, "tenant_id")
        _identity(change_set_ref, "change_set_ref")
    except (AttributeError, TypeError, ValueError):
        raise CatalogApprovalContractError("catalog_contract_invalid") from None
    validate_catalog_change_set_ref(change_set_ref)
    return TenantId(tenant_id), change_set_ref


def validate_catalog_change_set_ref(change_set_ref: str) -> None:
    """事实恢复只接受完整、大小写精确的两个 Catalog 引用。"""
    if not isinstance(change_set_ref, str) or not (
        _POLICY_REF.fullmatch(change_set_ref)
        or _CULTIVATION_REF.fullmatch(change_set_ref)
    ):
        raise CatalogApprovalContractError("catalog_contract_invalid")


def validate_catalog_command(value: object) -> CatalogApprovalCommand:
    """重新验证 model_copy/model_construct，避免调用方绕过严格 DTO。"""
    try:
        if isinstance(value, CatalogPolicyApprovalCommand):
            return CatalogPolicyApprovalCommand.model_validate(
                {
                    name: getattr(value, name)
                    for name in CatalogPolicyApprovalCommand.model_fields
                }
            )
        if isinstance(value, CatalogCultivationApprovalCommand):
            return CatalogCultivationApprovalCommand.model_validate(
                {
                    name: getattr(value, name)
                    for name in CatalogCultivationApprovalCommand.model_fields
                }
            )
    except (TypeError, ValueError):
        pass
    raise CatalogApprovalContractError("catalog_contract_invalid")


def catalog_package_fields(
    command: CatalogApprovalCommand,
) -> tuple[
    str,
    dict[str, object],
    str,
    BlastRadius,
    RunId | None,
    EmployeeId | None,
    tuple[str, ...],
]:
    """由严格命令投影固定审批文案与精确 proposed_change。"""
    if isinstance(command, CatalogPolicyApprovalCommand):
        before_id = (
            None
            if command.base_active_version is None
            else command.base_active_version.policy_version_id
        )
        policy_change = CatalogPolicyApprovalChange(
            schema_version=CATALOG_POLICY_NAMESPACE,
            tenant_id=command.tenant_id,
            approval_type="catalog_proposal_policy_change",
            policy_version_id=command.policy_version_id,
            content=command.content,
            content_hash=command.content_hash,
            base_active_version_id=before_id,
            before_policy=command.base_active_version,
            request_hash=command.request_hash,
            configuration_when_missing="未配置即关闭",
            external_action="无外部动作",
        )
        return (
            _POLICY_TITLE,
            policy_change.model_dump(mode="json"),
            _POLICY_REASON,
            BlastRadius(
                [str(command.policy_version_id)],
                _POLICY_APPROVED,
                _POLICY_REJECTED,
                True,
            ),
            None,
            command.proposed_by_employee,
            (),
        )
    cultivation_change = CatalogCultivationApprovalChange(
        schema_version=CATALOG_CULTIVATION_NAMESPACE,
        tenant_id=command.tenant_id,
        approval_type="catalog_product_cultivation",
        proposal_id=command.proposal_id,
        cluster_id=command.cluster_id,
        policy_version_id=command.policy_version_id,
        policy_content_hash=command.policy_content_hash,
        facts_hash=command.facts_hash,
        rule_results=command.rule_results,
        evidence_refs=command.evidence_refs,
        warning=command.warning,
        request_hash=command.request_hash,
    )
    return (
        _CULTIVATION_TITLE,
        cultivation_change.model_dump(mode="json"),
        _CULTIVATION_REASON,
        BlastRadius(
            [str(command.proposal_id), str(command.cluster_id)],
            _CULTIVATION_APPROVED,
            _CULTIVATION_REJECTED,
            False,
        ),
        command.proposed_by_run,
        None,
        command.evidence_refs,
    )


def catalog_package_fact(package: ApprovalPackage) -> CatalogApprovalFact | None:
    """严格解码持久行；任一疑似 Catalog 标记不完整都失败关闭。"""
    schema = package.proposed_change.get("schema_version")
    marked = (
        package.approval_type.value
        in {"catalog_proposal_policy_change", "catalog_product_cultivation"}
        or isinstance(package.contract_namespace, str)
        and package.contract_namespace.casefold().startswith("catalog-")
        or isinstance(package.change_set_ref, str)
        and package.change_set_ref.casefold().startswith(
            ("catalog-policy:", "catalog-cultivation:")
        )
        or isinstance(schema, str)
        and schema.casefold().startswith(("catalog-policy", "catalog-cultivation"))
    )
    if not marked:
        return None
    try:
        if package.contract_namespace == CATALOG_POLICY_NAMESPACE:
            policy_change = CatalogPolicyApprovalChange.model_validate(
                package.proposed_change
            )
            if (
                package.proposed_by_employee is None
                or package.owner_employee is None
                or package.change_set_ref is None
                or package.request_hash is None
                or package.expires_at_limit is None
            ):
                raise ValueError
            command: CatalogApprovalCommand = CatalogPolicyApprovalCommand(
                tenant_id=package.tenant_id,
                policy_version_id=policy_change.policy_version_id,
                content=policy_change.content,
                content_hash=policy_change.content_hash,
                base_active_version=policy_change.before_policy,
                proposed_by_employee=package.proposed_by_employee,
                owner_employee=package.owner_employee,
                change_set_ref=package.change_set_ref,
                request_hash=package.request_hash,
                expires_at_limit=package.expires_at_limit,
            )
            change: CatalogApprovalChange = policy_change
        elif package.contract_namespace == CATALOG_CULTIVATION_NAMESPACE:
            cultivation_change = CatalogCultivationApprovalChange.model_validate(
                package.proposed_change
            )
            if (
                package.proposed_by_run is None
                or package.owner_employee is None
                or package.change_set_ref is None
                or package.request_hash is None
                or package.expires_at_limit is None
            ):
                raise ValueError
            command = CatalogCultivationApprovalCommand(
                tenant_id=package.tenant_id,
                proposal_id=cultivation_change.proposal_id,
                cluster_id=cultivation_change.cluster_id,
                policy_version_id=cultivation_change.policy_version_id,
                policy_content_hash=cultivation_change.policy_content_hash,
                facts_hash=cultivation_change.facts_hash,
                rule_results=cultivation_change.rule_results,
                evidence_refs=cultivation_change.evidence_refs,
                proposed_by_run=package.proposed_by_run,
                owner_employee=package.owner_employee,
                change_set_ref=package.change_set_ref,
                request_hash=package.request_hash,
                expires_at_limit=package.expires_at_limit,
                warning=cultivation_change.warning,
            )
            change = cultivation_change
        else:
            raise ValueError
        if package.approval_type.value != (
            "catalog_proposal_policy_change"
            if isinstance(command, CatalogPolicyApprovalCommand)
            else "catalog_product_cultivation"
        ):
            raise ValueError
        title, proposed, reason, blast, run, employee, evidence = (
            catalog_package_fields(command)
        )
        validity_days = 7 if isinstance(command, CatalogPolicyApprovalCommand) else 3
        expected_expiry = min(
            package.created_at + timedelta(days=validity_days),
            command.expires_at_limit,
        )
        if (
            package.title != title
            or package.proposed_change != proposed
            or package.reason != reason
            or package.blast_radius != blast
            or package.proposed_by_run != run
            or package.proposed_by_employee != employee
            or package.evidence_refs != list(evidence)
            or package.owner_employee != command.owner_employee
            or package.change_set_ref != command.change_set_ref
            or package.request_hash != command.request_hash
            or package.expires_at_limit != command.expires_at_limit
            or package.expires_at != expected_expiry
        ):
            raise ValueError
        return CatalogApprovalFact(
            tenant_id=package.tenant_id,
            approval_id=package.approval_id,
            approval_type=(
                "catalog_proposal_policy_change"
                if isinstance(command, CatalogPolicyApprovalCommand)
                else "catalog_product_cultivation"
            ),
            contract_namespace=(
                CATALOG_POLICY_NAMESPACE
                if isinstance(command, CatalogPolicyApprovalCommand)
                else CATALOG_CULTIVATION_NAMESPACE
            ),
            title=package.title,
            proposed_change=change,
            reason=package.reason,
            blast_radius=package.blast_radius,
            proposed_by_run=package.proposed_by_run,
            proposed_by_employee=package.proposed_by_employee,
            owner_employee=package.owner_employee,
            evidence_refs=tuple(package.evidence_refs),
            change_set_ref=package.change_set_ref,
            created_at=package.created_at,
            expires_at=package.expires_at,
            expires_at_limit=package.expires_at_limit,
            request_hash=package.request_hash,
            state=package.state,
            decided_by_employee=package.decided_by,
            decided_at=package.decided_at,
            decision_note=package.decision_note,
            applied_at=package.applied_at,
            application_error_code=package.apply_error,
        )
    except (AttributeError, TypeError, ValueError):
        raise CatalogApprovalContractError("catalog_contract_invalid") from None


def same_catalog_request(existing: ApprovalPackage, candidate: ApprovalPackage) -> bool:
    """精确比较除生成 ID/创建时钟外的全部不可变请求字段。"""
    return (
        existing.tenant_id,
        existing.approval_type,
        existing.title,
        existing.proposed_change,
        existing.reason,
        existing.blast_radius,
        existing.proposed_by_run,
        existing.proposed_by_employee,
        existing.evidence_refs,
        existing.change_set_ref,
        existing.owner_employee,
        existing.contract_namespace,
        existing.request_hash,
        existing.expires_at_limit,
    ) == (
        candidate.tenant_id,
        candidate.approval_type,
        candidate.title,
        candidate.proposed_change,
        candidate.reason,
        candidate.blast_radius,
        candidate.proposed_by_run,
        candidate.proposed_by_employee,
        candidate.evidence_refs,
        candidate.change_set_ref,
        candidate.owner_employee,
        candidate.contract_namespace,
        candidate.request_hash,
        candidate.expires_at_limit,
    )


def catalog_policy_content_hash(content: CatalogPolicyContentFact) -> str:
    """独立重现 Products 的规范策略内容摘要。"""
    checked = CatalogPolicyContentFact.model_validate(content.model_dump(mode="python"))
    canonical = json.dumps(
        checked.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def catalog_policy_request_hash(
    content: CatalogPolicyContentFact,
    proposed_by: EmployeeId,
    base_active_version_id: CatalogProposalPolicyVersionId | None,
) -> str:
    """独立重现既有策略创建承诺；生成后的 policy ID 不属于该摘要。"""
    checked = CatalogPolicyContentFact.model_validate(content.model_dump(mode="python"))
    payload = {
        "schema_version": "catalog-policy-create-v1",
        "content": checked.model_dump(mode="json"),
        "proposed_by": str(proposed_by),
        "base_active_version_id": (
            None if base_active_version_id is None else str(base_active_version_id)
        ),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = (
    "CATALOG_CULTIVATION_NAMESPACE",
    "CATALOG_CULTIVATION_WARNING",
    "CATALOG_POLICY_NAMESPACE",
    "CatalogApprovalActorFact",
    "CatalogApprovalActorReader",
    "CatalogApprovalCommand",
    "CatalogApprovalContractError",
    "CatalogApprovalFact",
    "CatalogApprovalFactReader",
    "CatalogCultivationApprovalChange",
    "CatalogCultivationApprovalCommand",
    "CatalogPolicyApprovalChange",
    "CatalogPolicyApprovalCommand",
    "CatalogPolicyContentFact",
    "CatalogPolicyVersionFact",
    "CatalogRuleResultFact",
    "catalog_change_set_ref",
    "catalog_cultivation_request_hash",
    "catalog_package_fact",
    "catalog_package_fields",
    "catalog_policy_content_hash",
    "catalog_policy_request_hash",
    "same_catalog_request",
    "validate_catalog_change_set_ref",
    "validate_catalog_command",
)
