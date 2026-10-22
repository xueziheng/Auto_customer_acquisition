"""准备任务持久CAS机械操作，供独立检查点和同事务业务写入复用。"""
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.prospecting.inventory_followup import PreparationClaim
from infra.db.prospect_inventory_tables import (
    CustomerPreparationItemRow,
    InventoryFollowupRequestRow,
)
from shared.errors import PermissionDenied
from shared.schemas.identifiers import TenantId


async def lock_claim(session: AsyncSession, tenant: TenantId, claim: PreparationClaim,
                     now: datetime, *, allow_revoked: bool = False) -> CustomerPreparationItemRow:
    """先锁准备项，授权关联也必须是同一tenant与key；旧lease不可写。"""
    if claim.tenant_id != tenant:
        raise PermissionDenied("准备任务租户不一致")
    row = (await session.scalars(select(CustomerPreparationItemRow).where(
        CustomerPreparationItemRow.tenant_id == tenant,
        CustomerPreparationItemRow.allocation_id == claim.allocation_id,
    ).with_for_update())).one_or_none()
    if (row is None or row.state != "processing" or row.key_hash != claim.request_key_hash
        or row.lease_token != claim.lease_token or row.version != claim.version
        or row.lease_until is None or row.lease_until <= now):
        raise PermissionDenied("准备任务租约已失效")
    request = (await session.scalars(select(InventoryFollowupRequestRow).where(
        InventoryFollowupRequestRow.tenant_id == tenant,
        InventoryFollowupRequestRow.key_hash == row.key_hash,
    ).with_for_update(read=True))).one()
    if request.revoked and not allow_revoked:
        raise PermissionDenied("准备任务授权已撤销")
    return row


def checkpoint(row: CustomerPreparationItemRow, claim: PreparationClaim, *, stage: str,
               now: datetime, result_refs: dict[str, str] | None = None,
               external_started: bool = False, ownership_locked_at: datetime | None = None) -> PreparationClaim:
    """固定阶段与安全引用只前进；未知外部动作须由明确完成结果清除。"""
    transitions = {"authorize": {"assign"}, "assign": {"evidence"},
                   "evidence": {"verify"}, "verify": {"enroll"}, "enroll": {"completed"}}
    if stage != row.stage and stage not in transitions.get(row.stage, set()):
        raise PermissionDenied("准备阶段不可跳过")
    refs = result_refs or {}
    prefixes = {"account_id": "acc", "signal_id": "sig", "hypothesis_id": "hyp",
                "contact_point_id": "cp", "enrollment_id": "enr"}
    if any(k not in prefixes or re.fullmatch(rf"{prefixes[k]}_[0-7][0-9A-HJKMNP-TV-Z]{{25}}", v) is None for k, v in refs.items()):
        raise ValueError("准备任务只允许安全实体引用")
    if any(k in row.result_refs and row.result_refs[k] != v for k, v in refs.items()):
        raise PermissionDenied("准备任务引用不可改写")
    if ownership_locked_at is not None:
        if ownership_locked_at.tzinfo is None or (row.ownership_locked_at is not None and row.ownership_locked_at != ownership_locked_at):
            raise PermissionDenied("准备任务归属版本不可改写")
        row.ownership_locked_at = ownership_locked_at
    merged = row.result_refs | refs
    required = {"evidence": {"account_id"}, "verify": {"account_id", "signal_id", "hypothesis_id", "contact_point_id"},
                "enroll": {"account_id", "hypothesis_id", "contact_point_id"}, "completed": {"enrollment_id"}}
    if not required.get(stage, set()).issubset(merged) or (stage in required and row.ownership_locked_at is None):
        raise PermissionDenied("准备阶段缺少实际结果")
    if stage == "completed" and external_started:
        raise PermissionDenied("未知外部结果不能完成")
    row.result_refs, row.stage, row.external_started = merged, stage, external_started
    row.version += 1
    row.updated_at = now
    if stage == "completed":
        row.state = "enrolled"
        row.lease_until, row.lease_token = None, None
    return claim.model_copy(update={"version": row.version, "stage": stage,
                                    "result_refs": merged, "ownership_locked_at": row.ownership_locked_at})
