import pytest
from pydantic import ValidationError
from domains.conversations.business_mail_followup import (
    BusinessMailDraftCommand,
    BusinessMailHandlingCommand,
    validate_idempotency_key,
)


@pytest.mark.parametrize("value", ["", "  ", "a\nb", "a\rb", "a\x00b", "x" * 501])
def test_subject_rejected(value):
    with pytest.raises(ValidationError):
        BusinessMailDraftCommand(expected_revision=0, subject=value, body_text="Hello")


@pytest.mark.parametrize("revision", [True, "0", -1])
def test_revision_strict(revision):
    with pytest.raises(ValidationError):
        BusinessMailHandlingCommand(
            expected_revision=revision, status="completed", note="说明"
        )


def test_trim_and_extra_identity():
    assert (
        BusinessMailDraftCommand(
            expected_revision=0, subject=" Hi ", body_text=" Hello "
        ).subject
        == "Hi"
    )
    with pytest.raises(ValidationError):
        BusinessMailHandlingCommand(expected_revision=0, status="pending", note="说明")
    with pytest.raises(ValidationError):
        BusinessMailDraftCommand(
            expected_revision=0, subject="Hi", body_text="Hi", actor_id="forged"
        )


@pytest.mark.parametrize("key", ["", "short", "a" * 201, "abcdefgh\n", "中文abcdefgh"])
def test_key_rejected(key):
    with pytest.raises(ValueError):
        validate_idempotency_key(key)
