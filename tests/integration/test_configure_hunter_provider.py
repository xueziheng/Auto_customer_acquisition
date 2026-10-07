"""Hunter 配置声明命令只写安全元数据，不装配运行时凭证或 transport。"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from connectors.hunter.transport import HunterApiHttpTransport
from infra.db.session import create_engine_from
from infra.db.tables import ProviderReadinessEventRow
from infra.secrets import EnvironmentSecretResolver
from scripts.configure_hunter_provider import main


def _environ(database_url: str) -> dict[str, str]:
    return {
        "DATABASE_URL": database_url,
        "TRADEOS_TENANT_ID": "tn_01K2C5R6J7ABCDEFGHJKMNPQRS",
        "TRADEOS_HUNTER_CONTACTS_ENABLED": "true",
        "TRADEOS_HUNTER_CONFIGURATION_VERSION": "deploy-v1",
        "TRADEOS_HUNTER_API_KEY_SECRET_REF": "HUNTER_API_KEY_PROD",
        "TRADEOS_HUNTER_API_KEY_VERSION": "key-v1",
    }


async def _persisted_tenant_ids(database_url: str, tenant_id: str) -> list[str]:
    engine = create_engine_from(database_url)
    try:
        async with engine.connect() as connection:
            return list(
                (
                    await connection.execute(
                        select(ProviderReadinessEventRow.tenant_id).where(
                            ProviderReadinessEventRow.tenant_id == tenant_id
                        )
                    )
                ).scalars()
            )
    finally:
        await engine.dispose()


def test_configuration_command_is_idempotent_and_does_not_construct_runtime_dependencies(
    db_url: str, capsys, monkeypatch
) -> None:
    def unexpected_runtime_dependency(*args, **kwargs) -> None:
        del args, kwargs
        raise AssertionError("配置声明不得构造密钥解析器或 Hunter transport")

    monkeypatch.setattr(EnvironmentSecretResolver, "__init__", unexpected_runtime_dependency)
    monkeypatch.setattr(HunterApiHttpTransport, "__init__", unexpected_runtime_dependency)
    environ = _environ(str(db_url))

    first = main(environ, ["--actor-id", "employee:hunter-operator"])
    first_output = json.loads(capsys.readouterr().out)
    replay = main(environ, ["--actor-id", "employee:hunter-operator"])
    replay_output = json.loads(capsys.readouterr().out)

    assert first == 0
    assert replay == 0
    assert first_output == {
        "provider": "hunter",
        "configuration_version": "deploy-v1",
        "state": "validation_not_run",
    }
    assert replay_output == first_output
    assert asyncio.run(
        _persisted_tenant_ids(str(db_url), environ["TRADEOS_TENANT_ID"])
    ) == [environ["TRADEOS_TENANT_ID"]]


def test_configuration_command_rejects_conflict_and_disabled_metadata_without_secret_output(
    db_url: str, capsys
) -> None:
    environ = _environ(str(db_url))
    assert main(environ, ["--actor-id", "employee:hunter-operator"]) == 0
    capsys.readouterr()

    conflicting = dict(environ)
    conflicting["TRADEOS_HUNTER_API_KEY_VERSION"] = "key-v2"
    conflict_status = main(conflicting, ["--actor-id", "employee:hunter-operator"])
    conflict_output = capsys.readouterr()

    disabled = {
        "DATABASE_URL": str(db_url),
        "TRADEOS_TENANT_ID": "tn_01K2C5R6J7ABCDEFGHJKMNPQRS",
        "TRADEOS_HUNTER_CONTACTS_ENABLED": "false",
    }
    disabled_status = main(disabled, ["--actor-id", "employee:hunter-operator"])
    disabled_output = capsys.readouterr()

    assert conflict_status != 0
    assert disabled_status != 0
    assert asyncio.run(
        _persisted_tenant_ids(str(db_url), environ["TRADEOS_TENANT_ID"])
    ) == [environ["TRADEOS_TENANT_ID"]]
    for output in (conflict_output, disabled_output):
        assert "HUNTER_API_KEY_PROD" not in output.out
        assert "HUNTER_API_KEY_PROD" not in output.err
        assert str(db_url) not in output.out
        assert str(db_url) not in output.err
