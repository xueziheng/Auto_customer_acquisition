"""HTTP 只接受当前身份、原键和空命令。"""

from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient

from tests.unit.test_inbox_api import MESSAGE, _headers, _inbox_app
from workflows.reply_qualification.retry import ReplyRetryView


class Retry:
    def __init__(self):
        self.calls = []

    async def read(self, tenant, message, actor):
        return ReplyRetryView(
            message_id=message,
            state="eligible",
            reason="not_dispatched",
            can_retry=True,
        )

    async def resume(self, tenant, message, actor, key):
        return await self.request(tenant, message, actor, key)

    async def request(self, tenant, message, actor, key):
        self.calls.append((tenant, message, actor, key))
        return ReplyRetryView(
            message_id=message, state="queued", reason="manual_recovery"
        )


@pytest.mark.parametrize(
    "case,status",
    [
        ("valid", 200),
        ("no-key", 400),
        ("duplicate-key", 400),
        ("extra-body", 400),
        ("extra-query", 400),
        ("role", 403),
    ],
)
@pytest.mark.parametrize("suffix", ["", "/resume"])
async def test_retry_http_contract(case, status, suffix):
    app, _ = _inbox_app(role="product" if case == "role" else "boss")
    retry = Retry()
    app.state.dependencies = replace(app.state.dependencies, reply_retry=retry)
    headers = list(_headers().items())
    if case != "no-key":
        headers.append(("Idempotency-Key", "retry-command-1"))
    if case == "duplicate-key":
        headers.append(("Idempotency-Key", "second-key"))
    path = f"/inbox/messages/{MESSAGE}/retry-classification{suffix}" + (
        "?sequence=99" if case == "extra-query" else ""
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        result = await client.post(
            path, headers=headers, json={"sequence": 9} if case == "extra-body" else {}
        )
    assert result.status_code == status
    assert len(retry.calls) == (case == "valid")
    if retry.calls:
        assert (
            retry.calls[0][0] == "tenant-a"
            and retry.calls[0][2].employee_id == "emp-inbox"
        )


@pytest.mark.parametrize(
    "body,status",
    [
        ({"expected_sequence": 1, "acknowledge_cost": True}, 200),
        ({"expected_sequence": "1", "acknowledge_cost": True}, 400),
        ({"expected_sequence": 1, "acknowledge_cost": "yes"}, 400),
        ({"expected_sequence": -1, "acknowledge_cost": True}, 400),
        ({"expected_sequence": 1, "acknowledge_cost": True, "tenant_id": "other"}, 400),
    ],
)
async def test_failed_retry_http_strict_observed_attempt(body, status):
    app, _ = _inbox_app(role="boss")

    class FailedRetry(Retry):
        async def retry_failed(self, tenant, message, actor, key, command):
            self.calls.append(command)
            return ReplyRetryView(
                message_id=message, state="queued", reason="manual_recovery"
            )

    retry = FailedRetry()
    app.state.dependencies = replace(app.state.dependencies, reply_retry=retry)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        result = await client.post(
            f"/inbox/messages/{MESSAGE}/retry-classification/failed",
            headers={**_headers(), "Idempotency-Key": "failed-retry-command"},
            json=body,
        )
    assert result.status_code == status
    assert len(retry.calls) == int(status == 200)
