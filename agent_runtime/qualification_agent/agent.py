"""QualificationAgent —— 见同目录 AGENTS.md。"""

from __future__ import annotations

from agent_runtime.base import AgentTask, CapabilityAgent, ChangeSet


class QualificationAgent(CapabilityAgent):
    name = "qualification_agent"

    async def run(self, task: AgentTask, context: object) -> ChangeSet:
        raise NotImplementedError
