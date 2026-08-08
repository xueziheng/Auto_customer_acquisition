"""outreach_campaign 步骤 handler 骨架。流程图见 AGENTS.md。

每个 handler 实现 workflows.engine.runner.StepHandler。
LLM/IO 全部在 handler 内；handler 必须幂等。
"""

from __future__ import annotations

from typing import Any

from workflows.engine.runner import WorkflowRun


class DraftContentStep:
    """调 outreach_agent 生成本步草稿。产出进 run.context['draft_ref']。"""

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        raise NotImplementedError


class PrepareSendStep:
    """调 outreach 域 prepare_send。未授权时按拒绝原因分流（见 AGENTS.md）。"""

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        raise NotImplementedError


class SendStep:
    """经 tool_gateway 执行 email.send，幂等键来自 SendAuthorization。"""

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        raise NotImplementedError


class RecordSentStep:
    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        raise NotImplementedError


class WaitForReplyStep:
    """WAITING_EVENT(ReplyReceived)；超时后决定发下一封还是完成。"""

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        raise NotImplementedError
