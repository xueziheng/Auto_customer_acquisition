"""reply_qualification 步骤 handler 骨架。流程图见 AGENTS.md。"""

from __future__ import annotations

from typing import Any

from workflows.engine.runner import WorkflowRun


class ClassifyStep:
    """qualification_agent 分类。AUTO_REPLY 短路 complete。"""

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        raise NotImplementedError


class ApplyActionsStep:
    """按 REPLY_ACTIONS 逐个执行域动作，逐个幂等。"""

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        raise NotImplementedError


class ExtractNeedStep:
    """提取需求字段进 demand 域，字段级 provenance。"""

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        raise NotImplementedError


class DecideNextStep:
    """接管 / 追问 / 结束 三分支。"""

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        raise NotImplementedError
