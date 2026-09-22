"""Web 冷启动窄命令必须显式确认，保留当前 boss 权限与原域行为。"""
from dataclasses import replace

from domains.sending_identity.schemas import IdentityState
from tests.unit.test_sending_identity_api import (
    BOSS,
    IDENTITY,
    SALES,
    TENANT,
    _app,
    _Client,
    _employee,
    _headers,
)


def test_management_lists_created_identity_without_campaign_filter():
    app, sending = _app(_employee(BOSS, "boss"))

    async def management(tenant_id, *, limit, actor):
        assert tenant_id == TENANT and limit == 8 and actor.role == "boss"
        return [replace((await sending.get(TENANT, IDENTITY, actor=actor)), state=IdentityState.CREATED)]

    sending.list_for_management = management
    result = _Client(app).get("/crm/sending-identities/management?limit=8", headers=_headers(BOSS))
    assert result.status_code == 200
    assert result.json()[0]["state"] == "created"


def test_registration_confirmation_and_trusted_identity():
    app, sending = _app(_employee(BOSS, "boss"))
    calls = []

    async def register(tenant_id, request, *, actor):
        calls.append((tenant_id, request, actor))
        return IDENTITY

    sending.register = register
    client = _Client(app)
    payload = {"address": "sales@cold.example.test", "domain": "cold.example.test", "role": "cold_outreach", "confirmed": True}
    result = client.post("/crm/sending-identities", headers=_headers(BOSS), json=payload)
    assert result.status_code == 200
    assert result.json()["identity_id"] == str(IDENTITY)
    assert calls[0][0] == TENANT and calls[0][2].actor_id == str(BOSS)
    for invalid in ({**payload, "confirmed": False}, {**payload, "actor": "boss"}, {**payload, "state": "active"}):
        assert client.post("/crm/sending-identities", headers=_headers(BOSS), json=invalid).status_code == 400
    assert len(calls) == 1


def test_warmup_confirmation_preserves_domain_command_and_rejects_bypass():
    app, sending = _app(_employee(BOSS, "boss"))
    calls = []

    async def warmup(tenant_id, identity_id, target_daily_volume, *, actor):
        calls.append((tenant_id, identity_id, target_daily_volume, actor.role))

    sending.start_warmup = warmup
    client = _Client(app)
    path = f"/crm/sending-identities/{IDENTITY}/warmup"
    body = {"target_daily_volume": 15, "confirmed": True}
    assert client.post(path, headers=_headers(BOSS), json=body).status_code == 200
    assert calls == [(TENANT, IDENTITY, 15, "boss")]
    for invalid in ({**body, "confirmed": False}, {**body, "started_on": "2020-01-01"}, {**body, "target_daily_volume": 4}, {**body, "target_daily_volume": True}):
        assert client.post(path, headers=_headers(BOSS), json=invalid).status_code == 400
    assert len(calls) == 1


def test_management_and_commands_deny_sales():
    app, _ = _app(_employee(SALES, "sales"))
    client = _Client(app)
    assert client.get("/crm/sending-identities/management", headers=_headers(SALES)).status_code == 403
    assert client.post("/crm/sending-identities", headers=_headers(SALES), json={"address": "sales@cold.example.test", "domain": "cold.example.test", "role": "cold_outreach", "confirmed": True}).status_code == 403
    assert client.post(f"/crm/sending-identities/{IDENTITY}/warmup", headers=_headers(SALES), json={"confirmed": True, "target_daily_volume": 5}).status_code == 403


def test_inbound_raw_openapi_matches_actual_binary_content():
    app, _ = _app(_employee(BOSS, "boss"))
    operation = app.openapi()["paths"]["/email-inbound/reviews/{review_id}/raw"]["get"]
    assert operation["responses"]["200"]["content"]["application/octet-stream"]["schema"] == {"type": "string", "format": "binary"}
    assert all(str(status) in operation["responses"] for status in (400, 403, 404, 409, 503))


async def test_management_domain_reads_created_and_checks_boss_before_io():
    import pytest

    from shared.errors import PermissionDenied
    from tests.unit.test_sending_identity_service import (
        _TENANT,
        _boss,
        _build,
        _manager,
        _seed,
    )

    service, factory, _audit, order = _build()
    row = _seed(factory, state=IdentityState.CREATED)
    async def management(tenant_id, limit):
        order.append("management_sql")
        return [row]
    # Each UoW owns a repository instance; attach to the test repository type.
    from tests.unit.test_sending_identity_service import _Identities
    original = getattr(_Identities, "list_for_management", None)
    _Identities.list_for_management = lambda self, tenant_id, limit: management(tenant_id, limit)
    try:
        assert (await service.list_for_management(_TENANT, limit=2, actor=_boss()))[0].state is IdentityState.CREATED
        order.clear()
        with pytest.raises(PermissionDenied):
            await service.list_for_management(_TENANT, limit=2, actor=_manager(identity_ids=frozenset({row.identity_id})))
        assert "management_sql" not in order
        assert _audit.records[-1]["rule"] == "deny:authorization"
    finally:
        if original is None:
            del _Identities.list_for_management
        else:
            _Identities.list_for_management = original


def test_confirmation_must_be_boolean_true_not_numeric_assertion():
    app, _ = _app(_employee(BOSS, "boss"))
    client = _Client(app)
    result = client.post("/crm/sending-identities", headers=_headers(BOSS), json={"address": "sales@cold.example.test", "domain": "cold.example.test", "role": "cold_outreach", "confirmed": 1})
    assert result.status_code == 400
