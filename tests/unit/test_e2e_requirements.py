"""真实浏览器 E2E gate 在 CI 必需、本地可选时的 Docker fail-closed 契约。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_E2E_NODE = (
    "tests/e2e/test_opportunity_board.py::"
    "test_real_opportunity_board_and_handoff_queue"
)


def _run_without_docker_cli(tmp_path: Path, *, require_e2e: bool) -> subprocess.CompletedProcess[str]:
    environ = {
        **os.environ,
        "PATH": str(tmp_path),
    }
    if require_e2e:
        environ["TRADEOS_REQUIRE_E2E"] = "1"
    else:
        environ.pop("TRADEOS_REQUIRE_E2E", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", _E2E_NODE, "-q", "-W", "error"],
        cwd=_REPO_ROOT,
        env=environ,
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )


def test_required_e2e_fails_when_docker_is_unavailable(tmp_path: Path) -> None:
    """CI 标记 E2E 必需时，缺 Docker CLI 必须产生非零 pytest exit。"""
    result = _run_without_docker_cli(tmp_path, require_e2e=True)

    assert result.returncode != 0
    assert "Docker 不可用" in result.stdout


def test_optional_local_e2e_skips_when_docker_is_unavailable(tmp_path: Path) -> None:
    """本地未标记必需时，缺 Docker CLI 仍允许显式 skip。"""
    result = _run_without_docker_cli(tmp_path, require_e2e=False)

    assert result.returncode == 0
    assert "1 skipped" in result.stdout
