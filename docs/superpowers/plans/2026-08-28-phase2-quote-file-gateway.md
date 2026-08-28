# Task 8B1：文件授权、Gateway与持久恢复 Implementation Plan

> **状态：前置核对完成，T8B1尚未实施、未运行本任务测试。** T5/T6/T7与T8A已完成独立审查（A最终6938804）；控制器已核实际公共接口并记录交接。文内“新增/提取”均不是已存在能力；当前依据/决策人共同规则仍需本任务提取。ADR指定`docs/adr/0021-quotation-file-gateway-recovery.md`，2026-08-28派发前核未占号；若期间发生冲突回报控制器，不覆盖。
> **For agentic workers:** 在现SDD控制器下使用superpowers:test-driven-development与superpowers:verification-before-completion实施下列切片；不另启executing-plans批次。控制器管理派发与一次完整Task审查，不自行再派代理或启动T8B2。

**Goal:** 交付正式/历史文件授权、客户安全版本发现、真实有界Generated读取/专用惰性写入、四个文件Gateway工具、持久限速及有限metadata恢复。
**Architecture:** 业务规则留quotation，跨域事实由workflow适配公共service；Gateway通过shared中立存储端口执行，infra只适配持久化。T8B1交付可独立真实PG验收的下层能力，T8B2才注册HTTP、解析总配置、装配实际API/worker及管理生命周期。
**Tech Stack:** 现Python 3.12+/Pydantic v2/SQLAlchemy 2.x/Postgres、现Gateway与T6 Store、T7离线renderer、T8A bounded S3；无新增服务/库/迁移。
**Spec:** `docs/superpowers/specs/2026-08-28-phase2-costing-quotation-design.md` §6/§7，以及主计划`2026-08-28-phase2-costing-quotation.md`的T8B1。本文整合已核定的文件/预留/恢复契约，不依赖临时规划笔记；不改变批准规格。

## 0. 范围、前置与验收口径

- 属于同一Phase2批次的顺序拆分，不新增Phase。T8B1完整审查通过后才开始B2；B1不等于T8B上线。
- 先读根AGENTS/HANDBOOK及涉及目录AGENTS；九条硬边界不变。所有业务金额复用既有Decimal计算/客户投影，不重算或由renderer决定。
- 不修改Gateway pipeline、通用ledger接口/状态机，不新增表、索引迁移、钱包、fencing、通用权限/事务框架。旧Raw/EMAIL_DRAFT/邮件工具语义不改。
- 不写HTTP routes、总settings JSON/env解析、实际factory/lifecycle/expiry、旧上传Deferred wrapper；全部B2。B1不实现来源解析器、真实发送、mark_sent或发送回执替身。
- 所有限额/时钟/租约依赖显式；无生产默认值。测试仅受控对象传输、真实PG+Store、可计数有限流及真实S3适配器配受控SDK；不新增MinIO镜像，不声称真实S3网络/商业资料验收。
- 当前真实代码依据：T4 `version_schemas.py`为`QuoteDetailView(content,state)`；T6已将T5/T6共同snapshot/request/binding/receipt/run纯验证抽至`domains/quotations/approval_rules.py`，B1复用这份实际实现并另核实时事实，不能恢复第二份弱化副本。Gateway `pipeline.py:304–310,491–665`与`infra/db/repositories/tool_calls.py:336–343`决定claim/recovery事实。这些接口核对不代替B1交付审查。
- 消费永久子计划：`2026-08-28-phase2-quote-approvals.md`、`2026-08-28-phase2-quote-files.md`、`2026-08-28-phase2-quote-evidence.md`（均在docs/superpowers/plans）。T6的0046及T8A的bounded能力是前置，不在B1补成假实现。

## 1. 文件责任清单

下列Create若前置交付已存在等效文件则作窄增量，不另造第二实现；具体导出先由控制器核对。

| 动作/文件 | 唯一责任 |
| --- | --- |
| Create `domains/quotations/file_access_schemas.py`, `file_access.py`, `customer_versions.py` | 文件事实/快照/安全版本DTO、当前正式与历史规则、客户分页 |
| Modify `domains/quotations/context.py`, `service.py`, `schemas.py`, `errors.py` | 新context用途与公开导出；固定错误，不扩大四角色内部门 |
| Modify `domains/quotations/approval_rules.py`, `approval_service.py`及T6 `file_service.py` | 将已有绑定/receipt/当前依据规则提取为同域复用；旧apply/文件服务均调用，不复制审批逻辑 |
| Modify `domains/quotations/version_repository.py`, `infra/db/repositories/quotations.py`, `infra/db/quote_context.py` | 窄分页读与文件用途事实租约；沿原Employee→Opportunity→Need锁，不写业务规则 |
| Create `workflows/quote_approval/file_facts.py`, `files.py`, `file_schemas.py` | approvals/demand公共事实适配、Gateway应用与技术命令/结果/错误wrapper |
| Create `shared/schemas/generated_documents.py`, `infra/quote_document_store.py` | 中立Generated DTO/ports、公开Store适配；独立metadata-only对象 |
| Modify `artifact_store/store.py`, `service_impl.py`, `transport.py` | 新增独立BoundedGenerated端口/可选transport、QUOTE_PDF writer固定技术错误；旧get不改 |
| Create `connectors/object_store/quote_pdf.py` | 专用lazy put/delete；get固定拒绝；必要时仅复用T8A实际同目录生命周期helper |
| Create `tool_gateway/handlers/quote_files.py`, `quote_file_recovery.py`, `tool_gateway/checks/quote_files.py` | 四工具/两个read各自实例、typed prepared/slot、只调用域判权、独立恢复handler |
| Create `tool_gateway/file_rate_limit.py`, `quote_file_ledger.py`, `infra/db/quote_file_rate_limit.py` | 技术预留/执行历史端口与PG适配；public ledger读取与新恢复requested审计 |
| Modify `tool_gateway/manifest.py`仅必要注册入口 | 实际`ToolRegistry.register`已存在，优先在新handler模块提供注册helper；不得为新工具改核心验证/枚举 |
| Create下文列名的unit/integration测试及局部fixtures | 真实PG多连接/Store/Gateway、受控SDK与安全断言；不伪造receipt/run |
| Modify相关AGENTS；Create `docs/adr/0021-quotation-file-gateway-recovery.md` | 中立store边界、技术预留写、MEDIUM/NONE内部关联写、保守恢复/无fencing；控制器已核未占号，不覆盖0019/0020 |

`tool_gateway`/`workflows`不得导入artifact_store/infra或域私有仓储；adapter放infra。ToolCallId仍只定义于tool_gateway.repository，不移动到shared/domain。共享CustomerQuoteView与QUOTE_PDF_TEMPLATE_VERSIONS仍各唯一class/常量。

## 2. 既有消费契约与新文件域契约

以下T6公共签名是前置目标，需核真实交付：
T6最终已保留既有EmployeeId身份形状：actor_id用shared.schemas.quote_facts.fact_identity（严格str、非空、无首尾空白/C0-C1控制、最多40字符），不新增emp_ULID门。新文件域、Gateway应用和技术DTO中由同一员工ID包装的user_id保持该兼容；仍核真实tenant/ID/在职/当前权限，格式本身不是授权。quote/file/artifact/run/ToolCallId等本批严格ID不变，增加合法旧员工ID及非法/不存在/撤权零IO反例。

```python
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

T6 QuoteFileView字段恰为`file_id,quote_id,quote_version,artifact_id,content_hash,quote_content_hash,customer_content_hash,template_version,size_bytes,generated_at`。content_hash=PDF bytes SHA256；另两个分别为完整quote hash与客户投影hash，不能互换。QuoteFileApprovalFact为`tenant_id,quote_id,quote_version,quote_content_hash,approval_run_id,approval_facts_hash,applied_at`，只证明历史真实成功归属。
T5 `QuoteApprovalPolicyReader.open(tenant_id,category:str|None)->AsyncContextManager[QuotePolicySelection]`，selection.current() async返回QuotePolicySnapshot；`QuoteWorkflowRunReader.read(tenant_id,run_id)->QuoteWorkflowRunFact|None`。T5 get_approval_application需要真实executor，**不**供文件发现run；文件从T6 get_file_approval读取真实receipt稳定run，run不必仍running。
其余既有强类型来自quotation.service/shared；本节所有新DTO strict/frozen/extra-forbid，Hash=64位小写hex，整数正且拒bool，时间aware规范UTC。内部事实不得自动HTTP/log dump。

```text
QuoteFileScopeFacts: tenant_id:TenantId;opportunity_id:OpportunityId;actor:QuoteEmployeeFact;owner:QuoteEmployeeFact
QuoteFileCurrentFacts: business:QuoteBusinessContext;deciders:tuple[QuoteEmployeeFact,...]
QuoteFormalFileSnapshot:
 tenant_id:TenantId;quote_id:QuoteId;opportunity_id:OpportunityId;quote_version:int
 quote_content_hash:Hash;customer_content_hash:Hash;approval_run_id:RunId;approval_facts_hash:Hash
 template_version:str;customer:CustomerQuoteView;checked_at:datetime
```

```python
# QuoteContextProvider追加；旧open/open_for_approval/open_approval_access不放松
def open_file_scope(self, tenant_id: TenantId, opportunity_id: OpportunityId,
                    actor_id: EmployeeId) -> AsyncContextManager[QuoteFileScopeFacts]: ...
def open_for_file(self, tenant_id: TenantId, opportunity_id: OpportunityId, actor_id: EmployeeId,
                  *, prepared_by: EmployeeId, decider_ids: tuple[EmployeeId, ...]
                  ) -> AsyncContextManager[QuoteFileCurrentFacts]: ...
class QuoteFileApprovalFactsReader(Protocol):
    async def read(self, tenant_id: TenantId, approval_ids: tuple[ApprovalId, ...]
                   ) -> tuple[QuoteApprovalFact, ...]: ...
class QuoteFileNeedValidator(Protocol):
    def require_current_unit(self, facts: NeedQuoteFacts) -> FactualField[str]: ...
class QuoteFileAccessService(Protocol):
    async def authorize(self, tenant_id: TenantId, quote_id: QuoteId,
                        *, actor_id: EmployeeId) -> QuoteFormalFileSnapshot: ...
    async def authorize_history(self, tenant_id: TenantId, quote_id: QuoteId, file_id: QuoteFileId,
                                *, actor_id: EmployeeId) -> QuoteFileView: ...
def require_quote_file_scope(facts: QuoteFileScopeFacts) -> None: ...
```

`QuoteFileAccessServiceImpl(uow_factory,actor_reader,contexts,approval_facts,need_validator,policies,workflow_runs,files,*,now,template_version)`全部必填，对应类型依次QuotationUowFactory/QuotationActorReader/QuoteContextProvider/QuoteFileApprovalFactsReader/QuoteFileNeedValidator/QuoteApprovalPolicyReader/QuoteWorkflowRunReader/QuoteFileService/Callable[[],datetime]/str；无默认actor或可空成功依赖。
本域`ContextQuoteFileScopeAuthorizer(contexts)`实现T6 guard(tenant_id,opportunity_id,*,actor_id)->AsyncContextManager[None]：open_file_scope→唯一require_quote_file_scope→yield。它不依赖FileAccessService，避免T6 files↔正式access构造环。
workflow `ApprovalServiceQuoteFileFactsReader(approvals:ApprovalService)`逐ID调用真实read_fact(tenant_id,approval_id)->ApprovalFactView并复用T5明确逐字段映射；`DemandQuoteFileNeedValidator`只调用demand.service.require_current_unit并映射固定错误，不读raw或复制规则。

### 2.1 正式/历史授权与T5规则复用

require_quote_file_scope只允许同tenant且active的boss、sales本人owner、manager当前直属owner；其余包括product/sourcing/finance拒绝。manager沿既有机会scope，owner当前在职条件不放松。原prepared_by不自动获得文件权；历史preparer失活不单独阻断。当前文件actor始终是真请求人，不等于quote_send决策人，也不构造假QuoteWorkflowExecutor。
open_file_scope仅排序锁actor/owner Employee SHARE→Opportunity SHARE，无Need/issuer/policy；open_for_file在机会锁前一次排序去重锁actor/owner/存在的preparer/全部deciders，再Opportunity SHARE→Need SHARE。全部tenant过滤/owner bootstrap重核，原SQL投影复用；已持Opportunity后不能补锁其他员工，不升级Opportunity FOR UPDATE。

正式authorize严格顺序：

1. actor_reader真实当前初检；本域短读quote仅取opportunity/preparer，随后短scope gate。拒绝零审批事实/metadata/object/renderer；不从内部四角色get取得文件许可。
2. T6 get_file_approval取得真实receipt摘要/run；本域短读原receipt/bindings，受信read_fact取得全部deciders。缺receipt为approval_missing；APPLIED/QuoteApproved不代替receipt。plain读真实run并核type/version/subject/quote version/content hash；completed/failed但绑定正确的历史run可读。
3. open_for_file锁齐所有员工→O→Need，再次scope gate；quotation UoW取现`lock_opportunity`（quotation-create-v1 advisory）并重读quote、bindings、receipt及实时facts。decider/owner/不可变决定集合变化则context_changed退出，绝不补员工锁或在此调用T6公开get/list嵌套scope。
4. 取政策selection lease，顺序context→quotation业务锁→policy；所有等待后selection.current()及新now。selected policy id/hash必须等于basis/submission，不只比较利润底线。
5. 复用T5原请求/绑定/receipt校验：类型恰全且唯一、tenant/quote/version/payload/request/期限/run一致；实时facts的不可变决定hash=receipt.facts_hash，APPROVED→APPLIED不能改变hash。每包approve、真实decided_by/at、state approved或applied且未过期；全部deciders逐一沿require_quote_approval_access(action='apply')，禁止prepared_by/当前owner/提交owner自批。
6. quote为当前有效approved/sent且未过期；Need完整事实/当前unit、issuer/context/scope/全部evidence/quoted及期限重验。复用T5当前依据门与T4 validate_quote_basis(真实intent,basis,context,now=now)，不重价、不重读客户原件。
7. project_customer→validate_customer_projection→customer_quote_hash，构造snapshot；退出只读UoW/policy/context。不写state/receipt/mark_applied/sent。snapshot仅检查时点，不是可跨请求传递的授权票据。

**同域最小提取，不调用审批apply冒充校验：** T5交付后将现_snapshot/_validate/_bound/receipt/_fresh及decider循环中的纯规则提取到approval_rules.py，T5原入口与文件入口一起调用。以下是目标签名，已有等效helper必须复用，不保留两套逻辑：

```python
def quote_approval_snapshot(quote: QuoteDetailView, previous: QuoteDetailView | None) -> QuoteApprovalSnapshot: ...
def require_quote_approval_requests(snapshot: QuoteApprovalSnapshot,
    facts: tuple[QuoteApprovalFact, ...], *, run_id: RunId) -> None: ...
def require_quote_approval_bindings(bindings: tuple[QuoteApprovalFact, ...],
    facts: tuple[QuoteApprovalFact, ...]) -> None: ...
def require_quote_approval_receipt(snapshot: QuoteApprovalSnapshot,
    bindings: tuple[QuoteApprovalFact, ...], receipt: QuoteApprovalApplicationReceipt) -> None: ...
def require_quote_current_deciders(quote: QuoteDetailView, facts: tuple[QuoteApprovalFact, ...],
    business: QuoteBusinessContext, deciders: tuple[QuoteEmployeeFact, ...]) -> None: ...
def require_quote_current_basis(quote: QuoteDetailView, context: QuoteBusinessContext,
    policy: QuotePolicySnapshot, *, now: datetime) -> None: ...
```

previous仅同tenant真实version-1（无replaces的终态后新报价也如此），version>1缺历史固定storage_inconsistent。requests只核原包不要求已决定；bindings比原不可变请求字段；receipt另核quote/run/决定集合/hash/quote_send_decider。实时决定/期限验证从T5 apply提取同域helper`require_quote_approved_decisions(facts:tuple[QuoteApprovalFact,...],*,now:datetime,allow_applied:bool)->None`，allow_applied只由可信入口固定：原fresh apply=False，file=True且先验receipt，不暴露HTTP参数。
T5仍单独要求执行actor为quote_send decider及pending状态、先历史receipt恢复；提取不得改变它的重放/状态/错误行为。文件只读入口不调用T5执行session，不因复用helper要求文件actor拥有成本角色或是批准人。run校验复用T6已提取的require_quote_run_binding；没有该真实helper则先核对，不能导入workflow私有repository。
history仅委托T6 get_file的当前scope、历史quote/customer/artifact/receipt/run完整绑定，不要求当前Need/unit/policy/decider仍有效；不返回客户DTO、不重新生成、不自动从正式失败fallback。正式和历史各在IO前后重验；后置失败清bytes且不删已提交文件。T6 metadata读在写锁外；无锁可覆盖客户端最终收到PDF的全部时间。

### 2.2 无成本客户版本分页

```python
class QuoteCustomerVersionsService(Protocol):
    async def list_versions(self, tenant_id: TenantId, opportunity_id: OpportunityId,
        *, actor_id: EmployeeId, before_version: int | None, limit: int) -> QuoteCustomerVersionPage: ...
# QuotationVersionRepository窄增量，SQL同tenant/机会，版本降序
async def customer_version_page(self, tenant_id: TenantId, opportunity_id: OpportunityId,
    *, before_version: int | None, limit: int) -> tuple[QuoteDetailView, ...]: ...
```

```text
QuoteFileAction = Literal['generate','download_current','read_history']
QuoteFileBlockerCode = Literal['quote_inactive','quote_expired','approval_missing','approval_invalid',
 'approval_expired','decider_invalid','context_changed','policy_stale','basis_invalid']
QuoteFileActionBlocker: action:QuoteFileAction;code:QuoteFileBlockerCode
QuoteCustomerFileEntry: file:QuoteFileView;allowed_actions:tuple[QuoteFileAction,...]
QuoteCustomerVersionView: quote_id:QuoteId;version:int;state:QuoteState;created_at:datetime;valid_until:datetime
 is_past_valid_until:bool;files:tuple[QuoteCustomerFileEntry,...];allowed_actions:tuple[QuoteFileAction,...]
 blockers:tuple[QuoteFileActionBlocker,...];checked_at:datetime
QuoteCustomerVersionPage: items:tuple[QuoteCustomerVersionView,...];next_before_version:int|None
```

`QuoteCustomerVersionsServiceImpl(uow_factory,contexts,access,files,*,now,maximum_page_size:int)`依赖均必填。scope→读页→释放scope→逐quote T6 list_files/同正式authorize→返回前scope再验；不调用四角色内部get/list，不在持长锁时套公开文件guard。limit正int拒bool且<=显式maximum_page_size；before_version可空否则正int。SQL取limit+1判断后页，返回前limit项，确有后页时游标=本页末version，否则None。
version.allowed_actions仅generate（已有文件时同canonical只复用）；已有file的allowed_actions仅download_current/read_history。只确定性正式失效映射上述blockers（generate/download_current各自有对应blocker），已有文件历史动作不受其影响；基础设施/绑定损坏请求失败，不能伪装空列表/商业blocker。真实state不因读而过期，另给is_past_valid_until；actions仅checked_at提示不是许可token。无价格/成本/利润/Need/basis/provenance/审批payload/对象地址。

## 3. 中立Store与QUOTE_PDF专属IO

共享DTO只含技术存储契约；不持业务规则，Hash/ID/正int/UTC沿§2。content repr=False，不注册HTTP/JSON。

```text
GeneratedDocumentMeta: tenant_id:TenantId;artifact_id:ArtifactId;kind:Literal['quote_pdf'];artifact_hash:Hash
 size_bytes:int;mime_type:Literal['application/pdf'];workflow_run_id:RunId;subject_ref:str;sequence_number:int
 idempotency_key:IdempotencyKey;generated_by:str;generated_at:datetime
GeneratedDocumentPayload: meta:GeneratedDocumentMeta;content:bytes
QuotePdfWriteLimits: connect_timeout_ms:int;read_timeout_ms:int;total_timeout_ms:int;maximum_attempts:Literal[1]
```

```python
# shared/schemas/generated_documents.py
class GeneratedDocumentMetadataReader(Protocol):
    async def get_meta_by_key(self, tenant_id: TenantId, key: IdempotencyKey) -> GeneratedDocumentMeta | None: ...
class GeneratedDocumentStore(GeneratedDocumentMetadataReader, Protocol):
    async def get_bounded(self, tenant_id: TenantId, artifact_id: ArtifactId,
                          *, maximum_bytes: int) -> GeneratedDocumentPayload: ...
    async def put_pdf(self, tenant_id: TenantId, content: bytes, *, workflow_run_id: RunId,
        subject_ref: str, sequence_number: int, idempotency_key: IdempotencyKey,
        generated_by: str) -> GeneratedDocumentMeta: ...
# artifact_store/store.py；独立增量，不迫使旧Store用户迁移
class BoundedGeneratedArtifactStore(Protocol):
    async def get_bounded(self, tenant_id: TenantId, artifact_id: ArtifactId,
                          *, maximum_bytes: int) -> tuple[GeneratedArtifactMeta, bytes]: ...
```

`infra/quote_document_store.py::GeneratedStoreDocumentAdapter(store:GeneratedArtifactStore,bounded:BoundedGeneratedArtifactStore)`逐字段适配公开口；另`GeneratedStoreDocumentMetadataReader(store:GeneratedArtifactStore)`只公开get_meta_by_key，不把有put/get的同对象换窄注解交给显式恢复。均无SQL/SDK/凭证/业务规则，kind/MIME错绑定拒绝，不把email_draft强转quote_pdf；generated_by=受信template，artifact_hash=Store.content_hash。
GeneratedArtifactStoreImpl新增keyword-only bounded_transport:BoundedObjectBlobTransport|None=None，旧构造/get不变；新get_bounded缺依赖固定ArtifactBoundedReadUnavailable，绝不fallback。tenant metadata短事务退出后，检查kind/MIME/size，limit=min(caller maximum_bytes,store maximum_bytes)；meta超限零对象IO。将min(limit,meta.size_bytes)交T8A bounded get，最多累计该值+1哨兵即停，核真实len及SHA256；不能先全量body.read()再验len，也不信ContentLength/Range。沿T8A：预检/transport超限为ArtifactReadLimitExceeded，短对象或同长坏hash为ArtifactIntegrityError。
新S3QuotePdfObjectBlobTransport(settings:S3ObjectStoreSettings,secret_resolver:ObjectStoreSecretResolver,*,limits:QuotePdfWriteLimits)结构化实现旧ObjectBlobTransport三个async签名put(key,bytes)->None、delete(key)->None、get(key)->bytes；get固定read_unsupported且零IO。writer只接受validated generated/{tenant}/{artifact}键和bytes；它不选择winner、不读业务、不开放删除到shared口。
limits必填strict/frozen，毫秒正int拒bool，maximum_attempts严格1。构造零resolve/SDK/网络/文件读取；每次put/delete执行才校验→单调deadline→resolve固定refs→专属client→一次put_object/delete_object→finally close。Config显式connect/read timeout、retries.total_max_attempts=1；不用TransferManager/multipart/常驻credential/client/自动重试。deadline前后检查不再开始新操作。
total_timeout是预算/分类，不证明可kill SDK线程。取消等待受配置SDK调用与finally close收口后原样传播，禁止先删candidate；超时/SDK错误/成功后close错误一旦开始写均outcome_unknown，早于SDK确定失败才unavailable。close不覆盖原取消/更早unknown。不为此新建进程或服务沙箱。
T6 Store的put_attempted保守语义优先：之后任何不能证明未提交的异常保留candidate、ArtifactCommitUnknownError；取消原样。仅确认EXISTING且自己的candidate未被引用才允许既有loser cleanup；unknown零delete、winner永不删。T8B1不重写T6事务策略或旧EMAIL_DRAFT cleanup。

## 4. 四工具、typed载荷与唯一结果槽

manifest版本各为显式`v1`，high_risk_stage_profile=None，cost_class=FREE（技术成本分类不表示资源无限或无S3费用）；permission标签只作工具能力名，不替代域ABAC。

| tool_id | risk/idempotency/requires_approval | checks / required_permissions / strict params |
| --- | --- | --- |
| quotation.file.generate | MEDIUM/REQUIRED/True | tenant,permission,approval,idempotency,rate_limit / quotation:file_generate / quote_id |
| quotation.file.read | LOW/NONE/True | tenant,permission,approval / quotation:file_read / quote_id,file_id |
| quotation.file.history.read | LOW/NONE/False | tenant,permission / quotation:file_history_read / quote_id,file_id |
| quotation.file.reconcile | MEDIUM/NONE/True | tenant,permission,approval / quotation:file_reconcile / quote_id,original_generation_call_id |

每个input_schema additionalProperties=False，ID按现quo/qfl/tcl+ULID验证；无actor/role/approved/run/key/template/history/force/客户内容。output_schema仅安全provider_ref:file_id，reconcile另固定status='metadata_recovered_original_unresolved'；其他状态/bytes/DTO只进typed槽，不扩pipeline安全output键。
新增QuoteFilePermissionCheck/QuoteFileApprovalCheck均实现现CheckStage.check(ctx,state)，只调公共服务。permission先QuotationActorReader.read_current核真实tenant+员工（ctx.user_id是B2传入的真实员工标识的UserId包装，不造usr/default boss），并执行文件scope或正式授权；approval复用完整正式authorize，history不注册。初始许可不得把角色/员工映射写进共享可变属性。
注册helper`register_quote_file_tools(registry:ToolRegistry,*,generate:QuoteFileGenerateHandler,read:QuoteFileReadHandler,history:QuoteFileReadHandler,recovery:QuoteFileRecoveryHandler)->None`仅注册四固定manifest/实例；read/history实例分别固定用途，不接客户端mode。独立文件registry/check映射交B2，不修改旧email/DNS注册集合。

```text
QuoteFilePreflight（Gateway本地）:
 tenant_id:TenantId;user_id:UserId;actor_id:EmployeeId;tool_id:Literal['quotation.file.generate','quotation.file.read','quotation.file.history.read']
 quote_id:QuoteId;file_id:QuoteFileId|None;snapshot:QuoteFormalFileSnapshot|None;history_file:QuoteFileView|None
QuoteFilePrepared（Gateway本地，payload/repr=False）:
 tenant_id:TenantId;actor_id:EmployeeId;tool_id:Literal['quotation.file.generate','quotation.file.read','quotation.file.history.read'];quote_id:QuoteId;file_id:QuoteFileId|None
 snapshot:QuoteFormalFileSnapshot|None;history_file:QuoteFileView|None;rate_claim:QuoteFileRateRequest|None
QuoteFileBytesPayload: file:QuoteFileView;content:bytes（repr=False）
QuoteFileRecoveryPayload: file:QuoteFileView;original_generation_call_id:ToolCallId;recovery_call_id:ToolCallId
 original_status_at_check:Literal['executing'];checked_at:datetime
QuoteFileCallFailure: code:QuoteFileFailureCode;retry_after_seconds:int|None
QuoteFileCallResult = QuoteFileBytesPayload | QuoteFileRecoveryPayload | QuoteFileCallFailure
```

permission生成QuoteFilePreflight，approval只用同身份重验并替换snapshot；prepare逐项核ctx/handler固定tool_id绑定。generate必须snapshot非空/file_id及history_file空；正式read必须snapshot及file_id非空/history_file空；history必须history_file及file_id非空/snapshot空。rate_claim仅generate在rate stage填入，执行时必须非空；read/history始终空，不接客户端自报。
`QuoteFileResultSlot.put(result:QuoteFileCallResult)->None/take()->QuoteFileCallResult|None/clear()->None`同步；容量总计一份，async-task owner隔离（含继承ContextVar的子task拒领取），take即删；重复put/跨task抛Gateway本地QuoteFileResultSlotError(ValidationError)，固定code=invalid_input/message=“文件结果交接无效”。失败替换bytes须先clear；所有check/prepare/execute/reconcile固定失败进同槽，finally清本task槽。只有Gateway SUCCEEDED才能取bytes/recovery；非成功仅消费failure，误有bytes立即清并失败。DUPLICATE没有槽，按真实provider_ref恢复；槽不是持久状态，不建立通用结果框架。
GenerateHandler必填access/files/store/renderer:QuotePdfRenderer/history_reader/fingerprints/slot/now；ReadHandler只需access/files/store/slot/fingerprints/maximum_bytes和构造时固定的正式或历史用途。RecoveryHandler依赖见§6，绝不持宽Store/renderer。
prepare/execute沿现async签名`prepare(ctx,preflight)->PreparedToolCall`、`execute(tenant_id,prepared)->Mapping[str,SafeScalar]`；generate额外实现同形状async reconcile(tenant_id,prepared)，只恢复metadata。prepared payload不进repr/ledger，audit_projection不意味着已持久化。

### 4.1 canonical与执行恢复

唯一生成key=`{quote_id}:{quote_version}:quote_pdf:{template}`，Store与Gateway相同；tenant/tool由ledger绑定，actor不改变key。HMAC helper `quote_file_generation_parts(snapshot:QuoteFormalFileSnapshot)->tuple[bytes,...]`固定顺序：b'quote-file-v1'、tenant、b'quotation.file.generate'、quote_id、version十进制ASCII、template、quote_content_hash、customer_content_hash、approval_run_id、approval_facts_hash，其余UTF-8。fingerprint(parts)返回digest及真实key_version原样填PreparedToolCall两字段；协议part不是key_version。
tool version单独由manifest/ledger绑定并在恢复核验。密钥或协议变更冲突失败关闭，不换key/template重生成，不声称跨密钥轮换恢复。每次user_id审计/当前授权保持；ToolCallContext.run_id/approval_ref/campaign_ref=None，artifact.workflow_run_id取真实receipt run，不冒充本次正在执行批准run。
应用先真实authorize得到canonical，再invoke；check/prepare再次核key与新snapshot等值，变化固定冲突，零对象IO。执行路径：

1. mark_executing及其事件已确定提交后，handler重新authorize；用§5真实rate_claim核canonical执行历史，恰1事件才可能首次生成，>=2只metadata恢复，0/错绑定/非executing拒绝。
2. T6 list_files查本模板，再按原key get_meta_by_key。现存file/metadata都必须严格真实绑定；已有file返回同ID，只有metadata则T6 record_file补关联。错绑定/故障不能当不存在。
3. 只有普通execute、首次历史、两个查询均确定无记录时render(customer,template_version=template)→put_pdf(真实run/key/version/template)→record_file(artifact_id,actor_id)。T7由B2提供显式byte/page/text limits；B1不能自行接受或重算金额。全部对象IO在Employee/O/Need/policy长锁外。
4. generate.reconcile和历史>=2分支同一个metadata-only helper；没metadata、存储/关联未知一律reconciliation_required，零render/put/delete。不能用执行历史0冒称首次，也不能用category被RATE_LIMITED覆盖后重新生成。
5. 最终T6 get_file及authorize再核quote/version/三hash/receipt run/facts hash/template。成功provider_ref只用真实file_id；取消/后置拒绝/ledger完成失败不返回成功、不清winner。generate只返回metadata，不顺带读bytes。
6. DUPLICATE先当前authorize，再用真实provider_ref解析qfl并T6 get_file核绑定；无/错ID固定storage_inconsistent，不能猜ID。read/history各前后门，并核bounded返回meta与file/真实receipt绑定后才put bytes槽。

真正EXECUTING无条件不可reclaim，即使lease过期。现_can_reclaim仅允许CLAIMED或FAILED_TRANSIENT，且lease_expires_at非空且<=now、retry_after_at为空或<=now；重认领后只有**原status=FAILED_TRANSIENT且原error_category=RECONCILIATION_REQUIRED**才标记reconciliation_only进入既有hook。到期CLAIMED或其他可重认领FAILED_TRANSIENT走execute，但仍由执行历史safeguard保证旧执行只能metadata恢复。原artifact已commit但卡EXECUTING只能用§6显式恢复，不改旧ledger成功、不假造原claim可恢复。

## 5. 真实PG事件预留与执行历史

```text
QuoteFileRateLimits: maximum_admissions:int;window_seconds:int;lock_timeout_ms:int;statement_timeout_ms:int
QuoteFileRateRequest: canonical_call_id:ToolCallId;tool_version:str;idempotency_key:IdempotencyKey
 request_fingerprint:Hash;fingerprint_version:str;actor_id:UserId
QuoteFileRateDecision: outcome:Literal['reserved','limited'];reservation_event_id:str|None;retry_after_seconds:int|None
```

```python
class QuoteFileGenerationRateLimiter(Protocol):
    async def reserve(self, tenant_id: TenantId, request: QuoteFileRateRequest) -> QuoteFileRateDecision: ...
class QuoteFileExecutionHistoryReader(Protocol):
    async def has_prior_execution(self, tenant_id: TenantId, request: QuoteFileRateRequest) -> bool: ...
```

全部strict/frozen；limits正int排bool无默认。reserved仅tce_<ULID>且retry=None；limited仅event=None且retry整数1..86400（Gateway技术上限，不是窗口默认）。infra的PostgresQuoteFileGenerationRateLimiter(factory,*,limits,lease_owner:str,id_generator:Callable[[str],str])与PostgresQuoteFileExecutionHistoryReader(factory,*,statement_timeout_ms:int)必填；后者不写表。
QuoteFileRateLimitCheck(limiter,slot)在claim后读真实state.tool_call_id、受信manifest.version、prepared指纹及ctx.key/user，验证generate/stage/prepared非空。tool_version必须原样来自manifest且按既有安全label校验，不从请求体取得；reserve及history reader均精确核canonical.tool_version一致。reserved后替换本工具frozen payload的rate_claim及state.prepared，保留原摘要；不能用prepare时received ID、state不存在的attempt/lease字段或局部reconciliation_only。
reserve独立短事务：设置显式timeouts→`quote-file-rate-v1`+tenant稳定advisory→仅本canonical行FOR UPDATE→锁后clock_timestamp。核tenant/tool/key/digest/key_version/status=claimed/真实attempt>=1/lease_owner同配置/lease_expires_at>DB now。不能要求本次actor=canonical初始user。
同事务JOIN同tenant ToolCallRow，过滤tool=generate及events stage=rate_limit/outcome=reserved/rule=quote-file-generate-v1、occurred_at>now-window；失败/unknown/未来时间戳均保守计入，不按actor/run/template分桶。降序occurred_at/event_id取第N条（offset N-1 limit1），不存在则可预留；存在则limited。
预留插真实ToolCallEventRecord：new_id('tce'),tenant,canonical ID,上述stage/outcome/rule,actor_id=本次user,occurred_at=锁后DB now,duration_ms=0,cost_note='claim_attempt:{真实attempt}'；category/run/campaign/message refs全部None。event与计数同事务，确定commit/close成功才reserved。不可用后续allowed事件代替预留；event append-only，不退款/改写，不按attempt去重。
Retry-After=ceil(max(第N条时间+window,claim.lease_expires_at)-DB now)，夹1..86400，只表示再查时间。窗口/N变更保守计数；每次新reserve新event，未知/取消/metadata恢复也消耗；成功duplicate在前面短路不消耗。commit/close未知不放行，不自动重试reserve。
limited必须抛ToolGatewayError(RATE_LIMITED,retry_after_seconds=...)而非CheckRejection（CLAIMED不能写REJECTED）；claim_invalid→RECONCILIATION_REQUIRED，storage_unavailable/commit_unknown→PROVIDER_TRANSIENT（本次还未对象写，预留不退款）。同槽failure保留固定rate_limited/安全retry。
history reader仅plain读取真实tenant/canonical/tool/key/HMAC/status=executing及最多2条已提交ledger/executing事件：1条False，>=2 True，0/错绑定/非executing抛claim_invalid。本次事件已由pipeline提交，第二条即先前执行；前次尚未render也保守禁重新生成。无法依靠会被覆盖的error_category判断unknown。
无新fencing：attempt_count/lease_owner不是invocation token；限速只保证每次放行有独立已提交预留，不修复通用旧协程与新claim所有权竞争。tenant锁不与业务锁交叉、count JOIN不锁别的call；SQL超时失败关闭，无新索引/迁移、无内存fallback。

## 6. 显式恢复：新call前置审计、旧call完全只读

`tool_gateway/quote_file_ledger.py`定义下列技术DTO/ports及public UoW适配；不导入workflow或domain私有仓储。

```text
QuoteGenerationLedgerFact: tenant_id:TenantId;call_id:ToolCallId;tool_id:str;tool_version:str
 idempotency_key:IdempotencyKey|None;request_fingerprint:Hash|None;fingerprint_version:str|None
 status:ToolCallStatus;provider_ref:str|None
QuoteFileRecoveryPreflight: tenant_id:TenantId;user_id:UserId;actor_id:EmployeeId;quote_id:QuoteId
 original_generation_call_id:ToolCallId;recovery_call_id:ToolCallId;recovery_tool_version:str
```

```python
class QuoteGenerationLedgerReader(Protocol):
    async def read(self, tenant_id: TenantId, call_id: ToolCallId) -> QuoteGenerationLedgerFact | None: ...
class QuoteRecoveryAudit(Protocol):
    async def append_requested(self, tenant_id: TenantId, *, preflight: QuoteFileRecoveryPreflight,
        original: QuoteGenerationLedgerFact, request_fingerprint: str, fingerprint_version: str) -> None: ...
```

PublicLedgerQuoteGenerationReader(uow_factory:ToolGatewayUnitOfWorkFactory)仅调用真实calls.get(tenant,id)逐字段投影；无find_by_key、枚举或complete代理。PublicLedgerQuoteRecoveryAudit(uow_factory,*,now:Callable[[],datetime],id_factory:Callable[[str],str])只public get/append，退出UoW确定成功才返回；无SQL新表或业务判权。
RecoveryHandler必填access/files/metadata_only:GeneratedDocumentMetadataReader/ledger_reader/audit/fingerprints/slot/generate_tool_version/now；不持renderer、宽Store、put/delete/bytes/raw/SDK/resolver。prepared专用`QuoteFileRecoveryPrepared(preflight,original,snapshot)`三字段，均本次真实typed对象且repr=False。
permission check把state真实new ID及manifest.version写入preflight，approval保留，prepare核tenant/user/actor/quote/old ID均一致；NONE technical claim保证new received ID就是新canonical，这一点不得推广到REQUIRED generate。
先正式authorize，再read old：仅同tenant原generate、受支持tool_version、EXECUTING/provider_ref=None、非空原canonical/key/HMAC可恢复。逐值匹配§4原key及生成协议HMAC（不能用reconcile tool ID）；wrong key/version/fingerprint或旧密钥不支持固定original_binding_invalid，其他状态original_state_changed。
recovery本次fingerprint parts固定`quote-file-reconcile-v1,tenant,quotation.file.reconcile,original_call_id,original_key,original_digest,original_key_version,quote_content_hash,customer_content_hash,approval_run_id,approval_facts_hash,template`（UTF-8）；返回真实key_version。ctx.idempotency_key/run_id=None；新技术key=call:{newID}绝不传Store。
execute重新正式授权/old绑定→仅按原key读metadata并严格核tenant/kind/MIME/subject/sequence/run/template/key/hash/size；无metadata固定metadata_not_found且零生成。**record_file前**await audit.append_requested，成功退出UoW后才补关联。
audit核新行：tenant/newID≠oldID/tool=reconcile/版本同preflight/status EXECUTING/duplicate_of/provider_ref=None/key=call:{newID}/摘要版本同prepared/user同preflight/run-campaign-message None。旧行与传入old事实全部绑定逐值一致、仍EXECUTING/provider_ref=None；只plain SELECT，不取旧call锁、不假称fence。
新event仅落newID：stage=recovery/outcome=requested/rule=original:{oldID}、new_id('tce')、真实本次user、UTC now、duration_ms=0，其余category/run/campaign/message/cost_note None。规则标签合法有界，不含正文。旧行/events零写；prepared.audit_projection不能替代此持久事件。
审计已知失败/commit未知/close异常/取消均零record_file；成功event仅证明请求恢复，不是metadata_linked或旧生成成功。随后T6幂等record_file/get_file核三hash及真实receipt；正式后置门再验，最后plain读old仍EXECUTING才put恢复payload；old变化报original_state_changed，已补file保留。
原线程可仍运行：metadata不可变、T6唯一quote/template及同artifact幂等确保同file；不取消旧线程、不改attempt/provider_ref/错误、不delete。旧线程最后自行成功不是恢复插件的功劳。这里只找回metadata/关联，不保证对象bytes存在，正式/历史下载仍独立bounded核验。

## 7. 固定错误与交给B2的技术应用

新domain `QuoteFileAccessError(ValidationError)`仅§2 QuoteFileBlockerCode；`QuoteFileAccessPermissionError(PermissionDenied)`仅permission_denied；`QuoteFileAccessUnavailableError(TradeOSError)`仅dependency_unavailable/lock_timeout/storage_inconsistent。输入/缺对象沿T6 QuoteFileError invalid_input/not_found；不存在与跨tenant统一。业务失效才可成为customer页blocker，facts/receipt/metadata损坏一律storage_inconsistent，不能误报“待审批”。
明确映射：当前quantity/unit缺失/陈旧/未确认及当前issuer缺失→context_changed；当前policy变化→policy_stale；当前证据期限/quoted/scope适用性不满足→basis_invalid；quote未过期状态之外→quote_inactive，到期→quote_expired。持久原绑定/hash/run错配不是这些商业缺项，固定storage_inconsistent；未知reader故障dependency_unavailable，原lock_timeout保持。无任意异常转blocker的兜底。
shared `GeneratedDocumentError(TradeOSError)`代码固定not_found/invalid_binding/conflict/unavailable/commit_unknown/corrupt/read_limit/bounded_unavailable，分别映射真实Artifact错误；未知异常固定unavailable，写尝试后未知commit_unknown，取消原样。transport `QuotePdfBlobTransportError(TradeOSError)`仅invalid_input/read_unsupported/unavailable/outcome_unknown，中文分别“报价文件对象输入无效/报价文件对象须使用有界读取/报价文件对象传输不可用/报价文件对象操作结果未知”。不带底层文本、端点、key或密钥。
QuoteFileRateError(TradeOSError)仅invalid_input/claim_invalid/storage_unavailable/commit_unknown，中文分别“文件限速输入无效/文件调用认领无效/文件限速存储不可用/文件限速预留结果未知”。QuoteRecoveryAuditError(TradeOSError)仅recovery_audit_binding_invalid/recovery_audit_unavailable/recovery_audit_unknown，中文分别“恢复审计绑定无效/恢复审计暂不可用/恢复审计状态待核对”。
文件统一安全failure白名单是下列**完整并集**，定义于Gateway本地，不扩ToolErrorCategory；workflow只导入同别名，不复制枚举：

```text
QuoteFileFailureCode = QuoteFileBlockerCode | Literal[
 'permission_denied','not_found','invalid_input','storage_inconsistent','dependency_unavailable','lock_timeout',
 'read_limit','reconciliation_required','idempotency_conflict','rate_limited',
 'original_not_found','original_binding_invalid','original_state_changed','metadata_not_found','recovery_unavailable',
 'recovery_audit_binding_invalid','recovery_audit_unavailable','recovery_audit_unknown',
 'invalid_config','template_unsupported','text_limit_exceeded','page_limit_exceeded','byte_limit_exceeded',
 'font_unavailable','unsupported_glyph','layout_failed','render_failed']
```

所有code固定中文表一一覆盖，异常constructor只接code/安全retry；不得str(exc)、拼对象ID或自由底层消息。T7固定code原样纳入，bounded不可用→dependency_unavailable，Store corrupt/invalid_binding→storage_inconsistent，冲突→idempotency_conflict，实际限额→read_limit；权限→PERMISSION_DENIED，业务批准/当前适用失效→APPROVAL_REQUIRED，输入/渲染/异绑定→VALIDATION，确定同key冲突→IDEMPOTENCY_CONFLICT，限速→RATE_LIMITED。
未尝试对象/关联写的依赖失败→PROVIDER_TRANSIENT；已尝试put/record后的任意未知→RECONCILIATION_REQUIRED。metadata暂缺、original_state_changed、审计未知也为RECONCILIATION_REQUIRED；审计绑定错为VALIDATION，已知未提交的审计不可用为PROVIDER_TRANSIENT。check/prepare错误也进同槽再抛typed Gateway错误；取消原样且finally清槽。

workflow files.py公开下列async入口；constructor必填`gateway:QuoteFileGatewayInvoker,access:QuoteFileAccessService,files:QuoteFileService,slot:QuoteFileResultSlot,ledger:QuoteGenerationLedgerReader,fingerprints:HmacFingerprintProvider,generate_tool_version:str`，不自己构造Gateway或现审批实例。

```python
class QuoteFileGatewayInvoker(Protocol):
    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult: ...
class QuoteFilesApplication:
    async def generate(self, tenant_id: TenantId, quote_id: QuoteId, *, actor_id: EmployeeId) -> QuoteFileView: ...
    async def download(self, tenant_id: TenantId, quote_id: QuoteId, file_id: QuoteFileId,
                       *, actor_id: EmployeeId) -> tuple[QuoteFileView, bytes]: ...
    async def read_history(self, tenant_id: TenantId, quote_id: QuoteId, file_id: QuoteFileId,
                           *, actor_id: EmployeeId) -> tuple[QuoteFileView, bytes]: ...
    async def reconcile(self, tenant_id: TenantId, quote_id: QuoteId, original_generation_call_id: ToolCallId,
                        *, actor_id: EmployeeId) -> QuoteFileRecoveryResult: ...
```

workflow file_schemas.py独占以下strict技术wrapper，导入原ToolCallId并校验tcl_<ULID>；domain不导入/重导出，Gateway不导入workflow：

```text
QuoteFileRecoveryCommand: quote_id:QuoteId;original_generation_call_id:ToolCallId
QuoteFileRecoveryResult: outcome:Literal['metadata_recovered_original_unresolved'];file:QuoteFileView
 original_generation_call_id:ToolCallId;recovery_call_id:ToolCallId;original_status_at_check:Literal['executing']
 original_ledger_modified:Literal[False];checked_at:datetime
QuoteFileApiError: code:QuoteFileFailureCode;message:str（固定表）;tool_call_id:ToolCallId|None
 original_generation_call_id:ToolCallId|None;retry_after_seconds:int|None
QuoteFileApplicationError(TradeOSError): detail:QuoteFileApiError
```

应用先核Gateway状态再消费槽；recovery仅SUCCEEDED且result.tool_call_id=payload.recovery_call_id、provider_ref=file.file_id才构造wrapper。错误保留真实ToolCallResult.tool_call_id；只有再次真实正式授权并ledger逐项核同quote/key/HMAC/tool版本且仍EXECUTING，才能额外给original_generation_call_id。CONFLICT新ID不标原ID；尚未invoke的错误ID=None。不改全局ApiErrorResponse，不把技术ID写进message；B2负责路由/状态/OpenAPI/Retry-After投影。
丢失原ID可再次同canonical generate：真实旧EXECUTING导致IN_PROGRESS短路，拿到原canonical ID，零rate/render/put；这不是find_by_key或成功恢复。无当前权/旧指纹不匹配不保证找回。无ack/complete旧审计接口；显式恢复只陈述checked_at原审计仍unresolved，不能压成普通生成成功。

注意现Gateway.claim比较digest/key_version，但**不比较tool_version**；不能假定通用幂等替插件验证该字段。generate的DUPLICATE/IN_PROGRESS短路没有经过rate/history，因此应用必须通过已有ledger reader按真实result.tool_call_id核tenant/generate/tool_version=配置generate_tool_version/原canonical/key/HMAC，DUPLICATE还核真实provider_ref，之后才返回文件或可恢复原ID。版本不匹配固定idempotency_conflict（无原恢复ID），零render/put；不改核心claim、不改HMAC既定parts、不换canonical。真实PG测试覆盖当前manifest v1遇持久其他版本的可重认领、DUPLICATE及IN_PROGRESS三路径。

## 8. TDD切片与提交（一次Task审查，不逐提交另派review）

测试前控制器确认前置真实交付。所有RED先写行为断言→运行该单测确认行为FAIL（缺接口可ImportError，不能把DB/依赖缺失当有效RED）→最小GREEN→同组PASS→小提交。下列命令显式unset TEST_DATABASE_URL，使用现临时PG fixture，不触任意既有库。以下case fixture是本切片需新增的受控测试组织，不是声称仓库已有；内部真实服务/DB，只有renderer计数包装与SDK/对象边界受控。

### 8.1 文件规则与客户发现

- [ ] 创建`tests/unit/test_quote_file_access.py`, `test_quote_customer_versions.py`及`tests/integration/test_quote_file_access.py`；fixture用T4真实新报价+T5真实submit/decide/apply/run生成receipt，T6真实Store/file，不直接INSERT假成功receipt。
- [ ] RED最小断言：

```python
async def test_file_actor_is_not_send_decider(approved_file_case):
    c = approved_file_case
    assert c.sales_id != c.quote_send_decider
    s = await c.access.authorize(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    assert s.approval_run_id == c.real_receipt.approval_run_id
    assert s.customer_content_hash == customer_quote_hash(s.customer)
```

- [ ] Run `env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quote_file_access.py tests/unit/test_quote_customer_versions.py tests/integration/test_quote_file_access.py -q`；GREEN新增domain/SQL用途、T5同域helper提取、workflow事实adapter、分页。
- [ ] 参数化actor/decider不同、任一非quote_send决策人失活/换role/直属、自批三ID、四成本角色文件拒绝、history只当前ABAC；receipt/bindings/run/hash错误、APPLIED无receipt、mixed APPROVED/APPLIED稳定hash、包到期及completed run。
- [ ] 多连接测Employee归属/owner/Need数量及来源/材质/required_by/issuer/policy变更与未来policy生效、revision/expiry；所有员工先锁、锁后fresh now、无T6嵌套锁。客户页limit/before/limit+1、空与503、过期未扫、无成本/原文递归泄漏；T5旧apply/历史恢复及旧审批目标回归。
- [ ] Commit `feat: 增加客户文件用途授权和安全版本发现`（显式git add本组文件，禁止git add .）。

### 8.2 中立Store、有界下载与新lazy writer

- [ ] 新增`tests/unit/test_quote_document_store.py`, `test_quote_pdf_object_transport.py`与`tests/integration/test_generated_artifact_bounded.py`。RED：

```python
async def test_actual_object_read_is_bounded(generated_bounded_case):
    c = generated_bounded_case
    with pytest.raises(ArtifactReadLimitExceeded):
        await c.store.get_bounded(c.tenant_id, c.artifact_id, maximum_bytes=c.limit)
    assert c.body.total_read <= c.meta.size_bytes + 1
    assert c.body.closed
```

- [ ] fixture真实metadata合法且size<limit，对象体比meta大；按T8A transport超限→ArtifactReadLimitExceeded契约，断言必须是**读取计数**而非事后len。另测meta超限零IO、短对象/同长坏hash、kind/MIME/tenant、缺bounded依赖、取消/慢流/close。
- [ ] Run `env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quote_document_store.py tests/unit/test_quote_pdf_object_transport.py tests/integration/test_generated_artifact_bounded.py -q`；GREEN只新增端口/adapter/新writer，不改旧get。
- [ ] 真实新S3代码注入受控SDK body/client，构造零secret/SDK、每次read(n)有限、一次put/delete、Config attempts=1、无multipart；SDK前/中/成功后取消/close错不冒称未写。真实PG Store commit成功后返回错误保留winner、unknown零delete、仅确认loser cleanup；旧Raw/EMAIL_DRAFT目标回归。
- [ ] Commit `feat: 为报价PDF增加中立有界存储和惰性写入`。

### 8.3 四manifest、三分支槽与正常文件链

- [ ] 新增`tests/unit/test_quote_file_gateway.py`、`tests/integration/test_quote_file_gateway.py`，真实ToolGateway/PG ledger+前两组真实服务。RED：

```python
async def test_cross_actor_duplicate_keeps_one_file(file_gateway_case):
    c = file_gateway_case
    a = await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    b = await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.manager_id)
    assert a == b
    assert c.renderer.calls == c.objects.put_calls == 1
```

- [ ] Run `env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quote_file_gateway.py tests/integration/test_quote_file_gateway.py -q`；GREEN固定manifest/strict params/checks/handlers/slot/HMAC/app；可先受控limiter跑本组，不冒充§8.4持久验收。
- [ ] 测key跨actor一致、HMAC真实key_version不被协议覆盖、变版本/协议冲突零生成、generated_by=template；独立read/history、二次撤权零bytes且不删产物、Gateway complete失败/取消清槽；跨task/重复put/错误分支拒绝、三个分支永不同存，repr/ledger无客户/PDF/source marker。
- [ ] Commit `feat: 增加报价文件Gateway与安全结果交接`。

### 8.4 持久预留与历史unknown保护

- [ ] 新增`tests/unit/test_quote_file_rate_limit.py`、`tests/integration/test_quote_file_rate_limit.py`；用真实独立PG连接/ToolGateway调用，不fake claim返回值。RED：

```python
async def test_over_limit_never_enters_render(rate_case):
    c = rate_case
    results = await c.invoke_distinct_quotes_concurrently(c.limits.maximum_admissions + 1)
    assert await c.count_reserved_events() == c.limits.maximum_admissions
    assert c.render_calls == c.limits.maximum_admissions
    assert sum(r.error_category is ToolErrorCategory.RATE_LIMITED for r in results) == 1
```

- [ ] Run `env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quote_file_rate_limit.py tests/integration/test_quote_file_rate_limit.py -q`；GREEN独立tenant预留SQL、真实canonical prepared注入、history reader及handler只恢复分支。
- [ ] 测reserved commit/close未知零render、不退款；窗口边界/N缩小/未来timestamp/锁后DB now/SQL超时、真实received≠canonical及actor审计、不按attempt免费复用、Retry-After含lease。unknown→reclaim→rate拒→category覆盖→后次>=2事件仍零render/put；0事件/错绑定拒绝。
- [ ] 真实EXECUTING过期仍IN_PROGRESS且零handler；CLAIMED/FAILED_TRANSIENT均须lease到期且retry_after满足才能重认领，仅原FAILED_TRANSIENT+RECONCILIATION_REQUIRED进hook并新计，其余execute仍受历史保护；retry_after未到零handler。延迟旧协程任何放行均有独立event，但不宣称fencing。技术lease以真实DB当前UTC建测试预算，报价业务now另设；不改append-only历史事件来操纵时钟。
- [ ] Commit `feat: 持久限制报价生成并保护未知执行历史`。

### 8.5 显式metadata恢复与安全错误ID

- [ ] 新增`tests/unit/test_quote_file_recovery.py`、`tests/integration/test_quote_file_gateway_recovery.py`；RED：

```python
async def test_recovery_links_without_changing_old_ledger(recovery_case):
    c = recovery_case
    before = await c.read_original_record_and_events()
    result = await c.app.reconcile(c.tenant_id, c.quote_id, c.original_call_id, actor_id=c.sales_id)
    assert result.outcome == 'metadata_recovered_original_unresolved'
    assert result.original_generation_call_id != result.recovery_call_id
    assert await c.read_original_record_and_events() == before
    assert await c.requested_event_exists(result.recovery_call_id, c.original_call_id)
    assert c.recovery_object_calls == 0
```

- [ ] Run `env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quote_file_recovery.py tests/integration/test_quote_file_gateway_recovery.py -q`；GREENmetadata-only handler、public ledger reader/audit、workflow wrappers，不能给恢复handler宽Store。
- [ ] 真PG测requested成功退出在record_file之前；append失败/commit未知/close错/取消零关联，audit_projection不当证据。旧线程artifact commit后暂停→恢复补关联→旧线程继续同file；并发恢复唯一、关联unknown再恢复；原线程最终完成前后at_check语义正确。
- [ ] 错old tenant/tool/version/key/HMAC/run/template/hash/size、无metadata、缺/伪preflight/newcall/user/status均失败；新NONE key不进Store。丢ID再generate保留真实旧canonical ID，CONFLICT新ID不冒原ID；新ledger完成失败无payload，无ack/旧call写或自动history fallback。
- [ ] Commit `feat: 显式找回已提交报价文件并保留未决原审计`。

### 8.6 整项验收与B2交付门

- [ ] 将上述unit/integration各组**全部**重跑，真实PG测试不能skip跳过后称通过；运行`python3 scripts/check_boundaries.py`及本次文件ruff/既有配置的类型检查。测试环境故障列阻断，不读/输出凭证或把受控SDK称live。
- [ ] 完成窄ADR/就近规则：rate_limit技术预留是写，MEDIUM/NONE恢复是内部幂等关联写；旧CheckStage“仅幂等写”注释偏差披露，不改核心；object_store新writer执行期才取秘密，旧constructor仍原事实。
- [ ] 提交文档/契约收口，报告真实命令/结果、skip数、未运行范围。一次完整T8B1审查覆盖所有提交与多连接测试；不逐提交另派review，不自行启动B2。

## 9. B2收到什么；剩余派发前核对

B2消费真实QuoteFileAccessService、ContextQuoteFileScopeAuthorizer、QuoteCustomerVersionsService、QuoteFilesApplication/技术wrappers、四manifest/register helper/check类、单slot、RateLimits/WriteLimits、Store及metadata-only adapters。B1不返回一个需要尚未构造Gateway的整体factory，B2按真实依赖自行有序装配；上下游均必填真实reader，缺依赖失败关闭。
B2负责唯一approvals实例/handler-before-engine/延迟run reader、实际S3 settings/limits/HMAC/lease owner注入、API/worker进程资源关闭与HTTP安全错误call ID投影；B1只提供构造端口与受控集成证明。T7三个限额由B2显式构造renderer；T8A bounded reader的对象读limits由B2配置，B1不重造parser/settings/probe。
B1不触真实发送；T4未装配QuoteSendReceiptReader的现实仍保持，下载不等于sent。客户历史读取不能被当前正式文件入口或裸bool代替。unknown孤立object、旧HMAC不可核、未提交metadata、无法定位oldcall等仍需核对，不承诺本插件解决所有unknown。

派发前仅四项真实对齐门，未满足应回控制器，不自行扩大范围：

1. T5/T6最终同域helper/文件receipt及run契约是否已提取；按§2目标同域复用并保持原T5 fresh/history语义，不能套假的executor。T6接口或三hash触发器尚未交付则不得跳过。
2. T8A实际BoundedObjectBlobTransport错误及S3实现是否接受已验证generated键、支持累计上限+哨兵与资源关闭；仅可做同目录窄复用，不退回旧get。§8.2具体异常断言按真实契约选择，不凭空引用类。
3. T7真实QuotePdfRenderer/错误/唯一模板已交付且合法投影可真实渲染；未交付只能保留RED/阻断，不能用受控bytes冒充整链renderer验收。
4. 为本次ADR分配未占号路径；保留原ToolCallId位置和四工具窄manifest裁定，不借实现增新迁移、全局stage或fencing。除此之外无新增业务裁定请求。
