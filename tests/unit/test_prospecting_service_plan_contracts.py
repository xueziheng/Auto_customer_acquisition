"""服务期与排程身份不得通过缺失时区或不完整身份静默获得授权。"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from domains.prospecting.service_plan_contracts import ServicePlanState


def plan_data():
    now = datetime(2026, 10, 10, tzinfo=UTC)
    return {"tenant_id":"tenant-a", "tier":"basic", "version":1,
            "period_started_at":now, "period_ends_at":now + timedelta(days=30),
            "candidates_reserved":0, "searches_reserved":0, "contacts_reserved":0,
            "schedule_enabled":False}


def test_schedule_requires_confirmed_proposal_actor_and_due_time():
    with pytest.raises(ValidationError):
        ServicePlanState(**{**plan_data(), "schedule_enabled": True})


def test_period_rejects_naive_or_reversed_dates():
    data = plan_data()
    with pytest.raises(ValidationError):
        ServicePlanState(**{**data, "period_started_at": data["period_started_at"].replace(tzinfo=None)})
    with pytest.raises(ValidationError):
        ServicePlanState(**{**data, "period_ends_at": data["period_started_at"]})


def test_client_cannot_attach_paid_or_unlimited_flags():
    with pytest.raises(ValidationError):
        ServicePlanState(**plan_data(), unlimited=True)


def test_blank_identities_cannot_enable_schedule():
    with pytest.raises(ValidationError):
        ServicePlanState(**{**plan_data(), "schedule_enabled": True,
            "proposal_id":" ", "acting_user_id":" ",
            "next_run_at":datetime(2026, 10, 10, tzinfo=UTC)})
