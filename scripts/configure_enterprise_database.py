"""显式启用已迁移企业的受限数据库角色；先停止服务，再以 profile 所有者运行。

默认 dry-run 不连接数据库、不生成凭证、不改配置。--apply 只配置当前企业，
不迁移 schema、不备份、不修改业务行，也不旋转未关联的同名角色。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from pathlib import Path
from typing import Never

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infra.db.schema import assert_database_schema_current
from infra.db.tenant_security import (
    assert_tenant_database_isolation,
    provision_tenant_role,
    tenant_database_role,
)
from infra.pilot.config import PilotConfig, PilotError, exclusive_profile_lock
from infra.pilot.mailbox_config import MailboxConfig

DatabaseConfig = PilotConfig | MailboxConfig


class ConfigurationFailure(RuntimeError):
    """只有本模块固定分类可越过 CLI 边界。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        raise ConfigurationFailure("arguments_invalid")


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = _SafeParser(
        description="配置当前企业的受限数据库角色；必须先显式迁移并停止业务服务",
        allow_abbrev=False,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--profile", type=Path)
    source.add_argument("--mailbox-config", type=Path)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def _engine(url: SecretStr) -> AsyncEngine:
    return create_async_engine(
        url.get_secret_value(), pool_size=1, max_overflow=0, hide_parameters=True,
    )


async def _role_oid(connection: AsyncConnection, role: str) -> int | None:
    return await connection.scalar(text(
        "SELECT oid FROM pg_catalog.pg_roles WHERE rolname = :role"
    ), {"role": role})


async def _cleanup_created_role(admin: AsyncEngine, role: str, expected_oid: int) -> None:
    """只清理本次事务实际创建且仍有同一 OID 的角色；不接业务对象删除能力。"""
    async with admin.begin() as connection:
        current = await _role_oid(connection, role)
        if current is None:
            return
        if current != expected_oid:
            raise ConfigurationFailure("role_cleanup_identity_changed")
        # role 只来自固定哈希派生，不能来自 CLI 任意标识符。
        await connection.execute(text(f'DROP OWNED BY "{role}"'))
        await connection.execute(text(f'DROP ROLE "{role}"'))


def _restore_original_config(
    path: Path, original: DatabaseConfig, candidate: DatabaseConfig,
) -> None:
    """write 可能在原子替换后 fsync 失败；先核对/恢复原配置，才允许删除新角色。"""
    try:
        observed = type(original).read(path)
        if observed == original:
            return
        if observed != candidate:
            raise ConfigurationFailure("profile_restore_uncertain")
        original.write(path)
        if type(original).read(path) != original:
            raise ConfigurationFailure("profile_restore_uncertain")
    except Exception:  # noqa: BLE001 -- 文件或验证异常可能包含凭证，统一固定分类
        raise ConfigurationFailure("profile_restore_uncertain") from None


async def configure(
    profile: Path, config: DatabaseConfig, *, config_path: Path | None = None,
) -> str:
    """调用者持有 profile 锁；Mailbox 必须显式指定文件，不复制角色配置流程。"""
    if config_path is None:
        if not isinstance(config, PilotConfig):
            raise ConfigurationFailure("configuration_path_required")
        config_path = profile / "config.json"
    if config_path.parent != profile or config_path.is_symlink():
        raise ConfigurationFailure("configuration_path_invalid")
    if type(config).read(config_path) != config:
        raise ConfigurationFailure("configuration_profile_changed")
    role = tenant_database_role(config.tenant_id)
    admin = _engine(config.migration_database_url)
    created_oid: int | None = None
    candidate: DatabaseConfig | None = None
    written = False
    phase = "schema_not_current"
    try:
        await assert_database_schema_current(admin)
        if config.database_username == role:
            phase = "configured_role_verification_failed"
            runtime = _engine(config.database_url)
            try:
                await assert_tenant_database_isolation(runtime, config.tenant_id)
            finally:
                await runtime.dispose()
            return "already_configured"

        phase = "role_prepare_failed"
        password = SecretStr(secrets.token_urlsafe(32))
        candidate = type(config).model_validate({
            **config.model_dump(mode="python"),
            "database_username": role,
            "database_runtime_password": password,
        })
        async with admin.begin() as connection:
            if await _role_oid(connection, role) is not None:
                raise ConfigurationFailure("role_exists_unassociated")
            await provision_tenant_role(
                connection, config.tenant_id, password, create_only=True,
            )
            created_oid = await _role_oid(connection, role)
            if created_oid is None:
                raise ConfigurationFailure("created_role_identity_missing")

        phase = "new_role_verification_failed"
        runtime = _engine(candidate.database_url)
        try:
            await assert_tenant_database_isolation(runtime, config.tenant_id)
        finally:
            await runtime.dispose()

        phase = "profile_write_failed"
        candidate.write(config_path)
        written = True
        return "configured"
    except BaseException as failure:
        if created_oid is not None and not written:
            if candidate is not None:
                _restore_original_config(config_path, config, candidate)
            try:
                await _cleanup_created_role(admin, role, created_oid)
            except Exception:  # noqa: BLE001 -- 不能回显数据库对象或底层异常参数
                raise ConfigurationFailure("created_role_cleanup_failed") from None
        if isinstance(failure, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(failure, ConfigurationFailure):
            raise
        raise ConfigurationFailure(phase) from None
    finally:
        try:
            await admin.dispose()
        except Exception:  # noqa: BLE001 -- 已写回配置不得因关闭连接失败而删角色
            raise ConfigurationFailure("administrative_connection_cleanup_failed") from None


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse(argv)
        if args.mailbox_config is not None:
            config_path = args.mailbox_config
            profile = config_path.parent
            config_type = MailboxConfig
        else:
            profile = args.profile
            config_path = profile / "config.json"
            config_type = PilotConfig
        with exclusive_profile_lock(profile):
            config = config_type.read(config_path)
            if not args.apply:
                action = (
                    "verify_configured_role"
                    if config.database_username == tenant_database_role(config.tenant_id)
                    else "create_and_verify_runtime_role"
                )
                print(json.dumps({"status": "dry_run", "action": action}))
                return 0
            status = asyncio.run(configure(profile, config, config_path=config_path))
        print(json.dumps({"status": status}))
        return 0
    except ConfigurationFailure as failure:
        print(json.dumps({"status": "failed", "reason": failure.reason}), file=sys.stderr)
        return 1
    except PilotError:
        print(json.dumps({"status": "failed", "reason": "profile_invalid_or_busy"}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"status": "failed", "reason": "interrupted"}), file=sys.stderr)
        return 130
    except Exception:  # noqa: BLE001 -- CLI 绝不回显配置、DSN 或凭证
        print(json.dumps({"status": "failed", "reason": "configuration_failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
