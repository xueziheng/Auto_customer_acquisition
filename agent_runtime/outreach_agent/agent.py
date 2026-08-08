"""OutreachAgent —— 见同目录 AGENTS.md。"""

from __future__ import annotations

from agent_runtime.base import AgentTask, CapabilityAgent, ChangeSet


class OutreachAgent(CapabilityAgent):
    name = "outreach_agent"

    async def run(self, task: AgentTask, context: object) -> ChangeSet:
        raise NotImplementedError
