"""私有文件仅在对应应用停止确认后清理，不跨 owner 扫描。"""

from pathlib import Path

import pytest

from infra.controlled.config import ControlledError
from scripts.controlled_web_supervisor import Supervisor


@pytest.mark.parametrize("stop_fails", [False, True])
def test_reply_model_files_removed_only_after_owner_stops(tmp_path, stop_fails):
    supervisor = Supervisor(Path.cwd(), tmp_path, [])
    names = ["config.json"] + [
        stem + suffix
        for stem in ("mail.sqlite", "reply-model.sqlite")
        for suffix in ("", "-journal", "-wal", "-shm")
    ]
    for name in names:
        (supervisor.directory / name).touch()
    untouched = supervisor.directory / "other-owned.sqlite"
    untouched.touch()
    foreign = tmp_path / "reply-model.sqlite"
    foreign.touch()
    stopped = []

    class Process:
        def stop(self):
            assert all((supervisor.directory / name).exists() for name in names)
            stopped.append(True)
            if stop_fails:
                raise ControlledError("process_cleanup_unknown")

    supervisor.processes = [Process()]
    supervisor.close()
    assert stopped == [True]
    assert all((supervisor.directory / name).exists() == stop_fails for name in names)
    assert untouched.exists() and foreign.exists()
    assert supervisor.cleanup_errors == (
        ["process_cleanup_unknown"] if stop_fails else []
    )
