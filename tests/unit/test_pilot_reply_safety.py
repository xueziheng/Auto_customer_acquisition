"""当前 pilot 分类器必须识别明确退订，且保留正确抑制范围。"""

import json
from pathlib import Path

import pytest

from agent_runtime.qualification_agent.deterministic import DeterministicReplyClassifier
from domains.conversations.schemas import ReplyCategory, ReplySuppressScope

_CORPUS = Path(__file__).parents[1] / "evals" / "replies" / "unsubscribe"
_CASES = tuple(sorted(_CORPUS.glob("*/input.json")))


@pytest.mark.parametrize("path", _CASES, ids=lambda path: path.parent.name)
async def test_pilot_recognizes_existing_unsubscribe_corpus(path):
    """漏掉退订表达或把公司级降为联系人级，均应阻止交付。"""
    message = json.loads(path.read_text())
    # 实际正文投影会移除历史主题，不能依赖样本标题提示退订类别。
    message["subject"] = "(current reply)"
    expected = json.loads((path.parent / "expected.json").read_text())
    result = await DeterministicReplyClassifier().classify(message=message)
    assert result.category is ReplyCategory.UNSUBSCRIBE
    assert result.suppress_scope is ReplySuppressScope(expected["suppress_scope"])
    assert result.candidate_fields == ()


@pytest.mark.parametrize(
    "body,scope",
    [
        ("Kindly take us off the mailing list.", ReplySuppressScope.ACCOUNT),
        (
            "Please delete my contact details from your mailing database.",
            ReplySuppressScope.CONTACT,
        ),
        ("Please unsubsribe me.", ReplySuppressScope.CONTACT),
        ("Don't email me again.", ReplySuppressScope.CONTACT),
        (
            "Please add my address to your do-not-contact list.",
            ReplySuppressScope.CONTACT,
        ),
        ("请不要再给我发邮件了。", ReplySuppressScope.CONTACT),
        ("请把我们公司从邮件名单中删除。", ReplySuppressScope.ACCOUNT),
        ("请停止联系我和我的同事。", ReplySuppressScope.ACCOUNT),
    ],
)
async def test_pilot_unsubscribe_variants_without_subject_hint(body, scope):
    """正文自身应足以证明退订，不能靠测试主题中的标签补全类别。"""
    result = await DeterministicReplyClassifier().classify(
        message={
            "message_id": "msg_pilot_safety",
            "subject": "Re: enquiry",
            "body": body,
        }
    )
    assert result.category is ReplyCategory.UNSUBSCRIBE
    assert result.suppress_scope is scope
    assert result.candidate_fields == ()
