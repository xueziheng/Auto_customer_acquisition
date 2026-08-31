"""SourcingCaseHandedToCosting 到唯一 ESTIMATED 成本表的事件编排。"""

from __future__ import annotations

from domains.costing.permissions import CostingActor
from domains.costing.schemas import SourcingEstimateCreate
from domains.costing.service import CostingService
from domains.sourcing.permissions import SourcingActor
from domains.sourcing.schemas import SourcingHandoffSnapshot
from domains.sourcing.service import SourcingService
from shared.errors import TransientError, ValidationError
from shared.events.catalog import SourcingCaseHandedToCosting
from shared.schemas.identifiers import CostSheetId, TenantId


def _dependency_error(
    *, transient: bool, invalid: bool, transient_message: str, invalid_message: str
) -> None:
    if transient:
        raise TransientError(transient_message)
    if invalid:
        raise ValidationError(invalid_message)


class SourcingCostHandoffHandler:
    """只消费精确终态快照；域服务的来源唯一键负责重复投递收敛。"""

    def __init__(
        self,
        *,
        sourcing: SourcingService,
        costing: CostingService,
        tenant_id: TenantId,
        sourcing_actor: SourcingActor,
        costing_actor: CostingActor,
    ) -> None:
        self._sourcing = sourcing
        self._costing = costing
        self._tenant_id = tenant_id
        self._sourcing_actor = sourcing_actor
        self._costing_actor = costing_actor

    async def handle(self, event: object) -> None:
        if not isinstance(event, SourcingCaseHandedToCosting):
            raise ValidationError("成本交接处理器只接受 SourcingCaseHandedToCosting")
        if event.tenant_id != self._tenant_id:
            return

        snapshot: SourcingHandoffSnapshot | None = None
        transient = False
        invalid = False
        try:
            snapshot = await self._sourcing.get_handoff_snapshot(
                self._tenant_id,
                self._sourcing_actor,
                event.case_id,
                event.review_id,
            )
        except TransientError:
            transient = True
        except ValidationError:
            invalid = True
        except Exception:  # noqa: BLE001 -- 存储自由异常可能包含连接信息
            transient = True
        _dependency_error(
            transient=transient,
            invalid=invalid,
            transient_message="寻源成本交接快照暂不可用",
            invalid_message="寻源成本交接快照读取失败",
        )
        if (
            not isinstance(snapshot, SourcingHandoffSnapshot)
            or snapshot.case_id != event.case_id
            or snapshot.review_id != event.review_id
            or snapshot.need_id != event.need_id
            or snapshot.opportunity_id != event.opportunity_id
        ):
            raise ValidationError("寻源成本交接快照与事件不一致")
        if snapshot.quantity < snapshot.moq:
            raise ValidationError("寻源成本交接快照不满足 MOQ")
        applicable = tuple(
            option
            for option in snapshot.price_options
            if option.minimum_quantity <= snapshot.quantity
        )
        if not applicable:
            raise ValidationError("寻源成本交接快照没有适用数量档")
        tier = max(applicable, key=lambda option: option.minimum_quantity)
        command = SourcingEstimateCreate(
            sourcing_case_id=snapshot.case_id,
            primary_option_id=snapshot.primary_option_id,
            supplier_candidate_id=snapshot.supplier_candidate_id,
            product_id=snapshot.product_id,
            opportunity_id=snapshot.opportunity_id,
            quantity=snapshot.quantity,
            unit_amount=tier.unit_amount,
            currency=tier.currency,
            evidence_ref=tier.evidence_ref,
        )

        created: object = None
        transient = False
        invalid = False
        try:
            created = await self._costing.create_sourcing_estimate(
                self._tenant_id, command, actor=self._costing_actor
            )
        except TransientError:
            transient = True
        except ValidationError:
            invalid = True
        except Exception:  # noqa: BLE001 -- 数据库自由异常不得进入 Outbox 日志
            transient = True
        _dependency_error(
            transient=transient,
            invalid=invalid,
            transient_message="寻源估算成本创建暂不可用",
            invalid_message="寻源估算成本来源内容不一致",
        )
        if not isinstance(created, str) or not created.strip():
            raise ValidationError("寻源估算成本创建结果无效")
        CostSheetId(created)


__all__ = ("SourcingCostHandoffHandler",)
