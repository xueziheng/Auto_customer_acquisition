"""公共候选准入、确定性来源计划和公司交付服务契约。"""

from __future__ import annotations

import ipaddress
import re
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from shared.schemas.discovery_sources import get_discovery_source
from shared.schemas.identifiers import (
    AcquisitionJobId,
    CandidateAllocationId,
    EmployeeId,
    ProspectAccountId,
    PublicCandidateId,
    TenantId,
)
from shared.schemas.provenance import Provenance, SourceType

from .projection_diagnostic import SaveSummary, SourceDiagnostic


class InventoryQuery(BaseModel):
    """只接受公开市场与产品标签；不承载企业私有原件。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    region: Literal["africa"] | None = None
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    product_tags: tuple[str, ...] = Field(min_length=1, max_length=20)

    @field_validator("product_tags")
    @classmethod
    def normalize_tags(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """规范标签以确保幂等与精确匹配；禁止空标签。"""
        if any(not v.strip() or len(v) > 100 for v in values):
            raise ValueError("产品标签必须非空且不超过100字符")
        return tuple(sorted({v.strip().casefold() for v in values}))


def canonical_domain(value: str) -> str:
    """只规范公开企业域名；不猜测名称与母子公司关系。"""
    value = value.strip().lower().rstrip(".")
    value = value.removeprefix("www.")
    value = value.encode("idna").decode("ascii")
    if not re.fullmatch(
        r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", value
    ):
        raise ValueError("必须提供企业公开域名")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("域名不能是IP")


# ADR0091：公共共享许可独立于API认证方式；不得由请求方自由添加。
PUBLIC_SOURCE_IDS = frozenset(
    (
        "ocp",
        "ted",
        "find_a_tender",
        "contracts_finder",
        "sam_gov",
        "canadabuys",
        "pncp",
        "compras_gov_br",
        "world_bank",
        "ungm",
        "company_websites",
        "industry_directories",
        "trade_associations",
        "chambers",
        "trade_fairs",
        "dealer_networks",
        "company_procurement",
        "public_procurement_pages",
        "company_news",
        "rss",
        "linkedin_public",
        "trade_record_indexes",
        "singapore_data",
        "gebiz",
        "gdelt",
        "greenhouse",
        "openstreetmap",
        "common_crawl",
        "gleif",
        "blum",
        "nfda",
        "efda",
        "sicci",
        "smcci",
        "eisenwarenmesse",
        "ish",
    )
)


class PublicCandidate(BaseModel):
    """受信采集器白名单；公共网页/API事实不授予触达权。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: PublicCandidateId
    canonical_domain: str
    name: str = Field(min_length=1, max_length=300)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    product_tags: tuple[str, ...] = Field(min_length=1)
    public_emails: tuple[str, ...] = ()
    source_id: str
    source_url: str
    evidence_text: str = Field(min_length=1, max_length=20000)
    observed_at: AwareDatetime
    expires_at: AwareDatetime
    provenance: Provenance

    @field_validator("observed_at", "expires_at")
    @classmethod
    def utc_times(cls, value: datetime) -> datetime:
        """证据时间规范UTC，防跨时区增量更新顺序错误。"""
        return value.astimezone(UTC)

    @field_validator("canonical_domain")
    @classmethod
    def domain(cls, value: str) -> str:
        """企业身份确定性规范。"""
        return canonical_domain(value)

    @field_validator("product_tags")
    @classmethod
    def tags(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """复用查询标签规则。"""
        return InventoryQuery.normalize_tags(values)

    @field_validator("public_emails")
    @classmethod
    def emails(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """规范公开邮箱，不冒充可达性验证。"""
        normalized = tuple(sorted({v.strip().lower() for v in values}))
        if any(not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", v) for v in normalized):
            raise ValueError("公开邮箱格式错误")
        return normalized

    @model_validator(mode="after")
    def admission(self) -> PublicCandidate:
        """拒绝私有来源与无原件证据的自由推广。"""
        source = get_discovery_source(self.source_id)
        p = self.provenance
        u = urlsplit(self.source_url)
        if source is None or source.source_id not in PUBLIC_SOURCE_IDS:
            raise ValueError("来源尚未批准公共共享")
        if (
            u.scheme not in ("http", "https")
            or not u.hostname
            or u.username
            or u.password
        ):
            raise ValueError("来源必须为公开URL")
        canonical_domain(u.hostname)
        if p.source_type not in (SourceType.WEB_PAGE, SourceType.EXTERNAL_API):
            raise ValueError("企业私有资料不能进入公共候选池")
        if (
            p.source_url != self.source_url
            or not p.page_hash
            or not re.fullmatch("[0-9a-f]{64}", p.page_hash)
        ):
            raise ValueError("来源URL和内容哈希必须可追溯")
        if not p.extracted_at.tzinfo or self.expires_at <= self.observed_at:
            raise ValueError("候选时间无效")
        return self


class CandidateAllocation(BaseModel):
    """企业独立交付，不含其他企业员工或沟通事实。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: TenantId
    id: CandidateAllocationId
    employee_id: EmployeeId | None
    candidate: PublicCandidate
    allocated_at: AwareDatetime
    request_key: str
    assigned_at: AwareDatetime | None = None
    assigned_by: EmployeeId | None = None
    account_id: ProspectAccountId | None = None


def validate_assignment_ids(
    allocation_ids: tuple[CandidateAllocationId, ...],
) -> tuple[CandidateAllocationId, ...]:
    """批量命令最多100条且每条唯一，不修剪身份避免改变命令含义。"""
    if (
        not 1 <= len(allocation_ids) <= 100
        or len(set(allocation_ids)) != len(allocation_ids)
        or any(not value.strip() or value != value.strip() for value in allocation_ids)
    ):
        raise ValueError("分配记录必须为1至100个唯一有效ID")
    return allocation_ids


class InventoryAssignmentRequest(BaseModel):
    """只包含目标记录和负责人；公司与操作者必须来自受信会话。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    allocation_ids: tuple[CandidateAllocationId, ...] = Field(
        min_length=1, max_length=100
    )
    employee_id: EmployeeId = Field(min_length=1, max_length=40)

    @field_validator("allocation_ids")
    @classmethod
    def unique_ids(
        cls, values: tuple[CandidateAllocationId, ...]
    ) -> tuple[CandidateAllocationId, ...]:
        """HTTP与内部服务复用确定性批量约束。"""
        return validate_assignment_ids(values)

    @field_validator("employee_id")
    @classmethod
    def valid_employee(cls, value: EmployeeId) -> EmployeeId:
        """员工身份不能为空或含首尾空白。"""
        if not value.strip() or value != value.strip():
            raise ValueError("负责人ID无效")
        return value


class InventoryTransferRequest(BaseModel):
    """显式转交命令，以原负责人和分配时间拒绝陈旧页面覆盖。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    employee_id: EmployeeId = Field(min_length=1, max_length=40)
    expected_employee_id: EmployeeId = Field(min_length=1, max_length=40)
    expected_assigned_at: AwareDatetime | None
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def valid_transfer(self) -> InventoryTransferRequest:
        """负责人必须改变；操作者由服务端身份提供。"""
        if self.employee_id == self.expected_employee_id:
            raise ValueError("请选择其他负责人")
        if any(
            not value.strip() or value != value.strip()
            for value in (self.employee_id, self.expected_employee_id, self.reason)
        ):
            raise ValueError("转交字段不能为空或带首尾空白")
        return self


class AcquisitionJob(BaseModel):
    """缺口任务状态与租约快照；令牌是并发标识，不是凭证。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: TenantId
    id: AcquisitionJobId
    employee_id: EmployeeId
    query: InventoryQuery
    requested_count: int
    remaining_count: int
    status: Literal["queued", "processing", "complete", "failed"]
    attempts: int = 0
    created_at: AwareDatetime
    updated_at: AwareDatetime
    failure_reason: (
        Literal["sources_unavailable", "budget_exhausted", "collector_failed"] | None
    ) = None
    lease_until: AwareDatetime | None = None
    lease_token: str | None = None


class InventoryStatus(BaseModel):
    """当前企业额度与公共库存摘要。"""

    tenant_id: TenantId
    daily_limit: int
    used_today: int
    remaining_today: int
    reset_at: AwareDatetime
    fresh_candidates: int


class InventoryResult(BaseModel):
    """幂等请求返回已交付事实及持久缺口。"""

    allocations: tuple[CandidateAllocation, ...]
    job: AcquisitionJob | None


class SourceState(BaseModel):
    """运行检查结果与历史事实；不能以静态selected当可用。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_id: str
    availability: Literal[
        "available",
        "not_configured",
        "not_integrated",
        "permission_denied",
        "quota_exhausted",
        "unhealthy",
    ]
    cost_class: Literal["free", "included", "paid", "unknown"]
    relevant: bool
    inventory_capability: Literal[
        "not_integrated", "web_discovery", "search_backend", "model_runner"
    ] = "not_integrated"

    @computed_field  # type: ignore[prop-decorator]  # Pydantic只读computed property
    @property
    def source_role(
        self,
    ) -> Literal[
        "company_discovery",
        "procurement_signal",
        "contact_enrichment",
        "incremental_subscription",
        "base_search",
        "model_executor",
    ]:
        """按实际用途区分来源与支持工具，不以目录名称推断就绪。"""
        if self.source_id == "codex_cli":
            return "model_executor"
        if self.source_id in {"tavily", "brave"}:
            return "base_search"
        if self.source_id in {"hunter", "snov"}:
            return "contact_enrichment"
        if self.source_id == "rss":
            return "incremental_subscription"
        if self.source_id in {
            "ocp",
            "ted",
            "find_a_tender",
            "contracts_finder",
            "sam_gov",
            "canadabuys",
            "pncp",
            "compras_gov_br",
            "world_bank",
            "ungm",
            "company_procurement",
            "public_procurement_pages",
        }:
            return "procurement_signal"
        return "company_discovery"

    valid_new_count: int = Field(default=0, ge=0)
    attempts: int = Field(default=0, ge=0)
    last_used_at: AwareDatetime | None = None


def select_sources(states: list[SourceState], request_budget: int) -> tuple[str, ...]:
    """保留一个轮换探索槽位；执行者不得在目标命中后跳过已选来源。"""
    if isinstance(request_budget, bool) or request_budget < 0:
        raise ValueError("来源预算必须为非负整数")
    candidates = {
        s.source_id: s
        for s in states
        if s.source_role not in {"base_search", "model_executor", "contact_enrichment"}
        and s.availability == "available"
        and get_discovery_source(s.source_id) is not None
        and s.cost_class != "unknown"
    }
    if not candidates or request_budget == 0:
        return ()
    oldest = datetime.min.replace(tzinfo=UTC)
    exploration = min(
        candidates.values(),
        key=lambda s: (s.attempts, s.last_used_at or oldest, s.source_id),
    )
    rank = {"free": 0, "included": 1, "paid": 2}
    exploitation = sorted(
        (s for s in candidates.values() if s != exploration),
        key=lambda s: (
            rank[s.cost_class],
            not s.relevant,
            -s.valid_new_count,
            s.attempts,
            s.last_used_at or oldest,
            s.source_id,
        ),
    )
    return tuple(s.source_id for s in exploitation[: request_budget - 1]) + (
        exploration.source_id,
    )


def validate_request_count(count: int) -> int:
    """限制一次交付请求规模，不把搜索次数当成果。"""
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
        raise ValueError("每次请求数量必须在1至100之间")
    return count


class PublicCatalog(Protocol):
    """绑定控制租户的受信投影；不接收业务租户ID或企业资料。"""

    async def fresh(
        self, query: InventoryQuery | None, now: datetime
    ) -> tuple[PublicCandidate, ...]:
        """读取并校验公共白名单，过期资料不返回。"""
        ...

    async def save_public(self, candidates: tuple[PublicCandidate, ...]) -> int:
        """返回原子新增的新鲜企业域名数；仅受信采集组合根调用。"""
        ...

    async def save_public_report(
        self, candidates: tuple[PublicCandidate, ...], query: InventoryQuery
    ) -> SaveSummary:
        """受信采集返回实际保存分支及过滤计数。"""
        ...


class InventoryService(Protocol):
    """企业绑定服务；上层负责角色授权，本层负责原子隔离和去重。"""

    async def status(self, tenant_id: TenantId) -> InventoryStatus:
        """返回额度、重置时间及新鲜公共候选数。"""
        ...

    async def request(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        query: InventoryQuery,
        count: int,
        request_key: str,
    ) -> InventoryResult:
        """优先库存，原子分配与排队；幂等参数不同拒绝。"""
        ...

    async def list_allocations(
        self,
        tenant_id: TenantId,
        allowed_owner_ids: tuple[EmployeeId, ...] | None,
        limit: int,
        cursor: str | None = None,
        *,
        unassigned_only: bool = False,
    ) -> tuple[CandidateAllocation, ...]:
        """None只用于上层已授权的全公司访问，空集合不返回任何人。"""
        ...

    async def get_allocation(
        self, tenant_id: TenantId, allocation_id: CandidateAllocationId
    ) -> CandidateAllocation | None:
        """精确读取公司取得记录；员工授权门面继续限制当前负责人。"""
        ...

    async def assign(
        self,
        tenant_id: TenantId,
        allocation_ids: tuple[CandidateAllocationId, ...],
        employee_id: EmployeeId,
        *,
        acting_employee_id: EmployeeId,
    ) -> tuple[CandidateAllocation, ...]:
        """当前老板原子分配给同公司在职员工；同归属重试，禁止覆盖他人。"""
        ...

    async def transfer(
        self,
        tenant_id: TenantId,
        allocation_id: CandidateAllocationId,
        command: InventoryTransferRequest,
        *,
        acting_employee_id: EmployeeId,
    ) -> CandidateAllocation:
        """当前老板显式转交，原子更新真实归属并记录历史，不消耗交付额度。"""
        ...

    async def list_jobs(
        self,
        tenant_id: TenantId,
        allowed_owner_ids: tuple[EmployeeId, ...] | None,
        limit: int,
    ) -> tuple[AcquisitionJob, ...]:
        """读取上层授权范围内的未完成任务，供刷新恢复。"""
        ...

    async def get_job(
        self, tenant_id: TenantId, job_id: AcquisitionJobId
    ) -> AcquisitionJob | None:
        """只读取当前企业任务，上层继续检查员工权限。"""
        ...

    async def set_daily_limit(self, tenant_id: TenantId, daily_limit: int) -> None:
        """仅平台授权组合根接线，必须绑定目标公司。"""
        ...


# UN M49 Africa（2026-10-13核实）：包含区域内国家与地区，不表达政治归属。
# https://unstats.un.org/unsd/methodology/m49/
AFRICA_COUNTRIES = frozenset(
    [
        "DZ",
        "EG",
        "LY",
        "MA",
        "SD",
        "TN",
        "EH",
        "BI",
        "KM",
        "DJ",
        "ER",
        "ET",
        "KE",
        "MG",
        "MW",
        "MU",
        "YT",
        "MZ",
        "RE",
        "RW",
        "SC",
        "SO",
        "SS",
        "UG",
        "TZ",
        "ZM",
        "ZW",
        "AO",
        "CM",
        "CF",
        "TD",
        "CG",
        "CD",
        "GQ",
        "GA",
        "ST",
        "BW",
        "SZ",
        "LS",
        "NA",
        "ZA",
        "BJ",
        "BF",
        "CV",
        "CI",
        "GM",
        "GH",
        "GN",
        "GW",
        "LR",
        "ML",
        "MR",
        "NE",
        "NG",
        "SH",
        "SN",
        "SL",
        "TG",
    ]
)


def matches_query(candidate: PublicCandidate, query: InventoryQuery) -> bool:
    """按产品标签与地理相交匹配；匹配不是客户需求确认。"""
    return (
        (query.country is None or candidate.country == query.country)
        and (query.region is None or candidate.country in AFRICA_COUNTRIES)
        and bool(set(query.product_tags).intersection(candidate.product_tags))
    )


class AcquisitionWorker(Protocol):
    """受信后台补充端口；外部采集必须经过Gateway，不由仓储调用。"""

    async def source_history(self, tenant_id: TenantId) -> tuple[SourceHistory, ...]:
        """绑定公司聚合历史用于下一次确定性来源计划。"""
        ...

    async def claim_job(
        self, tenant_id: TenantId, lease_duration: timedelta
    ) -> AcquisitionJob | None:
        """领取排队/租约过期任务。"""
        ...

    async def record_source_attempt(
        self,
        tenant_id: TenantId,
        job_id: AcquisitionJobId,
        lease_token: str,
        source_id: str,
        outcome: str,
        valid_new_count: int,
        *,
        diagnostic: SourceDiagnostic | None = None,
    ) -> None:
        """有效租约保存实际来源结果。"""
        ...

    async def deliver_job(
        self, tenant_id: TenantId, job_id: AcquisitionJobId, lease_token: str
    ) -> InventoryResult:
        """从公共库存交付公司待分配池并持久更新缺口。"""
        ...

    async def fail_job(
        self,
        tenant_id: TenantId,
        job_id: AcquisitionJobId,
        lease_token: str,
        reason: Literal["sources_unavailable", "budget_exhausted", "collector_failed"],
    ) -> None:
        """明确终止任务并保留安全原因。"""
        ...


DEFAULT_DAILY_LIMIT = 50


def plan_allocations(
    candidates: tuple[PublicCandidate, ...],
    domains: set[str],
    emails: set[str],
    count: int,
    daily_limit: int,
    used_today: int,
    now: datetime,
) -> tuple[PublicCandidate, ...]:
    """确定性交付规则：新鲜、企业域名和邮箱均未分配，且剩余额度充足。"""
    selected: list[PublicCandidate] = []
    seen_domains, seen_emails = set(domains), set(emails)
    capacity = min(count, max(0, daily_limit - used_today))
    for candidate in candidates:
        if len(selected) >= capacity:
            break
        if (
            candidate.observed_at > now
            or candidate.expires_at <= now
            or candidate.canonical_domain in seen_domains
            or seen_emails.intersection(candidate.public_emails)
        ):
            continue
        selected.append(candidate)
        seen_domains.add(candidate.canonical_domain)
        seen_emails.update(candidate.public_emails)
    return tuple(selected)


class SourceHistory(BaseModel):
    """实际尝试统计，不推导运行可用或费用结论。"""

    source_id: str
    attempts: int
    valid_new_count: int
    last_used_at: AwareDatetime


def replenishment_target(requested_count: int) -> Literal[50, 100]:
    """补公共库存目标与本次交付数量分离；目标不是保证产出。"""
    validate_request_count(requested_count)
    return 50 if requested_count <= 50 else 100
