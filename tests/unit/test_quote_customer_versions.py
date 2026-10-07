"""客户发现输入在数据库前拒绝隐式转换与无界分页。"""

import pytest

from domains.quotations.customer_versions import QuoteCustomerVersionsServiceImpl


@pytest.mark.parametrize(
    "limit,before", [(True, None), (0, None), (6, None), (1, False), (1, 0)]
)
async def test_invalid_page_never_enters_dependencies(limit, before):
    service = QuoteCustomerVersionsServiceImpl(
        None, None, None, None, now=lambda: None, maximum_page_size=5
    )
    with pytest.raises(Exception) as error:
        await service.list_versions(
            "tn_00000000000000000000000001",
            "opp_existing",
            actor_id="emp_owner",
            before_version=before,
            limit=limit,
        )
    assert error.value.code == "invalid_input"


@pytest.mark.parametrize("code", ["context_changed", "dependency_unavailable", "storage_inconsistent"])
async def test_only_deterministic_business_failure_becomes_customer_blocker(code):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from domains.quotations.errors import (
        QuoteFileAccessError,
        QuoteFileAccessUnavailableError,
    )
    from domains.quotations.file_access_schemas import QuoteFileScopeFacts
    from tests.unit.test_quote_file_service import file_case

    c = await file_case()
    quote = c.store["quote"]
    actor = c.actor.model_copy(update={"role": "boss"})
    facts = QuoteFileScopeFacts(tenant_id=c.tenant, opportunity_id=quote.content.opportunity_id,
        actor=actor, owner=actor)
    scopes = []
    @asynccontextmanager
    async def scope(tenant, opportunity, actor_id):
        assert (tenant, opportunity, actor_id) == (c.tenant, quote.content.opportunity_id, c.actor_id)
        scopes.append("enter")
        try:
            yield facts
        finally:
            scopes.append("exit")
    repository = AsyncMock()
    repository.customer_version_page.return_value = (quote,)
    @asynccontextmanager
    async def factory(tenant):
        assert tenant == c.tenant
        yield SimpleNamespace(quotes=repository)
    access, files = AsyncMock(), AsyncMock()
    files.list_files.return_value = ()
    failure = QuoteFileAccessError(code) if code == "context_changed" else QuoteFileAccessUnavailableError(code)
    access.authorize.side_effect = failure
    service = QuoteCustomerVersionsServiceImpl(factory, SimpleNamespace(open_file_scope=scope), access, files,
        now=lambda: quote.content.created_at, maximum_page_size=5)
    kwargs = {"actor_id": c.actor_id, "before_version": None, "limit": 1}
    if code == "context_changed":
        page = await service.list_versions(c.tenant, quote.content.opportunity_id, **kwargs)
        assert page.items[0].allowed_actions == ()
        assert {blocker.code for blocker in page.items[0].blockers} == {code}
        assert scopes == ["enter", "exit", "enter", "exit"]
    else:
        with pytest.raises(QuoteFileAccessUnavailableError) as error:
            await service.list_versions(c.tenant, quote.content.opportunity_id, **kwargs)
        assert error.value is failure
        assert scopes == ["enter", "exit"]
    repository.customer_version_page.assert_awaited_once_with(c.tenant, quote.content.opportunity_id,
        before_version=None, limit=2)
