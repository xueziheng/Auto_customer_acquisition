"""完整报价原件容量边界；不放宽解析资源或原件授权。"""
from pathlib import Path

import pytest

from apps.api.composition.enterprise_knowledge import EnterpriseKnowledgeApplication
from connectors.files._knowledge_parser import MAX_FILE
from connectors.files.knowledge import KnowledgeParseFailure, KnowledgeParseLimits
from domains.products.schemas import KnowledgeActor
from infra.standalone.knowledge_settings import KnowledgeSettings
from shared.errors import ValidationError
from tests.unit.test_enterprise_knowledge_api import (
    EMPLOYEE,
    TENANT,
    Artifacts,
    Knowledge,
)

LIMIT = 25 * 1024 * 1024

@pytest.mark.parametrize("size", [19531502, LIMIT, LIMIT + 1])
async def test_original_upload_boundary(size):
    calls = []
    artifacts = Artifacts(calls)
    service = Knowledge(calls, artifacts)
    app = EnterpriseKnowledgeApplication(service, artifacts, maximum_upload_bytes=LIMIT)
    actor = KnowledgeActor(tenant_id=TENANT, employee_id=EMPLOYEE)
    content = b"x" * size
    if size > LIMIT:
        with pytest.raises(ValidationError):
            await app.upload(TENANT, actor, uploaded_by=None, filename="spec.txt", mime_type="text/plain", content=content, idempotency_key="boundary")
        assert "put" not in calls
    else:
        result = await app.upload(TENANT, actor, uploaded_by=None, filename="spec.txt", mime_type="text/plain", content=content, idempotency_key="boundary")
        assert result.source.size_bytes == size
        assert artifacts.content == content

def test_explicit_configuration_and_parser_share_hard_ceiling():
    fields = {"enabled": True, "root": Path("/knowledge"), "codex_binary": Path("/bin/codex")}
    assert KnowledgeSettings(**fields).max_upload_bytes == 10 * 1024 * 1024
    assert KnowledgeSettings(**fields, max_upload_bytes=LIMIT).max_upload_bytes == LIMIT
    with pytest.raises(ValueError):
        KnowledgeSettings(**fields, max_upload_bytes=LIMIT + 1)
    KnowledgeParseLimits(max_bytes=LIMIT).validate()
    with pytest.raises(KnowledgeParseFailure):
        KnowledgeParseLimits(max_bytes=LIMIT + 1).validate()
    assert MAX_FILE == LIMIT
    assert KnowledgeParseLimits().max_chars == 60000
    assert KnowledgeParseLimits().timeout_seconds == 30
