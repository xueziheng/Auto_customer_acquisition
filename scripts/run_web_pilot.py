"""持久化本机 Web 内测 CLI：显式初始化/迁移、停止、冷备份与新目标恢复。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Never

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infra.pilot.backup import backup_profile, restore_profile
from infra.pilot.config import PilotConfig, PilotError, exclusive_profile_lock
from infra.pilot.resources import PilotProfile


class SafeParser(argparse.ArgumentParser):
    """未知参数只返回固定错误，不回显误传密码。"""

    def error(self, message: str) -> Never:
        raise PilotError("pilot_input_invalid")


def parser() -> argparse.ArgumentParser:
    result = SafeParser(
        allow_abbrev=False, description="持久化本机 Web 内测；不读取 .env 或外部凭证"
    )
    commands = result.add_subparsers(
        dest="command", required=True, parser_class=SafeParser
    )
    for name in (
        "init",
        "migrate",
        "start",
        "stop",
        "status",
        "backup",
        "restore",
        "accounts",
    ):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--profile", type=Path, required=True)
        if name == "accounts":
            command.add_argument("account_args", nargs=argparse.REMAINDER)
        elif name == "init":
            command.add_argument("--policy-file", type=Path, required=True)
        elif name == "backup":
            command.add_argument("--destination", type=Path, required=True)
        elif name == "restore":
            command.add_argument("--backup", type=Path, required=True)
    return result


def start_profile(path: Path) -> None:
    """交给独立 supervisor，三个真实健康检查通过后返回。"""
    from scripts.pilot_web_supervisor import launch

    launch(path.absolute())


def account_profile(path: Path, argv: list[str]) -> None:
    """只用本 profile 的 tenant/当前 DB，getpass 不接受密码参数。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from apps.api.pilot_accounts import (
        parse_command,
        read_password,
        run_account_command,
    )
    from infra.db.session import create_engine_from
    from shared.schemas.identifiers import TenantId

    command = parse_command(argv)
    password = (
        read_password() if command.action in {"create", "reset-password"} else None
    )
    profile = PilotProfile(path)

    async def execute() -> None:
        engine = create_engine_from(profile.config.database_url.get_secret_value())
        try:
            await run_account_command(
                async_sessionmaker(engine, expire_on_commit=False),
                TenantId(profile.config.tenant_id),
                command,
                password=password,
            )
        finally:
            await engine.dispose()

    try:
        with exclusive_profile_lock(path):
            profile.reload()
            profile.verify_all()
            profile.check_schema_locked()
            asyncio.run(execute())
    finally:
        profile.client.close()


def main(argv: list[str] | None = None) -> int:
    profile: PilotProfile | None = None
    try:
        args = parser().parse_args(argv)
        if args.command == "accounts":
            account_profile(args.profile, args.account_args)
        elif args.command == "init":
            PilotConfig.create(args.profile, args.policy_file)
            profile = PilotProfile(args.profile)
            profile.provision_storage()
            profile.migrate()
            profile.stop()
        elif args.command == "restore":
            profile = restore_profile(args.backup, args.profile)
        elif args.command == "backup":
            backup_profile(args.profile, args.destination)
        elif args.command == "start":
            start_profile(args.profile)
        else:
            profile = PilotProfile(args.profile)
            if args.command == "status":
                print(json.dumps(profile.status(), ensure_ascii=False))
                return 0
            if args.command == "migrate":
                profile.migrate()
            elif args.command == "stop":
                profile.stop()
        print(
            json.dumps(
                {"status": "completed", "command": args.command}, ensure_ascii=False
            )
        )
        return 0
    except PilotError as error:
        print(
            json.dumps({"status": "failed", "reason": error.reason}, ensure_ascii=False)
        )
        return 2
    except KeyboardInterrupt:
        print(
            json.dumps(
                {"status": "failed", "reason": "pilot_interrupted"}, ensure_ascii=False
            )
        )
        return 2
    except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
        print(
            json.dumps(
                {"status": "failed", "reason": "pilot_operation_failed"},
                ensure_ascii=False,
            )
        )
        return 2
    finally:
        if profile is not None:
            profile.client.close()


if __name__ == "__main__":
    raise SystemExit(main())
