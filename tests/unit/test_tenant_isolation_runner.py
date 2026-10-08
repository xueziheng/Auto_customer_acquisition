"""隔离验收入口必须拥有测试库、透传失败且不回显连接材料。"""

from __future__ import annotations

import importlib
import secrets
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import pytest
from sqlalchemy.engine import URL


def _runner():
    try:
        return importlib.import_module("tests.run_tenant_isolation")
    except ModuleNotFoundError:
        pytest.fail("独立企业隔离验收入口尚未实现")


@pytest.mark.parametrize(
    ("exit_code", "skipped", "empty", "expected"),
    [(0, False, False, 0), (1, False, False, 1), (5, False, False, 5),
     (0, True, False, 1), (0, False, True, 1)],
)
def test_runner_uses_owned_database_and_preserves_pytest_failure(
    monkeypatch, capsys, exit_code: int, skipped: bool, empty: bool, expected: int,
) -> None:
    module = _runner()
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    for name in ("TEST_DATABASE_URL", "DATABASE_URL", "TRADEOS_ISOLATION_TEST_DATABASE_URL"):
        monkeypatch.setenv(name, "inherited-database-canary")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--ignore=tests/integration/test_tenant_row_security.py")
    instances = []
    reports: list[Path] = []
    url_canary = secrets.token_hex(16)

    class Container:
        def __init__(self, image, **kwargs):
            self.image, self.options, self.stopped = image, kwargs, False
            instances.append(self)

        def start(self):
            return self

        def stop(self):
            self.stopped = True

        def get_connection_url(self, *, driver):
            redact_credentials = False
            return URL.create(
                "postgresql+" + driver, username="synthetic", password=url_canary,
                host="127.0.0.1", port=5432, database=self.options["dbname"],
            ).render_as_string(hide_password=redact_credentials)

    def run(command, **kwargs):
        assert command[:3] == [module.sys.executable, "-m", "pytest"]
        assert command[3:5] == [
            "tests/integration/test_tenant_row_security.py",
            "tests/integration/test_enterprise_api_isolation.py",
        ]
        env = kwargs["env"]
        assert "TEST_DATABASE_URL" not in env
        assert "DATABASE_URL" not in env
        assert "PYTEST_ADDOPTS" not in env
        assert url_canary in env["TRADEOS_ISOLATION_TEST_DATABASE_URL"]
        assert "inherited-database-canary" not in repr(env)
        assert kwargs["capture_output"] is True
        report = next(value.split("=", 1)[1] for value in command if value.startswith("--junitxml="))
        result = "<skipped/>" if skipped else "<failure/>" if exit_code == 1 else ""
        case = (
            '<testcase classname="tests.integration.test_tenant_row_security" '
            f'name="test_owned[private-parameter-canary]">{result}</testcase>'
        )
        Path(report).write_text(
            f"<testsuites><testsuite>{'' if empty else case}</testsuite></testsuites>"
        )
        reports.append(Path(report))
        return subprocess.CompletedProcess(command, exit_code, "private-output-canary", "private-error-canary")

    monkeypatch.setattr(module, "PostgresContainer", Container)
    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.main() == expected
    assert len(instances) == 1
    assert instances[0].image == "pgvector/pgvector:pg16"
    assert instances[0].options["dbname"].startswith("tradeos_isolation_test_")
    assert instances[0].stopped
    captured = capsys.readouterr()
    assert "canary" not in captured.out + captured.err
    assert url_canary not in captured.out + captured.err
    assert reports and all(not report.exists() for report in reports)
    if not empty and (skipped or exit_code == 1):
        category = "skipped" if skipped else "failure"
        assert (
            "tests.integration.test_tenant_row_security::test_owned " + category
        ) in captured.out


def test_docker_failure_is_nonzero_and_still_cleans_partial_container(monkeypatch, capsys) -> None:
    module = _runner()
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    stopped = []

    class BrokenContainer:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("private-docker-canary")

        def stop(self):
            stopped.append(True)

    monkeypatch.setattr(module, "PostgresContainer", BrokenContainer)
    assert module.main() == 2
    assert stopped == [True]
    captured = capsys.readouterr()
    assert "Docker" in captured.out
    assert "canary" not in captured.out + captured.err


def test_runner_rejects_remote_docker_before_container_creation(monkeypatch) -> None:
    module = _runner()
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.invalid:2376")
    monkeypatch.setattr(module, "PostgresContainer", lambda *args, **kwargs: pytest.fail("不可访问远程Docker"))
    assert module.main() == 2


def test_diagnostics_emit_only_fixed_modules_test_names_and_categories() -> None:
    module = _runner()
    root = ElementTree.Element("testsuites", {"message": "private-attribute-canary"})
    case = ElementTree.SubElement(
        root, "testcase",
        classname="tests.integration.test_enterprise_knowledge_isolation",
        name="test_company_scope[postgresql://synthetic:private-parameter-canary@host/db]",
        file="private-file-canary",
        other="private-other-canary",
    )
    for category in ("failure", "error", "skipped"):
        result = ElementTree.SubElement(case, category, message="private-message-canary")
        result.text = "private-traceback-canary"
    ElementTree.SubElement(case, "system-out").text = "private-stdout-canary"
    before = ElementTree.tostring(root)
    assert module._safe_test_diagnostics(root) == (
        "tests.integration.test_enterprise_knowledge_isolation::test_company_scope error",
        "tests.integration.test_enterprise_knowledge_isolation::test_company_scope failure",
        "tests.integration.test_enterprise_knowledge_isolation::test_company_scope skipped",
    )
    assert ElementTree.tostring(root) == before


@pytest.mark.parametrize(
    ("classname", "name"),
    [
        ("", "test_scope"),
        ("tests.integration.test_enterprise_knowledge_isolation.private_canary", "test_scope"),
        ("tests/integration/test_enterprise_knowledge_isolation.py", "test_scope"),
        ("postgresql://private-module-canary", "test_scope"),
        ("tests.integration.test_enterprise_knowledge_isolation", ""),
        ("tests.integration.test_enterprise_knowledge_isolation", "owned"),
        ("tests.integration.test_enterprise_knowledge_isolation", "test_scope private-canary"),
        ("tests.integration.test_enterprise_knowledge_isolation", "test_scope\npostgresql://private-canary"),
        ("tests.integration.test_enterprise_knowledge_isolation", "test_租户"),
        ("tests.integration.test_enterprise_knowledge_isolation", "test_scope]private-canary"),
    ],
)
def test_diagnostics_reject_noncanonical_module_and_test_names(
    classname: str, name: str,
) -> None:
    module = _runner()
    root = ElementTree.Element("testsuites")
    case = ElementTree.SubElement(root, "testcase", classname=classname, name=name)
    ElementTree.SubElement(case, "failure", message="private-canary")
    assert module._safe_test_diagnostics(root) == ()


def test_diagnostics_ignore_passed_cases_and_deduplicate_parameter_failures() -> None:
    module = _runner()
    root = ElementTree.Element("testsuites")
    for path in module._FILES:
        classname = path.removesuffix(".py").replace("/", ".")
        ElementTree.SubElement(root, "testcase", classname=classname, name="test_passed")
        for parameter in ("private-first-canary", "private-second-canary"):
            case = ElementTree.SubElement(
                root, "testcase", classname=classname, name=f"test_scope[{parameter}]",
            )
            ElementTree.SubElement(case, "failure")
    assert module._safe_test_diagnostics(root) == tuple(sorted(
        f"{path.removesuffix('.py').replace('/', '.')}::test_scope failure"
        for path in module._FILES
    ))
    assert module._safe_test_diagnostics(ElementTree.Element("testsuites")) == ()
