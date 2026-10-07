"""静态类型检查夹具：验证公开准入模型与 Service 入口。"""

from datetime import datetime

from domains.sourcing.admission import AdmissionState
from domains.sourcing.permissions import SourcingActor
from domains.sourcing.schemas import SourcingAdmissionEnqueueCommand
from domains.sourcing.service import SourcingService
from shared.schemas.identifiers import (
    SourcingAdmissionId,
    SourcingCaseId,
    TenantId,
    ValidatedNeedId,
)


def is_waiting(state: AdmissionState) -> bool:
    """模拟领域外只读取公开状态类型的消费者。"""

    return state is AdmissionState.WAITING


async def enqueue_without_constructable_priority_facts(
    service: SourcingService,
    tenant_id: TenantId,
    case_id: SourcingCaseId,
    need_id: ValidatedNeedId,
    ready_at: datetime,
    actor: SourcingActor,
) -> SourcingAdmissionId:
    """证明应用层能无需伪造 facts 创建固定无首快照阻断。"""

    return await service.enqueue_admission(
        tenant_id,
        case_id,
        need_id,
        ready_at=ready_at,
        command=SourcingAdmissionEnqueueCommand(
            blocked_reason="priority_facts_invalid"
        ),
        actor=actor,
    )
