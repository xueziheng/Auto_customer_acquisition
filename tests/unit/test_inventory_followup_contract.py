"""授权搜索只接受精确版本引用，不能接收客户端伪造的准备授权。"""

import pytest
from pydantic import ValidationError

from apps.api.routers.prospect_inventory import InventoryRequestBody
from shared.schemas.identifiers import new_id


def test_search_accepts_exact_scope_reference():
    scope = {"campaign_id": new_id("cmp"), "campaign_version": 2}
    body = InventoryRequestBody.model_validate(
        {"query": {"product_tags": ["hinges"]}, "count": 1, "followup_scope": scope}
    )
    assert body.followup_scope.campaign_version == 2


@pytest.mark.parametrize(
    "scope",
    [
        {"campaign_id": "other", "campaign_version": 1},
        {"campaign_id": new_id("cmp"), "campaign_version": 0},
        {
            "campaign_id": new_id("cmp"),
            "campaign_version": 1,
            "owner_employee_id": new_id("emp"),
        },
    ],
)
def test_search_rejects_invalid_or_forged_scope(scope):
    with pytest.raises(ValidationError):
        InventoryRequestBody.model_validate(
            {"query": {"product_tags": ["hinges"]}, "count": 1, "followup_scope": scope}
        )
