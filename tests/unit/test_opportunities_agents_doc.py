"""S3-2 文档-事件契约测试（TDD）。

行为断言：``domains/opportunities/events.py`` 的 ``PUBLISHES`` 中每个事件
类名都必须出现在 ``domains/opportunities/AGENTS.md`` 文本（含 ``OpportunityWon``）。
文档与代码的事件清单一旦漂移立刻被发现。

导入约定：直接 import 域公共符号；新符号延迟导入转行为失败，不用 try/except 吞错。
"""
from __future__ import annotations

from pathlib import Path

from domains.opportunities.events import PUBLISHES

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENTS_MD = _REPO_ROOT / "domains" / "opportunities" / "AGENTS.md"


def test_every_published_event_documented_in_agents_md() -> None:
    """``PUBLISHES`` 每个事件类名都出现在 AGENTS.md 文本。"""
    doc = _AGENTS_MD.read_text(encoding="utf-8")
    missing = [ev.__name__ for ev in PUBLISHES if ev.__name__ not in doc]
    assert not missing, f"AGENTS.md 发布事件清单缺失: {missing}"
