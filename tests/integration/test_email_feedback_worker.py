"""邮件反馈 worker 的真实 PostgreSQL advisory lock 与 schema 共用适配器。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from shared.schemas.email_feedback import EmailFeedbackPage
from shared.schemas.identifiers import TenantId, new_id


@pytest_asyncio.fixture
async def feedback_worker_engine(db_url: str) -> AsyncEngine:
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def test_lock_key_is_stable_signed_int64_and_length_prefixed() -> None:
    module = importlib.import_module("infra.db.advisory_lock")
    tenant = TenantId(new_id("tn"))
    first = module.derive_advisory_lock_key(tenant, "feedback")
    assert first == module.derive_advisory_lock_key(tenant, "feedback")
    assert -(2**63) <= first <= 2**63 - 1
    assert first != module.derive_advisory_lock_key(tenant, "feedback-2")
    assert module.derive_advisory_lock_key(TenantId("ab"), "c") != module.derive_advisory_lock_key(TenantId("a"), "bc")


@pytest.mark.asyncio
async def test_real_postgres_lock_competition_heartbeat_and_release(
    feedback_worker_engine: AsyncEngine,
) -> None:
    module = importlib.import_module("infra.db.advisory_lock")
    key = module.derive_advisory_lock_key(TenantId(new_id("tn")), "feedback")
    first = module.PostgresAdvisoryLock(feedback_worker_engine, key)
    second = module.PostgresAdvisoryLock(feedback_worker_engine, key)
    assert await first.acquire() is True
    assert first.backend_pid is not None
    assert await first.heartbeat() is True
    assert await second.acquire() is False
    await second.close()
    await first.close()

    winner = module.PostgresAdvisoryLock(feedback_worker_engine, key)
    assert await winner.acquire() is True
    await winner.close()


@pytest.mark.asyncio
async def test_backend_termination_makes_heartbeat_fail_closed(
    feedback_worker_engine: AsyncEngine,
) -> None:
    module = importlib.import_module("infra.db.advisory_lock")
    key = module.derive_advisory_lock_key(TenantId(new_id("tn")), "feedback")
    lease = module.PostgresAdvisoryLock(feedback_worker_engine, key)
    assert await lease.acquire() is True
    backend_pid = lease.backend_pid
    assert backend_pid is not None
    async with feedback_worker_engine.connect() as killer:
        assert (
            await killer.execute(
                text("SELECT pg_terminate_backend(:backend_pid)"),
                {"backend_pid": backend_pid},
            )
        ).scalar_one() is True
        await killer.commit()
    await asyncio.sleep(0)
    assert await lease.heartbeat() is False
    await lease.close()


@pytest.mark.asyncio
async def test_backend_termination_during_fetch_discards_real_page_without_processing(
    feedback_worker_engine: AsyncEngine,
) -> None:
    config_module = importlib.import_module("apps.email_feedback_worker.config")
    health_module = importlib.import_module("apps.email_feedback_worker.health")
    runtime_module = importlib.import_module("apps.email_feedback_worker.runtime")
    lock_module = importlib.import_module("infra.db.advisory_lock")
    holder: dict[str, object] = {}
    processed: list[EmailFeedbackPage] = []

    class _Reader:
        async def fetch(self, *_args: object) -> EmailFeedbackPage:
            lease = holder["lease"]
            backend_pid = lease.backend_pid
            async with feedback_worker_engine.connect() as killer:
                assert (
                    await killer.execute(
                        text("SELECT pg_terminate_backend(:backend_pid)"),
                        {"backend_pid": backend_pid},
                    )
                ).scalar_one() is True
                await killer.commit()
            return EmailFeedbackPage(None, "next", ())

    class _Processor:
        async def process(self, *_args: object) -> object:
            processed.append(_args[-1])
            raise AssertionError("锁丢失后不得处理页面")

    class _Cursor:
        async def get_cursor(self, *_args: object) -> None:
            return None

    class _Metrics:
        def record(self, *_args: object, **_kwargs: object) -> None:
            return None

    def lock_factory(engine: AsyncEngine, key: int) -> object:
        lease = lock_module.PostgresAdvisoryLock(engine, key)
        holder["lease"] = lease
        return lease

    config = config_module.EmailFeedbackWorkerConfig(
        new_id("tn"), "feedback", new_id("sid"), "feedback-v1", enabled=True
    )
    runtime = runtime_module.EmailFeedbackRuntime(
        feedback_worker_engine,
        _Reader(),
        _Processor(),
        config,
        health_module.EmailFeedbackHealthState(),
    )

    result = await runtime_module.run_email_feedback_worker(
        runtime,
        cursor_reader=_Cursor(),
        metrics=_Metrics(),
        lock_factory=lock_factory,
        stop_event=asyncio.Event(),
        install_signal_handlers=False,
    )

    assert result.status is runtime_module.WorkerRunStatus.LOCK_LOST
    assert processed == []


@pytest.mark.asyncio
async def test_shared_schema_probe_accepts_exact_migrated_head(
    feedback_worker_engine: AsyncEngine,
) -> None:
    schema = importlib.import_module("infra.db.schema")
    await schema.assert_database_schema_current(feedback_worker_engine)


class _FeedbackTransport:
    async def search(self, **_kwargs: object) -> None:
        return None

    async def send(self, **_kwargs: object) -> str:
        raise AssertionError("feedback worker 不得注册或调用发送")

    async def get_profile_history_id(self, **_kwargs: object) -> str:
        return "100"

    async def list_feedback_messages(
        self, **_kwargs: object
    ) -> tuple[tuple[str, ...], None]:
        return (), None

    async def list_feedback_history(
        self, **_kwargs: object
    ) -> tuple[tuple[str, ...], None, str]:
        return (), None, "100"

    async def get_raw_message(self, **_kwargs: object) -> bytes:
        raise AssertionError("空页不得读取 message")


class _CleanupFailure(BaseException):
    pass


class _RuntimePrimary(BaseException):
    pass


class _CleanupFailingTransport(_FeedbackTransport):
    async def aclose(self) -> None:
        raise _CleanupFailure


class _TrackingEnvironment(Mapping[str, str]):
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self.reads: list[str] = []

    def __getitem__(self, key: str) -> str:
        self.reads.append(key)
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def _worker_environment(db_url: str) -> dict[str, str]:
    return {
        "DATABASE_URL": db_url,
        "GMAIL_OAUTH_TOKEN_REF": "GMAIL_WORKER_OAUTH_TOKEN",
        "GMAIL_WORKER_OAUTH_TOKEN": "oauth-marker-value",
        "TOOL_CALL_FINGERPRINT_KEY_REF": "TOOL_WORKER_FINGERPRINT_KEY",
        "TOOL_WORKER_FINGERPRINT_KEY": "k" * 32,
        "TOOL_CALL_FINGERPRINT_KEY_VERSION": "feedback-v1",
        "TRADEOS_DEV_MODE": "false",
        "TRADEOS_TENANT_ID": new_id("tn"),
        "TRADEOS_EMAIL_FEEDBACK_GMAIL_BASE_URL": "https://gmail.googleapis.com",
        "TRADEOS_EMAIL_FEEDBACK_MAILBOX_ALIAS": "feedback",
        "TRADEOS_EMAIL_FEEDBACK_SENDING_IDENTITY_ID": new_id("sid"),
        "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "feedback-v1",
        "TRADEOS_EMAIL_FEEDBACK_ENABLED": "true",
        "TRADEOS_EMAIL_FEEDBACK_HEALTH_PORT": "8092",
        "TRADEOS_EMAIL_FEEDBACK_POLL_INTERVAL_SECONDS": "30",
        "TRADEOS_EMAIL_FEEDBACK_PAGE_LIMIT": "100",
    }


@pytest.mark.asyncio
async def test_production_composition_registers_feedback_read_only_and_fetches_typed_page(
    db_url: str,
) -> None:
    runtime = importlib.import_module("apps.email_feedback_worker.runtime")
    environment = _worker_environment(db_url)
    tenant_id = environment["TRADEOS_TENANT_ID"]
    factory = runtime.EmailFeedbackRuntimeFactory(
        environment,
        transport_factory=lambda _base_url: _FeedbackTransport(),
        now=lambda: datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
    )

    async with factory() as application:
        assert application.registered_tool_ids == ("email.feedback.fetch",)
        assert application.runtime.health.is_ready
        page = await application.runtime.reader.fetch(
            TenantId(tenant_id), "feedback", None, 100
        )
        assert page.starting_cursor is None
        assert page.items == ()
        assert page.next_cursor


@pytest.mark.asyncio
async def test_production_composition_resolves_oauth_only_inside_connector_fetch(
    db_url: str,
) -> None:
    runtime = importlib.import_module("apps.email_feedback_worker.runtime")
    environment = _TrackingEnvironment(_worker_environment(db_url))
    tenant_id = environment["TRADEOS_TENANT_ID"]
    environment.reads.clear()
    factory = runtime.EmailFeedbackRuntimeFactory(
        environment,
        transport_factory=lambda _base_url: _FeedbackTransport(),
        now=lambda: datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
    )

    async with factory() as application:
        assert "GMAIL_WORKER_OAUTH_TOKEN" not in environment.reads
        await application.runtime.reader.fetch(
            TenantId(tenant_id), "feedback", None, 100
        )
        assert environment.reads.count("GMAIL_WORKER_OAUTH_TOKEN") == 1


@pytest.mark.asyncio
async def test_runtime_factory_propagates_cleanup_failure_without_primary(
    db_url: str,
) -> None:
    runtime = importlib.import_module("apps.email_feedback_worker.runtime")
    factory = runtime.EmailFeedbackRuntimeFactory(
        _worker_environment(db_url),
        transport_factory=lambda _base_url: _CleanupFailingTransport(),
        now=lambda: datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
    )

    with pytest.raises(_CleanupFailure):
        async with factory():
            pass

    primary = _RuntimePrimary()
    with pytest.raises(_RuntimePrimary) as captured:
        async with factory():
            raise primary
    assert captured.value is primary


@pytest.mark.asyncio
async def test_production_composition_passes_validated_base_url_to_transport(
    db_url: str,
) -> None:
    runtime = importlib.import_module("apps.email_feedback_worker.runtime")
    environment = _worker_environment(db_url)
    environment["TRADEOS_DEV_MODE"] = "true"
    environment["TRADEOS_EMAIL_FEEDBACK_GMAIL_BASE_URL"] = (
        "http://127.0.0.1:18111"
    )
    received: list[str] = []

    def transport_factory(base_url: str) -> _FeedbackTransport:
        received.append(base_url)
        return _FeedbackTransport()

    factory = runtime.EmailFeedbackRuntimeFactory(
        environment,
        transport_factory=transport_factory,
        now=lambda: datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
    )

    async with factory():
        pass

    assert received == ["http://127.0.0.1:18111"]
