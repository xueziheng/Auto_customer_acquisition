"""真实演示启动器必须加载所选 checkout，且不继承父进程的额外环境。"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from shared.schemas.identifiers import new_id


@pytest.mark.parametrize(
    "demo",
    [
        "artifact_store",
        "email_feedback",
        "gmail_manual_send",
        "opportunities",
        "opportunity_board",
        "outreach",
        "sending_identity",
        "slice4_manual_send",
    ],
)
def test_demo_launcher_loads_selected_checkout_without_parent_environment(
    demo: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """漏传或允许覆盖 checkout 路径时，真实子进程会加载错误的同名包。"""
    launcher = importlib.import_module(f"tests.integration.test_demo_{demo}")
    checkout = tmp_path / "selected checkout"
    other_checkout = tmp_path / "other checkout"
    for root in (checkout, other_checkout):
        (root / "shared").mkdir(parents=True)
        (root / "shared" / "__init__.py").write_text("", encoding="utf-8")
    (checkout / "scripts").mkdir()
    script = checkout / "scripts" / f"demo_{demo}.py"
    script.write_text(
        "import json, os, shared\n"
        "from pathlib import Path\n"
        "print(json.dumps({\n"
        "  'checkout': str(Path(shared.__file__).resolve().parent.parent),\n"
        "  'parent_marker': 'TRADEOS_PARENT_SENTINEL' in os.environ,\n"
        "  'explicit_input': os.environ.get('DATABASE_URL'),\n"
        "}))\n",
        encoding="utf-8",
    )
    root_attribute = "_ROOT" if demo == "email_feedback" else "_REPO_ROOT"
    monkeypatch.setattr(launcher, root_attribute, checkout)
    if demo == "email_feedback":
        monkeypatch.setattr(launcher, "_SCRIPT", script)
    monkeypatch.setenv("TRADEOS_PARENT_SENTINEL", "synthetic-parent-only")
    monkeypatch.setenv("PYTHONPATH", str(other_checkout))

    if demo == "artifact_store":
        result = launcher._run(
            {"DATABASE_URL": "unused", "PYTHONPATH": str(other_checkout)}
        )
    elif demo == "email_feedback":
        result = launcher._run_demo("unused", tenant_id=new_id("tn"))
    elif demo == "slice4_manual_send":
        result = launcher._run_demo(
            "unused", extra_env={"PYTHONPATH": str(other_checkout)}
        )
    else:
        result = launcher._run_demo("unused")

    assert result.returncode == 0, "checkout 探针子进程失败"
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "checkout": str(checkout.resolve()),
        "parent_marker": False,
        "explicit_input": "unused",
    }
