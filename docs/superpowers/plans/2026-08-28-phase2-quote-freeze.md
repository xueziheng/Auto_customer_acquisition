# Task 3B：完整业务上下文、成本适用性与可恢复冻结 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 控制器已全文自检；T3A独立审查通过并正式派发后才实现，不派子代理或审查代理。

**Goal:** 在真实事务保护下，将完整Need事实、人工成本适用性、确定性计算与完整创建意图冻结为可恢复且不可变的报价依据。
**Architecture:** shared仅放中立事实/意图及纯编码；demand保留单位有效性规则。报价准备用途的内部guard投影员工→机会→Need，costing独立UoW保存scope确认、basis与operation；workflow只编排公开接口，不建通用事务/锁框架。
**Tech Stack:** Python 3.12+、Pydantic v2、SQLAlchemy 2.x、PostgreSQL；不增加运行依赖。
**Spec:** `docs/superpowers/specs/2026-08-28-phase2-costing-quotation-design.md`，特别是§4.1、§6、§7、§11；本brief落实控制器已选A/B方案，不修改批准规格。

## Global Constraints

- 所有记录有tenant_id；查询强制租户过滤；外键采用tenant+ID；金额只用有限Decimal，不设业务默认值。
- 一次报价一个完整产品规格、数量档、成本表、报价币种；无客户单位/来源、无政策/清单不能冻结。
- 不改T3A旧完整度、提取词表、单位确认权限/来源语义；不改旧成本API/readiness、研究、Campaign。
- 报价准备与内部成本/报价读取仅boss/product/sourcing/finance；不扩通用CRM、客户文件、原件权限。
- 客户文件仍走现有机会ABAC；审批仍由approvals决定，禁止自批；本切片不装配这两条生产路径。
- 完整事实含source_quote，仅可信内部使用；不可将context/basis/scope自动model_dump成HTTP响应或日志。
- Gateway/原件读取在所有context和成本锁之外；本切片只注入受控来源权限/抬头依赖，不注册为生产可用。
- 跨域零直接导入；业务规则在域，编排在workflow；infra只查询、映射、锁、事务，不复制角色/业务规则。
- 不push/merge/部署、不读凭证、不连接生产；控制器维护主计划与ledger，实施者只交付本切片及报告。

## 0. 前置、文件与边界

工作树 `/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-costing-quotation`。
**执行前置：T3A经控制器验收并提交，head已含0042。** 当前正在实施的T3A文件不可抢改；派发时先核对其最终公开字段/错误与本brief一致，有差异先报控制器，不重做0042。
阅读根AGENTS/HANDBOOK、shared/domains/costing/quotations/demand/workflows/quote_approval/infra/tests就近AGENTS，以及本计划。实际锁路径、完整类型与验收顺序均在本文定义。
只新增迁移 `0043_costing_quote_lock.py`（down_revision=0042）；T4报价0044、文件0045仍未实施。

| 文件（Create，除标Modify） | 单一职责 |
| --- | --- |
| `shared/schemas/quote_facts.py` | NeedQuoteFacts、QuoteEmployeeFact、QuoteRuntimeFacts纯DTO与兼容事实编码 |
| `shared/schemas/quote_creation.py` | 创建意图、terms/rounding、operation/completion中立DTO及规范化意图hash |
| Modify `domains/demand/schemas.py`、`unit_facts.py`、`service.py` | NeedQuoteFacts原名重导出同class；保持T3A hash/错误行为，单位规则仍在demand |
| `domains/quotations/context.py`、`permissions.py`；Modify `schemas.py`、`service.py`、`errors.py` | context/抬头DTO、公开端口、业务hash和报价准备纯授权；不实现报价CRUD |
| `domains/costing/freeze_schemas.py`；Modify `schemas.py`、`service.py`、`errors.py`、`permissions.py` | 本地context/scope/basis契约，schemas/service显式重导出；新增typed action和固定错误 |
| `domains/costing/quote_lock.py`、`freeze_service.py`、`freeze_repository.py` | 冻结纯校验、scope确认/计算/恢复服务、窄Repository/UoW Protocol |
| `infra/db/quote_context.py` | 同session事实投影及FOR SHARE lease，不导入其他域repository |
| `infra/db/costing_freeze_uow.py`、`infra/db/repositories/costing_freeze.py` | 专用事务/操作与scope仓储，复用T2只增依据仓储 |
| Modify `infra/db/tables.py`、`infra/db/repositories/costing.py` | 0043表与第一次locked_at窄写；不走全量update重新抢成本锁 |
| Modify `domains/costing/calculation.py`、`quote_service.py`、`quote_repository.py`、`infra/db/repositories/costing_quote.py` | 两个deferred minors；政策选择并发保护端口与SQL接入，其他T2行为不改 |
| `workflows/quote_approval/application.py` | 受控准备application、DTO转换、demand公共单位检查适配；T4在此续接真实create |
| `migrations/versions/0043_costing_quote_lock.py` | 新表、复合FK、只增/状态约束及安全downgrade |
| `tests/unit/test_quote_context_contracts.py`、`test_quote_cost_lock.py` | hash、权限、scope映射、minor及纯守卫 |
| `tests/integration/test_quote_context_locks.py`、`test_quote_cost_lock.py`、`test_quote_lock_migration.py` | 真实多连接、恢复、迁移；controlled外部端口 |
| Modify `tests/unit/test_costing_calculation.py`、`tests/integration/test_costing_quote_evidence.py`、`tests/integration/test_migrations.py` | 两个minor及head回归 |
| Modify `domains/{costing,quotations,demand}/AGENTS.md`、`docs/adr/0018-costing-quotation-contracts.md` | 记录类型迁移、用途隔离、scope/幂等/修订复用契约；不放宽九边界 |

## 1. 完整类型词典

所有新DTO strict/frozen/extra-forbid；集合用tuple。ID用已有shared NewType，新增记录ID用str+new_id前缀。
`Hash=str`仅记法，实际Annotated约束64位小写hex；时间aware→UTC；key 1..128无首尾空白/控制字符。
普通业务文本非空且拒控制字符；客户英文条款允许换行、≤4096字，unit≤64，note≤4096；资源上限不是业务默认。

### 1.1 shared事实：迁移而非重定义语义

`NeedQuoteFacts`逐字段、验证器、必填/可空规则原样搬自T3A最终实现；demand.schemas显式同名重导出，必须满足class identity。精确形状：

```text
tenant_id:TenantId; need_id:ValidatedNeedId; account_id:ProspectAccountId; status:str
product_category:FactualField[str]
application,material,size_spec,packaging,destination,current_supply_issue,
certification_required,unit:FactualField[str]|None
quantity:FactualField[int]|None; required_by:FactualField[date]|None
target_price:FactualField[Money]|None; unit_quantity_fact_hash:Hash|None; unit_confirmation_id:str|None
QuoteEmployeeFact: tenant_id:TenantId; employee_id:EmployeeId; role:Literal[
 'boss','manager','sales','sourcing','product','finance','viewer']; is_active:bool;
 manager_id:EmployeeId|None; team_id:TeamId|None
QuoteRuntimeFacts: current_actor:QuoteEmployeeFact; owner:QuoteEmployeeFact;
 preparer:QuoteEmployeeFact|None
```

Provenance/FactualField/Money仍复用shared原类。`quantity_fact_hash`、`need_quote_facts_hash`、`require_current_unit`保留demand.service原导出/异常code。
可将T3A纯canonical编码移shared，但旧`need-quantity-fact-v1`/`need-quote-facts-v1`字节与hash必须不变，包括Decimal尾零、None、UTC与所有Provenance。新意图使用独立版本的Decimal规范化编码，不能“统一normalize”改变旧事实hash。
shared不抛demand错误；原domain包装层继续映射既有NeedUnitError。unit有效性/正数量/人工确认仍由demand公共纯函数决定。

### 1.2 业务context与完整规格

`QuoteIssuer`（quotations）= `issuer_id:str, content_hash:Hash, name,address,contact,source_ref:str, confirmed_by:EmployeeId,confirmed_at:datetime, field_provenance:dict[str,Provenance]`。字段name/address/contact分别带完整Provenance；T4由老板直接确认EMPLOYEE_INPUT事实，source_ref/source_id为服务器生成的issuer确认ID，不冒称外部原件已核验。T3B受控reader必须提供该完整形状。
`QuoteSpecificationFacts`（quotations）= `product_category:str,application,material,size_spec,packaging,certification_required:str|None`，全部显式必填。
`QuoteBusinessContext`（quotations）= `tenant_id:TenantId,opportunity_id:OpportunityId,need_id:ValidatedNeedId,account_id:ProspectAccountId,opportunity_state:str,owner_id:EmployeeId,prepared_by:EmployeeId,account_name,country,category,specification,unit,destination:str,quantity:int,need_facts:NeedQuoteFacts,need_facts_hash:Hash,specification_hash:Hash,issuer:QuoteIssuer,runtime:QuoteRuntimeFacts,context_hash:Hash`。
`CostingContext`（costing）= 同名tenant/opportunity/need/account/state/owner/prepared_by/category/specification/unit/destination/quantity、need_facts/need_facts_hash/specification_hash/runtime/context_hash；不含issuer/客户名称，使用原context_hash而不重算另一套。

公开纯函数（quotations.service重导出context.py实现）：
`quote_specification(facts:NeedQuoteFacts)->QuoteSpecificationFacts`；
`canonical_quote_specification(spec:QuoteSpecificationFacts)->str`（固定键JSON，版本quote-specification-v1，保留None/原值）；
`quote_specification_hash(spec:QuoteSpecificationFacts)->str`；`quote_context_hash(context:QuoteBusinessContext)->str`。
QuoteBusinessContext.context_hash是只读computed_field，不接收构造输入；纯函数从其他字段构造载荷，禁止临时填伪hash。CostingContext接收该派生值。business hash覆盖tenant/机会/账户/Need关联、full need hash、owner、原prepared_by、客户名称/国家、展示事实及issuer_id/hash；排除runtime/本次actor/checked_at，以及正常流转的Opportunity.state。
material/packaging不能丢；显示规格可确定性逐字段排版，但供应商原specification保持原文，绝不与canonical JSON作等价判断。
context的scalar必须来自facts并校验一致；destination、quantity、unit必须存在，数量正整数、客户单位绑定有效；报价规格至少material/size_spec/application之一有非空事实，其他None不补造。

### 1.3 创建意图（shared/quote_creation.py）

```text
QuoteTerm: kind:Literal['discount','delivery_commitment','payment_terms','certification_commitment']; text:str
QuoteRoundingInput: unit_places:int;total_places:int;strategy:str
QuoteCreationIntent:
 tenant_id:TenantId;prepared_by:EmployeeId;opportunity_id:OpportunityId;cost_sheet_id:CostSheetId
 expected_context_hash:Hash;expected_sheet_hash:Hash;valid_until:datetime;unit_price:Money
 rounding:QuoteRoundingInput;quote_fx_ref:str|None;terms:tuple[QuoteTerm,...]
 replaces_quote_id:QuoteId|None;expected_quote_version:int|None
 scope_confirmation_id:str;scope_confirmation_hash:Hash
QuoteCreationCompletion:
 tenant_id:TenantId;operation_id:str;request_hash:Hash;basis_id:str;quote_id:QuoteId;quote_version:int
 quote_content_hash:Hash;replaces_quote_id:QuoteId|None;replaced_quote_version:int|None
QuoteCreationOperationView:
 tenant_id:TenantId;operation_id:str;idempotency_key:str;request_hash:Hash;intent:QuoteCreationIntent
 basis_id:str;state:Literal['frozen','completed'];created_at:datetime
 completion:QuoteCreationCompletion|None;completed_at:datetime|None
```

rounding精度0..12、strategy只允许Money现有舍入策略。revision两个字段同None或同有，version正整数；unit_price正值、有限Decimal。terms为空可以，顺序与重复项不可擅自去重；不支持的商业承诺明确拒绝，不用自由note绕过T5。
shared只检形状；领域校验kind与其文本/后续审批映射。quotations.schemas重导出QuoteTerm/QuoteRoundingInput。
`quote_creation_request_hash(intent:QuoteCreationIntent)->str`在shared实现、quotations.service重导出；版本quote-create-request-v1，完整intent canonical JSON、Decimal无损规范化、UTC，保留None/terms次序；不含key/operation_id/now/当前审批人。
T4的QuoteDraftCommand必须包含scope_confirmation_id；scope hash从持久记录解析，HTTP不传prepared_by/确认hash/已锁标记。

### 1.4 scope与冻结（costing，内部含敏感事实）

```text
CostScopeEvidenceBinding: evidence_id:str;evidence_hash:Hash;applicability_note:str
CostScopeAccess: tenant_id:TenantId;actor_id:EmployeeId;evidence_bindings:tuple[CostScopeEvidenceBinding,...]
CostScopeConfirmationCommand:
 coverage_id:str;expected_sheet_hash:Hash;expected_coverage_hash:Hash;expected_need_facts_hash:Hash
 terms:tuple[QuoteTerm,...];valid_until:datetime;evidence_bindings:tuple[CostScopeEvidenceBinding,...]
CostScopeConfirmationView:
 tenant_id:TenantId;confirmation_id:str;opportunity_id:OpportunityId;need_id:ValidatedNeedId
 cost_sheet_id:CostSheetId;sheet_hash:Hash;coverage_id:str;coverage_hash:Hash
 need_facts:NeedQuoteFacts;need_facts_hash:Hash;specification:str;specification_hash:Hash
 terms:tuple[QuoteTerm,...];terms_hash:Hash;valid_until:datetime
 evidence_bindings:tuple[CostScopeEvidenceBinding,...];content_hash:Hash;provenance:Provenance
FrozenCostBasis:
 tenant_id:TenantId;basis_id:str;operation_id:str;request_hash:Hash;opportunity_id:OpportunityId
 cost_sheet_id:CostSheetId;context_hash:Hash;sheet_hash:Hash;basis_hash:Hash;policy_id:str
 quantity:int;specification,unit,destination:str;need_facts:NeedQuoteFacts
 scope_confirmation:CostScopeConfirmationView;policy:PricingPolicyView;coverage:CostCoverageView
 calculation:CalculationSnapshot;price_evidence:tuple[PriceEvidenceView,...]
 pricing_options:PricingOptions;quote_fx:QuoteFxView|None;valid_until:datetime;frozen_at:datetime
```

scope全部依据ID/hash精确覆盖coverage引用集合，按ID排序且唯一；各自applicability_note是人工业务映射说明，不冒充客户/供应商原话。原PriceEvidence自由文本与来源完整保留在basis。
`cost_scope_hash(view:CostScopeConfirmationView)->str`（costing.service公开）排除content_hash自身，但含全部scope、确认记录身份、evidence mapping及确认Provenance；terms_hash用shared纯编码的quote-terms-v1。
FrozenCostBasis的basis_hash覆盖全部业务快照/操作绑定，排除basis_hash自身、frozen_at与随机basis_id；计算inputs_hash沿用T1，不挤入新的计算公式。
T4 QuoteBasis须等值携带完整scope确认、Need事实及原始供应商specification/evidence，不能缩为hash后失去第二道规格映射校验；T3B不实现T4存储。

## 2. 公共端口、构造与错误

原T2 `CostingQuoteService`只管依据确认，新增独立 `CostingFreezeService`，避免膨胀quote_service.py；均在costing.service公开。

```python
class QuoteContextProvider(Protocol):  # quotations.service
    def open(self, tenant_id: TenantId, opportunity_id: OpportunityId,
             actor_id: EmployeeId, *, prepared_by: EmployeeId
             ) -> AsyncContextManager[QuoteBusinessContext]: ...
class QuoteIssuerReader(Protocol):
    async def get_confirmed(self, tenant_id: TenantId) -> QuoteIssuer: ...
class QuotePreparationPolicy(Protocol):
    def require(self, tenant_id: TenantId, actor: QuoteEmployeeFact,
                *, action: Literal['prepare','read_internal']) -> str: ...
class NeedFactsValidator(Protocol):  # costing.service；上层适配demand公共纯函数
    def require_current_unit(self, facts: NeedQuoteFacts) -> FactualField[str]: ...
class CostScopeSourceAccess(Protocol):  # 只确认当前能读这些已持久来源，不决定业务适用性
    async def require(self, tenant_id: TenantId, evidence: tuple[PriceEvidenceView, ...],
                      *, actor_id: EmployeeId) -> None: ...
class QuoteCreationCompletionReader(Protocol):  # T4真实持久quote→receipt，T3B受控
    async def read(self, tenant_id: TenantId, operation_id: str,
                   *, actor_id: EmployeeId) -> QuoteCreationCompletion | None: ...
class CostingFreezeService(Protocol):
    async def prepare_scope_access(self, tenant_id: TenantId, cost_sheet_id: CostSheetId,
        command: CostScopeConfirmationCommand, *, actor: CostingActor) -> CostScopeAccess: ...
    async def confirm_scope(self, tenant_id: TenantId, cost_sheet_id: CostSheetId,
        command: CostScopeConfirmationCommand, context: CostingContext, *,
        actor: CostingActor, idempotency_key: str,
        source_access: CostScopeAccess) -> CostScopeConfirmationView: ...
    async def get_scope(self, tenant_id: TenantId, confirmation_id: str,
                        *, actor: CostingActor) -> CostScopeConfirmationView: ...
    async def calculate(self, tenant_id: TenantId, cost_sheet_id: CostSheetId,
        options: PricingOptions, context: CostingContext, *, quote_fx_ref: str | None, actor: CostingActor
        ) -> CalculationSnapshot: ...
    async def freeze(self, tenant_id: TenantId, cost_sheet_id: CostSheetId,
        options: PricingOptions, context: CostingContext, *, idempotency_key: str,
        intent: QuoteCreationIntent, actor: CostingActor) -> FrozenCostBasis: ...
    async def get_frozen(self, tenant_id: TenantId, basis_id: str,
                         *, actor: CostingActor) -> FrozenCostBasis: ...
    async def get_creation(self, tenant_id: TenantId, idempotency_key: str,
                           *, actor: CostingActor) -> QuoteCreationOperationView | None: ...
    async def complete_creation(self, tenant_id: TenantId, operation_id: str,
                                *, actor: CostingActor) -> QuoteCreationOperationView: ...
```

complete_creation不接调用者手写quote_id/receipt；先在锁外调用注入completion_reader，校验后持久应用；T4由真实报价表读取，T3B仅受控实现，未装配则dependency_unavailable。
构造 `CostingFreezeServiceImpl(uow_factory:CostingFreezeUowFactory, actor_reader:CostingActorReader, need_validator:NeedFactsValidator, source_access:CostScopeSourceAccess, completion_reader:QuoteCreationCompletionReader, *, now:Callable[[],datetime])`；依赖无默认。
source_access仅confirm_scope前置调用，必须在context lease外：由application预取/授权后进入guard；服务中的confirm_scope不得再次在锁内执行外部调用。CostScopeAccess仅限本次可信application调用产生，不是可持久复用的外部凭证；非HTTP、不是verified布尔值，不替代本次actor/持久evidence hash核对。
`prepare_scope_access(tenant_id:TenantId,cost_sheet_id:CostSheetId,command:CostScopeConfirmationCommand,*,actor:CostingActor)->CostScopeAccess`新增到CostingFreezeService：短UoW读取并关闭→source_access.require→返回精确绑定。confirm_scope核对票据tenant/actor/全部evidence；确认前后身份由同guard保护。

`QuotePreparationApplication`（workflow）的构造：context_provider、costing:CostingFreezeService、policy:QuotePreparationPolicy、actors:CostingActorReader；方法：
`confirm_scope(tenant_id,opportunity_id,cost_sheet_id,command,*,actor_id,idempotency_key)->CostScopeConfirmationView`；
`calculate(tenant_id,opportunity_id,cost_sheet_id,options,*,quote_fx_ref:str|None,actor_id)->CalculationSnapshot`；
`freeze(tenant_id,intent:QuoteCreationIntent,options:PricingOptions,*,actor_id:EmployeeId,idempotency_key:str)->FrozenCostBasis`，均async，ID类型依上表。
此application仅可信内部，不是HTTP接口；create场景强制intent.prepared_by==actor_id，记录operation读取/完成另调公开costing方法。T4再实现QuoteApplicationService.create，不能此时假返回QuoteView。
provider适配构造 `SqlAlchemyQuoteContextProvider(session_factory:async_sessionmaker[AsyncSession],issuer_reader:QuoteIssuerReader,*,lock_timeout_ms:int,statement_timeout_ms:int)`；runtime事实/tenant校验之外的权限/单位规则由上层/域执行。

新增动作 `CostingAction.SCOPE_CONFIRM/QUOTE_CALCULATE/QUOTE_FREEZE/QUOTE_OPERATION_READ/QUOTE_OPERATION_COMPLETE`；仍仅四成本角色且显式tenant scope。当前actor由EmployeeId reader重读，必须与传入actor一致，失活/角色变化拒绝；context.runtime.current_actor也须匹配。
QuotePreparationPolicy纯实现只开放prepare/read_internal四角色；无默认CRM许可。future customer-file/apply-approval不能借该policy，保留各自既有域授权。

错误统一固定code/中文消息，不附SQL/DSN/原文：
`QuoteContextError(ValidationError)`、`QuoteContextPermissionError(PermissionDenied)`、`QuoteContextUnavailableError(TradeOSError)`；costing等值为`CostFreezeError/CostFreezePermissionError/CostFreezeUnavailableError`。构造仅 `(code:对应Literal)`。
Validation类code：`invalid_input,context_changed,facts_missing,facts_corrupt,unit_missing,unit_stale,fact_unconfirmed,specification_mismatch,quantity_mismatch,destination_mismatch,coverage_stale,scope_stale,evidence_invalid,evidence_expired,policy_missing,fx_missing,idempotency_conflict,operation_pending,revision_conflict`。
Permission类仅`permission_denied`；Unavailable类`dependency_unavailable,lock_timeout,storage_unknown`。get不存在用`record_not_found` Validation类，T8映射404；冲突409、输入422、权限403、未知/超时503。保留T3A原错误行为，只在跨端口映射固定code。
低利润计算不报冻结失败，保留metrics供T5独立margin_floor_override；不在T3B批准例外。

## 3. 窄持久接口与0043

`CostingFreezeUow`继承结构上已有CostingUnitOfWork端口，另有 `freezes:CostingFreezeRepository`；新专用实现复用仓储，不扩大其他T2事务职责。`CostingFreezeUowFactory.__call__(tenant_id:TenantId)->CostingFreezeUow`。
`CostingFreezeRepository`的async方法（每个均有tenant参数）：

```text
lock_key(tenant_id:TenantId,kind:Literal['scope','creation'],key:str)->None
get_scope_by_key(tenant_id,key)->StoredCostScope|None
get_scope(tenant_id,confirmation_id:str)->CostScopeConfirmationView|None
add_scope(tenant_id,record:StoredCostScope)->None
get_operation_by_key(tenant_id,key)->QuoteCreationOperationView|None
get_operation(tenant_id,operation_id:str)->QuoteCreationOperationView|None
pending_for_sheet(tenant_id,cost_sheet_id:CostSheetId)->QuoteCreationOperationView|None
get_basis(tenant_id,basis_id:str)->FrozenCostBasis|None
add_frozen(tenant_id,basis:FrozenCostBasis,operation:QuoteCreationOperationView)->None
complete(tenant_id,operation_id:str,receipt:QuoteCreationCompletion,at:datetime)->None
mark_sheet_locked_once(tenant_id,cost_sheet_id:CostSheetId,at:datetime)->None
```

所有简写tenant_id为TenantId、key为str；`StoredCostScope(view:CostScopeConfirmationView,idempotency_key:str,request_hash:Hash)`是costing内部DTO。
key advisory xact lock覆盖“行尚不存在”；作用域包含tenant/kind/key，不能只锁已存在行。所有读写同session。
`SqlAlchemyCostingFreezeUow(factory,tenant_id,*,now:Callable[[],datetime],lock_timeout_ms:int,statement_timeout_ms:int)`显式正配置，参数化set_config；异常含取消均rollback/close。

0043新增：

- `cost_scope_confirmations`：tenant/id/sheet/opportunity/need/coverage/key/request_hash/content_hash/need_facts_hash/specification_hash/terms_hash/confirmed_by/confirmed_at显式列，完整view JSONB；UNIQUE(tenant,key)、UNIQUE(tenant,id)，到sheet/coverage/Need/Employee复合FK；UPDATE/DELETE拒绝。
- `costing_quote_bases`：tenant/basis_id/operation_id/cost_sheet_id/opportunity_id/scope_confirmation_id/request_hash/context_hash/sheet_hash/basis_hash/policy_id/valid_until/frozen_at显式列，完整basis JSONB；UNIQUE(tenant,basis_id)、UNIQUE(tenant,operation_id)；FK到scope/sheet/policy；UPDATE/DELETE拒绝。
- `quote_creation_operations`：tenant/operation_id/key/request_hash/sheet/basis/state/created_at/completed_at及intent/completion JSONB；UNIQUE(tenant,key)、UNIQUE(tenant,operation_id)；basis FK，basis/operation双向关联可采用明确DEFERRABLE INITIALLY DEFERRED复合FK并在事务内验证，不关闭约束。
- operation部分唯一 `(tenant_id,cost_sheet_id) WHERE state='frozen'`，**不是永久unique sheet**。只允许frozen→completed；intent/key/hash/basis等不可改，completion仅首次填写且之后不可改；删除拒绝。JSON显式对象、hash格式、UTC时间、state/completion同空性CHECK；金额JSON用字符串。
- 不建quotation FK假表；T4加真实quotation→operation唯一FK。0043 receipt的quote_id仅可信端口事实，不能宣称数据库已验证quote存在。
- locked_at第一次更新在已持sheet FOR UPDATE事务里窄写；复用已锁sheet时不更新locked_at。旧repo.update继续拒绝已锁表。
- downgrade若三新表有业务记录先拒绝并提示需授权归档；空库可downgrade→upgrade；不改0042/旧迁移。

## 4. 主流程与锁顺序

### 4.1 context lease

无锁bootstrap仅发现候选owner/need/account；已确认issuer读取不含外部bytes。新session对actor/owner/存在的preparer去重排序FOR SHARE → Opportunity FOR SHARE并重读关联 → Need FOR SHARE并完整投影。
owner/need/account相对bootstrap变化即整轮释放、context_changed，不持Opportunity锁补锁新员工；必须刷新ORM旧缓存。tenant/账户/Need关系逐项校验。
不调用resolve_owner/assign；账户OwnershipLock与Opportunity.owner不是同一事实。无跨session写已锁行回调；FOR SHARE不升FOR UPDATE，允许FK KEY SHARE。
生成完整context后由application执行四角色policy、demand单位校验和costing调用；直到scope确认/冻结事务提交完才退出lease。get_facts短事务不能替代同session投影。历史preparer不强制当前在职；当前actor必须在职。

### 4.2 人工scope确认

读当前actor → prepare_scope_access（短读结束后来源ACL受控端口，零锁）→ context lease → policy → confirm_scope。
confirm_scope：匹配票据/命令/context → scope key锁 → 同key校验request_hash并原样返回 → sheet FOR UPDATE → 重新读覆盖及全部immutable依据 → demand注入校验 → 范围/quoted/有效期/金额/重复费用硬检查 → 人工映射完整性 → 保存scope，提交→退出lease。
请求hash包括tenant/sheet/actor/命令/full need hash；票据不是“适用性已经批准”，人工命令才是业务确认。确认Provenance为EMPLOYEE_INPUT，source_id指确认记录，extracted_by/confirmed_by为当前员工ID，时间注入同一now。
每条supplier evidence原unit/destination必须与当前事实完全一致；数量在min/max且满足MOQ；kind supplier_price且basis quoted。自由specification通过绑定到full Need的人工mapping_note对应，不能改写原文、猜别名或接受quote flag代替证据。
费用逐项复验T2的币种、is_per_unit、适用quantity、source_line_ref、allocation_scope及22项完整性；共享纯验证器可在costing域内提取，不复制规则两份，不调用另开UoW的confirm_coverage。
scope同时绑定material/packaging/required_by/destination/full Provenance、terms与valid_until；变化即需新人工确认，不能freeze自动给旧coverage贴新hash。金额确实未变也须新scope记录。

### 4.3 计算、freeze、恢复

calculate允许target/manual但仅当前四角色；读取当前sheet/coverage/policy、按quote_fx_ref解析已确认FX、当前完整Need后调用T1，不写锁定/operation。options若带quote_fx须逐字段等于该持久确认；同币ref必须None，异币缺ref拒绝，不能把FxRate.source自由文本当作fx_id。所得context_hash只用于乐观重验，不承诺以后仍有效。
freeze只允许manual、intent.unit_price等于options.unit_price、rounding/FX引用及所有重复字段一致；intent.prepared_by在新建时等于当前actor，options不接受HTTP未经解析的FX事实。
顺序：当前actor/policy门→context lease（application）→creation key锁→查winner→sheet FOR UPDATE→查其他pending operation→当前coverage/scope/价格/policy/FX核验→T1计算→basis+operation+首次locked_at同事务→commit→释放lease。
同key同完整intent且当前context/适用性仍有效时返回原basis，异terms/valid_until/replaces/version/scope/actor均冲突；相同key换sheet也冲突。旧basis与当前context失配可用get_creation查询恢复状态，但freeze不得把旧passed作为当前有效；返回context_changed/scope_stale。
政策选择需防并发新确认插入：freeze持tenant级“当前报价政策”共享advisory xact lock，T2 confirm_policy保存前持同key独占锁；纯策略仍在域，SQL锁在UoW/仓储。该窄锁只保护确认与选当前政策，不搭全局锁框架；两边固定在policy读取/写入前取得，且confirm_policy不锁sheet。
精确端口为现有PricingPolicyRepository新增 `lock_selection(tenant_id:TenantId,*,exclusive:bool)->None`（async）；SQL实现用同tenant-key的pg_advisory_xact_lock_shared/pg_advisory_xact_lock。confirm_policy在其幂等键锁之前取exclusive，freeze在creation key→sheet之后取shared，不互相回取对方key。
有效期均以取得本次key/sheet/政策锁之后的单次注入now核验，不能使用来源IO或等待锁之前的旧时钟；客户valid_until不得晚于任一有界依据，scope.valid_until必须等于intent。scope确认的confirmed_at同样在锁取得后确定。FX必须按成本表核算币→报价币直连，quoted价格/确认/政策缺失不冻结。
pending operation存在时新key拒绝operation_pending，不自动解锁；整个事务异常回滚，commit未知返回storage_unknown，仅原key恢复。
get_creation/get_frozen/get_scope仍检查当前四角色，返回内部DTO但无HTTP直接序列化；读取历史不等于当下可对客使用。
complete_creation：锁外读真实/受控receipt→creation key锁→sheet锁→重读operation/basis→精确比tenant/op/basis/request/replaces/version→写completion并completed。无receipt不完成；同receipt重复no-op，另一quote/内容拒绝revision_conflict；未知提交同key恢复。
T4写quote须UNIQUE(tenant,operation_id)，先找原quote后完成operation；T3B不模拟生产quote写成功。

### 4.4 完成后的显式修订复用

completed后可用新key+replaces_quote_id+expected_quote_version复用未变锁表；必须找到该sheet上匹配旧quote/version的不可变completed receipt。该receipt仅由complete_creation的可信reader核验后写入，freeze不在持锁区间重复调用跨域reader；T4另在freeze前读取当前报价并在写入事务CAS核验活跃版本。无真实completion reader时生产完结不可用，T3B的受控receipt不算真实报价完成。
scope、basis、计算全是新记录；旧locked_at/items/成本FX/basis/intent零改写。sheet hash必须相同；修改成本或表内核算FX须新成本版本。独立报价FX不属于旧表FX元组，可选择新的已确认quote_fx_ref并进入新intent/basis、重算利润，不覆写旧报价FX记录；报价币种本身仍与成本表一致。
仅改条款/有效期也需新scope人工确认；现政策或依据不适用时阻断，不能靠复用续命。并发修订最终由T4真实quote事务expected version/active唯一约束裁决；T3B最多保护同sheet pending，不能宣称跨不同sheet的修订并发已验收。

## 5. RED → GREEN：分成四个可独立审查提交

### Task B1：共享事实/意图与两个minor

- [ ] 先写T3A类型identity、既有golden hash完全不变、意图terms/None/order/金额表示的测试；加同币种无关FX失败测试。

```python
def test_need_fact_public_type_identity():
    from domains.demand.schemas import NeedQuoteFacts as Public
    from shared.schemas.quote_facts import NeedQuoteFacts as Shared
    assert Public is Shared

@pytest.mark.parametrize("mode", ["target", "manual"])
def test_same_currency_does_not_accept_unrelated_identity_fx(mode):
    calc = importlib.import_module("domains.costing.calculation")
    from types import SimpleNamespace
    sheet = SimpleNamespace(base_currency="USD", quote_currency="USD")
    options = SimpleNamespace(quote_fx=_rate("EUR", "CNY", "1"))
    with pytest.raises(ValidationError):
        if mode == "target":
            calc._base_to_quote_price(Decimal("2"), sheet=sheet, options=options)
        else:
            calc._quote_to_base_revenue(Money(Decimal("2"), "USD"), sheet=sheet, options=options)
```

测试放既有test_costing_calculation.py复用其_rate/import；GREEN改两处同币分支为仅None或base=quote=当前币种且rate=1。新冻结同币quote_fx_ref必须None，不伪造T2不接受的同币FX确认。
另将CostingActorReader.read_current参数改EmployeeId；quote_service及全部受控reader调用显式EmployeeId转换，保留当前身份比对行为。
- [ ] RED：`python3 -m pytest tests/unit/test_quote_context_contracts.py tests/unit/test_costing_calculation.py -q`；失败须为新行为，不计环境ImportError。
- [ ] 按§1搬类型/公开重导出、加纯hash及两个minor；新增意图测试构造完整显式fixture，每改一个字段确认hash变化，不使用生产默认。
- [ ] GREEN与T3A/T2定向回归后commit `feat: 明确报价事实与创建意图公共契约`。

### Task B2：真实context lease与用途权限

- [ ] RED新增四角色prepare/read_internal允许、sales/manager/viewer拒绝；不改变既有CRM矩阵、T3A交集、inbox/附件ACL。上下文含source_quote但任何准备用途对外结果不含它。
- [ ] RED实现测试fixture `context_case`：显式tenant/actor/owner/Need/Opportunity、confirmed issuer受控reader、独立sessionmaker；fixture创建数据均为受控事实，提供 `provider,actor_id,opportunity_id,prepared_by`。

```python
async def test_context_does_not_lock_after_scope_exit(context_case):
    c = context_case
    async with c.provider.open(c.tenant_id, c.opportunity_id, c.actor_id,
                               prepared_by=c.prepared_by) as context:
        assert context.need_facts.quantity.value == context.quantity
        assert context.runtime.current_actor.employee_id == c.actor_id
    await c.update_owner_manager()  # 独立连接、真实EmployeeRepository.update，有界超时
```

fixture的quantity为显式整数，`update_owner_manager()->None`每次独立session并commit；生产代码不含测试hook。
- [ ] RED：`env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quote_context_contracts.py tests/integration/test_quote_context_locks.py -q`。
- [ ] GREEN实现§4.1，业务授权留quotations纯policy/application；adapter只做SQL/DTO。用事件/barrier控制下列独立连接交错，不靠长sleep：
  1. actor.role/is_active与owner.manager_id更新双方先后；真实EmployeeRepository.update被SHARE阻塞或读取到新值。
  2. OpportunityService.assign在bootstrap之后提交→context_changed；已持SHARE时assign等待；不把账户transfer误当assign。
  3. DemandService.update_need_fields改quantity/material/size_spec/packaging/required_by；同值新来源也改变hash；未退出lease前写方不能通过。
  4. actor==owner去重、两个context并行、FK KEY SHARE兼容；跨tenant/账户错配拒绝；异常/取消/超时后等待写方完成。
- [ ] 定向GREEN后commit `feat: 保护报价准备的当前事实与用途权限`。

### Task B3：0043与人工scope确认

- [ ] RED先测material变而size_spec/sheet_hash不变仍拒旧scope；raw supplier specification与canonical JSON不同不自动拒绝/通过，必须有显式完整人工映射并仍过unit/destination/quantity硬检查。
- [ ] 创建专用隔离DB fixture `freeze_case`：真实0041/0042/0043迁移、真实T2确认服务+成本仓储、真实demand更新、真实context provider和FreezeService；actor/source_access/issuer/completion是显式受控依赖，fixture不挂生产。
  字段 `tenant_id,actor,actor_id,opportunity_id,cost_sheet_id,provider,service,scope_command,intent,options,factory`均用§1/§2类型；`scope()->CostScopeConfirmationView`通过真实application确认；`count(table:Literal['scope','basis','operation'])->int`独立tenant-filtered连接查询；`change_material()->None`走真实DemandService。

```python
async def test_material_change_cannot_reuse_old_scope(freeze_case):
    c = freeze_case
    confirmed = await c.scope()
    await c.change_material()
    async with c.provider.open(c.tenant_id, c.opportunity_id, c.actor_id,
                               prepared_by=c.actor_id) as current:
        with pytest.raises(CostFreezeError) as error:
            await c.freeze_using(confirmed, current)
    assert error.value.code == "scope_stale"
    assert await c.count("basis") == 0
```

`freeze_using(scope:CostScopeConfirmationView,current:QuoteBusinessContext)->FrozenCostBasis`为fixture方法：显式本域投影、intent仅刷新expected_context_hash，保留旧scope ID/hash，调用真实service.freeze，不能mock守卫。
- [ ] RED：`env -u TEST_DATABASE_URL python3 -m pytest tests/integration/test_quote_cost_lock.py tests/integration/test_quote_lock_migration.py -q`。
- [ ] GREEN实现窄repo/UoW/migration与scope流程；测同key同命令一记录、同key异Need/terms/actor冲突、来源权限失败零写、检查后Need/员工变更不能穿透guard、确认中断无部分记录。
- [ ] 测UPDATE/DELETE/跨tenant FK/JSON损坏/第一次locked_at；迁移upgrade→downgrade→upgrade和有业务数据downgrade拒绝；ORM列约束与head=0043一致。测试skip Docker不可用不算通过。
- [ ] GREEN后commit `feat: 记录与完整需求绑定的成本适用性确认`。

### Task B4：冻结、恢复与completed后修订

- [ ] RED写完整意图幂等矩阵，same key改变terms/valid_until/replaces/expected version/scope/actor/sheet任一项均冲突；不同连接同key仅一个operation/basis、同sheet不同key仅一个pending。
- [ ] 写故障点测试：basis后/operation后/locked_at前抛错全回滚；commit结果未知只原key恢复；完结receipt缺失/错tenant/hash/basis/quote拒绝；重复同receipt一次效果。

```python
async def test_pending_cannot_be_bypassed_with_new_key(freeze_case):
    c = freeze_case
    first = await c.freeze(key="creation-1")
    with pytest.raises(CostFreezeError) as error:
        await c.freeze(key="creation-2")
    assert error.value.code == "operation_pending"
    assert (await c.freeze(key="creation-1")).basis_id == first.basis_id
    assert await c.count("operation") == 1
```

fixture `freeze(*,key:str)->FrozenCostBasis`先建立/复用同一真实scope，再经application同guard调用；intent完全相同，仅外部key变动。
- [ ] RED：`env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quote_cost_lock.py tests/integration/test_quote_cost_lock.py -q`。
- [ ] GREEN实现§4.3/4.4、typed错误、数据只增、T1计算；freeze与实际add_item竞争、政策确认与freeze竞争、现FX方向/有效期/低利润不越权均覆盖。
- [ ] 用受控completion_reader模拟T4已持久receipt：完成后新key+显式revision复用同sheet、旧时间/依据不改；新scope/basis；改表内成本FX/成本拒绝，新的已确认独立报价FX可进入新basis并重算；缺expected version拒绝。记录“仅receipt契约测试”，不能标真实T4闭环通过。
- [ ] application同一lease覆盖costing commit；source_access.require的受控回调检查未持context/成本锁；无另开session更新同一锁行的自等待。
- [ ] GREEN后commit `feat: 冻结报价依据并持久恢复创建操作`。

## 6. 最终验证、交接与未运行边界

```bash
export PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/Users/xueziheng/.nvm/versions/node/v24.15.0/bin:$PATH"
export PYTHONPATH="$PWD"
env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quote_context_contracts.py tests/unit/test_quote_cost_lock.py tests/unit/test_costing_calculation.py tests/unit/test_need_units.py tests/integration/test_quote_context_locks.py tests/integration/test_quote_cost_lock.py tests/integration/test_quote_lock_migration.py tests/integration/test_need_units.py tests/integration/test_need_unit_migration.py tests/integration/test_costing_quote_evidence.py tests/integration/test_costing_repository.py tests/integration/test_migrations.py -q
python3 scripts/check_boundaries.py
```

- [ ] 每个多连接用例记录READ COMMITTED及独立连接数，事件/barrier控制时序、显式timeout；同连接savepoint不得冒充并发。
- [ ] 自审无dict/假verified跨边界、无默认参数、无T3A hash变化、无模型猜规格、无schema原文自动HTTP输出、无永久unique sheet；两minor均列入报告。
- [ ] 交付本切片提交范围、测试命令/结果、已知限制，写 `task-3b-report.md`；不修改控制器ledger/主计划。
- [ ] T4尚未运行：真实quotation创建/修订CAS/同operation唯一、receipt实际来源、QuoteBasis第二道映射、真实抬头持久reader。
- [ ] T5/T8尚未运行：真实审批ABAC、客户文件机会ABAC、Gateway原件ACL reader、真实API/scheduler装配；T3B受控依赖不得标production-ready。
- [ ] 交接T4：全intent/request_hash、scope完整快照与原始specification不可裁掉；创建成功即使complete失败也按tenant+operation找回；完成前不得把quote返回当操作已全部提交。
- [ ] T4在freeze前持报价业务专用机会锁验证replaces为当前active，或无active时的latest expired，且version匹配；同quotation session保持至创建提交并做CAS，不能另开连接对外层已锁Opportunity取FOR UPDATE。仅active旧版转superseded，expired旧版原样保留。T3B completed receipt只证明旧创建事实，不提供当前版本授权。
