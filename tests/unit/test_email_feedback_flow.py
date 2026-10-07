"""邮件反馈整页流程的决策、幂等、授权与顺序合同。"""

from __future__ import annotations

import importlib
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Self

import pytest

from shared.errors import PermissionDenied, TransientError, ValidationError
from shared.schemas.email_feedback import (
    EmailFeedbackCorrelation,
    EmailFeedbackItem,
    EmailFeedbackKind,
    EmailFeedbackPage,
    EmailFeedbackParseIssue,
    EmailFeedbackQuarantineReason,
    EmailFeedbackResult,
)
from shared.schemas.identifiers import TenantId, new_id

NOW = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
TENANT = TenantId(new_id("tn"))
OTHER_TENANT = TenantId(new_id("tn"))
IDENTITY = new_id("sid")
OTHER_IDENTITY = new_id("sid")
ATTEMPT = new_id("mat")
ENROLLMENT = new_id("enr")
ACCOUNT = new_id("acc")
CONTACT = new_id("cp")


def _flow() -> object:
    try:
        return importlib.import_module("workflows.email_feedback.flow")
    except ModuleNotFoundError as exc:
        pytest.fail(f"缺少邮件反馈整页流程：{exc}")


def _contract() -> object:
    return importlib.import_module("workflows.email_feedback.repository")


def _target(*, tenant_id: TenantId = TENANT, identity_id: str = IDENTITY) -> object:
    service = importlib.import_module("domains.outreach.service")
    return service.DeliveryFeedbackTarget(
        tenant_id=tenant_id,
        attempt_id=ATTEMPT,
        enrollment_id=ENROLLMENT,
        account_id=ACCOUNT,
        contact_point_id=CONTACT,
        sending_identity_id=identity_id,
    )


def _correlation(
    *,
    route_id: str | None = "route-v1",
    message: str | None = None,
    header: str | None = None,
) -> EmailFeedbackCorrelation:
    prefix = f"{route_id}." if route_id is not None else ""
    return EmailFeedbackCorrelation(
        route_id=route_id,
        deterministic_message_id=(
            message
            if message is not None
            else prefix + "a" * 64 + "@messages.tradeos.invalid"
        ),
        idempotency_header=(header if header is not None else prefix + "b" * 64),
    )


def _item(
    event: str,
    *,
    kind: EmailFeedbackKind = EmailFeedbackKind.HARD_BOUNCE,
    ordinal: int = 0,
    correlation: EmailFeedbackCorrelation | None = None,
    issue: EmailFeedbackParseIssue | None = None,
    occurred_at: datetime = NOW,
) -> EmailFeedbackItem:
    return EmailFeedbackItem(
        provider_event_id=event * 64,
        provider_ref_digest=event * 64,
        ordinal=ordinal,
        kind=kind,
        occurred_at=occurred_at,
        correlation=(
            correlation
            if correlation is not None
            else None
            if kind is EmailFeedbackKind.UNPARSEABLE
            else _correlation()
        ),
        parse_issue=issue,
    )


class _CursorRepo:
    def __init__(self, store: _Store) -> None:
        self.store = store

    async def lock_expected(
        self, tenant_id: TenantId, mailbox_alias: str, expected_cursor: str | None
    ) -> object:
        self.store.trace.append(("cursor.lock", tenant_id, mailbox_alias))
        if self.store.cursor != expected_cursor:
            raise TransientError("邮件反馈 cursor 已变化")
        contract = _contract()
        return contract.FeedbackCursor(
            tenant_id, mailbox_alias, self.store.cursor, self.store.version, NOW, None
        )

    async def advance(self, cursor: object, next_cursor: str, at: datetime) -> None:
        self.store.trace.append(("cursor.advance", next_cursor))
        self.store.maybe_fail("cursor")
        self.store.cursor = next_cursor
        self.store.version += 1


class _ReceiptRepo:
    def __init__(self, store: _Store) -> None:
        self.store = store

    async def get(
        self, tenant_id: TenantId, mailbox_alias: str, provider_event_id: str
    ) -> object | None:
        self.store.trace.append(("receipt.get", provider_event_id))
        self.store.maybe_fail("receipt_get")
        return self.store.receipts.get(provider_event_id)

    async def append_if_absent(self, receipt: object) -> object:
        contract = _contract()
        event_id = receipt.provider_event_id
        self.store.trace.append(("receipt.append", event_id))
        self.store.maybe_fail("receipt_append")
        existing = self.store.receipts.get(event_id)
        if existing is None:
            self.store.receipts[event_id] = receipt
            return contract.FeedbackReceiptAppendResult(
                contract.FeedbackReceiptAppendStatus.CREATED, receipt
            )
        if existing == receipt:
            return contract.FeedbackReceiptAppendResult(
                contract.FeedbackReceiptAppendStatus.EXISTING, existing
            )
        return contract.FeedbackReceiptAppendResult(
            contract.FeedbackReceiptAppendStatus.CONFLICT, None
        )


class _QuarantineRepo:
    def __init__(self, store: _Store) -> None:
        self.store = store

    async def append_if_absent(self, quarantine: object) -> bool:
        self.store.trace.append(("quarantine.append", quarantine.reason))
        self.store.maybe_fail("quarantine")
        existing = self.store.quarantines.get(quarantine.provider_event_id)
        if existing is None:
            self.store.quarantines[quarantine.provider_event_id] = quarantine
            return True
        if existing != quarantine:
            raise ValidationError("邮件反馈 quarantine 冲突")
        return False


class _Outreach:
    def __init__(self, store: _Store) -> None:
        self.store = store

    async def resolve_delivery_feedback(
        self, tenant_id: TenantId, lookup: object, *, actor: object
    ) -> object | None:
        self.store.trace.append(("outreach.resolve", tenant_id, actor, lookup))
        if self.store.resolve_error is not None:
            raise self.store.resolve_error
        return self.store.target

    async def apply_hard_bounce(
        self,
        tenant_id: TenantId,
        target: object,
        provider_event_id: str,
        occurred_at: datetime,
        *,
        actor: object,
    ) -> object:
        self.store.trace.append(
            ("outreach.hard", target.contact_point_id, provider_event_id, actor)
        )
        if self.store.outreach_error is not None:
            raise self.store.outreach_error
        self.store.outreach_effects.add(provider_event_id)
        return SimpleNamespace(created=True)


class _Sending:
    def __init__(self, store: _Store) -> None:
        self.store = store

    async def get(
        self, tenant_id: TenantId, identity_id: str, *, actor: object
    ) -> object:
        self.store.trace.append(("sending.get", tenant_id, identity_id, actor))
        if self.store.sending_read_error is not None:
            raise self.store.sending_read_error
        return SimpleNamespace(
            identity_id=identity_id,
            domain=self.store.domains.get(identity_id, "cold.example"),
        )

    async def record_delivery_event(
        self,
        tenant_id: TenantId,
        identity_id: str,
        event: object,
        *,
        actor: object,
    ) -> bool:
        self.store.trace.append(
            ("sending.record", identity_id, str(event.dedup_key), actor)
        )
        if self.store.sending_write_error is not None:
            raise self.store.sending_write_error
        self.store.sending_effects.add(str(event.dedup_key))
        return True


class _Store:
    def __init__(self) -> None:
        self.cursor: str | None = None
        self.version = 0
        self.receipts: dict[str, object] = {}
        self.quarantines: dict[str, object] = {}
        self.outreach_effects: set[str] = set()
        self.sending_effects: set[str] = set()
        self.trace: list[tuple[object, ...]] = []
        self.target: object | None = _target()
        self.resolve_error: BaseException | None = None
        self.outreach_error: BaseException | None = None
        self.sending_read_error: BaseException | None = None
        self.sending_write_error: BaseException | None = None
        self.domains: dict[str, str] = {}
        self.uow_entries = 0
        self.failure_at: str | None = None
        self.failure: BaseException | None = None

    def maybe_fail(self, boundary: str) -> None:
        if self.failure_at == boundary and self.failure is not None:
            raise self.failure


class _Uow:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.cursors = _CursorRepo(store)
        self.receipts = _ReceiptRepo(store)
        self.quarantines = _QuarantineRepo(store)
        self.outreach = _Outreach(store)
        self.sending_identities = _Sending(store)

    async def __aenter__(self) -> Self:
        self.store.uow_entries += 1
        self.snapshot = (
            self.store.cursor,
            self.store.version,
            dict(self.store.receipts),
            dict(self.store.quarantines),
            set(self.store.outreach_effects),
            set(self.store.sending_effects),
        )
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        commit_failed = False
        if exc_type is None:
            try:
                self.store.maybe_fail("commit")
            except BaseException:  # noqa: BLE001 - fake 模拟真实 cleanup
                commit_failed = True
        if exc_type is not None or commit_failed:
            (
                self.store.cursor,
                self.store.version,
                self.store.receipts,
                self.store.quarantines,
                self.store.outreach_effects,
                self.store.sending_effects,
            ) = self.snapshot
        if commit_failed and self.store.failure is not None:
            raise self.store.failure


class _Factory:
    def __init__(self, store: _Store) -> None:
        self.store = store

    def __call__(self, tenant_id: TenantId) -> _Uow:
        assert tenant_id == TENANT
        return _Uow(self.store)


def _processor(
    store: _Store, *, critical: list[dict[str, object]] | None = None
) -> object:
    module = _flow()
    critical_records = critical if critical is not None else []
    return module.FeedbackPageProcessor(
        _Factory(store),
        route_id="route-v1",
        outreach_actor_factory=lambda identity: ("outreach", identity),
        sending_identity_actor_factory=lambda identity: ("sending", identity),
        now=lambda: NOW,
        critical_sink=lambda **record: critical_records.append(record),
    )


@pytest.mark.asyncio
async def test_new_hard_soft_and_unparseable_are_applied_in_one_page() -> None:
    store = _Store()
    page = EmailFeedbackPage(
        None,
        "cursor-v1",
        (
            _item("a", kind=EmailFeedbackKind.HARD_BOUNCE),
            _item("b", kind=EmailFeedbackKind.SOFT_BOUNCE, ordinal=1),
            _item(
                "c",
                kind=EmailFeedbackKind.UNPARSEABLE,
                ordinal=2,
                issue=EmailFeedbackParseIssue.MALFORMED,
            ),
        ),
    )
    result = await _processor(store).process(
        TENANT, "feedback-primary", IDENTITY, None, page
    )
    assert result == _flow().FeedbackPageResult(3, 0, 1, 1, 1, "cursor-v1")
    assert store.cursor == "cursor-v1"
    assert {receipt.result for receipt in store.receipts.values()} == {
        EmailFeedbackResult.APPLIED,
        EmailFeedbackResult.RECORDED,
        EmailFeedbackResult.QUARANTINED,
    }
    assert len(store.sending_effects) == len(store.outreach_effects) == 1


@pytest.mark.asyncio
async def test_duplicate_receipt_never_calls_either_domain_or_writes_allow_effect() -> (
    None
):
    store = _Store()
    page = EmailFeedbackPage(None, "cursor-v1", (_item("a"),))
    first = await _processor(store).process(
        TENANT, "feedback-primary", IDENTITY, None, page
    )
    assert first.hard_bounces == 1
    store.trace.clear()
    duplicate = EmailFeedbackPage("cursor-v1", "cursor-v2", (_item("a"),))
    result = await _processor(store).process(
        TENANT, "feedback-primary", IDENTITY, "cursor-v1", duplicate
    )
    assert result == _flow().FeedbackPageResult(0, 1, 0, 0, 0, "cursor-v2")
    assert not any(
        call[0].startswith(("outreach.", "sending.")) for call in store.trace
    )
    assert len(store.sending_effects) == len(store.outreach_effects) == 1


@pytest.mark.asyncio
async def test_same_event_id_with_changed_payload_fails_whole_page() -> None:
    store = _Store()
    first = EmailFeedbackPage(None, "cursor-v1", (_item("a"),))
    await _processor(store).process(TENANT, "feedback-primary", IDENTITY, None, first)
    changed = EmailFeedbackPage(
        "cursor-v1",
        "cursor-v2",
        (_item("a", kind=EmailFeedbackKind.SOFT_BOUNCE),),
    )
    with pytest.raises(ValidationError):
        await _processor(store).process(
            TENANT, "feedback-primary", IDENTITY, "cursor-v1", changed
        )
    assert store.cursor == "cursor-v1"
    assert store.version == 1


@pytest.mark.asyncio
async def test_same_event_id_with_changed_correlation_fails_before_domain_read() -> (
    None
):
    store = _Store()
    first = EmailFeedbackPage(None, "cursor-v1", (_item("a"),))
    await _processor(store).process(TENANT, "feedback-primary", IDENTITY, None, first)
    store.trace.clear()
    changed = EmailFeedbackPage(
        "cursor-v1",
        "cursor-v2",
        (
            _item(
                "a",
                correlation=EmailFeedbackCorrelation(
                    "route-v1",
                    "route-v1." + "c" * 64 + "@messages.tradeos.invalid",
                    "route-v1." + "c" * 64,
                ),
            ),
        ),
    )
    with pytest.raises(ValidationError):
        await _processor(store).process(
            TENANT, "feedback-primary", IDENTITY, "cursor-v1", changed
        )
    assert store.trace == [
        ("cursor.lock", TENANT, "feedback-primary"),
        ("receipt.get", "a" * 64),
    ]
    assert (store.cursor, store.version) == ("cursor-v1", 1)


@pytest.mark.asyncio
async def test_late_changed_payload_fails_before_any_new_item_domain_read() -> None:
    store = _Store()
    await _processor(store).process(
        TENANT,
        "feedback-primary",
        IDENTITY,
        None,
        EmailFeedbackPage(None, "cursor-v1", (_item("a"),)),
    )
    store.trace.clear()

    with pytest.raises(ValidationError):
        await _processor(store).process(
            TENANT,
            "feedback-primary",
            IDENTITY,
            "cursor-v1",
            EmailFeedbackPage(
                "cursor-v1",
                "cursor-v2",
                (
                    _item("b"),
                    _item("a", kind=EmailFeedbackKind.SOFT_BOUNCE, ordinal=1),
                ),
            ),
        )

    assert store.trace == [
        ("cursor.lock", TENANT, "feedback-primary"),
        ("receipt.get", "b" * 64),
        ("receipt.get", "a" * 64),
    ]
    assert (store.cursor, store.version) == ("cursor-v1", 1)
    assert len(store.sending_effects) == len(store.outreach_effects) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("single_key", ("message", "header"))
async def test_one_correlation_key_is_sufficient_for_exact_resolution(
    single_key: str,
) -> None:
    correlation = EmailFeedbackCorrelation(
        "route-v1",
        (
            "route-v1." + "a" * 64 + "@messages.tradeos.invalid"
            if single_key == "message"
            else None
        ),
        "route-v1." + "a" * 64 if single_key == "header" else None,
    )
    store = _Store()
    result = await _processor(store).process(
        TENANT,
        "feedback-primary",
        IDENTITY,
        None,
        EmailFeedbackPage(None, "cursor-v1", (_item("a", correlation=correlation),)),
    )
    assert (result.processed, result.hard_bounces, result.quarantined) == (1, 1, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("item", "reason"),
    [
        (
            _item(
                "a",
                kind=EmailFeedbackKind.UNPARSEABLE,
                issue=EmailFeedbackParseIssue.UNSUPPORTED,
            ),
            EmailFeedbackQuarantineReason.UNSUPPORTED,
        ),
        (
            _item("b", correlation=_correlation(route_id=None)),
            EmailFeedbackQuarantineReason.MISSING_CORRELATION,
        ),
        (
            _item("c", correlation=_correlation(route_id="other-route")),
            EmailFeedbackQuarantineReason.CROSS_TENANT_CORRELATION,
        ),
    ],
)
async def test_deterministic_bad_items_are_quarantined_without_business_effects(
    item: EmailFeedbackItem,
    reason: EmailFeedbackQuarantineReason,
) -> None:
    store = _Store()
    critical: list[dict[str, object]] = []
    result = await _processor(store, critical=critical).process(
        TENANT,
        "feedback-primary",
        IDENTITY,
        None,
        EmailFeedbackPage(None, "cursor-v1", (item,)),
    )
    assert result.quarantined == 1
    assert next(iter(store.quarantines.values())).reason is reason
    assert not store.sending_effects and not store.outreach_effects
    assert bool(critical) is (
        reason is EmailFeedbackQuarantineReason.CROSS_TENANT_CORRELATION
    )
    if critical:
        assert critical == [
            {
                "tenant_id": TENANT,
                "mailbox_alias": "feedback-primary",
                "reason": "cross-tenant-correlation",
            }
        ]


@pytest.mark.asyncio
async def test_ambiguous_correlation_is_quarantined_but_other_failures_escape() -> None:
    service = importlib.import_module("domains.outreach.service")
    store = _Store()
    store.resolve_error = service.MessageAttemptConflictError(
        "delivery correlation 指向不同 Message Attempt"
    )
    result = await _processor(store).process(
        TENANT,
        "feedback-primary",
        IDENTITY,
        None,
        EmailFeedbackPage(None, "cursor-v1", (_item("a"),)),
    )
    assert result.quarantined == 1
    assert next(iter(store.quarantines.values())).reason is (
        EmailFeedbackQuarantineReason.AMBIGUOUS_CORRELATION
    )

    denied = _Store()
    denied.resolve_error = PermissionDenied("固定授权拒绝")
    with pytest.raises(PermissionDenied):
        await _processor(denied).process(
            TENANT,
            "feedback-primary",
            IDENTITY,
            None,
            EmailFeedbackPage(None, "cursor-v1", (_item("b"),)),
        )
    assert denied.receipts == denied.quarantines == {}
    assert denied.cursor is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [_target(tenant_id=OTHER_TENANT), _target(identity_id=OTHER_IDENTITY)],
)
async def test_malicious_or_wrong_identity_target_is_quarantined(
    target: object,
) -> None:
    store = _Store()
    store.target = target
    critical: list[dict[str, object]] = []
    result = await _processor(store, critical=critical).process(
        TENANT,
        "feedback-primary",
        IDENTITY,
        None,
        EmailFeedbackPage(None, "cursor-v1", (_item("a"),)),
    )
    assert result.quarantined == 1
    assert not any(call[0] == "sending.get" for call in store.trace)
    assert critical[0]["reason"] == "cross-tenant-correlation"


@pytest.mark.asyncio
async def test_cursor_stale_or_domain_failure_rolls_back_everything() -> None:
    stale = _Store()
    stale.cursor = "newer"
    with pytest.raises(TransientError):
        await _processor(stale).process(
            TENANT,
            "feedback-primary",
            IDENTITY,
            "old",
            EmailFeedbackPage("old", "next", (_item("a"),)),
        )
    assert stale.receipts == {}

    failed = _Store()
    sentinel = RuntimeError("fixed repository failure")
    failed.outreach_error = sentinel
    with pytest.raises(RuntimeError) as raised:
        await _processor(failed).process(
            TENANT,
            "feedback-primary",
            IDENTITY,
            None,
            EmailFeedbackPage(None, "next", (_item("a"),)),
        )
    assert raised.value is sentinel
    assert failed.cursor is None
    assert failed.receipts == failed.quarantines == {}
    assert not failed.sending_effects and not failed.outreach_effects


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    (
        "receipt_get",
        "receipt_append",
        "quarantine",
        "sending_read",
        "sending_write",
        "outreach_write",
        "cursor",
        "commit",
    ),
)
async def test_every_failure_boundary_preserves_original_exception_and_rolls_back(
    boundary: str,
) -> None:
    store = _Store()
    sentinel = RuntimeError(f"fixed {boundary} failure")
    if boundary == "sending_read":
        store.sending_read_error = sentinel
    elif boundary == "sending_write":
        store.sending_write_error = sentinel
    elif boundary == "outreach_write":
        store.outreach_error = sentinel
    else:
        store.failure_at = boundary
        store.failure = sentinel
    item = (
        _item(
            "a",
            kind=EmailFeedbackKind.UNPARSEABLE,
            issue=EmailFeedbackParseIssue.MALFORMED,
        )
        if boundary == "quarantine"
        else _item("a")
    )
    with pytest.raises(RuntimeError) as raised:
        await _processor(store).process(
            TENANT,
            "feedback-primary",
            IDENTITY,
            None,
            EmailFeedbackPage(None, "next", (item,)),
        )
    assert raised.value is sentinel
    assert store.cursor is None
    assert store.receipts == store.quarantines == {}
    assert not store.sending_effects and not store.outreach_effects


@pytest.mark.asyncio
async def test_hard_effects_use_stable_domain_then_contact_phase_order() -> None:
    store = _Store()
    second_target = replace(_target(), contact_point_id=new_id("cp"))
    targets = iter((_target(), second_target))

    async def resolve(*_args: object, **_kwargs: object) -> object:
        return next(targets)

    uow = _Uow(store)
    uow.outreach.resolve_delivery_feedback = resolve

    class Factory:
        def __call__(self, _tenant: TenantId) -> _Uow:
            return uow

    processor = _flow().FeedbackPageProcessor(
        Factory(),
        route_id="route-v1",
        outreach_actor_factory=lambda identity: ("outreach", identity),
        sending_identity_actor_factory=lambda identity: ("sending", identity),
        now=lambda: NOW,
        critical_sink=lambda **_record: None,
    )
    await processor.process(
        TENANT,
        "feedback-primary",
        IDENTITY,
        None,
        EmailFeedbackPage(None, "next", (_item("b"), _item("a", ordinal=1))),
    )
    effects = [
        call for call in store.trace if call[0] in {"sending.record", "outreach.hard"}
    ]
    assert [call[0] for call in effects] == [
        "sending.record",
        "sending.record",
        "outreach.hard",
        "outreach.hard",
    ]
    assert [call[2] for call in effects[:2]] == sorted(call[2] for call in effects[:2])
    assert [call[1] for call in effects[2:]] == sorted(call[1] for call in effects[2:])
    assert [
        (receipt.provider_event_id, receipt.ordinal)
        for receipt in store.receipts.values()
    ] == [("b" * 64, 0), ("a" * 64, 1)]


@pytest.mark.asyncio
async def test_cross_route_alert_is_emitted_only_after_successful_commit() -> None:
    store = _Store()
    store.failure_at = "commit"
    store.failure = RuntimeError("fixed commit failure")
    critical: list[dict[str, object]] = []
    with pytest.raises(RuntimeError):
        await _processor(store, critical=critical).process(
            TENANT,
            "feedback-primary",
            IDENTITY,
            None,
            EmailFeedbackPage(
                None,
                "cursor-v1",
                (_item("a", correlation=_correlation(route_id="other-route")),),
            ),
        )
    assert critical == []
    assert store.receipts == store.quarantines == {}


@pytest.mark.asyncio
async def test_post_commit_alert_failure_does_not_turn_success_into_replay() -> None:
    store = _Store()

    def fail_alert(**_record: object) -> None:
        raise RuntimeError("private alert failure")

    processor = _flow().FeedbackPageProcessor(
        _Factory(store),
        route_id="route-v1",
        outreach_actor_factory=lambda identity: ("outreach", identity),
        sending_identity_actor_factory=lambda identity: ("sending", identity),
        now=lambda: NOW,
        critical_sink=fail_alert,
    )
    result = await processor.process(
        TENANT,
        "feedback-primary",
        IDENTITY,
        None,
        EmailFeedbackPage(
            None,
            "cursor-v1",
            (_item("a", correlation=_correlation(route_id="other-route")),),
        ),
    )
    assert (result.quarantined, store.cursor, store.version) == (1, "cursor-v1", 1)


@pytest.mark.asyncio
async def test_empty_page_advances_once_but_unchanged_empty_page_is_noop() -> None:
    store = _Store()
    result = await _processor(store).process(
        TENANT,
        "feedback-primary",
        IDENTITY,
        None,
        EmailFeedbackPage(None, "cursor-v1", ()),
    )
    assert result.processed == 0
    assert (store.cursor, store.version, store.uow_entries) == ("cursor-v1", 1, 1)
    no_op = await _processor(store).process(
        TENANT,
        "feedback-primary",
        IDENTITY,
        "cursor-v1",
        EmailFeedbackPage("cursor-v1", "cursor-v1", ()),
    )
    assert no_op == _flow().FeedbackPageResult(0, 0, 0, 0, 0, "cursor-v1")
    assert (store.version, store.uow_entries) == (1, 1)


def test_page_result_repr_hides_private_cursor() -> None:
    result = _flow().FeedbackPageResult(1, 2, 3, 4, 5, "private-cursor-token")

    assert "private-cursor-token" not in repr(result)


def test_public_contract_exports_only_typed_flow_surface() -> None:
    module = _flow()
    assert set(module.__all__) == {"FeedbackPageProcessor", "FeedbackPageResult"}
    sending = importlib.import_module("domains.sending_identity.service")
    outreach = importlib.import_module("domains.outreach.service")
    assert sending.DeliveryEventType.HARD_BOUNCED.value == "hard_bounced"
    assert issubclass(outreach.MessageAttemptConflictError, Exception)
