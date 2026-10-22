"""来源连接状态的时效展示，页面读取不访问外部网站。"""
from datetime import UTC, datetime, timedelta

from shared.schemas.browser_probe import BrowserProbeObservation, BrowserSourceView


def browser_view(observation: BrowserProbeObservation | None, *, now: datetime | None = None) -> BrowserSourceView:
    """一天以前的成功只显示历史有效，网站故障不等于会话过期。"""
    if observation is None:
        return BrowserSourceView()
    now = now or datetime.now(UTC)
    status = observation.outcome
    stale = observation.checked_at > now + timedelta(minutes=1) or now - observation.checked_at >= timedelta(days=1)
    return BrowserSourceView(
        status="stale" if stale and status == "available" else status,
        checked_at=observation.checked_at,
        next_check_at=observation.checked_at + timedelta(minutes=1),
    )
