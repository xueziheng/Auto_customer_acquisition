"""客户数量单位：完整来源绑定，不改变旧需求门槛。"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Self

import pytest
from pydantic import ValidationError as SchemaError

from domains.demand import schemas, service
from domains.demand.errors import NeedUnitError, NeedUnitPermissionError
from shared.schemas.identifiers import EmployeeId, TenantId, ValidatedNeedId
from shared.schemas.money import Money
from shared.schemas.provenance import FactualField, Provenance, SourceType

NOW = datetime(2026, 8, 28, tzinfo=UTC)
TENANT = TenantId("tenant_test")
NEED = ValidatedNeedId("need_test")
ACTOR = EmployeeId("emp_test")


def field[T](value: T) -> FactualField[T]:
    """受控客户确认事实。"""
    return FactualField(
        value,
        Provenance(
            SourceType.CONVERSATION,
            "msg_customer_1",
            str(ACTOR),
            NOW,
            ACTOR,
            NOW,
            source_quote="We need 500 pieces.",
        ),
    )


def command(**changes: object) -> schemas.NeedUnitConfirmationCommand:
    """独立命令，不接受客户端确认人。"""
    assert hasattr(schemas, "NeedUnitConfirmationCommand"), "缺少单位确认契约"
    return schemas.NeedUnitConfirmationCommand.model_validate(
        {
            "unit": "pieces",
            "source_message_id": "msg_customer_1",
            "locator": "body:0:19",
            "source_quote": "We need 500 pieces.",
            "expected_quantity_fact_hash": "a" * 64,
            "expected_unit_confirmation_id": None,
            **changes,
        }
    )


def test_new_source_changes_quantity_hash() -> None:
    """同值换来源或确认时间必须使绑定失效。"""
    assert hasattr(service, "quantity_fact_hash"), "缺少数量事实哈希契约"
    old = field(500)
    for change in (
        {"source_id": "msg_customer_2"},
        {"confirmed_at": NOW + timedelta(days=1)},
        {"extracted_at": NOW + timedelta(seconds=1)},
        {"extracted_by": "emp_other"},
        {"confirmed_by": EmployeeId("emp_other")},
        {"source_quote": "We need exactly 500 pieces."},
        {"source_url": "https://example.test/source"},
        {"page_hash": "b" * 64},
        {"source_type": SourceType.UPLOAD},
    ):
        new = replace(old, provenance=replace(old.provenance, **change))
        assert service.quantity_fact_hash(
            TENANT, NEED, old
        ) != service.quantity_fact_hash(
            TENANT,
            NEED,
            new,
        )


def test_unit_is_not_an_old_model_field_or_a_default() -> None:
    """新增单位不能污染旧模型提取/更新入口。"""
    assert "unit" not in service.mutable_need_field_names()
    assert "unit" not in service.promotable_need_field_names()
    need = importlib.import_module("domains.demand.models").ValidatedNeed
    assert "unit" in need.__dataclass_fields__, "缺少可空单位事实"
    assert need.__dataclass_fields__["unit"].default is None


def test_command_cannot_claim_confirmation() -> None:
    """多出的确认人字段应被拒绝而不是忽略。"""
    with pytest.raises(SchemaError):
        command(confirmed_by="boss")


@pytest.mark.parametrize("unit", ["", " pieces", "pieces ", "x" * 65, "pie\x00ces"])
def test_command_rejects_malformed_unit(unit: str) -> None:
    with pytest.raises(SchemaError):
        command(unit=unit)


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_quantity_fact_hash": "A" * 64},
        {"expected_quantity_fact_hash": "a" * 63},
        {"expected_quantity_fact_hash": "a" * 64 + "\n"},
        {"source_message_id": "m" * 41},
        {"locator": "l" * 257},
        {"source_quote": "q" * 4097},
        {"source_quote": "We need\t500 pieces."},
    ],
)
def test_command_resource_and_hash_shape(changes: dict[str, object]) -> None:
    """输入资源保护与严格hash不能被换行、截断或大文本绕过。"""
    with pytest.raises(SchemaError):
        command(**changes)


@pytest.mark.parametrize("value", [True, 2.5, "500"])
def test_quantity_hash_never_coerces(value: object) -> None:
    assert hasattr(service, "quantity_fact_hash"), "缺少数量事实哈希契约"
    with pytest.raises(Exception) as caught:
        service.quantity_fact_hash(TENANT, NEED, field(value))
    assert getattr(caught.value, "code", None) == "quantity_invalid"


def facts(**changes: object) -> schemas.NeedQuoteFacts:
    """全字段显式提供，缺失不补造来源。"""
    return schemas.NeedQuoteFacts.model_validate(
        {
            "tenant_id": TENANT,
            "need_id": NEED,
            "account_id": "acct_test",
            "status": "validated",
            "product_category": field("hinges"),
            "application": None,
            "material": None,
            "size_spec": None,
            "packaging": None,
            "destination": None,
            "current_supply_issue": None,
            "certification_required": None,
            "unit": None,
            "quantity": field(500),
            "required_by": None,
            "target_price": None,
            "unit_quantity_fact_hash": None,
            "unit_confirmation_id": None,
            **changes,
        }
    )


def bound_facts(**changes: object) -> schemas.NeedQuoteFacts:
    """当前人工单位绑定。"""
    return facts(
        unit=field("pieces"),
        unit_confirmation_id="nuc_first",
        unit_quantity_fact_hash=service.quantity_fact_hash(TENANT, NEED, field(500)),
        **changes,
    )


def test_current_unit_and_full_hash() -> None:
    before = bound_facts()
    assert service.require_current_unit(before).value == "pieces"
    digest = service.need_quote_facts_hash(before)
    assert digest != service.need_quote_facts_hash(
        before.model_copy(
            update={
                "target_price": field(Money(Decimal("1.23"), "USD")),
            }
        )
    )
    assert digest == service.need_quote_facts_hash(before.model_copy())


@pytest.mark.parametrize(
    "change,code",
    [
        ({"unit": None, "unit_confirmation_id": None}, "unit_missing"),
        ({"quantity": field(0)}, "quantity_invalid"),
        ({"quantity": field(600)}, "unit_stale"),
        (
            {
                "quantity": replace(
                    field(500),
                    provenance=replace(field(500).provenance, source_id="msg_other"),
                )
            },
            "unit_stale",
        ),
        (
            {
                "quantity": replace(
                    field(500),
                    provenance=replace(
                        field(500).provenance, confirmed_by=None, confirmed_at=None
                    ),
                )
            },
            "fact_unconfirmed",
        ),
        (
            {
                "unit": replace(
                    field("pieces"),
                    provenance=replace(
                        field("pieces").provenance, confirmed_by=None, confirmed_at=None
                    ),
                )
            },
            "fact_unconfirmed",
        ),
    ],
)
def test_current_unit_gate(change: dict, code: str) -> None:
    with pytest.raises(NeedUnitError) as caught:
        service.require_current_unit(bound_facts().model_copy(update=change))
    assert caught.value.code == code


def test_timezone_hash_and_zero_legacy() -> None:
    old = field(0)
    adjusted = replace(
        old,
        provenance=replace(
            old.provenance,
            extracted_at=NOW.astimezone(timezone(timedelta(hours=8))),
            confirmed_at=NOW.astimezone(timezone(timedelta(hours=8))),
        ),
    )
    assert service.quantity_fact_hash(TENANT, NEED, old) == service.quantity_fact_hash(
        TENANT, NEED, adjusted
    )


@pytest.mark.parametrize("level", range(6))
@pytest.mark.parametrize("quantity", [0, 500])
def test_old_completeness_ignores_unit(level: int, quantity: int) -> None:
    need_type = importlib.import_module("domains.demand.models").ValidatedNeed
    need = need_type(
        need_id=NEED,
        tenant_id=TENANT,
        account_id="acct_test",
        product_category=field("hinges") if level >= 1 else None,
        source_message_id="msg_customer_1",
        created_at=NOW,
        application=field("doors") if level >= 2 else None,
        quantity=field(quantity) if level >= 3 else None,
        destination=field("DE") if level >= 4 else None,
        required_by=field(date(2026, 12, 1)) if level >= 4 else None,
        material=field("steel") if level == 5 else None,
        size_spec=field("M8") if level == 5 else None,
    )
    assert need.completeness == level
    assert replace(need, unit=field("pieces")).completeness == level


class MemoryCase:
    """copy-on-enter事务；外部IO替身明确监测持锁深度。"""

    def __init__(self) -> None:
        self.facts: schemas.NeedQuoteFacts | None = facts()
        self.records: dict[str, schemas.NeedUnitStoredConfirmation] = {}
        self.history: list[
            tuple[FactualField[str] | None, schemas.NeedUnitConfirmationView]
        ] = []
        self.depth = self.guard_depth = self.reads = self.reference_reads = 0
        self.deny_check = self.deny_guard = self.deny_reference = self.fail_history = (
            False
        )
        self.source_changes: dict[str, object] = {}
        self.on_read: Callable[[], None] | None = None
        self.on_lock: Callable[[MemoryUow], None] | None = None

    def factory(self, tenant_id: TenantId) -> MemoryUow:
        """每次返回独立短事务。"""
        assert tenant_id == TENANT
        return MemoryUow(self)

    async def check(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        actor_id: EmployeeId,
        *,
        action: schemas.NeedUnitAction,
    ) -> schemas.NeedUnitAccess:
        """受控当前权限，不以模型角色声明授权。"""
        if self.deny_check:
            raise NeedUnitPermissionError("permission_denied")
        return schemas.NeedUnitAccess(
            tenant_id=tenant_id,
            need_id=need_id,
            actor_id=actor_id,
            opportunity_id="opp_test",
            account_id="acct_test",
            authorization_ref="controlled",
        )

    @asynccontextmanager
    async def guard(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        actor_id: EmployeeId,
        *,
        action: schemas.NeedUnitAction,
    ) -> AsyncIterator[schemas.NeedUnitAccess]:
        """模拟当前权限保护生命周期。"""
        if self.deny_guard:
            raise NeedUnitPermissionError("permission_denied")
        self.guard_depth += 1
        try:
            yield await self.check(tenant_id, need_id, actor_id, action=action)
        finally:
            self.guard_depth -= 1

    async def read_verified(
        self, query: schemas.NeedUnitEvidenceQuery
    ) -> schemas.VerifiedNeedUnitEvidence:
        """来源IO必须发生在事务与guard外。"""
        assert self.depth == self.guard_depth == 0, "来源IO不得持锁"
        self.reads += 1
        if self.on_read:
            self.on_read()
        return schemas.VerifiedNeedUnitEvidence.model_validate(
            {
                "tenant_id": query.tenant_id,
                "need_id": query.need_id,
                "account_id": query.account_id,
                "source_message_id": query.source_message_id,
                "artifact_id": "art_test",
                "content_hash": "b" * 64,
                "locator": query.locator,
                "source_quote": query.source_quote,
                "unit": query.unit,
                "quantity_fact_hash": query.quantity_fact_hash,
                "observed_at": NOW,
                **self.source_changes,
            }
        )

    async def authorize_reference(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        actor_id: EmployeeId,
        source: schemas.VerifiedNeedUnitEvidence,
    ) -> None:
        """重放不取原文但必须重验当前阅读权。"""
        assert self.depth == self.guard_depth == 0
        self.reference_reads += 1
        if self.deny_reference:
            raise NeedUnitPermissionError("permission_denied")


class MemoryUow:
    """所有接口在副本上写入，异常不污染已提交状态。"""

    def __init__(self, case: MemoryCase) -> None:
        self.case = case
        self.units = self

    async def __aenter__(self) -> Self:
        """进入时复制已提交状态。"""
        self.case.depth += 1
        self.facts, self.records, self.history = deepcopy(
            (self.case.facts, self.case.records, self.case.history)
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        """只在成功退出时提交完整副本。"""
        if exc_type is None:
            self.case.facts, self.case.records, self.case.history = (
                self.facts,
                self.records,
                self.history,
            )
        self.case.depth -= 1

    async def read_facts(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> schemas.NeedQuoteFacts | None:
        """返回事务当前事实。"""
        return self.facts

    async def lock_facts(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> schemas.NeedQuoteFacts | None:
        """受控锁内竞争钩子用于重验测试。"""
        if self.case.on_lock:
            self.case.on_lock(self)
        return self.facts

    async def find_operation(
        self, tenant_id: TenantId, need_id: ValidatedNeedId, idempotency_key: str
    ) -> schemas.NeedUnitStoredConfirmation | None:
        """从事务副本读幂等记录。"""
        return self.records.get(idempotency_key)

    async def get_confirmation(
        self, tenant_id: TenantId, need_id: ValidatedNeedId, confirmation_id: str
    ) -> schemas.NeedUnitConfirmationView | None:
        """按确认ID读历史副本。"""
        return next(
            (
                r.view
                for r in self.records.values()
                if r.view.confirmation_id == confirmation_id
            ),
            None,
        )

    async def add_confirmation(
        self, tenant_id: TenantId, record: schemas.NeedUnitStoredConfirmation
    ) -> None:
        """仅修改事务副本。"""
        self.records[record.idempotency_key] = record

    async def apply_current_unit(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        confirmation: schemas.NeedUnitConfirmationView,
    ) -> None:
        """单位与两个绑定字段一起写副本。"""
        self.facts = self.facts.model_copy(
            update={
                "unit": confirmation.unit,
                "unit_confirmation_id": confirmation.confirmation_id,
                "unit_quantity_fact_hash": confirmation.quantity_fact_hash,
            }
        )

    async def append_unit_history(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        previous_unit: FactualField[str] | None,
        confirmation: schemas.NeedUnitConfirmationView,
    ) -> None:
        """历史错误必须让三个写入全部回滚。"""
        if self.case.fail_history:
            raise NeedUnitError("invalid_input")
        self.history.append((previous_unit, confirmation))


def unit_service(case: MemoryCase) -> service.NeedUnitService:
    try:
        implementation = importlib.import_module(
            "domains.demand.unit_service"
        ).NeedUnitServiceImpl
    except ModuleNotFoundError:
        pytest.fail("缺少单位服务实现")
    return implementation(case.factory, case, case, now=lambda: NOW)


async def confirm(
    svc: service.NeedUnitService, **changes: object
) -> schemas.NeedUnitConfirmationView:
    """所有断言走公开服务。"""
    return await svc.confirm(
        TENANT,
        NEED,
        command(
            expected_quantity_fact_hash=service.quantity_fact_hash(
                TENANT, NEED, field(500)
            ),
            **changes,
        ),
        actor_id=ACTOR,
        idempotency_key="one",
    )


async def test_confirm_atomic_receipt_and_reference_replay() -> None:
    case = MemoryCase()
    svc = unit_service(case)
    first = await confirm(svc)
    assert first.unit.value == "pieces"
    assert first.unit.provenance.confirmed_by == ACTOR
    assert first.unit.provenance.extracted_at == first.confirmed_at == NOW
    assert len(case.records) == len(case.history) == 1
    assert service.require_current_unit(case.facts) == first.unit
    assert await confirm(svc) == first
    assert case.reads == case.reference_reads == 1
    case.deny_reference = True
    with pytest.raises(NeedUnitPermissionError):
        await confirm(svc)
    with pytest.raises(NeedUnitPermissionError):
        await svc.get_confirmation(TENANT, NEED, first.confirmation_id, actor_id=ACTOR)


@pytest.mark.parametrize("mode", ["deny_check", "deny_guard", "fail_history"])
async def test_failures_leave_no_receipt_history_or_unit(mode: str) -> None:
    case = MemoryCase()
    setattr(case, mode, True)
    with pytest.raises((NeedUnitError, NeedUnitPermissionError)):
        await confirm(unit_service(case))
    assert not case.records and not case.history and case.facts.unit is None
    if mode == "deny_check":
        assert case.reads == 0


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "other"},
        {"need_id": "other"},
        {"account_id": "other"},
        {"source_message_id": "other"},
        {"unit": "boxes"},
        {"locator": "other"},
        {"source_quote": "Another quote."},
        {"quantity_fact_hash": "c" * 64},
    ],
)
async def test_reader_binding_mismatch_never_writes(change: dict) -> None:
    case = MemoryCase()
    case.source_changes = change
    with pytest.raises(NeedUnitError) as caught:
        await confirm(unit_service(case))
    assert caught.value.code == "source_mismatch"
    assert not case.records and not case.history


async def test_replay_cannot_reactivate_stale_unit_or_change_payload() -> None:
    case = MemoryCase()
    svc = unit_service(case)
    first = await confirm(svc)
    case.facts = case.facts.model_copy(update={"quantity": field(600)})
    assert await confirm(svc) == first
    with pytest.raises(NeedUnitError) as caught:
        service.require_current_unit(case.facts)
    assert caught.value.code == "unit_stale"
    with pytest.raises(NeedUnitError) as caught:
        await confirm(svc, unit="boxes")
    assert caught.value.code == "idempotency_conflict"


@pytest.mark.parametrize("unit", ["unit", "UNITS", "unknown", "未知", "待确认"])
async def test_unspecified_unit_rejected(unit: str) -> None:
    case = MemoryCase()
    with pytest.raises(NeedUnitError) as caught:
        await confirm(unit_service(case), unit=unit)
    assert caught.value.code == "unit_unspecified"
    assert case.reads == 0


async def test_quantity_changed_during_source_read_blocks_write() -> None:
    case = MemoryCase()
    case.on_read = lambda: setattr(case, "facts", facts(quantity=field(600)))
    with pytest.raises(NeedUnitError) as caught:
        await confirm(unit_service(case))
    assert caught.value.code == "quantity_changed"
    assert not case.records


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("tenant_id", "x" * 41),
        ("need_id", " bad"),
        ("account_id", ""),
    ],
)
def test_all_fact_identity_fields_are_validated(field_name: str, value: str) -> None:
    with pytest.raises(SchemaError):
        facts(**{field_name: value})


def test_provenance_times_must_be_aware_and_normalized() -> None:
    with pytest.raises(SchemaError):
        facts(
            quantity=replace(
                field(500),
                provenance=replace(
                    field(500).provenance, extracted_at=NOW.replace(tzinfo=None)
                ),
            )
        )
    shifted = replace(
        field(500),
        provenance=replace(
            field(500).provenance,
            extracted_at=NOW.astimezone(timezone(timedelta(hours=8))),
        ),
    )
    assert facts(quantity=shifted).quantity.provenance.extracted_at.tzinfo == UTC


@pytest.mark.parametrize("state", ["fulfilled", "withdrawn", "lost"])
async def test_terminal_need_rejects_without_source_io(state: str) -> None:
    case = MemoryCase()
    case.facts = facts(status=state)
    with pytest.raises(NeedUnitError) as caught:
        await confirm(unit_service(case))
    assert caught.value.code == "need_terminal" and case.reads == 0


async def test_second_key_with_same_expected_id_fails() -> None:
    case = MemoryCase()
    svc = unit_service(case)
    await confirm(svc)
    with pytest.raises(NeedUnitError) as caught:
        await svc.confirm(
            TENANT,
            NEED,
            command(
                expected_quantity_fact_hash=service.quantity_fact_hash(
                    TENANT, NEED, field(500)
                )
            ),
            actor_id=ACTOR,
            idempotency_key="another",
        )
    assert caught.value.code == "unit_changed"
    assert len(case.records) == len(case.history) == 1


async def test_missing_need_and_history_are_fixed_errors() -> None:
    case = MemoryCase()
    svc = unit_service(case)
    case.facts = None
    with pytest.raises(NeedUnitError) as caught:
        await svc.get_facts(TENANT, NEED, actor_id=ACTOR)
    assert caught.value.code == "need_not_found"
    with pytest.raises(NeedUnitError) as caught:
        await svc.get_confirmation(TENANT, NEED, "nuc_missing", actor_id=ACTOR)
    assert caught.value.code == "confirmation_not_found"


async def test_permission_and_dependency_exceptions_are_fixed_codes() -> None:
    from domains.demand.errors import NeedUnitUnavailableError
    from shared.errors import PermissionDenied

    case = MemoryCase()
    svc = unit_service(case)

    def denied() -> None:
        raise PermissionDenied("sensitive-source-message")

    case.on_read = denied
    with pytest.raises(NeedUnitPermissionError) as caught:
        await confirm(svc)
    assert caught.value.code == "permission_denied"
    assert "sensitive" not in str(caught.value)

    def unavailable() -> None:
        raise OSError("sensitive-network-error")

    case.on_read = unavailable
    with pytest.raises(NeedUnitUnavailableError) as caught:
        await confirm(svc)
    assert caught.value.code == "source_unavailable"
    assert "sensitive" not in str(caught.value)


async def test_confirmation_dto_rejects_inconsistent_receipt_source() -> None:
    case = MemoryCase()
    first = await confirm(unit_service(case))
    payload = {name: getattr(first, name) for name in type(first).model_fields}
    payload["source"] = first.source.model_copy(update={"need_id": "need_other"})
    with pytest.raises(SchemaError):
        schemas.NeedUnitConfirmationView.model_validate(payload)


async def test_refresh_then_reconfirm_keeps_old_receipt_historical() -> None:
    """明确新数量hash/旧确认ID/新键才可重新确认，旧键不能回滚新事实。"""
    case = MemoryCase()
    svc = unit_service(case)
    first = await confirm(svc)
    changed_quantity = replace(
        field(600),
        provenance=replace(
            field(600).provenance,
            source_id="msg_customer_2",
            source_quote="We now need 600 pieces.",
        ),
    )
    case.facts = case.facts.model_copy(update={"quantity": changed_quantity})
    refreshed = command(
        source_message_id="msg_customer_2",
        source_quote="We now need 600 pieces.",
        expected_quantity_fact_hash=service.quantity_fact_hash(
            TENANT, NEED, changed_quantity
        ),
        expected_unit_confirmation_id=first.confirmation_id,
    )
    second = await svc.confirm(
        TENANT, NEED, refreshed, actor_id=ACTOR, idempotency_key="second"
    )
    assert second.confirmation_id != first.confirmation_id
    assert (
        service.require_current_unit(case.facts).provenance.source_id
        == "msg_customer_2"
    )
    assert await confirm(svc) == first
    assert case.facts.unit_confirmation_id == second.confirmation_id
    assert len(case.records) == len(case.history) == 2


async def test_winner_found_after_lock_rechecks_source_access_outside_guard() -> None:
    """模拟短读后并发winner出现，重放必须退出两个持锁区再校验原件权限。"""
    case = MemoryCase()
    svc = unit_service(case)
    winner = await confirm(svc)
    stored = case.records["one"]
    case.facts, case.records, case.history = facts(), {}, []

    def concurrent_winner(uow: MemoryUow) -> None:
        uow.records["one"] = stored
        uow.facts = bound_facts().model_copy(
            update={"unit_confirmation_id": winner.confirmation_id}
        )

    case.on_lock = concurrent_winner
    case.deny_reference = True
    with pytest.raises(NeedUnitPermissionError):
        await confirm(svc)
    assert case.reference_reads == 1 and case.depth == case.guard_depth == 0
    assert case.facts.unit_confirmation_id == winner.confirmation_id
