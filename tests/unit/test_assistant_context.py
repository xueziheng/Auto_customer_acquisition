"""当前引用、来源闭包与不可截断的员工限制。"""

from datetime import UTC, datetime

import pytest

from agent_runtime.assistant.context import (
    AssistantContextBuilder,
    HistoryProjector,
    filter_fragments,
)
from domains.assistant.schemas import (
    AssistantActor,
    AuthorizedFragment,
    Explanation,
    ObjectRef,
    TurnView,
)
from shared.errors import PermissionDenied, ValidationError

A = ObjectRef(kind="opportunity", object_id="opp_a")
B = ObjectRef(kind="need", object_id="need_b")
ACTOR = AssistantActor(tenant_id="tenant_a", user_id="user_a", employee_id="employee_a")


class Reads:
    def __init__(self):
        self.allowed = {A, B}

    async def read(self, actor, ref):
        if ref.kind != "product_doc" and ref not in self.allowed:
            raise PermissionDenied("无权限")
        return AuthorizedFragment(text="受信读取", dependencies=(ref,))


class Identity:
    async def resolve(self, actor):
        return "sales", frozenset(
            {"product_help", "business_read", "research_proposal"}
        )


class History:
    def __init__(self, turns):
        self.values = turns

    async def turns(self, actor, session_id):
        return self.values


def turn(id, **kwargs):
    return TurnView(
        turn_id=id,
        session_id="session_a",
        run_id="run_a",
        state="completed",
        created_at=datetime.now(UTC),
        input_text="员工原话",
        **kwargs,
    )


def test_hidden_dependency_removes_whole_summary():
    summary = AuthorizedFragment(text="合并摘要", dependencies=(A, B))
    assert filter_fragments([summary], frozenset({A})) == ()


async def test_revoked_dependency_hides_subsequent_derived_turn_and_links():
    first = turn(
        "one",
        result=Explanation(
            fragments=(AuthorizedFragment(text="内部资料", dependencies=(A, B)),)
        ),
    )
    second = turn("two", proposal_id="proposal_private")
    reads = Reads()
    reads.allowed = {A}
    projector = HistoryProjector(Identity(), reads, History([first, second]))
    hidden = await projector.project(ACTOR, second)
    assert (
        hidden.content_hidden
        and hidden.result is None
        and hidden.input_text == ""
        and hidden.proposal_id is None
    )


async def test_context_never_silently_truncates_restrictions():
    history = History([turn("one")])
    projector = HistoryProjector(Identity(), Reads(), history)
    builder = AssistantContextBuilder(
        Identity(), Reads(), history, projector, configuration_version="v1", max_bytes=1
    )
    with pytest.raises(ValidationError, match="上下文"):
        await builder.build(ACTOR, "session_a", "one")


async def test_changed_version_hides_old_fragment():
    class Changed(Reads):
        async def read(self, actor, ref):
            return AuthorizedFragment(
                text="新版本", dependencies=(ref.model_copy(update={"version": "new"}),)
            )

    item = turn("one", object_refs=(A.model_copy(update={"version": "old"}),))
    p = HistoryProjector(Identity(), Changed(), History([item]))
    assert (await p.project(ACTOR, item)).content_hidden
