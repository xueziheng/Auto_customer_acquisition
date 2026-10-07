"""One-click unsubscribe capability token 的安全与幂等合同。"""

from __future__ import annotations

import importlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Self

import pytest

from domains.outreach.schemas import MessageSendPreflight
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    EnrollmentId,
    IdempotencyKey,
    MessageAttemptId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
)

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
TENANT = TenantId("tn_01HZX3S5Y3V9BAS7X9C04S0A00")


def _module() -> object:
    try:
        return importlib.import_module("workflows.email_feedback.unsubscribe")
    except ModuleNotFoundError as exc:
        pytest.fail(f"缺少 one-click unsubscribe 服务：{exc}")


def _preflight(**changes: object) -> MessageSendPreflight:
    values: dict[str, object] = {
        "tenant_id": TENANT,
        "attempt_id": MessageAttemptId("mat_01HZX3S5Y3V9BAS7X9C04S0A00"),
        "campaign_id": CampaignId("cmp_01HZX3S5Y3V9BAS7X9C04S0A00"),
        "enrollment_id": EnrollmentId("enr_01HZX3S5Y3V9BAS7X9C04S0A00"),
        "account_id": ProspectAccountId("acc_01HZX3S5Y3V9BAS7X9C04S0A00"),
        "contact_point_id": ContactPointId("cp_01HZX3S5Y3V9BAS7X9C04S0A00"),
        "sending_identity_id": SendingIdentityId(
            "sid_01HZX3S5Y3V9BAS7X9C04S0A00"
        ),
        "campaign_version": 1,
        "step_number": 1,
        "idempotency_key": IdempotencyKey("send-key-01"),
    }
    values.update(changes)
    return MessageSendPreflight(**values)  # type: ignore[arg-type]


class _Metrics:
    def __init__(self) -> None:
        self.results: list[str] = []

    def increment(self, result: str) -> None:
        assert result in {"valid", "expired", "invalid"}
        self.results.append(result)


class _TokenRepo:
    def __init__(self, store: _Store) -> None:
        self.store = store

    async def add_if_absent(self, record: object) -> bool:
        digest = record.nonce_sha256
        if digest in self.store.tokens:
            return False
        self.store.tokens[digest] = record
        return True

    async def get_for_update(self, tenant_id: TenantId, digest: bytes) -> object | None:
        assert tenant_id == TENANT
        return self.store.tokens.get(digest)

    async def mark_consumed(self, record: object, at: datetime) -> bool:
        current = self.store.tokens.get(record.nonce_sha256)
        if current is None or current.consumed_at is not None:
            return False
        self.store.tokens[record.nonce_sha256] = replace(current, consumed_at=at)
        return True


class _Outreach:
    def __init__(self, store: _Store) -> None:
        self.store = store

    async def add_suppression(
        self, tenant_id: TenantId, request: object, *, actor: object
    ) -> object:
        self.store.suppressions.append((tenant_id, request, actor))
        return SimpleNamespace(created=True)


class _Store:
    def __init__(self) -> None:
        self.tokens: dict[bytes, object] = {}
        self.suppressions: list[tuple[object, ...]] = []
        self.entries = 0


class _Uow:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.tokens = _TokenRepo(store)
        self.outreach = _Outreach(store)

    async def __aenter__(self) -> Self:
        self.store.entries += 1
        self.snapshot = (dict(self.store.tokens), list(self.store.suppressions))
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is not None:
            self.store.tokens, self.store.suppressions = self.snapshot


def _service(
    store: _Store,
    *,
    nonces: list[bytes] | None = None,
    now: datetime = NOW,
    keys: dict[str, bytes] | None = None,
    active: str = "current-v1",
    base_url: str = "https://unsubscribe.example.test",
) -> tuple[object, _Metrics]:
    module = _module()
    values = iter(nonces or [b"n" * 32])
    metrics = _Metrics()
    service = module.UnsubscribeServiceImpl(
        tenant_id=TENANT,
        uow_factory=lambda tenant: _Uow(store),
        key_ring=module.UnsubscribeKeyRing(
            active_key_id=active,
            keys=keys or {"current-v1": b"k" * 32, "old-v1": b"o" * 32},
        ),
        base_url=base_url,
        actor_factory=lambda contact: ("unsubscribe", contact),
        now=lambda: now,
        nonce_factory=lambda: next(values),
        metrics=metrics,
    )
    return service, metrics


@pytest.mark.asyncio
async def test_issue_stores_only_nonce_hash_and_token_has_no_business_identifier() -> None:
    store = _Store()
    service, _ = _service(store)
    preflight = _preflight()

    link = await service.issue(TENANT, preflight)

    assert link.tenant_id == TENANT
    assert link.attempt_id == preflight.attempt_id
    assert link.contact_point_id == preflight.contact_point_id
    assert link.active_key_id == "current-v1"
    assert link.policy_version == "unsubscribe-v1"
    for marker in ("cp_", "mat_", "tn_", "n" * 32):
        assert marker not in link.url
        assert marker not in repr(link)
    record = next(iter(store.tokens.values()))
    assert record.nonce_sha256 != b"n" * 32
    assert record.created_at == NOW
    assert record.expires_at == NOW + timedelta(days=90)
    assert record.key_id == "current-v1"


@pytest.mark.asyncio
async def test_consume_is_one_time_and_metrics_use_only_fixed_vocabulary() -> None:
    store = _Store()
    service, metrics = _service(store)
    link = await service.issue(TENANT, _preflight())
    token = link.url.rsplit("/", 1)[1]

    assert await service.consume(token) is True
    assert await service.consume(token) is False
    assert len(store.suppressions) == 1
    request = store.suppressions[0][1]
    assert request.target.contact_point_id == _preflight().contact_point_id
    assert request.reason.value == "unsubscribe"
    assert request.source_ref == request.idempotency_key
    assert metrics.results == ["valid", "invalid"]


@pytest.mark.asyncio
async def test_exact_expiry_is_expired_without_suppression() -> None:
    store = _Store()
    issuer, _ = _service(store)
    token = (await issuer.issue(TENANT, _preflight())).url.rsplit("/", 1)[1]
    consumer, metrics = _service(store, now=NOW + timedelta(days=90))

    assert await consumer.consume(token) is False
    assert store.suppressions == []
    assert metrics.results == ["expired"]


@pytest.mark.asyncio
async def test_key_id_is_signed_even_when_two_ids_share_equal_key_bytes() -> None:
    store = _Store()
    service, metrics = _service(
        store,
        keys={"current-v1": b"k" * 32, "other-v1": b"k" * 32},
    )
    token = (await service.issue(TENANT, _preflight())).url.rsplit("/", 1)[1]
    changed = token.replace("current-v1.", "other-v1.", 1)

    assert await service.consume(changed) is False
    assert metrics.results == ["invalid"]
    assert store.suppressions == []


@pytest.mark.asyncio
async def test_verification_only_key_can_consume_token_issued_before_rotation() -> None:
    store = _Store()
    keys = {"current-v1": b"k" * 32, "old-v1": b"o" * 32}
    old_issuer, _ = _service(store, keys=keys, active="old-v1")
    token = (await old_issuer.issue(TENANT, _preflight())).url.rsplit("/", 1)[1]
    rotated_consumer, metrics = _service(store, keys=keys, active="current-v1")

    assert await rotated_consumer.consume(token) is True
    assert metrics.results == ["valid"]


@pytest.mark.asyncio
async def test_signature_verification_uses_constant_time_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    store = _Store()
    service, _ = _service(store)
    token = (await service.issue(TENANT, _preflight())).url.rsplit("/", 1)[1]
    key_id, nonce, signature = token.split(".")
    changed = f"{key_id}.{nonce}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    calls: list[tuple[bytes, bytes]] = []
    original = module.hmac.compare_digest

    def compare(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr(module.hmac, "compare_digest", compare)

    assert await service.consume(changed) is False
    assert len(calls) == 1
    assert all(len(value) == 32 for value in calls[0])


@pytest.mark.asyncio
async def test_signature_is_bound_to_runtime_tenant_before_repository_access() -> None:
    module = _module()
    store = _Store()
    issuer, _ = _service(store)
    token = (await issuer.issue(TENANT, _preflight())).url.rsplit("/", 1)[1]
    entries_before = store.entries
    other_tenant = TenantId("tn_01HZX3S5Y3V9BAS7X9C04S0A01")
    consumer = module.UnsubscribeServiceImpl(
        tenant_id=other_tenant,
        uow_factory=lambda tenant: _Uow(store),
        key_ring=module.UnsubscribeKeyRing(
            "current-v1", {"current-v1": b"k" * 32, "old-v1": b"o" * 32}
        ),
        base_url="https://unsubscribe.example.test",
        actor_factory=lambda contact: ("unsubscribe", contact),
        now=lambda: NOW,
    )

    assert await consumer.consume(token) is False
    assert store.entries == entries_before
    assert store.suppressions == []


@pytest.mark.asyncio
async def test_nonce_hash_collision_regenerates_without_overwriting_winner() -> None:
    store = _Store()
    first, _ = _service(store, nonces=[b"a" * 32])
    first_link = await first.issue(TENANT, _preflight())
    winner = dict(store.tokens)
    second, _ = _service(store, nonces=[b"a" * 32, b"b" * 32])

    second_link = await second.issue(
        TENANT,
        _preflight(
            attempt_id=MessageAttemptId("mat_01HZX3S5Y3V9BAS7X9C04S0A01"),
            contact_point_id=ContactPointId("cp_01HZX3S5Y3V9BAS7X9C04S0A01"),
        ),
    )

    assert first_link.url != second_link.url
    assert len(store.tokens) == 2
    assert all(store.tokens[key] is value for key, value in winner.items())


@pytest.mark.asyncio
async def test_collision_attempt_cap_fails_without_overwrite() -> None:
    store = _Store()
    issuer, _ = _service(store, nonces=[b"a" * 32])
    await issuer.issue(TENANT, _preflight())
    before = dict(store.tokens)
    colliding, _ = _service(store, nonces=[b"a" * 32] * 3)

    with pytest.raises(ValidationError, match="退订 token 生成失败"):
        await colliding.issue(TENANT, _preflight())
    assert store.tokens == before


@pytest.mark.parametrize(
    ("active", "keys"),
    [
        ("UPPER", {"UPPER": b"k" * 32}),
        ("missing", {"current-v1": b"k" * 32}),
        ("current-v1", {"current-v1": b"short"}),
        ("current-v1", {"current-v1": b"k" * 32, "current-v1 ": b"x" * 32}),
    ],
)
def test_key_ring_rejects_invalid_configuration(
    active: str, keys: dict[str, bytes]
) -> None:
    with pytest.raises(ValidationError):
        _module().UnsubscribeKeyRing(active_key_id=active, keys=keys)


def test_key_ring_copies_and_freezes_verification_keys() -> None:
    configured = {"current-v1": b"k" * 32}
    ring = _module().UnsubscribeKeyRing("current-v1", configured)
    configured["current-v1"] = b"x" * 32
    assert ring.key("current-v1") == b"k" * 32
    with pytest.raises(TypeError):
        ring.keys["other-v1"] = b"o" * 32


@pytest.mark.parametrize(
    "base_url",
    (
        "https://",
        "https://UNSUBSCRIBE.example.test",
        "https://unsubscribe.example.test:443",
        "https://unsubscribe.example.test/",
        "https://unsubscribe.example.test/path",
        "http://unsubscribe.example.test",
        "https://user@unsubscribe.example.test",
    ),
)
def test_service_rejects_noncanonical_or_unsafe_origin(base_url: str) -> None:
    with pytest.raises(ValidationError, match="退订服务配置无效"):
        _service(_Store(), base_url=base_url)


@pytest.mark.asyncio
async def test_consume_rejects_non_utc_clock_before_repository_access() -> None:
    store = _Store()
    issuer, _ = _service(store)
    token = (await issuer.issue(TENANT, _preflight())).url.rsplit("/", 1)[1]
    entries_before = store.entries
    consumer, _ = _service(store, now=NOW.replace(tzinfo=None))

    with pytest.raises(ValidationError, match="退订时钟无效"):
        await consumer.consume(token)
    assert store.entries == entries_before
    assert store.suppressions == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token",
    ("", "a", "a.b.c.d", "current-v1.%%%%.%%%%", "x" * 1000, "a.b.\x00"),
)
async def test_malformed_token_is_indistinguishable_and_never_opens_uow(
    token: str,
) -> None:
    store = _Store()
    service, metrics = _service(store)

    assert await service.consume(token) is False
    assert store.entries == 0
    assert metrics.results == ["invalid"]
    if len(token) > 16:
        assert token not in repr(service)
