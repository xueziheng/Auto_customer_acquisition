"""无成本客户版本发现；actions只是检查时点提示，不能复用为授权。"""

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from domains.quotations.context import QuoteContextProvider
from domains.quotations.errors import QuoteFileAccessError, QuoteFileError
from domains.quotations.file_access import (
    QuoteFileAccessService,
    file_access_errors,
    require_quote_file_scope,
)
from domains.quotations.file_access_schemas import (
    QuoteCustomerFileEntry,
    QuoteCustomerVersionPage,
    QuoteCustomerVersionView,
    QuoteFileActionBlocker,
)
from domains.quotations.file_service import QuoteFileService, _identifier
from domains.quotations.version_repository import QuotationUowFactory
from shared.schemas.identifiers import EmployeeId, OpportunityId, TenantId
from shared.schemas.quote_facts import fact_identity, fact_utc


class QuoteCustomerVersionsService(Protocol):
    """客户安全分页不调用内部四角色get/list。"""

    async def list_versions(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        *,
        actor_id: EmployeeId,
        before_version: int | None,
        limit: int,
    ) -> QuoteCustomerVersionPage:
        """返回安全metadata与确定性当前阻断，基础设施故障整页失败。"""
        ...


class QuoteCustomerVersionsServiceImpl:
    """短scope→读页→释放→逐版本文件授权→返回前scope，不嵌套长锁。"""

    def __init__(
        self,
        uow_factory: QuotationUowFactory,
        contexts: QuoteContextProvider,
        access: QuoteFileAccessService,
        files: QuoteFileService,
        *,
        now: Callable[[], datetime],
        maximum_page_size: int,
    ) -> None:
        if type(maximum_page_size) is not int or maximum_page_size <= 0:
            raise QuoteFileError("invalid_input")
        self._uows, self._contexts, self._access, self._files = (
            uow_factory,
            contexts,
            access,
            files,
        )
        self._now, self._maximum = now, maximum_page_size

    async def list_versions(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        *,
        actor_id: EmployeeId,
        before_version: int | None,
        limit: int,
    ) -> QuoteCustomerVersionPage:
        """SQL取limit+1，只有真实后页才给最后version游标。"""
        if (
            type(limit) is not int
            or not 0 < limit <= self._maximum
            or (
                before_version is not None
                and (type(before_version) is not int or before_version <= 0)
            )
        ):
            raise QuoteFileError("invalid_input")
        _identifier(tenant_id, "tn")
        try:
            fact_identity(actor_id)
            fact_identity(opportunity_id)
        except ValueError:
            raise QuoteFileError("invalid_input") from None
        async with file_access_errors():
            async with self._contexts.open_file_scope(
                tenant_id, opportunity_id, actor_id
            ) as scope:
                require_quote_file_scope(scope)
                async with self._uows(tenant_id) as uow:
                    page = await uow.quotes.customer_version_page(
                        tenant_id,
                        opportunity_id,
                        before_version=before_version,
                        limit=limit + 1,
                    )
            items = []
            for quote in page[:limit]:
                c = quote.content
                files = await self._files.list_files(
                    tenant_id, c.quote_id, actor_id=actor_id
                )
                blockers: tuple[QuoteFileActionBlocker, ...] = ()
                try:
                    snapshot = await self._access.authorize(
                        tenant_id, c.quote_id, actor_id=actor_id
                    )
                    now = snapshot.checked_at
                except QuoteFileAccessError as error:
                    now = fact_utc(self._now())
                    blockers = tuple(
                        QuoteFileActionBlocker(action=a, code=error.code)
                        for a in ("generate", "download_current")
                    )
                items.append(
                    QuoteCustomerVersionView(
                        quote_id=c.quote_id,
                        version=c.version,
                        state=quote.state,
                        created_at=c.created_at,
                        valid_until=c.valid_until,
                        is_past_valid_until=c.valid_until <= now,
                        files=tuple(
                            QuoteCustomerFileEntry(
                                file=f,
                                allowed_actions=("read_history",)
                                if blockers
                                else ("download_current", "read_history"),
                            )
                            for f in files
                        ),
                        allowed_actions=() if blockers else ("generate",),
                        blockers=blockers,
                        checked_at=now,
                    )
                )
            async with self._contexts.open_file_scope(
                tenant_id, opportunity_id, actor_id
            ) as scope:
                require_quote_file_scope(scope)
            return QuoteCustomerVersionPage(
                items=tuple(items),
                next_before_version=items[-1].version if len(page) > limit else None,
            )
