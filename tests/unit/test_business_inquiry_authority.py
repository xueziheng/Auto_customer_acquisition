"""实际模型授权端拒绝未分类、人工限制和伪线程，不接触真实模型。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.scheduler_worker.business_inquiry import InquiryModelAuthority
from domains.conversations.business_inquiry import InquiryAnalysis, InquiryReplyScope
from shared.schemas.model_invocation import ModelGenerationError
from tests.unit.test_business_inquiry import PLAN
from tests.unit.test_business_inquiry_planner import identity
from tests.unit.test_business_reply import scope
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.business_inquiry import InquiryReplyHandler


def authority_fixture(*, followup=True):
    old = scope()
    ident = identity()
    config = SimpleNamespace(digest="f" * 64, actor=old.actor)
    reply_scope = InquiryReplyScope(
        **old.model_dump(),
        policy_digest=config.digest,
        source_revision=1,
        model_run_id="run-plan",
        subject="Re: inquiry",
        analysis=InquiryAnalysis.model_validate(PLAN),
    )
    fingerprints = HmacFingerprintProvider("v1", b"fixture" * 8)
    outgoing = (
        "<"
        + InquiryReplyHandler(reply_scope, fingerprints, None)
        .envelope()[0]
        .deterministic_message_id
        + ">"
    )
    source = {
        "message_id": "bmi-reply" if followup else old.message_id,
        "source_sha256": "a" * 64,
        "revision": 1,
        "classification_status": "classified",
        "category": "review" if followup else "business",
        "classification_origin": "model",
        "headers": {
            "From": old.recipient,
            "To": old.sender,
            "Message-ID": "<reply@example.com>",
            "In-Reply-To": outgoing,
        },
    }
    claim = {
        "identity": ident.model_dump(),
        "policy_digest": config.digest,
        "model": "fixture",
        "source_sha256": source["source_sha256"],
        "source_revision": 1,
        "followup": followup,
    }
    events = {
        "inquiry-claim:" + old.message_id + ":" + source["message_id"]: SimpleNamespace(
            note=json.dumps(claim)
        ),
        "inquiry-scope:" + old.message_id: SimpleNamespace(
            note=reply_scope.model_dump_json()
        ),
        "inquiry-sent:" + old.message_id: SimpleNamespace(note="sent"),
    }

    async def event(original, key):
        assert original == old.message_id
        return events.get(key)

    records = SimpleNamespace(
        scope=config,
        authorize=AsyncMock(),
        event=event,
        view=AsyncMock(return_value=SimpleNamespace(status="in_progress")),
    )
    settings = SimpleNamespace(
        model="fixture",
        configuration_version=ident.configuration_version,
        limits="fixture",
    )
    repository = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                model=settings.model,
                configuration_version=settings.configuration_version,
                limits=settings.limits,
                model_data_export_enabled=True,
            )
        )
    )
    configuration = SimpleNamespace(authorize=AsyncMock())

    async def metadata(message):
        assert message == source["message_id"]
        return source

    authority = InquiryModelAuthority(
        records,
        old.message_id,
        source,
        ident,
        configuration,
        repository,
        settings,
        metadata,
        fingerprints,
    )
    return authority, ident, source, events, configuration


async def test_matched_model_review_is_extraction_only_authorized():
    authority, ident, _, _, configuration = authority_fixture()
    await authority.check(ident)
    configuration.authorize.assert_awaited_once()


@pytest.mark.parametrize(
    "change",
    [
        "pending",
        "classifying",
        "failed",
        "human",
        "other",
        "source_revision",
        "unrelated_thread",
        "wrong_sender",
        "missing_sent",
        "initial_review",
    ],
)
async def test_live_authority_rechecks_reply_constraints(change):
    authority, ident, source, events, configuration = authority_fixture(
        followup=change != "initial_review"
    )
    if change in {"pending", "classifying", "failed"}:
        source["classification_status"] = change
    elif change == "human":
        source["classification_origin"] = "human"
    elif change == "other":
        source["category"] = "other"
    elif change == "initial_review":
        source["category"] = "review"
    elif change == "source_revision":
        source["revision"] = 2
    elif change == "unrelated_thread":
        source["headers"]["In-Reply-To"] = "<different@example.com>"
    elif change == "wrong_sender":
        source["headers"]["From"] = "stranger@example.com"
    elif change == "missing_sent":
        events.pop("inquiry-sent:" + authority.original)
    with pytest.raises(ModelGenerationError):
        await authority.check(ident)
    configuration.authorize.assert_not_awaited()
