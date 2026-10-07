"""真实浏览器 E2E gate 在 CI 必需、本地可选时的 Docker fail-closed 契约。"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_E2E_NODE = (
    "tests/e2e/test_opportunity_board.py::"
    "test_real_opportunity_board_and_handoff_queue"
)


def _run_with_unavailable_docker(*, require_e2e: bool) -> subprocess.CompletedProcess[str]:
    environ = dict(os.environ)
    for key in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        environ.pop(key, None)
    if require_e2e:
        environ["TRADEOS_REQUIRE_E2E"] = "1"
    else:
        environ.pop("TRADEOS_REQUIRE_E2E", None)
    # SDK 不依赖 CLI。独占未监听端口使连接必然拒绝，不接触宿主 Docker；
    # TCP 避开 Docker SDK 在 Unix connect 失败时遗失局部 socket 的缺陷。
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        environ["DOCKER_HOST"] = f"tcp://127.0.0.1:{unavailable.getsockname()[1]}"
        return subprocess.run(
            [sys.executable, "-m", "pytest", _E2E_NODE, "-q", "-W", "error"],
            cwd=_REPO_ROOT,
            env=environ,
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
        )


def test_required_e2e_fails_when_docker_is_unavailable() -> None:
    """CI 标记 E2E 必需时，Docker API 不可达必须产生非零 pytest exit。"""
    result = _run_with_unavailable_docker(require_e2e=True)

    assert result.returncode != 0
    assert "Docker 不可用" in result.stdout


def test_optional_local_e2e_skips_when_docker_is_unavailable() -> None:
    """本地未标记必需时，Docker API 不可达仍允许显式 skip。"""
    result = _run_with_unavailable_docker(require_e2e=False)

    assert result.returncode == 0
    assert "1 skipped" in result.stdout
