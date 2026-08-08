"""DemandIntelligenceAgent —— 见同目录 AGENTS.md。"""

from __future__ import annotations

from agent_runtime.base import AgentTask, CapabilityAgent, ChangeSet


class DemandIntelligenceAgent(CapabilityAgent):
    name = "demand_intelligence"

    async def run(self, task: AgentTask, context: object) -> ChangeSet:
        raise NotImplementedError
