"""持久化本机 Web 内测 CLI：显式初始化/迁移、停止、冷备份与新目标恢复。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infra.pilot.backup import backup_profile, restore_profile
from infra.pilot.config import PilotConfig, PilotError
from infra.pilot.resources import PilotProfile


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="持久化本机 Web 内测；不读取 .env 或外部凭证"
    )
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("init", "migrate", "start", "stop", "status", "backup", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--profile", type=Path, required=True)
        if name == "init":
            command.add_argument("--policy-file", type=Path, required=True)
        elif name == "backup":
            command.add_argument("--destination", type=Path, required=True)
        elif name == "restore":
            command.add_argument("--backup", type=Path, required=True)
    return result


def start_profile(path: Path) -> None:
    """Task 4 接线点：完整三进程 supervisor 完成前不能宣称 start 成功。"""
    raise PilotError("application_launch_not_configured")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    profile: PilotProfile | None = None
    try:
        if args.command == "init":
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
