"""Tavily 固定免费能力与供应商无关搜索契约验收。"""

from __future__ import annotations

import json

import pytest

from connectors.search_contracts import SearchCostStatus
from connectors.tavily.client import TavilySearchConnector
from connectors.tavily.transport import (
    TavilyAuthRequiredError,
    TavilyHttpResponse,
    TavilyRateLimitedError,
    TavilySearchApiTransport,
    TavilyTransientError,
)
from shared.errors import ValidationError


class _Resolver:
    def __init__(self, value: str = "tvly-secret-value-not-for-logs") -> None:
        self.value = value

    def resolve(self, secret_ref: str) -> str:
        assert secret_ref == "TAVILY_API_KEY_REF"
        return self.value


class _Transport:
    def __init__(self) -> None:
        self.search_request: tuple[str, str, int, str] | None = None
        self.usage_key: str | None = None
        self.result: dict[str, object] = {
            "title": "Acme expansion",
            "url": "https://example.com/news",
            "content": "New factory",
            "score": 0.99,
        }

    async def search(
        self, query: str, country: str, limit: int, *, api_key: str
    ) -> TavilyHttpResponse:
        self.search_request = (query, country, limit, api_key)
        return TavilyHttpResponse(
            status_code=200,
            payload={"results": [self.result]},
        )

    async def usage(self, *, api_key: str) -> TavilyHttpResponse:
        self.usage_key = api_key
        return TavilyHttpResponse(
            status_code=200,
            payload={
                "key": {"usage": 2, "limit": 100},
                "account": {
                    "current_plan": "Researcher",
                    "plan_usage": 2,
                    "plan_limit": 100,
                    "paygo_usage": 0,
                    "paygo_limit": 0,
                },
            },
        )


class _UrlValidator:
    async def validate_url(self, url: str) -> str:
        return url

    async def fetch(self, url: str) -> object:
        del url
        raise AssertionError("搜索结果规范化不应读取页面")


async def test_connector_normalizes_results_and_lazily_resolves_secret() -> None:
    """若 connector 透出 provider score 或构造时取密钥，发现证据会混入伪置信度。"""
    transport = _Transport()
    connector = TavilySearchConnector(transport, _UrlValidator())
    assert transport.search_request is None

    await connector.configure(_Resolver())
    results = await connector.search("hinge importer", country="US", limit=1)

    assert results[0].title == "Acme expansion"
    assert results[0].url == "https://example.com/news"
    assert results[0].description == "New factory"
    assert not hasattr(results[0], "score")
    assert repr(connector) == "TavilySearchConnector()"
    usage = await connector.usage()
    assert usage.paygo_enabled is False
    assert usage.cost_status is SearchCostStatus.FREE
    assert usage.included_credits_free is True


@pytest.mark.parametrize(
    ("title", "content", "expected_title", "expected_content"),
    [
        ("  Acme\n\r\t expansion  ", " New\n\n factory\t in\r US ", "Acme expansion", "New factory in US"),
        ("T" * 700, "C" * 2200, "T" * 500, "C" * 2000),
        ("Acme", " \t\r\n ", "Acme", ""),
    ],
    ids=["fold_whitespace", "bound_text", "empty_description"],
)
async def test_search_results_normalize_bounded_display_text(
    title: str, content: str, expected_title: str, expected_content: str,
) -> None:
    """供应商的展示空白与长摘要不得使整批有效网页定位结果报错。"""
    transport = _Transport()
    transport.result.update(title=title, content=content)
    connector = TavilySearchConnector(transport, _UrlValidator())
    await connector.configure(_Resolver())

    results = await connector.search("hinge importer", country="US", limit=1)

    assert results[0].title == expected_title
    assert results[0].description == expected_content


@pytest.mark.parametrize("field", ["title", "content"])
@pytest.mark.parametrize("value", [None, 5, {"text": "Acme"}])
async def test_search_display_normalization_does_not_coerce_nontext(
    field: str, value: object,
) -> None:
    """将异常结构直接转字符串会让伪造摘要混入搜索结果。"""
    transport = _Transport()
    transport.result[field] = value
    connector = TavilySearchConnector(transport, _UrlValidator())
    await connector.configure(_Resolver())

    with pytest.raises(ValidationError):
        await connector.search("hinge importer", country="US", limit=1)


@pytest.mark.parametrize("field", ["title", "content"])
@pytest.mark.parametrize("prefix_length", [0, 2500])
async def test_search_rejects_hidden_controls_before_truncation_without_leaking_content(
    field: str, prefix_length: int,
) -> None:
    """先截断后检查会漏掉尾部控制字符，且异常不得带回供应商正文。"""
    canary = "provider-private-canary"
    transport = _Transport()
    transport.result[field] = "A" * prefix_length + "\0" + canary
    connector = TavilySearchConnector(transport, _UrlValidator())
    await connector.configure(_Resolver())

    with pytest.raises(ValidationError) as failure:
        await connector.search("hinge importer", country="US", limit=1)

    assert canary not in str(failure.value)
    assert canary not in repr(failure.value)


async def test_search_does_not_truncate_url_before_security_validation() -> None:
    """摘要清理不能改写 URL；安全读取器必须看见供应商返回的完整地址。"""
    urls: list[str] = []

    class UrlValidator(_UrlValidator):
        async def validate_url(self, url: str) -> str:
            urls.append(url)
            return url

    transport = _Transport()
    raw_url = "https://example.com/" + "x" * 2500
    transport.result["url"] = raw_url
    connector = TavilySearchConnector(transport, UrlValidator())
    await connector.configure(_Resolver())

    with pytest.raises(ValidationError):
        await connector.search("hinge importer", country="US", limit=1)

    assert urls == [raw_url]


def test_transport_uses_only_fixed_basic_no_extras_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """若调用方能打开高级或答案选项，就可能在 gateway 检查前产生收费。"""
    request: dict[str, object] = {}

    class _Response:
        status = 200

        def getheader(self, name: str) -> None:
            del name

        def read(self, maximum: int) -> bytes:
            del maximum
            return b'{"results": []}'

    class _Connection:
        def __init__(self, host: str, port: int, **kwargs: object) -> None:
            request["host"] = host
            request["port"] = port
            request["kwargs"] = kwargs

        def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
            request["method"] = method
            request["path"] = path
            request["body"] = json.loads(body)
            request["headers"] = headers

        def getresponse(self) -> _Response:
            return _Response()

        def close(self) -> None:
            return None

    monkeypatch.setattr("connectors.tavily.transport.http.client.HTTPSConnection", _Connection)
    response = TavilySearchApiTransport()._search_sync(
        "hinge importer", "US", 1, "tvly-secret-value-not-for-logs"
    )

    assert response.payload == {"results": []}
    assert request["host"] == "api.tavily.com"
    assert request["path"] == "/search"
    request_body = request["body"]
    assert isinstance(request_body, dict)
    assert request_body["search_depth"] == "basic"
    assert request_body["auto_parameters"] is False
    assert request_body["include_answer"] is False
    assert request_body["include_raw_content"] is False
    assert request_body["include_images"] is False
    assert request_body["include_usage"] is False


async def test_usage_decoding_rejects_boolean_counts_and_missing_counts() -> None:
    """若把 bool/缺失解作整数零，额度 gate 会放行未知的可收费状态。"""
    connector = TavilySearchConnector(_Transport(), _UrlValidator())
    await connector.configure(_Resolver())
    transport = connector._transport
    assert isinstance(transport, _Transport)
    async def invalid_usage(*, api_key: str) -> TavilyHttpResponse:
        del api_key
        return TavilyHttpResponse(
            status_code=200,
            payload={
                "key": {"usage": 2, "limit": 100},
                "account": {
                    "current_plan": "Researcher",
                    "plan_usage": True,
                    "plan_limit": 100,
                    "paygo_usage": 0,
                    "paygo_limit": 0,
                },
            },
        )

    transport.usage = invalid_usage  # type: ignore[method-assign]

    with pytest.raises(ValidationError):
        await connector.usage()


async def test_usage_missing_fields_remain_unknown_instead_of_zero() -> None:
    """若缺失用量被补为零，额度门禁会把未证明的额度当作可用。"""
    transport = _Transport()

    async def missing_usage(*, api_key: str) -> TavilyHttpResponse:
        del api_key
        return TavilyHttpResponse(
            status_code=200,
            payload={"account": {"current_plan": "Researcher"}},
        )

    transport.usage = missing_usage  # type: ignore[method-assign]
    connector = TavilySearchConnector(transport, _UrlValidator())
    await connector.configure(_Resolver())

    usage = await connector.usage()

    assert usage.limit is None
    assert usage.used is None
    assert usage.paygo_enabled is None
    assert usage.cost_status is SearchCostStatus.UNKNOWN
    assert usage.included_credits_free is False


async def test_researcher_null_paygo_limit_preserves_unknown_cost() -> None:
    """真实免费账号可返回 null；不能把网页套餐名称当作 API 按量关闭证明。"""
    transport = _Transport()

    async def null_paygo_usage(*, api_key: str) -> TavilyHttpResponse:
        del api_key
        return TavilyHttpResponse(
            status_code=200,
            payload={
                "account": {
                    "current_plan": "Researcher",
                    "plan_usage": 0,
                    "plan_limit": 1000,
                    "paygo_usage": 0,
                    "paygo_limit": None,
                }
            },
        )

    transport.usage = null_paygo_usage  # type: ignore[method-assign]
    connector = TavilySearchConnector(transport, _UrlValidator())
    await connector.configure(_Resolver())

    usage = await connector.usage()

    assert usage.plan == "Researcher"
    assert usage.limit == 1000
    assert usage.used == 0
    assert usage.paygo_enabled is None
    assert usage.cost_status is SearchCostStatus.UNKNOWN
    assert usage.included_credits_free is True


async def test_usage_decoding_recognizes_only_exact_paid_plan() -> None:
    """若把未知 plan alias 当作已知付费/免费，会在套餐更名后误放行或误阻断。"""
    transport = _Transport()

    async def paid_usage(*, api_key: str) -> TavilyHttpResponse:
        del api_key
        return TavilyHttpResponse(
            status_code=200,
            payload={
                "account": {
                    "current_plan": "Project",
                    "plan_usage": 500,
                    "plan_limit": 15_000,
                    "paygo_usage": 0,
                    "paygo_limit": 0,
                }
            },
        )

    transport.usage = paid_usage  # type: ignore[method-assign]
    connector = TavilySearchConnector(transport, _UrlValidator())
    await connector.configure(_Resolver())

    usage = await connector.usage()
    assert usage.cost_status is SearchCostStatus.PAID
    assert usage.included_credits_free is False


@pytest.mark.parametrize(
    ("changes", "missing", "expected"),
    [
        ({}, None, True),
        ({"paygo_limit": 0}, None, True),
        ({"plan_limit": 0}, None, True),
        ({"plan_usage": 1001}, None, True),
        ({"current_plan": "researcher"}, None, False),
        ({"current_plan": "Project"}, None, False),
        ({"current_plan": "Unknown"}, None, False),
        ({"paygo_usage": 1}, None, False),
        ({"paygo_limit": 1}, None, False),
        ({"plan_limit": None}, None, False),
        ({"plan_usage": None}, None, False),
        ({"paygo_usage": None}, None, False),
        ({}, "current_plan", False),
        ({}, "plan_limit", False),
        ({}, "plan_usage", False),
        ({}, "paygo_usage", False),
        ({}, "paygo_limit", False),
    ],
)
async def test_included_free_credit_fact_requires_complete_exact_provider_evidence(
    changes: dict[str, object], missing: str | None, expected: bool,
) -> None:
    """把缺失键与显式 null 合并，或按别名猜套餐，都会错误放行未知额度。"""
    account: dict[str, object] = {
        "current_plan": "Researcher", "plan_limit": 1000, "plan_usage": 0,
        "paygo_usage": 0, "paygo_limit": None,
    }
    account.update(changes)
    if missing is not None:
        del account[missing]
    transport = _Transport()

    async def usage_response(*, api_key: str) -> TavilyHttpResponse:
        del api_key
        return TavilyHttpResponse(200, {"account": account})

    transport.usage = usage_response  # type: ignore[method-assign]
    connector = TavilySearchConnector(transport, _UrlValidator())
    await connector.configure(_Resolver())

    usage = await connector.usage()

    assert usage.included_credits_free is expected
    if account.get("paygo_limit") is None:
        assert usage.paygo_enabled is None


@pytest.mark.parametrize("field", ["plan_limit", "plan_usage", "paygo_usage", "paygo_limit"])
@pytest.mark.parametrize("value", [True, False, -1, "0", 0.0])
async def test_free_credit_evidence_rejects_noninteger_or_negative_counts(
    field: str, value: object,
) -> None:
    """零值必须来自真实整数，不能将 bool、浮点或字符串转成免费证明。"""
    account: dict[str, object] = {
        "current_plan": "Researcher", "plan_limit": 1000, "plan_usage": 0,
        "paygo_usage": 0, "paygo_limit": None,
    }
    account[field] = value
    transport = _Transport()

    async def usage_response(*, api_key: str) -> TavilyHttpResponse:
        del api_key
        return TavilyHttpResponse(200, {"account": account})

    transport.usage = usage_response  # type: ignore[method-assign]
    connector = TavilySearchConnector(transport, _UrlValidator())
    await connector.configure(_Resolver())

    with pytest.raises(ValidationError):
        await connector.usage()


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, TavilyAuthRequiredError),
        (429, TavilyRateLimitedError),
        (500, TavilyTransientError),
    ],
)
def test_transport_classifies_safe_errors_without_secret(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    error_type: type[Exception],
) -> None:
    """若 transport 原样传播鉴权/超时异常，日志和 repr 可能泄漏 API key。"""
    class _Response:
        def getheader(self, name: str) -> None:
            del name

        def read(self, maximum: int) -> bytes:
            del maximum
            return b"{}"

    class _Connection:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def request(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def getresponse(self) -> _Response:
            response = _Response()
            response.status = status
            return response

        def close(self) -> None:
            return None

    monkeypatch.setattr("connectors.tavily.transport.http.client.HTTPSConnection", _Connection)
    secret = "tvly-secret-value-not-for-logs"
    transport = TavilySearchApiTransport()

    with pytest.raises(error_type) as caught:
        transport._search_sync("hinge importer", "US", 1, secret)

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert secret not in repr(transport)


def test_transport_classifies_timeout_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """若超时异常穿透，调用方会误分类重试且可能记录请求上下文。"""
    class _Connection:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def request(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise TimeoutError

        def close(self) -> None:
            return None

    monkeypatch.setattr("connectors.tavily.transport.http.client.HTTPSConnection", _Connection)
    secret = "tvly-secret-value-not-for-logs"

    with pytest.raises(TavilyTransientError) as caught:
        TavilySearchApiTransport()._search_sync("hinge importer", "US", 1, secret)

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
