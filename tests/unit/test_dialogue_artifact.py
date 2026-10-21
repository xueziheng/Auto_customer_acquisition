"""普通对话派生计划绑定源Message，不能伪装冷Enrollment或原始客户证据。"""
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from artifact_store.store import (
    GeneratedArtifactKind,
    GeneratedArtifactMeta,
    validate_generated_key,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import new_id


def test_dialogue_plan_artifact_has_distinct_kind_subject_and_key():
    message = new_id('msg')
    meta = GeneratedArtifactMeta(tenant_id=new_id('tn'), artifact_id=new_id('art'), kind=GeneratedArtifactKind.DIALOGUE_PLAN,
        content_hash='a'*64, size_bytes=20, mime_type='application/vnd.tradeos.dialogue-plan+json', workflow_run_id=new_id('run'),
        subject_ref=message, sequence_number=1, idempotency_key=f'{message}:1:dialogue_plan', generated_by='ordinary_dialogue_v1', generated_at=datetime.now(UTC))
    validate_generated_key(meta.tenant_id, meta.idempotency_key)
    with pytest.raises(ValidationError):
        replace(meta, subject_ref=new_id('enr'))
    with pytest.raises(ValidationError):
        replace(meta, kind=GeneratedArtifactKind.EMAIL_DRAFT)
