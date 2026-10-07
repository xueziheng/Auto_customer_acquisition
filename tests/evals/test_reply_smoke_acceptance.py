"""三次烟测只证明合成业务链路；前置门禁和未知结果不得被误报为通过。"""

import asyncio
import importlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.evals.test_reply_live_settings import configured


def module():
    return importlib.import_module("tests.evals.reply_smoke_acceptance")


def test_disabled_cli_does_not_read_settings_or_create_report(tmp_path, capsys):
    report = tmp_path / "absent" / "report.json"
    assert module().main(["--settings-file", "missing", "--report", str(report)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "not_run"
    assert not report.parent.exists()


@pytest.mark.parametrize("case", ["relative", "budget", "quota", "export", "secret"])
def test_preflight_rejects_before_resources_and_never_discloses_input(tmp_path, case):
    data, path, environ = configured(tmp_path)
    maximum = 3
    if case == "relative":
        path = path.relative_to(path.parent)
    elif case == "budget":
        maximum = 163
    elif case == "quota":
        data["limits"]["employee_calls"] = 2
    elif case == "export":
        data["model_data_export_enabled"] = False
    else:
        environ.clear()
    if path.is_absolute():
        path.write_text(json.dumps(data))
    with pytest.raises(ValueError) as error:
        module().load_smoke_settings(path, maximum, environ)
    assert "synthetic-unit-only" not in str(error.value)
    assert "DEEPSEEK_API_KEY" not in str(error.value)


def test_smoke_clamps_only_in_memory_and_requires_new_report(tmp_path):
    _, path, environ = configured(tmp_path)
    original = path.read_bytes()
    loaded = module().load_smoke_settings(path, 3, environ)
    assert loaded.settings.limits.tenant_calls == 3
    assert loaded.settings.limits.employee_calls == 3
    assert loaded.settings.limits.tenant_concurrency == 1
    assert path.read_bytes() == original
    assert "synthetic-unit-only" not in repr(loaded)
    assert "DEEPSEEK_API_KEY" not in repr(loaded)
    report_path = tmp_path / "report.json"
    first = module().claim_report(report_path)
    with pytest.raises(ValueError):
        module().claim_report(report_path)
    assert json.loads(report_path.read_text()) == first


@pytest.mark.parametrize(
    "outcome", ["success", "unknown", "exception", "cancelled", "ledger_unavailable"]
)
async def test_stage_receipts_stop_at_unknown_and_do_not_claim_corpus(
    tmp_path, monkeypatch, outcome
):
    smoke = module()
    _, path, environ = configured(tmp_path)
    config = smoke.load_smoke_settings(path, 3, environ)
    chain = {
        "model": config.settings,
        "model_provider": None,
        "route": SimpleNamespace(tenant_id="tn_synthetic"),
    }
    records = []

    async def locked(_chain, exercise):
        await exercise(object())
        if outcome == "cancelled":
            raise asyncio.CancelledError

    async def prepare(_chain, _worker):
        return None

    async def probe(_chain, _worker):
        records.append({"invocation_id": "probe", "state": "succeeded"})

    async def positive(_chain, _worker):
        records.append(
            {
                "invocation_id": "positive",
                "state": "unknown"
                if outcome in {"unknown", "exception"}
                else "succeeded",
            }
        )
        if outcome == "exception":
            raise RuntimeError("synthetic-unit-only DEEPSEEK_API_KEY")

    async def unsubscribe(_chain, _worker):
        records.append({"invocation_id": "unsubscribe", "state": "succeeded"})

    async def ledger(_chain):
        if outcome == "ledger_unavailable" and len(records) == 2:
            raise OSError("synthetic private ledger failure")
        return {"calls": len(records), "records": [dict(row) for row in records]}

    for name, value in (
        ("locked", locked),
        ("configure_playbook", prepare),
        ("probe", probe),
        ("positive_chain", positive),
        ("unsubscribe_chain", unsubscribe),
        ("invocation_ledger", ledger),
    ):
        monkeypatch.setattr(smoke, name, value)
    report_path = tmp_path / "smoke.json"
    report = smoke.claim_report(report_path)
    if outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await smoke.run_smoke(chain, report, report_path)
    else:
        await smoke.run_smoke(chain, report, report_path)
    saved = json.loads(report_path.read_text())
    failed = outcome != "success"
    assert saved["corpus"] == "not_run"
    assert saved["smoke_complete"] is not failed
    if outcome == "ledger_unavailable":
        assert saved["ledger"]["calls"] == 1
        assert saved["error"] == "ledger_unavailable"
        assert saved["steps"]["unsubscribe"] == "not_run"
        assert len(saved["checkpoints"]) == 1
        return
    interrupted_reply = outcome in {"unknown", "exception"}
    assert saved["ledger"]["calls"] == (2 if interrupted_reply else 3)
    assert saved["steps"]["unsubscribe"] == (
        "not_run" if interrupted_reply else "passed"
    )
    assert len(saved["checkpoints"]) == (2 if interrupted_reply else 3)
    if interrupted_reply:
        assert saved["ledger"]["records"][-1]["state"] == "unknown"
    assert "synthetic-unit-only" not in report_path.read_text()
    assert "DEEPSEEK_API_KEY" not in report_path.read_text()


@pytest.mark.parametrize(
    "outcome", ["success", "ledger_unavailable", "cancelled", "write_failed"]
)
async def test_owned_storage_is_retained_until_evidence_is_durable(
    tmp_path, monkeypatch, outcome
):
    smoke = module()
    _, path, environ = configured(tmp_path)
    config = smoke.load_smoke_settings(path, 3, environ)
    report_path = tmp_path / "owned.json"
    report = smoke.claim_report(report_path)
    created = []

    def profiles(directory):
        owned = []
        yield directory, owned
        # 模拟原 fixture 清理真实卷的不可逆效果；失败时绝不应到这里。
        for profile in owned:
            profile.volume.unlink()

    def initialize(directory, profiles):
        # 真实 migrate/check_schema_locked 使用 asyncio.run；替身保留此边界。
        async def migrated():
            return object()

        pending = migrated()
        try:
            storage_config = asyncio.run(pending)
        finally:
            pending.close()
        volume = directory / "only-ledger-evidence"
        volume.write_text("durable synthetic invocation record")
        profile_file = directory / "owner-profile"
        profile_file.write_text("synthetic owner")

        def stop():
            (directory / "stopped").write_text("stopped")

        profile = SimpleNamespace(
            config=storage_config,
            volume=volume,
            stop=stop,
            client=SimpleNamespace(close=lambda: None),
        )
        profiles.append(profile)
        created.append(directory)
        return profile

    @asynccontextmanager
    async def chain(*args, **kwargs):
        yield {}

    async def execute(_chain, receipt, target):
        receipt["smoke_complete"] = outcome != "ledger_unavailable"
        receipt["ledger"] = {"calls": 3, "records": [{"state": "succeeded"}] * 3}
        if outcome == "ledger_unavailable":
            receipt["error"] = "ledger_unavailable"
        if outcome == "cancelled":
            raise asyncio.CancelledError
        if outcome == "write_failed":

            def cannot_write(_path, _report):
                raise OSError("synthetic private failure")

            monkeypatch.setattr(smoke, "save_report", cannot_write)
        else:
            smoke.save_report(target, receipt)

    monkeypatch.setattr(smoke, "owned_profiles", SimpleNamespace(__wrapped__=profiles))
    monkeypatch.setattr(smoke, "initialized", initialize)
    monkeypatch.setattr(smoke, "standalone_reply_chain", chain)
    monkeypatch.setattr(smoke, "run_smoke", execute)
    if outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await smoke.run_owned(config, report, report_path)
    elif outcome == "write_failed":
        with pytest.raises(OSError):
            await smoke.run_owned(config, report, report_path)
    else:
        await smoke.run_owned(config, report, report_path)
    directory = created[0]
    if outcome == "success":
        assert not directory.exists()
        assert report["cleanup"] == "passed"
    else:
        assert (
            directory / "only-ledger-evidence"
        ).read_text() == "durable synthetic invocation record"
        assert (directory / "owner-profile").exists()
        assert (directory / "stopped").exists()
        assert Path(report["evidence_directory"]) == directory
        assert report["cleanup"] == "retained_for_review"
        assert report["smoke_complete"] is False


def test_concurrent_report_claims_cannot_both_start(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "concurrent.json"

    def claim():
        try:
            module().claim_report(path)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: claim(), range(2)))
    assert outcomes.count(True) == 1
    assert json.loads(path.read_text())["smoke_complete"] is False


def test_cli_cancellation_returns_failure_without_exception_text(
    tmp_path, monkeypatch, capsys
):
    smoke = module()
    _, path, environ = configured(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", environ["DEEPSEEK_API_KEY"])

    async def cancelled(*args):
        raise asyncio.CancelledError("synthetic private cancellation detail")

    monkeypatch.setattr(smoke, "run_owned", cancelled)
    report_path = tmp_path / "cancelled.json"
    result = smoke.main(
        [
            "--live",
            "--settings-file",
            str(path),
            "--max-calls",
            "3",
            "--report",
            str(report_path),
        ]
    )
    assert result == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert json.loads(report_path.read_text())["smoke_complete"] is False
    assert "synthetic private cancellation detail" not in report_path.read_text()


async def test_cancellation_waits_for_storage_initializer_before_retaining_resources(
    tmp_path, monkeypatch
):
    smoke = module()
    _, path, environ = configured(tmp_path)
    config = smoke.load_smoke_settings(path, 3, environ)
    report_path = tmp_path / "initialization-cancelled.json"
    report = smoke.claim_report(report_path)
    started, release = threading.Event(), threading.Event()

    def profiles(directory):
        owned = []
        yield directory, owned
        raise AssertionError("中断的初始化不得销毁证据")

    def initialize(directory, profiles):
        started.set()
        if not release.wait(timeout=5):
            raise TimeoutError("受控初始化未获释放")
        (directory / "only-ledger-evidence").write_text("synthetic owner")

        def stop():
            (directory / "stopped").touch()

        profile = SimpleNamespace(
            config=object(), stop=stop, client=SimpleNamespace(close=lambda: None)
        )
        profiles.append(profile)
        return profile

    monkeypatch.setattr(smoke, "owned_profiles", SimpleNamespace(__wrapped__=profiles))
    monkeypatch.setattr(smoke, "initialized", initialize)
    task = asyncio.create_task(smoke.run_owned(config, report, report_path))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        asyncio.get_running_loop().call_later(0.05, release.set)
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    directory = Path(report["evidence_directory"])
    assert (directory / "only-ledger-evidence").exists()
    assert (directory / "stopped").exists()
    assert report["cleanup"] == "retained_for_review"
    assert report["smoke_complete"] is False


@pytest.mark.skipif(
    os.environ.get("TRADEOS_SMOKE_STORAGE_TEST") != "1",
    reason="仅显式开启时运行真实 owned PG/MinIO；不调用模型",
)
async def test_real_owned_storage_enters_async_reply_chain_without_model_calls(
    tmp_path, monkeypatch
):
    smoke = module()
    _, path, environ = configured(tmp_path)
    config = smoke.load_smoke_settings(path, 3, environ)
    report_path = tmp_path / "storage-boundary.json"
    report = smoke.claim_report(report_path)

    async def inspect_storage(chain, receipt, target):
        # 只替代付费阶段，真实初始化/迁移/API lifespan/controlled Gmail 均保留。
        ledger = await smoke.invocation_ledger(chain)
        assert ledger == {"calls": 0, "records": []}
        receipt.update(
            scope="owned_storage_boundary_test",
            real_model=False,
            ledger=ledger,
            smoke_complete=True,
        )
        smoke.save_report(target, receipt)

    monkeypatch.setattr(smoke, "run_smoke", inspect_storage)
    await smoke.run_owned(config, report, report_path)
    saved = json.loads(report_path.read_text())
    assert saved["scope"] == "owned_storage_boundary_test"
    assert saved["real_model"] is False
    assert saved["ledger"]["calls"] == 0
    assert saved["cleanup"] == "passed"
    assert not Path(saved["evidence_directory"]).exists()
