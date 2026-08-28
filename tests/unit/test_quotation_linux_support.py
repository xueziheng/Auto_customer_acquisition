"""构建白名单契约；不替代真实受限Linux工厂链。"""

import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.integration import quote_evidence_linux_support as support


@pytest.mark.parametrize("quotation", [True, False])
def test_runtime_image_uses_explicit_b2_whitelist_without_changing_a_chain(
    monkeypatch, quotation
):
    seen = {}

    def build(**kwargs):
        with tarfile.open(fileobj=kwargs["fileobj"]) as archive:
            seen["names"] = archive.getnames()
        seen["target"] = kwargs["target"]
        return SimpleNamespace(id="sha256:" + "a" * 64), []

    client = SimpleNamespace(
        images=SimpleNamespace(get=lambda _: object(), build=build), close=lambda: None
    )
    monkeypatch.setattr(support.docker, "from_env", lambda: client)
    kwargs = {"chain": True, "quotation": True} if quotation else {"chain": True}
    support.build_parser_image("sha256:" + "b" * 64, **kwargs)
    assert seen["target"] == ("quotation" if quotation else "chain")
    assert ("apps/api/composition/runtime.py" in seen["names"]) is quotation
    assert ("apps/scheduler_worker/runtime.py" in seen["names"]) is quotation
    for name in ("__init__.py", "quotations.py"):
        assert (f"apps/composition_support/{name}" in seen["names"]) is quotation
    assert ("connectors/object_store/quote_pdf.py" in seen["names"]) is quotation
    assert (
        "tests/integration/quotation_runtime_linux_cases.py" in seen["names"]
    ) is quotation
    assert not any(
        ".env" in name or ".git" in name or name.startswith(("apps/web/", "tmp/"))
        for name in seen["names"]
    )
    assert "tests/integration/quote_source_readers_linux_cases.py" in seen["names"]
    assert "tests/integration/test_quote_source_readers.py" not in seen["names"]


def test_a_chain_sets_its_fixed_entry_even_with_a_t10_image(monkeypatch):
    """显式image可能有T10默认CMD；A宿主必须实际执行自己的固定入口。"""
    network = SimpleNamespace(id="test-network", name="test-network", remove=Mock())
    client = Mock()
    client.networks.get.return_value.attrs = {"Internal": True}
    monkeypatch.setattr(support.docker, "from_env", lambda: client)
    monkeypatch.setattr(support, "image_id", lambda: "sha256:" + "a" * 64)
    monkeypatch.setattr(support, "Network", lambda **_: SimpleNamespace(create=lambda: network))
    pg, runner = Mock(), Mock()
    for container in (pg, runner):
        container.with_network.return_value = container
        container.with_network_aliases.return_value = container
        container.with_tmpfs_mount.return_value = container
        container.with_env.return_value = container
        container.with_command.return_value = container
    pg.get_wrapped_container.return_value.exec_run.return_value.exit_code = 0
    pg.get_wrapped_container.return_value.attrs = {"HostConfig": {"Memory": 536870912}}
    wrapped = runner.get_wrapped_container.return_value
    wrapped.attrs = {
        "HostConfig": {
            "Memory": 1073741824, "MemorySwap": 1073741824, "PidsLimit": 128,
            "NanoCpus": 2000000000, "ReadonlyRootfs": True, "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"], "Binds": [], "PortBindings": {},
            "Tmpfs": {"/tmp": "rw,size=67108864,noexec,nosuid"},
        },
        "Config": {"User": "65534:65534"},
        "NetworkSettings": {"Networks": {network.name: {}}},
    }
    wrapped.wait.return_value = {"StatusCode": 0}
    wrapped.logs.return_value = b"controlled-runner-output"
    monkeypatch.setattr(support, "DockerContainer", lambda image, **_: pg if image == "pgvector/pgvector:pg16" else runner)

    assert support.run_chain_cases() == (0, "controlled-runner-output")
    runner.with_command.assert_called_once_with([
        "python", "-m", "tests.integration.quote_evidence_linux_support",
    ])
    wrapped.wait.assert_called_once_with(timeout=240)
    runner.stop.assert_called_once()
    pg.stop.assert_called_once()
    network.remove.assert_called_once()


def test_a_chain_subpytest_keeps_three_linux_cases_and_fixed_budget(monkeypatch, capsys):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=(
            b"PASSED tests/integration/quote_source_readers_linux_cases.py::test_actual_parser_prices_and_unit_receipt_history\n"
            b"PASSED tests/integration/quote_source_readers_linux_cases.py::test_clipped_source_is_rejected_by_real_linux_need_confirmation[private-body-first]\n"
            b"PASSED tests/integration/quote_source_readers_linux_cases.py::test_clipped_source_is_rejected_by_real_linux_need_confirmation[private-body-second]\n"
            b"PASSED tests/integration/quote_source_readers_linux_cases.py::test_unknown_private[private-body]\n"
            b"PASSED other.py::test_actual_parser_prices_and_unit_receipt_history[private-body]\n"
            b"3 passed in 1.00s\n"
        ))

    monkeypatch.setenv("TEST_DATABASE_URL", "controlled-test-input")
    monkeypatch.setattr(support.subprocess, "run", run)
    assert support._chain_main() == 0
    assert len(calls) == 2
    command, kwargs = calls[1]
    assert command[5:9] == [
        "tests/unit/test_quote_source_readers.py",
        "tests/integration/quote_source_readers_linux_cases.py",
        "tests/unit/test_need_units.py", "tests/integration/test_need_units.py",
    ]
    assert kwargs["timeout"] == 180
    assert "-rA" in command
    assert capsys.readouterr().out == (
        "source_linux_case_passed=prices_and_unit_history\n"
        "source_linux_case_passed=clipped_source_rejected\n"
        "source_linux_case_passed=clipped_source_rejected\n"
        "3 passed in 1.00s\n"
    )


@pytest.mark.parametrize(
    "kwargs", [{"quotation": True}, {"chain": True, "quotation": True}]
)
def test_quotation_image_requires_chain_and_explicit_accepted_base(monkeypatch, kwargs):
    def forbidden():
        pytest.fail("无效构建请求不得访问Docker")

    monkeypatch.setattr(support.docker, "from_env", forbidden)
    with pytest.raises(ValueError):
        support.build_parser_image(**kwargs)


def test_original_stages_keep_fixed_a_entrypoints_and_b2_has_separate_entry():
    dockerfile = (
        Path(__file__).parents[1] / "fixtures/quote_evidence/linux/Dockerfile"
    ).read_text()
    stages = {}
    for block in dockerfile.split("\nFROM ")[1:]:
        header, body = block.split("\n", 1)
        stages[header.split(" AS ")[1]] = body
    assert (
        'CMD ["python", "-m", "tests.integration.quote_evidence_linux_support"]'
        in stages["chain"]
    )
    assert (
        'CMD ["python", "-m", "tests.integration.quotation_runtime_linux_support"]'
        in stages["quotation"]
    )
    for stage in ("parser", "refreshed"):
        assert (
            '"tests.integration.quotation_runtime_linux_support"' not in stages[stage]
        )
        assert "/tests/integration/evidence_parser_linux_cases.py" in stages[stage]


def test_t10_target_adds_only_named_helpers(monkeypatch):
    seen = {}

    def build(**kwargs):
        with tarfile.open(fileobj=kwargs["fileobj"]) as archive:
            seen["names"] = archive.getnames()
        seen["target"] = kwargs["target"]
        return SimpleNamespace(id="sha256:" + "a" * 64), []

    client = SimpleNamespace(images=SimpleNamespace(get=lambda _: object(), build=build), close=lambda: None)
    monkeypatch.setattr(support.docker, "from_env", lambda: client)
    support.build_parser_image("sha256:" + "b" * 64, chain=True, quotation=True, costing_quote=True)
    assert seen["target"] == "costing_quote"
    assert {"tests/integration/costing_quote_case.py", "tests/integration/test_costing_quote_closed_loop.py",
            "tests/e2e/costing_quote_server.py", "tests/e2e/costing_quote_relay.py"}.issubset(seen["names"])
    assert "tests/e2e/costing_quote_stack.py" not in seen["names"]
    assert "tests/e2e/costing_quote_bridge.py" not in seen["names"]
    assert not any(".env" in name or name.startswith(("output/", "tmp/", "apps/web/")) for name in seen["names"])
    dockerfile = (Path(__file__).parents[1] / "fixtures/quote_evidence/linux/Dockerfile").read_text()
    assert 'FROM quotation AS costing_quote\nCMD ["python", "-m", "tests.e2e.costing_quote_server", "--mode", "integration"]' in dockerfile


@pytest.mark.parametrize("kwargs", [{}, {"chain": True}, {"quotation": True}])
def test_t10_requires_actual_chain_and_quotation(monkeypatch, kwargs):
    monkeypatch.setattr(support.docker, "from_env", lambda: pytest.fail("无效T10构建不得访问Docker"))
    with pytest.raises(ValueError):
        support.build_parser_image("sha256:" + "b" * 64, costing_quote=True, **kwargs)


def test_t10_build_failure_diagnostics_are_fixed_and_b2_unchanged(monkeypatch, capsys):
    def fail(**_kwargs):
        raise ValueError("private build payload never printed")

    client = SimpleNamespace(images=SimpleNamespace(get=lambda _: object(), build=fail), close=lambda: None)
    monkeypatch.setattr(support.docker, "from_env", lambda: client)
    for costing_quote in (False, True):
        with pytest.raises(RuntimeError, match="来源测试镜像构建失败"):
            support.build_parser_image("sha256:" + "b" * 64, chain=True, quotation=True, costing_quote=costing_quote)
        assert capsys.readouterr().out == (
            "t10_build_failure_phase=docker_build\nt10_build_failure_type=unknown\n" if costing_quote else ""
        )
