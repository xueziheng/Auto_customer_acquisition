"""显式 opt-in 的真实来源验收；默认不读取配置、不访问网络。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections.abc import Mapping, Sequence
from typing import Never


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError("验收参数无效")


def main(
    environ: Mapping[str, str] | None = None, argv: Sequence[str] | None = None
) -> int:
    """输入和底层错误只输出固定分类，不回显引用、参数或异常原文。"""
    result: dict[str, object] = {
        "status": "not_run",
        "reason": "explicit_opt_in_required",
        "model": "not_run",
        "outreach": "not_run",
    }
    exit_code = 0
    try:
        parser = _Parser(add_help=False)
        parser.add_argument("--live", action="store_true")
        parser.add_argument("--budget-confirmed", action="store_true")
        parser.add_argument("--proposal-id")
        parser.add_argument("--actor-id")
        options = parser.parse_args(argv)
        if options.live:
            result["reason"] = "confirmed_budget_required"
            if options.budget_confirmed:
                if any(
                    not isinstance(value, str)
                    or re.fullmatch(r"[A-Za-z0-9_]{1,40}", value) is None
                    for value in (options.proposal_id, options.actor_id)
                ):
                    raise ValueError("验收参数无效")
                from apps.scheduler_worker.research_acceptance import (
                    run_live_acceptance,
                )

                result = asyncio.run(
                    run_live_acceptance(
                        os.environ if environ is None else environ,
                        options.proposal_id,
                        options.actor_id,
                    )
                )
                exit_code = (
                    0
                    if result["status"] == "not_run"
                    or (
                        result["status"] == "completed"
                        and result.get("reason") == "pages_only"
                    )
                    else 3
                )
    except KeyboardInterrupt:
        result = {
            "status": "interrupted",
            "reason": "operator_stopped",
            "model": "not_run",
            "outreach": "not_run",
        }
        exit_code = 3
    except Exception:  # noqa: BLE001 CLI 不能泄漏凭证、DSN 或底层异常
        result = {
            "status": "not_run",
            "reason": "configuration_or_input_invalid",
            "model": "not_run",
            "outreach": "not_run",
        }
        exit_code = 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
