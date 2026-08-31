"""公开寻源计划的零副作用草拟、精确运行与保守人工恢复。"""

from __future__ import annotations

from typing import Any

from connectors.search_contracts import SearchCostStatus
from domains.sourcing.permissions import SourcingActor
from domains.sourcing.schemas import (
    PublicSourcingPlanCommand,
    SourcingReviewCommand,
    SourcingUncertainReconciliationCommand,
)
from domains.sourcing.service import (
    PublicPlanStatus,
    PublicSourcingPlan,
    SourcingReview,
    SourcingSearchReconciliation,
    SourcingService,
)
from shared.errors import PolicyViolation, TransientError, ValidationError
from shared.schemas.identifiers import (
    RunId,
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


def _run_is_bound(run: WorkflowRun | None, tenant_id: TenantId, case_id: SourcingCaseId) -> bool:
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
                character not in "0123456789abcdef"
                for character in candidate_set_hash
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
        if not accepted and not await self._has_plan_event(
            tenant_id, case_id, payload
        ):
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
        reconciliation = (
            await self._sourcing.record_confirmed_consumed_reconciliation(
                tenant_id, case_id, command, actor=actor
            )
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
    "SourcingCaseApplication",
    "SourcingPlanDeliveryError",
    "SourcingPublicSearchBlockedError",
)
