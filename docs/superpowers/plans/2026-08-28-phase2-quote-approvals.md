# Task 5：报价单轮审批、当前授权与原子应用 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILLS: Use superpowers:test-driven-development and superpowers:verification-before-completion。此任务由主计划的SDD控制器派发，不另启动executing-plans批次或子代理。控制器已全文自检并补齐FX/真实Run契约；仅在核定T3B/T4最终接口并派发后实施。下列提交是TDD/自检边界，整项Task5交付后统一一次独立审查，不逐提交派review。

**Goal:** 将一个不可变报价版本的一轮独立审批安全提交、等待并原子应用，真实成功receipt可在重启后完成审批记账。
**Architecture:** quotations拥有安全业务payload、报价ABAC、状态/成功receipt；approvals拥有审批包/决定/请求幂等，通过注入的报价专用guard隔离新namespace。workflow显式转换并编排；context先锁全部员工，报价session持机会锁后取得costing公开政策租约，报价/outbox/receipt提交后再释放租约。
**Tech Stack:** Python 3.12+、Pydantic v2、SQLAlchemy 2.x、PostgreSQL、现有workflow engine/outbox；无新运行依赖。
**Spec:** `docs/superpowers/specs/2026-08-28-phase2-costing-quotation-design.md` §6/§7；主计划Task5；`2026-08-28-phase2-quote-freeze.md`与`2026-08-28-phase2-quotation-versions.md`。本子计划落实控制器裁定，不新增审批轮次或权限框架。

## Global Constraints

- 金额只用有限Decimal/Money，HTTP金额字符串；tenant强制过滤、复合FK；旧报价内容/旧basis/原始来源不改写。
- 每quote一轮审批；每quote/type至多一个approval。所有required_types精确绑定同quote/version/content/policy；quote_send必需，低于底线独立margin_floor_override，四类terms分别独立审批。
- 当前policy id/hash必须等于basis/submission；变化policy_stale，需新basis/quote/审批，不原位改价/改审批payload。
- boss租户、manager仅当前直属owner可decide/apply；自己起草/提交owner/当前owner均禁止自批，无老板豁免。read可读自己的起草/负责包，不因此授予decide或一般成本权限。
- 新quote namespace的get/list/decide须当前授权，缺依赖拒绝；legacy quote_send/邮件/其他审批保持原语义。只有严格namespace+payload同时匹配才进入新路径。
- 原件ACL独立：审批业务payload不含source_quote/source_url/locator/原件地址，不自动dump Need/basis/Provenance；完整内部对象不得进workflow context/outbox/日志。
- 不自动发邮件、不生成PDF、不建设多级或重提轮次、通用锁/事务/授权框架；不读凭证/生产来源、不push/merge/部署。
- 仅新建0045审批增量迁移；PDF迁移改0046由控制器维护。实施者不改主计划/ledger/批准规格。

## 0. 前置与精确文件

工作树`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-costing-quotation`。先读根AGENTS/HANDBOOK及domains/{approvals,quotations,costing}/shared/workflows/quote_approval/infra/apps/api/tests就近AGENTS。
**前置已核验：T3B至b1846ce、T4至b10d101均完成独立任务审查。** 真实0043/0044、QuotationVersionService/QuoteDetailView、context锁、冻结/完成reader已交付；本任务新增的approval context、policy selection公开租约和审批服务仍待实施，不能把本文作为它们已存在的证明。按派发context核对真实签名和旧测试构造；不重做已完成任务。
当前T4 §2.2已允许latest accepted/rejected且无active时，新cost sheet/scope/key、replaces=None开下一版本，旧终态不改；latest expired仍E2。本任务不恢复已否决包、不复用rejected报价的锁表。

| 文件 | 责任 |
| --- | --- |
| Create `domains/quotations/approval_schemas.py`、`approval_rules.py`、`approval_service.py` | 完整typed审批形状、安全payload/ABAC/hash、单用途session与原子效果 |
| Modify `domains/quotations/{service,service_impl,schemas,version_repository,errors}.py`；Verify existing `permissions.py` | 显式公共重导出、注入依赖、窄仓储；既有四角色准备用途保持，新审批矩阵唯一在approval_rules，不恢复旧bool审批入口 |
| Create `domains/approvals/quote_contract.py`；Modify `service.py`、`service_impl.py`、`schemas.py`、`models.py`、`repository.py`、`errors.py` | 新namespace检测/typed事实/请求hash、兼容旧接口、quote guard与原始limit |
| Create `domains/costing/approval_policy.py`；Modify `service.py` | 公开单用途selection lease，复用T3B政策选择和shared advisory锁 |
| Modify `domains/quotations/context.py`、`infra/db/quote_context.py` | 新access/full-approval投影；员工一次排序锁，原open行为不改 |
| Modify `infra/db/quotation_uow.py`、`repositories/quotations.py`、`repositories/approvals.py`、`tables.py`、`outbox.py` | 同session bus、binding/receipt/请求元数据、发布白名单 |
| Create `migrations/versions/0045_quote_approval_contracts.py` | 请求hash/limit/namespace唯一、binding唯一、成功receipt及对应不可变约束 |
| Create `workflows/quote_approval/approvals.py`、`policy_reader.py`、`run_reader.py`、`flow.py`、`steps.py`；Modify `application.py` | 显式域DTO适配、guard/真实run读取适配、真实流程；保留T3B/T4应用 |
| Modify `workflows/engine/runner.py`、`infra/db/workflow_engine.py` | WorkflowEngine公开get_run及真实tenant只读实现；复用WorkflowRun，不改audit HTTP或老板权限 |
| Modify `apps/api/routers/approvals.py`；Verify existing `apps/api/dependencies.py` | 现有read/list转新授权读取；decide仍真实当前身份。既有approvals:ApprovalService可选端口承载新wrapper，真实装配留T8，不写业务矩阵 |
| Create `tests/unit/test_quote_approval_contracts.py`、`test_quote_approval_workflow.py`；Modify `test_approval_service.py` | namespace/旧兼容、payload/hash、纯规则、流程 |
| Create `tests/integration/test_quote_approval_postgres.py`、`test_quote_approval_locks.py`、`test_quote_approval_migration.py`；Modify `test_migrations.py` | 真实包/quote/outbox/receipt、重启、多连接、0045 roundtrip |
| Modify `domains/{approvals,quotations,costing}/AGENTS.md`、`workflows/quote_approval/AGENTS.md`、`docs/adr/0018-costing-quotation-contracts.md` | 单轮/namespace/ABAC/锁/迁移与未装配边界，不放宽九条 |

## 1. 完整类型与纯函数

新增DTO strict/frozen/extra-forbid；序列tuple，金额WireDecimal/Money、时间aware→UTC、ID用shared NewType。`Hash`为64位小写hex。采用T4文本/精度限制；对新审批请求增加总JSON≤64000 bytes，与旧上限一致。只在唯一JSON适配边界把tuple变array/Decimal变string，不使用float。
以下类型在quotations.schemas显式导出；QuoteState/QuoteContentLine/QuoteProfitMetrics/QuotePolicySnapshot/QuoteDetailView/QuoteBusinessContext复用T4，不重复定义。

```text
QuoteApprovalType = Literal['quote_send','margin_floor_override','discount',
 'delivery_commitment','payment_terms','certification_commitment']
QuoteApprovalCustomerSummary:
 issuer_name:str;issuer_address:str;issuer_contact:str;account_name:str;country:str
 line:QuoteContentLine;valid_until:datetime;terms:tuple[QuoteTerm,...]
QuoteApprovalFxSummary:
 source_currency:str;target_currency:str;rate:WireDecimal;observed_at:datetime;reference_id:str
QuoteApprovalCalculationSummary:
 base_currency:str;quote_currency:str;effective_unit_revenue:Money
 displayed_unit_price:Money;displayed_total:Money;metrics:QuoteProfitMetrics
 cost_fx_rates:tuple[QuoteApprovalFxSummary,...];quote_fx:QuoteApprovalFxSummary|None
QuoteApprovalPolicySummary:
 policy_id:str;content_hash:Hash;category:str|None;minimum_margin_rate:WireDecimal
 target_margin_rate:WireDecimal;effective_from:datetime
QuoteApprovalEvidenceSummary:
 evidence_id:str;evidence_hash:Hash;kind:Literal['supplier_price','confirmed_expense']
 basis:Literal['quoted','actual'];amount:Money;valid_until:datetime|None
 confirmed_by:EmployeeId;confirmed_at:datetime
QuoteApprovalPreviousSummary:
 quote_id:QuoteId;version:int;content_hash:Hash;customer:QuoteApprovalCustomerSummary
 calculation:QuoteApprovalCalculationSummary;policy:QuoteApprovalPolicySummary
QuoteApprovalPackagePayload:
 schema_version:Literal['quote-approval-v1'];tenant_id:TenantId;quote_id:QuoteId;quote_version:int
 opportunity_id:OpportunityId;content_hash:Hash;context_hash:Hash;basis_id:str;basis_hash:Hash
 prepared_by:EmployeeId;submitted_owner_id:EmployeeId;approval_type:QuoteApprovalType
 required_types:tuple[QuoteApprovalType,...];policy:QuoteApprovalPolicySummary
 customer:QuoteApprovalCustomerSummary;calculation:QuoteApprovalCalculationSummary
 evidence:tuple[QuoteApprovalEvidenceSummary,...];previous:QuoteApprovalPreviousSummary|None
QuoteApprovalSnapshot:
 internal_quote:QuoteDetailView;required_types:tuple[QuoteApprovalType,...]
 payloads:tuple[QuoteApprovalPackagePayload,...];expires_at_limit:datetime
QuoteApprovalFact:
 tenant_id:TenantId;approval_id:ApprovalId;approval_type:QuoteApprovalType;change_set_ref:str
 request_hash:Hash;payload:QuoteApprovalPackagePayload;created_at:datetime;expires_at:datetime
 expires_at_limit:datetime;prepared_by:EmployeeId;submitted_owner_id:EmployeeId
 proposed_by_run:RunId|None;state:Literal['pending','approved','applied','apply_failed','rejected','expired']
 decision:Literal['approve','reject']|None;decided_by:EmployeeId|None;decided_at:datetime|None
 decision_note:str|None;applied_at:datetime|None;application_error_code:str|None
QuoteApprovalSubmission:
 tenant_id:TenantId;quote_id:QuoteId;quote_version:int;content_hash:Hash
 policy_id:str;policy_hash:Hash;required_types:tuple[QuoteApprovalType,...];facts:tuple[QuoteApprovalFact,...]
QuoteApprovalDecisionSnapshot: QuoteApprovalFact全部字段，去掉state/applied_at/application_error_code
QuoteApprovalApplicationReceipt:
 tenant_id:TenantId;quote_id:QuoteId;quote_version:int;content_hash:Hash;facts_hash:Hash
 decisions:tuple[QuoteApprovalDecisionSnapshot,...];applied_at:datetime;quote_send_decider:EmployeeId;approval_run_id:RunId
QuoteApprovalOutcome = Literal['waiting','approved','rejected','expired','blocked','obsolete','already_applied']
QuoteApprovalApplyResult:
 outcome:QuoteApprovalOutcome;quote:QuoteDetailView;receipt:QuoteApprovalApplicationReceipt|None
 error_code:QuoteApprovalErrorCode|None
QuoteApprovalPollResult:
 outcome:Literal['waiting','ready','rejected','expired','blocked','obsolete','already_applied']
 approval_ids:tuple[ApprovalId,...];deadline:datetime|None;error_code:QuoteApprovalErrorCode|None
QuoteWorkflowExecutor: workflow_type:Literal['quote_approval'];run_id:RunId;quote_id:QuoteId
QuoteWorkflowRunFact:
 tenant_id:TenantId;run_id:RunId;workflow_type:str;workflow_version:int;subject_ref:str
 quote_version:int;content_hash:Hash
QuoteApprovalAccessContext:
 tenant_id:TenantId;opportunity_id:OpportunityId;actor:QuoteEmployeeFact;owner:QuoteEmployeeFact
 prepared_by:EmployeeId;submitted_owner_id:EmployeeId
QuoteApprovalContext: business:QuoteBusinessContext;deciders:tuple[QuoteEmployeeFact,...]
QuoteApprovalAccessResult: can_decide:bool;current_role:QuoteEmployeeFact.role同一Literal
QuoteApprovalSubject:
 tenant_id:TenantId;approval_id:ApprovalId|None;quote_id:QuoteId;quote_version:int;content_hash:Hash
 opportunity_id:OpportunityId;prepared_by:EmployeeId;submitted_owner_id:EmployeeId;approval_type:QuoteApprovalType
```

QuoteWorkflowExecutor是受信handler从持久WorkflowRun构造的技术归属，不能从HTTP构造、不是EmployeeId、不授予decide；它本身不证明run存在。内部服务须经§3.4必填reader逐字段匹配真实tenant/quote/run绑定；外部入口只有经过当前员工判权的start/read/decide。QuoteWorkflowRunFact仅为中立只读事实，不含context或业务正文，不注册HTTP响应。
上述DecisionSnapshot是独立Pydantic类，不在运行时删除字段。fresh批准只接受全部fact.decision='approve'且state为approved/applied；有applied但无报价成功receipt属于storage_inconsistent，不借此补造成功。

公开纯函数（approval_rules.py实现，quotations.service导出）：

```text
quote_change_set_ref(quote_id:QuoteId,content_hash:Hash,approval_type:QuoteApprovalType)->str
required_quote_approvals(quote:QuoteDetailView)->tuple[QuoteApprovalType,...]
quote_approval_payloads(quote:QuoteDetailView,previous:QuoteDetailView|None)->tuple[QuoteApprovalPackagePayload,...]
parse_quote_approval_payload(value:dict[str,JsonValue])->QuoteApprovalPackagePayload
quote_approval_payload_hash(payload:QuoteApprovalPackagePayload)->Hash
quote_approval_facts_hash(facts:tuple[QuoteApprovalFact,...])->Hash
require_quote_approval_access(subject:QuoteApprovalSubject,context:QuoteApprovalAccessContext,
                             *,action:Literal['read','decide','apply'])->None
```

`JsonValue`使用Pydantic递归JSON值类型，不用Any业务DTO。change_set严格`quote:{quo_ULID}:{hash}:{六类type}`。required_types按上述Literal固定顺序、唯一；quote_send必有，margin依据冻结metrics.margin_rate < policy.minimum_margin_rate，terms按kind取集合，不靠模型判断；当前政策若不同会在锁内拒绝，而非采用旧底线冒称当前。
payload构造逐字段白名单；previous选该机会version-1实际历史记录，不仅replaces（T4终态后无replaces也有历史）；只保留上述安全商业信息。金额已算好，仅复制/格式化，不重算利润。source字段、供应商原文、scope适用性原文、整份Need/Provenance不入payload；证据确认摘要不等于原件读取授权。
实际FX必须可见（规格§5.2）：T3B FrozenCostBasis必填cost_fx_rates保存shared FxRate原值，T4 QuoteBasis等值携带，派发前核对，不从计算结果反推。成本FX按原tuple顺序映射base→source_currency、quote→target_currency，原rate/observed_at不变，reference_id=quote.basis.cost_sheet_id；独立quote.basis.quote_fx映射base_currency/quote_currency、原rate/observed_at，reference_id=fx_id，无quote FX时为None。空成本FX仅表示原冻结tuple为空，不补1:1默认值。两个FX摘要都不带source/source_ref/URL或确认人；尤其不得给shared FxRate伪造不存在的confirmed_by。previous使用自身历史basis，同样逐字段投影；payload/hash同时绑定全部FX，量纲沿customer.line数量/unit及metrics保留。
parse_quote_approval_payload从持久JSON使用JSON模式严格验证（含Money十进制字符串和UTC日期解析），不把Python字符串绕过WireDecimal规则强塞进Money；不接受额外字段或自动删字段以通过验证。
payload hash版本`quote-approval-payload-v1`；facts hash版本`quote-approval-facts-v1`：按approval_type固定顺序编码DecisionSnapshot全部字段，**排除state/applied_at/application_error_code**，绑定decision/真实decider/decided_at/note/原始request_hash/完整安全payload/limit等。APPROVED→APPLIED前后hash不变；任何批准人/时间/payload/原始limit变化必须不同。terms原顺序/重复项不变，None保留、Decimal无损规范化、UTC；不改旧quote/content/Need hash算法。

## 2. 新namespace审批契约与legacy兼容

### 2.1 approvals公开类型/端口

新增approvals.schemas类型，避免approvals导入quotation：

```text
ApprovalFactView:
 tenant_id:TenantId;approval_id:ApprovalId;approval_type:str;state:ApprovalState
 title:str;proposed_change:dict[str,JsonValue];reason:str;blast_radius:BlastRadius
 proposed_by_run:RunId|None;proposed_by_employee:EmployeeId|None;owner_employee:EmployeeId|None
 evidence_refs:tuple[str,...];change_set_ref:str|None;created_at:datetime;expires_at:datetime
 expires_at_limit:datetime|None;request_hash:Hash|None;contract_namespace:Literal['quote-approval-v1']|None
 decided_by_employee:EmployeeId|None;decided_at:datetime|None;decision_note:str|None
 applied_at:datetime|None;application_error_code:str|None
ApprovalQuoteSubject:
 tenant_id:TenantId;approval_id:ApprovalId|None;approval_type:str;change_set_ref:str
 quote_id:QuoteId;quote_version:int;content_hash:Hash;opportunity_id:OpportunityId
 prepared_by:EmployeeId;submitted_owner_id:EmployeeId
ApprovalAccessResult: can_decide:bool;current_role:QuoteEmployeeFact.role同一Literal
ApprovalReaderIdentity: employee_id:EmployeeId;role:QuoteEmployeeFact.role同一Literal
```

ApprovalFactView是内部事实DTO，不注册response_model；原ApprovalView字段/构造保持。BlastRadius在approvals.service原有公开导出，字段不改。

```python
class QuoteApprovalAccess(Protocol):  # approvals.service，由workflow适配quotation公开端口
    def subject(self, fact: ApprovalFactView) -> ApprovalQuoteSubject: ...
    def guard(self, subject: ApprovalQuoteSubject, *, actor_id: EmployeeId,
              action: Literal['read','decide']) -> AsyncContextManager[ApprovalAccessResult]: ...
# ApprovalService新增：
async def read_fact(tenant_id: TenantId, approval_id: ApprovalId) -> ApprovalFactView: ...
async def get_for_reader(tenant_id: TenantId, approval_id: ApprovalId,
                         *, reader: ApprovalReaderIdentity) -> ApprovalView: ...
async def list_for_reader(tenant_id: TenantId, *, reader: ApprovalReaderIdentity,
                          limit: int = 50) -> list[ApprovalView]: ...
# 原submit完整参数保持，仅末尾新增：expires_at_limit: datetime | None = None
# 原get(current_employee=None)、list_pending_for、decide、mark_applied/failed签名不破坏。
```

`ApprovalServiceImpl(...,quote_access:QuoteApprovalAccess|None=None,now=原接口)`允许旧测试/装配不传新依赖，但任何新namespace记录/请求必须有有效quote_access。新read_fact仅受信workflow使用，无HTTP，无客户来源原件；读取也要严格解码namespace/request元数据。
新read_fact须从实际持久字段重算submit request_hash并匹配，不只相信非空hash列；决定后的decider/decided_at/note是不可变决定事实，state可推进但这些值不得随mark_applied/failed改变。
`workflows/quote_approval/approvals.py`实现`QuotationApprovalAccess(quotations:QuotationVersionService)`：subject调用quotation纯parse并逐字段验证type/change_set/tenant/quote/hash/proposer/owner相合，再等值转换；guard调用quotation.open_approval_access，业务规则唯一在quotation。
get_for_reader/list_for_reader用于既有HTTP read/list：new按当前guard，可读自己起草/当前或提交时负责的包；legacy只向原boss/manager reader开放，保留旧展示/查询行为。reader.role来自可信RequestIdentity，不能从请求体/role header取；new路径还必须guard重读当前角色/在职，拒绝传入身份与当前事实不一致。guard结果的必填current_role来自租约内真实context.actor.role，workflow adapter原值映射；两个wrapper在租约内、HTTP投影前精确比较reader.role与current_role。角色不一致显式拒绝，不得被list的普通不可见候选过滤吞掉。结果只供内部使用，不是HTTP身份令牌；不新增expected_role参数链。旧get仅有current_employee时按真实当前权限判定，不虚构role。
RED须覆盖get及list：同一起草人从manager变为finance等两个均可读自身包的角色，使当前ABAC仍可读但陈旧reader.role必须被拒；保留真实PG角色变动/离职与租约顺序测试，不能只用角色本就无权访问的反例。
旧get若读取new且current_employee=None必须拒绝；有current_employee则同guard；旧list_pending_for遇new同样guard过滤，不绕过。worker改用read_fact，不把当前UI can_decide当批准事实。
router read/list去掉会误封新路径起草人的总boss/manager前置，改调get_for_reader/list_for_reader；legacy角色门由该服务新wrapper保持。decide仍只有boss/manager第一道角色门，new服务guard为第二道。没有新角色审批权。

### 2.2 namespace检测与跨状态请求幂等

`quote_contract.py`先识别标记再严格验证：change_set看起来使用quote:前缀（含去空白/casefold后的伪装），或payload.schema_version看起来使用quote-approval前缀，任一个出现即**不得回退legacy**；原始字符串必须精确合法、两个标记齐全、六类type和全部关联一致，否则`quote_contract_invalid`。不是看到approval_type=quote_send就切新路径。
新请求解析payload后再submit；legacy无标记按原逻辑，包括旧邮件quote_send。request_hash版本`quote-approval-submit-v1`绑定tenant/type/title/完整安全proposed_change/reason/blast_radius/实际proposed_by_run+employee/owner/evidence_refs原顺序/change_set_ref/**原expires_at_limit（含None）**；不含生成ID/created_at/now。
新请求必须明确expires_at_limit且等于payload.customer.valid_until与适用依据有效期的最早值（quotation生成；审批域只验证形状/持久输入）。第一次created_at取取得change_set锁后的UTC now，有效期=min(created_at+原类型DEFAULT_VALIDITY,limit)，limit>created_at；默认天数仍沿用现有表及未列类型3天，不人为延长。
新namespace持tenant+change_set事务advisory锁（命名`approval-quote-submit-v1`）→跨状态查既有→同request_hash返回原ID，异载荷或原limit不同冲突，即使两limit落到相同有效expires_at也冲突。重放先查winner，不以retry-now验证旧limit已过期；既有有效期只按其created_at核算，不更新旧记录。
并发首建靠DB新namespace跨状态唯一兜底；不得捕获IntegrityError直接返回成功。legacy保留pending-only幂等及原默认参数行为；可选limit对新采用该参数的legacy调用只缩短首次期限，不改变其既有同pending返回语义。

### 2.3 当前read/decide guard及决定事务

纯read规则：当前active同tenant且（boss，或manager且owner.is_active且owner.manager_id==actor.employee_id，或actor==prepared_by/submitted_owner_id/current_owner）。最后一项只给读权；其他角色不凭空获得范围。纯decide/apply：active boss或当前active直属owner的manager，且actor不等于prepared_by、submitted_owner_id、current_owner。不存在“老板自批”豁免。
审批服务new decide先短读不可变subject→进入quote_access.guard（员工→机会）→自己的approval row FOR UPDATE→重读subject/状态/新now→决定+ApprovalDecided同审批UoW提交→退出guard。不能先锁approval行再等员工/机会。对每次get/read返回，在guard内投影；can_decide由同一纯规则计算，read自己的包不触发自批拒读。
new列表不能复用“先SQL排proposer/owner”的旧候选查询，否则读自己包永远缺失。仓储新增按expires_at/approval_id稳定游标的pending候选读取；逐页guard筛选到limit或本次扫描结束（created_at<=scan_started_at），拒权跳过但损坏契约/依赖缺失失败关闭。legacy仍用原筛选，不扩大旧包可见性。
read/decide guard只保护历史quote关联/当前员工与机会owner，**不读取Need单位/最新issuer/policy**；旧quote因数量变化或来源不可读不应使用户无法查看历史审批。decide要求真实绑定的pending报价；revision竞争后晚到决定最多成为旧包记录，apply不能恢复旧quote。

## 3. Context、政策lease和报价专用session

### 3.1 公共context补口（quotations.service）

```python
class QuoteContextProvider(Protocol):  # 原open保留
    def open_approval_access(self, tenant_id: TenantId, opportunity_id: OpportunityId,
        actor_id: EmployeeId, *, prepared_by: EmployeeId, submitted_owner_id: EmployeeId
        ) -> AsyncContextManager[QuoteApprovalAccessContext]: ...
    def open_for_approval(self, tenant_id: TenantId, opportunity_id: OpportunityId,
        actor_id: EmployeeId, *, prepared_by: EmployeeId, decider_ids: tuple[EmployeeId,...]
        ) -> AsyncContextManager[QuoteApprovalContext]: ...
```

复用T3B bootstrap/同session投影和员工排序实现，不复制第二套锁算法。access方法只锁所需Employee→Opportunity，owner重读失配退出；不查Need/issuer，不调用四成本角色policy。
full方法锁前从真实facts获得全decider_ids；actor_id必须是该组quote_send真实decider。将actor/owner/存在的preparer/全deciders排序去重一次Employee FOR SHARE→Opportunity FOR SHARE→Need FOR SHARE。返回deciders完整同tenant事实；每个decider当前active/role/直属关系在报价域分别校验，business.runtime.current_actor只是quote_send决策人，不代替全组。
锁内重读facts若发现decider集合、owner关联变化，context_changed退出，不在Opportunity之后补员工锁。全部lease持至报价应用/事件/receipt提交后。额外deciders不进入原context_hash；原prepared_by保留，历史起草人不必仍active；禁止自批比较仍用原ID。

### 3.2 当前政策公开selection lease

costing.service新增独立`CostingApprovalPolicyReader`，不更改CostingQuoteService.get_policy权限：

```python
class CostingPolicySelection(Protocol):
    async def current(self) -> PricingPolicyView: ...
class CostingApprovalPolicyReader(Protocol):
    def open(self, tenant_id: TenantId, category: str | None
             ) -> AsyncContextManager[CostingPolicySelection]: ...
# quotations.service本地端口：
class QuotePolicySelection(Protocol):
    async def current(self) -> QuotePolicySnapshot: ...
class QuoteApprovalPolicyReader(Protocol):
    def open(self, tenant_id: TenantId, category: str | None
             ) -> AsyncContextManager[QuotePolicySelection]: ...
```

这是已授权报价审批编排专用内部reader，无HTTP入口，不制造system CostingActor。costing实现持自己UoW/T3B同tenant policy shared advisory lock，current使用**本次调用新now**和既有选择算法；关闭才释放锁，不能只锁历史policy行。未来生效政策需按调用时间重新选择。
`policy_reader.py`里的`CostingQuoteApprovalPolicyReader(costing:CostingApprovalPolicyReader)`只显式转DTO；不在infra重写政策选择。构造clock/timeout显式，未装配dependency_unavailable。

### 3.3 QuotationVersionService新增入口及session

```python
async def approval_snapshot(tenant_id:TenantId,quote_id:QuoteId,*,actor:QuotationActor)->QuoteApprovalSnapshot: ...
async def approval_target(tenant_id:TenantId,quote_id:QuoteId,*,executor:QuoteWorkflowExecutor)->QuoteApprovalSnapshot: ...
def open_approval_access(tenant_id:TenantId,subject:QuoteApprovalSubject,*,actor_id:EmployeeId,
    action:Literal['read','decide'])->AsyncContextManager[QuoteApprovalAccessResult]: ...
def open_approval(tenant_id:TenantId,quote_id:QuoteId,*,executor:QuoteWorkflowExecutor
    )->AsyncContextManager[QuoteApprovalSession]: ...
async def get_approval_application(tenant_id:TenantId,quote_id:QuoteId,
    *,executor:QuoteWorkflowExecutor)->QuoteApprovalApplicationReceipt|None: ...
class QuoteApprovalSession(Protocol):
    async def snapshot(self)->QuoteApprovalSnapshot: ...
    async def submission(self)->QuoteApprovalSubmission|None: ...
    async def receipt(self)->QuoteApprovalApplicationReceipt|None: ...
    async def prepare_submission(self,context:QuoteBusinessContext,*,actor:QuotationActor)->QuoteApprovalSnapshot: ...
    async def bind(self,submission:QuoteApprovalSubmission,context:QuoteBusinessContext,
                   *,actor:QuotationActor)->QuoteDetailView: ...
    async def recover_submission(self,submission:QuoteApprovalSubmission,
                                 *,actor:QuotationActor)->QuoteDetailView: ...
    async def apply(self,facts:tuple[QuoteApprovalFact,...],context:QuoteApprovalContext)->QuoteApprovalApplyResult: ...
    async def terminate(self,facts:tuple[QuoteApprovalFact,...])->QuoteApprovalApplyResult: ...
```

QuoteApprovalAccessResult与approvals本地ApprovalAccessResult均含can_decide和必填current_role，adapter显式逐值转换，不跨域导入；current_role与ApprovalReaderIdentity.role使用同一Literal值域。
approval_snapshot只给当前四成本角色；approval_target/get_approval_application是受信本workflow专用内部读取，不是对manager开放一般get，不把executor冒成员工。open_approval_access验证quote不可变身份、payload关联和（decide时）唯一真实binding，再调用context access与唯一纯规则；租约内从真实context.actor.role产生current_role，返回无成本的权限结果，不能由调用者声称的角色填充。
QuotationServiceImpl新增必填context_provider/approval_policy_reader/workflow_run_reader:QuoteWorkflowRunReader；创建路径仍用T4原规则。approval_service.py实现上述session，服务门面只委托同实现；不要求实现者沿用旧bind_approval单包自动转态或旧apply bool API。
session进入T4报价UoW，bootstrap本域quote获取不可变opportunity_id→同`quotation-create-v1`机会advisory→quote FOR UPDATE；没有Opportunity写锁。snapshot/submission/receipt只是本session真实读取。
prepare_submission/bind/apply才懒打开政策lease；顺序**外层context→报价机会锁→policy shared**。全部锁取得后调用selection.current与now，要求policy id/hash==basis/submission、完整context仍匹配、quote/依据未过期；prepare/bind还检查当前四角色提交权。apply检查所有deciders与全部绑定事实，不复用四角色gate。
**退出顺序明确**：正常退出先commit报价UoW（state/bindings/receipt/outbox一并），再关闭policy lease，最后由调用方退出context；异常/取消先rollback报价再关policy。不要用普通“内层policy with先退出、外层quote with后commit”的写法提前释放政策锁。receipt恢复只读session不打开policy/context。
bind验证required集合、每种type恰一真实包、精确payload/hash/owner/限期/policy，全部bindings+draft→pending_approval+事件同事务。若已绑定同一集合，幂等返回；异集合冲突；一轮结束不得替换包。创建包与绑定之间可崩溃，原namespace幂等恢复，不自动新建另一个包。
全组原包已持久但bindings事务失败时，使用独立`recover_submission`：仍校验当前四角色、真实run、完整原组及精确不可变请求绑定，不取fresh Need/issuer/policy。仅补原组关联并在同一报价事务中调用terminate；draft可先进入pending后立即expired/rejected，已expired/rejected/superseded等历史状态不得复活。未终止且仍pending的恢复不表示批准，后续apply仍执行全部fresh门。无/部分原组不得走此入口或补建过期包。
apply首先读真实成功receipt；有则同facts_hash返回already_applied，不重发事件。无则仅pending_approval可fresh批准：完整context/当前policy/第二道报价依据门禁、全部包approved且未过期、全decider授权通过→quote approved+状态事件+QuoteApproved+成功receipt一次提交。QuoteApproved.approved_by=quote_send真实decider，其他信息查receipt；事件不是授权凭证。
terminate不要求Need或policy仍可用，只按真实绑定facts和新now关闭当前draft/pending_approval：任一reject→rejected，任一包到期→expired；不改内容/valid_until，不重新开包。已superseded/accepted/rejected/expired返回obsolete/对应终止结果不改历史；已有成功receipt优先返回already_applied。pending未到期等待，不mark_apply_failed。

### 3.4 真实workflow绑定读口（本任务新增，不是现有接口）

现状：runner.py:134的WorkflowEngine没有get_run，find_active_run只返回活动ID；audit.py:51/128的安全视图缺quote_version/content_hash，audit.py:166/214的get_run又是boss-only。不能用FK、executor自述或伪造boss代替绑定校验，也不扩审计DTO/权限。

```python
# domains/quotations/approval_service.py声明，quotations.service显式导出；Fact从schemas导出
class QuoteWorkflowRunReader(Protocol):
    async def read(self, tenant_id: TenantId, run_id: RunId) -> QuoteWorkflowRunFact | None: ...
# workflows/engine/runner.py的既有WorkflowEngine新增，WorkflowRun复用该模块既有公开类型
async def get_run(self, tenant_id: TenantId, run_id: RunId) -> WorkflowRun | None: ...
# workflows/quote_approval/run_reader.py；延迟取已装配engine，解决engine→handlers→service→reader构造环
class WorkflowQuoteRunReader:
    def __init__(self, engine: Callable[[], WorkflowEngine | None]) -> None: ...
    async def read(self, tenant_id: TenantId, run_id: RunId) -> QuoteWorkflowRunFact | None: ...
```

PostgresWorkflowEngine.get_run只做显式tenant+run_id的plain SELECT、映射独立WorkflowRun快照后关闭session；含终态run，不用find_active_run，不FOR SHARE/UPDATE、不沿用调用方事务。可复用本实现既有_row_to_run，workflow adapter不得调用该私有方法/仓储。公开导入路径是workflows.engine.runner，无需增加另一套audit/repository出口；quotations不导入workflow/infra。
adapter仅调用engine.get_run，逐字段投影元数据与持久context的两个固定键`quote_version`（正整数，拒bool）/`content_hash`（Hash）；任何缺失/非法值抛workflow_binding_invalid，未找到原样返回None；不透传context、正文、原始异常。engine尚未装配返回dependency_unavailable。T5受控/真实DB构造及T8最终装配都按reader闭包→quotation service→application/handlers→PostgresWorkflowEngine→闭包发布engine顺序，在最后一步前禁止启动worker/对外服务；不是默认engine/actor或可由请求替换的依赖。
统一绑定检查在quotation approval_service中，approval_target/open_approval/get_approval_application都必须调用，不在infra复制：fact tenant/run与入参相等，type='quote_approval'、workflow_version=1、subject_ref=str(quote_id)、quote_version/content_hash与真实不可变quote相等，executor.quote_id也相等；缺失、跨tenant、错type/subject/version/hash均workflow_binding_invalid。session入口核验一次供其所有方法复用；bind再要求每个包proposed_by_run等于该真实run，receipt恢复再核approval_run_id等于该run。对fresh及历史均校验绑定，但**不要求run仍running**，不因历史Need/policy/员工变化阻止receipt恢复或稳定run引用读取。
get_run是仅受信workflow使用的内部公共读口，完整WorkflowRun不进入quotation/HTTP；不是一般审计授权旁路。engine执行handler时已持run行锁（infra/db/workflow_engine.py:472/607等），独立读必须用MVCC读取先前已提交的initial_context，不能自等该锁；quote_version/content_hash在start原子持久，所有本流程step patch不得改这两个键。

真实PG已复现：plain get_run虽能读，独立报价事务写成功receipt时，真实run外键需要KEY SHARE，仍与外层handler持有的FOR UPDATE冲突。仅将poll_due与deliver_event两个跨handler调用的Run锁改为FOR NO KEY UPDATE（SQLAlchemy `with_for_update(key_share=True)`，不加read=True）；step锁及step→run顺序不变，cancel/失败收尾的无handler Run锁保持原样。引擎执行只更新非键状态/context，不修改run_id、tenant_id、idempotency_key。保留0045真实FK与报价state/receipt/outbox原子提交，不特判quote工具、不跨域共享事务或移出receipt。依据：[PostgreSQL16锁模式](https://www.postgresql.org/docs/16/explicit-locking.html)、[SQLAlchemy锁参数](https://docs.sqlalchemy.org/en/20/core/selectable.html#sqlalchemy.sql.expression.GenerativeSelect.with_for_update)。

## 4. 0045与窄仓储

0045 down_revision=真实0044；不改旧迁移。没有审批新数据时upgrade→downgrade→upgrade；有新namespace/receipt时downgrade明确拒绝，不能删除事实以降级。

- approval_packages加`contract_namespace nullable('quote-approval-v1')`、`request_hash nullable char64`、`expires_at_limit nullable timestamptz`。新namespace三者必需；精确payload/schema/change_set/type匹配CHECK，marker存在但namespace=NULL不得绕过。legacy三者可空，允许显式新limit的legacy首次提交保留limit。
- 保留旧pending-only唯一；新增`UNIQUE(tenant_id,change_set_ref) WHERE contract_namespace='quote-approval-v1'`跨状态唯一。迁移发现已有无法证明原limit/hash的新标记行应中止报告，不伪造回填。
- 更新approval不可变trigger，将三新列纳入不可变比较；新namespace从pending首次决定后，decided_by/decided_at/decision_note也冻结，approved→applied/apply_failed只能补对应应用字段。其余legacy允许状态边保持，不增加applied→approved等回退。
- quotation_approval_bindings新增/约束`UNIQUE(tenant_id,quote_id,approval_type)`；已有PK/FK保留。补持久request_hash/payload_hash及binding必要的安全fact快照；只能绑定同quote/version/content/policy和精确namespace包。只增，不UPDATE替换过期包。
- 新`quotation_approval_receipts`：PK tenant+quote_id；quote_version/content_hash/facts_hash/applied_at/quote_send_decider/approval_run_id和完整DecisionSnapshot tuple JSONB；报价/员工/真实workflow_run复合FK。每个receipt决策ID须属于该quote唯一bindings；只增不可改/删。receipt与approved状态事件/outbox同UoW，不能用审批表APPLIED反推伪成功。
- QuoteApproved加入infra/db/outbox.py导入、EVENT_REGISTRY及安全形状/roundtrip测试；不改共享事件字段，不让事件携带payload/原文。quotation UoW增加同session PostgresEventBus。

quotation version_repository新增async方法（均首参数tenant_id:TenantId，全部同UoW/session）：

```text
approval_bindings(tenant_id,quote_id:QuoteId)->tuple[QuoteApprovalFact,...]
add_approval_bindings(tenant_id,submission:QuoteApprovalSubmission)->None
approval_receipt(tenant_id,quote_id:QuoteId)->QuoteApprovalApplicationReceipt|None
add_approval_receipt(tenant_id,receipt:QuoteApprovalApplicationReceipt)->None
```

binding持久快照为提交时事实，state可pending；新决定事实由workflow每次read_fact再传，不能以binding中的旧state假定批准。SQL负责真实FK/绑定一致，业务ABAC与依据规则不写infra。
approvals.repository新增async`lock_quote_change_set(tenant_id,change_set_ref:str)->None`、`find_quote_by_change_set(tenant_id,change_set_ref:str)->ApprovalPackage|None`（跨状态，内部模型仅本域使用）；get/read_fact映射新元数据。新列表候选`list_quote_pending_candidates(tenant_id,*,scan_started_at:datetime,after:tuple[datetime,ApprovalId]|None,limit:int)->tuple[ApprovalPackage,...]`只含严格新namespace，tenant-filtered排序分页，返回空即结束。
旧ApprovalPackage内部模型增加新字段尾部默认None以保持旧构造；相应models.py需列为Modify。quote_request_hash/limit比较在approvals域纯函数，仓储不决定旧/新业务幂等语义。

## 5. 真实workflow编排与恢复

`workflows/quote_approval/approvals.py`还实现`read_quote_facts(approvals:ApprovalService,tenant_id:TenantId,approval_ids:tuple[ApprovalId,...])->tuple[QuoteApprovalFact,...]`：只从read_fact取真实包，严格payload解析/域DTO转换；event只有approval_id唤醒作用。
`QuoteApprovalApplication(quotations:QuotationVersionService,approvals:ApprovalService,context_provider:QuoteContextProvider,actors:QuotationActorReader,*,now:Callable[[],datetime])`新增在application.py，方法：

```text
submit(tenant_id:TenantId,quote_id:QuoteId,*,initiated_by:EmployeeId,executor:QuoteWorkflowExecutor)->QuoteApprovalSubmission
poll(tenant_id:TenantId,quote_id:QuoteId,*,executor:QuoteWorkflowExecutor)->QuoteApprovalPollResult
apply(tenant_id:TenantId,quote_id:QuoteId,*,executor:QuoteWorkflowExecutor)->QuoteApprovalApplyResult
mark_completed(tenant_id:TenantId,quote_id:QuoteId,*,executor:QuoteWorkflowExecutor)->QuoteApprovalApplicationReceipt
```

submit：真实snapshot→当前initiated_by四角色→T3B原context.open（prepared_by用quote原起草人）→open_approval→prepare_submission（选当前policy）→逐required_type调用真实approvals.submit→read_fact→bind。proposed_by_employee=quote.prepared_by，owner_employee=quote.owner_id提交快照，proposed_by_run=真实executor.run_id；initiated_by另在workflow记录，不冒称起草人改名。title/reason/blast_radius由固定模板+quote ID/type构造，不携原文；evidence_refs只应用内`quote-evidence:{quote_id}:{evidence_id}`引用，不是对象存储/原件URL。
submit使用同一expires_at_limit（quote/依据最早值），不是approvals当前剩余期限；每包created_at及类型默认值由approvals服务计算。已有bindings直接核集合返回，不以当前时间重建payload；部分包已提交则跨状态同namespace/hash恢复；若某包在绑定前被决定/到期，仍精确绑定原组后立即进入终止/应用判断，不生成第二轮。
审批公开内部读口新增`find_quote_fact(tenant_id:TenantId,change_set_ref:str)->ApprovalFactView|None`，只接受精确新namespace，复用既有仓储find_quote_by_change_set及完整_fact校验；不注册HTTP、不改变旧get_by_change_set权限。无bindings时先按不可变snapshot定位全部原包；只有完整组已存在，才按原固定请求逐包调用submit重放精确request_hash/limit并核返回原ID，再读真实facts交recover_submission。原包只增不可删除，重放不新建；无/部分组仍须原fresh准备，过期不能补包。缺依赖/损坏/跨tenant/不同原请求保持固定失败。
poll从真实bindings读取全部facts，先看成功receipt；无receipt时有拒绝/到期则session.terminate；未齐/仍pending返回waiting；全部approved返回ready，仅表示可进入fresh apply，绝非报价已批准。deadline取真实包最早expires_at，未有包为None；返回QuoteApprovalPollResult，不返回包含原文的内部quote。
apply先查真实成功receipt，存在即mark_completed后already_applied，不进context/policy。否则读全部facts；pending/终止按poll分类。全部approved才取quote_send.decided_by为context actor，收集所有deciders→open_for_approval→open_approval→重读facts确认同ID/immutable决定集合→session.apply→quote提交→退出所有lease→mark_completed。
mark_completed只读真实receipt，核其quote/hash/精确bindings/decisions，从approvals读现事实并比排除state后的facts_hash；逐包调用mark_applied，稳定key=`quote-apply:{quote_id}:{content_hash}:{approval_type}`。APPROVED/APPLIED混合重启逐个补完；不可用事件或手写quote_id生成receipt。之后Need/issuer/policy/在职变化不阻历史补记，但不再次准许客户文件。
后续文件契约：receipt.approval_run_id固定为本轮真实quote_approval WorkflowRun.run_id，经§3.4读口校验，须与全部新包proposed_by_run及首次成功executor.run_id相同并有tenant复合FK；FK只证存在，不代替type/subject/version/hash校验。同quote成功只一条receipt，重启不改该ID。get_approval_application仍供本任务内部真实workflow查询；T6公开get_file_approval按quote和当前文件actor读取稳定run，T8消费后者，避免必须先知道executor才能发现run的循环。run已completed不抹去归属；不每次手动生成随机run、不伪造执行历史。本任务只提供该查询事实，不实现文件生成/未知提交恢复。
fresh确定性阻断需要mark_apply_failed时：重新开quote-only open_approval并先查receipt；若已成功改走mark_completed，不标失败。无receipt且仍该轮，持同报价机会锁期间仅对真实state=approved包调用mark_apply_failed固定code，防另一个成功apply与迟到失败标记交错。此路径不取新员工/Need锁、不调用guarded UI get；approvals.mark_apply_failed只锁自身包。状态pending/rejected/expired/applied不能传入mark_apply_failed。transient/timeout/storage_unknown不转永久失败，保留可恢复，禁止盲目重试到批准。

workflow公共构造（复用现有engine Protocol）：

```text
build_quote_approval_definition()->WorkflowDefinition
build_quote_approval_handlers(application:QuoteApprovalApplication,notifier:QuoteApprovalNotifier)->Mapping[str,StepHandler]
register_quote_approval(engine:WorkflowEngine,registry:OutboxHandlerRegistry,approvals:ApprovalService)->None
start_quote_approval(engine:WorkflowEngine,quotations:QuotationVersionService,tenant_id:TenantId,
                    quote_id:QuoteId,*,actor:QuotationActor)->RunId
QuoteApprovalNotifier.notify(tenant_id:TenantId,quote_id:QuoteId,*,run_id:RunId,
                            recipient_id:EmployeeId,outcome:QuoteApprovalOutcome,idempotency_key:str)->None  # async
```

OutboxHandlerRegistry复用现有register_handler(event_type,handler_name,handler)结构Protocol，不import别的workflow内部。start先当前四角色approval_snapshot，engine.start类型quote_approval/version1、subject=quote_id、key=`quote-approval:{tenant}:{quote_id}:{content_hash}`；initial_context固定保存`quote_id`、`quote_version`、`content_hash`、`prepared_by`、`initiated_by`，后续仅追加run关联、approval ID/type、deadline/固定状态码，金额/全文不存run。engine.start返回后构造QuoteWorkflowExecutor并调用quotations.approval_target，经统一reader核绑定才返回run_id（同key复用也核），禁止依赖engine.start仅返回ID即认定内容相同。
flow步骤assemble→submit→wait→apply→mark_applied→notify→complete；wait的run_on_entry=True，先poll防事件先到；ready→apply，waiting保持wait，拒绝/过期/obsolete→notify终止，blocked→固定失败。wait deadline为真实包最早expires_at；timeout执行poll/terminate，不能批准。apply返回already_applied同样进mark_applied幂等收尾。
ApprovalDecided handler调用read_fact解析严格namespace，仅匹配本quote/type/hash和已有run；不因event.decision或decided_by直接构造fact，不为旧版本唤醒新quote run。事件payload仅ID/固定状态；step每次从真实服务重读。notify经受控Protocol、固定幂等key，只传metadata；实际通知适配T8复用现有出口，不新增渠道/外发邮件。

## 6. 固定错误

`QuoteApprovalError(ValidationError)`构造仅`code:QuoteApprovalErrorCode`：
`invalid_input,quote_contract_invalid,idempotency_conflict,approval_binding_conflict,approval_round_closed,approval_fact_invalid,approval_expired,context_changed,policy_stale,evidence_invalid,evidence_expired,decider_invalid,quote_inactive,workflow_binding_invalid`。
权限类`QuoteApprovalPermissionError(PermissionDenied)`仅permission_denied；不可用`QuoteApprovalUnavailableError(TradeOSError)`仅dependency_unavailable/lock_timeout/storage_unknown/storage_inconsistent。原T3B/T4固定code保持，不泄SQL/DSN/原文。
新approvals契约错误固定`quote_contract_invalid,quote_request_conflict,quote_limit_invalid`；不把格式错误当legacy fallback。旧异常类/消息行为不全局重写。
workflow_binding_invalid为技术归属失败，不写成功receipt、不mark_apply_failed；reader底层不可用映射dependency_unavailable/lock_timeout/storage_unknown，不暴露SQL、其他租户存在性或run.context。
仅向现`_SAFE_APPLICATION_ERROR_CODES`追加：`QUOTE_APPROVAL_FACT_INVALID,QUOTE_APPROVAL_CONTEXT_CHANGED,QUOTE_APPROVAL_POLICY_STALE,QUOTE_APPROVAL_EVIDENCE_INVALID,QUOTE_APPROVAL_EVIDENCE_EXPIRED,QUOTE_APPROVAL_DECIDER_INVALID,QUOTE_APPROVAL_EXPIRED`。上层对应固定业务错误一一映射；其他错误不把任意异常文本送mark_apply_failed。policy_missing按policy_stale固定阻断，不用默认政策。

## 7. TDD提交切片（统一最终独立审查）

### 7.1 纯契约、安全payload与namespace

- [ ] RED在test_quote_approval_contracts.py写typed形状、精确namespace及局部标记fail-closed、六type绑定、白名单/64KB、批准state变化hash稳定；保留旧ApprovalView构造。
- [ ] RED安全FX：多币种成本FX保持base/quote/rate/observed_at和tuple顺序，reference_id均为对应cost_sheet_id；quote FX为fx_id、无FX为None；previous使用历史FX。源source含URL/敏感文本不出现在任何payload，成本FX无confirmed_by；改任一FX数值/时间/引用改变payload hash，缺上游cost_fx_rates拒绝而非补默认值。

```python
def test_applied_state_does_not_change_decision_facts_hash(approved_facts):
    applied = tuple(f.model_copy(update={'state':'applied','applied_at':f.decided_at}) for f in approved_facts)
    assert quote_approval_facts_hash(applied) == quote_approval_facts_hash(approved_facts)
    changed = (approved_facts[0].model_copy(update={'decision_note':'different'}), *approved_facts[1:])
    assert quote_approval_facts_hash(changed) != quote_approval_facts_hash(approved_facts)
```

- [ ] RED：`python3 -m pytest tests/unit/test_quote_approval_contracts.py tests/unit/test_approval_service.py -q`。
- [ ] GREEN实现§1/§2纯类/函数、公开导出；fixture approved_facts用完整显式安全payload和真实格式ID，不能省字段/假来源。参数化遍历每个变更字段、raw泄露键、旧quote_send无标记仍legacy。
- [ ] 通过目标单测后提交`feat: 定义报价单轮审批契约与安全业务载荷`。

### 7.2 approvals持久请求兼容、0045与当前guard

- [ ] RED真实DB测原limit持久、samekey跨approved/applied恢复、同effective期限但不同原limit冲突、重试now漂移不冲突、两个首次submit并发只有一包；旧pending-only/邮件quote_send默认期限不变。
- [ ] RED quote-specific read/list/decide：老板、当前直属manager、跨辖区manager、自己起草/当前及提交owner、自批boss、失活/角色变化、无guard、半个namespace；own read不等于decide，旧包不扩可见性。历史Need.unit失效仍能读包。
- [ ] RED：`env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_approval_service.py tests/integration/test_quote_approval_migration.py tests/integration/test_quote_approval_postgres.py -q`。
- [ ] GREEN新namespace/请求元数据、guard适配、0045/唯一FK/不可变trigger；read/list wrapper接现router。审批guard必须包围决定commit，now在行锁后。实际SQL测跨tenant、重复binding/type、UPDATE/DELETE、空数据库roundtrip、有新数据降级拒绝。
- [ ] 提交`feat: 隔离新版报价审批请求与当前读取决定授权`。

### 7.3 全员工context、政策selection lease与原子报价效果

- [ ] RED多连接：两个及以上deciders停用/改manager被SHARE阻塞；ID输入顺序相反不死锁；owner更换/集合新增退出重取，不补O→Employee锁。受控source reader断言锁内零外部调用。

```python
async def test_all_deciders_stay_guarded_until_quote_commit(approval_case):
    async with approval_case.pause_apply_before_commit() as run:
        change = await approval_case.start_employee_deactivation(run.second_decider)
        assert await approval_case.is_waiting_for_employee_lock(change)
        await run.allow_commit()
    await change
    assert await approval_case.count_success_receipts() == 1
    assert await approval_case.count_quote_approved_events() == 1
```

- [ ] RED政策插入/将生效政策、报价锁等待后到期、context变化、旧版本晚到、不同decider权限；选择锁须持到quote commit，不提前退出。deny/expire不能留下approved事件或成功receipt。
- [ ] RED：`env -u TEST_DATABASE_URL python3 -m pytest tests/integration/test_quote_approval_locks.py tests/integration/test_quote_approval_postgres.py -q`。
- [ ] GREEN复用T3B投影/政策算法，单用途approval session与同session bus；只首次apply写quote/state-event/QuoteApproved/receipt；强制故障点分别在每步写入后，均整体rollback。
- [ ] 提交`feat: 原子应用报价审批并保护全部决策人与当前政策`。

### 7.4 workflow真实重启、部分提交与成功恢复

- [ ] RED真实engine：逐包submit中断、全部包已存但bind未提交、事件在wait前、wait期间进程重启、多个包先后决定、包拒绝/到期一轮关闭、quote被新版本替代后晚到事件只终止旧run。
- [ ] RED真实DB run绑定：get_run存在/不存在/跨tenant；已有真实run但type、workflow_version、subject、quote_version、content_hash分别错误，两个context键缺失/格式错，伪executor指向另一条真实run，各入口均失败且不写receipt。全部包run与receipt/executor必须相同；completed/failed历史run仍可读取匹配receipt，恢复不取fresh context/policy。测试写入真实run而非仅mock返回Fact；单测另覆盖reader未装配fail-closed。
- [ ] RED handler已持真实run行锁时经reader→engine.get_run独立连接读取不自等，有限测试超时内返回已提交initial_context；run binding不进入step patch，engine.start同key返回错误绑定也拒绝。
- [ ] RED真实poll_due及deliver_event持锁handler均能在独立报价事务提交成功receipt；不同连接与明确barrier证明该锁仍阻止并发非键写、键修改/删除，cancel等待handler且不复活终态，poll/event不重复应用。最小改两处Run锁后运行既有引擎回归，不能通过删除FK取绿。

```python
async def test_receipt_recovers_after_fresh_context_is_no_longer_usable(approval_case):
    receipt = await approval_case.commit_success_then_crash_before_mark_applied()
    await approval_case.change_need_policy_and_disable_decider()
    app = approval_case.restart_application(forbid_fresh_context=True)
    recovered = await app.mark_completed(approval_case.tenant_id,approval_case.quote_id,
                                         executor=approval_case.executor)
    assert recovered == receipt
    assert await approval_case.all_packages_applied()
    assert await approval_case.count_quote_approved_events() == 1
```

- [ ] RED失败标记与另一apply竞争：持机会锁先查receipt，成功后不得迟到mark_apply_failed；部分APPROVED/部分APPLIED hash相同并逐个补记；缺真实receipt不能假成功；拒绝后新quote走T4新成本表路径，过期后走E2新版本。
- [ ] RED：`env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quote_approval_workflow.py tests/integration/test_quote_approval_postgres.py -q`。
- [ ] GREEN实现flow/steps/application/真实read_fact及run reader适配、engine.get_run和quotation统一绑定检查；只受控notifier，真实approvals/quotation/outbox/engine，不用fake receipt冒称完整恢复。提交`feat: 接通报价审批单轮工作流与持久成功恢复`。

### 7.5 目标回归与交付

- [ ] 执行`env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quote_approval_contracts.py tests/unit/test_quote_approval_workflow.py tests/unit/test_approval_service.py tests/integration/test_quote_approval_postgres.py tests/integration/test_quote_approval_locks.py tests/integration/test_quote_approval_migration.py tests/integration/test_migrations.py -q`。
- [ ] 增跑已有playbook/country-policy及邮件审批相关目标回归（按实际已有测试文件解析，不跑空匹配）；`python3 scripts/check_boundaries.py`。记录RED/GREEN、真实DB与受控端口边界。
- [ ] 更新本任务AGENTS/ADR并提交`docs: 记录报价审批单轮与历史恢复边界`；提交只含T5文件。完整Task5交控制器一次独立审查，不自行派review或开始T6/T8。

## 8. 交接声明

T5验收到真实quote+审批包+outbox+success receipt+engine恢复、当前staff/owner/policy锁；T8才装配真实原件展开ACL和通知/生产依赖，T6/T7文件使用0046。审批中心安全payload并非原件访问证明。
CustomerQuoteView仍shared唯一shape，正式文件必须再核当前quote/context/适用批准；QuoteApproved不是永久许可。Task5不触达客户、不自动sent、不增force/默认actor，不改T3A事实词表/旧成本API。
