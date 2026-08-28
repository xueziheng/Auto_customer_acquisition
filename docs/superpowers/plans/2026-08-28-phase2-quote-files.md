# Task 6：报价PDF存储与真实文件关联 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILLS: Use superpowers:test-driven-development and superpowers:verification-before-completion。此任务由主计划的SDD控制器派发，不另启动executing-plans批次或子代理。控制器已全文自检；仅核对T4/T5真实交付后实施。按下列TDD切片提交，完整Task6统一独立审查，不自行开始T7/T8。

**Goal:** 隔离QUOTE_PDF派生类型，按真实metadata/批准run/三个hash只增记录文件，并在未知提交后保留可恢复bytes。
**Architecture:** artifact_store只管内容完整性及幂等；quotation管文件关联/真实批准归属；infra仅把Store安全metadata映射为本地DTO。当前客户文件正式授权与所有bytes的Gateway编排归T8。
**Tech Stack:** 既有Python/Pydantic/SQLAlchemy/PostgreSQL/ObjectBlobTransport，无新增运行依赖。
**Spec:** `docs/superpowers/specs/2026-08-28-phase2-costing-quotation-design.md` §6/§7；主计划Task6；`2026-08-28-phase2-quotation-versions.md`与`2026-08-28-phase2-quote-approvals.md`。本文落实控制器已核定的文件契约，不修改批准规格。

## 0. 前置、约束与文件

先读根AGENTS/HANDBOOK及shared/artifact_store/domains/quotations/infra/tests就近AGENTS。工作树为`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-costing-quotation`。
派发前核对T4真实QuoteDetailView/project_customer/QuotationUow/机会锁，T5真实receipt/QuoteWorkflowRunReader/engine.get_run；本文不证明它们已经实现。0046依赖真实0045；不得回改0041–0045。
只增文件，不改quote内容/state，不标sent；没有生成/发送/PDF解析实现，不新增原件读取许可。金额仍Decimal，客户展示字符串不重新计算；所有SQL强制tenant过滤。
scope按当前机会ABAC：sales本人、manager当前直属owner、boss租户；不授予product/sourcing/finance客户文件权。T6只注入受控scope guard验证协议，T8才装配真实范围/文件context/当前批准门。
`workflows/AGENTS.md`不允许artifact_store导入：metadata adapter放infra，不以机检通过放宽目录规则；所有bytes在T8经Gateway，metadata不含object key/URL。

| 文件 | 责任 |
| --- | --- |
| Create `shared/schemas/quote_files.py`；Modify `shared/schemas/quote_document.py`、`identifiers.py` | 唯一模板常量、客户hash编码、QuoteFileId；不重复CustomerQuoteView |
| Modify `artifact_store/{store,service_impl,errors}.py` | 新kind/严格分支、仅PDF未知提交保护及固定错误 |
| Create `domains/quotations/file_schemas.py`、`file_service.py`；Modify `schemas.py`、`service.py`、`service_impl.py`、`version_repository.py`、`errors.py` | typed文件/reader/guard、真实关联、服务委托/窄仓储/固定错误 |
| Modify `domains/quotations/approval_rules.py`、`approval_service.py` | 复用/抽取T5同一snapshot、原请求集合、binding、receipt及run绑定纯校验，不重做审批 |
| Create `infra/quote_file_artifacts.py`；Modify `infra/db/{tables.py,repositories/quotations.py}`；Verify existing `infra/db/repositories/artifacts.py` | metadata适配、0046映射/只增关联；既有enum映射及带tenant的get_by_idempotency_key复用，仍需真实回归；无业务规则/SDK/bytes |
| Create `migrations/versions/0046_quote_pdf_artifacts.py`、`docs/adr/0019-quote-pdf-artifacts.md` | 双kind约束、文件表与不可变/绑定约束、失败恢复语义 |
| Create `tests/unit/test_quote_pdf_artifacts.py`、`test_quote_file_service.py`；Create `tests/integration/test_quote_pdf_artifacts.py`、`test_quote_file_migration.py` | typed/安全/服务、真实DB/未知commit/锁/迁移 |
| Modify `tests/integration/test_migrations.py`、`artifact_store/AGENTS.md`、`domains/quotations/AGENTS.md` | 迁移head及已实现/未装配边界；旧Artifact测试目标回归 |

## 1. 三hash与完整DTO

新增DTO采用strict/frozen/extra-forbid；Hash=64位小写hex，正整数拒bool，时间aware并规范UTC，ID使用shared强类型。新增QuoteFileId=NewType('QuoteFileId',str)，服务new_id('qfl')，格式`qfl_<ULID>`；复用原ULID字符/首位限制。
QuoteFileView是安全输出，不作为写入命令。只从service/schemas导出，不导出repository/model到上层。

```text
QuoteFileView:
 file_id:QuoteFileId;quote_id:QuoteId;quote_version:int;artifact_id:ArtifactId
 content_hash:Hash;quote_content_hash:Hash;customer_content_hash:Hash
 template_version:str;size_bytes:int;generated_at:datetime
QuoteFileApprovalFact:
 tenant_id:TenantId;quote_id:QuoteId;quote_version:int;quote_content_hash:Hash
 approval_run_id:RunId;approval_facts_hash:Hash;applied_at:datetime
QuoteGeneratedArtifactFact:
 tenant_id:TenantId;artifact_id:ArtifactId;kind:str;artifact_hash:Hash;size_bytes:int
 mime_type:str;workflow_run_id:RunId;subject_ref:str;sequence_number:int
 idempotency_key:IdempotencyKey;generated_by:str;generated_at:datetime
QuoteFileRecord: tenant_id:TenantId;view:QuoteFileView;approval_run_id:RunId
```

QuoteFileRecord为本域内部持久DTO；不注册HTTP。Fact.kind/mime保持真实字符串，不在adapter把email_draft强转成quote_pdf；业务服务再严格判kind。Fact不含bytes/路径/原件引用。
三hash唯一定义：quote_content_hash=T4完整不可变content hash；customer_content_hash=纯客户DTO规范hash；QuoteFileView.content_hash=PDF bytes hash，持久列名明确artifact_hash。不可用其中任一个代替另两个。
shared/quote_document.py新增纯`customer_quote_hash(view:CustomerQuoteView)->str`，quotations.service重导出。编码`{'version':'customer-quote-v1','customer':<逐字段dict>}`，JSON sort_keys=True、ensure_ascii=False、separators=(',',':')、allow_nan=False，UTF-8 SHA-256。
逐字段恰为T4唯一DTO：quote_id/version/issuer_name/issuer_address/issuer_contact/account_name/description/specification/unit/quantity_display/unit_price_display/total_display/currency/valid_until_display/approved_terms；terms tuple只转array且保留顺序。金额/时间展示字符串不trim/重排/转数值；不含template、state、actor、内部basis。
客户hash每次由真实quote.content→project_customer计算；历史文件用原quote内容，不拿当前Need/issuer重建。完整quote hash沿T4规则重验。文件服务不把CustomerQuoteView或内部quote直接返回metadata HTTP。

## 2. Store公共契约、模板与恢复

`GeneratedArtifactKind.QUOTE_PDF='quote_pdf'`；MIME集合仅application/pdf。RawArtifactKind.PDF原样保留，generated不作为证据。
shared/quote_files.py只定义`QUOTE_PDF_TEMPLATE_VERSIONS: frozenset[str]=frozenset({'quote_pdf_v1'})`；它是呈现版本标识契约，不是商业审批规则。Store/域/T7共用，API不能提供任意模板。
GeneratedArtifactMeta字段和GeneratedArtifactStore.put/get/get_meta签名保持；按kind选择互斥subject/key/MIME/template验证，不扩大旧正则：

```text
EMAIL_DRAFT: subject=enr_<ULID>, key={subject}:{sequence}:draft,
 MIME=application/vnd.tradeos.email-draft+json, generated_by沿旧正则
QUOTE_PDF: subject=quo_<ULID>, key={subject}:{sequence}:quote_pdf:{generated_by},
 MIME=application/pdf, generated_by必须属于唯一模板常量
```

sequence必须正int；QUOTE_PDF语义sequence=quote.version由quotation再核。workflow_run_id仍必填且格式严格；Store只保存归属，不判审批或声称它是真run。object key保持`generated/{tenant}/{artifact_id}`。
_generated_matches沿原全部绑定比较kind/hash/size/MIME/run/subject/sequence/key/generated_by；generated_at与随机candidate artifact_id不加入winner等价比较。相同key/bytes/全绑定返回原metadata（含原ID/时间），不同任一绑定抛原ArtifactConflictError，不覆盖。
新`ArtifactCommitUnknownError(TransientError)`固定code='artifact_commit_unknown'；新`ArtifactUnavailableError(TransientError)`固定code='artifact_unavailable'，无自由错误参数/SQL/路径。只用于新增QUOTE_PDF异常映射；旧Raw/EMAIL_DRAFT错误和补偿语义不顺带重写。
QUOTE_PDF put在任何object put尝试之后，若transport/UoW退出/commit/close发生异常而无法证明未提交，**保留candidate bytes**；非取消抛ArtifactCommitUnknownError from None，取消原样传播。不要因rollback调用成功或随后SELECT暂不可见就删除candidate。
尚未尝试object put的基础设施失败映射ArtifactUnavailableError；输入ValidationError/确定的同key冲突保持原类。QUOTE_PDF已确认成功的EXISTING结果可删除仅自己的未引用candidate；如果winner绑定不等仍冲突。cleanup失败沿固定安全错误，不能删除winner或改key。
受信调用方只有在已知可安全再次put的场景才显式用**原key/bytes/run/subject/sequence/template**调用put，不在catch里自动重试/换key。新增窄只读`GeneratedArtifactStore.get_meta_by_key(tenant_id:TenantId,idempotency_key:IdempotencyKey)->GeneratedArtifactMeta|None`，复用现有仓储get_by_idempotency_key，返回不可变安全metadata或不存在；验证tenant/key形状，存储故障固定ArtifactUnavailableError，取消原样，不读/写/删对象、不创建candidate。它供T8 Gateway既有reconcile钩子按原稳定key找回已提交产物，不为HTTP开放任意key查找；不能把暂未找到当未写入而重新render/put。真实bytes完整性仍由Store.get重算hash/length，不以get_meta或get_meta_by_key代替。
确未提交的失败也可保守留下孤立bytes；本任务不建清扫器/删除业务接口。QUOTE_PDF未知commit时DB暂不可读不报告成功、不删除；旧分支保持兼容。

get_meta_by_key补真实PG测试：已提交QUOTE_PDF/既有EMAIL_DRAFT精确原metadata、未知key与跨tenant为None、故障固定错误，read调用的对象get/put/delete均为零；真实commit成功但响应未知后按原key可读回。不得依赖生成服务重新渲染以获得查询所需bytes。

## 3. 本地端口、依赖与调用顺序

以下Protocol在file_service.py定义、quotation.service显式重导出；Fact/View在schemas导出。

```python
class QuoteFileScopeAuthorizer(Protocol):
    def guard(self, tenant_id: TenantId, opportunity_id: OpportunityId,
              *, actor_id: EmployeeId) -> AsyncContextManager[None]: ...
class QuoteGeneratedArtifactReader(Protocol):
    async def read(self, tenant_id: TenantId,
                   artifact_id: ArtifactId) -> QuoteGeneratedArtifactFact | None: ...
class QuoteFileService(Protocol):
    async def record_file(self, tenant_id: TenantId, quote_id: QuoteId,
                          artifact_id: ArtifactId, *, actor_id: EmployeeId) -> QuoteFileView: ...
    async def get_file(self, tenant_id: TenantId, quote_id: QuoteId, file_id: QuoteFileId,
                       *, actor_id: EmployeeId) -> QuoteFileView: ...
    async def list_files(self, tenant_id: TenantId, quote_id: QuoteId,
                         *, actor_id: EmployeeId) -> tuple[QuoteFileView, ...]: ...
    async def get_file_approval(self, tenant_id: TenantId, quote_id: QuoteId,
                                *, actor_id: EmployeeId) -> QuoteFileApprovalFact | None: ...
```

`QuoteFileServiceImpl(uow_factory:QuotationUowFactory,actor_reader:QuotationActorReader,scope_authorizer:QuoteFileScopeAuthorizer,artifact_reader:QuoteGeneratedArtifactReader,workflow_run_reader:QuoteWorkflowRunReader,*,id_generator:Callable[[str],str])`依赖全部必填。None/未装配调用失败，不默认允许；不接客户自报role、approved或verified。
QuotationVersionService追加这四个同签名方法；QuotationServiceImpl新增keyword `files:QuoteFileService|None=None`并仅委托，不把file方法送进prepare/read_internal四角色policy。None只保持既有T4/T5非文件入口构造兼容，四文件方法一律dependency_unavailable，绝非默认许可；T8有真实files才可注册能力。服务与门面共用实现，不重复规则。独立file service依赖全部必填，只依赖本域UoW/端口，不反向依赖门面实例。
actor_id沿既有EmployeeId/QuoteEmployeeFact身份规则：复用shared.schemas.quote_facts.fact_identity（严格str、非空、无首尾空白/控制字符、最多40字符），不得新增emp_ULID要求或迁移旧员工ID；仍必须由真实actor_reader及scope核对tenant/实际ID/在职与当前权限。新QuoteFileId及本批文件DTO规定的quote/artifact/run等ID严格格式不变。
scope guard由受信上层适配既有机会ABAC，保证tenant/active/current owner-manager关系并在调用期间保护Employee→Opportunity；不返回权限token。T6受控实现必须可拒绝并测试拒绝零artifact读取；T8实现真实guard。actor_reader另验真实tenant/active，不在file service复制角色矩阵。
`infra/quote_file_artifacts.py`新增`GeneratedStoreQuoteArtifactReader(store:GeneratedArtifactStore)`实现read：只调用get_meta，逐字段构造本地Fact；ArtifactNotFoundError→None（跨tenant同样），其余基础设施错误映射固定不可用，取消原样。不import私有artifact repository，不调get/put，不读bytes/SDK/凭证，不含quote审批/权限判断。
任何对外返回前先完成scope授权；允许短读本域quote仅作不可变opportunity_id bootstrap，但不得向未授权调用方泄露存在性。缺quote与跨tenant对外统一not_found；已经识别无权限时permission_denied，不回传内部详情。

### 3.1 get_file_approval：从真实receipt找到run

在scope guard内读取真实quote及T5唯一success receipt；无receipt返回None。验证receipt quote/version/content/hash/决定集合与真实bindings；读取receipt.approval_run_id对应真实run，不能要求调用方先有executor。
复用T5 run校验，若其内部尚未具名，抽取`require_quote_run_binding(tenant_id:TenantId,quote_id:QuoteId,quote_version:int,quote_content_hash:str,run:QuoteWorkflowRunFact|None)->None`至approval_rules.py；T5原executor入口调用同函数后仍另验executor ID，外部语义不变。
T5实际receipt链还包含snapshot（真实前一版本及原limit）、完整原组payload/owner/run核验、binding全部不可变请求字段比较，以及receipt决定hash/quote_send真实decider。将这些纯部分抽取至approval_rules并让T5/T6共用，数据库读取仍由各自service/session持有；T6读取真实version-1，不以replaces是否存在推断无前版。不得仅复用facts_hash/FK而遗漏其余校验，也不复制稍弱校验或为复用旧session伪造executor。T5原fresh准备、独立恢复与fresh apply门禁保持各自原语义。
该函数精确核tenant/run事实、type='quote_approval'/workflow_version=1/subject=quote_id/quote_version/content_hash；file service另核返回run.run_id=receipt.approval_run_id，全部receipt decisions.proposed_by_run也必须相等。使用T5既有不可变决定/hash与binding验证，不从APPLIED/事件伪造receipt。
get_file_approval仅返回安全Fact；不含批准人/原文/完整payload，不执行mark_applied。completed/failed run只要绑定正确仍可读取真实历史receipt，不要求当前Need/政策/员工仍支持fresh批准。其意义是历史批准归属，不是当前正式使用许可。
record/get/list在已有scope内调用同私有验证函数，不递归调用公开get_file_approval再开员工/机会lease；不在已持报价锁后补员工锁。

### 3.2 record/get/list

record_file顺序：当前actor+bootstrap → scope guard → 本域真实quote/receipt/run核对 → artifact_reader（**报价写锁外**）→新quotation UoW取得T4同机会advisory →重读quote/receipt →比对并插入文件 →commit →释放scope。不得对Opportunity FOR UPDATE，不调用T4内部get权限门。
锁内不读外部bytes/原件，不调用renderer/Store.put；metadata和receipt不可变，锁内重读真实quote/receipt确认身份/hash未改变。当前state/时间变动不把历史关联恢复变成重新批准；当前正式生成/下载门在T8。
严格匹配meta tenant/artifact ID、kind='quote_pdf'、mime='application/pdf'、subject=quote_id、sequence=quote.version、run=真实receipt.run、generated_by受信模板、key=`{quote_id}:{version}:quote_pdf:{template}`。hash/size/time原样取meta；三个hash由各自真实来源生成，不能从HTTP复制。
按tenant+quote+template查已有文件：同artifact/version/三hash/run/size/生成时间返回原file_id；任何异绑定file_conflict，不能新建旁路template或覆盖。没有已有行才生成qfl ID、插入QuoteFileRecord；只有UoW成功退出才返回。未知quote关联提交固定storage_unknown，原artifact重试查唯一winner，不清理Store文件。
get_file在scope内按tenant+quote+file精确读关联，重核原quote/customer hash、receipt/run及真实metadata；不匹配storage_inconsistent。list_files同样逐条验证，按(template_version,file_id)升序返回tuple；合法文件数受模板集合约束，不做跨quote/tenant list。
get/list/get_file_approval只返回安全metadata/历史归属，Need失效/quote superseded或expired不阻历史读取；不因此允许正式download或重生成。customer_content_hash不能由metadata独立证明，须重投影比对；PDF实际商业文字正确性是T7/T8管线验收范围。

## 4. 仓储与0046

version_repository增加下列async方法，均首参数tenant_id、同tenant-bound UoW/session；已存在的quote读取、approval_receipt、approval_bindings、机会锁方法复用：

```text
file_by_id(tenant_id:TenantId,quote_id:QuoteId,file_id:QuoteFileId)->QuoteFileRecord|None
file_by_template(tenant_id:TenantId,quote_id:QuoteId,template_version:str)->QuoteFileRecord|None
files_for_quote(tenant_id:TenantId,quote_id:QuoteId)->tuple[QuoteFileRecord,...]
add_file(tenant_id:TenantId,record:QuoteFileRecord)->None
```

机会advisory串行化record；DB唯一兜底，不新增通用upsert/update/delete。已有UoW commit/rollback/close即可，修改仅需满足真实映射，不引入分布式事务。
0046 down_revision=实际0045。artifacts原分立kind/MIME/subject/key/generated_by CHECK改成完整互斥两分支；各自保留旧约束精度，模板SQL字面值固定本迁移quote_pdf_v1，不import未来可变注册表。通用tenant/id/hash/size/run形状/object_key/sequence约束保持。
quotation_files列：tenant_id、file_id、quote_id、quote_version、artifact_id、quote_content_hash、customer_content_hash、artifact_hash、template_version、approval_run_id、size_bytes、generated_at。PK(tenant,file)，UNIQUE(tenant,quote,template)，所有列非空；Hash/ID/positive/模板CHECK。
复合FK到真实quotes(tenant,quote)、artifacts(tenant,artifact)、quotation_approval_receipts(tenant,quote)；必要时增加被引用唯一键，不修改旧行。文件生成时间是metadata时间，不用关联重试now覆盖。
新增仅quotation_files的INSERT绑定trigger：按tenant读quote/receipt/artifact，核quote版本/完整hash、receipt完整hash/version/run、artifact类型/MIME/hash/size/time/subject/sequence/template/key/run；缺失或异绑定拒绝。不得在trigger重复利润/ABAC或读取对象存储。
customer_content_hash只做格式/不可变DB约束，真实规范投影在服务读取/写入时重验；不声称FK或SQL已重算客户DTO。直接SQL植入错误customer hash必须在get/list检测并拒绝，不能透传。
quotation_files UPDATE/DELETE触发器拒绝，保留artifacts原不可变约束；关联表不写quote state/event，不新增QuoteApproved或sent事件。跨tenant、异quote、错artifact存在但不绑定须SQL拒绝。
downgrade先检查新kind或任何quotation_files；存在即固定拒绝，不能删除业务行以降级。无新数据时删除新表/trigger并恢复旧CHECK，保留全部EMAIL_DRAFT/Raw数据。测试upgrade→downgrade→upgrade及ORM/head一致。

## 5. 固定错误与幂等边界

`QuoteFileError(ValidationError)`仅code：invalid_input/not_found/approval_missing/file_conflict/metadata_mismatch/workflow_binding_invalid/template_unsupported。
`QuoteFilePermissionError(PermissionDenied)`仅permission_denied；`QuoteFileUnavailableError(TradeOSError)`仅dependency_unavailable/lock_timeout/storage_unknown/storage_inconsistent。构造不接受自由文本；T5绑定错误映射同固定code，已有T4/T5异常公共语义不改。
record缺receipt为approval_missing、缺artifact为not_found、现有metadata错绑定为metadata_mismatch、未注册template为template_unsupported；get_file_approval无receipt返回None。已存file重读发现任何hash/meta/receipt绑定损坏统一storage_inconsistent；普通get缺file为not_found。存储错误不透传SQL，取消始终原样传播。
复用的SqlAlchemyQuotationUow必须在__aenter__/__aexit__清理时保留原取消对象，即使rollback或close再次失败也尽力完成清理并仅记录固定安全信息；没有原取消时正常清理故障仍失败关闭，不改成功/未知commit、锁顺序、自动重试或其他UoW。为此允许窄改infra/db/quotation_uow.py，并新增tests/unit/test_quotation_uow_cleanup.py及文件入口组合回归；实际报价/审批事务旧回归仍须覆盖。
请求无原始client key参数：文件key完全由真实quote/version/template决定。file_id/quote hash/客户hash/run/时间均非HTTP信任字段。record_file(artifact_id)仅受信生成/恢复内部入口；HTTP生成只接quote，由T8产生artifact。
读取已存file无法证明对象bytes完整；T8必须Store.get并核关联hash/size。metadata reader固定失败不得fallback为客户端fact。未装配scope/reader时禁止注册生产文件能力。

## 6. TDD切片与提交（整项一次独立审查）

### 6.1 纯契约、三hash与严格kind

- [ ] RED在test_quote_pdf_artifacts.py覆盖新kind/MIME/模板、客户全字段hash、DTO无原件/URL、旧邮件draft不变。

```python
def test_quote_pdf_has_its_own_generated_kind():
    kind = GeneratedArtifactKind.QUOTE_PDF
    assert GENERATED_ARTIFACT_MIME_TYPES[kind] == frozenset({'application/pdf'})
    assert kind.value != RawArtifactKind.PDF.value
def test_customer_hash_binds_terms_order(customer_view):
    changed = customer_view.model_copy(update={'approved_terms':tuple(reversed(customer_view.approved_terms))})
    assert customer_quote_hash(changed) != customer_quote_hash(customer_view)
```

- [ ] RED命令：`python3 -m pytest tests/unit/test_quote_pdf_artifacts.py tests/unit/test_artifact_store_contracts.py -q`。terms fixture至少两个不同项，验证真实行为失败而非缺依赖。
- [ ] GREEN最少实现DTO/ID/编码/注册常量与Store分支；参数化enr/quo跨kind、任意template、key错run/sequence、bool/非法hash/时间。hash不依赖template/actor、客户字段变化全覆盖。
- [ ] 目标通过后提交`feat: 定义报价文件三hash与隔离PDF派生契约`。

### 6.2 0046与QUOTE_PDF未知提交恢复

- [ ] RED真实DB：合法双分支/错误组合、普通并发winner/loser、同key异bytes或run/template拒绝；Raw/EMAIL_DRAFT原案例回归。

```python
async def test_pdf_committed_winner_survives_lost_reply(pdf_store_case):
    with pytest.raises(ArtifactCommitUnknownError):
        await pdf_store_case.put_with_commit_succeeded_then_close_failed()
    winner = await pdf_store_case.read_committed_metadata()
    assert pdf_store_case.transport.contains(winner.artifact_id)
    recovered = await pdf_store_case.restart_store_and_retry_original_put()
    assert recovered == winner
    meta, content = await pdf_store_case.store.get(winner.tenant_id, winner.artifact_id)
    assert meta.content_hash == hashlib.sha256(content).hexdigest()
    assert meta.size_bytes == len(content)
```

- [ ] fixture使用真实SqlAlchemyArtifactUnitOfWork/PG，故障包装必须先await真实commit，再在返回/close处抛错；禁止fake repo自行声称提交。transport为受控内存object transport，contains按已有object key映射测试helper，不新增生产API。
- [ ] RED还测commit成功后取消、未知时DB暂不可读零delete、不宣称成功、确未提交原key可重试、成功EXISTING仅清理自身candidate。旧EMAIL_DRAFT提交前失败仍原补偿，Raw不改。
- [ ] RED：`env -u TEST_DATABASE_URL python3 -m pytest tests/integration/test_quote_pdf_artifacts.py tests/integration/test_quote_file_migration.py tests/integration/test_artifact_store_persistence.py -q`。
- [ ] GREEN新PDF保守异常分支、固定错误/迁移/映射；先补真实T4/T5父记录和隔离库fixture，不手写不存在FK。验证空新数据roundtrip/有新数据downgrade拒绝、旧草稿保留。
- [ ] 提交`feat: 持久化报价PDF并保护未知提交的文件内容`。

### 6.3 真实metadata/批准归属与文件关联

- [ ] RED服务：受控scope拒绝/失活/跨tenant，零artifact_reader调用；四成本角色不隐式放行，sales/manager按受控当前范围可读metadata；缺依赖fail closed。
- [ ] RED真实PG+真实Store.get_meta adapter+T5真实receipt/run：错误kind/subject/version/template/hash/size/run即使ID/FK存在仍拒绝；receipt缺失/错run/type/hash、completed run查询、不伪造executor。

```python
async def test_record_file_reuses_real_winner(quote_file_case):
    first = await quote_file_case.record_real_artifact()
    second = await quote_file_case.restart_file_service_and_record_same_artifact()
    assert second == first
    assert first.content_hash == quote_file_case.artifact_hash
    assert first.customer_content_hash == customer_quote_hash(quote_file_case.customer_view)
    assert await quote_file_case.count_files() == 1
```

- [ ] RED多连接同quote/template仅一file；关联commit成功后返回错误原artifact恢复；scope lease持至关联commit、metadata reader在写锁外，失败无file/state/event。直接SQL错metadata绑定、UPDATE/DELETE拒绝；错customer hash读时fail closed。
- [ ] RED历史：Need/unit/政策变动或quote终止仍能按当前scope读既存metadata/批准归属；无generate/bytes/正式许可；关联不自动sent、不改变accepted/rejected/expired。
- [ ] RED：`env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quote_file_service.py tests/integration/test_quote_pdf_artifacts.py -q`。
- [ ] GREEN仅本域file service/仓储与infra metadata adapter，复用T5绑定规则；真实成功receipt需由T5批准路径落库，不用mock成功receipt替代集成。最终get/list重验三hash/真实meta，未授权不返回存在性详情。
- [ ] 提交`feat: 关联真实报价PDF与稳定批准执行记录`。

### 6.4 回归与交接

- [ ] 跑以上全部目标与`tests/integration/test_migrations.py`，再`python3 scripts/check_boundaries.py`；记录RED/GREEN及受控范围，不跑凭证/生产迁移。
- [ ] ADR0019记录三个hash、stable approval run归属、template注册、metadata-only与正式授权分离、未知结果孤立bytes代价、legacy补偿保持；更新就近AGENTS，不放宽workflows导入规则。
- [ ] 提交`docs: 记录报价文件存储与当前授权边界`，整项交控制器独立审查；不得把受控scope/transport当生产装配已验收。

## 7. T7/T8消费边界

T7消费shared唯一CustomerQuoteView/模板常量，只做确定性离线render；同客户hash安全呈现升级需新受信template且不覆盖旧file，不能修改商业内容。
T8真实scope、文件用途context/当前政策/批准校验、Gateway与factory尚未运行；generate/download先正式authorize，receipt查询取得稳定run，真实render→Store.put→record_file；所有bytes经Gateway，返回前重验当前状态/适用性。
history是独立用途，只按当前机会ABAC读已有PDF并标真实状态，不重新生成；不接受裸HTTP history bool绕正式download。历史hash/receipt保持不代表当前许可，原件ACL始终另验。
完成T6只证明真实DB/Store metadata/受控bytes完整性/关联恢复，不证明PDF商业文本正确、生产授权、发送或客户承诺已验证。
