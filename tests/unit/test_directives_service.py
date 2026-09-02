"""Sourcing admission Directive 的严格输入、完整复制与乐观基线。"""

from __future__ import annotations

import importlib
from dataclasses import replace
from datetime import UTC, datetime
from typing import Self

import pytest

from domains.directives.schemas import SourcingAdmissionConfigInput
from domains.directives.service_impl import DirectiveServiceImpl
from shared.errors import InvalidStateTransition, PermissionDenied, ValidationError
from shared.schemas.identifiers import DirectiveId, EmployeeId, TenantId

_models = importlib.import_module("domains.directives.models")
Directive = _models.Directive
DirectiveContent = _models.DirectiveContent
DirectiveObjective = _models.DirectiveObjective
DirectiveProposal = _models.DirectiveProposal
DiscoveryConfig = _models.DiscoveryConfig
HandoffRules = _models.HandoffRules
MarketAssignment = _models.MarketAssignment
OutreachBounds = _models.OutreachBounds
ProposalState = _models.ProposalState
SourcingAdmissionConfig = _models.SourcingAdmissionConfig

NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)
TENANT = TenantId("tn_directive_admission")
BOSS = EmployeeId("emp_boss")
STAFF = EmployeeId("emp_staff")


class _Employees:
    async def is_active_boss(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> bool:
        return tenant_id == TENANT and employee_id == BOSS

    async def names_for(
        self, tenant_id: TenantId, employee_ids: tuple[EmployeeId, ...]
    ) -> dict[EmployeeId, str]:
        assert tenant_id == TENANT
        return {employee_id: str(employee_id) for employee_id in employee_ids}


class _Bus:
    async def publish(self, event: object) -> None:
        return None


class _State:
    def __init__(self) -> None:
        self.proposals: dict[str, DirectiveProposal] = {}
        self.directives: list[Directive] = []
        self.active: Directive | None = None


class _Proposals:
    def __init__(self, state: _State) -> None:
        self._state = state

    async def add(self, proposal: DirectiveProposal) -> None:
        self._state.proposals[proposal.proposal_id] = proposal

    async def get(
        self, tenant_id: TenantId, proposal_id: str
    ) -> DirectiveProposal | None:
        assert tenant_id == TENANT
        return self._state.proposals.get(proposal_id)

    async def get_for_update(
        self, tenant_id: TenantId, proposal_id: str
    ) -> DirectiveProposal | None:
        return await self.get(tenant_id, proposal_id)

    async def update(self, proposal: DirectiveProposal) -> None:
        self._state.proposals[proposal.proposal_id] = proposal

    async def list_pending(self, tenant_id: TenantId) -> list[DirectiveProposal]:
        assert tenant_id == TENANT
        return [
            proposal
            for proposal in self._state.proposals.values()
            if proposal.state is ProposalState.PENDING_CONFIRMATION
        ]

    async def list_rejected(
        self, tenant_id: TenantId, limit: int
    ) -> list[DirectiveProposal]:
        assert tenant_id == TENANT
        return [
            proposal
            for proposal in self._state.proposals.values()
            if proposal.state is ProposalState.REJECTED
        ][:limit]


class _Directives:
    def __init__(self, state: _State) -> None:
        self._state = state

    async def add(self, directive: Directive) -> None:
        self._state.directives.append(directive)

    async def get_active(self, tenant_id: TenantId) -> Directive | None:
        assert tenant_id == TENANT
        return self._state.active

    async def get_active_for_update(self, tenant_id: TenantId) -> Directive | None:
        return await self.get_active(tenant_id)

    async def get_version(self, tenant_id: TenantId, version: int) -> Directive | None:
        assert tenant_id == TENANT
        return next(
            (item for item in self._state.directives if item.version == version),
            None,
        )

    async def next_version(self, tenant_id: TenantId) -> int:
        assert tenant_id == TENANT
        return max((item.version for item in self._state.directives), default=0) + 1

    async def mark_superseded(
        self, tenant_id: TenantId, directive_id: DirectiveId
    ) -> None:
        assert tenant_id == TENANT
        assert self._state.active is not None
        assert self._state.active.directive_id == directive_id
        superseded = replace(self._state.active, superseded_at=NOW)
        self._state.directives = [
            superseded if item.directive_id == directive_id else item
            for item in self._state.directives
        ]
        self._state.active = superseded

    async def set_active(self, directive: Directive) -> None:
        self._state.active = directive

    async def list_versions(self, tenant_id: TenantId, limit: int) -> list[Directive]:
        assert tenant_id == TENANT
        return sorted(self._state.directives, key=lambda item: item.version)[-limit:]


class _Uow:
    def __init__(self, state: _State) -> None:
        self.proposals = _Proposals(state)
        self.directives = _Directives(state)
        self.bus = _Bus()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _Factory:
    def __init__(self, state: _State) -> None:
        self._state = state

    def __call__(self, tenant_id: TenantId) -> _Uow:
        assert tenant_id == TENANT
        return _Uow(self._state)


def _service(state: _State) -> DirectiveServiceImpl:
    return DirectiveServiceImpl(_Factory(state), _Employees(), now=lambda: NOW)


def _config(**changes: object) -> SourcingAdmissionConfigInput:
    values: dict[str, object] = {
        "mode": "cluster_ranked",
        "automatic_admission_enabled": True,
        "batch_limit": 3,
    }
    values.update(changes)
    return SourcingAdmissionConfigInput(**values)  # type: ignore[arg-type]


def _full_content() -> DirectiveContent:
    return DirectiveContent(
        objective=DirectiveObjective.FOCUS_EXISTING_NEEDS,
        market_assignments=[MarketAssignment("US", EmployeeId("emp_owner"))],
        discovery=DiscoveryConfig(
            need_first_ratio=30,
            catalog_assisted_ratio=70,
            focus_categories=["hinges"],
            excluded_buyer_types=["retailer"],
        ),
        outreach=OutreachBounds(
            primary_channel="email",
            max_sequence_messages=3,
            stop_on_reply=True,
        ),
        handoff=HandoffRules(
            manager=EmployeeId("emp_manager"), triggers=["quote_requested"]
        ),
        paused_markets=["DE"],
        monthly_budget_credits=900,
        notes="Keep every existing section.",
    )


def _active(state: _State, version: int, content: DirectiveContent) -> Directive:
    directive = Directive(
        directive_id=DirectiveId(f"dir_{version}"),
        tenant_id=TENANT,
        version=version,
        content=content,
        source_proposal_id=f"dpr_{version}",
        activated_at=NOW,
        activated_by=BOSS,
    )
    state.directives.append(directive)
    state.active = directive
    return directive


@pytest.mark.parametrize(
    "config",
    [
        _config(mode="fifo"),
        _config(automatic_admission_enabled=1),
        _config(batch_limit=True),
        _config(batch_limit=0),
        _config(batch_limit=51),
    ],
)
async def test_sourcing_admission_proposal_rejects_non_exact_configuration(
    config: SourcingAdmissionConfigInput,
) -> None:
    with pytest.raises(ValidationError):
        await _service(_State()).submit_sourcing_admission_proposal(
            TENANT,
            "Enable bounded cluster-ranked sourcing.",
            config,
            "Enable cluster-ranked sourcing admission.",
            ["Up to three waiting cases may be admitted per cycle."],
            "directive-parser-v1",
        )


def test_sourcing_admission_input_rejects_extra_fields() -> None:
    with pytest.raises(TypeError):
        SourcingAdmissionConfigInput(
            mode="cluster_ranked",
            automatic_admission_enabled=True,
            batch_limit=3,
            unconfirmed_limit=9,  # type: ignore[call-arg]
        )


async def test_sourcing_admission_proposal_requires_expected_behavior_changes() -> None:
    with pytest.raises(ValidationError, match="预计行为变化不能为空"):
        await _service(_State()).submit_sourcing_admission_proposal(
            TENANT,
            "Enable bounded cluster-ranked sourcing.",
            _config(),
            "Enable cluster-ranked sourcing admission.",
            [],
            "directive-parser-v1",
        )


async def test_generic_proposal_cannot_bypass_sourcing_admission_baseline() -> None:
    with pytest.raises(ValidationError, match="必须通过寻源准入提案接口"):
        await _service(_State()).submit_proposal(
            TENANT,
            "Enable bounded cluster-ranked sourcing.",
            DirectiveContent(
                objective=DirectiveObjective.FOCUS_EXISTING_NEEDS,
                sourcing_admission=SourcingAdmissionConfig(
                    mode="cluster_ranked",
                    automatic_admission_enabled=True,
                    batch_limit=3,
                ),
            ),
            "Enable cluster-ranked sourcing admission.",
            ["Up to three waiting cases may be admitted per cycle."],
            "directive-parser-v1",
        )


async def test_sourcing_admission_proposal_copies_the_complete_active_directive() -> (
    None
):
    state = _State()
    original = _full_content()
    _active(state, 7, original)

    proposal_id = await _service(state).submit_sourcing_admission_proposal(
        TENANT,
        "Enable bounded cluster-ranked sourcing.",
        _config(),
        "Enable cluster-ranked sourcing admission.",
        ["Up to three waiting cases may be admitted per cycle."],
        "directive-parser-v1",
    )

    proposal = state.proposals[proposal_id]
    assert proposal.base_directive_version == 7
    assert proposal.parsed == replace(
        original,
        sourcing_admission=SourcingAdmissionConfig(
            mode="cluster_ranked",
            automatic_admission_enabled=True,
            batch_limit=3,
        ),
    )

    view = await _service(state).get_proposal(TENANT, proposal_id)
    assert view.sourcing_admission_mode == "cluster_ranked"
    assert view.automatic_sourcing_admission_enabled is True
    assert view.sourcing_admission_batch_limit == 3


async def test_non_boss_cannot_confirm_sourcing_admission() -> None:
    state = _State()
    proposal_id = await _service(state).submit_sourcing_admission_proposal(
        TENANT,
        "Enable bounded cluster-ranked sourcing.",
        _config(),
        "Enable cluster-ranked sourcing admission.",
        ["Up to three waiting cases may be admitted per cycle."],
        "directive-parser-v1",
    )

    with pytest.raises(PermissionDenied, match="只有在职老板"):
        await _service(state).confirm_proposal(TENANT, proposal_id, STAFF)
    assert state.active is None


async def test_stale_sourcing_admission_proposal_cannot_overwrite_newer_directive() -> (
    None
):
    state = _State()
    original = _full_content()
    _active(state, 7, original)
    service = _service(state)
    proposal_id = await service.submit_sourcing_admission_proposal(
        TENANT,
        "Enable bounded cluster-ranked sourcing.",
        _config(),
        "Enable cluster-ranked sourcing admission.",
        ["Up to three waiting cases may be admitted per cycle."],
        "directive-parser-v1",
    )
    newer_content = replace(original, monthly_budget_credits=1200)
    newer = _active(state, 8, newer_content)
    before = list(state.directives)

    with pytest.raises(InvalidStateTransition, match="陈旧"):
        await service.confirm_proposal(TENANT, proposal_id, BOSS)

    assert state.active == newer
    assert state.directives == before
    assert state.proposals[proposal_id].state is ProposalState.PENDING_CONFIRMATION
