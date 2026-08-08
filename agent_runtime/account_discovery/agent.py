"""AccountDiscoveryAgent —— 见同目录 AGENTS.md。"""

from __future__ import annotations

from agent_runtime.base import AgentTask, CapabilityAgent, ChangeSet


class AccountDiscoveryAgent(CapabilityAgent):
    name = "account_discovery"

    async def run(self, task: AgentTask, context: object) -> ChangeSet:
        raise NotImplementedError
