"""新解释器及原回复入口均必须完整注册 QQ 账本表。"""
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("entry", ["infra.db.tenant_security", "tests.integration.test_reply_completion"])
def test_fresh_process_registers_qq_before_readiness(entry):
    root = Path(__file__).resolve().parents[2]
    script = f"""
import importlib
importlib.import_module({entry!r})
from infra.db.tenant_security import _tenant_table_names
before = _tenant_table_names()
assert {{'qq_sent_aliases', 'qq_reply_trials', 'qq_reply_trial_events'}} <= before
from infra.db.qq_trial_tables import QQSentAliasRow, QQTrialRow, QQTrialEventRow
from infra.db.tables import Base
assert before == _tenant_table_names() == set(Base.metadata.tables)
assert all(row.__table__.metadata is Base.metadata for row in (QQSentAliasRow, QQTrialRow, QQTrialEventRow))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)}, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, "新解释器的 QQ 表注册不完整"
