"""人工核对是中性结果；不吞技术错误、不提升意向或扩大通知。"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agent_runtime.qualification_agent.agent import (
    QualificationAgent,
    ReplyClassificationResult,
)
from domains.conversations.schemas import ReplyCategory
from domains.conversations.service import reply_requires_owner_attention
from shared.errors import PermissionDenied, ValidationError
from tests.unit.test_reply_action_dispatch import _RecordingOutreach, _run
from workflows.reply_qualification.ports import (
    ReplyContentNeedsReview,
    ReplyMessageContent,
)
from workflows.reply_qualification.steps import ApplyActionsStep, ClassifyStep


@pytest.mark.parametrize('extra', [
    {'candidate_fields': [{'field': 'quantity', 'value': '100', 'quote': '100'}]},
    {'suppress_scope': 'contact'}, {'reason': 'guess'}, {'confidence': 0.9},
])
def test_review_rejects_claims_or_unauthorized_output(extra):
    with pytest.raises(ValidationError):
        QualificationAgent._validate_model_output(
            json.dumps({'category': 'needs_review', **extra}), {'body': '100'})


def test_explicit_unsubscribe_is_not_diluted_to_review():
    result = QualificationAgent._validate_model_output(
        '{"category":"needs_review","candidate_fields":[]}',
        {'body': 'Please unsubscribe our entire company from your list.'},
    )
    assert result.category is ReplyCategory.UNSUBSCRIBE
    assert result.suppress_scope.value == 'account'


class Reader:
    def __init__(self, error=None):
        self.error = error

    async def load(self, *args):
        if self.error:
            raise self.error
        return ReplyMessageContent(subject='Current reply', body='Which one?')


class Classifier:
    model = 'synthetic:test'
    calls = 0

    async def classify(self, **kwargs):
        self.calls += 1
        return ReplyClassificationResult(category=ReplyCategory.NEEDS_REVIEW)


class Access:
    def __init__(self, fail_after=None):
        self.calls = 0
        self.fail_after = fail_after

    async def require(self, *args):
        self.calls += 1
        if self.calls == self.fail_after:
            raise PermissionDenied('revoked')
        return 'trusted-actor'


class Conversations:
    def __init__(self):
        self.records = []

    async def record_classification(self, tenant, message, category, **kwargs):
        self.records.append((category, kwargs))
        assert category is ReplyCategory.NEEDS_REVIEW
        assert kwargs['candidate_fields'] == ()
        assert kwargs['suppress_scope'] is None
        assert kwargs['actor'] == 'trusted-actor'
        return ('stop_sequence', 'review_reply')

    async def get_classification(self, *args):
        return SimpleNamespace(classified_at=datetime(2026, 10, 16, tzinfo=UTC))


def setup_case(error=None, fail_after=None):
    classifier, conversations, access = Classifier(), Conversations(), Access(fail_after)
    guard = SimpleNamespace(check=lambda **kwargs: None)
    step = ClassifyStep(classifier, Reader(error), guard, conversations, access)
    run = _run('tn_review', 'review_reply', category='needs_review')
    run.context.pop('category')
    return classifier, conversations, access, step, run


@pytest.mark.parametrize('projection_failed', [False, True])
async def test_review_stops_sequence_and_dispatches_only_current_owner_work(projection_failed):
    classifier, conversations, access, step, run = setup_case(
        ReplyContentNeedsReview('review') if projection_failed else None)
    status, next_step, patch = await step.execute(run)
    assert (status, next_step) == ('advance', 'apply_actions')
    assert patch['category'] == 'needs_review'
    assert patch['actions'] == ['stop_sequence', 'review_reply']
    assert classifier.calls == (0 if projection_failed else 1)
    assert access.calls == (2 if projection_failed else 3)
    assert conversations.records[0][1]['classified_by'] == (
        'system:reply-content-review-v1' if projection_failed else classifier.model)
    assert not reply_requires_owner_attention(ReplyCategory.NEEDS_REVIEW)
    outreach = _RecordingOutreach()
    calls = []

    class Actions:
        async def review_reply(self, tenant, context, key):
            calls.append((tenant, context.message_id, key))

    run.context.update(patch)
    await ApplyActionsStep(outreach, run.tenant_id, lambda: datetime.now(UTC), Actions()).execute(run)
    assert len(outreach.stops) == 1 and not outreach.suppressions
    assert calls == [('tn_review', run.subject_ref, f'reply:review_reply:{run.subject_ref}')]


@pytest.mark.parametrize('error', [ValidationError('unsafe or missing'), PermissionDenied('denied')])
async def test_ordinary_errors_are_not_converted_to_manual_review(error):
    classifier, conversations, _, step, run = setup_case(error)
    with pytest.raises(type(error)):
        await step.execute(run)
    assert classifier.calls == 0 and not conversations.records


async def test_projection_review_rechecks_revoked_authority_before_any_write():
    classifier, conversations, _, step, run = setup_case(ReplyContentNeedsReview('review'), 2)
    with pytest.raises(PermissionDenied):
        await step.execute(run)
    assert classifier.calls == 0 and not conversations.records


@pytest.mark.parametrize('private', [False, True])
async def test_composed_review_uses_existing_queue_even_for_private_test(monkeypatch, private):
    from contextlib import asynccontextmanager

    from apps.scheduler_worker.reply_composition import (
        CurrentEmployeeReplyActions,
        PrivateReplyTestReader,
    )
    from workflows.reply_qualification.ports import ReplyActionContext

    requests = []

    async def matches(*args):
        return private

    async def enqueue(tenant, request):
        requests.append((tenant, request))

    async def employee(*args, **kwargs):
        return SimpleNamespace(tenant_id='tn_review', employee_id='emp_boss', role='boss',
                               is_active=True, manager_id=None, team_id=None)

    async def forbidden(*args, **kwargs):
        pytest.fail("人工核对不应读取需求或创建商机、接管、发送等业务副作用")

    @asynccontextmanager
    async def scope(*args):
        yield SimpleNamespace(get_employee=employee, resolve_owner=forbidden)

    monkeypatch.setattr(PrivateReplyTestReader, 'matches', matches)
    unreachable = SimpleNamespace(**dict.fromkeys([
        'load', 'get_enrollment', 'resolve_delivery_feedback', 'get_hypothesis',
        'get_need', 'get_account', 'get_contact_point', 'get_by_need', 'get_playbook',
        'create_from_need', 'assign', 'transition', 'update_need_fields',
        'promote_to_validated', 'request_handoff', 'apply_hard_bounce',
        'apply_complaint', 'record_delivery_event',
    ], forbidden))
    core = SimpleNamespace(employee_scope=scope,
        conversations=SimpleNamespace(enqueue_reply_work_action=enqueue),
        demand=unreachable, prospecting=unreachable, organization=unreachable, sending=unreachable)
    resources = SimpleNamespace(sessions=unreachable, opportunities=unreachable)
    actions = CurrentEmployeeReplyActions('tn_review', 'emp_boss', core, unreachable,
                                          resources, unreachable, unreachable)
    context = ReplyActionContext('msg_review', 'out_review', 'enr_review', 'acc_review', 'cp_review')
    await actions.review_reply('tn_review', context, 'reply:review_reply:msg_review')
    assert len(requests) == 1
    assert requests[0][1].action.value == 'review_reply'
    assert requests[0][1].idempotency_key == 'reply:review_reply:msg_review'


@pytest.mark.parametrize('error', [ValidationError('invalid model output'), TimeoutError('provider timeout')])
async def test_model_failures_are_not_review_classifications(error):
    classifier, conversations, _, step, run = setup_case()

    async def fail(**kwargs):
        raise error

    classifier.classify = fail
    with pytest.raises(type(error)):
        await step.execute(run)
    assert not conversations.records
