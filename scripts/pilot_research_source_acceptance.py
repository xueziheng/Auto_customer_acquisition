"""用本机私有 profile 运行现有真实来源验收，不在环境或命令行暴露密钥。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Never

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infra.pilot.config import PilotConfig, private_read
from scripts.accept_research_discovery import main as accept_sources


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError("input_invalid")


def main(argv: list[str] | None = None) -> int:
    """所有未满足门禁的调用均保持 not_run；真实 key 仅存活于本进程内存。"""
    try:
        parser = SafeParser(add_help=False, allow_abbrev=False)
        parser.add_argument("--profile", type=Path, required=True)
        parser.add_argument("--live", action="store_true")
        parser.add_argument("--budget-confirmed", action="store_true")
        parser.add_argument("--exclusive-account-confirmed", action="store_true")
        parser.add_argument("--proposal-id")
        parser.add_argument("--actor-id")
        options = parser.parse_args(argv)
        if not options.live or not options.budget_confirmed:
            return accept_sources(
                environ={},
                argv=[
                    *(["--live"] if options.live else []),
                    *(["--budget-confirmed"] if options.budget_confirmed else []),
                ],
            )
        if not options.exclusive_account_confirmed:
            print(json.dumps({
                "status": "not_run",
                "reason": "exclusive_account_not_confirmed",
                "model": "not_run",
                "outreach": "not_run",
            }, ensure_ascii=False, separators=(",", ":")))
            return 0
        if (
            not options.profile.is_absolute()
            or not all(
                isinstance(value, str)
                and re.fullmatch(r"[A-Za-z0-9_]{1,40}", value)
                for value in (options.proposal_id, options.actor_id)
            )
        ):
            raise ValueError("input_invalid")
        profile = options.profile
        config = PilotConfig.read(profile / "config.json")
        key = private_read(profile / "tavily-api-key").decode("ascii").strip()
        if not 32 <= len(key) <= 256 or any(character.isspace() for character in key):
            raise ValueError("configuration_invalid")
        environ = config.runtime_environment()
        environ.update({
            "TAVILY_API_KEY_REF": "PILOT_TAVILY_API_KEY",
            "PILOT_TAVILY_API_KEY": key,
            "PILOT_FINGERPRINT": config.resolve("PILOT_FINGERPRINT"),
            "PILOT_OBJECT_ACCESS": config.resolve("PILOT_OBJECT_ACCESS"),
            "PILOT_OBJECT_SECRET": config.resolve("PILOT_OBJECT_SECRET"),
            "TRADEOS_TAVILY_EXCLUSIVE_ACCOUNT_CONFIRMED": "true",
        })
        return accept_sources(
            environ=environ,
            argv=[
                "--live",
                "--budget-confirmed",
                "--proposal-id", options.proposal_id or "",
                "--actor-id", options.actor_id or "",
            ],
        )
    except Exception:  # noqa: BLE001 固定结果；不输出配置、凭证或底层异常
        print(json.dumps({
            "status": "not_run",
            "reason": "configuration_or_input_invalid",
            "model": "not_run",
            "outreach": "not_run",
        }, ensure_ascii=False, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
