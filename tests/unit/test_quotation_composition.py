"""真实工厂构造拓扑与零IO边界；持久业务链另由PG/Linux集成验收。"""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from apps.api.composition import quotations as composition
from infra.quotation_settings import from_mapping
from tests.quotation_runtime_fixtures import quotation_settings_values
from tests.unit.test_quotation_contracts import basis_case
from tool_gateway.fingerprint import HmacFingerprintProvider

TENANT = "tn_01KZXT00000000000000000001"


@pytest.mark.parametrize(
    "module,name,original",
    [
        (
            "domains.costing.service",
            "CostingUnitOfWorkFactory",
            "domains.costing.repository",
        ),
        (
            "domains.costing.service",
            "CostingFreezeUowFactory",
            "domains.costing.freeze_repository",
        ),
        (
            "domains.demand.service",
            "NeedUnitUnitOfWork",
            "domains.demand.unit_repository",
        ),
    ],
)
def test_public_composition_uow_exports_keep_same_protocol_identity_in_fresh_interpreter(
    module, name, original
):
    import os
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"from {module} import {name}; from {original} import {name} as old; assert {name} is old",
        ],
        env={**os.environ, "PYTHON_DOTENV_DISABLED": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def forbidden_factory():
    pytest.fail("构造不允许打开数据库session")


def source_case(composition_module=composition):
    settings = from_mapping(quotation_settings_values())
    raw, uploads, authorizer = AsyncMock(), AsyncMock(), AsyncMock()
    fingerprints = HmacFingerprintProvider("test-v1", b"x" * 32)
    assert hasattr(composition, "build_quotation_evidence"), "缺少来源独立工厂"
    evidence = composition_module.build_quotation_evidence(
        forbidden_factory,
        settings,
        tenant_id=TENANT,
        raw=raw,
        uploads=uploads,
        need_authorizer=authorizer,
        fingerprints=fingerprints,
        lease_duration=timedelta(seconds=120),
        lease_owner="quote-source-test",
        now=lambda: basis_case()[3],
    )
    return settings, evidence, raw, uploads, authorizer


def test_source_factory_builds_exact_independent_gateway_without_probe_or_io():
    from connectors.evidence_text.client import LinuxEvidenceTextParser
    from tool_gateway.handlers.quote_evidence import MANIFEST
    from workflows.quote_approval.source_readers import (
        GatewayNeedUnitEvidenceReader,
        GatewayPricingEvidenceReader,
    )

    _settings, evidence, raw, uploads, authorizer = source_case()
    assert isinstance(evidence.parser, LinuxEvidenceTextParser)
    assert evidence.parser.capability().status == "unavailable"
    assert isinstance(evidence.pricing, GatewayPricingEvidenceReader)
    assert isinstance(evidence.need_units, GatewayNeedUnitEvidenceReader)
    gateway = evidence.preview_reader._gateway
    assert [m.tool_id for m in gateway._registry.list_manifests()] == [MANIFEST.tool_id]
    assert evidence.pricing._reader is evidence.preview_reader
    assert evidence.need_units._reader is evidence.preview_reader
    assert not raw.mock_calls and not uploads.mock_calls and not authorizer.mock_calls


def test_source_factory_tenant_is_required_not_inferred_from_ports():
    assert hasattr(composition, "build_quotation_evidence"), "缺少来源独立工厂"
    with pytest.raises(TypeError, match="tenant_id"):
        composition.build_quotation_evidence(
            forbidden_factory,
            from_mapping(quotation_settings_values()),
            raw=AsyncMock(),
            uploads=AsyncMock(),
            need_authorizer=AsyncMock(),
            fingerprints=HmacFingerprintProvider("test", b"x" * 32),
            lease_duration=timedelta(seconds=120),
            lease_owner="source-test",
            now=lambda: basis_case()[3],
        )


@pytest.mark.parametrize("files_enabled", [True, False])
def test_domain_factory_builds_real_services_and_binds_issuer_once_without_io(
    files_enabled,
):
    from domains.costing.freeze_service import CostingFreezeServiceImpl
    from domains.costing.quote_service import CostingQuoteServiceImpl
    from domains.demand.unit_service import NeedUnitServiceImpl
    from domains.quotations.service_impl import QuotationServiceImpl
    from workflows.quote_approval.need_unit_access import CurrentNeedUnitAuthorizer

    settings, evidence, _, _, _ = source_case()
    if not files_enabled:
        settings = from_mapping({**quotation_settings_values(), "files": None})
    assert hasattr(composition, "build_quotation_domains"), "缺少真实报价域工厂"
    domain = composition.build_quotation_domains(
        forbidden_factory,
        settings,
        tenant_id=TENANT,
        evidence=evidence,
        run_reader=AsyncMock(),
        artifact_reader=AsyncMock() if files_enabled else None,
        now=lambda: basis_case()[3],
    )
    assert isinstance(domain.quotations, QuotationServiceImpl)
    assert isinstance(domain.costing_quotes, CostingQuoteServiceImpl)
    assert isinstance(domain.costing_freeze, CostingFreezeServiceImpl)
    assert isinstance(domain.need_units, NeedUnitServiceImpl)
    assert isinstance(domain.need_units._authorizer, CurrentNeedUnitAuthorizer)
    assert domain.context_provider._issuer._quotations() is domain.quotations
    assert domain.costing_freeze._completions._quotes is domain.quotations
    assert domain.approval_access._quotes is domain.quotations
    assert (domain.files is not None) is files_enabled
    assert domain.quotations._files is domain.files


def test_domain_factory_tenant_is_required_not_inferred_from_readers():
    assert hasattr(composition, "build_quotation_domains"), "缺少真实报价域工厂"
    with pytest.raises(TypeError, match="tenant_id"):
        composition.build_quotation_domains(
            forbidden_factory,
            from_mapping(quotation_settings_values()),
            evidence=AsyncMock(),
            run_reader=AsyncMock(),
            artifact_reader=None,
            now=lambda: basis_case()[3],
        )


@pytest.mark.parametrize("missing", [None, "files", "generated", "metadata_only"])
@pytest.mark.parametrize("root_name", ["api", "scheduler"])
def test_http_factory_has_whole_file_group_and_metadata_only_recovery(
    missing, root_name
):
    from apps.scheduler_worker import quotations as scheduler

    root = composition if root_name == "api" else scheduler
    settings, evidence, _, _, _ = source_case(root)
    if missing == "files":
        settings = from_mapping({**quotation_settings_values(), "files": None})
    domain = root.build_quotation_domains(
        forbidden_factory,
        settings,
        tenant_id=TENANT,
        evidence=evidence,
        run_reader=AsyncMock(),
        artifact_reader=AsyncMock() if missing != "files" else None,
        now=lambda: basis_case()[3],
    )

    class Metadata:
        async def get_meta_by_key(self, *args):
            pytest.fail("构造不读取metadata")

    metadata = Metadata()
    generated = AsyncMock()
    final_factory = (
        root.build_quotation_http
        if root_name == "api"
        else root.build_quotation_runtime
    )
    result = final_factory(
        domain,
        factory=forbidden_factory,
        approvals=AsyncMock(),
        engine=AsyncMock(),
        settings=settings,
        evidence=evidence,
        generated=None if missing in {"files", "generated"} else generated,
        metadata_only=None if missing in {"files", "metadata_only"} else metadata,
        fingerprints=HmacFingerprintProvider("test-v1", b"x" * 32),
        now=lambda: basis_case()[3],
    )
    assert result.domain is domain and result.evidence is evidence
    assert result.lifecycle._parser is evidence.parser
    if root_name == "api":
        assert result.approval_starter is not None
    else:
        assert not hasattr(result, "approval_starter")
    if missing:
        assert result.files_application is None and result.customer_versions is None
    else:
        gateway = result.files_application._gateway
        assert gateway is not evidence.preview_reader._gateway
        assert {m.tool_id for m in gateway._registry.list_manifests()} == {
            "quotation.file.generate",
            "quotation.file.read",
            "quotation.file.history.read",
            "quotation.file.reconcile",
        }
        recovery = gateway._registry.get("quotation.file.reconcile")[1]
        assert recovery._metadata is metadata
        assert not any(
            hasattr(recovery._metadata, name)
            for name in ("put", "get_bounded", "delete", "render")
        )
        rate = gateway._checks["rate_limit"]._limiter
        assert rate._owner == gateway._lease_owner
    assert generated.mock_calls == []


def test_two_roots_share_mechanics_but_never_runtime_instances(monkeypatch):
    import inspect
    from functools import wraps

    from apps.composition_support import quotations as common
    from apps.scheduler_worker import quotations as scheduler

    delegated = []

    def observe(name):
        original = getattr(common, name)

        @wraps(original)
        def build(*args, **kwargs):
            delegated.append(name)
            return original(*args, **kwargs)

        monkeypatch.setattr(common, name, build)

    for name in (
        "build_quotation_evidence",
        "build_quotation_domains",
        "build_quotation_runtime_parts",
    ):
        observe(name)
    parsers = []
    parser_type = common.LinuxEvidenceTextParser

    def parser(**kwargs):
        value = parser_type(**kwargs)
        parsers.append(value)
        return value

    monkeypatch.setattr(common, "LinuxEvidenceTextParser", parser)
    results = []
    for root, owner, final_factory in (
        (composition, "api-quote-files", composition.build_quotation_http),
        (scheduler, "scheduler-quote-files", scheduler.build_quotation_runtime),
    ):
        for name in (
            "QuotationDomainComposition",
            "QuotationEvidenceComposition",
            "QuotationRuntimeLifecycle",
        ):
            assert getattr(root, name) is getattr(common, name)
        for name in (
            "build_need_unit_authorizer",
            "build_quotation_evidence",
            "build_quotation_domains",
        ):
            assert inspect.signature(getattr(root, name)) == inspect.signature(
                getattr(common, name)
            )
        settings, evidence, *_ = source_case(root)
        domain = root.build_quotation_domains(
            forbidden_factory,
            settings,
            tenant_id=TENANT,
            evidence=evidence,
            run_reader=AsyncMock(),
            artifact_reader=AsyncMock(),
            now=lambda: basis_case()[3],
        )
        approvals, engine = AsyncMock(), AsyncMock()
        result = final_factory(
            domain,
            factory=forbidden_factory,
            approvals=approvals,
            engine=engine,
            settings=settings,
            evidence=evidence,
            generated=AsyncMock(),
            metadata_only=AsyncMock(),
            fingerprints=HmacFingerprintProvider("test", b"x" * 32),
            now=lambda: basis_case()[3],
        )
        gateway = result.files_application._gateway
        assert gateway._lease_owner == owner
        assert gateway._checks["rate_limit"]._limiter._owner == owner
        access = result.files_application._access
        assert access._runs._engine() is engine
        assert access._facts._approvals is approvals
        assert result.lifecycle._parser is evidence.parser
        assert domain.approval_access._quotes is domain.quotations
        results.append(result)
    first, second = results
    assert len(parsers) == 2 and parsers[0] is not parsers[1]
    assert first.domain.quotations is not second.domain.quotations
    assert (
        first.evidence.preview_reader._gateway
        is not second.evidence.preview_reader._gateway
    )
    assert first.files_application._gateway is not second.files_application._gateway
    assert first.files_application._slot is not second.files_application._slot
    assert first.lifecycle is not second.lifecycle
    assert delegated == [
        "build_quotation_evidence",
        "build_quotation_domains",
        "build_quotation_runtime_parts",
        "build_quotation_evidence",
        "build_quotation_domains",
        "build_quotation_runtime_parts",
    ]


def test_shared_file_factory_requires_explicit_process_lease_owner():
    import inspect

    from apps.composition_support.quotations import build_quotation_runtime_parts

    parameter = inspect.signature(build_quotation_runtime_parts).parameters[
        "lease_owner"
    ]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


@pytest.mark.parametrize("mode", ["enabled", "no_files", "no_config", "no_store"])
def test_real_api_factory_has_single_guarded_approvals_and_zero_s3_construction(
    monkeypatch, mode
):
    import json

    from apps.api.composition.runtime import build_phase1_dependencies
    from apps.api.runtime_config import Phase1RuntimeSettings
    from connectors.object_store import s3
    from connectors.object_store.config import S3ObjectStoreSettings
    from tests.unit.test_api_runtime import _ManualSecrets
    from tests.unit.test_api_runtime_config import _VALID_ENV

    def sdk(*args, **kwargs):
        pytest.fail("真实API构造不能创建S3 SDK")

    monkeypatch.setattr(s3.boto3, "client", sdk)
    values = quotation_settings_values()
    if mode == "no_files":
        values["files"] = None
    environ = {**_VALID_ENV, "TRADEOS_TENANT_ID": TENANT}
    if mode != "no_config":
        environ["TRADEOS_QUOTATION_SETTINGS_JSON"] = json.dumps(values)
    settings = Phase1RuntimeSettings.from_environ(environ)
    secrets = _ManualSecrets()
    objects = S3ObjectStoreSettings(
        True,
        "http://localhost:9000",
        "test-bucket",
        "TEST_ACCESS",
        "TEST_SECRET",
        "us-east-1",
        2097152,
        2097152,
    )
    dependencies = build_phase1_dependencies(
        settings,
        forbidden_factory,
        now=lambda: basis_case()[3],
        secret_resolver=secrets,
        object_store_settings=None if mode == "no_store" else objects,
    )
    assert "TEST_ACCESS" not in secrets.refs and "TEST_SECRET" not in secrets.refs
    assert secrets.refs.count(settings.tool_call_fingerprint_key_ref) == 1
    workflow = dependencies.workflow_engine
    if mode in {"no_store", "no_config"}:
        assert dependencies.quotation is None
        assert dependencies.approvals._quote_access is None
        assert ("quote_approval", 1) not in workflow._definitions
        assert not any(
            name.startswith("quote_approval.") for name in workflow._handlers
        )
    else:
        quotation = dependencies.quotation
        assert quotation is not None
        assert dependencies.approvals._quote_access is quotation.domain.approval_access
        assert ("quote_approval", 1) in workflow._definitions
        assert (
            len([n for n in workflow._handlers if n.startswith("quote_approval.")]) == 7
        )
        for name in (
            "assemble",
            "submit",
            "wait",
            "apply",
            "mark_applied",
            "notify",
            "complete",
        ):
            application = workflow._handlers["quote_approval." + name]._application
            assert application._approvals is dependencies.approvals
        assert quotation.approval_starter._engine is workflow
        assert quotation.domain.quotations._approvals._runs._engine() is workflow
        assert (quotation.files_application is None) is (mode == "no_files")
        assert quotation.lifecycle._parser is quotation.evidence.parser
