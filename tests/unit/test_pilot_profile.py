"""持久化内测配置、锁和备份拒绝边界。"""

import importlib.util
import io
import json
import os
import secrets
import tarfile
from pathlib import Path

import pytest


def synthetic_policy(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "handoff_policy": {
                    "sla_seconds": 7200,
                    "backlog_threshold": 7,
                    "t1_seconds": 40,
                    "t2_seconds": 90,
                },
                "scoring_policy": {
                    "version": "synthetic-pilot",
                    "currency": "USD",
                    "value_band_boundaries": ["250.00"],
                    "bucket_map": {str(k): "low" for k in range(1, 8)},
                },
            }
        )
    )
    return path


def test_profile_contract_exists():
    assert importlib.util.find_spec("infra.pilot") is not None, "PILOT_PROFILE_MISSING"


def make_config(tmp_path):
    from infra.pilot.config import PilotConfig

    return PilotConfig.create(
        tmp_path / "profile", synthetic_policy(tmp_path / "policy.json")
    )


def test_config_private_roundtrip_policy_and_secrets(tmp_path):
    from infra.pilot.config import PilotConfig, PilotError

    config = make_config(tmp_path)
    reread = PilotConfig.read(tmp_path / "profile/config.json")
    hidden = all(
        s.get_secret_value() not in repr(reread) for s in reread.secrets.values()
    )
    assert hidden, "PRIVATE_REPR_EXPOSED"
    assert config.tenant_id == reread.tenant_id
    assert reread.web_port == reread.api_port
    assert (tmp_path / "profile").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "profile/config.json").stat().st_mode & 0o777 == 0o600
    env = reread.runtime_environment()
    assert json.loads(env["TRADEOS_HANDOFF_POLICY"])["sla_seconds"] == 7200
    assert env["TRADEOS_DEV_MODE"] == "false"
    assert "GMAIL_OAUTH_TOKEN_REF" not in env
    with pytest.raises(PilotError, match="secret_reference_rejected"):
        reread.resolve("UNCONFIGURED_PROVIDER")


@pytest.mark.parametrize(
    "target,mode", [("profile", 0o755), ("profile/config.json", 0o644)]
)
def test_config_refuses_insecure_permissions(tmp_path, target, mode):
    from infra.pilot.config import PilotConfig, PilotError

    make_config(tmp_path)
    (tmp_path / target).chmod(mode)
    with pytest.raises(PilotError, match="configuration_invalid"):
        PilotConfig.read(tmp_path / "profile/config.json")


def test_config_refuses_symlink_hardlink_and_wrong_owner(tmp_path, monkeypatch):
    from infra.pilot.config import PilotConfig, PilotError

    make_config(tmp_path)
    original = tmp_path / "profile/config.json"
    moved = tmp_path / "profile/private.json"
    original.rename(moved)
    original.symlink_to(moved)
    with pytest.raises(PilotError, match="configuration_invalid"):
        PilotConfig.read(original)
    original.unlink()
    os.link(moved, original)
    with pytest.raises(PilotError, match="configuration_invalid"):
        PilotConfig.read(original)
    moved.unlink()
    monkeypatch.setattr(os, "getuid", lambda: original.stat().st_uid + 1)
    with pytest.raises(PilotError, match="configuration_invalid"):
        PilotConfig.read(original)


def test_policy_missing_extra_and_implicit_rejected_before_profile_creation(tmp_path):
    from infra.pilot.config import PilotConfig, PilotError

    for payload in (
        {},
        {"handoff_policy": {}},
        {"DATABASE_URL": secrets.token_urlsafe(32)},
    ):
        policy = tmp_path / "policy.json"
        policy.write_text(json.dumps(payload))
        with pytest.raises(PilotError, match="policy_invalid"):
            PilotConfig.create(tmp_path / "profile", policy)
        assert not (tmp_path / "profile").exists()


def test_profile_lock_excludes_concurrent_operation(tmp_path):
    from infra.pilot.config import PilotError, exclusive_profile_lock

    make_config(tmp_path)
    with (
        exclusive_profile_lock(tmp_path / "profile"),
        pytest.raises(PilotError, match="profile_busy"),
        exclusive_profile_lock(tmp_path / "profile"),
    ):
        pytest.fail("LOCK_NOT_EXCLUSIVE")


def test_port_reservation_reuses_stable_port_and_rejects_occupied(tmp_path):
    from infra.pilot.config import PilotError
    from infra.pilot.resources import reserve_port

    config = make_config(tmp_path)
    with reserve_port(config.api_port) as listener:
        assert listener.getsockname()[1] == config.api_port
        with pytest.raises(PilotError, match="port_unavailable"):
            reserve_port(config.api_port)


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", "file"),
        ("/outside", "file"),
        ("data/../../escape", "file"),
        ("data/link", "symlink"),
        ("data/hard", "hardlink"),
        ("data/device", "device"),
        ("wrong/file", "file"),
    ],
)
def test_archive_rejects_unsafe_members(tmp_path, name, kind):
    from infra.pilot.backup import validate_archive
    from infra.pilot.config import PilotError

    archive = tmp_path / "object.tar"
    with tarfile.open(archive, "w") as stream:
        entry = tarfile.TarInfo(name)
        if kind == "symlink":
            entry.type, entry.linkname = tarfile.SYMTYPE, "../../escape"
        elif kind == "hardlink":
            entry.type, entry.linkname = tarfile.LNKTYPE, "data/target"
        elif kind == "device":
            entry.type = tarfile.CHRTYPE
        stream.addfile(entry, io.BytesIO())
    with pytest.raises(PilotError, match="backup_invalid"):
        validate_archive(archive, "data")


def test_process_birth_mismatch_never_treated_as_stopped():
    from infra.pilot.config import PilotError
    from infra.pilot.resources import ProcessIdentity

    current = ProcessIdentity.current()
    assert current.live()
    stale = current.model_copy(update={"born": current.born - 10})
    with pytest.raises(PilotError, match="process_identity_invalid"):
        stale.live()


def test_atomic_publish_refuses_even_empty_existing_directory(tmp_path):
    from infra.pilot.backup import _rename_new
    from infra.pilot.config import PilotError

    source, target = tmp_path / "pending", tmp_path / "existing"
    source.mkdir()
    target.mkdir()
    (source / "marker").write_text("synthetic")
    with pytest.raises(PilotError, match="backup_publish_failed"):
        _rename_new(source, target)
    assert list(target.iterdir()) == []
    assert (source / "marker").exists()


def test_cli_start_does_not_claim_application_ready(tmp_path, capsys):
    from scripts.run_web_pilot import main

    result = main(["start", "--profile", str(tmp_path / "not-created")])
    assert result == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "reason": "application_launch_not_configured",
    }
    assert not (tmp_path / "not-created").exists()


def test_cli_missing_policy_is_safe_no_profile(tmp_path, capsys):
    from scripts.run_web_pilot import main

    result = main(
        [
            "init",
            "--profile",
            str(tmp_path / "profile"),
            "--policy-file",
            str(tmp_path / "missing"),
        ]
    )
    assert result == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "policy_invalid"
    assert not (tmp_path / "profile").exists()


def test_policy_float_money_and_incomplete_ranks_rejected(tmp_path):
    from infra.pilot.config import PilotConfig, PilotError

    policy = synthetic_policy(tmp_path / "policy.json")
    payload = json.loads(policy.read_bytes())
    payload["scoring_policy"]["value_band_boundaries"] = [250.0]
    policy.write_text(json.dumps(payload))
    with pytest.raises(PilotError, match="policy_invalid"):
        PilotConfig.create(tmp_path / "profile", policy)
    payload["scoring_policy"]["value_band_boundaries"] = ["250.00"]
    payload["scoring_policy"]["bucket_map"].pop("7")
    policy.write_text(json.dumps(payload))
    with pytest.raises(PilotError, match="policy_invalid"):
        PilotConfig.create(tmp_path / "profile", policy)


def test_private_read_missing_file_uses_fixed_error(tmp_path):
    from infra.pilot.config import PilotError, private_read

    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    with pytest.raises(PilotError, match="configuration_invalid"):
        private_read(directory / "missing")


def test_direct_cli_works_outside_checkout_without_pythonpath(tmp_path):
    import subprocess
    import sys

    from infra.pilot.resources import ROOT

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/run_web_pilot.py"), "--help"],
        cwd=tmp_path,
        env={"PATH": os.defpath, "PYTHON_DOTENV_DISABLED": "1"},
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, "DIRECT_CLI_IMPORT_FAILED"


def test_process_from_previous_boot_is_dead_even_if_numeric_pid_reused():
    import psutil

    from infra.pilot.resources import ProcessIdentity

    current = ProcessIdentity.current()
    old = current.model_copy(update={"born": psutil.boot_time() - 1})
    assert not old.live()


def test_cli_restore_interruption_before_resource_creation_is_fixed_failure(
    tmp_path, monkeypatch, capsys
):
    from scripts import run_web_pilot

    def interrupted(backup, profile):
        raise KeyboardInterrupt

    monkeypatch.setattr(run_web_pilot, "restore_profile", interrupted)
    try:
        result = run_web_pilot.main(
            [
                "restore",
                "--backup",
                str(tmp_path / "backup"),
                "--profile",
                str(tmp_path / "new-profile"),
            ]
        )
    except KeyboardInterrupt:
        result = None
    assert result == 2, "CLI_INTERRUPTION_ESCAPED"
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "reason": "pilot_interrupted",
    }
    assert not (tmp_path / "new-profile").exists()
