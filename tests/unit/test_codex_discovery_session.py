"""真实session跨MCP任务交互，不依赖网络或模型凭证。"""

import asyncio

import pytest


def session_type():
    import connectors.codex_agent as package

    assert hasattr(package, "discovery"), "RED: discovery工具session尚未实现"
    return package.discovery.DiscoverySession


async def test_session_runs_cross_task_calls_in_one_owner_and_bounds_requests():
    cls = session_type()
    owners = []

    async def execute(name, arguments):
        owners.append(asyncio.current_task())
        return {"name": name, "value": arguments["query"]}

    async with cls(
        execute, max_requests=2, timeout_seconds=5, max_result_bytes=200
    ) as session:
        assert (
            await asyncio.create_task(session.call("source_search", {"query": "a"}))
        )["value"] == "a"
        await asyncio.create_task(session.call("source_search", {"query": "b"}))
        assert (await session.call("source_search", {"query": "c"}))["error"][
            "code"
        ] == "budget_exhausted"
    assert len(owners) == 2 and owners[0] is owners[1]


async def test_session_rejects_unknown_tools_oversized_results_and_calls_after_close():
    cls = session_type()

    async def execute(name, arguments):
        return {"text": "x" * 100}

    async with cls(
        execute, max_requests=2, timeout_seconds=5, max_result_bytes=20
    ) as session:
        with pytest.raises(ValueError):
            await session.call("shell", {})
        with pytest.raises(ValueError):
            await session.call("read_public_page", {"page_id": "p"})
    with pytest.raises(ValueError):
        await session.call("source_search", {})


async def test_session_cancellation_stops_active_tool():
    cls = session_type()
    entered, exited = asyncio.Event(), asyncio.Event()

    async def execute(name, arguments):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    async with cls(
        execute, max_requests=2, timeout_seconds=5, max_result_bytes=200
    ) as session:
        task = asyncio.create_task(session.call("source_search", {}))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(exited.wait(), 1)


async def test_discovery_broker_executes_only_model_issued_call_once():
    from connectors.codex_agent.client import AgentBroker
    from connectors.codex_agent.discovery import DiscoverySession
    from connectors.deepseek.client import DeepSeekFailure
    from shared.schemas.model_invocation import ModelRequest

    calls = []

    async def execute(name, arguments):
        calls.append((name, arguments))
        return {"page_id": "trusted-page"}

    class Delegate:
        async def round(self, body):
            assert any(t["name"].endswith("source_search") for t in body["tools"])
            return {
                "status": "completed",
                "model": "chosen",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "c1",
                        "name": "mcp__tradeos__source_search",
                        "arguments": '{"source_id":"company_websites","query":"tricycles Kenya"}',
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }

    async with DiscoverySession(
        execute, max_requests=2, timeout_seconds=5, max_result_bytes=200
    ) as session:
        broker = AgentBroker(
            Delegate(),
            ModelRequest(
                model="chosen", system_prompt="JSON", payload={}, max_output_tokens=20
            ),
            max_output_bytes=1000,
            discovery=session,
        )
        await broker.exchange(
            {
                "model": "chosen",
                "input": [],
                "tools": [{"type": "function", "name": "mcp__tradeos__source_search"}],
            }
        )
        raw = {
            "method": "tools/call",
            "params": {
                "name": "source_search",
                "arguments": {
                    "source_id": "company_websites",
                    "query": "tricycles Kenya",
                },
            },
        }
        assert (await broker.exchange(raw))["result"] == {"page_id": "trusted-page"}
        with pytest.raises(DeepSeekFailure):
            await broker.exchange(raw)
        assert len(calls) == 1


async def test_unix_broker_lists_discovery_tools_and_default_is_context_only(tmp_path):
    import json
    import struct

    from connectors.codex_agent.client import AgentBroker
    from connectors.codex_agent.discovery import DiscoverySession
    from shared.schemas.model_invocation import ModelRequest

    async def execute(name, arguments):
        return {}

    async with DiscoverySession(
        execute, max_requests=2, timeout_seconds=5, max_result_bytes=200
    ) as session:
        for index, selected in enumerate((None, session)):
            broker = AgentBroker(
                None,
                ModelRequest(
                    model="chosen",
                    system_prompt="JSON",
                    payload={},
                    max_output_tokens=20,
                ),
                max_output_bytes=1000,
                discovery=selected,
            )
            path = str(tmp_path / str(index))
            async with await asyncio.start_unix_server(broker.handle, path=path):
                reader, writer = await asyncio.open_unix_connection(path)
                payload = json.dumps({"method": "tools/list"}).encode()
                writer.write(struct.pack("!I", len(payload)) + payload)
                await writer.drain()
                length = struct.unpack("!I", await reader.readexactly(4))[0]
                result = json.loads(await reader.readexactly(length))
                assert [t["name"] for t in result["tools"]] == (
                    []
                    if selected is None
                    else ["source_search", "read_public_page", "browse_public_page"]
                )
                for tool in result["tools"]:
                    assert tool["annotations"] == {
                        "readOnlyHint": True,
                        "destructiveHint": False,
                        "openWorldHint": True,
                    }
                writer.close()
                await writer.wait_closed()
            await broker.close()


@pytest.mark.parametrize("failure_mode", ["page", "budget", "permission", "uncertain"])
async def test_unix_broker_page_failure_or_budget_can_finish_but_authority_and_uncertain_close(
    tmp_path, failure_mode
):
    import json
    import struct

    from connectors.codex_agent import discovery
    from connectors.codex_agent.client import AgentBroker
    from shared.schemas.model_invocation import ModelRequest

    assert hasattr(discovery, "DiscoveryToolFailure"), "RED: 缺少显式可恢复工具错误"
    executed = []

    async def execute(name, arguments):
        executed.append(name)
        if failure_mode == "permission":
            raise PermissionError("synthetic private diagnostic")
        if failure_mode == "uncertain":
            raise TimeoutError("synthetic private diagnostic")
        if failure_mode == "page":
            raise discovery.DiscoveryToolFailure("page_unavailable")
        return {"text": "trusted evidence"}

    class Delegate:
        rounds = 0

        async def round(self, body):
            self.rounds += 1
            if self.rounds < 3:
                output = [
                    {
                        "type": "function_call",
                        "call_id": f"c{self.rounds}",
                        "name": "mcp__tradeos__"
                        + (
                            "read_task_context"
                            if self.rounds == 1
                            else "read_public_page"
                        ),
                        "arguments": "{}" if self.rounds == 1 else '{"page_id":"p"}',
                    }
                ]
            else:
                output = [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": '{"candidates":[]}'}
                        ],
                    }
                ]
            return {
                "status": "completed",
                "model": "chosen",
                "output": output,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }

    async with discovery.DiscoverySession(
        execute, max_requests=1, timeout_seconds=5, max_result_bytes=200
    ) as session:
        if failure_mode == "budget":
            await session.call("read_public_page", {"page_id": "previous"})
        broker = AgentBroker(
            Delegate(),
            ModelRequest(
                model="chosen", system_prompt="JSON", payload={}, max_output_tokens=20
            ),
            max_output_bytes=1000,
            discovery=session,
        )
        path = str(tmp_path / "broker")

        async def request(raw):
            reader, writer = await asyncio.open_unix_connection(path)
            encoded = json.dumps(raw).encode()
            writer.write(struct.pack("!I", len(encoded)) + encoded)
            await writer.drain()
            length = struct.unpack("!I", await reader.readexactly(4))[0]
            result = json.loads(await reader.readexactly(length))
            writer.close()
            await writer.wait_closed()
            return result

        async with await asyncio.start_unix_server(broker.handle, path=path):
            base = {"model": "chosen", "input": []}
            assert (await request(base))["status"] == 200
            base["input"] = [
                {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": '{"instructions":"JSON","input":{}}',
                }
            ]
            assert (await request(base))["status"] == 200
            result = await request(
                {
                    "method": "tools/call",
                    "params": {
                        "name": "read_public_page",
                        "arguments": {"page_id": "p"},
                    },
                }
            )
            assert "synthetic private" not in json.dumps(result)
            if failure_mode in ("permission", "uncertain"):
                assert result["status"] == 400 and broker.failure is not None
                assert (await request(base))["status"] == 400
            else:
                assert result["result"]["error"]["code"] == (
                    "page_unavailable" if failure_mode == "page" else "budget_exhausted"
                )
                assert broker.failure is None
                base["input"].append(
                    {
                        "type": "function_call_output",
                        "call_id": "c2",
                        "output": json.dumps(result["result"]),
                    }
                )
                assert (await request(base))["status"] == 200
                assert broker.response.text == '{"candidates":[]}'
                assert len(executed) == 1
        await broker.close()


async def test_uncertain_tool_failure_does_not_dispatch_already_queued_calls():
    from connectors.codex_agent.discovery import DiscoverySession

    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def execute(name, arguments):
        calls.append(name)
        entered.set()
        await release.wait()
        raise TimeoutError("synthetic uncertain request")

    async with DiscoverySession(
        execute, max_requests=2, timeout_seconds=5, max_result_bytes=200
    ) as session:
        first = asyncio.create_task(session.call("source_search", {}))
        await entered.wait()
        second = asyncio.create_task(session.call("source_search", {}))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, ValueError) for result in results)
    assert calls == ["source_search"]
