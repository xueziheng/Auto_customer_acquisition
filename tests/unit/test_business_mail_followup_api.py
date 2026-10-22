"""HTTP身份只取认证依赖，错误不得反射草稿正文。"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.dependencies import get_request_identity
from apps.api.routers.business_mail_followup import router
from domains.conversations.business_mail_followup import (
    BusinessMailFollowupConflict,
    BusinessMailFollowupService,
    BusinessMailFollowupView,
)


class Repository:
    def __init__(self):
        self.actor = None
        self.conflict = False

    async def get(self, actor, message_id):
        self.actor = actor
        return BusinessMailFollowupView(
            message_id=message_id,
            revision=0,
            owner_id=actor.employee_id,
            status="pending",
            draft=None,
            history=[],
        )

    async def apply(self, actor, message_id, command, *, idempotency_key):
        if self.conflict:
            raise BusinessMailFollowupConflict()
        return await self.get(actor, message_id)


@pytest.fixture
def client():
    app = FastAPI()
    repo = Repository()
    app.state.business_mail_followup = BusinessMailFollowupService(repo)
    app.include_router(router)
    app.dependency_overrides[get_request_identity] = lambda: SimpleNamespace(
        tenant_id="tn-test",
        employee=SimpleNamespace(employee_id="emp-test", user_id="usr-test"),
    )
    return TestClient(app), repo


def test_get_server_identity_and_headers(client):
    http, repo = client
    response = http.get("/inbox/business-mail/message/followup")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert repo.actor.employee_id == "emp-test"


def test_extra_identity_rejected(client):
    http, _ = client
    response = http.post(
        "/inbox/business-mail/message/reply-draft",
        headers={"Idempotency-Key": "request-123"},
        json={
            "expected_revision": 0,
            "subject": "Hi",
            "body_text": "Hello",
            "actor_id": "forged",
        },
    )
    assert response.status_code == 400


def test_conflict_fixed_and_private(client):
    http, repo = client
    repo.conflict = True
    response = http.post(
        "/inbox/business-mail/message/handling",
        headers={"Idempotency-Key": "request-123"},
        json={"expected_revision": 0, "status": "completed", "note": "处理"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "邮件处理记录已变化，请刷新后重新确认"
    assert response.headers["cache-control"] == "private, no-store"
