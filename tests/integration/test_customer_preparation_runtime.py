"""实际 API、当前事实读取与原 singleton：只替换外部页面、模型、Hunter 传输。"""
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.composition.enterprise_knowledge import EnterpriseKnowledgeApplication
from apps.api.composition.inventory import CurrentCustomerAgentAuthority, bind_inventory
from apps.api.composition.runtime import build_phase1_dependencies
from apps.api.dependencies import get_request_identity
from apps.api.identity import RequestIdentity
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from apps.api.runtime_config import Phase1RuntimeSettings
from apps.scheduler_worker.bootstrap import (
    CanonicalSchedulerBootstrap,
    ResearchRuntimePorts,
)
from apps.scheduler_worker.customer_preparation_runtime import (
    build_customer_preparation,
    build_preparation_send_guard,
)
from apps.scheduler_worker.runtime import SchedulerRuntimeFactory
from artifact_store.service_impl import RawArtifactStoreImpl
from artifact_store.store import RawArtifactKind
from connectors.hunter.transport import HunterHttpResponse
from connectors.web_search.transport import PublicPageResponse
from domains.compliance.permissions import ComplianceActor, ComplianceScope
from domains.compliance.schemas import (
    DECISION_FIELDS,
    CountryPolicyApprovalFact,
    CountryPolicyProposalCreate,
)
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.opportunities.permissions import Actor, OpportunityScope, ScopeLevel
from domains.products.schemas import (
    KnowledgeActor,
    KnowledgeAnalysis,
    KnowledgeFact,
    KnowledgeSource,
)
from domains.products.service import EnterpriseKnowledgeServiceImpl
from domains.sending_identity.permissions import Actor as SendingActor
from domains.sending_identity.permissions import ScopeLevel as SendingLevel
from domains.sending_identity.permissions import SendingIdentityScope
from domains.sending_identity.schemas import (
    AuthenticationResult,
    DomainRole,
    IdentityRegisterRequest,
)
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.customer_assignment import CustomerAssignmentBridge
from infra.db.enterprise_knowledge import SqlAlchemyKnowledgeUnitOfWork
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.prospect_inventory import PostgresInventoryService, PostgresPublicCatalog
from infra.db.prospect_inventory_tables import CustomerPreparationItemRow
from infra.db.provider_readiness_uow import SqlAlchemyProviderReadinessUnitOfWork
from infra.db.tables import (
    ContactPointRow,
    EmployeeRow,
    ModelInvocationRow,
    OutreachEnrollmentRow,
    ToolCallRow,
)
from infra.secrets import EnvironmentSecretResolver
from infra.standalone.hunter_settings import HunterCustomerSettings
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import (
    ApprovalId,
    EmployeeId,
    IdempotencyKey,
    TenantId,
    UserId,
    new_id,
)
from shared.schemas.model_invocation import ModelResponse, ModelUsage
from shared.schemas.provenance import SourceType
from tests.integration.test_agent_profile_evidence import Objects
from tests.integration.test_api_runtime import _runtime_env
from tests.integration.test_scheduler_worker import (
    _factory_environ,
    _FactoryHealthServer,
    _FactoryResolver,
)
from tests.runtime_database_fixtures import (
    runtime_database_url as runtime_database_url,  # noqa: PLC0414
)
from tests.unit.test_prospect_inventory import candidate
from tests.unit.test_standalone_model_settings import settings as model_settings
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.provider_readiness import (
    ProviderConfiguration,
    ProviderReadinessActor,
    ProviderReadinessPermission,
    ProviderReadinessServiceImpl,
)


class External:
    """外部协议边界的合成响应，不替换授权/领域/账本。"""
    def __init__(self):
        self.models = self.pages = self.verifications = 0
        self.failure = None
        self.before_model = None
    def resolve(self, reference):
        return "synthetic-key-only"
    async def validate_url(self, url):
        assert url.startswith("https://acme.com")
        return url
    async def fetch(self, url):
        self.pages += 1
        return PublicPageResponse(url, b"<p>Acme Ltd. We distribute hinges. Sales sales@acme.com.</p>")
    async def send(self, *args, **kwargs):
        raise AssertionError("发送关闭时不得发信")
    async def search(self, *args, **kwargs):
        raise AssertionError("即时库存不应发起搜索")
    async def get(self, path, params, *, api_key):
        assert path == "/email-verifier"
        self.verifications += 1
        if self.failure == "unknown_verifier":
            from connectors.hunter.transport import HunterNetworkError
            raise HunterNetworkError(may_have_reached_provider=True)
        return HunterHttpResponse(200, {"data": {"status": "valid"}})
    async def generate(self, request):
        import json
        self.models += 1
        if self.before_model is not None: await self.before_model()
        if "candidates_for_fit" in request.payload:
            value = {"assessments": [{"candidate_id": c["candidate_id"], "matched": True, "evidence_quote": c["evidence_text"], "inference": "推断：经营铰链，适配已确认产品"} for c in request.payload["candidates_for_fit"]]}
            return ModelResponse(text=json.dumps(value), model=request.model, usage=ModelUsage(input_tokens=100,output_tokens=100,cached_input_tokens=0))
        value = {"entity_type":{"value":"distributor","quote":"We distribute hinges"},
            "industry":{"value":"hardware","quote":"hinges"}, "subject_type":"legal_entity",
            "legal_entity_quote":"Acme Ltd", "public_email":"sales@acme.com", "email_quote":"Sales sales@acme.com",
            "fit_quote":"We distribute hinges", "fit_reasoning":"推断：适配铰链，不代表购买意愿", "matched":True, "category":"hinges"}
        if self.failure == "no_basis": value["subject_type"] = "unknown"
        assert request.payload["internal_confirmed_context"]
        assert tuple(request.payload["allowed_entity_types"]) == ("distributor",)
        return ModelResponse(text=json.dumps(value), model=request.model, usage=ModelUsage(input_tokens=100,output_tokens=100,cached_input_tokens=0))
    async def aclose(self): pass


async def seed_knowledge(factory, tenant, boss, store):
    source = await store.put(tenant, RawArtifactKind.TEXT, b"Product: hinges.", "text/plain")
    knowledge = EnterpriseKnowledgeServiceImpl(lambda t: SqlAlchemyKnowledgeUnitOfWork(factory, t))
    actor = KnowledgeActor(tenant_id=tenant, employee_id=boss)
    doc = await knowledge.register_upload(tenant, actor, KnowledgeSource(filename="hinges.txt",mime_type="text/plain",size_bytes=source.size_bytes,sha256=source.content_hash,artifact_id=source.artifact_id), idempotency_key="upload")
    claim = await knowledge.claim_next(tenant, lease_owner="synthetic-external-parser")
    draft = await knowledge.complete_processing(tenant, claim, source_text="Product: hinges.", analysis=KnowledgeAnalysis(title="Hinges", facts=(KnowledgeFact(label="Product",value="hinges",source_quote="Product: hinges."),), inferences=()), model="synthetic", extracted_by="synthetic")
    confirmed = await knowledge.confirm(tenant, actor, doc.document_id, revision_id=draft.revision.revision_id, expected_version=draft.document.version)
    return knowledge, {"document_id":doc.document_id,"revision_id":confirmed.revision.revision_id}


async def seed_sender(service, tenant, boss, now):
    actor = SendingActor(str(boss), SendingIdentityScope(level=SendingLevel.TENANT), "boss")
    identity = await service.register(tenant, IdentityRegisterRequest("sender@outreach.example", "outreach.example", DomainRole.COLD_OUTREACH), actor=actor)
    await service.begin_authentication(tenant, identity, actor=actor)
    await service.record_authentication_result(tenant, identity, AuthenticationResult(checked_at=now,spf_passed=True,dkim_passed=True,dmarc_passed=True,failures=(),check_ref="synthetic-dns"), actor=SendingActor("system:dns",SendingIdentityScope(level=SendingLevel.SYSTEM,allowed_identity_ids=frozenset({identity})),"system"))
    await service.start_warmup(tenant, identity, 5, actor=actor)
    return identity


async def seed_country(service, tenant, boss, now, allowed=True):
    proposal = await service.propose_country_policy(tenant, CountryPolicyProposalCreate.model_validate({
        "country":"US", "public_research_allowed":True,"contact_enrichment_allowed":True,"cold_b2b_email_allowed":allowed,
        "personal_data_basis_required":True,"subject_type_affects_judgment":True,"contact_type_affects_judgment":True,
        "opt_out_deadline_days":30,"local_representative_required":False,"requirements":["retain.assessment_ref","honor_opt_out"],
        "notes":"合成验收政策，不是实际法律建议", "field_sources":{field:{"source_type":SourceType.EMPLOYEE_INPUT,"source_id":f"synthetic:{field.value}"} for field in DECISION_FIELDS}}),
        actor=ComplianceActor(str(boss),tenant,ComplianceScope.TENANT,"boss"), idempotency_key=IdempotencyKey("synthetic-country-" + str(allowed)))
    await service.activate_country_policy(tenant, proposal.country_policy_version_id,
        CountryPolicyApprovalFact(approval_id=ApprovalId(new_id("apr")),approval_type="country_policy_change",change_set_ref=proposal.change_set_ref,decided_by=EmployeeId(new_id("emp")),decided_at=now),
        actor=ComplianceActor("system:synthetic-approval",tenant,ComplianceScope.SYSTEM,"system"))


@pytest.mark.parametrize("configured,private,failure", [(False,False,None),(True,False,None),(True,True,None),
    (True,False,"no_policy"),(True,False,"policy_revoked"),(True,False,"scope_withdrawn"),
    (True,False,"owner_transfer"),(True,False,"artifact_mismatch"),(True,False,"suppressed"),(True,False,"sender_retired"),(True,False,"no_basis"),(True,False,"unknown_verifier"),(True,False,"legacy")])
async def test_actual_request_and_singleton_prepare_with_sending_disabled(integration_engine, db_url, runtime_database_url, tmp_path, monkeypatch, configured, private, failure, browser_width=0, page_only=False, deepseek_search=False):
    browser_check = None
    if browser_width:
        from tests.customer_preparation_browser import PreparationBrowserCheck
        browser_check = PreparationBrowserCheck(browser_width, tmp_path)
    now = datetime.now(UTC)
    tenant, control = TenantId(new_id("tn")), TenantId(new_id("tn"))
    runtime_url = await runtime_database_url(tenant)
    boss, sales = EmployeeId(new_id("emp")), EmployeeId(new_id("emp"))
    user = UserId(new_id("usr"))
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    async with sessions.begin() as db:
        db.add_all([EmployeeRow(tenant_id=tenant,employee_id=boss,user_id=user,name="合成老板",role="boss",is_active=True),EmployeeRow(tenant_id=tenant,employee_id=sales,name="合成员工",role="sales",is_active=True)])
    fingerprints = HmacFingerprintProvider("v1", b"f" * 32)
    external, objects = External(), Objects()
    external.failure = failure
    failures = []
    from apps.scheduler_worker.customer_preparation_model import (
        CustomerPreparationExtractor,
    )
    original_extract = CustomerPreparationExtractor.extract
    async def extract(self, *args, **kwargs):
        try: return await original_extract(self, *args, **kwargs)
        except Exception as exc:
            failures.append((type(exc).__name__, str(exc), str(getattr(exc,"category",None))))
            raise
    monkeypatch.setattr(CustomerPreparationExtractor, "extract", extract)
    store = RawArtifactStoreImpl(lambda t: SqlAlchemyArtifactUnitOfWork(sessions,t), objects, 2097152, lambda:now, new_id, bounded_transport=objects)
    knowledge, ref = await seed_knowledge(sessions,tenant,boss,store)
    env = _runtime_env(str(db_url)); env["TRADEOS_TENANT_ID"] = tenant
    phase = Phase1RuntimeSettings.from_environ(env)
    api = build_phase1_dependencies(phase,sessions,now=lambda:now,secret_resolver=EnvironmentSecretResolver(env),gmail_transport=external)
    api = replace(api, enterprise_knowledge=EnterpriseKnowledgeApplication(knowledge,store,maximum_upload_bytes=2097152))
    sender = await seed_sender(api.sending_identities,tenant,boss,now)
    if failure != "no_policy": await seed_country(api.compliance,tenant,boss,now)
    async with api.employees(tenant) as employees:
        employee = await employees.get_employee(tenant,boss,actor=api.employee_lookup_actor)
    identity = RequestIdentity(tenant,employee,EmployeeActor(str(boss),EmployeeScope.TENANT,"boss"),Actor(str(boss),OpportunityScope(level=ScopeLevel.TENANT),"boss"))
    app = create_app(settings=ApiSettings(tenant_id=tenant,dev_mode=True,retry_after_seconds=1),dependencies=api, cors_allowed_origins=(browser_check.web_origin,) if browser_check else ())
    app.dependency_overrides[get_request_identity] = lambda:identity
    catalog = PostgresPublicCatalog(sessions,control,now=lambda:now)
    if not deepseek_search:
        await catalog.save_public((candidate(observed_at=now,expires_at=now.replace(year=now.year+1)),))
    inventory = PostgresInventoryService(sessions,tenant,catalog,now=lambda:now,assignment_bridge=CustomerAssignmentBridge(now=lambda:now),followup_authority=CurrentCustomerAgentAuthority(api,tenant))
    app.state.prospect_inventory_factory = lambda identity:bind_inventory(inventory,api,identity)
    keyfile = tmp_path/"synthetic-hunter-key"; keyfile.write_text("synthetic-hunter-only"); keyfile.chmod(0o600)
    hunter = HunterCustomerSettings(enabled=True,business_tenant_id=tenant,configuration_version="test-v1",api_key_version="key-v1",maximum_daily_verifications=5,secret_file=keyfile)
    readiness_actor = ProviderReadinessActor("synthetic:human",tenant,frozenset(ProviderReadinessPermission))
    readiness = ProviderReadinessServiceImpl(lambda t:SqlAlchemyProviderReadinessUnitOfWork(sessions,t),runtime_actor=readiness_actor)
    config = ProviderConfiguration.hunter_contacts("test-v1","key-v1")
    if configured:
        await readiness.declare_configuration(tenant,config,actor=readiness_actor,idempotency_key=IdempotencyKey("configured"))
        await readiness.mark_validation_started(tenant,config.configuration_hash,validation_key=IdempotencyKey("validated"),actor=readiness_actor)
        await readiness.mark_validation_passed(tenant,config.configuration_hash,validation_key=IdempotencyKey("validated"),evidence_ref="tool-call:synthetic-validation",actor=readiness_actor)
    settings = StandaloneModelSettings.model_validate({**model_settings(),"limits": {**model_settings()["limits"], "max_output_tokens": 2048, "max_input_bytes": 16384, "tenant_calls": 10, "employee_calls": 10}, "codex_job_root":tmp_path/"codex-jobs", "api_control_tenant":control, "api_key_file":tmp_path/"synthetic-model-unused", "api_verified_at":now,"api_business_tenant":tenant})
    if deepseek_search:
        from shared.schemas.search_backend import InventorySearchPolicy
        settings = settings.model_copy(update={"api_connection_version":"synthetic-cv1", "inventory_search":InventorySearchPolicy(version="search-p1",paid_search_enabled=True,maximum_cost_cny="0.05")})
        class SearchTransport:
            async def search(self, query, country, limit, *, api_key):
                assert api_key == "synthetic-key-only"
                assert len(query) <= 128 and "内部" not in query
                return {"results":[{"title":"Acme Ltd", "url":"https://acme.com/", "snippet":"Hinges distributor"}]}
        monkeypatch.setattr("apps.scheduler_worker.standalone_inventory.DeepSeekSearchApiTransport", SearchTransport)
        monkeypatch.setattr("apps.scheduler_worker.standalone_inventory.ModelKeyFileResolver", lambda *a:external)
        async def fetch(url):
            external.pages += 1
            return PublicPageResponse(url,b"<p>Acme Ltd. We distribute hinges.</p><p>Address: United States</p><p>Sales sales@acme.com.</p>")
        external.fetch = fetch
    if deepseek_search:
        from infra.db.inventory_search_readiness import InventorySearchReadinessReader
        assert (await InventorySearchReadinessReader(sessions, tenant, settings).read(tenant, settings.inventory_search)).connection_status == "passed"
    repository = SqlModelConfigurationRepository(sessions,fingerprints)
    await repository.initialize(tenant,settings.configuration_version,settings.model,settings.limits,True)
    for process in ("api","scheduler"):
        await repository.register_process(tenant,process,settings.configuration_version,"synthetic")
    ports = ResearchRuntimePorts(external,settings.model,user,external,external,objects,2097152,"SYNTHETIC",external,True,bounded_object_transport=objects)
    monkeypatch.setattr("apps.scheduler_worker.customer_preparation_runtime.HunterApiHttpTransport", lambda:external)
    monkeypatch.setattr("apps.scheduler_worker.customer_preparation_model.configured_codex_provider", lambda *_:external)
    send_guards = []
    async def prepare(core,factory,outreach,verifier):
        send_guards.append(build_preparation_send_guard(core, factory, tenant, outreach, lambda: datetime.now(UTC)))
        return await build_customer_preparation(core=core,sessions=factory,control_sessions=sessions,control_tenant=control,tenant_id=tenant,outreach=outreach,existing_verifier=verifier,settings=settings,resolver=external,fingerprints=fingerprints,research=None if page_only else ports,owner="synthetic-preparation",hunter=hunter if configured else None, inventory_ports=__import__("apps.scheduler_worker.inventory_ports", fromlist=["InventoryRuntimePorts"]).InventoryRuntimePorts(external,objects,2097152,objects) if page_only else None)
    from apps.scheduler_worker.standalone_inventory import build_inventory_driver
    monkeypatch.setattr("apps.scheduler_worker.inventory_model.configured_codex_provider", lambda *_args, **_kwargs:external)
    if deepseek_search:
        class DiscoveryProvider:
            def __init__(self, session): self.session = session
            async def generate(self, request):
                import json
                if "candidates_for_fit" in request.payload:
                    return await external.generate(request)
                source = request.payload["allowed_source"]
                rows = []
                if source == "company_websites":
                    found = await self.session.call("source_search", {"source_id":source,"query":"hinges United States"})
                    page = found["results"][0]["page_id"]
                    await self.session.call("read_public_page", {"page_id":page})
                    rows = [{"page_id":page,"name":"Acme Ltd","country":"US","public_emails":["sales@acme.com"]}]
                return ModelResponse(text=json.dumps({"candidates":rows}),model=request.model,usage=ModelUsage(input_tokens=100,output_tokens=100,cached_input_tokens=0))
            async def aclose(self): pass
        monkeypatch.setattr("apps.scheduler_worker.inventory_model.configured_codex_provider", lambda *a, discovery:DiscoveryProvider(discovery))
    def acquire(core,factory):
        return build_inventory_driver(core=core,sessions=factory,control_sessions=sessions,control_tenant=control,tenant_id=tenant,settings=settings,resolver=external,fingerprints=fingerprints,research=None if deepseek_search else ports,owner="synthetic-acquisition",inventory_ports=__import__("apps.scheduler_worker.inventory_ports",fromlist=["InventoryRuntimePorts"]).InventoryRuntimePorts(external,objects,2097152,objects) if deepseek_search else None)
    bootstrap = CanonicalSchedulerBootstrap(phase.scoring_policy,phase.handoff_policy,preparation_factory=prepare,inventory_factory=acquire if private else None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://test",headers={"X-Tenant-Id":tenant}) as client:
        created = await client.post("/crm/campaigns",json={"name":"合成准备范围","markets":["US"],"target_entity_types":["distributor"],"allowed_categories":["hinges"],"sender_identity_ids":[sender],"steps":[{"step_number":1,"intent":"discovery","wait_days":0}],"daily_new_contact_limit":5,"daily_total_message_limit":5,"handoff_triggers":[],"stop_on_reply":True,"customer_agent_policy":{"owner_employee_id":sales,"assessment_ref":"lia:synthetic","confirmed_refs":[ref]}})
        assert created.status_code == 200, created.text
        campaign = created.json()["campaign_id"]
        started = await client.post(f"/crm/campaigns/{campaign}/start")
        assert started.status_code == 200, started.text
        request = {"query":{"product_tags":["hinges"],"country":"US"},"count":1,"followup_scope":{"campaign_id":campaign,"campaign_version":1}}
        if failure == "legacy": request.pop("followup_scope")
        if private: request["query"]["confirmed_refs"] = [ref]
        first = await client.post("/prospects/inventory/requests",json=request,headers={"Idempotency-Key":"single-intent"})
        assert first.status_code == 202, first.text
        assert len(first.json()["allocations"]) == (0 if private else 1)
        repeated = await client.post("/prospects/inventory/requests",json=request,headers={"Idempotency-Key":"single-intent"})
        assert repeated.json() == first.json()
        if failure == "policy_revoked": await seed_country(api.compliance,tenant,boss,now,allowed=False)
        if failure == "scope_withdrawn":
            paused = await client.post(f"/crm/campaigns/{campaign}/pause",json={"reason":"human withdrawal"})
            assert paused.status_code == 200, paused.text
        if failure == "suppressed":
            from domains.outreach.permissions import Actor as OutreachActor
            from domains.outreach.permissions import OutreachScope
            from domains.outreach.permissions import ScopeLevel as OutreachLevel
            from domains.outreach.schemas import (
                SuppressionReason,
                SuppressionRequest,
                SuppressionTarget,
            )
            allocated = (await inventory.list_allocations(tenant,None,10))[0]
            assigned = (await inventory.assign(tenant, (allocated.id,), sales, acting_employee_id=boss))[0]
            await api.outreach.add_suppression(tenant, SuppressionRequest(SuppressionTarget(account_id=assigned.account_id), SuppressionReason.UNSUBSCRIBE, now, "synthetic-opt-out", IdempotencyKey("synthetic-opt-out")), actor=OutreachActor(str(boss), OutreachScope(level=OutreachLevel.TENANT), "boss"))
        if failure == "sender_retired":
            await api.sending_identities.retire(tenant, sender, "合成身份停用", actor=SendingActor(str(boss), SendingIdentityScope(level=SendingLevel.TENANT), "boss"))
        if failure == "artifact_mismatch":
            async def corrupt(key, *, maximum_bytes):
                return b"<p>Different private original.</p>"
            objects.get_bounded = corrupt
        if failure == "owner_transfer":
            from domains.prospecting.inventory import InventoryTransferRequest
            async def transfer():
                allocated = (await inventory.list_allocations(tenant,None,10))[0]
                await inventory.transfer(tenant, allocated.id, InventoryTransferRequest(employee_id=boss,expected_employee_id=sales,expected_assigned_at=allocated.assigned_at,reason="合成实际转交"),acting_employee_id=boss)
            external.before_model = transfer
        async with SchedulerRuntimeFactory(_factory_environ(str(runtime_url),tenant,hunter_enabled=False),bootstrap=bootstrap,resolver_factory=_FactoryResolver,health_server_factory=_FactoryHealthServer)() as runtime:
            assert runtime.campaign_driver is None and settings.automatic_send_enabled is False
            assert runtime.inventory_driver is not None
            import asyncio

            from apps.scheduler_worker.main import run_scheduler_worker
            stop = asyncio.Event()
            cycles = 0
            async def stop_after_cycle(interval,event):
                nonlocal cycles
                cycles += 1
                if cycles >= (2 if private else 1): stop.set()
            await run_scheduler_worker(runtime,stop_event=stop,wait=stop_after_cycle,install_signal_handlers=False)
        # 同配置重建真实runtime再次扫描，终态及未知结果均不能重复调用外部。
        before_restart = (external.models, external.pages, external.verifications)
        async with SchedulerRuntimeFactory(_factory_environ(str(runtime_url),tenant,hunter_enabled=False),bootstrap=bootstrap,resolver_factory=_FactoryResolver,health_server_factory=_FactoryHealthServer)() as restarted:
            stop = asyncio.Event()
            async def stop_restarted(interval,event): stop.set()
            await run_scheduler_worker(restarted,stop_event=stop,wait=stop_restarted,install_signal_handlers=False)
            if configured and failure is None:
                from workflows.customer_preparation.service import PreparationBlocked
                async with sessions() as db:
                    enrollment_id = (await db.scalars(select(OutreachEnrollmentRow.enrollment_id).where(OutreachEnrollmentRow.tenant_id == tenant))).one()
                await send_guards[-1].require(tenant, enrollment_id)
                await seed_country(api.compliance, tenant, boss, now, allowed=False)
                with pytest.raises(PreparationBlocked, match="country_policy_denied"):
                    await send_guards[-1].require(tenant, enrollment_id)
        assert (external.models, external.pages, external.verifications) == before_restart
        expected_enrolled = configured and failure is None
        async with sessions() as db:
            task = (await db.scalars(select(CustomerPreparationItemRow).where(CustomerPreparationItemRow.tenant_id==tenant))).one_or_none()
            trace = list((await db.execute(select(ToolCallRow.tool_id,ToolCallRow.status,ToolCallRow.error_category).where(ToolCallRow.tenant_id==tenant))).all())
            if failure == "legacy": assert task is None
            else:
                assert task.state == ("enrolled" if expected_enrolled else "blocked"), (task.stage,task.blocked_reason,failures,trace)
                expected_reason = {"no_policy":"country_policy_denied", "policy_revoked":"country_policy_denied", "scope_withdrawn":"authorization_changed", "owner_transfer":"ownership_changed", "sender_retired":"sender_unavailable", "artifact_mismatch":"evidence_invalid", "suppressed":"enrollment_denied", "no_basis":"evidence_invalid", "unknown_verifier":"external_reconciliation_required"}.get(failure)
                if not configured: expected_reason = "verification_unavailable"
                assert task.blocked_reason == expected_reason, (failures, trace)
            assert await db.scalar(select(func.count()).select_from(OutreachEnrollmentRow).where(OutreachEnrollmentRow.tenant_id==tenant)) == int(expected_enrolled)
            assert await db.scalar(select(func.count()).select_from(ContactPointRow).where(ContactPointRow.tenant_id==tenant)) == int(expected_enrolled or failure == "unknown_verifier")
            tools = list(await db.scalars(select(ToolCallRow.tool_id).where(ToolCallRow.tenant_id==tenant)))
            assert "email.send" not in tools
            if expected_enrolled:
                assert {"web.read_page","model.generate","contact.verify"}.issubset(tools)
                point = (await db.scalars(select(ContactPointRow).where(ContactPointRow.tenant_id==tenant))).one()
                assert point.verification_status == "verified"
                assert external.pages == (2 if deepseek_search else 1)
                assert external.verifications == 1
                assert external.models == (2 if private else 1)
                assert "customer_preparation" in list(await db.scalars(select(ModelInvocationRow.capability).where(ModelInvocationRow.tenant_id==tenant)))
        allocation = (await inventory.list_allocations(tenant,None,10))[0]
        detail = await client.get("/prospects/customers/"+allocation.id)
        assert detail.status_code == 200, detail.text
        if failure == "legacy": assert detail.json()["preparation"] is None
        else: assert detail.json()["preparation"]["state"] == ("enrolled" if expected_enrolled else "blocked")
        if browser_check:
            await browser_check.check(app, tenant, boss, allocation.id)
        # API持有的认证身份与实际当前员工仍要相符；不凭客户端tenant读取原企业。
        other_tenant = TenantId(new_id("tn"))
        foreign = replace(identity, tenant_id=other_tenant)
        app.dependency_overrides[get_request_identity] = lambda:foreign
        denied = await client.get("/prospects/customers/"+allocation.id)
        assert denied.status_code in {400,403,404}
        assert allocation.candidate.name not in denied.text


@pytest.mark.e2e
@pytest.mark.parametrize("width", [1280, 390])
async def test_actual_preparation_browser(integration_engine, db_url, runtime_database_url, tmp_path, monkeypatch, width):
    await test_actual_request_and_singleton_prepare_with_sending_disabled(integration_engine, db_url, runtime_database_url, tmp_path, monkeypatch, True, False, None, browser_width=width)


async def test_preparation_without_tavily_or_search_key(integration_engine, db_url, runtime_database_url, tmp_path, monkeypatch):
    await test_actual_request_and_singleton_prepare_with_sending_disabled(integration_engine, db_url, runtime_database_url, tmp_path, monkeypatch, True, False, None, page_only=True)


async def test_deepseek_search_page_private_fit_allocation_without_tavily(integration_engine, db_url, runtime_database_url, tmp_path, monkeypatch):
    await test_actual_request_and_singleton_prepare_with_sending_disabled(integration_engine, db_url, runtime_database_url, tmp_path, monkeypatch, True, True, None, page_only=True, deepseek_search=True)
