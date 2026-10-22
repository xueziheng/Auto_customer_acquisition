"""分类提交后流程事务崩溃，可复用持久分类，不能再调用模型。"""

from agent_runtime.qualification_agent.agent import ReplyClassificationResult
from apps.scheduler_worker.reply_model_binding import BoundReplyClassifier
from domains.conversations.schemas import ReplyCategory


async def test_committed_recovery_is_replayed_without_new_model_identity():
    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError("must not call model again")

    result = ReplyClassificationResult(category=ReplyCategory.WILLING_TO_CONTINUE)

    async def replay(message):
        assert message == "msg_committed"
        return result

    classifier = BoundReplyClassifier(
        Forbidden(),
        Forbidden(),
        model="test-model",
        configuration_version="v1",
        max_output_tokens=512,
        replay=replay,
    )
    assert (
        await classifier.classify(
            message={"message_id": "msg_committed", "body": "already committed"}
        )
        is result
    )
