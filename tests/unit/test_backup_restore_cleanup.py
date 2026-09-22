"""Task13验收脚手架关闭故障回归；全部资源为替身，无DB/Docker/网络。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from artifact_store.store import RawArtifactKind, RawArtifactMeta
from infra.controlled.config import ControlledError
from shared.schemas.identifiers import new_id
from tests.integration import test_web_core_backup_restore as backup


async def test_transport_close_failure_still_disposes_engine(monkeypatch):
    events = []

    class Engine:
        async def dispose(self):
            events.append("engine.dispose")

    class Transport:
        async def aclose(self):
            events.append("transport.close")
            raise RuntimeError("synthetic close failure")

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def scalar(self, *args):
            return 1

    tenant, artifact = new_id("tn"), new_id("art")
    meta = RawArtifactMeta(
        tenant,
        artifact,
        RawArtifactKind.EMAIL_RAW,
        "a" * 64,
        1,
        "message/rfc822",
        None,
        datetime.now(UTC),
    )

    class Store:
        async def get(self, *args):
            return meta, b"x"

    config = SimpleNamespace(
        database_url=SimpleNamespace(get_secret_value=lambda: "placeholder"),
        runtime_environment=dict,
        tenant_id=tenant,
    )
    monkeypatch.setattr(backup, "_verify", lambda _: config)
    monkeypatch.setattr(backup, "create_engine_from", lambda _: Engine())
    monkeypatch.setattr(
        backup.S3ObjectStoreSettings,
        "from_environ",
        lambda _: SimpleNamespace(raw_max_bytes=1024),
    )
    monkeypatch.setattr(backup, "S3ObjectBlobTransport", lambda *args: Transport())
    monkeypatch.setattr(backup, "async_sessionmaker", lambda *args, **kwargs: Session)
    monkeypatch.setattr(backup, "RawArtifactStoreImpl", lambda *args: Store())
    errors = []
    with pytest.raises(Exception) as failure:
        await backup._artifact(object(), artifact, errors)
    assert errors == ["transport_close_failed"]
    assert events == ["transport.close", "engine.dispose"]
    assert isinstance(failure.value, ControlledError)
    assert str(failure.value) == "backup_resource_cleanup_failed"


def _restore_stubs(
    monkeypatch, *, second_creation_fails=False, first_close_fails=False
):
    events = []
    main_failure = ControlledError("backup_client_construction_failed")

    class Body:
        def read(self, *args):
            return b"synthetic"

        def close(self):
            events.append("body.close")

    class Client:
        def __init__(self, name):
            self.name = name

        def list_objects_v2(self, **kwargs):
            if self.name == "first":
                return {"KeyCount": 1, "Contents": [{"Key": "synthetic"}]}
            return {"KeyCount": 0}

        def get_object(self, **kwargs):
            return {"Body": Body()}

        def put_object(self, **kwargs):
            return None

        def close(self):
            events.append(self.name + ".close")
            if self.name == "first" and first_close_fails:
                raise RuntimeError("synthetic close failure")

    created = 0

    def client(_):
        nonlocal created
        created += 1
        if created == 2 and second_creation_fails:
            raise main_failure
        return Client("first" if created == 1 else "second")

    container = SimpleNamespace(put_archive=lambda *args: True)
    scope = SimpleNamespace(
        containers=SimpleNamespace(ids=["synthetic"], verify=lambda _: container)
    )
    monkeypatch.setattr(backup, "_require_empty_target", lambda *args: None)
    monkeypatch.setattr(backup, "_pg", lambda *args: b"synthetic")
    monkeypatch.setattr(
        backup, "_verify", lambda _: SimpleNamespace(bucket="synthetic")
    )
    monkeypatch.setattr(backup, "_client", client)
    return scope, events, main_failure


def test_second_client_creation_failure_closes_first_and_preserves_primary(monkeypatch):
    scope, events, primary = _restore_stubs(
        monkeypatch, second_creation_fails=True, first_close_fails=True
    )
    errors = []
    with pytest.raises(Exception) as failure:
        backup._restore(scope, scope, errors)
    assert events == ["first.close"]
    assert errors == ["source_client_close_failed"]
    assert failure.value is primary


def test_first_client_close_failure_does_not_skip_second(monkeypatch):
    scope, events, _ = _restore_stubs(monkeypatch, first_close_fails=True)
    errors = []
    with pytest.raises(Exception) as failure:
        backup._restore(scope, scope, errors)
    assert errors == ["source_client_close_failed"]
    assert events.count("first.close") == events.count("second.close") == 1
    assert events.count("body.close") == 1
    assert isinstance(failure.value, ControlledError)
    assert str(failure.value) == "backup_resource_cleanup_failed"


async def test_evidence_write_failure_restores_logging_and_preserves_primary(
    monkeypatch, tmp_path
):
    class Evidence:
        parent = SimpleNamespace(mkdir=lambda **kwargs: None)

        def write_text(self, *args):
            raise OSError("synthetic evidence write failure")

    def failed_source(*args):
        raise ControlledError("backup_source_construction_failed")

    monkeypatch.setattr(backup, "Supervisor", failed_source)
    monkeypatch.setattr(backup, "reserve", lambda _: None)
    monkeypatch.setattr(backup, "EVIDENCE", Evidence())
    prior = logging.root.manager.disable
    try:
        with pytest.raises(Exception) as failure:
            await backup.test_owned_static_pg_and_original_restore_to_distinct_empty_target(
                tmp_path
            )
        observed = logging.root.manager.disable
    finally:
        logging.disable(prior)
    assert observed == prior
    assert isinstance(failure.value, AssertionError)
    assert str(failure.value) == "backup_restore_failed:create_source"
