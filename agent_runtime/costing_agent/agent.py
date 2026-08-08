"""CostingAgent —— 见同目录 AGENTS.md。"""

from __future__ import annotations

from agent_runtime.base import AgentTask, CapabilityAgent, ChangeSet


class CostingAgent(CapabilityAgent):
    name = "costing_agent"

    async def run(self, task: AgentTask, context: object) -> ChangeSet:
        raise NotImplementedError
