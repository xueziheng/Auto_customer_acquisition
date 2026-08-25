"""Alembic 必须忽略 T7/macOS 写入的 AppleDouble 迁移 sidecar。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VERSIONS = _REPO_ROOT / "migrations" / "versions"
_APPLEDOUBLE_MAGIC = b"\x00\x05\x16\x07"
_APPLEDOUBLE_VERSION = b"\x00\x02\x00\x00"
_APPLEDOUBLE_FILLER = b"Mac OS X        "


def _valid_appledouble_sidecar() -> bytes:
    entry_count = (1).to_bytes(2, byteorder="big")
    header_size = len(_APPLEDOUBLE_MAGIC + _APPLEDOUBLE_VERSION + _APPLEDOUBLE_FILLER)
    entry_table_size = 12
    payload_offset = (header_size + len(entry_count) + entry_table_size).to_bytes(
        4, byteorder="big"
    )
    entry = (9).to_bytes(4, byteorder="big") + payload_offset + (1).to_bytes(
        4, byteorder="big"
    )
    return _APPLEDOUBLE_MAGIC + _APPLEDOUBLE_VERSION + _APPLEDOUBLE_FILLER + entry_count + entry + b"\0"


def test_project_alembic_runner_removes_appledouble_sidecar_before_listing_heads() -> None:
    """受控入口清理 AppleDouble 后仍只能发现 0037 这个有效 head。"""
    sidecar = _VERSIONS / "._0036_reply_acceptance_fixes.py"
    unrelated = _VERSIONS / "._preserve-me.txt"
    sidecar.write_bytes(_valid_appledouble_sidecar())
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
        assert result.stdout.strip() == "0037 (head)"
        assert unrelated.exists()
    finally:
        sidecar.unlink(missing_ok=True)
        unrelated.unlink(missing_ok=True)


def test_project_alembic_runner_preserves_and_rejects_malformed_magic_prefix() -> None:
    """只含 AppleDouble 魔数、缺少受支持完整头的文件绝不可删除。"""
    sidecar = _VERSIONS / "._9998_malformed_magic_prefix.py"
    sidecar.write_bytes(_APPLEDOUBLE_MAGIC + b"\0" * 22)

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


def test_project_alembic_runner_rejects_mixed_candidates_without_deleting_valid_sidecar() -> None:
    """任何一个候选未确认时，已确认 AppleDouble 也必须保留。"""
    valid_sidecar = _VERSIONS / "._1000_valid_appledouble.py"
    malformed_sidecar = _VERSIONS / "._2000_unrecognized_sidecar.py"
    valid_sidecar.write_bytes(_valid_appledouble_sidecar())
    malformed_sidecar.write_text(
        'revision = "2000"\ndown_revision = "0033"\n', encoding="utf-8"
    )

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
        assert valid_sidecar.exists()
        assert malformed_sidecar.exists()
    finally:
        valid_sidecar.unlink(missing_ok=True)
        malformed_sidecar.unlink(missing_ok=True)


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
