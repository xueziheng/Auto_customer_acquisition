"""云端邮箱连接未装配时仍有明确状态，不能让 UI 误报为已连接。"""
from tests.unit.test_api_app import _ApiClient, _app, _employee


def test_gmail_web_status_is_explicitly_unconfigured():
    employee = _employee()
    app, _, _, _, _ = _app(employee)
    response = _ApiClient(app).get("/inbox/gmail/status", headers={
        "X-Tenant-Id": str(employee.tenant_id),
        "X-Employee-Id": str(employee.employee_id),
    })
    assert response.status_code == 200
    assert response.json() == {
        "configured": False, "email": None, "mailbox_id": None, "can_test": False,
    }
    assert response.headers["Cache-Control"] == "no-store"

def test_configured_gmail_rejects_development_header_identity():
    from apps.api.composition.gmail_web import GmailWebService
    employee = _employee()
    app, _, _, _, _ = _app(employee)
    app.state.gmail_web = object.__new__(GmailWebService)
    response = _ApiClient(app).get("/inbox/gmail/status", headers={
        "X-Tenant-Id": str(employee.tenant_id),
        "X-Employee-Id": str(employee.employee_id),
    })
    assert response.status_code == 401
