import json

import pytest

from connectors.codex_agent.bridge import (
    checked_request,
    response_events,
    to_chat_messages,
)
from connectors.deepseek.client import DeepSeekFailure


def test_agent_request_locks_model_and_rejects_unapproved_tools():
    raw = {"model": "other", "input": "test", "tools": []}
    with pytest.raises(DeepSeekFailure):
        checked_request(raw, "chosen", 128)
    with pytest.raises(DeepSeekFailure):
        checked_request({"model": "chosen", "input": "test", "tools": [{"type": "web_search"}]}, "chosen", 128)
    value = checked_request({"model": "chosen", "input": "test", "store": True, "max_output_tokens": 9000}, "chosen", 128)
    assert value["store"] is False
    assert value["max_output_tokens"] == 128


def test_chat_conversion_keeps_tool_call_and_result():
    messages = to_chat_messages({"instructions": "Use task context", "input": [
        {"role": "user", "content": [{"type": "input_text", "text": "start"}]},
        {"type": "function_call", "call_id": "call-1", "name": "mcp__tradeos__read_task_context", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call-1", "output": "context"},
    ]})
    assert messages[-2]["tool_calls"][0]["id"] == "call-1"
    assert messages[-1] == {"role": "tool", "tool_call_id": "call-1", "content": "context"}


def test_sse_preserves_function_calls_for_codex():
    body = {"id": "r1", "status": "completed", "model": "chosen", "output": [
        {"type": "function_call", "id": "f1", "call_id": "c1", "name": "mcp__tradeos__read_task_context", "arguments": "{}", "status": "completed"}
    ]}
    events = [json.loads(line[6:]) for line in response_events(body).splitlines() if line.startswith("data: ")]
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["output"][0]["call_id"] == "c1"
    assert any(e["type"] == "response.output_item.done" for e in events)

async def test_broker_requires_real_tool_round_and_sums_usage():
    from connectors.codex_agent.client import AgentBroker
    from shared.schemas.model_invocation import ModelRequest
    class Delegate:
        calls = 0
        async def round(self, body):
            self.calls += 1
            output = ([{"type":"function_call", "id":"fc1", "call_id":"c1", "name":"mcp__tradeos__read_task_context", "arguments":"{}", "status":"completed"}] if self.calls == 1 else
                [{"type":"message", "id":"m1", "role":"assistant", "status":"completed", "content":[{"type":"output_text", "text":'{"ok":true}'}]}])
            return {"id":"r1", "status":"completed", "model":"chosen", "output":output, "usage":{"input_tokens":10, "output_tokens":5, "input_tokens_details":{"cached_tokens":2}}}
    broker = AgentBroker(Delegate(), ModelRequest(model="chosen",system_prompt="Return JSON",payload={"test":True},max_output_tokens=20), max_output_bytes=4096)
    first = {"model":"chosen", "input":[{"role":"user", "content":"start"}], "tools":[{"type":"function", "name":"mcp__tradeos__read_task_context"}]}
    assert (await broker.exchange(first))["status"] == 200
    assert broker.response is None
    await broker.exchange({**first,"input":[{"type":"function_call_output","call_id":"c1","output":'{"instructions":"Return JSON","input":{"test":true}}'}]})
    assert broker.response.text == '{"ok":true}'
    assert broker.response.usage.output_tokens == 10
    assert broker.response.usage.input_tokens == 20
    with pytest.raises(DeepSeekFailure):
        await broker.exchange(first)


async def test_broker_rejects_final_without_context_and_unknown_tool():
    from connectors.codex_agent.client import AgentBroker
    from shared.schemas.model_invocation import ModelRequest
    class Delegate:
        async def round(self, body):
            return {"status":"completed", "model":"chosen", "output":[{"type":"message", "role":"assistant","status":"completed","content":[{"type":"output_text","text":'{"ok":true}'}]}],"usage":{"input_tokens":1,"output_tokens":1}}
    broker = AgentBroker(Delegate(), ModelRequest(model="chosen",system_prompt="Return JSON",payload={},max_output_tokens=20),max_output_bytes=100)
    with pytest.raises(DeepSeekFailure):
        await broker.exchange({"model":"chosen","input":[]})


def test_namespaced_codex_tool_is_flattened_without_changing_other_names():
    body = checked_request({'model':'chosen','tools':[{'type':'namespace','name':'mcp__tradeos','tools':[{'type':'function','name':'read_task_context'}]}],
        'input':[{'type':'function_call','namespace':'mcp__tradeos','name':'read_task_context','call_id':'c','arguments':'{}'}]}, 'chosen', 128)
    assert body['tools'][0]['name'] == 'mcp__tradeos__read_task_context'
    assert body['input'][0]['name'] == 'mcp__tradeos__read_task_context'
    assert 'namespace' not in body['input'][0]


def test_sse_omits_unknown_usage_instead_of_serializing_null_integers():
    text=response_events({'id':'r','status':'completed','model':'chosen','output':[], 'usage':{'input_tokens':10,'output_tokens':5,'input_tokens_details':{'cached_tokens':None},'output_tokens_details':{'reasoning_tokens':None}}})
    event=json.loads([l[6:] for l in text.splitlines() if l.startswith('data: ')][-1])
    assert event['response']['usage']=={'input_tokens':10,'output_tokens':5,'total_tokens':15}
    text=response_events({'id':'r','status':'completed','model':'chosen','output':[], 'usage':{'input_tokens':None,'output_tokens':None}})
    event=json.loads([l[6:] for l in text.splitlines() if l.startswith('data: ')][-1])
    assert 'usage' not in event['response']


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("provider", "failed:provider:req:1"),
        ("arguments", "failed:tool_args:res:1"),
        ("final", "failed:final_json:res:1"),
    ],
)
async def test_discovery_failure_keeps_safe_phase_without_provider_text(mode, expected):
    from connectors.codex_agent.client import AgentBroker
    from connectors.codex_agent.discovery import DiscoverySession
    from shared.schemas.model_invocation import ModelRequest

    async def execute(name, arguments):
        return {}

    class Delegate:
        async def round(self, body):
            if mode == "provider":
                raise DeepSeekFailure("invalid_request")
            output = (
                [
                    {
                        "type": "function_call",
                        "name": "mcp__tradeos__source_search",
                        "arguments": "{malformed PRIVATE",
                    }
                ]
                if mode == "arguments"
                else [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "PRIVATE non-json"}
                        ],
                    }
                ]
            )
            return {
                "status": "completed",
                "model": "chosen",
                "output": output,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }

    async with DiscoverySession(
        execute, max_requests=1, timeout_seconds=10, max_result_bytes=1000
    ) as session:
        broker = AgentBroker(
            Delegate(),
            ModelRequest(
                model="chosen", system_prompt="JSON", payload={}, max_output_tokens=20
            ),
            max_output_bytes=1000,
            discovery=session,
        )
        broker._read_context = True
        with pytest.raises(DeepSeekFailure) as raised:
            await broker.exchange({"model": "chosen", "input": []})
        assert raised.value.diagnostic.outcome == expected
        assert "PRIVATE" not in str(raised.value.diagnostic)


@pytest.mark.parametrize(
    "stage,code,rounds",
    [
        ("PRIVATE", "invalid_response", 1),
        ("provider", "PRIVATE", 1),
        ("provider", "unknown", 17),
        ("provider", "unknown", True),
    ],
)
def test_diagnostic_rejects_unbounded_or_raw_values(stage, code, rounds):
    from shared.schemas.discovery_diagnostic import DiscoveryDiagnostic

    with pytest.raises(ValueError, match="^采集诊断无效$"):
        DiscoveryDiagnostic(stage, code, rounds)


def test_diagnostic_storage_accepts_only_bounded_canonical_projection():
    from shared.schemas.discovery_diagnostic import is_discovery_failure

    assert is_discovery_failure("failed:provider:req:16")
    for value in (
        "failed:provider:req:17",
        "failed:provider:req:01",
        "failed:provider:PRIVATE:1",
        "failed:https://private:req:1",
    ):
        assert not is_discovery_failure(value)


async def test_broker_keeps_safe_collector_failure_and_records_current_round():
    from connectors.codex_agent.client import AgentBroker
    from connectors.codex_agent.discovery import DiscoverySession
    from shared.schemas.discovery_diagnostic import (
        DiscoveryDiagnostic,
        DiscoveryDiagnosticError,
    )
    from shared.schemas.model_invocation import ModelRequest

    async def execute(name, arguments):
        raise DiscoveryDiagnosticError(
            DiscoveryDiagnostic("source_order", "invalid_request", 0)
        )

    async with DiscoverySession(
        execute, max_requests=1, timeout_seconds=10, max_result_bytes=1000
    ) as session:
        broker = AgentBroker(
            None,
            ModelRequest(
                model="chosen", system_prompt="JSON", payload={}, max_output_tokens=20
            ),
            max_output_bytes=1000,
            discovery=session,
        )
        broker._rounds = 3
        broker._issued.append(
            ("source_search", {"source_id": "company_websites", "query": "x"})
        )
        with pytest.raises(DeepSeekFailure) as raised:
            await broker.exchange(
                {
                    "method": "tools/call",
                    "params": {
                        "name": "source_search",
                        "arguments": {"source_id": "company_websites", "query": "x"},
                    },
                }
            )
        assert raised.value.diagnostic.outcome == "failed:source_order:req:3"


@pytest.mark.parametrize(
    "exit_code,has_final,output,expected",
    [
        (2, False, b"", "failed:cli_no_final:res:3"),
        (2, True, b"", "failed:cli_exit:res:3"),
        (0, True, b'{"final":"{\\"different\\":true}"}', "failed:cli_match:res:3"),
        (0, True, b"PRIVATE invalid envelope", "failed:cli_match:res:3"),
    ],
)
def test_cli_safe_diagnostic_distinguishes_provider_final(
    exit_code, has_final, output, expected
):
    from types import SimpleNamespace

    from connectors.codex.client import _validated_cli_response
    from shared.schemas.model_invocation import ModelResponse, ModelUsage

    broker = SimpleNamespace(
        failure=None,
        dispatched=True,
        _rounds=3,
        response=ModelResponse(
            text='{"ok":true}',
            model="chosen",
            usage=ModelUsage(input_tokens=1, output_tokens=1, cached_input_tokens=None),
        )
        if has_final
        else None,
    )
    with pytest.raises(DeepSeekFailure) as raised:
        _validated_cli_response(exit_code, output, broker)
    assert raised.value.diagnostic.outcome == expected


async def test_broker_keeps_first_failure_when_cli_sends_after_rejection(tmp_path):
    import asyncio
    import struct

    from connectors.codex_agent.client import AgentBroker
    from shared.schemas.model_invocation import ModelRequest

    class Delegate:
        async def round(self, body):
            raise DeepSeekFailure('invalid_request')

    broker = AgentBroker(Delegate(), ModelRequest(model='chosen', system_prompt='JSON',
                         payload={}, max_output_tokens=20), max_output_bytes=1000)
    path = str(tmp_path / 'b')
    async with await asyncio.start_unix_server(broker.handle, path=path):
        for _ in range(2):
            reader, writer = await asyncio.open_unix_connection(path)
            payload = json.dumps({'model': 'chosen', 'input': []}).encode()
            writer.write(struct.pack('!I', len(payload)) + payload)
            await writer.drain()
            length = struct.unpack('!I', await reader.readexactly(4))[0]
            await reader.readexactly(length)
            writer.close()
            await writer.wait_closed()
    assert broker.failure.diagnostic.outcome == 'failed:provider:req:1'


def test_all_safe_diagnostics_fit_existing_outcome_column():
    from shared.schemas.discovery_diagnostic import CODES, STAGES, DiscoveryDiagnostic
    for stage in STAGES:
        for code in CODES:
            assert len(DiscoveryDiagnostic(stage, code, 16).outcome) <= 30


@pytest.mark.parametrize('variant,stage', [
    ('missing_status', 'final_absent'), ('bad_status', 'final_status'),
    ('role', 'final_role'), ('parts', 'final_parts'),
    ('fence', 'final_fence'), ('prose', 'final_json'),
    ('scalar', 'final_object'), ('nonfinite', 'final_number'),
    ('commentary', 'final_phase'), ('mixed', 'final_mixed'),
    ('empty', 'final_empty'), ('oversize', 'final_size'),
])
async def test_final_diagnostic_classifies_only_format_without_accepting_invalid_output(variant, stage):
    from connectors.codex_agent.client import AgentBroker
    from shared.schemas.model_invocation import ModelRequest

    message = {'type': 'message', 'role': 'assistant', 'status': 'completed',
               'content': [{'type': 'output_text', 'text': '{"ok":true}'}]}
    outputs = [message]
    if variant == 'missing_status':
        message.pop('status')
    elif variant == 'bad_status':
        message['status'] = 'in_progress'
    elif variant == 'role':
        message['role'] = 'user'
    elif variant == 'parts':
        message['content'] = [{'type': 'refusal', 'refusal': 'PRIVATE'}]
    elif variant in ('fence', 'prose', 'scalar', 'nonfinite', 'empty', 'oversize'):
        message['content'][0]['text'] = {
            'fence': '```json\n{"PRIVATE":true}\n```', 'prose': 'PRIVATE prose',
            'scalar': '["PRIVATE"]', 'nonfinite': '{"PRIVATE":NaN}',
            'empty': '', 'oversize': '{"PRIVATE":"' + 'x' * 1000 + '"}',
        }[variant]
    else:
        message['phase'] = 'commentary'
        message['content'][0]['text'] = 'PRIVATE intermediate explanation'
        if variant == 'mixed':
            outputs.append({'type': 'message', 'role': 'assistant', 'status': 'completed',
                            'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': '{"ok":true}'}]})

    class Delegate:
        async def round(self, body):
            return {'model': 'chosen', 'status': 'completed', 'output': outputs,
                    'usage': {'input_tokens': 1, 'output_tokens': 1}}

    broker = AgentBroker(Delegate(), ModelRequest(model='chosen', system_prompt='JSON',
                         payload={}, max_output_tokens=20), max_output_bytes=500)
    broker._read_context = True
    with pytest.raises(DeepSeekFailure) as raised:
        await broker.exchange({'model': 'chosen', 'input': []})
    assert broker.response is None
    assert raised.value.diagnostic.outcome == f'failed:{stage}:res:1'
    assert 'PRIVATE' not in str(raised.value.diagnostic)


def test_discovery_requires_json_output_without_disabling_tools_or_changing_other_tasks():
    raw = {'model': 'chosen', 'input': [], 'text': {'format': {'type': 'text'}},
           'tools': [{'type': 'function', 'name': 'mcp__tradeos__read_task_context'}]}
    body = checked_request(raw, 'chosen', 128, discovery=True)
    assert body['text'] == {'format': {'type': 'json_object'}}
    assert {t['name'] for t in body['tools']} == {
        'mcp__tradeos__read_task_context', 'mcp__tradeos__source_search',
        'mcp__tradeos__read_public_page', 'mcp__tradeos__browse_public_page'}
    assert body['max_output_tokens'] == 128
    assert 'text' not in checked_request(raw, 'chosen', 128)


@pytest.mark.parametrize('discovery', [True, False])
async def test_responses_transport_delivers_json_constraint_and_keeps_tool_round(discovery):
    from types import SimpleNamespace

    from connectors.codex_agent.client import AgentTransport

    calls = []
    async def create(**params):
        calls.append(params)
        tool = {'type': 'function_call', 'id': 'f1', 'call_id': 'c1',
                'name': 'mcp__tradeos__read_task_context', 'arguments': '{}'}
        return SimpleNamespace(model_dump=lambda: {'status': 'completed', 'output': [tool]})

    sdk = SimpleNamespace(responses=SimpleNamespace(create=create))
    resolver = SimpleNamespace(resolve=lambda ref: 'synthetic-test-key')
    transport = AgentTransport('synthetic', resolver, base_url='https://api.deepseek.com',
                               protocol='responses', timeout_seconds=10,
                               client_factory=lambda key, timeout: sdk)
    body = checked_request({'model': 'chosen', 'input': [], 'tools': [
        {'type': 'function', 'name': 'mcp__tradeos__read_task_context'}]},
        'chosen', 128, discovery=discovery)
    response = await transport.round(body)
    assert response['output'][0]['type'] == 'function_call'
    assert len(calls) == 1
    assert bool(calls[0]['tools'])
    if discovery:
        assert calls[0]['text'] == {'format': {'type': 'json_object'}}
    else:
        assert 'text' not in calls[0]
