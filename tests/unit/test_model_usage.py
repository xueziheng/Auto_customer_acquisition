"""UTC 窗口与实际 usage 的 Decimal 费用，不把未知补零。"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from shared.schemas.model_invocation import ModelUsage


def test_daily_window_is_utc():
    from tool_gateway.model_usage import window_start

    assert window_start(
        datetime.fromisoformat("2026-09-23T00:00:01+08:00"), 86400
    ) == datetime(2026, 9, 22, tzinfo=UTC)
    with pytest.raises(ValueError):
        window_start(datetime(2026, 9, 22), 86400)  # noqa: DTZ001 - 验证拒绝无时区输入


def test_money_is_exact_and_requires_measured_usage():
    from tool_gateway.model_usage import model_cost

    usage = ModelUsage(input_tokens=100, cached_input_tokens=40, output_tokens=20)
    assert model_cost(usage, Decimal(2), Decimal("0.5"), Decimal(8)) == Decimal(
        "0.0003"
    )
    assert model_cost(usage, Decimal(2), None, Decimal(8)) is None
    missing = ModelUsage(input_tokens=None, cached_input_tokens=None, output_tokens=20)
    assert model_cost(missing, Decimal(2), Decimal("0.5"), Decimal(8)) is None
    with pytest.raises(ValueError):
        model_cost(usage, 2.0, Decimal("0.5"), Decimal(8))
