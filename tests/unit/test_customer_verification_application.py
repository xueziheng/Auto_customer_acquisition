"""未配置的客户验证入口保持关闭，身份变化不能获得联系点。"""

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.errors import PermissionDenied
from tests.unit.test_customer_followup_application import A, fixture


def module():
    try:
        return importlib.import_module("apps.api.composition.customer_verification")
    except ModuleNotFoundError:
        pytest.fail("客户验证应用尚未实现")


async def test_unconfigured_runtime_and_policy_are_visible_blockers():
    existing, identity, prospecting, _, _ = fixture()
    prospecting.get_account_detail = AsyncMock(return_value=SimpleNamespace(account=SimpleNamespace(account_id=A, tenant_id=identity.tenant_id), contacts=()))
    application = module().CustomerVerificationApplication(existing._reader, prospecting)
    view = await application.read(identity, "cal_test")
    assert view.can_verify is False
    assert set(view.blocking_reasons) == {"provider_not_configured", "verification_policy_not_configured"}


async def test_startup_composes_only_explicit_runtime_and_keeps_missing_runtime_closed():
    existing, _, prospecting, _, _ = fixture()
    runtime = SimpleNamespace(compose=AsyncMock())
    application = module().CustomerVerificationApplication(existing._reader, prospecting, runtime)
    await application.startup()
    runtime.compose.assert_awaited_once_with()
    await module().CustomerVerificationApplication(existing._reader, prospecting).startup()


@pytest.mark.parametrize("scope", [{"owner": "emp_other"}, {"active": False}])
async def test_inactive_or_non_owner_is_rejected_before_contact_read(scope):
    existing, identity, prospecting, _, _ = fixture(**scope)
    prospecting.get_account_detail = AsyncMock()
    application = module().CustomerVerificationApplication(existing._reader, prospecting)
    with pytest.raises(PermissionDenied):
        await application.read(identity, "cal_test")
    prospecting.get_account_detail.assert_not_awaited()


async def test_mismatched_account_projection_is_not_returned_to_current_owner():
    existing, identity, prospecting, _, _ = fixture()
    prospecting.get_account_detail = AsyncMock(return_value=SimpleNamespace(account=SimpleNamespace(account_id="acc_other", tenant_id=identity.tenant_id), contacts=()))
    application = module().CustomerVerificationApplication(existing._reader, prospecting)
    with pytest.raises(PermissionDenied):
        await application.read(identity, "cal_test")


async def test_recorded_basis_policy_rejects_missing_assessment_without_country_guess():
    from datetime import UTC, datetime

    from domains.prospecting.schemas import (
        ContactPointDetailView,
        ContactPointKind,
        ContactPointView,
        ContactType,
        LegalBasisType,
        ProspectAccountView,
        SubjectType,
        VerificationStatus,
    )
    from shared.errors import PolicyViolation
    from tests.unit.test_prospect_inventory_api import TENANT
    _, identity, _, _, _ = fixture()
    now = datetime(2026, 10, 14, tzinfo=UTC)
    point = ContactPointView("cp_fixture", TENANT, "pc_fixture", A, ContactPointKind.EMAIL,
                             "info@company.test", VerificationStatus.UNVERIFIED, now)
    detail = ContactPointDetailView(point, LegalBasisType.LEGITIMATE_INTEREST, SubjectType.LEGAL_ENTITY,
                                   ContactType.ROLE_BASED, "public-company-page", now)
    account = ProspectAccountView(A, TENANT, "Company", "NG", now)
    policy = module().RecordedContactBasisPolicy()
    with pytest.raises(PolicyViolation):
        await policy.require_current(identity, account, detail)
    from dataclasses import replace
    await policy.require_current(identity, account, replace(detail, assessment_ref="reviewed-assessment"))
