"""无模型回复分类器只处理明确短语；不确定内容必须失败关闭。"""

import pytest

from agent_runtime.qualification_agent.deterministic import (
    DeterministicReplyClassifier,
)
from domains.conversations.schemas import ReplyCategory, ReplySuppressScope
from shared.errors import ValidationError


@pytest.mark.parametrize(
    "body,expected",
    [
        ("Please send quotation for 20 units.", ReplyCategory.REQUESTS_QUOTE),
        ("Could you quote the solar tricycle?", ReplyCategory.REQUESTS_QUOTE),
        ("What is the price for one container?", ReplyCategory.REQUESTS_QUOTE),
        ("Please send a sample first.", ReplyCategory.REQUESTS_SAMPLE),
        ("Please share your product catalogue.", ReplyCategory.REQUESTS_MATERIALS),
        ("We are not interested, thank you.", ReplyCategory.REJECTION),
        ("Automatic reply: I am out of the office.", ReplyCategory.AUTO_REPLY),
    ],
)
async def test_deterministic_reply_classifier_handles_only_explicit_intent(
    body, expected
):
    result = await DeterministicReplyClassifier().classify(
        message={"message_id": "msg_1", "subject": "Re: solar tricycle", "body": body}
    )
    assert result.category is expected
    assert result.candidate_fields == ()


@pytest.mark.parametrize(
    "body,scope",
    [
        ("Unsubscribe me.", ReplySuppressScope.CONTACT),
        ("Remove our entire company from your list.", ReplySuppressScope.ACCOUNT),
        ("Do not contact us again.", ReplySuppressScope.ACCOUNT),
    ],
)
async def test_deterministic_reply_classifier_prioritizes_unsubscribe(body, scope):
    result = await DeterministicReplyClassifier().classify(
        message={"message_id": "msg_1", "subject": "Re: offer", "body": body}
    )
    assert result.category is ReplyCategory.UNSUBSCRIBE
    assert result.suppress_scope is scope


@pytest.mark.parametrize(
    "body",
    [
        "Maybe later.",
        "Thanks for the email.",
        "Interesting.",
        "Can you tell me more?",
        "We use three-wheelers.",
    ],
)
async def test_deterministic_reply_classifier_fails_closed_on_ambiguous_reply(body):
    with pytest.raises(ValidationError, match="需人工核对"):
        await DeterministicReplyClassifier().classify(
            message={"message_id": "msg_1", "subject": "Re: offer", "body": body}
        )
