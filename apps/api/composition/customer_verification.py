"""本人客户单邮箱验证：归属锁、原 Gateway 与真实 typed 观察落库。"""

from typing import Protocol

from connectors.email_verification.client import (
    EmailVerificationResult,
    VerificationCostNote,
)
from domains.prospecting.customer_followup import CustomerContextReader
from domains.prospecting.customer_verification import (
    CustomerVerificationCommand,
    CustomerVerificationOption,
    CustomerVerificationResult,
    CustomerVerificationView,
)
from domains.prospecting.schemas import (
    ContactPointDetailView,
    ContactPointKind,
    LegalBasisType,
    ProspectAccountDetailView,
    ProspectAccountView,
    VerificationRecordRequest,
    VerificationStatus,
)
from domains.prospecting.service import ProspectingService
from shared.errors import (
    PermissionDenied,
    PolicyViolation,
    TransientError,
    ValidationError,
)
from shared.schemas.identifiers import (
    CandidateAllocationId,
    ContactPointId,
    ProspectAccountId,
    TenantId,
)
from workflows.account_discovery.ports import ContactVerifier

from ..identity import RequestIdentity


class CustomerVerificationPolicy(Protocol):
    async def require_current(
        self, identity: RequestIdentity, account: ProspectAccountView,
        point: ContactPointDetailView,
    ) -> None:
        """核对当前联系点的真实处理依据，不能从公开来源推断许可。"""
        ...


class RecordedContactBasisPolicy:
    """只核对已经记录的处理依据，不推断国家许可或创建评估记录。"""

    async def require_current(
        self, identity: RequestIdentity, account: ProspectAccountView,
        point: ContactPointDetailView,
    ) -> None:
        contact = point.contact_point
        if (account.tenant_id != identity.tenant_id or contact.tenant_id != identity.tenant_id
                or contact.account_id != account.account_id or contact.kind is not ContactPointKind.EMAIL):
            raise PermissionDenied("联系方式处理范围不一致")
        if (not isinstance(point.legal_basis, LegalBasisType) or not point.legal_basis_source.strip()
                or (point.legal_basis is LegalBasisType.LEGITIMATE_INTEREST
                    and (not point.assessment_ref or not point.assessment_ref.strip()))):
            raise PolicyViolation("联系方式缺少真实处理依据或评估记录")


class CustomerVerificationRuntime(Protocol):
    async def compose(self) -> None:
        """只组合精确已验证配置；不声明配置、不发起供应商账户测试。"""
        ...

    async def blocking_reasons(self, tenant_id: TenantId) -> tuple[str, ...]:
        """只返回当前配置的安全固定原因；账户测试不等于已组合。"""
        ...

    async def verifier_for(
        self, identity: RequestIdentity, account_id: ProspectAccountId,
        contact_point_id: ContactPointId,
    ) -> ContactVerifier:
        """显式受信入口按当前身份/联系点组装原 Gateway，不缓存跨请求身份。"""
        ...


class CustomerVerificationApplication:
    def __init__(
        self, reader: CustomerContextReader, prospecting: ProspectingService,
        runtime: CustomerVerificationRuntime | None = None, *,
        policy: CustomerVerificationPolicy | None = None,
    ) -> None:
        self._reader, self._prospecting, self._runtime, self._policy = reader, prospecting, runtime, policy

    async def startup(self) -> None:
        """由 API 生命周期启动显式 runtime；缺配置的入口继续保持关闭。"""
        if self._runtime is not None:
            await self._runtime.compose()

    async def _blocking_reasons(self, tenant_id: TenantId) -> tuple[str, ...]:
        reasons: tuple[str, ...] = ()
        if self._runtime is None:
            reasons += ("provider_not_configured",)
        else:
            reasons += await self._runtime.blocking_reasons(tenant_id)
        if self._policy is None:
            reasons += ("verification_policy_not_configured",)
        return reasons

    async def _detail(self, tenant_id: TenantId, account_id: ProspectAccountId) -> ProspectAccountDetailView:
        detail = await self._prospecting.get_account_detail(tenant_id, account_id)
        if detail.account.account_id != account_id or detail.account.tenant_id != tenant_id:
            raise PermissionDenied("客户详情与当前归属不一致")
        return detail

    @staticmethod
    def _point(detail: ProspectAccountDetailView, tenant_id: TenantId, point_id: ContactPointId) -> ContactPointDetailView:
        for contact in detail.contacts:
            for point in contact.contact_points:
                if point.contact_point.contact_point_id == point_id:
                    if (point.contact_point.tenant_id != tenant_id or point.contact_point.account_id != detail.account.account_id
                            or point.contact_point.kind is not ContactPointKind.EMAIL):
                        raise PermissionDenied("联系方式不属于当前客户")
                    return point
        raise PermissionDenied("联系方式不属于当前客户")

    async def read(self, identity: RequestIdentity, allocation_id: CandidateAllocationId) -> CustomerVerificationView:
        async with self._reader.guard(identity.tenant_id, identity.employee.employee_id, allocation_id) as context:
            context.require_identity(identity.tenant_id, identity.employee.employee_id, identity.employee.user_id)
            allocation = context.require(expected_user_id=identity.employee.user_id, own=True)
            assert allocation.account_id is not None
            detail = await self._detail(identity.tenant_id, allocation.account_id)
            reasons = await self._blocking_reasons(identity.tenant_id)
            options: list[CustomerVerificationOption] = []
            for contact in detail.contacts:
                for point in contact.contact_points:
                    if point.contact_point.kind is not ContactPointKind.EMAIL:
                        continue
                    point = self._point(detail, identity.tenant_id, point.contact_point.contact_point_id)
                    erasure_pending = point.contact_point.verification_cost_note == VerificationCostNote.PRIVACY_REFUSED.value
                    blocked = () if erasure_pending else reasons
                    if not erasure_pending and not blocked and self._policy is not None:
                        try:
                            await self._policy.require_current(identity, detail.account, point)
                        except PolicyViolation:
                            blocked = ("verification_policy_denied",)
                        except TransientError:
                            blocked = ("verification_policy_unknown",)
                    options.append(CustomerVerificationOption(contact=point, can_verify=not blocked, blocking_reasons=blocked))
            return CustomerVerificationView(allocation_id=allocation_id, account_id=allocation.account_id,
                contacts=tuple(options), can_verify=any(option.can_verify for option in options), blocking_reasons=reasons)

    async def verify(
        self, identity: RequestIdentity, allocation_id: CandidateAllocationId,
        command: CustomerVerificationCommand,
    ) -> CustomerVerificationResult:
        async with self._reader.guard(identity.tenant_id, identity.employee.employee_id, allocation_id) as context:
            context.require_identity(identity.tenant_id, identity.employee.employee_id, identity.employee.user_id)
            allocation = context.require(expected_user_id=identity.employee.user_id, own=True)
            assert allocation.account_id is not None
            detail = await self._detail(identity.tenant_id, allocation.account_id)
            point_id = ContactPointId(command.contact_point_id)
            point = self._point(detail, identity.tenant_id, point_id)
            stored = point.contact_point
            if stored.verification_cost_note == VerificationCostNote.PRIVACY_REFUSED.value:
                if stored.verification_provider is None or stored.verification_checked_at is None:
                    raise ValidationError("隐私拒绝观察缺少原始来源")
                await self._prospecting.handle_erasure_request(identity.tenant_id, stored.value)
                return CustomerVerificationResult(
                    allocation_id=allocation_id, contact_point_id=point_id, result=stored.verification,
                    provider=stored.verification_provider, checked_at=stored.verification_checked_at,
                    cache_hit=False, privacy_erased=True,
                )
            if await self._blocking_reasons(identity.tenant_id):
                raise PolicyViolation("当前验证服务或处理政策尚未就绪")
            assert self._runtime is not None and self._policy is not None
            await self._policy.require_current(identity, detail.account, point)
            verifier = await self._runtime.verifier_for(identity, allocation.account_id, point_id)
            result = await verifier.verify(identity.tenant_id, point_id)
            if not isinstance(result, EmailVerificationResult):
                raise ValidationError("邮箱验证结果无效")
            status = VerificationStatus(result.outcome.value)
            cached = result.cost_note is VerificationCostNote.CACHE_HIT
            privacy_claimed = result.privacy_claimed
            if cached:
                current = await self._prospecting.get_contact_point(identity.tenant_id, point_id)
                if (current.account_id != allocation.account_id or current.verification is not status
                        or current.verification_provider != result.provider or current.verification_checked_at != result.checked_at):
                    raise ValidationError("邮箱验证缓存与当前观察不一致")
                privacy_claimed = privacy_claimed or current.verification_cost_note == VerificationCostNote.PRIVACY_REFUSED.value
            else:
                await self._prospecting.record_verification(identity.tenant_id, VerificationRecordRequest(
                    point_id, status, result.provider, result.checked_at, result.cost_note.value))
            if privacy_claimed:
                await self._prospecting.handle_erasure_request(identity.tenant_id, point.contact_point.value)
            return CustomerVerificationResult(allocation_id=allocation_id, contact_point_id=point_id, result=status,
                provider=result.provider, checked_at=result.checked_at, cache_hit=cached, privacy_erased=privacy_claimed)
