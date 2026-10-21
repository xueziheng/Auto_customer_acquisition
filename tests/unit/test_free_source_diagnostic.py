"""赠金额度必须排除所有币种充值；不访问账户。"""

import pytest

from scripts.diagnose_free_sources import free_grant_eligible


def row(currency="CNY", total="1.0", grant="1.0", topup="0"):
    return {
        "currency": currency,
        "total_balance": total,
        "granted_balance": grant,
        "topped_up_balance": topup,
    }


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([row()], True),
        ([row(total="0.005", grant="0.005")], True),
        ([row(total="0.004", grant="0.004")], False),
        ([row(), row("USD", total="1", grant="0", topup="1")], False),
        ([row(total="2", topup="1")], False),
        ([row("EUR")], False),
        ([row(grant="NaN")], False),
        ([row(grant="-1")], False),
        ([row(total=1)], False),
        ([row(), row()], False),
        ([], False),
        ([row("USD")], False),
        ([row(), row("USD")], True),
    ],
)
def test_grant_qualification(rows, expected):
    assert (
        free_grant_eligible({"is_available": True, "balance_infos": rows}) is expected
    )
    assert not free_grant_eligible({"is_available": False, "balance_infos": rows})


def test_supplier_http_cap_is_fixed_and_never_refunded():
    from scripts.diagnose_free_sources import SupplierRequestBudget

    budget = SupplierRequestBudget()
    for _ in range(6):
        budget.take()
    with pytest.raises(ValueError, match="http_cap"):
        budget.take()
    assert budget.count == 6
