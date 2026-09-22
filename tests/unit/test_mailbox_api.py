"""邮箱 HTTP 只使用当前登录员工，不接受客户端自报 owner。"""

from dataclasses import replace

from tests.unit.test_api_app import _ApiClient, _app, _employee


def test_mailbox_api_identity_and_unconfigured_status():
    employee = _employee()
    app, _, _, dependencies, _ = _app(employee)
    headers = {
        "X-Tenant-Id": str(employee.tenant_id),
        "X-Employee-Id": str(employee.employee_id),
    }
    assert _ApiClient(app).get("/inbox/mailboxes", headers=headers).status_code == 503
    actors = []

    class Service:
        async def mailboxes(self, actor):
            actors.append(actor)
            return []

    app.state.dependencies = replace(dependencies, mailbox=Service())
    response = _ApiClient(app).get("/inbox/mailboxes", headers=headers)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert actors[0].tenant_id == employee.tenant_id
    assert actors[0].employee_id == employee.employee_id
    assert _ApiClient(app).get("/inbox/mailboxes").status_code in {400, 401}
