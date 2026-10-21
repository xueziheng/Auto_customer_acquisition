"""按真实Enrollment原幂等键读取准备授权关联，不从模型或请求正文推测来源。"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.prospecting.inventory_followup import (
    InventoryFollowupAuthorization,
    PreparationBinding,
)
from infra.db.prospect_inventory_tables import (
    CustomerPreparationItemRow,
    InventoryFollowupRequestRow,
)
from infra.db.tables import OutreachEnrollmentRow
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EnrollmentId, TenantId


class PreparationSendBindingReader:
    def __init__(self, factory: async_sessionmaker[AsyncSession], tenant_id: TenantId) -> None:
        self.factory, self.tenant = factory, tenant_id

    async def read(self, tenant_id: TenantId, enrollment_id: EnrollmentId) -> PreparationBinding | None:
        if tenant_id != self.tenant:
            raise PermissionDenied("发送企业范围无效")
        async with self.factory() as session:
            enrollment = (await session.scalars(select(OutreachEnrollmentRow).where(
                OutreachEnrollmentRow.tenant_id == tenant_id, OutreachEnrollmentRow.enrollment_id == enrollment_id))).one_or_none()
            if enrollment is None:
                raise PermissionDenied("发送入组不存在")
            prefix = "customer-preparation:"
            if not enrollment.idempotency_key.startswith(prefix):
                return None
            parts = enrollment.idempotency_key.split(":")
            if len(parts) != 3:
                raise PermissionDenied("发送准备绑定无效")
            row = (await session.scalars(select(CustomerPreparationItemRow).where(
                CustomerPreparationItemRow.tenant_id == tenant_id, CustomerPreparationItemRow.allocation_id == parts[1],
                CustomerPreparationItemRow.key_hash == parts[2]))).one_or_none()
            request = (await session.scalars(select(InventoryFollowupRequestRow).where(
                InventoryFollowupRequestRow.tenant_id == tenant_id, InventoryFollowupRequestRow.key_hash == parts[2]))).one_or_none()
            if row is None or request is None or request.revoked or row.state == "blocked" or row.stage not in {"enroll", "completed"}:
                raise PermissionDenied("发送准备授权不可用")
            auth = InventoryFollowupAuthorization.model_validate(request.authorization)
            if (auth.scope.campaign_id != enrollment.campaign_id or auth.scope.campaign_version != enrollment.campaign_version
                or row.result_refs.get("account_id") != enrollment.account_id
                or row.result_refs.get("contact_point_id") != enrollment.contact_point_id
                or row.result_refs.get("hypothesis_id") != enrollment.source_hypothesis_id
                or row.ownership_locked_at is None):
                raise PermissionDenied("发送准备结果绑定不一致")
            return PreparationBinding(tenant_id=tenant_id, allocation_id=row.allocation_id, request_key_hash=row.key_hash,
                authorization=auth, result_refs=row.result_refs, ownership_locked_at=row.ownership_locked_at)
