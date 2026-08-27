"""真实验收入口默认禁用；不可信参数及异常不泄漏到输出。"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_acceptance_without_opt_in_is_not_run_and_does_not_read_credentials():
    result = subprocess.run(
        [sys.executable, "scripts/accept_research_discovery.py"],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT)},
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "status": "not_run",
        "reason": "explicit_opt_in_required",
        "model": "not_run",
        "outreach": "not_run",
    }
    assert result.stderr == b""


def test_acceptance_live_without_configuration_is_safely_not_run():
    result = subprocess.run(
        [
            sys.executable,
            "scripts/accept_research_discovery.py",
            "--live",
            "--budget-confirmed",
            "--proposal-id",
            "dpr_test",
            "--actor-id",
            "emp_test",
        ],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT)},
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["reason"] == "configuration_missing"
    assert result.stderr == b""


def test_acceptance_rejects_untrusted_cli_values_without_echoing_them(capsys):
    from scripts.accept_research_discovery import main

    assert (
        main(
            {},
            [
                "--live",
                "--budget-confirmed",
                "--proposal-id",
                "invalid/?token=sentinel",
                "--actor-id",
                "emp_test",
            ],
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "sentinel" not in captured.out + captured.err
    assert json.loads(captured.out)["status"] == "not_run"


def test_acceptance_does_not_treat_live_flag_as_budget_confirmation(capsys):
    from scripts.accept_research_discovery import main

    assert main({}, ["--live"]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "confirmed_budget_required"
