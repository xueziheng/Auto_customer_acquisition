"""账户发现 Campaign 事件只唤醒三方版本完全一致的等待 run。"""

from __future__ import annotations

from datetime import UTC, datetime

from apps.scheduler_worker.campaign_events import (
    AccountDiscoveryCampaignEventHandlers,
)
from shared.events.catalog import CampaignStateChanged
from shared.schemas.identifiers import CampaignId, RunId, TenantId, new_id


class _Engine:
    def __init__(self) -> None:
        self.deliveries: list[tuple[RunId, dict[str, object]]] = []

    async def deliver_event(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        event_type: str,
        payload: dict[str, object],
    ) -> bool:
        del tenant_id
        assert event_type == "CampaignStateChanged"
        self.deliveries.append((run_id, payload))
        return True


class _VersionedHandler(AccountDiscoveryCampaignEventHandlers):
    def __init__(
        self,
        *,
        engine: _Engine,
        run_id: RunId,
        bound_version: int,
        current_version: int,
    ) -> None:
        super().__init__(
            engine=engine,  # type: ignore[arg-type]
            approvals=object(),  # type: ignore[arg-type]
            factory=object(),  # type: ignore[arg-type]
            tenant_id=TenantId(new_id("tn")),
        )
        self._run_id = run_id
        self._bound_version = bound_version
        self._current_version = current_version

    async def _runs(
        self, campaign_id: str, *, only_waiting: bool
    ) -> list[tuple[RunId, int | None]]:
        del campaign_id
        assert only_waiting is True
        return [(self._run_id, self._bound_version)]

    async def _current_campaign_version(self, campaign_id: str) -> int | None:
        del campaign_id
        return self._current_version


async def test_active_event_does_not_route_when_event_version_is_stale() -> None:
    engine = _Engine()
    run_id = RunId(new_id("run"))
    handler = _VersionedHandler(
        engine=engine,
        run_id=run_id,
        bound_version=3,
        current_version=3,
    )

    await handler.on_campaign_state_changed(
        CampaignStateChanged(
            tenant_id=TenantId(new_id("tn")),
            occurred_at=datetime(2026, 8, 26, tzinfo=UTC),
            campaign_id=CampaignId(new_id("cmp")),
            campaign_version=2,
            state="active",
        )
    )

    assert engine.deliveries == []


async def test_active_event_does_not_route_when_persisted_version_changed() -> None:
    engine = _Engine()
    run_id = RunId(new_id("run"))
    handler = _VersionedHandler(
        engine=engine,
        run_id=run_id,
        bound_version=3,
        current_version=4,
    )

    await handler.on_campaign_state_changed(
        CampaignStateChanged(
            tenant_id=TenantId(new_id("tn")),
            occurred_at=datetime(2026, 8, 26, tzinfo=UTC),
            campaign_id=CampaignId(new_id("cmp")),
            campaign_version=3,
            state="active",
        )
    )

    assert engine.deliveries == []


async def test_active_event_routes_only_exact_event_bound_and_persisted_version() -> None:
    engine = _Engine()
    run_id = RunId(new_id("run"))
    handler = _VersionedHandler(
        engine=engine,
        run_id=run_id,
        bound_version=3,
        current_version=3,
    )
    campaign_id = CampaignId(new_id("cmp"))

    await handler.on_campaign_state_changed(
        CampaignStateChanged(
            tenant_id=TenantId(new_id("tn")),
            occurred_at=datetime(2026, 8, 26, tzinfo=UTC),
            campaign_id=campaign_id,
            campaign_version=3,
            state="active",
        )
    )

    assert engine.deliveries == [
        (
            run_id,
            {
                "campaign_id": str(campaign_id),
                "campaign_version": 3,
                "state": "active",
                "occurred_at": "2026-08-26T00:00:00+00:00",
            },
        )
    ]
