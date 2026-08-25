"""Alembic 必须忽略 T7/macOS 写入的 AppleDouble 迁移 sidecar。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VERSIONS = _REPO_ROOT / "migrations" / "versions"
_APPLEDOUBLE_MAGIC = b"\x00\x05\x16\x07"


def test_project_alembic_runner_removes_appledouble_sidecar_before_listing_heads() -> None:
    """受控入口清理 AppleDouble 后仍只能发现 0033 这个有效 head。"""
    sidecar = _VERSIONS / "._0033_provider_readiness_events.py"
    unrelated = _VERSIONS / "._preserve-me.txt"
    sidecar.write_bytes(_APPLEDOUBLE_MAGIC + b"\x00" * 22)
    unrelated.write_text("not an Alembic migration", encoding="utf-8")

    try:
        result = subprocess.run(
            [sys.executable, "scripts/run_alembic.py", "heads"],
            cwd=_REPO_ROOT,
            env={"PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "0033 (head)"
        assert unrelated.exists()
    finally:
        sidecar.unlink(missing_ok=True)
        unrelated.unlink(missing_ok=True)


def test_project_alembic_runner_preserves_and_rejects_unrecognized_sidecar() -> None:
    """入口拒绝删除无法确认为 AppleDouble 的同名 Python 文件。"""
    sidecar = _VERSIONS / "._9999_unrecognized_sidecar.py"
    sidecar.write_text('revision = "9999"\ndown_revision = "0033"\n', encoding="utf-8")

    try:
        result = subprocess.run(
            [sys.executable, "scripts/run_alembic.py", "heads"],
            cwd=_REPO_ROOT,
            env={"PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert "拒绝删除无法确认的迁移 sidecar" in result.stderr
        assert sidecar.exists()
    finally:
        sidecar.unlink(missing_ok=True)
