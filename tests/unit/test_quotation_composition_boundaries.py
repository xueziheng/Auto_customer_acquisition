"""非进程装配库的绝对/相对依赖门；不以顶层apps层级检查代替进程隔离。"""

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest


def app_dependencies(source, package):
    result = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            result.update(
                alias.name for alias in node.names if alias.name.startswith("apps")
            )
        elif isinstance(node, ast.ImportFrom):
            name = "." * node.level + (node.module or "")
            base = resolve_name(name, package) if node.level else name
            if base == "apps" or base.startswith("apps."):
                result.add(base)
                result.update(base + "." + alias.name for alias in node.names)
    return result


def forbidden_imports(source, package, allowed):
    return {
        name
        for name in app_dependencies(source, package)
        if name != "apps"
        and not any(
            name == prefix or name.startswith(prefix + ".") for prefix in allowed
        )
    }


@pytest.mark.parametrize(
    "source",
    [
        "import apps.api.runtime",
        "from apps.api import runtime",
        "from ..api import runtime",
        "from .. import api",
        "from apps import scheduler_worker",
        "from ..scheduler_worker.runtime import SchedulerRuntimeFactory",
    ],
)
def test_dependency_rule_rejects_absolute_and_relative_process_imports(source):
    assert forbidden_imports(
        source, "apps.composition_support", ("apps.composition_support",)
    )


@pytest.mark.parametrize(
    "source",
    [
        "from .quotations import QuotationRuntimeLifecycle",
        "from domains.quotations.service import QuotationVersionService",
        "from infra.quotation_settings import QuotationRuntimeSettings",
    ],
)
def test_dependency_rule_accepts_own_library_and_lower_layers(source):
    assert not forbidden_imports(
        source, "apps.composition_support", ("apps.composition_support",)
    )


def test_actual_shared_library_and_both_roots_have_no_process_back_dependency():
    root = Path(__file__).parents[2]
    common = root / "apps/composition_support"
    assert (common / "quotations.py").is_file(), "未抽取共享装配实现"
    files = [
        file
        for directory in (common, root / "apps/api", root / "apps/scheduler_worker")
        for file in directory.rglob("*.py")
        if not file.name.startswith("._")
    ]
    for file in files:
        relative = file.relative_to(root)
        package = ".".join(relative.parts[:-1])
        allowed = ("apps.composition_support",)
        if "api" in relative.parts:
            allowed += ("apps.api",)
        elif "scheduler_worker" in relative.parts:
            allowed += ("apps.scheduler_worker",)
        assert not forbidden_imports(file.read_text(), package, allowed), relative
