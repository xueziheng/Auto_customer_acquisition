"""本地站内模式不放宽生产默认邮件约束。"""

import httpx

from apps.notification_worker.health import (
    NotificationHealthState,
    create_notification_health_app,
)
from apps.notification_worker.runtime import NotificationRuntimeMode


async def test_local_mode_ready_never_claims_email_delivery():
    state = NotificationHealthState(
        delivery_mode=NotificationRuntimeMode.LOCAL_IN_APP.value
    )
    for checkpoint in ("config", "schema", "database", "registry"):
        state.mark_ready(checkpoint)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_notification_health_app(state)),
        base_url="http://test",
    ) as client:
        result = (await client.get("/health/capabilities")).json()
        assert result == {
            "mode": "local_in_app",
            "email": "disabled",
            "in_app": "enabled",
        }


async def test_local_real_persistent_delivery_and_production_still_rejects(
    owned_infrastructure,
):
    import asyncio
    from datetime import UTC, datetime

    import pytest
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from apps.notification_worker.config import NotificationWorkerConfig
    from apps.notification_worker.runtime import (
        notification_worker_runtime,
        run_notification_worker,
    )
    from infra.db.session import create_engine_from
    from infra.pilot.resources import reserve_port
    from notification_gateway.jobs import (
        NotificationContext,
        NotificationJob,
        NotificationKind,
    )
    from notification_gateway.models import NotificationPriority
    from shared.errors import ValidationError
    from shared.schemas.identifiers import (
        EmployeeId,
        NotificationJobId,
        TenantId,
        new_id,
    )

    config = owned_infrastructure.config
    tenant = TenantId(new_id("tn"))
    with reserve_port(0) as listener:
        port = listener.getsockname()[1]
    settings = NotificationWorkerConfig(
        config.database_url, tenant, 1, 10, port, "pilot-test-notification"
    )

    def forbidden_transport(*args):
        pytest.fail("LOCAL_MODE_CREATED_EMAIL_CLIENT")

    with pytest.raises(ValidationError):
        async with notification_worker_runtime(settings):
            pytest.fail("PRODUCTION_ACCEPTED_MISSING_EMAIL")
    async with notification_worker_runtime(
        settings,
        mode=NotificationRuntimeMode.LOCAL_IN_APP,
        transport_factory=forbidden_transport,
    ) as runtime:
        job = NotificationJob(
            NotificationJobId(new_id("njb")),
            tenant,
            EmployeeId(new_id("emp")),
            NotificationPriority.URGENT,
            NotificationContext(
                NotificationKind.COMMITMENT_OVERDUE, new_id("com"), None, None, None
            ),
            "f" * 64,
            "CommitmentOverdue",
            new_id("dedup"),
            datetime.now(UTC),
        )
        assert await runtime.jobs.enqueue(job)

        async def wait(seconds, event):
            event.set()

        result = await run_notification_worker(
            runtime, stop_event=asyncio.Event(), wait=wait
        )
        assert result.jobs_completed == 1
    engine = create_engine_from(config.database_url.get_secret_value())
    try:
        async with async_sessionmaker(engine)() as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM in_app_notifications WHERE tenant_id=:t AND priority='urgent'"
                    ),
                    {"t": tenant},
                )
                == 1
            )
    finally:
        await engine.dispose()


from tests.integration.test_email_inbound_gateway import (
    owned_infrastructure as _owned_infrastructure,
)

owned_infrastructure = _owned_infrastructure
