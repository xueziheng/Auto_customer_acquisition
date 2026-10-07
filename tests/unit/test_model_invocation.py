"""模型边界拒绝伪造身份、隐式限额与虚构用量，默认投影不泄漏正文。"""

import pytest
from pydantic import ValidationError


def test_usage_is_measured_not_coerced():
    from shared.schemas.model_invocation import ModelUsage

    assert (
        ModelUsage(
            input_tokens=None, cached_input_tokens=None, output_tokens=None
        ).input_tokens
        is None
    )
    for values in [(True, 0, 1), (2, 3, 1), (-1, 0, 1), (1, 0, "2")]:
        with pytest.raises(ValidationError):
            ModelUsage(
                input_tokens=values[0],
                cached_input_tokens=values[1],
                output_tokens=values[2],
            )


def test_limits_have_no_defaults_and_reject_coercion():
    from shared.schemas.model_invocation import ModelLimits

    with pytest.raises(ValidationError):
        ModelLimits.model_validate({})
    fields = {
        "window_seconds": 86400,
        "tenant_calls": 10,
        "employee_calls": 3,
        "tenant_concurrency": 2,
        "employee_concurrency": 1,
        "max_input_bytes": 4096,
        "max_output_tokens": 64,
        "timeout_seconds": 10,
    }
    assert ModelLimits(**fields).tenant_calls == 10
    for name in fields:
        for invalid in (True, 0, -1, "5"):
            with pytest.raises(ValidationError):
                ModelLimits(**(fields | {name: invalid}))


def test_identity_cannot_contain_untrusted_authority_or_credentials():
    from shared.schemas.model_invocation import InvocationIdentity

    values = {
        "tenant_id": "tn_test",
        "user_id": "usr_test",
        "employee_id": "emp_test",
        "run_id": "run_test",
        "turn_id": None,
        "capability": "product_help",
        "configuration_version": "v1",
        "sequence": 0,
    }
    assert InvocationIdentity(**values).sequence == 0
    for patch in (
        {"role": "boss"},
        {"secret_ref": "credential"},
        {"tenant_id": ""},
        {"run_id": " "},
        {"sequence": True},
        {"capability": "shell"},
        {"configuration_version": ""},
    ):
        with pytest.raises(ValidationError):
            InvocationIdentity(**(values | patch))


def test_default_projection_and_validation_errors_hide_content():
    from shared.schemas.model_invocation import ModelRequest, ModelResponse, ModelUsage

    sentinel = "PRIVATE_CONTENT_SENTINEL"
    request = ModelRequest(
        model="test-model",
        system_prompt=sentinel,
        payload={"message": sentinel},
        max_output_tokens=64,
    )
    response = ModelResponse(
        text=sentinel,
        model="test-model",
        usage=ModelUsage(input_tokens=1, cached_input_tokens=0, output_tokens=2),
    )
    for value in (request, response):
        assert sentinel not in repr(value)
        assert sentinel not in value.model_dump_json()
    with pytest.raises(ValidationError) as caught:
        ModelRequest(
            model="test-model",
            system_prompt=sentinel,
            payload={"message": sentinel},
            max_output_tokens=0,
        )
    assert sentinel not in str(caught.value)


def test_request_rejects_non_json_payload():
    from shared.schemas.model_invocation import ModelRequest

    for payload in ({"bad": object()}, {"bad": float("nan")}, {"bad": float("inf")}):
        with pytest.raises(ValidationError):
            ModelRequest(
                model="test-model",
                system_prompt="规则",
                payload=payload,
                max_output_tokens=64,
            )


def test_reply_capability_preserves_canonical_identity():
    from shared.schemas.model_invocation import InvocationIdentity

    identity = InvocationIdentity(
        tenant_id="tn_reply",
        user_id="usr_reply",
        employee_id="emp_reply",
        run_id="run_reply",
        capability="reply_qualification",
        configuration_version="reply-v1",
        sequence=0,
    )
    restored = InvocationIdentity.model_validate_json(identity.model_dump_json())
    assert restored.capability == "reply_qualification"
    assert restored.run_id == "run_reply" and restored.turn_id is None
    assert restored.sequence == 0
