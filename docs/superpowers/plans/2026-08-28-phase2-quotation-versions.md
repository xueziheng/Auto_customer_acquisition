# Task 4：不可变报价版本与真实创建恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans。按下列TDD切片连续执行，不派子代理。控制器已全文自检；T3B最终交付的接口须核对后才能派发。本文件不是已验收实现。

**Goal:** 将T3B真实冻结操作落为单产品、不可改写的报价版本，完成持久receipt恢复、机会级修订CAS、老板抬头确认与安全客户投影。
**Architecture:** quotations拥有内容、版本/状态与本业务creation session；workflow仅转换两个域公开DTO并编排。外层context lease → 报价机会advisory事务锁/preflight → costing独立freeze → 同报价session提交 → 锁外complete。保留旧骨架接口，不注册绕过校验的兼容实现。
**Tech Stack:** Python 3.12+、Pydantic v2、SQLAlchemy 2.x、PostgreSQL；不新增运行依赖。
**Spec:** `docs/superpowers/specs/2026-08-28-phase2-costing-quotation-design.md` §6/§7；主计划Task4及`docs/superpowers/plans/2026-08-28-phase2-quote-freeze.md`。下列内容已包含执行中接口核对的裁定，不依赖临时交接记录，不扩功能或权限。

## Global Constraints

- Decimal/Money确定性计算；所有数据及查询tenant-bound、复合FK；无默认actor/单位/政策/汇率，无客户端已锁/已核验/已批准标志。
- 新创建及内部读取只boss/product/sourcing/finance；人工抬头仅boss。通用CRM、客户文件机会ABAC、原件ACL、审批授权互不替代。
- 完整Need、basis、scope、Provenance可能包含source_quote；仅可信内部处理，禁止自动HTTP序列化、日志打印或复制到客户DTO。
- 不改旧QuoteView/QuoteLineView/Quote/QuoteLine构造；旧create_draft、apply_approval_result(bool)、mark_sent(str)等不装配生产，也不转调新入口。
- 不自动审批/发送、不把PDF下载当发送、不实现T5审批应用/T6文件/T8真实Gateway装配；T4仅受控reader+真实DB验证。
- 不导入其他域内部模块。新共享CustomerQuoteView只有一个定义；业务校验仍在域，infra不复制角色或报价适用性规则。
- 不改主计划/规格/ledger，不读凭证/生产来源，不运行生产迁移；按任务文件提交，不夹带其他代理改动。

## 0. 执行前置与文件

工作树：`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-costing-quotation`。先读根AGENTS/HANDBOOK及shared/domains/quotations/workflows/quote_approval/infra/tests就近AGENTS。
**本文不将T3B视为已验收交付。** 派发前控制器必须确认：0043已验收；shared事实/意图、完整QuoteIssuer、CostingFreezeService、context lease、scope/basis/operation接口与本brief一致；0043包含未完成操作的sheet部分唯一约束、真实completion-reader注入口、独立quote_fx_ref及policy锁修正。差异先报控制器，不擅改T3A/T3B。T3B内部completed_for_revision窄查询不进入T4应用，公共CostingFreezeService不变。
只新建`0044_quotations.py`，down_revision使用实际0043 revision；0045归T5审批契约，文件切片顺延0046。不回改0041–0043。

| 文件 | 责任 |
| --- | --- |
| Create `shared/schemas/quote_document.py` | 唯一CustomerQuoteView纯DTO |
| Create `domains/quotations/version_schemas.py`、`basis_schemas.py`、`content.py` | 新内容/证据快照、hash/格式化/第二道门纯规则 |
| Create `domains/quotations/creation.py`、`service_impl.py`、`version_repository.py` | 专用creation session及统一规则、服务门面、窄存储Protocol |
| Modify `domains/quotations/{schemas,service,permissions,errors,models}.py` | 新公开重导出/端口/固定错误，状态表补边；不改旧构造 |
| Create `infra/db/quotation_uow.py`、`infra/db/repositories/quotations.py`；Modify `infra/db/tables.py` | 同session事务/锁、报价/抬头持久化；基础设施只映射与持久约束 |
| Create `migrations/versions/0044_quotations.py` | 本节新表、tenant复合FK、不可变和状态/活跃唯一约束 |
| Modify `workflows/quote_approval/application.py`；Create `basis_adapter.py`、`completion_reader.py`、`issuer_reader.py` | 保留T3B准备应用，追加真实create/recovery、抬头reader与显式DTO转换 |
| Create `tests/unit/test_quotation_contracts.py`、`test_quotation_service.py`、`test_quote_creation_application.py` | 纯契约、服务/受控端口、编排顺序 |
| Modify `tests/unit/test_quotation_models.py`；Create `tests/integration/test_quotations.py`、`test_quotation_creation_recovery.py`、`test_quotation_migration.py`；Modify `tests/integration/test_migrations.py` | 旧行为、真实DB/多连接/重启与0044 roundtrip |
| Modify `domains/quotations/AGENTS.md`、`docs/adr/0018-costing-quotation-contracts.md` | 记录新旧入口、session/E2/manual/DTO依赖契约；不放宽九边界 |

不为旧Repository/Quote增加泛型update，不建设通用事务、锁管理器或文件授权框架。以下五个切片分别TDD/提交、自审，Task4完整交付后由控制器独立审查；仍属于同一Task4，不自行安排五次重复审查。

## 1. 一次定义完整公开形状

新Pydantic DTO strict/frozen/extra-forbid；序列tuple。复用shared强ID、Money、FxRate、Provenance、NeedQuoteFacts、QuoteEmployeeFact、QuoteCreationIntent/Completion/OperationView、QuoteTerm/QuoteRoundingInput；`Hash`为64位小写hex约束别名。所有可空字段仍显式给出，无隐式业务默认。
时间必须aware，持久/新hash编码归UTC；key为1..128且无首尾空白/控制字符；有限金额JSON只收/输出十进制字符串；bool不能作整数。遵守T3B文本/精度限制。旧Need hash编码不得更改。
frozen不是嵌套dict深冻结：复用上游dict字段须防御性深拷贝；存储/读取逐次验证内容hash，不持有调用方可变引用，不提供保存修改后detail的入口。持久内容由DB拒绝更新/删除。

### 1.1 新报价、抬头与客户视图（quotations.schemas重导出）

```text
QuotationActor: employee_id:EmployeeId; role:QuoteEmployeeFact.role同一Literal
QuoteDraftCommand:
 opportunity_id:OpportunityId;cost_sheet_id:CostSheetId;expected_context_hash:Hash;expected_sheet_hash:Hash
 scope_confirmation_id:str;unit_price:Money;rounding:QuoteRoundingInput;quote_fx_ref:str|None
 valid_until:datetime;terms:tuple[QuoteTerm,...];replaces_quote_id:QuoteId|None;expected_quote_version:int|None
QuoteIssuerCreate: name:str;address:str;contact:str
QuoteIssuer: 原样使用T3B完整类型，不另定义；包含issuer_id/hash/source_ref、三字段值与Provenance、确认人/时间
QuoteContentLine:
 line_number:int;description:str;specification:str;unit:str;quantity:int
 unit_price:Money;line_total:Money;rounding:QuoteRoundingInput
QuoteContentSnapshot:
 tenant_id:TenantId;quote_id:QuoteId;opportunity_id:OpportunityId;version:int
 operation_id:str;request_hash:Hash;intent:QuoteCreationIntent;basis:QuoteBasis
 prepared_by:EmployeeId;owner_id:EmployeeId;issuer:QuoteIssuer;account_name:str;country:str
 lines:tuple[QuoteContentLine,...];terms:tuple[QuoteTerm,...];valid_until:datetime
 replaces_quote_id:QuoteId|None;replaced_quote_version:int|None;created_at:datetime;content_hash:Hash
QuoteDetailView: content:QuoteContentSnapshot;state:QuoteState
QuoteSendReceipt: tenant_id:TenantId;attempt_id:MessageAttemptId;quote_id:QuoteId;content_hash:Hash;sent_at:datetime
```

QuoteDraftCommand不接prepared_by、actor/role、scope_hash、operation、issuer、locked/confirmed/approved、自由付款note。revision ID/version成对且version>0；unsupported term kind拒绝，terms保留顺序/重复，不能由文本暗渡另一类承诺。
首次创建prepared_by=当前actor；旧operation恢复保留原prepared_by。已有quote的恢复允许当前四角色读取/补完成；未落quote的继续创建仍要求当前actor为原prepared_by，不能借恢复代为新起草。
每quote恰一行且line_number=1。description来自已确认product_category；specification由T3B完整规格字段按固定英文标签顺序排版，省略None，不缩成size_spec。unit/quantity来自当前事实；不接任意描述覆盖事实。
行价必须等于basis.calculation.displayed_unit_price；行额=展示单价×数量再按total_places/strategy舍入，并等于basis.calculation.displayed_total。不得使用旧QuoteLine严格未舍入构造，也不在本域重算利润。
`quote_content_hash(content:QuoteContentSnapshot)->str`公开于service，content.py实现：固定版本`quote-content-v1`，编码上述内容除content_hash/created_at外全部字段；嵌套完整intent/basis/issuer和Provenance显式编码，不把state/本次reader/runtime/审批决定混入。
同文件纯工厂`build_quote_content(quote_id:QuoteId,version:int,intent:QuoteCreationIntent,basis:QuoteBasis,context:QuoteBusinessContext,*,created_at:datetime,replaced_quote_version:int|None)->QuoteContentSnapshot`先组相同固定载荷再算hash，不临时塞伪hash；operation_id取basis、replaces取intent。`format_quote_specification(spec:QuoteSpecificationFacts)->str`按product_category/application/material/size_spec/packaging/certification_required固定次序与英文标签排版，不改事实值。
新hash不得修改T3B意图或Need编码；读取旧quote只取其存储快照，不用最新issuer/Need重建。需要旧展示时另提供`to_legacy_quote_view(detail:QuoteDetailView)->QuoteView`纯投影；无身份展示名则保留None，不从名字推ID。

shared唯一`CustomerQuoteView`字段如下；quotations.schemas原名重导出同class，connector不导入domains：

```text
quote_id:str;version:int;issuer_name:str;issuer_address:str;issuer_contact:str;account_name:str
description:str;specification:str;unit:str;quantity_display:str;unit_price_display:str;total_display:str
currency:str;valid_until_display:str;approved_terms:tuple[str,...]
```

`project_customer(quote:QuoteDetailView)->CustomerQuoteView`逐字段白名单构造，不做授权；本任务只在可信、已确认适用审批的调用链之后供正式文件使用。数额固定使用line.rounding位数，数量整数十进制，valid_until为UTC ISO8601，terms按存储顺序text投影。纯函数不会证明terms获批；T5/T6必须先证明完整内容/每种承诺均有适用批准，未接通不得正式发布。不得将内部DTO当草稿HTTP响应。
`validate_customer_projection(view:CustomerQuoteView,quote:QuoteDetailView)->None`在content.py实现并从service导出：与同一`project_customer(quote)`全部字段严格等值，异值固定`basis_mismatch`，不重算金额或自造金额正则。这是可信报价到客户DTO的内容绑定，不是批准或权限证明；T8正式授权后使用，renderer只消费该投影。覆盖金额被改为任意文本、数量/币种/条款/其他白名单字段被改和合法精确投影，不仅断言schema字段名。

### 1.2 本域冻结投影：不是导入costing，也不是只存引用

以下为`basis_schemas.py`完整字段词典；带`同...`表示类型严格同上，不新增任意dict载荷。shared Money金额带币种；其余金额/比率用WireDecimal。所有名称经quotations.schemas导出。

```text
QuoteEvidenceSource:
 tenant_id:TenantId;source_ref:str;artifact_id:str;content_hash:Hash;locator:str;observed_at:datetime
 source_type:Literal['upload','conversation','web_page','employee_input','external_api'];source_url:str|None
QuoteEvidenceConfirmation（共用字段，不是证据kind）:
 source:QuoteEvidenceSource;field_provenance:dict[str,Provenance];confirmed_by:EmployeeId;confirmed_at:datetime
QuoteSupplierEvidence（包含Confirmation全部字段）:
 kind:Literal['supplier_price'];tenant_id:TenantId;opportunity_id:OpportunityId;evidence_id:str;evidence_hash:Hash
 source_ref:str;locator:str;amount:Money;need_id:ValidatedNeedId;supplier_ref:str;specification:str;unit:str;destination:str
 basis:Literal['quoted','indicative'];quantity_min:int;quantity_max:int;moq:int;quoted_at:datetime;valid_until:datetime
QuoteExpenseEvidence（包含Confirmation全部字段）:
 kind:Literal['confirmed_expense'];tenant_id:TenantId;opportunity_id:OpportunityId;evidence_id:str;evidence_hash:Hash
 source_ref:str;locator:str;amount:Money;item_type:str;allocation_scope:str;is_per_unit:bool;quantity:int
 basis:Literal['quoted','actual'];observed_at:datetime;valid_until:datetime|None
QuotePriceEvidence = Annotated[QuoteSupplierEvidence|QuoteExpenseEvidence,Field(discriminator='kind')]
QuoteFxSnapshot（包含Confirmation全部字段）:
 fx_id:str;content_hash:Hash;base_currency:str;quote_currency:str;source_ref:str;rate:WireDecimal;observed_at:datetime
QuotePolicySnapshot:
 policy_id:str;content_hash:Hash;category:str|None;minimum_margin_rate:WireDecimal;target_margin_rate:WireDecimal
 cost_groups:dict[str,Literal['goods','variable','fixed']];effective_from:datetime;source_ref:str
 confirmed_by:EmployeeId;confirmed_at:datetime;source:QuoteEvidenceSource|None;field_provenance:dict[str,Provenance]
QuoteCostItemBinding: item_sequence:int;evidence_id:str;source_line_ref:str;allocation_scope:str
QuoteCoverageDecision: item_type:str;applicable:bool;reason:str;item_bindings:tuple[QuoteCostItemBinding,...]
QuoteCoverageSnapshot:
 expected_sheet_hash:Hash;decisions:tuple[QuoteCoverageDecision,...];acquisition_mode:Literal['summary','detail']
 coverage_id:str;cost_sheet_id:CostSheetId;content_hash:Hash;confirmed_by:EmployeeId;confirmed_at:datetime
 field_provenance:dict[str,Provenance]
QuoteScopeEvidenceBinding: evidence_id:str;evidence_hash:Hash;applicability_note:str
QuoteScopeConfirmation:
 tenant_id:TenantId;confirmation_id:str;opportunity_id:OpportunityId;need_id:ValidatedNeedId;cost_sheet_id:CostSheetId
 sheet_hash:Hash;coverage_id:str;coverage_hash:Hash;need_facts:NeedQuoteFacts;need_facts_hash:Hash
 specification:str;specification_hash:Hash;terms:tuple[QuoteTerm,...];terms_hash:Hash;valid_until:datetime
 evidence_bindings:tuple[QuoteScopeEvidenceBinding,...];content_hash:Hash;provenance:Provenance
QuoteProfitMetrics:
 unit_full_cost:WireDecimal;minimum_price:WireDecimal;target_price:WireDecimal;gross_profit:WireDecimal
 contribution_profit:WireDecimal;full_cost_profit:WireDecimal;margin_rate:WireDecimal
 discount_headroom:WireDecimal;additional_acquisition_headroom:WireDecimal
QuoteCalculationSnapshot:
 cost_sheet_id:CostSheetId;policy_id:str;inputs_hash:Hash;context_hash:Hash;version_number:int;computed_at:datetime
 base_currency:str;quote_currency:str;metrics:QuoteProfitMetrics;effective_unit_revenue:Money
 displayed_unit_price:Money;displayed_total:Money
QuotePricingOptions:
 mode:Literal['target','manual'];unit_price:Money|None;rounding:QuoteRoundingInput
 quote_fx:FxRate|None;algorithm_version:Literal['costing-v1']
QuoteBasis:
 tenant_id:TenantId;basis_id:str;operation_id:str;request_hash:Hash;opportunity_id:OpportunityId;cost_sheet_id:CostSheetId
 context_hash:Hash;sheet_hash:Hash;basis_hash:Hash;policy_id:str;quantity:int;specification:str;unit:str;destination:str
 need_facts:NeedQuoteFacts;scope_confirmation:QuoteScopeConfirmation;policy:QuotePolicySnapshot
 coverage:QuoteCoverageSnapshot;calculation:QuoteCalculationSnapshot;price_evidence:tuple[QuotePriceEvidence,...]
 pricing_options:QuotePricingOptions;cost_fx_rates:tuple[FxRate,...];quote_fx:QuoteFxSnapshot|None;valid_until:datetime;frozen_at:datetime
```

这是FrozenCostBasis全字段等值本域投影：原amount+currency合为Money，tenant从已验证source/冻结租户取得并核对，其他值不改写。currency不再重复存字段；field_provenance的原currency键仍完整保存。价格hash保留原hash，不对本地改形DTO冒用上游hash算法。
cost_fx_rates使用shared原FxRate，完整保留表内核算FX顺序、来源和时间，与独立quote_fx严格分开；映射/内容hash/持久读取均覆盖，审批安全摘要不能从source自由文本获得原件权限。
`basis_adapter.py`显式构造每个分支及嵌套字段，不`model_dump→Any`透传、不把费用伪装supplier、不新建假Provenance。覆盖T2价格/FX/policy与T3Bscope/calculation全部值；新增上游字段须使映射契约测试失败再核定。
第二道门由`validate_quote_basis(intent:QuoteCreationIntent,basis:QuoteBasis,context:QuoteBusinessContext,*,now:datetime)->None`纯函数执行：tenant/机会/Need/sheet/操作/hash/完整scope/terms/valid_until精确相合，quantity/unit/destination硬匹配，采购全部quoted且MOQ/数量档/时间通过；费用保留真实basis与allocation，有限期必须覆盖quote；证据ID/hash精确覆盖coverage/scope，source与确认事实完整一致。
规格语义来自该完整scope的人工作用范围确认；canonical规格/hash只与Need/scope对账，**不得**同供应商自由文本比较相等。material/packaging/required_by等变化必须scope_stale/context_changed，不能自动补scope。Money币种、manual售价/舍入/quote_fx_ref、policy/calculation等绑定须一致；低利润不是创建失败，交T5独立例外审批。
上游basis/hash真实性通过真实冻结返回值及持久operation/basis绑定校验；本域不重造T1算法或跨域查询来声称重新计算了basis_hash。完整冻结快照仍持久保存以供T5再验、历史重现。

## 2. 公共端口、当前身份与creation session

保留旧`QuotationService` Protocol及旧DTO，新增`QuotationVersionService`作为新生产候选；实现类`QuotationServiceImpl`只实现新Protocol。全部跨模块入口在`service.py`显式导出；新schema在`schemas.py`导出。

```python
class QuotationActorReader(Protocol):
    async def read_current(self, tenant_id: TenantId, employee_id: EmployeeId) -> QuoteEmployeeFact | None: ...
class QuoteSendReceiptReader(Protocol):
    async def read(self, tenant_id: TenantId, attempt_id: MessageAttemptId, *, actor_id: EmployeeId) -> QuoteSendReceipt | None: ...
class QuoteCreationSession(Protocol):
    async def preflight(self, intent: QuoteCreationIntent, *, operation_id: str | None) -> QuoteDetailView | None: ...
    async def create_from_basis(self, intent: QuoteCreationIntent, basis: QuoteBasis,
                                *, operation_id: str) -> QuoteDetailView: ...
class QuotationVersionService(Protocol):
    def open_creation(self, tenant_id: TenantId, opportunity_id: OpportunityId,
        context: QuoteBusinessContext, *, actor: QuotationActor) -> AsyncContextManager[QuoteCreationSession]: ...
    async def create_from_basis(self, tenant_id: TenantId, intent: QuoteCreationIntent,
        basis: QuoteBasis, context: QuoteBusinessContext, *, operation_id: str,
        actor: QuotationActor) -> QuoteDetailView: ...
    async def get(self, tenant_id: TenantId, quote_id: QuoteId, *, actor: QuotationActor) -> QuoteDetailView: ...
    async def list_versions(self, tenant_id: TenantId, opportunity_id: OpportunityId,
                            *, actor: QuotationActor) -> tuple[QuoteDetailView, ...]: ...
    async def get_by_operation(self, tenant_id: TenantId, operation_id: str,
                               *, actor: QuotationActor) -> QuoteDetailView | None: ...
    async def creation_completion(self, tenant_id: TenantId, operation_id: str,
                                  *, actor: QuotationActor) -> QuoteCreationCompletion | None: ...
    async def confirm_issuer(self, tenant_id: TenantId, command: QuoteIssuerCreate,
                             *, actor: QuotationActor, idempotency_key: str) -> QuoteIssuer: ...
    async def get_confirmed_issuer(self, tenant_id: TenantId) -> QuoteIssuer: ...
    async def expire_overdue(self, tenant_id: TenantId, *, limit: int) -> int: ...
    async def record_verified_send(self, tenant_id: TenantId, quote_id: QuoteId,
                                   receipt: QuoteSendReceipt, *, actor: QuotationActor) -> QuoteDetailView: ...
```

`QuotationServiceImpl(uow_factory:QuotationUowFactory, actor_reader:QuotationActorReader, preparation_policy:QuotePreparationPolicy, send_reader:QuoteSendReceiptReader, *, now:Callable[[],datetime])`所有依赖必填。actor只由可信上层构造；每入口reader重取tenant/active/role并与actor相合。读取/创建复用T3B纯policy；issuer另有boss动作，业务判断不写infra。
`get_confirmed_issuer`是T3B可信内部reader专用，非无鉴权HTTP；`expire_overdue`是租户后台作业专用，无人类actor不是默认老板。T4不注册这两者为公开HTTP。
get/list/get_by_operation/completion不要求当前Need或basis尚未过期，只核当前内部读取权和存储完整性；list按version降序。当前actor缺失/失活/role不一致统一permission_denied，不能先返回存在性。
正式客户文件不能借内部get给sales/manager开成本权限，也不能先要求四成本角色再执行CRM gate而误封他们。T4只交付纯project_customer；T5/T6/T8增加独立purpose-specific业务读取入口，先按现有机会ABAC授权，返回客户白名单，另验批准/有效期。审批详情读取由approvals授权，不以四角色policy取代。原件展开始终另外验ACL；本批不自造授权token或假装这些路径已接通。

### 2.1 session契约与唯一规则归属

- open_creation调用者必须已进入T3B context lease，并保持至session成功退出；session绑定tenant/机会/context/actor。进入同报价UoW后获取`quotation-create-v1 + tenant + opportunity`事务advisory锁，重读当前actor并与被lease保护的runtime相合，然后读报价状态；context不得作为HTTP信任输入。
- session.__aexit__无异常时commit、有异常/取消rollback，始终close；commit成功前QuoteDetailView只是暂存结果，上层不得报告成功。lock/statement timeout显式配置，固定code，无无界重试。
- preflight先查所给operation的真实quote：同intent/request/机会返回历史对象，不看当前expiry/active；不同绑定idempotency_conflict。若无对象，检新intent/context及当前latest/active/revision/时钟，验证context.issuer是本租户真实不可变确认且ID/hash/三字段一致，保存仅本session内的preflight载荷和预期版本，返回None。没有预检不能调用create。
- create_from_basis再次核同intent/preflight、current actor/context、锁后新now及第二道门，查同operation幂等，执行CAS和一次写入。不重新取得机会锁、不调用另一个服务session、不在持锁时调外部reader。
- 服务级create_from_basis只是`open_creation → preflight → 同session.create_from_basis`的便利包装；它和application-session共用creation.py同一份规则/写函数，不复制校验。只有可信、已持context lease的上层可调用；主创建流程必须用freeze前session形式。
- 不能在另一连接对Opportunity `FOR UPDATE`，也不能更新Opportunity作为版本分配锁；会与外层SHARE自等待。只写quotation本域行。锁顺序：外层员工→机会→Need SHARE；内层报价机会advisory→报价行；freeze临时进入其既有key→sheet→policy锁并退出；quote提交后才释放context。

### 2.2 修订/E2与时间

无replaces时若已有active报active_quote_exists，不自动取代；已有latest expired必须走显式E2，不能以无replaces绕过expected version。accepted/rejected不可作为replaces，不在本切片增加它们的新修订权限。
有replaces时精确匹配tenant/机会/latest quote_id/expected_quote_version，且目标为当前active，或无active时latest expired；否则revision_conflict。当前active且now>=valid_until视为已到期：成功创建事务只将其落expired，不先改成superseded；历史expired保持expired。未过期active在同事务转superseded，新版本=max(version)+1为draft。不能替换更旧expired。
失败事务不能改变旧状态；latest accepted/rejected且无active时，可以使用新成本表、新scope、新key且replaces=None显式创建下一版本，不修改或取代终态。该路径不复用旧锁表，不暗中改机会/Need状态；报价机会锁保护首个新active，其余并发请求冲突。latest expired仍必须显式E2，不能绕过expected version。
模型只补draft/pending/approved→expired与draft/pending→superseded缺边，原构造及其他既有边不动；新服务不使用legacy expired→superseded边。状态变更必须追加事件，报价业务内容不变。
所有当前有效期判断使用**取得报价写锁后**的新时钟；valid_until必须未来且不晚于全部适用依据有效期。历史成功回放不重新准许对客使用。quote_fx_ref独立变化可按T3B确认/新basis规则复用未变锁表；成本项/成本内FX改变必须新表。

## 3. 窄持久端口与0044

`QuotationUowFactory.__call__(tenant_id:TenantId)->QuotationUnitOfWork`；UoW是async context manager，提供`quotes:QuotationVersionRepository`及async commit()/rollback()->None；服务/session拥有提交边界。UoW进入begin，退出未commit一律rollback/close，不另作隐式commit；creation session正常退出明确commit一次。SQL实现构造`SqlAlchemyQuotationUow(factory:async_sessionmaker[AsyncSession],tenant_id:TenantId,*,lock_timeout_ms:int,statement_timeout_ms:int)`，配置均显式正数。除读取已绑定session中的DTO外不把SQLAlchemy session暴露给workflow。
repository所有方法async、首参数tenant_id:TenantId；同一UoW必须同session。精确接口：

```text
lock_opportunity(tenant_id,opportunity_id:OpportunityId)->None
get(tenant_id,quote_id:QuoteId,*,for_update:bool=False)->QuoteDetailView|None
get_by_operation(tenant_id,operation_id:str)->QuoteDetailView|None
list_versions(tenant_id,opportunity_id:OpportunityId)->tuple[QuoteDetailView,...]
add(tenant_id,quote:QuoteDetailView)->None
transition(tenant_id,quote_id:QuoteId,expected:QuoteState,target:QuoteState,event:QuoteStateEvent)->bool
overdue_opportunities(tenant_id,*,now:datetime,limit:int)->tuple[OpportunityId,...]
lock_issuer(tenant_id)->None
issuer_by_key(tenant_id,key:str)->StoredQuoteIssuer|None
current_issuer(tenant_id)->QuoteIssuer|None
get_issuer(tenant_id,issuer_id:str)->QuoteIssuer|None
add_issuer(tenant_id,record:StoredQuoteIssuer)->None
send_receipt(tenant_id,attempt_id:MessageAttemptId)->QuoteSendReceipt|None
add_send_receipt(tenant_id,receipt:QuoteSendReceipt)->None
QuoteStateEvent: event_id:str;quote_id:QuoteId;from_state:QuoteState|None;to_state:QuoteState
 actor_id:EmployeeId|None;reason:Literal['created','revision','expiry','verified_send',
 'approval_submitted','approval_approved','approval_rejected'];at:datetime;reference_id:str|None
StoredQuoteIssuer: issuer:QuoteIssuer;version:int;idempotency_key:str;request_hash:Hash
```

add只接draft并原子写内容/一行/evidence refs/created event；transition只做SQL条件更新及事件同写，状态矩阵/业务原因由域判定。三个approval事件reason仅预留T5，不因此实现T4批准入口。报价行锁统一在机会advisory之后；expiry逐机会短事务，不持一组报价行再反取机会锁。issuer使用tenant级专用事务advisory避免首条/版本并发；无需通用key-lock注册器。

0044建表（全部tenant显式列，ID长度与实际父表一致，新quote/issuer/event遵守new_id，evidence/basis/operation/hash至少容纳64）：

- `quotation_issuers`：tenant/issuer_id PK，version、key、request_hash/content_hash、confirmed_by/at、payload JSONB；UNIQUE tenant+version、tenant+key；employee复合FK；不可改/删。“当前”取最大version，不靠timestamp并列。
- `quotations`：tenant/quote_id PK；opportunity_id/version/state、operation_id/request_hash/basis_id/cost_sheet_id/issuer_id/content_hash、prepared_by/owner_id、replaces_quote_id/replaced_quote_version、valid_until/created_at、content JSONB显式存储；tenant+opportunity+version及tenant+operation唯一，active部分唯一`draft,pending_approval,approved,sent`。
- `quotation_lines`：tenant/quote_id/line_number PK，完整新line payload；复合FK报价；不可改删。
- `quotation_evidence_refs`：tenant/quote_id/evidence_id PK，evidence_hash、kind；复合FK报价及现有`costing_price_evidence(tenant_id,evidence_id)`；每份basis全部evidence均写，不用旧price_snapshot_ref代替。
- `quotation_state_events`：tenant/event_id PK，quote_id/from_state/to_state/actor_id/reason/at/reference_id；FK报价与可空employee；只增。
- `quotation_approval_bindings`：tenant/quote_id/approval_type/approval_id PK，quote_version/content_hash/bound_at；FK报价及`approval_packages(tenant_id,approval_id)`；只增。T4仅建表，T5定义批准事实应用，不能靠本表非空放行。
- `quotation_send_receipts`：tenant/attempt_id PK，quote_id/content_hash/sent_at；FK报价及`outreach_message_attempts(tenant_id,attempt_id)`；只增。存在attempt只证存在，可信reader另验确已发送和精确内容。

quote→operation/basis/issuer/机会/员工/前版均tenant复合FK；0044为真实quote补operation绑定，不能修改0043文件。FK分别存在不足以防混搭：0044加入insert约束trigger，验证operation.request_hash/basis_id/intent.replaces与该quote相合、basis.operation/sheet/tenant相合，以及quote/evidence refs的hash/kind与冻结basis快照和真实价格行相合；SQL不推导业务适用性。引用JSON的具体键以控制器验收的0043实际存储为准，缺必要持久列时在0044增加支持性约束/索引，不另造父表。
报价不可变trigger拒绝业务显式列/content更新及删除，仅允许state变更；状态转换约束与deferred状态事件约束保证每次state更新恰有对应event（created包含None→draft），不能直接UPDATE无审计。line/ref/receipt/issuer均只增；同事件幂等需真实唯一键。Migration测试覆盖SQL绕过与downgrade，不能只测ORM构造。

## 4. 创建应用、真实receipt与幂等顺序

workflow新增`QuoteApplicationService(context_provider:QuoteContextProvider,costing:CostingFreezeService,quotations:QuotationVersionService,actors:QuotationActorReader,policy:QuotePreparationPolicy,*,now:Callable[[],datetime])`。
唯一外部创建方法：`async create(tenant_id:TenantId,command:QuoteDraftCommand,*,actor_id:EmployeeId,idempotency_key:str)->QuoteDetailView`。不接任意operation_id；保留T3B QuotePreparationApplication原行为。
显式适配函数：`costing_context(context:QuoteBusinessContext)->CostingContext`复用T3B已公开的同名函数，不新增同义包装；`to_quote_basis(frozen:FrozenCostBasis)->QuoteBasis`与`pricing_options_from_intent(intent:QuoteCreationIntent)->PricingOptions`在workflow。后者manual、unit_price/rounding来自intent，quote_fx=None，algorithm_version='costing-v1'；freeze通过quote_fx_ref解析真实FX，不能用客户端rate。
同模块`creation_intent(tenant_id:TenantId,command:QuoteDraftCommand,*,prepared_by:EmployeeId,scope_hash:Hash)->QuoteCreationIntent`只逐字段映射；成本actor由当前QuoteEmployeeFact经已验T3B的四角色policy后显式构造CostingActor(actor_id=employee_id,role=role,scope=CostingScope.TENANT)，不是默认scope给所有员工。quotation actor同样取当前ID/role；reader缺事实拒绝。
`PersistentQuoteCreationCompletionReader(quotations:QuotationVersionService,actors:QuotationActorReader)`实现T3B reader.read(tenant_id,operation_id,*,actor_id)->QuoteCreationCompletion|None；调用公开creation_completion。receipt从真实持久content投影，不接受调用方quote_id/成功标志。
`PersistentQuoteIssuerReader(quotations:QuotationVersionService)`在issuer_reader.py实现T3B `async get_confirmed(tenant_id:TenantId)->QuoteIssuer`，只转调get_confirmed_issuer。真实DB测试中context provider必须注入它，issuer先经真实boss确认；不是继续用T3B手写issuer来声称报价→抬头FK已接通。两个adapter均无默认后备，T8实际composition root再装配。

严格顺序（步骤为编排，不重复域业务规则）：

1. 校验command/key及当前身份四角色；查costing.get_creation(tenant,key)。有operation时以其原prepared_by/scope_hash和此次command组成完整候选intent，与原intent/request_hash比较；所有terms顺序、None、valid_until、unit/rounding、FX、replaces/expected及scope ID均绑定，不因当前scope变化改写原意图。
2. **先恢复成功结果**：operation存在则quotations.get_by_operation；若存在，核operation/basis/request/receipt，调用costing.complete_creation并返回原quote。即使Need、issuer、owner、policy、当前时间已变化也不重新freeze；当前读取权仍必需。completed却无真实quote报storage_inconsistent，不返回空成功。complete失败保留可重试，不报告完整创建成功。
3. 无真实quote才读取持久scope（不可自动确认），新operation用当前actor作为prepared_by并构造完整intent；已有pending使用原intent且原prepared_by限制。进入context_provider.open(...,prepared_by=intent.prepared_by)，域policy/context校验；context hash与intent相合。
4. 在context内进入quotations.open_creation；取报价机会锁后**再查同key operation**，处理别的调用刚提交的同key。若出现真实quote，session.preflight返回它，跳过freeze/write；退出session/context后complete。异intent冲突；不能把锁前“没有operation”当最终事实。
5. session.preflight(intent,operation_id=已知ID或None)；无历史quote且CAS/资格通过才调用costing.freeze(...,idempotency_key=原key,intent=intent,actor=当前受控成本身份)，随后显式转换、session.create_from_basis。quote UoW提交成功仍在context lease内；退出后调用complete_creation，再返回。
6. 若quote commit前失败：成本已freeze的operation仍pending、不可解锁/换key；相同key按原意图恢复。若quote已commit但complete失败/进程退出：重启走步骤1–2，无新quote/basis/version。commit结果未知报storage_unknown，由同key真实查询恢复，不能宣称未写入。

不同sheet并发同一旧version：后到者在preflight看见新latest后revision_conflict，**其costsheet尚未freeze**。不能为它自动改expected/version或另生成key。不同机会但同tenant/key的碰撞由costing key唯一/hash拒绝。
调用complete_creation时必须已退出报价session/context，reader不能在成本锁内逆向取报价锁；同进程复用同service实例可以，禁止循环调用应用create作为reader。
quote写入时将replaced_quote_version存为第一次成功CAS值，receipt不从后来的active版本猜；completion字段逐一对应shared QuoteCreationCompletion。

## 5. 抬头、过期与受控发送

confirm_issuer：输入只有name/address/contact；老板当前身份校验→issuer事务锁→再次读当前身份→查key/hash。request hash绑定tenant、实际employee_id和三字段，samekey同载荷返同记录，异载荷冲突。
服务生成issuer_id；source_ref=issuer_id；exact三键field_provenance分别为EMPLOYEE_INPUT/source_id=issuer_id/extracted_by=当前老板EmployeeId/extracted_at=confirmed_at=取得issuer锁后的注入now/confirmed_by=同一老板，source_quote为该字段原输入（不标raw verified）。内容hash绑定完整三字段及确认事实；保留原值，非法空白/控制符拒绝，不默默补公司信息。source_url/page_hash=None。旧quote引用原issuer快照永不替换。
“当前抬头”用于context读取时选版；确认新抬头只新增，不撤销旧确认。creation采用该次context所选真实版本快照，不在事务中追逐latest issuer或增加全租户issuer写锁；若重试重新读context选到新issuer，expected_context_hash冲突。控制器已确认该快照语义，不增加撤销/生效窗口；T5/T8当前context门禁仍会阻断不再匹配新抬头的正式文件。
expire_overdue：limit正整数≤1000；候选为全部active且valid_until<=now。按机会锁后重读时钟/状态，变expired+event原子提交；无效候选跳过，返回实际状态变更数；不自动发邮件、续期或创建新版本。
record_verified_send：先当前内部授权，锁外send_reader按attempt读取真实receipt并与全部入参相等；sent_at须在quote.created_at与读取now之间。拿机会锁后重验身份/quote/content/时钟。已存同receipt回放返回当前quote、不重复事件；异receipt绑定冲突；新receipt只允许当前approved且未过期，写receipt+sent+event同事务。PDF下载/随机非空attempt不可通过。
T4 send_reader受控、未生产装配；T8才从实际发送记录构造可信receipt（外部动作仍Gateway）。T5负责真正approved与QuoteApproved outbox；T4不新增通过布尔值批准或手写sent HTTP，不实现accepted/rejected人工决策入口。

固定错误保留旧公开异常名；新增`QuotationError(ValidationError)`、`QuotationPermissionError(PermissionDenied)`、`QuotationUnavailableError(TradeOSError)`，构造仅code:对应Literal，中文固定消息无SQL/原文/DSN。
Validation code：`invalid_input,unsupported_term,quote_not_found,issuer_not_found,idempotency_conflict,revision_conflict,active_quote_exists,context_changed,basis_mismatch,scope_stale,evidence_invalid,evidence_expired,quote_expired,approval_missing,receipt_invalid,invalid_state`。
Permission仅`permission_denied`。Unavailable：`dependency_unavailable,lock_timeout,storage_unknown,storage_inconsistent`。T3B原有code（含operation_pending/unit_stale等）原样跨应用保留，不任意翻成500。T8映射不存在404、冲突409、输入422、权限403、未知/超时503；本切片不改router。

## 6. 五个TDD/提交审查切片

先在各目标测试文件写RED并记录失败确因缺失契约/规则；再GREEN最少实现。`tests`的受控fixture用真实tenant/employee/opportunity/Need/artifact/evidence/coverage/scope/FK，不能手写fake verified来源或猜父表ID；来源不调用真实系统。数据库测试按现有conftest隔离机制，`env -u TEST_DATABASE_URL`，不读取环境秘密。

### 6.1 契约与旧行为兼容

- [ ] RED新增新DTO严格JSON、完整basis显式映射、唯一CustomerQuoteView、旧构造原样、完整规格/费用分支及舍入例子。

```python
from decimal import Decimal, ROUND_HALF_UP
from domains.quotations.schemas import QuoteContentLine, QuoteRoundingInput, CustomerQuoteView
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.quote_document import CustomerQuoteView as SharedCustomerQuoteView

def test_new_line_rounds_without_weakening_legacy_line():
    line = QuoteContentLine(line_number=1, description='Part', specification='Material: Steel',
        unit='piece', quantity=3, unit_price=Money(Decimal('1.235'), CurrencyCode('USD')),
        line_total=Money(Decimal('3.71'), CurrencyCode('USD')),
        rounding=QuoteRoundingInput(unit_places=3,total_places=2,strategy=ROUND_HALF_UP))
    assert line.line_total.amount == Decimal('3.71')
    assert CustomerQuoteView is SharedCustomerQuoteView
```

- [ ] RED命令：`python3 -m pytest tests/unit/test_quotation_contracts.py tests/unit/test_quotation_models.py -q`。
- [ ] GREEN新增schemas/content及公开重导出，只补允许状态边；表驱动second gate测试quantity/unit/destination、完整scope/terms、quoted/expense实际凭证、日期/币种/舍入；旧单价乘数量严格规则仍通过旧测试。
- [ ] 提交`feat: 定义兼容旧模型的不可变报价内容契约`，自审typed快照/原文泄露边界。

### 6.2 持久化、抬头与不可变约束

- [ ] RED真实DB测试issuer当前boss/samekey异输入、字段Provenance、tenant+key隔离；报价/line/证据/issuer SQL UPDATE/DELETE拒绝；伪/跨tenant FK、operation/basis混搭、价格hash/kind错配拒绝；state无event拒绝；0044升级/降级/再升级。
- [ ] RED命令：`env -u TEST_DATABASE_URL python3 -m pytest tests/integration/test_quotation_migration.py tests/integration/test_quotations.py -q`。
- [ ] GREEN实现0044/rows/repository/UoW及issuer，应用按最少公开端口测试；复用现有migration隔离fixture，禁止为测试绕过真实FK。金额JSON字符串精确roundtrip。
- [ ] 提交`feat: 持久化报价快照与老板抬头确认`，自审DDL/tenant/不可变约束。

### 6.3 创建session、CAS与第二道门

- [ ] RED单元验证服务便利入口与session走同规则；preflight缺失/载荷变化拒绝；独立入口锁等待后actor失活、期限到达拒绝；创建lease下员工调整实际阻塞，不伪造“锁内员工已被更新”场景；低利润允许draft、未quoted不允许。
- [ ] RED多连接：真实T3Bcontext SHARE下创建不自等待；同机会不同sheet竞争同revision只一方freeze/成功；第一版竞争；E2 latest expired、按时钟到期active、较旧expired/错误version/accepted/rejected拒绝；旧active状态与新quote原子、rollback不改旧。
- [ ] 另验latest accepted/rejected无active时新成本表/新scope/无replaces能开下一版本，旧终态原样；复用旧锁表或有active时拒绝，两个新建竞争只产生一个active。这里拒绝的是以终态为replaces的改写，不是禁止新的报价事实。

```python
async def test_revision_loser_has_not_frozen_its_sheet(real_creation_race):
    # fixture用两个真实连接、两个有效sheet/scope、同一expected版本，barrier停在机会锁前。
    winner, loser = await real_creation_race.run()
    assert winner.quote.content.version == real_creation_race.previous_version + 1
    assert loser.error.code == 'revision_conflict'
    assert await real_creation_race.operation_for(loser.key) is None
    assert not await real_creation_race.sheet_locked(loser.sheet_id)
```

- [ ] RED命令：`env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quotation_service.py tests/integration/test_quotations.py tests/integration/test_quote_context_locks.py -q`。
- [ ] GREEN实现creation.py单一校验/CAS，真实报价机会锁跨freeze保持；任何代码路径不得对Opportunity升级写锁；新now在锁后。提交`feat: 在上下文租约内串行创建报价版本`，自审锁顺序/事务失败窗口。

### 6.4 真实operation恢复与跨域编排

- [ ] RED创建→quote commit后注入complete故障→重建service/application→更换当前Need或issuer/推进至过期→samekey返回exact旧quote并完成原operation；没有当前context/freeze调用。samekey变terms顺序/valid_until/FX/None/replaces/expected/scope冲突。

```python
async def test_recover_committed_quote_before_rechecking_context(real_recovery):
    original = await real_recovery.commit_quote_then_fail_completion()
    await real_recovery.change_need_and_issuer_then_expire()
    app = real_recovery.restarted_app(context_must_not_open=True)
    recovered = await app.create(real_recovery.tenant, real_recovery.command,
        actor_id=real_recovery.actor_id,idempotency_key=real_recovery.key)
    assert recovered.content == original.content
    assert (await real_recovery.operation()).completion.quote_id == original.content.quote_id
    assert await real_recovery.quote_count_for_operation() == 1
```

- [ ] RED另测freeze后quote前失败同key继续、pending换key被拒、completed缺quote报不一致、两调用锁前均未看到operation的重查竞态、commit未知、取消/timeout归还连接；恢复调用complete时已无context/quote锁。
- [ ] RED命令：`env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quote_creation_application.py tests/integration/test_quotation_creation_recovery.py tests/integration/test_quote_cost_lock.py -q`。
- [ ] GREEN显式basis_adapter与PersistentQuoteCreationCompletionReader接真实quotation；不得用mock receipt冒称端到端恢复。T3B原completion受控fixture在本测试替换为真实reader。提交`feat: 从真实报价持久记录恢复冻结创建操作`。

### 6.5 投影、过期、受控回执与回归交接

- [ ] RED客户DTO keys精确白名单，递归序列化不含cost/profit/supplier/source_quote/原件引用；金额显示位数和完整规格正确；内部失活/越tenant拒绝，不把sales/manager加进内部policy；issuer换版不影响旧报价。
- [ ] RED expiry覆盖四active状态/重复运行、与revision竞争；发送reader拒绝伪造/异tenant/内容错配/非实际发送、approved→sent一次，download无receipt不改变state；无T5真实批准时不宣称正式文件链已通过。
- [ ] RED命令：`env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quotation_contracts.py tests/unit/test_quotation_service.py tests/integration/test_quotations.py -q`。
- [ ] GREEN纯投影/expiry/受控receipt；AGENTS/ADR写明新入口与未装配边界，不改旧NotImplemented入口实现。提交`feat: 完成报价客户投影与受控状态记录`。
- [ ] 最终目标回归：`env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quotation_contracts.py tests/unit/test_quotation_models.py tests/unit/test_quotation_service.py tests/unit/test_quote_creation_application.py tests/integration/test_quotations.py tests/integration/test_quotation_creation_recovery.py tests/integration/test_quotation_migration.py tests/integration/test_quote_context_locks.py tests/integration/test_quote_cost_lock.py tests/integration/test_migrations.py -q`，再`python3 scripts/check_boundaries.py`。报告真实通过/未运行/受控依赖，不把命令计划当已执行。

## 7. 交接与不在本批内

T3B最终接口核对是派发门槛；T4不抢改T3A文件。T5消费QuoteDetailView/完整basis/immutable content_hash/身份ID和审批binding表，负责包内全部terms、margin例外、当前审批ABAC/禁止自批、真实批准事实/QuoteApproved outbox；不能沿用旧QuoteApprovalPackage缺失身份和hash的DTO作为新路径。
T6/T7消费shared唯一CustomerQuoteView，但正式文件仍需T5事实与当前quote/context/来源适用性门禁；纯投影不等于批准。T8装配真实current actor、CRM purpose-specific客户读取、Gateway原件/发送reader与HTTP安全摘要；不得把QuotationActor/QuoteBasis/context直接从客户端解析为可信输入。
T4验证到真实DB报价、operation completion及跨连接CAS，不代表已运行T5审批/T8 Gateway或生产文件下载。未装配send reader应dependency_unavailable，不能默认verified。没有真实quote时不会产生“成功receipt”。
规模控制：按上述五次TDD提交、自审后完整交付，独立审查由控制器安排一次；如单个实现文件需要混合DTO、规则、SQL和编排才完成，先按已列责任文件拆分，不新增业务阶段/功能或通用框架。
