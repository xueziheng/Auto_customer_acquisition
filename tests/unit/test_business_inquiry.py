"""普通自然语言询盘、来源护栏及旧TEST-04身份兼容。"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from domains.conversations.business_inquiry import (
    InquiryAnalysis,
    InquiryAutomationScope,
    InquiryReplyScope,
    customer_text,
    evidence_note,
    inquiry_headers,
    reply_text,
    validate_analysis,
)
from shared.errors import ValidationError
from tests.unit.test_business_reply import scope

TEXT = (
    "I am interested in your solar pedestal fans. Please send specifications and MOQ."
)
PLAN = {
    "intent": "buyer_inquiry",
    "reason": "客户询问产品资料，尚缺用途与数量",
    "intent_quote": TEXT,
    "fields": [
        {
            "name": "product",
            "value": "solar pedestal fans",
            "evidence_quote": "your solar pedestal fans",
        }
    ],
    "next_fields": ["application", "quantity"],
}


def test_natural_inquiry_has_no_fixed_subject_product_or_label_requirement():
    plan = validate_analysis(json.dumps(PLAN), TEXT, followup=False)
    assert plan.fields[0].value == "solar pedestal fans"
    assert "application" in reply_text(plan) and "quantity" in reply_text(plan)
    assert "YJ-168" not in reply_text(plan)


@pytest.mark.parametrize(
    "change",
    [
        "hallucination",
        "quote_history",
        "duplicate",
        "already_known",
        "too_many",
        "extra_permission",
        "false_quantity",
    ],
)
def test_unsupported_model_proposals_fail_closed(change):
    data = json.loads(json.dumps(PLAN))
    if change == "hallucination":
        data["fields"][0]["value"] = "made up product"
    elif change == "quote_history":
        data["fields"][0]["evidence_quote"] = "Product: fan"
    elif change == "duplicate":
        data["fields"] *= 2
    elif change == "already_known":
        data["next_fields"] = ["product"]
    elif change == "too_many":
        data["next_fields"] = ["application", "quantity", "product"]
    elif change == "extra_permission":
        data["send_allowed"] = True
    elif change == "false_quantity":
        data["fields"].append(
            {"name": "quantity", "value": "100 units", "evidence_quote": "MOQ"}
        )
    with pytest.raises((ValueError, ValidationError)):
        validate_analysis(
            json.dumps(data), TEXT + "\nOn Monday wrote:\nProduct: fan", followup=False
        )


@pytest.mark.parametrize("body", ["Please unsubscribe. " + TEXT, "不要再联系。" + TEXT])
def test_stop_overrides_positive_words(body):
    with pytest.raises(ValidationError):
        validate_analysis(json.dumps(PLAN), body, followup=False)


def test_natural_supplement_preserves_exact_value_and_no_human_confirmation():
    body = "We need about 100 units for indoor ventilation (test data)."
    plan = validate_analysis(
        json.dumps(
            {
                "intent": "customer_details",
                "reason": "补充用途和数量",
                "intent_quote": body,
                "fields": [
                    {
                        "name": "quantity",
                        "value": "100 units",
                        "evidence_quote": "about 100 units",
                    },
                    {
                        "name": "application",
                        "value": "indoor ventilation",
                        "evidence_quote": "for indoor ventilation",
                    },
                ],
                "next_fields": ["product"],
            }
        ),
        body,
        followup=True,
    )
    note = json.loads(
        evidence_note(
            plan,
            {
                "message_id": "bmi-source",
                "artifact_id": "art-source",
                "source_sha256": "f" * 64,
            },
            "run-model",
        )
    )
    assert note["confirmed_by"] is None and note["validated_need"] is False
    assert note["source_id"] == "bmi-source" and note["extracted_by"] == "run-model"


def test_quoted_mail_does_not_fill_current_missing_fields():
    assert customer_text("Thank you\n> Quantity: 100 units") == "Thank you"
    assert (
        customer_text(
            "Hello\n------------------ Original ------------------\nQuantity: 100"
        )
        == "Hello"
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"Auto-Submitted": "auto-replied"},
        {"List-Id": "newsletter"},
        {"Precedence": "bulk"},
        {"Reply-To": "other@example.com"},
        {"To": "me@example.com, other@example.com"},
        {"In-Reply-To": "<old@example.com>"},
        {"References": "<old@example.com>"},
        {"Message-ID": "bad"},
    ],
)
def test_robot_list_cross_thread_and_redirect_not_new_inquiry(patch):
    headers = {
        "From": "Buyer <buyer@example.com>",
        "To": "me@example.com",
        "Message-ID": "<one@example.com>",
    }
    assert inquiry_headers(headers, "me@example.com", "buyer@example.com")
    assert not inquiry_headers(headers | patch, "me@example.com", "buyer@example.com")


def test_scope_limits_and_legacy_digest_stay_stable():
    old = scope()
    digest = old.digest
    s = InquiryReplyScope(
        **old.model_dump(),
        policy_digest="f" * 64,
        source_revision=1,
        model_run_id="run-plan",
        subject="Re: Your inquiry",
        analysis=InquiryAnalysis.model_validate(PLAN),
    )
    assert old.digest == digest and old.key == s.key and old.digest != s.digest
    assert len(s.model_dump_json()) < 2000
    now = datetime.now(UTC)
    data = {
        k: v
        for k, v in old.model_dump().items()
        if k in InquiryAutomationScope.model_fields
    }
    data.update(
        mailbox_id="bmb-fixture",
        allowed_recipients=(old.recipient,),
        recovery_message_ids=(),
        approved_at=now,
        expires_at=now + timedelta(hours=25),
    )
    with pytest.raises(ValueError):
        InquiryAutomationScope(**data)


@pytest.mark.parametrize(
    "status,category,origin,followup,expected",
    [
        ("pending", "pending", None, True, "waiting_classification"),
        ("classifying", "pending", None, True, "waiting_classification"),
        ("classified", "review", "model", True, "ready"),
        ("classified", "review", "model", False, "reply_review_required"),
        ("classified", "review", "human", True, "reply_review_required"),
        ("classified", "review", None, True, "reply_review_required"),
        ("classified", "other", "model", True, "reply_review_required"),
        ("failed", "review", "system", True, "reply_review_required"),
        ("classified", "business", "human", False, "ready"),
    ],
)
def test_analysis_classification_boundary(status, category, origin, followup, expected):
    from domains.conversations.business_inquiry import inquiry_analysis_status

    assert (
        inquiry_analysis_status(
            {
                "classification_status": status,
                "category": category,
                "classification_origin": origin,
            },
            followup=followup,
        )
        == expected
    )
