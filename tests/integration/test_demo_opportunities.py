"""S2-12 演示脚本集成冒烟测试。

用 conftest 的 ``db_url`` fixture（隔离 testcontainers Postgres + alembic upgrade head，
repr 脱敏），子进程运行 ``scripts/demo_opportunities.py``（``sys.executable``、仓库根、
仅经 env 注入 DATABASE_URL 和当前 checkout 的 PYTHONPATH）。断言退出码 0、stdout 含中文走查关键字，且 stdout/stderr
**不得包含注入的完整 db_url**。

RED：脚本缺失 → 子进程非零（非 Docker/fixture/语法假红）。Docker 不可用时按现有
fixture 语义 skip（环境结果）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_WALKTHROUGH_KEYWORDS = (
    "分桶",
    "门槛解释",
    "InvalidStateTransition",
    "MissingLossReasonError",
    "died_at_state",
    "loss_reason",
    "closed_by",
)


def _run_demo(database_url: str) -> subprocess.CompletedProcess[str]:
    """用受限环境启动真实子进程，便于独立验证 checkout 隔离。"""
    # 最小权限：不给子进程继承其余环境变量（不传父进程潜在 token/secret）。
    # 脚本模式不会将 cwd 加入 sys.path，必须显式绑定 checkout，避免 editable 安装串树。
    env = {"DATABASE_URL": database_url, "PYTHONPATH": str(_REPO_ROOT)}
    return subprocess.run(
        [sys.executable, "scripts/demo_opportunities.py"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=_REPO_ROOT,
        env=env,
        check=False,  # 显式：非零退出由断言处理，不抛异常
    )


def test_demo_opportunities_walkthrough(db_url: str) -> None:
    """子进程运行演示脚本：退出码 0、含全部走查关键字、不泄露完整连接串。"""
    result = _run_demo(str(db_url))
    assert result.returncode == 0, "演示脚本运行失败（不输出连接内容）"

    for keyword in _WALKTHROUGH_KEYWORDS:
        assert keyword in result.stdout, f"stdout 缺少走查关键字：{keyword}"

    # 明确验证不泄露完整连接串（失败 introspection 只打印固定脱敏消息）
    if db_url in result.stdout or db_url in result.stderr:
        pytest.fail("演示脚本不得输出连接串")
