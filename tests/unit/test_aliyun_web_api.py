"""阿里邮箱连接只接受真实本人会话，错误不得回显密码。"""
import asyncio

from httpx import ASGITransport, AsyncClient

from tests.unit.test_api_app import _ApiClient, _app, _employee


def test_unconfigured_aliyun_is_no_store():
    employee = _employee()
    app, _, _, _, _ = _app(employee)
    response = _ApiClient(app).get("/inbox/aliyun/status", headers={
        "X-Tenant-Id": str(employee.tenant_id), "X-Employee-Id": str(employee.employee_id),
    })
    assert response.status_code == 200
    assert response.json()["configured"] is False
    assert response.headers["Cache-Control"] == "no-store"


def test_aliyun_rejects_development_identity_and_redacts_validation_errors():
    from apps.api.composition.aliyun_web import AliyunWebService
    employee = _employee()
    app, _, _, _, _ = _app(employee)
    app.state.aliyun_web = object.__new__(AliyunWebService)
    headers = {"X-Tenant-Id": str(employee.tenant_id), "X-Employee-Id": str(employee.employee_id)}
    response = _ApiClient(app).get("/inbox/aliyun/status", headers=headers)
    assert response.status_code == 401
    async def invalid_request():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            return await client.post("/inbox/aliyun/connect", headers=headers, json={
                "email": "invalid", "password": "synthetic-should-not-return", "confirm_connect": True,
            })
    response = asyncio.run(invalid_request())
    assert response.status_code == 400
    assert "synthetic-should-not-return" not in response.text
