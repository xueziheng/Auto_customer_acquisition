"""将供应商额度事实变成平台提示，不把额度状态当作业务采集许可。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from shared.schemas.provider_usage import DataSourceUsageView, ProviderUsageObservation


def usage_view(observation: ProviderUsageObservation | None, *, now: datetime | None = None) -> DataSourceUsageView:
    """低额阈值为总量 10%；统一积分池优先，单项用完不宣称全部服务不可用。"""
    if observation is None:
        return DataSourceUsageView()
    current = now or datetime.now(UTC)
    status: str = observation.outcome
    if observation.outcome == "success":
        pools = [item for item in observation.counters if item.kind == "credits"] or list(observation.counters)
        known = [item for item in pools if item.remaining is not None]
        empty = [item for item in known if item.remaining == 0]
        if not known:
            status = "unknown"
        elif len(empty) == len(pools):
            status = "exhausted"
        elif empty:
            status = "partial_exhausted"
        elif any(item.limit and item.remaining is not None and item.remaining <= item.limit * Decimal("0.1") for item in known):
            status = "low"
        elif len(known) < len(pools):
            status = "unknown"
        else:
            status = "available"
    return DataSourceUsageView(
        status=status, checked_at=observation.checked_at,  # type: ignore[arg-type]
        counters=observation.counters, reset_date=observation.reset_date,
        next_check_at=observation.checked_at + timedelta(seconds=max(60, observation.retry_after_seconds or 0)),
        stale=current - observation.checked_at > timedelta(minutes=10),
    )
