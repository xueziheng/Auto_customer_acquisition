"""真实Linux Codex执行MCP采集工具；模型与公开网络由受控端口替代。"""

import asyncio
import json
import os
from pathlib import Path

import pytest

from connectors.codex_agent.client import codex_agent_provider
from connectors.codex_agent.discovery import DiscoverySession
from connectors.deepseek.client import DeepSeekFailure
from connectors.obsidian.workspace import KnowledgeTaskScope
from shared.schemas.model_invocation import ModelRequest
from tests.unit.test_codex_provider import FakeDelegate, settings
from tests.unit.test_codex_provider import short_jobs as short_jobs  # noqa: PLC0414


@pytest.mark.parametrize("final_phase", ["omitted", None, "final_answer"])
@pytest.mark.skipif(
    not os.environ.get("TRADEOS_TEST_CODEX_BINARY"),
    reason="须显式选择Linux原生Codex；仅使用合成模型及网页材料",
)
async def test_native_codex_executes_discovery_tools_and_returns_bound_results(
    short_jobs, final_phase,
):
    calls = []
    tasks = []

    async def execute(name, arguments):
        calls.append((name, arguments))
        tasks.append(asyncio.current_task())
        if name == "source_search":
            return {"results": [{"page_id": "synthetic-page", "title": "Synthetic"}]}
        assert name == "read_public_page" and arguments == {"page_id": "synthetic-page"}
        return {
            "page_id": "synthetic-page",
            "text": "Synthetic public company evidence",
        }

    class Delegate(FakeDelegate):
        def __init__(self):
            super().__init__()
            self.rounds = 0
            self.outputs = []

        async def round(self, body):
            assert body["text"] == {"format": {"type": "json_object"}}
            self.rounds += 1
            self.outputs = [
                item.get("output")
                for item in body["input"]
                if item.get("type") == "function_call_output"
            ]
            sequence = (
                ("read_task_context", {}),
                (
                    "source_search",
                    {"source_id": "company_websites", "query": "tricycles Kenya"},
                ),
                ("read_public_page", {"page_id": "synthetic-page"}),
            )
            if self.rounds <= len(sequence):
                name, arguments = sequence[self.rounds - 1]
                output = [
                    {
                        "type": "function_call",
                        "id": f"f{self.rounds}",
                        "call_id": f"c{self.rounds}",
                        "name": "mcp__tradeos__" + name,
                        "arguments": json.dumps(arguments),
                        "status": "completed",
                    }
                ]
            else:
                assert self.rounds == 4
                assert "Synthetic public company evidence" in json.dumps(body["input"])
                output = [
                    {
                        "type": "message",
                        "id": "m4",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": '{"candidates":[]}'}
                        ],
                    }
                ]
            if self.rounds == 4 and final_phase != "omitted":
                output[0]["phase"] = final_phase
            return {
                "id": f"r{self.rounds}",
                "status": "completed",
                "model": body["model"],
                "output": output,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }

    delegate = Delegate()
    async with DiscoverySession(
        execute, max_requests=4, timeout_seconds=40, max_result_bytes=4096
    ) as session:
        provider = codex_agent_provider(
            delegate,
            settings=settings(
                short_jobs, Path(os.environ["TRADEOS_TEST_CODEX_BINARY"]), timeout=40
            ),
            scope=KnowledgeTaskScope("tenant_test", "employee_test", "run_test"),
            discovery=session,
        )
        try:
            result = await provider.generate(
                ModelRequest(
                    model="deepseek-flash",
                    system_prompt="Read context, search and read public evidence; return JSON.",
                    payload={"query": "synthetic"},
                    max_output_tokens=128,
                )
            )
        except DeepSeekFailure as failure:
            pytest.fail(
                f"code={failure.code}; rounds={delegate.rounds}; tools={[name for name, _ in calls]}; outputs={delegate.outputs}",
                pytrace=False,
            )
        finally:
            await provider.aclose()
    assert json.loads(result.text) == {"candidates": []}
    assert [name for name, _ in calls] == ["source_search", "read_public_page"]
    assert tasks[0] is tasks[1]
    assert result.usage.output_tokens == 20
    assert not list(short_jobs.iterdir())
