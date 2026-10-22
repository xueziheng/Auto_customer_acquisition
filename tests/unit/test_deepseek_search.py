"""真实搜索协议、未知结果与凭证边界，不访问供应商。"""
import importlib.util

import pytest

from shared.errors import ValidationError


def test_search_connector_exists():
    assert importlib.util.find_spec("connectors.deepseek_search") is not None


@pytest.mark.parametrize("base", ["https://api.deepseek.com", "https://api.deepseek.com/v1"])
async def test_official_request_shape_and_unicode(monkeypatch, base):
    from connectors.deepseek_search.client import DeepSeekSearchConnector
    from connectors.deepseek_search.transport import DeepSeekSearchApiTransport
    calls = []
    class Response:
        status = 200
        def read(self, limit):
            return b'{"results":[{"title":"Company","url":"https://example.com/","snippet":"Public","rerank_score":2.4}]}'
    class Connection:
        def __init__(self, host, port, **kwargs):
            assert (host, port) == ("api.deepseek.com", 443)
        def request(self, method, path, body, headers):
            import json
            calls.append((method, path, json.loads(body)))
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr("http.client.HTTPSConnection", Connection)
    class Resolver:
        def resolve(self, ref):
            assert ref == "MODEL_API_KEY"
            return "test-not-real-key-12345"
    connector = DeepSeekSearchConnector(DeepSeekSearchApiTransport(), Resolver(), base_url=base)
    results = await connector.search("中" * 128, country="CN", limit=3)
    assert results[0].url == "https://example.com/"
    assert calls == [("POST", "/v1/tools/search", {"query": "中" * 128, "count": 3, "need_content": False})]
    with pytest.raises(ValidationError):
        await connector.search("中" * 129, country="CN", limit=3)
    assert len(calls) == 1


async def test_wrong_endpoint_never_resolves():
    from connectors.deepseek_search.client import DeepSeekSearchConnector
    class Resolver:
        def resolve(self, ref):
            pytest.fail("wrong endpoint resolved credential")
    with pytest.raises(ValidationError):
        DeepSeekSearchConnector(object(), Resolver(), base_url="https://compatible.example/v1")


@pytest.mark.parametrize("status", [301, 401, 402, 429, 500, 504])
async def test_no_retry_or_raw_error(monkeypatch, status):
    from connectors.deepseek_search.transport import DeepSeekSearchApiTransport
    calls = []
    class Response:
        def read(self, limit):
            return b'{"error":{"message":"secret-marker"}}'
    Response.status = status
    class Connection:
        def __init__(self, *args, **kwargs):
            pass
        def request(self, *args, **kwargs):
            calls.append(1)
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr("http.client.HTTPSConnection", Connection)
    with pytest.raises(Exception) as error:
        await DeepSeekSearchApiTransport().search("steel Kenya", "KE", 1, api_key="test-key")
    assert "secret-marker" not in str(error.value)
    assert len(calls) == 1
