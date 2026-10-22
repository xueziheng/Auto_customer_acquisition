"""本人客户公开资料与适配推断的原子应用服务，不验证邮箱或触达。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Protocol

from domains.demand.schemas import SignalCaptureRequest
from domains.demand.service import DemandService
from domains.prospecting.customer_followup import (
    CustomerContextReader,
    CustomerHypothesisOption,
)
from domains.prospecting.customer_preparation import (
    AccountProfilePatch,
    CustomerPreparationCommand,
    CustomerPreparationView,
)
from domains.prospecting.schemas import ContactPointKind, DiscoveredContactRequest
from domains.prospecting.service import ProspectingService
from infra.db.customer_preparation import (
    PostgresCustomerPreparationTransactions as PostgresCustomerPreparationTransactions,  # noqa: PLC0414 -- 兼容旧公共入口
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    CandidateAllocationId,
    ProspectAccountId,
    TenantId,
)
from shared.schemas.provenance import Provenance, SourceType

from ..identity import RequestIdentity


class CustomerPreparationTransactions(Protocol):
    def guard(
        self, tenant_id: TenantId
    ) -> AbstractAsyncContextManager[tuple[ProspectingService, DemandService]]:
        """资料、公开信号、假设与未验证联系方式共用同一提交/回滚边界。"""
        ...


class CustomerPreparationApplication:
    def __init__(
        self,
        reader: CustomerContextReader,
        transactions: CustomerPreparationTransactions,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._reader, self._transactions = reader, transactions
        self._now = now or (lambda: datetime.now(UTC))

    @staticmethod
    async def _view(
        tenant: TenantId,
        allocation_id: CandidateAllocationId,
        account_id: ProspectAccountId,
        prospecting: ProspectingService,
        demand: DemandService,
        *,
        can_prepare: bool,
    ) -> CustomerPreparationView:
        account = await prospecting.get_account_detail(tenant, account_id)
        hypotheses = await demand.list_account_outreach_hypotheses(tenant, account_id)
        return CustomerPreparationView(
            allocation_id=str(allocation_id),
            account=account.account,
            contacts=account.contacts,
            hypotheses=tuple(
                CustomerHypothesisOption(
                    hypothesis_id=str(v.hypothesis_id),
                    category=v.category,
                    status=v.status,
                )
                for v in hypotheses
            ),
            can_prepare=can_prepare,
        )

    async def read(
        self, identity: RequestIdentity, allocation_id: CandidateAllocationId
    ) -> CustomerPreparationView:
        """当前身份/归属保护下读取；管理员查看他人客户不授予修改权。"""
        async with self._reader.guard(
            identity.tenant_id, identity.employee.employee_id, allocation_id
        ) as context:
            context.require_identity(
                identity.tenant_id,
                identity.employee.employee_id,
                identity.employee.user_id,
            )
            allocation = context.require(
                expected_user_id=identity.employee.user_id, own=False
            )
            if allocation.account_id is None:
                raise ValidationError("客户档案尚未关联")
            async with self._transactions.guard(identity.tenant_id) as (
                prospecting,
                demand,
            ):
                return await self._view(
                    identity.tenant_id,
                    allocation_id,
                    allocation.account_id,
                    prospecting,
                    demand,
                    can_prepare=(
                        allocation.employee_id == context.employee_id
                        and context.ownership_owner == context.employee_id
                    ),
                )

    async def prepare(
        self,
        identity: RequestIdentity,
        allocation_id: CandidateAllocationId,
        command: CustomerPreparationCommand,
    ) -> CustomerPreparationView:
        """公开证据不能被解释为客户已验证需求；全部写入同一事务，失败全回滚。"""
        async with self._reader.guard(
            identity.tenant_id, identity.employee.employee_id, allocation_id
        ) as context:
            context.require_identity(
                identity.tenant_id,
                identity.employee.employee_id,
                identity.employee.user_id,
            )
            allocation = context.require(
                expected_user_id=identity.employee.user_id, own=True
            )
            assert allocation.account_id is not None
            command.require_contact_basis()
            now = self._now()
            provenance = Provenance(
                source_type=SourceType.WEB_PAGE,
                source_id=command.snapshot_artifact_ref,
                source_url=command.source_url,
                page_hash=command.page_hash,
                source_quote=command.public_observation,
                extracted_by="human",
                extracted_at=command.observed_at.astimezone(UTC),
                confirmed_by=context.employee_id,
                confirmed_at=now,
            )
            async with self._transactions.guard(identity.tenant_id) as (
                prospecting,
                demand,
            ):
                account = await prospecting.fill_missing_account_profile(
                    identity.tenant_id,
                    allocation.account_id,
                    AccountProfilePatch(
                        command.entity_type, command.industry, provenance
                    ),
                )
                signal_id = await demand.capture_signal(
                    identity.tenant_id,
                    SignalCaptureRequest(
                        signal_type="complementary_category",
                        entity_name=account.name,
                        raw_observation=command.public_observation,
                        observed_at=command.observed_at.astimezone(UTC),
                        source_type="web_page",
                        source_id=command.page_hash,
                        extracted_by="human",
                        source_url=command.source_url,
                        page_hash=command.page_hash,
                        snapshot_artifact_ref=command.snapshot_artifact_ref,
                    ),
                )
                await demand.create_hypothesis(
                    identity.tenant_id,
                    allocation.account_id,
                    command.product_category,
                    [signal_id],
                    command.fit_reasoning,
                    f"employee:{context.employee_id}",
                )
                if command.public_email is not None:
                    assert command.legal_basis is not None
                    from dataclasses import replace

                    await prospecting.record_discovered_contact(
                        identity.tenant_id,
                        DiscoveredContactRequest(
                            account_id=allocation.account_id,
                            kind=ContactPointKind.EMAIL,
                            value=command.public_email,
                            legal_basis=replace(
                                command.legal_basis,
                                collected_at=command.legal_basis.collected_at.astimezone(
                                    UTC
                                ),
                            ),
                        ),
                    )
                return await self._view(
                    identity.tenant_id,
                    allocation_id,
                    allocation.account_id,
                    prospecting,
                    demand,
                    can_prepare=True,
                )
