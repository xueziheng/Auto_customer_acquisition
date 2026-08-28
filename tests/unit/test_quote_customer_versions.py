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
