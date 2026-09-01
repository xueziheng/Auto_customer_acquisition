"""构建白名单契约；不替代真实受限Linux工厂链。"""

import tarfile
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from tests.integration import quote_evidence_linux_support as support


def test_dependency_manifest_is_sorted_and_newline_terminated(monkeypatch):
    """依赖清单 hash 采用可复现的 Unix 文本形式，不能依赖 Docker 输出顺序。"""

    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return b"z==1\na==1\n"

    container_client = SimpleNamespace(
        containers=SimpleNamespace(run=run)
    )

    assert support._dependency_manifest_sha256(
        container_client, "sha256:" + "a" * 64
    ) == sha256(b"a==1\nz==1\n").hexdigest()
    assert calls[0][0][1] == ["-m", "pip", "freeze", "--all"]
    assert calls[0][1]["entrypoint"] == ["python"]


def test_image_id_rebuilds_current_source_from_an_offline_dependency_image(
    monkeypatch,
):
    """完整门不用作者机 image ID，且验收前校验 source manifest 与平台。"""

    dependency = "sha256:" + "d" * 64
    source = "sha256:" + "e" * 64
    manifest = "c" * 64
    calls = []
    image = SimpleNamespace(
        attrs={
            "Architecture": "arm64",
            "Os": "linux",
            "RootFS": {"Layers": ["sha256:python-base", "sha256:dependencies"]},
            "Config": {
                "Labels": {
                    "org.tradeos.source-manifest-sha256": manifest,
                    "org.tradeos.source-platform": "linux/arm64",
                }
            },
        }
    )
    client = SimpleNamespace(images=SimpleNamespace(get=lambda value: image), close=Mock())
    monkeypatch.setattr(support.docker, "from_env", lambda: client)
    monkeypatch.setattr(
        support,
        "build_parser_image",
        lambda *args, **kwargs: calls.append((args, kwargs)) or source,
    )
    monkeypatch.setattr(support, "audited_dependency_image_id", lambda: dependency)
    monkeypatch.setattr(
        support, "source_manifest_sha256", lambda **_kwargs: manifest
    )

    assert support.image_id() == source
    assert calls == [((dependency,), {"chain": True})]
    client.images.get(source)
    client.close.assert_called_once()


def test_audited_dependency_image_validates_only_the_explicit_bootstrap_artifact(
    monkeypatch,
):
    """不能扫描作者机镜像；只接受本次固定配方刚构建出的 dependency-only 产物。"""

    dependency = "sha256:" + "a" * 64
    image = SimpleNamespace(
        id=dependency,
        attrs={
            "Architecture": "arm64",
            "Os": "linux",
            "RootFS": {"Layers": ["sha256:python-base", "sha256:dependencies"]},
            "Config": {
                "Labels": {
                    "org.tradeos.dependency-only": "true",
                    "org.tradeos.dependency-base": support.PYTHON_IMAGE,
                    "org.tradeos.dependency-lock-sha256": "lock-sha",
                    "org.tradeos.dependency-manifest-sha256": (
                        support._AUDITED_DEPENDENCY_MANIFEST_SHA256
                    ),
                    "org.tradeos.dependency-platform": "linux/arm64",
                }
            },
        }
    )
    pinned_python = SimpleNamespace(
        attrs={"RootFS": {"Layers": ["sha256:python-base"]}}
    )
    images = SimpleNamespace(
        get=Mock(
            side_effect=lambda value: image
            if value == "fixed-tag"
            else pinned_python
        )
    )
    client = SimpleNamespace(images=images, containers=SimpleNamespace(), close=Mock())
    monkeypatch.setattr(support.docker, "from_env", lambda: client)
    monkeypatch.setattr(support, "dependency_lock_sha256", lambda: "lock-sha")
    monkeypatch.setattr(support, "dependency_bootstrap_image_tag", lambda: "fixed-tag")
    monkeypatch.setattr(
        support,
        "_dependency_manifest_sha256",
        lambda _client, image_id: support._AUDITED_DEPENDENCY_MANIFEST_SHA256,
    )
    monkeypatch.setattr(support, "_dependency_runtime_is_complete", lambda *_: True)
    monkeypatch.setattr(support, "_dependency_image_is_source_free", lambda *_: True)

    assert support.audited_dependency_image_id() == dependency
    assert images.get.call_args_list == [
        call("fixed-tag"),
        call(support.PYTHON_IMAGE),
    ]
    client.close.assert_called_once()


def test_audited_dependency_image_requires_the_pinned_python_rootfs_prefix(
    monkeypatch,
):
    """候选仅能由本地 pinned Python 基础层增量构建，不能借相同标签冒充。"""

    dependency = "sha256:" + "a" * 64
    image = SimpleNamespace(
        id=dependency,
        attrs={
            "Architecture": "arm64",
            "Os": "linux",
            "RootFS": {"Layers": ["sha256:python-base", "sha256:dependencies"]},
            "Config": {
                "Labels": {
                    "org.tradeos.dependency-only": "true",
                    "org.tradeos.dependency-base": support.PYTHON_IMAGE,
                    "org.tradeos.dependency-lock-sha256": "lock-sha",
                    "org.tradeos.dependency-manifest-sha256": (
                        support._AUDITED_DEPENDENCY_MANIFEST_SHA256
                    ),
                    "org.tradeos.dependency-platform": "linux/arm64",
                }
            },
        },
    )
    pinned_python = SimpleNamespace(
        attrs={"RootFS": {"Layers": ["sha256:python-base"]}}
    )
    images = SimpleNamespace(
        get=Mock(
            side_effect=lambda value: image
            if value == "fixed-tag"
            else pinned_python
        )
    )
    client = SimpleNamespace(images=images, containers=SimpleNamespace(), close=Mock())
    monkeypatch.setattr(support.docker, "from_env", lambda: client)
    monkeypatch.setattr(support, "dependency_lock_sha256", lambda: "lock-sha")
    monkeypatch.setattr(support, "dependency_bootstrap_image_tag", lambda: "fixed-tag")
    monkeypatch.setattr(
        support,
        "_dependency_manifest_sha256",
        lambda _client, image_id: support._AUDITED_DEPENDENCY_MANIFEST_SHA256,
    )
    monkeypatch.setattr(support, "_dependency_runtime_is_complete", lambda *_: True)
    monkeypatch.setattr(support, "_dependency_image_is_source_free", lambda *_: True)

    assert support.audited_dependency_image_id() == dependency
    assert images.get.call_args_list == [
        call("fixed-tag"),
        call(support.PYTHON_IMAGE),
    ]


def test_audited_dependency_image_rejects_non_pinned_python_rootfs_prefix(
    monkeypatch,
):
    """标签正确但基底层不匹配的本机候选必须 fail-closed。"""

    dependency = "sha256:" + "a" * 64
    image = SimpleNamespace(
        id=dependency,
        attrs={
            "Architecture": "arm64",
            "Os": "linux",
            "RootFS": {"Layers": ["sha256:untrusted-base", "sha256:dependencies"]},
            "Config": {"Labels": support._dependency_labels()},
        },
    )
    pinned_python = SimpleNamespace(
        attrs={"RootFS": {"Layers": ["sha256:python-base"]}}
    )
    images = SimpleNamespace(
        get=Mock(
            side_effect=lambda value: image
            if value == "fixed-tag"
            else pinned_python
        )
    )
    client = SimpleNamespace(images=images, containers=SimpleNamespace(), close=Mock())
    monkeypatch.setattr(support.docker, "from_env", lambda: client)
    monkeypatch.setattr(support, "dependency_bootstrap_image_tag", lambda: "fixed-tag")

    with pytest.raises(RuntimeError, match="pinned Python RootFS"):
        support.audited_dependency_image_id()
    assert images.get.call_args_list == [
        call("fixed-tag"),
        call(support.PYTHON_IMAGE),
    ]


def test_dependency_source_free_verifier_checks_every_tradeos_top_level_root():
    """隔离依赖镜像不得可导入或携带任一 TradeOS 顶层源码根。"""

    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return b"dependency-only\n"

    client = SimpleNamespace(containers=SimpleNamespace(run=run))

    assert support._dependency_image_is_source_free(client, "sha256:" + "a" * 64)
    program = calls[0][0][1][1]
    for root in (
        "apps",
        "domains",
        "shared",
        "connectors",
        "workflows",
        "tool_gateway",
        "artifact_store",
        "infra",
        "migrations",
        "agent_runtime",
        "notification_gateway",
        "scripts",
        "skills",
        "tests",
    ):
        assert repr(root) in program
    assert "find_spec" in program


@pytest.mark.parametrize(
    "labels",
    [
        {},
        {"org.tradeos.dependency-only": "false"},
        {"org.tradeos.dependency-only": "true"},
    ],
)
def test_audited_dependency_image_rejects_missing_or_wrong_bootstrap_labels(
    monkeypatch, labels
):
    dependency = "sha256:" + "a" * 64
    image = SimpleNamespace(
        id=dependency,
        attrs={
            "Architecture": "arm64", "Os": "linux", "Config": {"Labels": labels}
        }
    )
    client = SimpleNamespace(
        images=SimpleNamespace(get=Mock(return_value=image)),
        containers=SimpleNamespace(),
        close=Mock(),
    )
    monkeypatch.setattr(support.docker, "from_env", lambda: client)
    monkeypatch.setattr(support, "dependency_lock_sha256", lambda: "lock-sha")
    monkeypatch.setattr(support, "dependency_bootstrap_image_tag", lambda: "fixed-tag")

    with pytest.raises(RuntimeError, match="dependency-only"):
        support.audited_dependency_image_id()
    client.close.assert_called_once()


def test_audited_dependency_image_hard_fails_when_explicit_bootstrap_is_missing(
    monkeypatch,
):
    """正式源码门不能为补依赖联网，缺 artifact 必须要求单独 bootstrap。"""

    client = SimpleNamespace(
        images=SimpleNamespace(
            get=Mock(side_effect=support.docker.errors.ImageNotFound("missing"))
        ),
        close=Mock(),
    )
    monkeypatch.setattr(support.docker, "from_env", lambda: client)
    monkeypatch.setattr(support, "dependency_bootstrap_image_tag", lambda: "missing")
    monkeypatch.setattr(
        support,
        "bootstrap_dependency_image",
        lambda: pytest.fail("正式 selector 不得启动联网 bootstrap"),
    )

    with pytest.raises(RuntimeError, match="先显式运行 bootstrap"):
        support.audited_dependency_image_id()
    client.images.get.assert_called_once_with("missing")
    client.close.assert_called_once()


def test_dependency_bootstrap_is_the_only_networked_build_and_uses_minimal_context(
    monkeypatch,
):
    """bootstrap 可取公开锁定依赖；后续源码层仍必须 network none。"""

    seen = {}

    def build(**kwargs):
        with tarfile.open(fileobj=kwargs["fileobj"]) as archive:
            seen["names"] = archive.getnames()
        seen["target"] = kwargs["target"]
        seen["network_mode"] = kwargs["network_mode"]
        seen["labels"] = kwargs["labels"]
        seen["tag"] = kwargs["tag"]
        return SimpleNamespace(id="sha256:" + "a" * 64), []

    client = SimpleNamespace(images=SimpleNamespace(build=build), close=Mock())
    monkeypatch.setattr(support.docker, "from_env", lambda: client)
    monkeypatch.setattr(support, "dependency_lock_sha256", lambda: "lock-sha")
    monkeypatch.setattr(support, "dependency_bootstrap_image_tag", lambda: "fixed-tag")

    assert support.bootstrap_dependency_image() == "sha256:" + "a" * 64
    assert seen["names"] == [
        "tests/fixtures/quote_evidence/linux/Dockerfile",
        "tests/fixtures/quote_evidence/linux/requirements.lock",
    ]
    assert seen["target"] == "dependency"
    assert seen["network_mode"] == "default"
    assert seen["tag"] == "fixed-tag"
    assert seen["labels"] == {
        "org.tradeos.dependency-only": "true",
        "org.tradeos.dependency-base": support.PYTHON_IMAGE,
        "org.tradeos.dependency-lock-sha256": "lock-sha",
        "org.tradeos.dependency-manifest-sha256": (
            support._AUDITED_DEPENDENCY_MANIFEST_SHA256
        ),
        "org.tradeos.dependency-platform": "linux/arm64",
    }
    client.close.assert_called_once()


def test_dependency_bootstrap_lock_is_hashed_and_cannot_install_the_project():
    """锁文件既固定版本也固定 arm64 轮子内容，不能借 editable 项目源码。"""

    lock = (
        Path(__file__).parents[1]
        / "fixtures/quote_evidence/linux/requirements.lock"
    ).read_text()
    entries = [line for line in lock.splitlines() if line and not line.startswith("#")]
    assert entries
    assert all("==" in line and " --hash=sha256:" in line for line in entries)
    assert "tradeos-agent" not in "\n".join(entries).lower()
    assert "file://" not in "\n".join(entries).lower()


@pytest.mark.parametrize("quotation", [True, False])
def test_runtime_image_uses_explicit_b2_whitelist_without_changing_a_chain(
    monkeypatch, quotation
):
    seen = {}

    def build(**kwargs):
        with tarfile.open(fileobj=kwargs["fileobj"]) as archive:
            seen["names"] = archive.getnames()
        seen["target"] = kwargs["target"]
        seen["network_mode"] = kwargs["network_mode"]
        return SimpleNamespace(id="sha256:" + "a" * 64), []

    client = SimpleNamespace(
        images=SimpleNamespace(get=lambda _: object(), build=build), close=lambda: None
    )
    monkeypatch.setattr(support.docker, "from_env", lambda: client)
    kwargs = {"chain": True, "quotation": True} if quotation else {"chain": True}
    support.build_parser_image("sha256:" + "b" * 64, **kwargs)
    assert seen["target"] == ("quotation" if quotation else "chain")
    assert seen["network_mode"] == "none"
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
    assert "tests/e2e/costing_quote_lifecycle.py" not in seen["names"]
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
