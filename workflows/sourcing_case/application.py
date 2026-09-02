"""公开寻源计划的零副作用草拟、精确运行与保守人工恢复。"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from connectors.search_contracts import SearchCostStatus
from domains.directives.schemas import DirectiveView
from domains.directives.service import DirectiveService
from domains.sourcing.permissions import SourcingActor
from domains.sourcing.schemas import (
    CaseView,
    PublicSourcingPlanCommand,
    SourcingAdmissionManualStartCommand,
    SourcingAdmissionReadView,
    SourcingCaseReadView,
    SourcingCurrentQuotaReadView,
    SourcingNeedSnapshot,
    SourcingReviewCommand,
    SourcingUncertainExecutionReadView,
    SourcingUncertainReconciliationCommand,
)
from domains.sourcing.service import (
    AdmissionBlockedReason,
    AdmissionState,
    PublicPlanStatus,
    PublicSourcingPlan,
    SourcingAdmission,
    SourcingReview,
    SourcingSearchReconciliation,
    SourcingService,
)
from shared.errors import (
    InvalidStateTransition,
    PermissionDenied,
    PolicyViolation,
    TradeOSError,
    TransientError,
    ValidationError,
)
from shared.schemas.identifiers import (
    RunId,
    SourcingAdmissionId,
    SourcingCaseId,
    SourcingPlanId,
    SourcingReviewId,
    TenantId,
)
from tool_gateway.free_search_contracts import (
    SearchQuotaRepository,
    SearchQuotaSnapshot,
)
from workflows.engine.runner import StepStatus, WorkflowEngine, WorkflowRun

_WORKFLOW_TYPE = "sourcing_case"
_WORKFLOW_VERSION = 2
_ADMISSION_READ_ROLES = frozenset({"boss", "product", "sourcing", "finance"})
_ADMISSION_MANUAL_ROLES = frozenset({"boss", "sourcing"})

SourcingAdmissionAttemptOutcome = Literal[
    "admitted", "waiting", "blocked", "pending_recovery"
]


@dataclass(frozen=True)
class SourcingAdmissionPolicyRead:
    """当前 active Directive 中经二次校验的寻源准入段。"""

    directive_id: str
    directive_version: int
    enabled: bool
    batch_limit: int


class SourcingAdmissionPolicyReader(Protocol):
    async def read(self, tenant_id: TenantId) -> SourcingAdmissionPolicyRead | None: ...


class DirectiveSourcingAdmissionPolicyReader:
    """把 active、老板确认过的 Directive 投影为准入策略。"""

    def __init__(self, directives: DirectiveService) -> None:
        if not isinstance(directives, DirectiveService):
            raise ValidationError("寻源准入指令读取器依赖无效")
        self._directives = directives

    async def read(self, tenant_id: TenantId) -> SourcingAdmissionPolicyRead | None:
        read_error = False
        directive: DirectiveView | None = None
        try:
            directive = await self._directives.get_active(tenant_id)
        except Exception:  # noqa: BLE001 - 下层异常可能含连接信息
            read_error = True
        if read_error:
            raise TransientError("寻源准入策略状态暂不可确认") from None
        if directive is None:
            return None
        if not isinstance(directive, DirectiveView):
            raise TransientError("寻源准入策略状态暂不可确认") from None
        section = (
            directive.sourcing_admission_mode,
            directive.automatic_sourcing_admission_enabled,
            directive.sourcing_admission_batch_limit,
        )
        if section == (None, None, None):
            return None
        if (
            not isinstance(directive.directive_id, str)
            or not directive.directive_id
            or directive.directive_id != directive.directive_id.strip()
            or len(directive.directive_id) > 200
            or type(directive.version) is not int
            or directive.version < 1
            or directive.superseded_at is not None
            or directive.sourcing_admission_mode != "cluster_ranked"
            or type(directive.automatic_sourcing_admission_enabled) is not bool
            or type(directive.sourcing_admission_batch_limit) is not int
            or not 1 <= directive.sourcing_admission_batch_limit <= 50
        ):
            raise TransientError("寻源准入策略状态暂不可确认") from None
        return SourcingAdmissionPolicyRead(
            directive_id=directive.directive_id,
            directive_version=directive.version,
            enabled=directive.automatic_sourcing_admission_enabled,
            batch_limit=directive.sourcing_admission_batch_limit,
        )


def sourcing_case_start_context(
    snapshot: SourcingNeedSnapshot, case_id: str
) -> dict[str, object]:
    """只从可信 Need snapshot 生成既有 V2 context，禁止补造业务事实。"""

    if not isinstance(snapshot.product_category.value, str):
        raise ValidationError("可信寻源需求品类必须是文本")

    def normalize(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()

    category = normalize(snapshot.product_category.value)
    if not category:
        raise ValidationError("可信寻源需求品类不能为空")
    keywords = sorted(
        {
            normalized
            for fact in (snapshot.application, snapshot.material, snapshot.size_spec)
            if fact is not None and isinstance(fact.value, str)
            if (normalized := normalize(fact.value))
        }
    )
    return {
        "case_id": case_id,
        "need_id": str(snapshot.need_id),
        "need_snapshot_hash": snapshot.snapshot_hash,
        "product_category": category,
        "keywords": keywords,
    }


class SourcingAdmissionStarter:
    """单条已 claim admission 的唯一 start/bind 编排。"""

    def __init__(
        self,
        *,
        sourcing: SourcingService,
        engine: WorkflowEngine,
        tenant_id: TenantId,
        sourcing_actor: SourcingActor,
        now: Callable[[], datetime],
    ) -> None:
        required = (
            (sourcing, "get_admission_case_snapshot"),
            (sourcing, "complete_admission"),
            (sourcing, "release_admission_claim"),
            (sourcing, "block_admission"),
            (engine, "start"),
        )
        if any(not callable(getattr(value, name, None)) for value, name in required):
            raise ValidationError("寻源准入启动依赖无效")
        if (
            not isinstance(tenant_id, str)
            or not tenant_id
            or tenant_id != tenant_id.strip()
            or not isinstance(sourcing_actor, SourcingActor)
            or sourcing_actor.tenant_id != tenant_id
            or sourcing_actor.scope.value != "system"
            or sourcing_actor.role != "system"
            or not callable(now)
        ):
            raise ValidationError("寻源准入启动 tenant、身份或时钟无效")
        self._sourcing = sourcing
        self._engine = engine
        self._tenant_id = tenant_id
        self._sourcing_actor = sourcing_actor
        self._now = now

    def _current_time(self) -> datetime:
        current = self._now()
        if (
            not isinstance(current, datetime)
            or current.tzinfo is None
            or current.utcoffset() != timedelta(0)
        ):
            raise ValidationError("寻源准入时钟必须返回 UTC 时间")
        return current

    async def admit_one(
        self, admission: SourcingAdmission
    ) -> SourcingAdmissionAttemptOutcome:
        """处理一条已 claim admission；未知提交结果等待租约恢复。"""

        if (
            not isinstance(admission, SourcingAdmission)
            or admission.tenant_id != self._tenant_id
            or admission.state is not AdmissionState.STARTING
            or admission.claim_token is None
        ):
            return "pending_recovery"
        claim_token = admission.claim_token
        try:
            case = await self._sourcing.get_admission_case_snapshot(
                self._tenant_id,
                admission.case_id,
                actor=self._sourcing_actor,
            )
        except Exception as error:  # noqa: BLE001 - 未启动前按已知语义处置
            if isinstance(error, TradeOSError):
                if error.is_retryable:
                    return await self._release(admission, claim_token)
                return await self._block(admission, claim_token)
            return "pending_recovery"
        if case is None:
            return await self._block(admission, claim_token)
        if not isinstance(case, SourcingCaseReadView):
            return "pending_recovery"
        snapshot = case.need_snapshot
        if (
            case.case_id != admission.case_id
            or case.need_id != admission.need_id
            or type(case.workflow_version) is not int
            or case.workflow_version != _WORKFLOW_VERSION
            or case.state != "opened"
            or not isinstance(snapshot, SourcingNeedSnapshot)
            or snapshot.need_id != admission.need_id
            or not isinstance(snapshot.snapshot_hash, str)
            or len(snapshot.snapshot_hash) != 64
            or any(
                character not in "0123456789abcdef"
                for character in snapshot.snapshot_hash
            )
        ):
            return await self._block(admission, claim_token)
        try:
            safe_context = sourcing_case_start_context(snapshot, str(admission.case_id))
        except TradeOSError:
            return await self._block(admission, claim_token)

        try:
            run_id = await self._engine.start(
                self._tenant_id,
                _WORKFLOW_TYPE,
                str(admission.case_id),
                safe_context,
                f"sourcing-case:v2:{self._tenant_id}:{admission.need_id}",
            )
        except TradeOSError as error:
            if error.is_retryable:
                return await self._release(admission, claim_token)
            return await self._block(admission, claim_token)
        except Exception:  # noqa: BLE001 - start 结果未知，严禁即时换键
            return "pending_recovery"
        if not isinstance(run_id, str) or not run_id or run_id != run_id.strip():
            return "pending_recovery"
        try:
            await self._sourcing.complete_admission(
                self._tenant_id,
                admission.admission_id,
                claim_token=claim_token,
                workflow_run_id=RunId(run_id),
                admitted_by=self._sourcing_actor.actor_id,
                admitted_at=self._current_time(),
                actor=self._sourcing_actor,
            )
        except Exception:  # noqa: BLE001 - Run 或 bind 可能已提交
            return "pending_recovery"
        return "admitted"

    async def _release(
        self, admission: SourcingAdmission, claim_token: str
    ) -> SourcingAdmissionAttemptOutcome:
        try:
            await self._sourcing.release_admission_claim(
                self._tenant_id,
                admission.admission_id,
                claim_token=claim_token,
                released_at=self._current_time(),
                actor=self._sourcing_actor,
            )
        except Exception:  # noqa: BLE001 - cleanup 不确定仍保守等待 lease
            return "pending_recovery"
        return "waiting"

    async def _block(
        self, admission: SourcingAdmission, claim_token: str
    ) -> SourcingAdmissionAttemptOutcome:
        try:
            await self._sourcing.block_admission(
                self._tenant_id,
                admission.admission_id,
                reason=AdmissionBlockedReason.CASE_STATE_MISMATCH,
                blocked_at=self._current_time(),
                claim_token=claim_token,
                actor=self._sourcing_actor,
            )
        except Exception:  # noqa: BLE001 - cleanup 不确定仍保守等待 lease
            return "pending_recovery"
        return "blocked"


class SourcingAdmissionPolicyView(BaseModel):
    """Directive 策略的安全投影；不把 scheduler 状态写入寻源域。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    status: Literal[
        "enabled",
        "policy_not_configured",
        "automatic_admission_disabled",
        "policy_status_unknown",
    ]
    directive_id: str | None = Field(default=None, max_length=200)
    directive_version: int | None = Field(default=None, ge=1)
    automatic_admission_enabled: bool | None = None
    batch_limit: int | None = Field(default=None, ge=1, le=50)


class SourcingAdmissionListView(BaseModel):
    """保留服务端顺序的准入列表与当前策略状态。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    policy: SourcingAdmissionPolicyView
    items: tuple[SourcingAdmissionReadView, ...]


class SourcingAdmissionDetailView(BaseModel):
    """单条安全准入事实与当前策略状态。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    policy: SourcingAdmissionPolicyView
    admission: SourcingAdmissionReadView


class SourcingAdmissionApplication:
    """寻源准入的跨域只读与人工启动编排。"""

    def __init__(
        self,
        *,
        sourcing: SourcingService,
        policy: SourcingAdmissionPolicyReader,
        starter: SourcingAdmissionStarter,
        tenant_id: TenantId,
        sourcing_actor: SourcingActor,
        lease_duration: timedelta,
        now: Callable[[], datetime],
    ) -> None:
        required = (
            (sourcing, "list_admissions"),
            (sourcing, "get_admission"),
            (sourcing, "get_admission_case_snapshot"),
            (sourcing, "claim_manual_admission"),
            (policy, "read"),
            (starter, "admit_one"),
        )
        if any(not callable(getattr(value, name, None)) for value, name in required):
            raise ValidationError("寻源准入 application 依赖无效")
        if (
            not isinstance(tenant_id, str)
            or not tenant_id
            or tenant_id != tenant_id.strip()
            or not isinstance(sourcing_actor, SourcingActor)
            or sourcing_actor.tenant_id != tenant_id
            or sourcing_actor.scope.value != "system"
            or sourcing_actor.role != "system"
        ):
            raise ValidationError("寻源准入 application tenant 或系统身份无效")
        if not isinstance(lease_duration, timedelta) or lease_duration <= timedelta(0):
            raise ValidationError("寻源准入 application lease_duration 必须为正时长")
        if not callable(now):
            raise ValidationError("寻源准入 application 时钟无效")
        self._sourcing = sourcing
        self._policy = policy
        self._starter = starter
        self._tenant_id = tenant_id
        self._sourcing_actor = sourcing_actor
        self._lease_duration = lease_duration
        self._now = now

    def _current_time(self) -> datetime:
        current = self._now()
        if (
            not isinstance(current, datetime)
            or current.tzinfo is None
            or current.utcoffset() != timedelta(0)
        ):
            raise ValidationError("寻源准入 application 时钟必须返回 UTC 时间")
        return current

    def _require_actor(
        self,
        tenant_id: TenantId,
        actor: SourcingActor,
        *,
        roles: frozenset[str],
    ) -> None:
        if (
            tenant_id != self._tenant_id
            or not isinstance(actor, SourcingActor)
            or actor.tenant_id != tenant_id
            or actor.scope.value != "tenant"
            or actor.role not in roles
        ):
            raise PermissionDenied("寻源准入 application 拒绝请求")

    async def _policy_view(self) -> SourcingAdmissionPolicyView:
        try:
            policy = await self._policy.read(self._tenant_id)
        except Exception:  # noqa: BLE001 - policy status 是安全投影，不外泄异常
            return SourcingAdmissionPolicyView(status="policy_status_unknown")
        if policy is None:
            return SourcingAdmissionPolicyView(status="policy_not_configured")
        if not isinstance(policy, SourcingAdmissionPolicyRead):
            return SourcingAdmissionPolicyView(status="policy_status_unknown")
        try:
            status: Literal["enabled", "automatic_admission_disabled"] = (
                "enabled" if policy.enabled else "automatic_admission_disabled"
            )
            return SourcingAdmissionPolicyView(
                status=status,
                directive_id=policy.directive_id,
                directive_version=policy.directive_version,
                automatic_admission_enabled=policy.enabled,
                batch_limit=policy.batch_limit,
            )
        except ValueError:
            return SourcingAdmissionPolicyView(status="policy_status_unknown")

    async def _get_admission(
        self,
        admission_id: SourcingAdmissionId,
        *,
        actor: SourcingActor,
    ) -> SourcingAdmissionReadView | None:
        try:
            admission = await self._sourcing.get_admission(
                self._tenant_id,
                admission_id,
                actor=actor,
            )
        except (PermissionDenied, ValidationError, InvalidStateTransition):
            raise
        except Exception:  # noqa: BLE001 - 存储异常不得进入 API
            raise TransientError("寻源准入状态暂不可用") from None
        if admission is None:
            return None
        if (
            not isinstance(admission, SourcingAdmissionReadView)
            or admission.admission_id != admission_id
        ):
            raise TransientError("寻源准入状态暂不可用") from None
        return admission

    async def list_read_view(
        self,
        tenant_id: TenantId,
        *,
        state: str,
        limit: int,
        actor: SourcingActor,
    ) -> SourcingAdmissionListView:
        self._require_actor(tenant_id, actor, roles=_ADMISSION_READ_ROLES)
        try:
            admission_state = AdmissionState(state)
        except (TypeError, ValueError):
            raise ValidationError("admission state 无效") from None
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValidationError("limit 必须是 1..50 的整数")
        try:
            admissions = await self._sourcing.list_admissions(
                self._tenant_id,
                state=admission_state,
                limit=limit,
                now=self._current_time(),
                actor=actor,
            )
        except (PermissionDenied, ValidationError):
            raise
        except Exception:  # noqa: BLE001 - 存储异常不得进入 API
            raise TransientError("寻源准入列表暂不可用") from None
        if not isinstance(admissions, list) or any(
            not isinstance(item, SourcingAdmissionReadView) for item in admissions
        ):
            raise TransientError("寻源准入列表暂不可用") from None
        return SourcingAdmissionListView(
            policy=await self._policy_view(),
            items=tuple(admissions),
        )

    async def get_read_view(
        self,
        tenant_id: TenantId,
        admission_id: SourcingAdmissionId,
        *,
        actor: SourcingActor,
    ) -> SourcingAdmissionDetailView | None:
        self._require_actor(tenant_id, actor, roles=_ADMISSION_READ_ROLES)
        admission = await self._get_admission(admission_id, actor=actor)
        if admission is None:
            return None
        return SourcingAdmissionDetailView(
            policy=await self._policy_view(),
            admission=admission,
        )

    async def admit_one(
        self,
        tenant_id: TenantId,
        admission_id: SourcingAdmissionId,
        *,
        request_id: str,
        actor: SourcingActor,
    ) -> SourcingAdmissionReadView | None:
        self._require_actor(tenant_id, actor, roles=_ADMISSION_MANUAL_ROLES)
        try:
            command = SourcingAdmissionManualStartCommand(request_id=request_id)
        except ValueError:
            raise ValidationError("人工寻源准入 request id 无效") from None

        admission = await self._get_admission(admission_id, actor=actor)
        if admission is None:
            return None
        try:
            case = await self._sourcing.get_admission_case_snapshot(
                self._tenant_id,
                admission.case_id,
                actor=self._sourcing_actor,
            )
        except Exception:  # noqa: BLE001 - canonical Case 读取错误统一脱敏
            raise TransientError("寻源准入 Case 暂不可确认") from None
        if admission.state == AdmissionState.BLOCKED.value:
            raise InvalidStateTransition("寻源准入当前不可人工启动")
        if (
            not isinstance(case, SourcingCaseReadView)
            or case.case_id != admission.case_id
            or case.need_id != admission.need_id
        ):
            raise InvalidStateTransition("寻源准入当前不可人工启动")
        if admission.state == AdmissionState.ADMITTED.value:
            return admission

        try:
            claimed = await self._sourcing.claim_manual_admission(
                self._tenant_id,
                admission_id,
                command,
                claim_expires_at=self._current_time() + self._lease_duration,
                actor=actor,
            )
        except (PermissionDenied, ValidationError, InvalidStateTransition):
            raise
        except Exception:  # noqa: BLE001 - claim 存储异常不得进入 API
            raise TransientError("寻源准入启动状态暂不可确认") from None
        if claimed is None:
            return None
        if (
            not isinstance(claimed, SourcingAdmission)
            or claimed.tenant_id != self._tenant_id
            or claimed.admission_id != admission_id
            or claimed.case_id != admission.case_id
            or claimed.need_id != admission.need_id
        ):
            raise TransientError("寻源准入启动状态暂不可确认") from None
        if claimed.state is AdmissionState.BLOCKED:
            raise InvalidStateTransition("寻源准入当前不可人工启动")
        if claimed.state is AdmissionState.ADMITTED:
            canonical = await self._get_admission(admission_id, actor=actor)
            if (
                canonical is not None
                and canonical.state == AdmissionState.ADMITTED.value
            ):
                return canonical
            raise TransientError("寻源准入启动状态暂不可确认") from None
        if claimed.state is not AdmissionState.STARTING:
            raise TransientError("寻源准入启动状态暂不可确认") from None

        try:
            outcome = await self._starter.admit_one(claimed)
        except Exception:  # noqa: BLE001 - 共享启动路径的异常统一脱敏
            raise TransientError("寻源准入启动状态暂不可确认") from None
        if outcome == "blocked":
            raise InvalidStateTransition("寻源准入当前不可人工启动")
        if outcome != "admitted":
            raise TransientError("寻源准入启动状态暂不可确认") from None
        canonical = await self._get_admission(admission_id, actor=actor)
        if canonical is None or canonical.state != AdmissionState.ADMITTED.value:
            raise TransientError("寻源准入启动状态暂不可确认") from None
        return canonical


class SourcingPublicSearchBlockedError(PolicyViolation):
    """免费账户不满足运行门禁；``stop_code`` 可直接投影到 API/Run Center。"""

    def __init__(self, stop_code: str) -> None:
        super().__init__("公开寻源免费额度门禁未通过")
        self.stop_code = stop_code


class SourcingPlanDeliveryError(TransientError):
    """计划已进入 running，但 Workflow 投递暂时失败；可用同参数重试。"""


def _free_snapshot(
    snapshot: SearchQuotaSnapshot | None, tenant_id: TenantId, needed: int
) -> None:
    if not isinstance(snapshot, SearchQuotaSnapshot):
        raise SourcingPublicSearchBlockedError("quota_status_unknown")
    if snapshot.paygo_enabled is True or snapshot.cost_status is SearchCostStatus.PAID:
        raise SourcingPublicSearchBlockedError("paid_usage_enabled")
    if (
        snapshot.provider != "tavily"
        or snapshot.tenant_id != tenant_id
        or snapshot.cost_status is not SearchCostStatus.FREE
        or snapshot.paygo_enabled is not False
        or snapshot.checked_at is None
        or snapshot.checked_at.tzinfo is None
        or snapshot.checked_at.utcoffset() is None
    ):
        raise SourcingPublicSearchBlockedError("quota_status_unknown")
    if snapshot.remaining < needed:
        raise SourcingPublicSearchBlockedError("quota_exhausted")


async def _read_free_snapshot(
    quota: SearchQuotaRepository, tenant_id: TenantId, needed: int
) -> None:
    """把额度存储的任意读取失败收敛为无异常链的未知免费状态。"""

    snapshot: SearchQuotaSnapshot | None = None
    failed = False
    try:
        snapshot = await quota.snapshot()
    except Exception:  # noqa: BLE001 -- 原始异常可能包含 Provider 凭证或响应
        failed = True
    if failed:
        raise SourcingPublicSearchBlockedError("quota_status_unknown")
    _free_snapshot(snapshot, tenant_id, needed)


def _run_is_bound(
    run: WorkflowRun | None, tenant_id: TenantId, case_id: SourcingCaseId
) -> bool:
    return (
        isinstance(run, WorkflowRun)
        and run.tenant_id == tenant_id
        and run.workflow_type == _WORKFLOW_TYPE
        and run.workflow_version == _WORKFLOW_VERSION
        and run.subject_ref == str(case_id)
        and run.status in {StepStatus.RUNNING, StepStatus.WAITING_EVENT}
        and isinstance(run.context, dict)
        and run.context.get("case_id") == str(case_id)
    )


def _raise_delivery_error(error: Exception) -> None:
    """在原异常上下文外抛固定错误，避免下层自由文本形成异常链。"""

    if isinstance(error, TransientError):
        raise SourcingPlanDeliveryError("寻源计划工作流投递暂不可用") from None
    raise ValidationError("寻源计划工作流投递失败") from None


class SourcingCaseApplication:
    """跨领域协调；不打开 sourcing UoW，也不直接调用 Connector/Gateway。"""

    def __init__(
        self,
        *,
        sourcing: SourcingService,
        quota: SearchQuotaRepository,
        engine: WorkflowEngine,
    ) -> None:
        self._sourcing = sourcing
        self._quota = quota
        self._engine = engine

    async def create_plan(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        command: PublicSourcingPlanCommand,
        *,
        actor: SourcingActor,
    ) -> PublicSourcingPlan:
        """仅保存计划；不读额度、不投递事件、不预留或搜索。"""

        return await self._sourcing.save_public_plan(
            tenant_id, case_id, command, actor=actor
        )

    async def get_current_quota_read_view(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        *,
        actor: SourcingActor,
    ) -> SourcingCurrentQuotaReadView | None:
        """在已授权 Case 边界返回当前安全额度；故障只声明 unknown。"""

        case = await self._sourcing.get_case_read_view(tenant_id, case_id, actor=actor)
        if case is None:
            return None
        snapshot: SearchQuotaSnapshot | None = None
        try:
            snapshot = await self._quota.snapshot()
        except Exception:  # noqa: BLE001 -- Provider/存储自由错误绝不进入 HTTP。
            snapshot = None
        if (
            not isinstance(snapshot, SearchQuotaSnapshot)
            or snapshot.tenant_id != tenant_id
            or snapshot.provider != "tavily"
            or isinstance(snapshot.remaining, bool)
            or not isinstance(snapshot.remaining, int)
            or snapshot.remaining < 0
            or isinstance(snapshot.reservations, bool)
            or not isinstance(snapshot.reservations, int)
            or snapshot.reservations < 0
            or not isinstance(snapshot.cost_status, SearchCostStatus)
            or (
                snapshot.paygo_enabled is not None
                and not isinstance(snapshot.paygo_enabled, bool)
            )
            or (
                snapshot.checked_at is not None
                and (
                    snapshot.checked_at.tzinfo is None
                    or snapshot.checked_at.utcoffset() is None
                )
            )
        ):
            return SourcingCurrentQuotaReadView(
                remaining=None,
                reservations=None,
                cost_status="unknown",
                paygo_enabled=None,
                checked_at=None,
            )
        return SourcingCurrentQuotaReadView(
            remaining=snapshot.remaining,
            reservations=snapshot.reservations,
            cost_status=snapshot.cost_status.value,
            paygo_enabled=snapshot.paygo_enabled,
            checked_at=snapshot.checked_at,
        )

    async def list_uncertain_execution_read_views(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        *,
        actor: SourcingActor,
        limit: int = 50,
    ) -> tuple[SourcingUncertainExecutionReadView, ...] | None:
        """补足安全恢复投影的实时额度条件，不能把不确定请求误标为可重试。"""

        views = await self._sourcing.list_uncertain_execution_read_views(
            tenant_id, case_id, actor=actor, limit=limit
        )
        if views is None:
            return None
        current: list[SourcingUncertainExecutionReadView] = []
        for view in views:
            can_reconcile = False
            if actor.role == "boss" and view.reconciliation is None:
                try:
                    await self._sourcing.get_uncertain_search_execution(
                        tenant_id,
                        case_id,
                        view.run_id,
                        view.request_key,
                        actor=actor,
                    )
                    reservation = await self._quota.get(view.run_id, view.request_key)
                    can_reconcile = (
                        reservation is not None
                        and reservation.tenant_id == tenant_id
                        and reservation.run_id == view.run_id
                        and reservation.request_key == view.request_key
                        and reservation.status == "uncertain"
                    )
                except Exception:  # noqa: BLE001 -- 不确定时保持不可恢复。
                    can_reconcile = False
            current.append(
                view.model_copy(update={"can_current_user_reconcile": can_reconcile})
            )
        return tuple(current)

    @staticmethod
    def _request_id(value: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 200
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValidationError("寻源审核 request_id 无效")
        return value

    async def _review_event_delivered(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        run_id: RunId,
        required_context: dict[str, Any],
        event_type: str,
        payload: dict[str, str],
    ) -> bool:
        transient = False
        delivered: object = False
        try:
            delivered = await self._engine.has_delivered_event(
                tenant_id,
                _WORKFLOW_TYPE,
                str(case_id),
                event_type,
                payload,
                workflow_version=_WORKFLOW_VERSION,
                required_context=required_context,
                run_id=run_id,
            )
        except TransientError:
            transient = True
        except Exception:  # noqa: BLE001 -- 丢弃 Engine 存储中的自由错误
            transient = True
        if transient:
            raise SourcingPlanDeliveryError("寻源审核工作流证据暂不可用") from None
        if not isinstance(delivered, bool):
            raise ValidationError("寻源审核工作流证据无效")
        return delivered

    async def _review_run(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> WorkflowRun:
        transient = False
        run_id = None
        try:
            run_id = await self._engine.find_active_run(
                tenant_id, _WORKFLOW_TYPE, str(case_id)
            )
        except TransientError:
            transient = True
        except Exception:  # noqa: BLE001 -- 丢弃 Engine 自由错误
            transient = True
        if transient:
            raise SourcingPlanDeliveryError("寻源审核工作流暂不可用") from None
        if run_id is None:
            raise ValidationError("寻源审核 Workflow Run 绑定无效")
        run: WorkflowRun | None = None
        transient = False
        try:
            run = await self._engine.get_run(tenant_id, run_id)
        except TransientError:
            transient = True
        except Exception:  # noqa: BLE001 -- 丢弃 Engine 自由错误
            transient = True
        if transient:
            raise SourcingPlanDeliveryError("寻源审核工作流暂不可用") from None
        if not _run_is_bound(run, tenant_id, case_id):
            raise ValidationError("寻源审核 Workflow Run 绑定无效")
        assert run is not None
        if run.run_id != run_id:
            raise ValidationError("寻源审核 Workflow Run 绑定无效")
        return run

    @staticmethod
    def _review_required_context(run: WorkflowRun) -> dict[str, Any]:
        """供应商路径把封存 generation 一并绑定到 owning Run 查询。"""

        required: dict[str, Any] = {"case_id": run.context["case_id"]}
        candidate_ids = run.context.get("supplier_candidate_ids")
        if candidate_ids in (None, []):
            return required
        case_version = run.context.get("candidate_case_version")
        candidate_set_hash = run.context.get("candidate_set_hash")
        if (
            not isinstance(candidate_ids, list)
            or not candidate_ids
            or candidate_ids != sorted(set(candidate_ids))
            or any(
                not isinstance(item, str) or not item.strip() or len(item) > 200
                for item in candidate_ids
            )
            or isinstance(case_version, bool)
            or not isinstance(case_version, int)
            or case_version < 1
            or not isinstance(candidate_set_hash, str)
            or len(candidate_set_hash) != 64
            or any(
                character not in "0123456789abcdef" for character in candidate_set_hash
            )
        ):
            raise ValidationError("寻源审核 Workflow Run generation 绑定无效")
        required.update(
            {
                "supplier_candidate_ids": candidate_ids,
                "candidate_case_version": case_version,
                "candidate_set_hash": candidate_set_hash,
            }
        )
        return required

    async def review(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        command: SourcingReviewCommand,
        *,
        request_id: str,
        actor: SourcingActor,
    ) -> SourcingReview:
        """先保存/重放人工事实，再仅唤醒精确等待中的 V2 Run。"""

        bounded_request_id = self._request_id(request_id)
        transient = False
        failed = False
        review: SourcingReview | None = None
        try:
            review = await self._sourcing.review(
                tenant_id, case_id, command, actor=actor
            )
        except TransientError:
            transient = True
        except Exception:  # noqa: BLE001 -- 不传播 Sourcing/存储自由错误
            failed = True
        if transient:
            raise TransientError("寻源审核保存暂不可用")
        if failed:
            raise ValidationError("寻源审核保存失败")
        if (
            not isinstance(review, SourcingReview)
            or review.tenant_id != tenant_id
            or review.case_id != case_id
            or not isinstance(review.review_id, str)
            or not review.review_id.strip()
        ):
            raise ValidationError("寻源审核事实绑定无效")
        if review.confirmed_by is None or review.confirmed_at is None:
            return review
        case_view: CaseView | None = None
        transient = False
        failed = False
        try:
            case_view = await self._sourcing.get_case(tenant_id, actor, case_id)
        except ValidationError:
            failed = True
        except Exception:  # noqa: BLE001 -- Sourcing/存储自由错误必须固定脱敏。
            transient = True
        if transient:
            raise SourcingPlanDeliveryError("寻源审核 Case 状态暂不可用") from None
        if failed:
            raise ValidationError("寻源审核 Case 状态无效") from None
        if (
            not isinstance(case_view, CaseView)
            or case_view.case_id != str(case_id)
            or not isinstance(case_view.need_id, str)
            or not case_view.need_id.strip()
        ):
            raise ValidationError("寻源审核 Case 绑定无效")
        if case_view.state == "handed_to_costing":
            return review
        payload = {
            "review_id": str(SourcingReviewId(review.review_id)),
            "request_id": bounded_request_id,
        }
        run = await self._review_run(tenant_id, case_id)
        required_context = self._review_required_context(run)
        if await self._review_event_delivered(
            tenant_id,
            case_id,
            run.run_id,
            required_context,
            "SourcingReviewSubmitted",
            payload,
        ):
            return review
        if run.current_step == "await_review":
            event_type = "SourcingReviewSubmitted"
        elif (
            run.current_step == "handoff_costing"
            and run.context.get("sourcing_stop_reason") == "opportunity_required"
        ):
            event_type = "SourcingHandoffRetryRequested"
        else:
            raise ValidationError("寻源审核 Run 不在可唤醒等待边界")
        if await self._review_event_delivered(
            tenant_id,
            case_id,
            run.run_id,
            required_context,
            event_type,
            payload,
        ):
            return review
        transient = False
        accepted: object = False
        try:
            accepted = await self._engine.deliver_event(
                tenant_id, run.run_id, event_type, payload
            )
        except TransientError:
            transient = True
        except Exception:  # noqa: BLE001 -- 不传播 Engine 自由错误
            transient = True
        if transient:
            raise SourcingPlanDeliveryError("寻源审核工作流唤醒暂不可用") from None
        if not isinstance(accepted, bool):
            raise ValidationError("寻源审核工作流唤醒结果无效")
        if not accepted and not await self._review_event_delivered(
            tenant_id,
            case_id,
            run.run_id,
            required_context,
            event_type,
            payload,
        ):
            transient = False
            fresh_run: WorkflowRun | None = None
            try:
                fresh_run = await self._engine.get_run(tenant_id, run.run_id)
            except Exception:  # noqa: BLE001 -- Engine 自由错误必须固定脱敏并可重试
                transient = True
            if transient:
                raise SourcingPlanDeliveryError("寻源审核工作流暂不可用") from None
            if (
                not _run_is_bound(fresh_run, tenant_id, case_id)
                or fresh_run is None
                or fresh_run.run_id != run.run_id
                or self._review_required_context(fresh_run) != required_context
            ):
                raise ValidationError("寻源审核 Workflow Run 绑定无效")
            target_is_fresh = (
                event_type == "SourcingReviewSubmitted"
                and fresh_run.current_step == "await_review"
            ) or (
                event_type == "SourcingHandoffRetryRequested"
                and fresh_run.current_step == "handoff_costing"
                and fresh_run.context.get("sourcing_stop_reason")
                == "opportunity_required"
            )
            if target_is_fresh:
                raise SourcingPlanDeliveryError(
                    "寻源审核工作流尚未进入等待边界"
                ) from None
            raise ValidationError("寻源审核工作流未接受唤醒事件")
        return review

    async def confirm_plan(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        plan_id: SourcingPlanId,
        expected_plan_hash: str,
        *,
        actor: SourcingActor,
    ) -> PublicSourcingPlan:
        """只确认显式计划与哈希；Case 绑定由领域服务原子验证。"""

        plan = await self._sourcing.confirm_public_plan(
            tenant_id,
            plan_id,
            expected_plan_hash,
            actor=actor,
            expected_case_id=case_id,
        )
        return plan

    async def _find_active_run(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> WorkflowRun | None:
        run_id = await self._engine.find_active_run(
            tenant_id, _WORKFLOW_TYPE, str(case_id)
        )
        if run_id is None:
            return None
        run = await self._engine.get_run(tenant_id, run_id)
        if not _run_is_bound(run, tenant_id, case_id):
            raise ValidationError("公开寻源 Workflow Run 绑定无效")
        assert run is not None
        return run

    async def _active_run(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> WorkflowRun:
        run = await self._find_active_run(tenant_id, case_id)
        if run is None:
            raise ValidationError("公开寻源缺少唯一活动 Workflow Run")
        return run

    async def _has_plan_event(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        payload: dict[str, str],
    ) -> bool:
        """只以 Engine 的持久事件指纹作为精确计划已交付证明。"""

        delivered: object = False
        error: Exception | None = None
        try:
            delivered = await self._engine.has_delivered_event(
                tenant_id,
                _WORKFLOW_TYPE,
                str(case_id),
                "SourcingPlanConfirmed",
                payload,
                workflow_version=_WORKFLOW_VERSION,
                required_context={"case_id": str(case_id)},
            )
        except Exception as exc:  # noqa: BLE001 -- 丢弃 Engine/存储自由异常
            error = exc
        if error is not None:
            _raise_delivery_error(error)
        if not isinstance(delivered, bool):
            raise ValidationError("寻源计划工作流事件证据无效")
        return delivered

    async def run(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        plan_id: SourcingPlanId,
        expected_plan_hash: str,
        *,
        actor: SourcingActor,
    ) -> PublicSourcingPlan:
        """通过实时免费门禁后原子标记 running，再唤醒精确 Case Run。"""

        view = await self._sourcing.get_public_plan_run_view(
            tenant_id,
            case_id,
            plan_id,
            expected_plan_hash,
            actor=actor,
        )
        await _read_free_snapshot(
            self._quota, tenant_id, view.active_plan.worst_case_credits
        )
        payload = {"plan_id": str(plan_id), "plan_hash": expected_plan_hash}
        run = await self._find_active_run(tenant_id, case_id)
        if run is None:
            if (
                view.active_plan.status is PublicPlanStatus.RUNNING
                and await self._has_plan_event(tenant_id, case_id, payload)
            ):
                return view.active_plan
            raise ValidationError("公开寻源缺少唯一活动 Workflow Run")
        if (
            view.active_plan.status is PublicPlanStatus.RUNNING
            and await self._has_plan_event(tenant_id, case_id, payload)
        ):
            return view.active_plan
        if run.current_step != "await_public_plan":
            if await self._has_plan_event(tenant_id, case_id, payload):
                refreshed = await self._sourcing.get_public_plan_run_view(
                    tenant_id,
                    case_id,
                    plan_id,
                    expected_plan_hash,
                    actor=actor,
                )
                if refreshed.active_plan.status is PublicPlanStatus.RUNNING:
                    return refreshed.active_plan
            raise ValidationError("公开寻源 Run 不在计划授权边界")
        running = await self._sourcing.authorize_public_plan_run(
            tenant_id,
            case_id,
            plan_id,
            expected_plan_hash,
            actor=actor,
        )
        error: Exception | None = None
        accepted = False
        try:
            accepted = await self._engine.deliver_event(
                tenant_id,
                run.run_id,
                "SourcingPlanConfirmed",
                payload,
            )
        except Exception as exc:  # noqa: BLE001 -- 先丢弃自由异常再固定分类
            error = exc
        if error is not None:
            _raise_delivery_error(error)
        if not accepted and not await self._has_plan_event(tenant_id, case_id, payload):
            raise ValidationError("寻源计划工作流未接受确认事件")
        return running

    async def reconcile_uncertain(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        command: SourcingUncertainReconciliationCommand,
        *,
        actor: SourcingActor,
    ) -> SourcingSearchReconciliation:
        """先保存人工核对事实，再把旧额度键从 uncertain 收紧为 consumed。"""

        execution = await self._sourcing.get_uncertain_search_execution(
            tenant_id,
            case_id,
            command.run_id,
            command.request_key,
            actor=actor,
        )
        reservation = await self._quota.get(command.run_id, command.request_key)
        if (
            reservation is None
            or reservation.tenant_id != tenant_id
            or reservation.run_id != command.run_id
            or reservation.request_key != command.request_key
            or reservation.status not in {"uncertain", "consumed"}
        ):
            raise ValidationError("免费搜索额度记录不是可核对的不确定状态")
        run = await self._active_run(tenant_id, case_id)
        if run.run_id != command.run_id or run.current_step != "public_search":
            raise ValidationError("不确定搜索 Run 不在公开搜索恢复边界")
        reconciliation = await self._sourcing.record_confirmed_consumed_reconciliation(
            tenant_id, case_id, command, actor=actor
        )
        try:
            await self._quota.acknowledge_uncertain_as_consumed(
                command.run_id, command.request_key
            )
        except Exception as acknowledgement_error:  # noqa: BLE001
            # 事实已提交；固定错误支持安全重放且不保留下层自由文本。
            _raise_delivery_error(acknowledgement_error)
        payload = {
            "reconciliation_id": reconciliation.reconciliation_id,
            "execution_id": execution.execution_id,
        }
        delivery_error: Exception | None = None
        accepted = False
        try:
            accepted = await self._engine.deliver_event(
                tenant_id,
                run.run_id,
                "SourcingSearchRetryRequested",
                payload,
            )
        except Exception as exc:  # noqa: BLE001 -- 不传播下层自由错误文本
            delivery_error = exc
        if delivery_error is not None:
            _raise_delivery_error(delivery_error)
        if not accepted:
            delivered = await self._engine.has_delivered_event(
                tenant_id,
                _WORKFLOW_TYPE,
                str(case_id),
                "SourcingSearchRetryRequested",
                payload,
            )
            if not delivered:
                raise ValidationError("寻源搜索恢复事件未被 Workflow 接受")
        return reconciliation


__all__ = (
    "DirectiveSourcingAdmissionPolicyReader",
    "SourcingAdmissionApplication",
    "SourcingAdmissionAttemptOutcome",
    "SourcingAdmissionDetailView",
    "SourcingAdmissionListView",
    "SourcingAdmissionPolicyRead",
    "SourcingAdmissionPolicyReader",
    "SourcingAdmissionPolicyView",
    "SourcingAdmissionStarter",
    "SourcingCaseApplication",
    "SourcingPlanDeliveryError",
    "SourcingPublicSearchBlockedError",
    "sourcing_case_start_context",
)
