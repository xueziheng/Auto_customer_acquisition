"""文件用途独立于成本角色及审批决定人；当前scope不能由格式推导。"""

import asyncio
from contextlib import asynccontextmanager

import pytest

from domains.quotations.file_access import (
    QuoteFileAccessServiceImpl,
    require_quote_file_scope,
)
from domains.quotations.file_access_schemas import QuoteFileScopeFacts
from shared.schemas.quote_facts import QuoteEmployeeFact


def scope(
    role="sales",
    *,
    actor="emp_owner",
    active=True,
    owner_active=True,
    manager="emp_manager",
):
    return QuoteFileScopeFacts(
        tenant_id="tn_00000000000000000000000001",
        opportunity_id="opp_existing",
        actor=QuoteEmployeeFact(
            tenant_id="tn_00000000000000000000000001",
            employee_id=actor,
            role=role,
            is_active=active,
            manager_id=None,
            team_id=None,
        ),
        owner=QuoteEmployeeFact(
            tenant_id="tn_00000000000000000000000001",
            employee_id="emp_owner",
            role="sales",
            is_active=owner_active,
            manager_id=manager,
            team_id=None,
        ),
    )


@pytest.mark.parametrize(
    "role,actor",
    [("sales", "emp_owner"), ("manager", "emp_manager"), ("boss", "legacy_boss")],
)
def test_only_current_file_scope_can_pass(role, actor):
    assert require_quote_file_scope(scope(role, actor=actor)) is None


def test_scope_preserves_original_opportunity_id():
    facts = scope()
    assert (facts.tenant_id, facts.opportunity_id, facts.actor.employee_id) == ("tn_00000000000000000000000001", "opp_existing", "emp_owner")


@pytest.mark.parametrize(
    "options",
    [
        {"role": role, "actor": "emp_owner"}
        for role in ("product", "sourcing", "finance")
    ]
    + [
        {"actor": "other_sales"},
        {"active": False},
        {"owner_active": False},
        {"role": "manager", "actor": "not_direct_manager"},
    ],
)
def test_cost_roles_ownership_or_revocation_cannot_bypass_scope(options):
    with pytest.raises(Exception) as error:
        require_quote_file_scope(scope(**options))
    assert error.value.code == "permission_denied"


async def test_policy_cleanup_does_not_replace_primary_cancellation():
    class Policies:
        @asynccontextmanager
        async def open(self, tenant_id, category):
            yield object()
            raise RuntimeError("private cleanup detail")

    service = QuoteFileAccessServiceImpl(
        None,
        None,
        None,
        None,
        None,
        Policies(),
        None,
        None,
        now=lambda: None,
        template_version="quote_pdf_v1",
    )
    primary = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as error:
        async with service._policy_lease("tenant", "category"):
            raise primary
    assert error.value is primary


@pytest.mark.parametrize("kind,code", [
    ("missing", "context_changed"), ("other_domain", "storage_inconsistent"),
    ("corrupt", "storage_inconsistent"), ("unknown", "dependency_unavailable"),
])
async def test_issuer_failure_mapping_is_exact_not_exception_code_duck_typing(kind, code):
    from domains.quotations.errors import (
        QuotationError,
        QuotationUnavailableError,
        QuoteFileAccessError,
        QuoteFileAccessUnavailableError,
    )
    from domains.quotations.file_access import file_access_errors

    class UnknownReaderError(RuntimeError):
        code = "issuer_not_found"

    failure = {
        "missing": QuotationError("issuer_not_found"),
        "other_domain": QuotationError("invalid_input"),
        "corrupt": QuotationUnavailableError("storage_inconsistent"),
        "unknown": UnknownReaderError("private-reader-marker"),
    }[kind]
    expected = QuoteFileAccessError if kind == "missing" else QuoteFileAccessUnavailableError
    with pytest.raises(expected) as error:
        async with file_access_errors():
            raise failure
    assert error.value.code == code
    assert "private-reader-marker" not in str(error.value)


class ControlledCleanupSignal(BaseException):
    """受控终止信号，仅直接await并捕获，不向OS或后台Task发送。"""


@pytest.mark.parametrize("signal_type", [SystemExit, KeyboardInterrupt, GeneratorExit, ControlledCleanupSignal])
@pytest.mark.parametrize("cancel_first", [False, True])
@pytest.mark.parametrize("purpose", ["scope", "current"])
async def test_context_unknown_base_exception_is_never_replaced(signal_type, cancel_first, purpose):
    from types import SimpleNamespace

    from infra.db.quote_context import SqlAlchemyQuoteContextProvider

    signal = signal_type("controlled cleanup signal")
    cancellation = asyncio.CancelledError()

    class Session:
        async def __aenter__(self):
            return self

        async def execute(self, statement):
            if cancel_first:
                raise cancellation
            return SimpleNamespace(one_or_none=lambda: None)

        async def __aexit__(self, *args):
            raise signal

    provider = SqlAlchemyQuoteContextProvider(Session, None,
        lock_timeout_ms=1000, statement_timeout_ms=2500)
    args = ("tn_00000000000000000000000001", "opp_existing", "emp_owner")
    lease = (provider.open_file_scope(*args) if purpose == "scope" else
        provider.open_for_file(*args, prepared_by="emp_boss", decider_ids=("emp_decider",)))
    with pytest.raises(signal_type) as error:
        async with lease:
            pytest.fail("失败的bootstrap不可产出上下文")
    assert error.value is signal


@pytest.mark.parametrize("signal_type", [SystemExit, KeyboardInterrupt, GeneratorExit, ControlledCleanupSignal])
@pytest.mark.parametrize("purpose", ["scope", "current"])
@pytest.mark.parametrize("source,cancel_first", [
    ("bootstrap", False), ("business", False), ("rollback", False), ("rollback", True),
])
@pytest.mark.parametrize("later", ["sql", "validation", "cancel", "terminal"])
async def test_context_first_terminal_survives_later_cleanup(
    signal_type, purpose, source, cancel_first, later, caplog
):
    from types import SimpleNamespace

    from sqlalchemy.exc import SQLAlchemyError

    from infra.db.quote_context import SqlAlchemyQuoteContextProvider

    signal = signal_type("private-first-terminal")
    cancellation = asyncio.CancelledError("private-primary-cancel")
    subsequent = {"sql": SQLAlchemyError("private-later-sql"),
        "validation": ValueError("private-later-validation"),
        "cancel": asyncio.CancelledError("private-later-cancel"),
        "terminal": ControlledCleanupSignal("private-second-terminal")}[later]
    phases, cleanup = [], []

    class Session:
        def __init__(self):
            self.bootstrap = not phases
            phases.append(self)

        async def __aenter__(self):
            return self

        async def execute(self, *args):
            if self.bootstrap:
                if source == "bootstrap":
                    raise signal
                candidate = SimpleNamespace(owner="emp_owner", need_id="need_existing", account_id="acct_existing")
                return SimpleNamespace(one_or_none=lambda: candidate)
            # 只定位业务session首个await的异常控制流，不伪造员工/抬头授权事实。
            if source == "business":
                raise signal
            raise cancellation if cancel_first else RuntimeError("private-initial-failure")

        async def rollback(self):
            cleanup.append("rollback")
            raise signal if source == "rollback" else subsequent

        async def __aexit__(self, *args):
            if self.bootstrap and source != "bootstrap":
                return
            cleanup.append("close")
            if source == "business":
                raise SQLAlchemyError("private-last-close")
            raise subsequent

    class Issuer:
        async def get_confirmed(self, tenant):
            return object()

    provider = SqlAlchemyQuoteContextProvider(Session, Issuer(),
        lock_timeout_ms=1000, statement_timeout_ms=2500)
    args = ("tn_00000000000000000000000001", "opp_existing", "emp_owner")
    lease = (provider.open_file_scope(*args) if purpose == "scope" else
        provider.open_for_file(*args, prepared_by="emp_boss", decider_ids=("emp_decider",)))
    with pytest.raises(signal_type) as error:
        async with lease:
            pytest.fail("终止路径不能产出上下文")
    assert error.value is signal
    assert cleanup == (["close"] if source == "bootstrap" else ["rollback", "close"])
    assert "private-" not in caplog.text
