# Task 8B2：安全HTTP与真实进程装配 Implementation Plan

> **状态：T8B2整项首次审查发现3项Important，Fix1实施中。** 8.1安全读取5c4799b、8.2 HTTP 1179405c、8.3 API/worker 1b8920、8.4完整受控链7c6c061及§2.4中文展示9de7640已提交。审查前相关1844 unit（最后纯测试import排序后定向151）、类型修正后117隔离PG、Linux实际factory全链1项零skip，59生产文件mypy/Ruff/结构通过；这些未覆盖本次发现的两个负向组合，不是最终验收或整个Phase 2完成。首次整项范围585cead..9b05f70，Fix1按§9修文件整组门、轮中失锁和共享机械装配，修复范围复审通过才关门。未勾选项仍待验，T9/T10未开始。

**Goal:** 将已验收域/文件能力接入安全HTTP、显式配置和真实API/worker，保持旧流程兼容。
**Spec:** `docs/superpowers/specs/2026-08-28-phase2-costing-quotation-design.md`与主计划T8B2。文件能力精确要求见[文件Gateway子计划](2026-08-28-phase2-quote-file-gateway.md)，来源能力见[有界取证子计划](2026-08-28-phase2-quote-evidence.md)。本文件不重做B1的文件/限速/恢复规则；下文方法描述是规范，不独立构成验收结论。

ADR指定`docs/adr/0022-quotation-runtime-http-contracts.md`：2026-08-29控制器派发前核未占号，记录本批准备事实公共契约、权限交集、HTTP与实际运行时兼容决策；若被其他工作占用先回报，不覆盖已有ADR。实现者只更新本批实现相关规则/ADR，不改控制器永久计划或前置验收状态。

## 1. 实际接缝与责任

| 证据 | 必须解决的接缝 |
| --- | --- |
| `apps/api/composition/runtime.py:824,857,1166–1192,1238` | 真factory先构造唯一approvals、后复制handlers进engine；不能在外部另造quotation专用approvals，也不能建好engine后才补原handlers字典。 |
| `apps/api/runtime.py:66–113`；`runtime_config.py:239–266` | 当前零参入口读环境、构造factory，lifespan只校验schema/释放DB；新settings/probe/关闭必须接这里，不停留在孤立factory。 |
| `apps/api/dependencies.py:237–280` | frozen ConfiguredApiDependencies可追加一个可选typed quotation依赖；现tool_gateway是手动发送依赖，不能被文件Gateway替换。 |
| `apps/api/routers/costing_quotes.py:36–63` | 现成本门仅boss/product/sourcing/finance，域仍二次判权；不能把它套客户文件、版本发现或单位消息入口。 |
| `domains/demand/service.py:80–97`；`schemas.py:112–121,172–182` | 真实unit confirm/get_facts/get_confirmation已存在；命令有expected_quantity_fact_hash、expected_unit_confirmation_id，完整输出却含source_quote，需安全投影。 |
| `infra/db/quote_context.py:104,150–163` | open先取必需issuer，且缺quantity/destination/unit即报错；首次单位页面不能靠它初始化，更不能为GET伪造默认单位。 |
| `domains/costing/service.py:283–340,376–457` | T2缺list_price_evidence/get_coverage；T3B有get_scope(id)，无按sheet发现scope。calculate接PricingOptions+quote_fx_ref，HTTP不能直接接受其FxRate。 |
| `domains/quotations/service.py:150–195`；`version_schemas.py:40–73,110–139` | get/list为四角色内部完整内容；get_confirmed_issuer是受信无actor口，不能直接挂HTTP；QuoteDraftCommand已绑定完整CAS/条款。 |
| `workflows/quote_approval/application.py:219,374,405` | 真实create/confirm_scope/calculate可复用；不能router自己拼freeze、成本回执或锁顺序。 |
| `domains/quotations/service_impl.py:62–82`；`workflows/quote_approval/issuer_reader.py:11` | T5服务需要context，而context的现PersistentQuoteIssuerReader直接要该服务，除engine环外还有抬头reader构造环。 |
| `apps/api/main.py:75–113` | 现OpenAPI全局覆盖400，且仅保留明确ApiErrorResponse的422；不能声称新增文件错误schema在任意status都会自动保留。 |
| `apps/scheduler_worker/runtime.py:970,1216–1246,1322–1348`；`main.py:90–99,187–242,299–333` | scheduler有自己的approvals与factory，handlers必须先齐；expiry仅放成功取得且仍持有singleton锁的cycle，资源退出先于DB dispose。 |

新增HTTP业务DTO放`domains/{costing,demand,quotations}/http_schemas.py`并从各自schemas/service重导出；纯白名单投影放各域`http_projection.py`。技术run/file调用wrapper放`workflows/quote_approval/{http_schemas,file_schemas}.py`。router不定义第二业务模型，不反向导入Gateway到域，不把业务判断移至infra。
新增流程HTTP适配放`workflows/quote_approval/http.py`；它只调用域公共端口、逐字段适配。API routes集中`apps/api/routers/quotation_actions.py`，复用`/costing-quotes`前缀，不改旧costing_quotes的readiness/CRUD语义。

## 2. HTTP映射与身份/幂等

可信当前员工ID沿T6与T8A/B1的fact_identity既有契约，不要求emp_ULID或迁移旧员工；身份仍只来自真实请求依赖，body/query不得自报actor/role。实际factory/ASGI覆盖合法旧员工ID的授权成功及不存在、失活、撤权拒绝，不把格式合法当许可；其余资源ID按对应公开DTO严格规则处理。

真实Gateway审计仍只表示最多32字符的既有safe-label员工编号，且保留敏感token禁用；业务DTO的40字符身份范围不因此扩大网关列。本批不迁移/截断/hash编号或伪造UserId。文件路径真实invoke尚无result的ValidationError按B1裁定返回固定invalid_input/无call ID；来源路径沿A现有固定错误映射失败关闭。实际factory需补已存在且不超过32字符的中文/内部空格/保留token编号零对象IO反例，并保留合法短旧编号原样入审计正例；不能宣称所有fact_identity都可执行Gateway。employees.employee_id本身也是String(32)，33/40字符拒绝仅用B1受控actor/access配真实Gateway覆盖，不声称存在这种持久员工或要求真实PG插入。metadata内部读权仍沿原域契约，不能全局改成32字符身份门。

派发前核实的同链兼容修复：`workflows/quote_approval/steps.py::_identity`目前仍对prepared_by/initiated_by要求`startswith("emp_")`，会拒绝服务已允许的合法短旧员工编号。B2仅将这两个值的格式校验改为调用现`fact_identity`并保留原字符串；该函数只校验且返回None，不能把返回值当ID。其余run/quote/version/hash绑定、实际当前员工和审批独立性检查全部保留，不改workflow版本或既有run数据。先用真实步骤验证非emp_旧编号的行为RED，补非法/空/非字符串拒绝与canonical编号保护测试；§8.4用实际factory、真实持久旧编号走submit→步骤→独立审批→文件，不能只证明HTTP202即称同链兼容。原错误仍固定workflow_binding_invalid，不增加通用身份规则或改Gateway safe-label限制。

实际Linux同链发现的第二处旧员工接缝：公共ApprovalServiceImpl.submit仅在quote_contract_subject返回None的legacy分支执行原proposed_by_employee/owner_employee的emp_前缀检查；真实新版subject已由ApprovalQuoteSubject→QuoteDTO按既有fact_identity验证两人，原值入库、不重复或转换。decide保留ApprovalId/approved/note门，把员工格式判断移至现_decision_guard短读package并严格_marker之后：legacy仍原_optional_id，真实新版用fact_identity，ValueError映固定ValidationError。不得增加SQL、放宽半标记/损坏namespace、改变当前guard/员工机会→审批锁序/禁止自批/回执。代价是无效legacy决定人的格式错误可能晚于不存在/损坏package错误，短读先于格式校验；仅此错误优先级变化，不改变旧可接受员工集合。四个已观察行为RED保留，补新版非法/None/控制字符/超长、旧短编号拒绝、canonical与自批/撤权保护，实际持久短编号贯通Linux独立审批与文件；ADR0022与审批就近规则留痕。

本片类型门的窄例外：steps.py已触及，扩大mypy发现同分支T5既有的两项QuoteWorkflowRunFact入参可空类型错误与一次poll/apply局部变量类型复用，B2一起修正，不能排除该文件宣称类型通过。将同一七字段原值映射交给现QuoteWorkflowRunFact.model_validate，沿其原strict/frozen/extra-forbid及全部validator，不先cast未经校验的quote_version/content_hash为int/str、不做转换/默认值、model_construct或ignore；apply分支result/patch局部变量仅分别改名apply_result/apply_patch。原workflow_binding_invalid映射、身份原文、版本/hash/流程行为均不变。先保存已观察静态RED；新增或复用真实step对缺/None/bool/0/字符串版本、非法hash与合法输入的保护测试（改前已GREEN就如实记回归），改后原19文件及本片新增公共出口完整mypy及原报价审批unit/相关真实PG回归通过。此修正限这三项，不扩大为通用WorkflowRun上下文重构。

下表路径均相对`/costing-quotes`。C=当前在职四成本角色；B=boss且当前在职；U=原NeedUnitAuthorizer的Need/机会权限，依T3A§1明确为C **并且**现有机会访问权，涉及消息原件或历史receipt时再叠加T8A现boss-only入站消息ACL；F=当前机会ABAC：sales本人/manager当前直属owner/boss租户，绝不自动包含四成本角色。现有机会矩阵中C与机会访问权交集只有boss，不能让U自动等于F或为其他成本角色增加CRM权限。
所有路径先可信RequestIdentity；tenant与actor只来自认证，服务再读当前员工。跨tenant/缺对象统一404；真实拒权403；缺依赖503，不伪装空列表。K=必填`Idempotency-Key`，按既有QuoteKey/各域原校验绑定，原样传给对应公共服务，重放不换key；不新增通用幂等表。请求不能自证confirmed_by/at、tenant、role、owner、prepared_by、locked/approved或operation_id。

| 方法/路径 | 安全入参 → 安全响应；真实调用 | 门/幂等 |
| --- | --- | --- |
| GET `/policies?category=...` | category显式可空 → PricingPolicyPublicView；T2 get_policy；无当前有效已确认政策404 | C，读 |
| POST `/policies` | 现PricingPolicyCreate → PricingPolicyPublicView；confirm_policy | B+本人资料ACL，K；根来源`$` |
| GET/POST `/issuer` | GET无body；POST现QuoteIssuerCreate(name/address/contact) → QuoteIssuerPublicView | GET C新guard；POST B，K；无客户端source_ref |
| POST `/quote-fx`；GET `/quote-fx/{fx_id}` | 现QuoteFxCreate/路径ID → QuoteFxPublicView；confirm/get_quote_fx | C；POST本人资料ACL+K，`$` |
| POST `/price-evidence`；GET `/opportunities/{opportunity_id}/price-evidence` | 现PriceEvidenceCreate判别union → PriceEvidencePublicView；GET tuple同view；confirm_price/新增list | C；POST本人upload ACL+K，不额外套CRM gate |
| POST/GET `/cost-sheets/{sheet_id}/coverage` | 现CostCoverageCreate → CostCoveragePublicView；GET可选content_hash，省略取持久最新确认 | C；POST K；原confirm返回hash后精确读回 |
| POST/GET `/cost-sheets/{sheet_id}/scope-confirmations` | 现CostScopeConfirmationCommand → CostScopePublicView；GET tuple同view | C；POST全部依据本次来源授权+K，沿prepare_scope_access→context→confirm |
| GET `/cost-sheets/{sheet_id}/scope-confirmations/{confirmation_id}` | 路径匹配真实sheet → CostScopePublicView；get_scope并核所属sheet | C，历史只读，不宣称当前适用 |
| POST `/cost-sheets/{sheet_id}/calculate` | CostCalculationCommand → 现CalculationSnapshot；QuotePreparationApplication.calculate | C；只读测算无K、无freeze |
| GET `/opportunities/{opportunity_id}/quote-context` | 无quote前置 → QuotePreparationPublicView（缺项时context_hash可空；数量缺失时quantity_fact_hash可空） | C；当前事实与hash GET，不写入 |
| GET `/needs/{need_id}/unit` | 无quote/issuer/unit前置 → NeedUnitPreparationView；get_facts安全投影 | U，读；完整原文另走T8A |
| POST `/needs/{need_id}/unit-confirmations` | 现NeedUnitConfirmationCommand → NeedUnitConfirmationPublicView；confirm | U+真实消息ACL，K；首次expected_unit_confirmation_id=None |
| GET `/needs/{need_id}/unit-confirmations/{confirmation_id}` | ID → NeedUnitConfirmationPublicView；get_confirmation | U+原引用ACL；旧receipt不恢复stale unit |
| POST `/opportunities/{opportunity_id}/quotes` | 现QuoteDraftCommand → QuoteInternalPublicView；QuoteApplicationService.create | C，K；body.opportunity_id须与path相同 |
| POST `/quotes/{quote_id}/revisions` | 同QuoteDraftCommand → 同view；仍调用create | C，K；replaces_quote_id=path且expected_quote_version显式；不改旧终态规则 |
| GET `/opportunities/{opportunity_id}/quotes`；GET `/quotes/{quote_id}` | tuple QuoteInternalPublicView/单view；内部get/list后本域纯投影 | C，不能给sales/manager复用 |
| POST `/quotes/{quote_id}/submit` | 无body → QuoteApprovalStartResult；T5 start_quote_approval | C；T5固定quote/content canonical，无客户端executor/批准包/新run |
| GET `/opportunities/{opportunity_id}/customer-quote-versions?before_version=&limit=` | 显式正limit、可空正before_version → 已裁定QuoteCustomerVersionPage | F；独立服务，后台allowed_actions/blockers，metadata绑定故障503 |
| GET `/quotes/{quote_id}/files` | 无body → tuple QuoteFileView；T6 list_files | F；列已有文件不是正式下载授权 |
| POST `/quotes/{quote_id}/files` | 空body → QuoteFileView；QuoteFilesApplication.generate | F+当前全部正式门；仅服务端canonical，不接客户端key/template |
| GET `/quotes/{quote_id}/files/{file_id}` | 路径IDs → application/pdf bytes；download | F+当前全部正式门，独立LOW read |
| GET `/quotes/{quote_id}/files/{file_id}/history` | 路径IDs → 原已存application/pdf；新增application.read_history | F；独立LOW history，不接受history bool，不再生成 |
| POST `/quotes/{quote_id}/files/reconcile` | workflow QuoteFileRecoveryCommand；quote_id匹配path → QuoteFileRecoveryResult | F+当前全部正式门；独立MEDIUM/NONE、无客户端key/force |
| POST `/evidence/preview`；POST `/evidence/locator` | T8A preview/locate的安全技术wrapper → EvidencePreviewPublicView/EvidenceLocatorPublicView | 按scope分别pricing或need_unit当前权限+资料ACL；LOW/NONE |

POST只读calculate/preview/locator、T5固定单轮submit与NONE reconcile不假装支持“任意HTTP key持久重放”；控制器已接受这些窄例外，见§6。旧create_sheet/add_item不在本批补造幂等能力。K绑定actor/完整请求沿既有域规则，冲突409；同key不同terms/order/valid_until/scope/CAS必须冲突。
空body端点只接受无body或`{}`，复用报价域拟议零字段`QuoteEmptyCommand(extra='forbid')`；不能因FastAPI未声明body而默默接受force/approved。新文件端点未知history/template/key/actor查询或body字段直接按输入错误拒绝；Idempotency-Key在非K的新端点不作为操作身份，不能影响server canonical。成功GET/确认/创建默认200（幂等重放同形状），submit单独202；不以201暗示重放又创建了一条记录。
修订仍按T4：latest expired走显式E2；latest accepted/rejected且无active时，用新成本表+新scope+新key、replaces=None从机会POST新建下一版，旧终态不改；禁止把它发至revisions端点绕原CAS/锁表规则。
来源HTTP不用自由URL/对象键，也不开放verify任意资料的代理端点。preview body复用T8A EvidencePreviewRequest；locator body复用EvidenceLocateRequest；scope含purpose，need_unit必含need_id/action。API不接受actor/role；reader重新核本次用途，真正confirm由域reader选受信用途。body源文本不进日志/ledger，T8A原文槽只在本次已授权HTTP按白名单领取。
PDF响应使用`Content-Type: application/pdf`、`Cache-Control: private, no-store`、`X-Content-Type-Options: nosniff`；Content-Disposition文件名只由已核canonical quote ID/version/template组成，历史使用attachment。无客户名/来源/成本/路径headers，无JSON/base64/永久URL；bytes先有界核hash-size且后置授权成功才构造Response，不边读边向客户端泄出未核bytes。

### 2.1 必须新增的安全DTO（字段是allowlist，不用exclude黑名单）

各域HTTP外层DTO strict/frozen/extra-forbid，JSON数组/日期/Decimal按现公开wire适配无损转换；不以strict阻断合法JSON数组，也不接受float金额。共享的纯`ProvenanceSummary`可放shared/schemas/provenance.py：source_type:SourceType、source_id:str、extracted_by:str、extracted_at:datetime、confirmed_by:EmployeeId|None、confirmed_at:datetime|None，**无source_quote/URL/locator**；只定义形状/投影，不建授权框架。下列逐字段继承的商业类型/可空性沿现源DTO，不把原可空事实强制非空或改成自由str；新ID/Hash/时间分别用现强类型/Hash/UTC aware。

- `NeedUnitPreparationView`（demand）：need_id、account_id、quantity:int|None、quantity_fact_hash:Hash|None、unit:str|None、unit_confirmation_id:str|None、quantity_status:NeedQuantityPreparationStatus、unit_status:NeedUnitPreparationStatus、quantity_origin/unit_origin:ProvenanceSummary|None。状态/hash来自§2.2的demand纯投影；仅quantity=None时数量hash为空，历史0仍有真实hash，不能用于报价。
- `NeedUnitConfirmationPublicView`：need_id、confirmation_id、quantity_fact_hash、unit:str、confirmed_by/at、source_message_id、artifact_id、content_hash、observed_at、unit_origin；不携source_quote/locator。GET当前unit可发现最近绑定ID；失败后POST原K仍是receipt恢复路径，不新增猜测ID入口。
- `PricingSourceSummary`（costing）：source_ref、artifact_id、content_hash、source_type、observed_at；字段来源统一转ProvenanceSummary。POST入参仍现locator，响应并不意味着任意调用者可展开原文。
- `PricingPolicyPublicView`：policy_id/content_hash/category/minimum_margin_rate/target_margin_rate/cost_groups/effective_from/confirmed_by/confirmed_at、source:PricingSourceSummary、field_provenance:dict[str,ProvenanceSummary]；无旧source完整对象。
- `PriceEvidencePublicView = Annotated[SupplierPriceEvidencePublicView | ExpenseEvidencePublicView, Field(discriminator='kind')]`；共同evidence_id/evidence_hash/opportunity_id/currency/amount/confirmed_by/confirmed_at/source:PricingSourceSummary/field_provenance摘要。supplier的kind固定supplier_price，另need_id/supplier_ref/specification/unit/destination/basis/quantity_min/quantity_max/moq/quoted_at/valid_until；expense的kind固定confirmed_expense，另item_type/allocation_scope/is_per_unit/quantity/basis/observed_at/valid_until。字段/类型按下表逐值投影，不从两分支凑通用记录。供应商自由specification保留，不替换为客户JSON或当机器语义核验。
- `QuoteFxPublicView`：fx_id/content_hash/base_currency/quote_currency/rate/observed_at/confirmed_by/confirmed_at/source/field_provenance。`CostCoveragePublicView`：coverage_id/cost_sheet_id/content_hash/expected_sheet_hash/decisions/acquisition_mode/confirmed_by/confirmed_at/field_provenance；decisions保留现22类及item_sequence/evidence_id/source_line_ref/allocation_scope。
- `QuoteIssuerPublicView`（quotations）：issuer_id/content_hash/name/address/contact/source_ref/confirmed_by/confirmed_at/field_provenance摘要，保留manual来源；无原件读权含义。
- `QuoteNeedPublicSummary`（quotations）：need_id/account_id/status；product_category:str必填；application/material/size_spec/packaging/destination/current_supply_issue/certification_required/unit:str|None；quantity:int|None、required_by:date|None、target_price:Money|None；unit_quantity_fact_hash/unit_confirmation_id可空；每个存在事实的`origins:dict[str,ProvenanceSummary]`。product_category沿真实NeedQuoteFacts必填契约，不能为损坏记录擅改可空；不传FactualField本体或runtime。
- `QuotePreparationPublicView`：opportunity_id/account_id/owner_id/prepared_by/account_name/country、need:QuoteNeedPublicSummary、need_facts_hash/specification_hash:Hash、specification:QuoteSpecificationFacts、issuer:QuoteIssuerPublicView|None、quantity_fact_hash:Hash|None、quantity_status:NeedQuantityPreparationStatus、unit_status:NeedUnitPreparationStatus、context_hash:Hash|None、blockers:tuple[QuotePreparationBlocker,...]、checked_at。blocker的field/code精确配对见§2.2；真实可构造规格的hash保持非空，只有完整context才能给context_hash。
- `CostScopePublicView`（costing）：confirmation_id/opportunity_id/need_id/cost_sheet_id/sheet_hash/coverage_id/coverage_hash/need_facts_hash/specification/specification_hash/terms/terms_hash/valid_until/evidence_bindings/content_hash、provenance摘要；**不带need_facts**，当前需求另读上项；原始scope内部仍完整保存全部facts。
- `CostCalculationCommand`（costing）：mode:Literal['target','manual']、unit_price:Money|None、rounding:RoundingPolicy、quote_fx_ref:str|None、algorithm_version:Literal['costing-v1']；复用原mode校验。workflow构造PricingOptions(quote_fx=None)，交T3B按真实ref查FX；不能让客户端嵌入FxRate或CalculationSnapshot。
- `QuoteInternalPublicView`（quotations）：quote_id/opportunity_id/version/state/created_at/valid_until/prepared_by/owner_id/replaces_quote_id/replaced_quote_version/content_hash/request_hash、issuer:QuoteIssuerPublicView、account_name/country、lines:tuple[QuoteContentLine,...]、terms、calculation:QuoteCalculationSnapshot、basis_id/basis_hash/cost_sheet_id/scope_confirmation_id；没有basis/intent/Need原文/runtime。客户显示字符串只由project_customer产生，内部展示不等于正式文件许可。
- `QuoteApprovalStartResult`（workflow）：quote_id、run_id；只有T5 start后真实run绑定核验成功才返回，202不宣称已提交全部包或已批准。审批读取/决定仍现approvals router+T5 guarded service，不新加HTTP approve/apply/bool入口。
- `EvidencePreviewPublicView`（workflow技术wrapper）：source_ref、scope、artifact_id、raw_hash、profile、page、text、text_hash；`EvidenceLocatorPublicView`同上加start/end/excerpt_hash/excerpt/locator。字段逐值从T8A结果取，verify-root不用于此响应；仅这两个已授权原文端点能输出text/excerpt，无对象地址/原始metadata dump。
- 客户版本页、QuoteFileView与recovery命令/结果严格复用文件rulings；不在router复制CustomerQuoteView或ToolCallId。

实际字段对照（源码是本次读取事实，不代表T4/T5已经最终验收）：

| 安全投影 | 唯一实际来源/禁止补造 |
| --- | --- |
| supplier商业字段 | `domains/costing/schemas.py:408–441,554–567`的SupplierPriceEvidenceView；basis仅quoted/indicative，数量是quantity_min/max与moq，不存在quantity或observed_at顶层字段；valid_until必填。 |
| expense商业字段 | 同文件`:453–463,570–575`的ExpenseEvidenceView；basis仅quoted/actual，quantity是真实整数，valid_until可空；无need_id/supplier_ref/unit/destination/moq/quoted_at。 |
| 两分支source/field_provenance | 实际`view.source`与`view.field_provenance`分别白名单投影；共同evidence_hash不是source.content_hash。原顶层source_ref/locator不额外dump，source摘要保留真实source_ref但无locator/source_url/source_quote。QuoteFxView另用content_hash，不能混成第三种PriceEvidence。 |
| QuoteInternalPublicView的版本字段 | `domains/quotations/version_schemas.py:110–139`：令`c = detail.content`；除state取detail.state，其余quote_id/opportunity_id/version/created_at/valid_until/prepared_by/owner_id/replaces_quote_id/replaced_quote_version/content_hash/request_hash/account_name/country/terms全取c同名字段，issuer只投影c.issuer。 |
| 内部lines/calculation与依据ID | lines=c.lines（同文件`:84–94`的QuoteContentLine，含rounding；无旧行moq/lead_time/price_snapshot_ref）；calculation=c.basis.calculation，basis_id/basis_hash/cost_sheet_id取c.basis同名；scope_confirmation_id取c.basis.scope_confirmation.confirmation_id（`basis_schemas.py:201–218,235–249,262–289`），不是不存在的basis.scope_confirmation_id。 |

两个投影分别在costing/quotation的http_projection.py显式构造返回；禁止`getattr(..., default)`补不存在字段或递归model_dump后排除。内部金额沿真实Money/QuoteCalculationSnapshot，不造detail.total/利润新算法；来源摘要转换不是原件授权。

### 2.2 缺失读口与准备事实入口

以下新增方法在所属域service导出；实现仍本域仓储、当前actor校验，返回内部DTO再交本域纯public投影。不是router直查DB：

```python
# CostingQuoteService；对齐主计划已要求的refresh口
async def list_price_evidence(self, tenant_id: TenantId, opportunity_id: OpportunityId,
                              *, actor: CostingActor) -> tuple[PriceEvidenceView, ...]: ...
async def get_coverage(self, tenant_id: TenantId, cost_sheet_id: CostSheetId,
                       *, actor: CostingActor,
                       content_hash: str | None = None) -> CostCoverageView | None: ...
# CostingFreezeService；按确认时间、ID稳定排序，旧scope不被新scope覆盖
async def list_scopes(self, tenant_id: TenantId, cost_sheet_id: CostSheetId,
                      *, actor: CostingActor) -> tuple[CostScopeConfirmationView, ...]: ...
# QuotationVersionService；原get_confirmed_issuer保持内部用途
async def get_issuer(self, tenant_id: TenantId, *, actor: QuotationActor
                     ) -> QuoteIssuerPublicView | None: ...
```

无任何确认时coverage/issuer返回JSON null；指定coverage hash却不存在则HTTP 404，损坏/不可用不能返回null。confirm_coverage原hash返回不能伪装成完整View：同调用后get_coverage(content_hash=返回hash)核真实sheet/hash再投影，不取可能被并发新确认替换的latest；结果未知时保留原K，不能永久卡在“旧K回放→新latest不匹配”。这是主计划get_coverage口的兼容keyword增量；刷新GET不补写。scope GET按真实tenant+sheet发现历史ID，不从前端缓存或成本表备注推断。
新增报价域async `QuotePreparationReadService.get(tenant_id:TenantId,opportunity_id:OpportunityId,*,actor_id:EmployeeId)->QuotePreparationPublicView`，与QuotePreparationPolicy的C用途一致；新增`QuoteContextProvider.open_preparation_facts(tenant_id:TenantId,opportunity_id:OpportunityId,actor_id:EmployeeId,*,prepared_by:EmployeeId)->AsyncContextManager[QuotePreparationFacts]`。
内部`QuotePreparationFacts`字段精确为tenant_id:TenantId、opportunity_id:OpportunityId、account_id:ProspectAccountId、owner_id/prepared_by:EmployeeId、account_name/country/opportunity_state:str、need_facts:NeedQuoteFacts、runtime:QuoteRuntimeFacts、issuer:QuoteIssuer|None；其余hash/合法性由域公共纯函数计算。SQL复用T3B同员工→机会→Need锁/投影，不要求unit/issuer已具备，不另复制业务角色或规格规则；仅明确issuer_not_found变None，损坏/权限/DB故障向上传递。
该新增只读口**不替代或放松**原open：calculate/scope/create仍要求完整事实、有效unit和issuer。准备GET用于本次新起草，prepared_by由服务固定为可信actor_id并在响应明示，不接客户端替身；先在lease内调用policy.require(tenant_id, facts.runtime.current_actor, action='prepare')检查C，再调用下面用途专属纯投影。首次U单位页面仍直接NeedUnitService.get_facts后调用demand本域同一纯投影，不因无issuer被封，也不把U扩大成C。

#### 2.2.1 demand规则的最窄公开接缝（新增拟议，旧validator不改用途）

现`domains/demand/service.py:27–39`公开三个纯函数，`unit_facts.py:25–65`拥有数量/hash/单位有效性规则；`workflows/quote_approval/application.py`的DemandNeedFactsValidator会把quantity_invalid映射为CostFreezeError(quantity_mismatch)，只能继续服务原冻结用途，不能拿来区分准备页面缺项，更不能给它加可变mode/purpose。
新增下面**一个共享纯结果DTO**到`shared/schemas/quote_facts.py`；demand与quotation从service重导出同class/类型别名，shared不含判断实现。DTO没有原文，内部调用仍核tenant/need/hash绑定；不接受HTTP提交这个结果。

```python
NeedQuantityPreparationStatus = Literal['missing', 'non_positive', 'unconfirmed', 'current']
NeedUnitPreparationStatus = Literal['blocked_by_quantity', 'missing', 'unconfirmed', 'stale', 'current']
class NeedQuotePreparationAssessment(NeedFactDTO):
    tenant_id: TenantId
    need_id: ValidatedNeedId
    need_facts_hash: FactHash
    quantity_fact_hash: FactHash | None
    quantity_status: NeedQuantityPreparationStatus
    unit_status: NeedUnitPreparationStatus

# demand/unit_facts.py；demand/service.py公开重导出，无IO、无actor参数
def assess_quote_preparation(facts: NeedQuoteFacts) -> NeedQuotePreparationAssessment: ...
# quotations/service.py；实现不导入demand
class QuoteNeedPreparationProjector(Protocol):
    def project(self, facts: NeedQuoteFacts) -> NeedQuotePreparationAssessment: ...
```

新增workflow `preparation_facts.py::DemandQuotePreparationProjector`实现该Protocol，**仅**从两个域service导入并委托assess_quote_preparation，显式转换固定异常，不重新判断数量/确认/单位。QuotePreparationReadServiceImpl放quotations/preparation_read.py，构造必填`context_provider:QuoteContextProvider, policy:QuotePreparationPolicy, need_projector:QuoteNeedPreparationProjector, now:Callable[[],datetime]`；不设None/默认成功adapter。§4本地factory显式注入该workflow adapter；不在infra或router复制规则。新跨域公共契约在实施时留窄ADR，不改原T3A语义/模型词表/完整度。
demand纯函数先验证真实typed输入和编码能力，再调用原need_quote_facts_hash/quantity_fact_hash及require_current_unit；缺项的分类只在demand本域定义。可提取本域私有只读分类helper减少重复，但必须保持三个旧函数的原返回、错误顺序与hash字节；不能把quantity None/0改为默认数，不能用source字串重新拼一个“等价”hash。

| 真实事实/原规则 | assessment结果 → 准备blocker(field, code) |
| --- | --- |
| quantity=None | quantity_status=missing，quantity_fact_hash=None，unit_status=blocked_by_quantity → (quantity,facts_missing)；unit即使有旧值也不宣称有效。 |
| quantity为真实int且<=0（含历史0） | non_positive，**保留原quantity_fact_hash**，unit blocked_by_quantity → (quantity,quantity_invalid)。 |
| 正整数数量但其Provenance未人工确认 | unconfirmed，保留真实数量hash，unit blocked_by_quantity → (quantity,fact_unconfirmed)。 |
| 正整数数量已确认，unit或unit_confirmation_id缺失 | quantity current，unit missing → (unit,unit_missing)。 |
| 上述数量有效，unit存在/有确认ID但unit的Provenance未确认 | unit unconfirmed → (unit,fact_unconfirmed)；遵守原require_current_unit先确认、后绑定顺序。 |
| unit已确认但unit_quantity_fact_hash与当前数量完整事实hash不等（含None） | unit stale → (unit,unit_stale)；旧receipt不补写、不把hash刷新当重新确认。 |
| 原require_current_unit成功 | quantity current、unit current；没有数量/单位blocker。 |

`QuotePreparationBlocker`是quotation公开strict DTO：`field:Literal['quantity','unit','destination','specification','issuer']`与`code:Literal['facts_missing','quantity_invalid','fact_unconfirmed','unit_missing','unit_stale','issuer_missing']`；只允许上表配对，以及(destination,facts_missing)、(specification,facts_missing)、(issuer,issuer_missing)。按quantity→unit→destination→specification→issuer固定排序/去重；quantity阻断时不再附加误导的unit_missing，原unit值及绑定ID仍只读展示。
quotation自己的三个缺项条件仅为：destination事实为None；可构造规格中material/size_spec/application全为None；issuer真实缺少。这沿`context.py:121–132`现商业完整性与issuer规则，不将required_by/packaging/certification缺失或其他事实未人工确认自行升级为新门槛。
**错误白名单不是blocker兜底**：demand纯函数仅把表内正常缺项作为assessment返回；数量bool/str/其他非int、typed事实/Provenance损坏或hash编码失败抛NeedUnitError(facts_corrupt)。adapter只把该code映射为QuoteContextError(facts_corrupt)；意外异常/未知code固定QuoteContextUnavailableError(dependency_unavailable)，取消原样传播。reader的record_not_found/permission_denied/context_changed/lock_timeout/storage_unknown等仍按真实类别失败；不得catch Exception后返回“待补单位”。准备GET的facts_corrupt固定503，非200 blocker，也不能因继承ValidationError误当客户端400；权限403/对象404沿§2原约定。

#### 2.2.2 规格与完整context的真实可构造边界

`shared/schemas/quote_facts.py:82–102`要求product_category:FactualField[str]，`quotations/context.py:63–89`又要求规格文本非空、无首尾空白/控制字符。规格六字段只能由原quote_specification(facts)投影，再用原canonical_quote_specification/quote_specification_hash；不得model_construct、trim、默认“unknown”、删除None或拿机会摘要补齐。
quantity/unit/destination/issuer缺失不妨碍合法规格构造；material/size_spec/application全None时规格对象和hash仍**真实可计算**，但必须(specification,facts_missing)且context_hash=None。反之product_category缺失/非法，或存在但不符合现文本契约的规格字段，使原规格不可构造：这是当前typed边界外的事实，固定facts_corrupt失败，不返回伪造QuoteSpecificationFacts，也不借nullable specification/hash藏错。完整Need hash同样可覆盖quantity=None/0，但错误typed事实不能靠忽略字段生成hash。
在同一个准备lease内，精确核assessment.tenant_id/need_id等于输入，且assessment.need_facts_hash等于本次完整facts按既有shared canonical编码（版本need-quote-facts-v1）的hash；错tenant/Need或陈旧facts结果按facts_corrupt拒绝，不接受仅hash形状合法。数量/单位状态仍只由demand实现，不在quotation复制其分类。域随后投影安全Need/规格/issuer与blockers。仅blockers为空时按原QuoteBusinessContext字段构造并使用其原context_hash；可提取并复用原构造纯helper，但不得在持锁区再调用open另开连接/重复锁，也不把runtime.current_actor/checked_at/public摘要新增到业务hash。原prepared_by仍绑定：不同员工开始新起草可因prepared_by不同而得到不同hash，不承诺不同actor的这个GET永远同hash。构造剩余校验失败必须原类别报错，不吞成None；无blocker而context_hash=None禁止返回。普通GET只读，无锁定成本/确认记录/来源IO。
sheet→opportunity由workflow调用现CostingService.get_sheet得真实ID，不能新增HTTP opportunity自证；scope/calculate仍调用现QuotePreparationApplication。内部报价project方法只裁剪已授权真实quote；QuoteInternalPublicView不可由HTTP反向提交。

#### 2.2.3 价格依据集合的真实机会存在性（实施期核对）

保留§2“对象缺失/跨租户404，存在但无依据200空集合”，不沿旧成本list把两者混为一谈。现Costing UoW没有该事实口；新增本域内部只读存储Protocol，不调用带CRM读权的机会服务，也不要求存在成本表/owner/unit/issuer：

```python
# domains/costing/quote_repository.py，内部存储事实，不是授权票据
class CostingOpportunityReferenceReader(Protocol):
    async def exists(self, tenant_id: TenantId, opportunity_id: OpportunityId) -> bool: ...
# domains/costing/repository.py::CostingUnitOfWork追加必填成员
opportunity_refs: CostingOpportunityReferenceReader
```

infra/db/repositories/costing_quote.py新增CostingOpportunityReferenceReaderImpl，复用本域tenant-bound基座，以infra.db.tables.OpportunityRow作显式tenant+opportunity的SELECT EXISTS，只返回真正bool，不投影客户/owner/Need/状态或复制权限。infra/db/costing_uow.py在同session构造opportunity_refs。list_price_evidence先按原当前员工/C读取权限核验，再在同一短UoW核exists并读取持久依据；不存在或跨tenant抛固定CostingQuoteNotFoundError，存在无依据返回空tuple。SQL失败不能当False/空集合；这是只读时点事实，无新锁/冻结/记录/来源IO，也不修改旧list_sheets等语义。

新增domains/costing/errors.py::CostingQuoteNotFoundError(ValidationError)，固定code=record_not_found、固定中文“成本报价记录不存在”，从costing.service显式重导出；本批安全读取路径使用，HTTP显式映射404，不按异常文字猜分类、不挪用freeze错误消息。未知仓储失败仍沿本批HTTP固定503，不伪装缺项。除§2.2.4明确的两个只读缺项分支外，旧确认/读取异常契约不在此顺改。

先测试实际同租户机会无价格→空、缺机会/跨tenant→not_found、有依据→稳定真实列表、无C权限→拒绝且不调用存在性/依据、存储故障→非not_found；PG fixture真实创建机会，不能用price外键或有无cost_sheet代替。内部Protocol/UoW与适配及这一个固定错误同步ADR0022/就近规则，原签名不新增自由HTTP参数，不扩CRM或授权范围。

#### 2.2.4 既有政策/汇率只读缺项与费用确认CAS分类（实施期核对）

仅将CostingQuoteServiceImpl.get_policy与get_quote_fx里真实record is None分支，从普通shared.ValidationError改为现CostingQuoteNotFoundError；签名和成功返回不变，仍为ValidationError子类，不新建第三种错误或第二套查询。两个新HTTP GET均返回固定404/record_not_found；GET当前政策没有有效记录（包括只有旧未确认margin或只有未生效新政策且无有效默认）是本次读取不存在，不是输入非法，也不返回200/null或409。原get_effective的分类/默认政策选择与当前时间判定完全保留，不能提升未确认旧政策或应用未来政策。

计算/冻结的policy_missing/fx_missing等既有商业前提错误仍保持原409分类；不把GET缺项一律改成该类别。其他非法输入仍400、无权限403、仓储失败503，不用异常文字分支或宽捕获转404。旧Phase1成本router只用原CostingService，不能随此改变旧HTTP状态；直接调用上述两个T2 reader的ValidationError捕获仍兼容，但固定异常类/消息收束须在ADR0022和就近规则说明。

另有旧CostingServiceImpl.get_sheet供新scope/calculate读取真实机会。其缺表分支须保留旧固定文本“成本表不存在”及ValidationError父类兼容，因此新增本域CostSheetNotFoundError(ValidationError)，固定code=record_not_found、固定该旧文本，从costing.service显式重导出；只把get_sheet的sheet is None分支改为此类，不改add_item/assess_for_quote或ID校验。新QuotationRoute按具名类映为固定404/record_not_found，旧costing_quotes router仍走原全局ValidationError处理，必须保持400及原code/message。不要用CostingQuoteNotFoundError的新文本替换旧服务消息，也不为了区分错误再查一次仓储。

费用确认confirm_coverage现将成本表不存在和expected_sheet_hash已变化合并为CostCoverageConflict，此CAS失败在新HTTP明确409/coverage_stale，不误写idempotency_conflict；保留现原子确认判断，不额外查询拆分原因。相应本批新GET成本表缺失仍404；旧成本GET保持上述400兼容。UI须重新读取当前状态再由用户决定，不自动换key/重确认；不能把此确认CAS特例扩为所有读取缺项409。

先补真实PG两T2 reader及get_sheet缺项/跨tenant、有效记录与原政策fallback、旧ValidationError兼容、SQL故障不伪缺项，以及新HTTP两个GET固定404、scope/calculate缺表404、原calculate政策缺失409和费用CAS非幂等分类测试。旧test_costing_service的“成本表不存在”断言须原样通过；旧成本GET用真实get_sheet缺表路径验证原400/code/message，不能只靠替身或改旧期望。保留既有旧成本router/service、成本仓储及T2政策/FX回归；仅变这三个读取缺项分支和新HTTP错误投影，不改确认/冻结业务规则、迁移、Gateway或生产配置。

### 2.3 固定错误与调用ID

仅文件routes复用B1已交付的workflow `QuoteFileApiError`（workflows/quote_approval/file_schemas.py）：code:QuoteFileFailureCode、message（固定中文表）、tool_call_id:ToolCallId|None、original_generation_call_id:ToolCallId|None、retry_after_seconds:int|None。QuoteFileFailureCode引用已裁定文件/限速/显式恢复固定集合，不另建自由code或第二错误DTO；现CallId别名保持B1真实Gateway身份校验，技术调用ID不引入业务域。
真实invoke已有结果才保留tool_call_id；只有经只读ledger核真实同quote/key/HMAC且仍EXECUTING才填original_generation_call_id。CONFLICT新ID、权限拒绝前的输入ID、没有结果的异常都不能假称原canonical；ID放专门字段，不拼message。无Gateway参与的普通输入/身份拒绝继续ApiErrorResponse(code,message)，不改全局契约。
固定状态映射：HTTP输入/尚未invoke的unsupported=400，权限403，缺对象404，idempotency/revision/context冲突及确定性正式阻断409，rate_limited=429，dependency/lock/storage/unknown=503。已有Gateway结果的文件VALIDATION/unsupported同样保留技术错误与call ID，归409，不掉回只code/message的400。文件同status同时可能有flat错误与技术错误时，OpenAPI用显式union；400保留现全局flat契约，不与main强制400覆盖冲突。
Retry-After仅真实结果合法整数1..86400，缺失就不加，不能使用ApiSettings.retry_after_seconds冒充unknown恢复时间；保留原K/原文件，取消不造成功response。业务固定code按T2/T3B/T4/T5/T6已实际导出的类显式映射，不透传异常消息；T2当前reader失败统一InvalidPricingEvidenceError，不能凭空承诺能输出T8A每个细码。

### 2.4 审批可读展示接缝（8.4同链完成后的本项收口）

实际ApprovalView只提供proposed_change_display:dict[str,str]，通用_view把嵌套报价对象json.dumps；这不是已交付的typed报价HTTP详情。为满足原规格§6.3/§7与T9一页审批需求，保留ApprovalView全部字段/构造及HTTP形状，不增加端点/DTO、不让前端JSON.parse猜业务。仅新版报价改用服务端已验证payload的显式中文平面展示。

- 在quotations本域新增approval_display.py，公开纯函数project_quote_approval_display(payload:QuoteApprovalPackagePayload)->dict[str,str]，从quotations.service同函数重导出；只逐字段格式化既有安全事实，不读库/原件、不决定权限/批准或重算金额、利润、差异/hash。禁止递归dump任意模型或自动加入未来字段。
- QuoteApprovalAccess新增纯display(fact:ApprovalFactView)->dict[str,str]；实际QuotationApprovalAccess先复用现_subject的完整绑定校验，再从公共parse_quote_approval_payload取得同安全DTO并委托域投影，不跨域导入私有模型/仓储。缺投影依赖失败关闭，无退回JSON展示的宽兼容。
- ApprovalServiceImpl仅在_read_view及list_for_reader已有当前报价read lease内，原replace(view,can_current_user_decide=...)同时覆盖proposed_change_display=access.display(fact)。通用_view、legacy字段和值、get_by_change_set的新namespace拒绝和其余guard/锁序/期限/hash/决定/apply全部不变。list_pending_for/get经原_read_view覆盖，旧employee前缀行为仍按原接口。
- 展示白名单含租户/quote/版本/机会/content/context/basis身份、起草人与owner、当前审批类型及全部required_types；本公司抬头/地址/联系、客户/国家、产品描述/完整规格/数量/单位、客户单价/整单合计/舍入/有效期、逐项商业条款；核算与报价币种、实际有效单件收入、完整metrics、政策ID/hash/category/最低与目标比例/生效时间；原顺序成本FX与报价FX（币种对、rate、时间、reference），证据ID/hash/类别/basis/原金额/有效期/确认人/时间；previous无则明确无上一版本，有则显示其ID/版本/hash及自身customer/calculation/policy全部安全内容。重复条款/FX/证据不合并或排序改意图，索引用于区分同名字段。
- Money直接保留原amount十进制字符串及币种；metrics除margin_rate/discount_headroom外按原核算币种明确单件，margin_rate、discount_headroom及政策阈值明确“比例，1=100%”，不乘100、量化或Number转换。负数/零/尾零不丢。整单只展示持久displayed_total，不能由投影乘数量造整单利润。前版只列原事实，不计算财务差额。所需例外直接展示required_types，不重复底线判断。明确本批批准不自动发送，不把引用文本当可点击原件地址。
- 所有标签中文且稳定、值只纯文本；不输出原始资料source_quote/source_url/locator、供应商身份、完整Need/basis/Provenance/模型/SQL或动态HTML。该展示仍仅内部审批读权，不成为客户文件或原文授权。
- 先新tests/unit/test_quote_approval_display.py取得缺函数/真实显示RED，并补原test_approval_service对get/list租约内调用、缺guard/越权/损坏namespace零投影、legacy原字典原样断言。实际adapter/独立导入/Decimal低precision上下文不影响值、重复项/前版/无FX/尾零/负值/敏感原文不泄覆盖。唯一旧QuoteAccessCase补显式display适配，不删旧断言；country_policy_change实际依赖旧display完全等值，连同旧playbook/审批回归。
- 新生产文件及已触service/adapter纳Ruff、结构和mypy；当前实际API/worker/旧报价PG回归及最终Linux同链定向验证新展示为中文纯文本、身份/自批等门不变。ADR0022与两域/流程规则记录新namespace展示键变化和Protocol增量。此为8.4后最后小片，整B2 BASE不变，全部完成才一次整项独立审查。
- 本片扩大59文件mypy在已触workflows/quote_approval/approvals.py发现6项T5原构造错误（approval_type的str→Literal两处、request_hash/limit可空、state与decision的Literal）：仅将guard的QuoteApprovalSubject与read_quote_facts的QuoteApprovalFact两处构造改为同类model_validate(逐字段原值mapping)，保留同一strict/frozen/extra-forbid与validator，不转换/默认/cast/ignore，不改_subject、状态推导、异常、锁或回执。原6项静态RED保留；先补或复用真实adapter的非法Literal/缺hash/limit/版本及合法值保护（改前已GREEN注明），最终同59文件及新增生产文件全门与受影响PG改后验证。不归入T10四文件债务，也不排除adapter。代价是两处模型调用表达变化与防验证语义漂移回归，无公开字段/运行规则变更。

代价：新版proposed_change_display内部键从机器字段变为中文展示标签，按旧机器键解析新版包的未知消费者需适配；仓库唯一业务等值消费者country_policy_change属legacy须完全不变。新增纯projection接口及测试替身维护，不新增存储/审批规则/HTTP字段。前端沿生成ApprovalView显示服务端结果，不承诺新增嵌套typed HTTP报价对象。


## 3. 最小显式配置与禁用粒度（已裁定）

新增`infra/quotation_settings.py`只解析非秘密DTO，不读环境/文件/凭证；API runtime_config与scheduler runtime各读同一可选环境变量`TRADEOS_QUOTATION_SETTINGS_JSON`并调用它，不跨apps导入解析器。缺整个变量=旧运行模式；JSON提供时所有下列字段必填，允许指定None仅在标注处，未知/重复key、NaN、bool冒充int、空字符串或缺子字段均固定配置错误，不静默降级为默认。

```python
class QuotationCoreSettings(BaseModel):
    lock_timeout_ms: int
    statement_timeout_ms: int
    maximum_page_size: int
    expiry_batch_limit: int
class QuoteFileRuntimeSettings(BaseModel):
    template_version: str
    maximum_bytes: int
    maximum_pages: int
    maximum_text_bytes: int
    object_read: ObjectReadLimits
    object_write: QuotePdfWriteLimits
    rate_limit: QuoteFileRateLimits
    gateway_lease_seconds: int
class QuotationRuntimeSettings(BaseModel):
    core: QuotationCoreSettings
    evidence: QuoteEvidenceSettings
    files: QuoteFileRuntimeSettings | None

def from_mapping(value: Mapping[str, object]) -> QuotationRuntimeSettings: ...
```

所有DTO strict/frozen/extra-forbid；int均正且排bool。数据库毫秒timeout/分页值限制在Postgres有符号32位可表示范围；expiry_batch_limit还必须满足T4真实expire_overdue的1..1000，不能配置后每轮被域拒绝；都不给业务默认。gateway_lease_seconds遵守现Gateway上限一天。模板必须属于唯一shared集合。maximum_bytes同时限制T7输出、新QUOTE_PDF Store及download；maximum_pages/maximum_text_bytes交T7，三项缺任何一项即不能配置files。QUOTE_PDF writer.maximum_attempts必填严格1。rate_limit直接复用既定四字段；ObjectReadLimits复用T8A五字段，不建第二同名DTO。文件限速window/Retry-After沿既定算法，不另填次数/秒数。
evidence直接调用T8A `infra.quote_evidence_settings.from_mapping`，完整复用raw_maximum_bytes、object_read、parser全部11字段、probe全部7字段及原交叉校验，不另定义parser预算/平台开关。QuoteEvidenceContextReader的statement_timeout_ms取core同字段，context lock/statement取core；T8A限额hash仍由其自身完整编码产生。文件bounded读取用files.object_read，与raw来源可不同预算；两者都必须明确给值。
来源Gateway lease复用本进程既有显式API settings.tool_lease或scheduler config.tool_lease_seconds（真实装配见API composition:1144、scheduler runtime:1082），不依赖可空files。文件Gateway用files.gateway_lease_seconds；两者都验证现1天上限。新固定技术lease_owner按进程/用途区分，rate limiter注入的owner必须与其文件Gateway同值；不是新增actor或fencing。HMAC复用本进程现真实provider/key_version；API/worker配置必须指向相同generation协议密钥版本，轮换冲突仍沿rulings失败关闭，不自动换key。
API `Phase1RuntimeSettings`末尾追加`quotation:QuotationRuntimeSettings|None=None`仅为旧构造兼容；from_environ缺变量得到None，提供则严格JSON解码→纯from_mapping。scheduler在SchedulerRuntimeFactory._resources从其传入environ同样解析，无需import API settings。资源配置错误只报固定变量名/错误类型，不打印JSON。S3 settings与技术HMAC引用沿既有配置单独输入，不把凭证塞本DTO，也不新增生产数字示例。

禁用按以下明确分组，不为了局部缺项造成功依赖：

- 整体quotation配置缺失：新quotation composition=None；新HTTP 503，不注册quote handlers/definition/文件或来源工具；旧costing/readiness、旧审批/邮件照旧。T5无quote_access时新namespace本身仍失败关闭。
- 提供完整core/evidence且S3公开settings及所需真实ports齐备：构造报价核心+来源；缺S3/必要ports则本组unavailable，不注册半套。显式畸形配置直接固定启动错误，不用“不可用”掩盖拼写错误；不放宽零参数runtime现有旧S3/全局配置必填规则，旧入口本来不能启动的情况不承诺可降级启动。
- files=None：报价核心/人工依据/审批/expiry继续；文件generation/read/history/reconcile及客户文件版本发现整体503、不注册文件工具。内部quote get/list仍可用，不能把文件依赖缺失伪装空files。避免无预算仍开放部分文件动作。
- parser probe失败：只关闭真正需要parse的请求（price片段、unit新取证、preview/locator）；T8A pricing `$`根hash与持久安全GET、已有basis/报价/审批/expiry、独立PDF文件路径仍可用，其各自业务门不变。不在router按“unit/price”整类拦掉原服务可完成的metadata-only历史/幂等恢复；由T8A真实parse入口capability失败关闭。不得自动用另一parser；没有probe成功不parse。runtime内再次降级同样读取真实capability，不只缓存startup bool。
- 元数据/准备摘要GET不触probe/构造对象delegate/修复记录；实际正式/历史PDF GET例外，仍经已授权Gateway执行有界对象读取。能力摘要如需UI读取，仅提供固定可用性code，不能借ready字段激活parser；现全局DB readiness不增加写入或secret调用。

测试值仅位于fixture，不能抄入.env.example/from_mapping默认：settings测试逐字段0/-1/bool/缺失/未知键与边界；JSON金额/时间另做HTTP wire测试。文件与DB预算fixture独立明确，T8A Linux/parser/PG链一律复用其永久计划已裁定runtime及fixture限额，不另选镜像或声称Mac通过。技术Gateway lease按真实PG当前时间构造，业务quote时钟分离，避免历史now造成假过期。

## 4. 解构造环与真实装配顺序

`NeedUnitAuthorizer`当前只有demand公开Protocol与受控测试实现，B2须新增真实check/guard适配，并非引用一个已存在的生产类。guard在零原件IO前提下保护当前员工/机会授权行至内层Need事务提交，顺序Employee→Opportunity→Need；check用于锁外即时复核，精确绑定tenant/need/account/actor，不要求已有issuer或unit。具体公共facts/lease适配见§4.1；派发前仍核B1最终接口，以域公共规则判权，不在infra复制业务权限或把普通read当持锁guard。

### 4.1 单位授权的精确实现接缝（B1接口核对后的补正）

不复用B1文件scope作为U授权：它额外要求owner存在且在职，且DTO没有Need/account绑定；这会改变T3A的C∩原机会访问权。新增以下窄事实lease，不扩一般CRM读权或新增权限框架：

| 文件 | 责任 |
| --- | --- |
| `domains/demand/schemas.py`、`service.py` | 内部NeedUnitScopeFacts与NeedUnitScopeReader，公开重导出；不注册HTTP、不改旧NeedUnitService签名 |
| `domains/opportunities/service.py` | 仅显式重导出现有OpportunityAuthorizer/OpportunityAction/ScopeLevel，Actor/OpportunityScope已可用；不改原权限矩阵 |
| 新`infra/db/need_unit_scope.py` | 真实Employee→Opportunity SHARE锁、Need/account结构绑定与短事务关闭；没有角色业务判断 |
| 新`workflows/quote_approval/need_unit_access.py` | 组合成本与机会两个域公共权限，给真实NeedUnitAuthorizer的check/guard |
| 新`tests/unit/test_need_unit_access.py`、`tests/integration/test_need_unit_access.py` | 角色交集、无owner/issuer/unit前置、真实多连接授权锁与Need提交边界 |

```python
# demand.schemas：strict/frozen/extra-forbid，ID沿原fact_identity，actor沿唯一shared DTO
class NeedUnitScopeFacts(NeedFactDTO):
    tenant_id: TenantId
    need_id: ValidatedNeedId
    opportunity_id: OpportunityId
    account_id: ProspectAccountId
    actor: QuoteEmployeeFact

# demand.service，内部存储适配端口，不是授权结果
class NeedUnitScopeReader(Protocol):
    def open(self, tenant_id: TenantId, need_id: ValidatedNeedId,
             actor_id: EmployeeId) -> AsyncContextManager[NeedUnitScopeFacts]: ...

# infra：全部依赖显式，无业务默认
class SqlAlchemyNeedUnitScopeReader:
    def __init__(self, factory: SessionFactory, *, lock_timeout_ms: int,
                 statement_timeout_ms: int) -> None: ...

# workflow：contexts复用A的真实当前员工读取，不需要quotation/issuer先构造
class CurrentNeedUnitAuthorizer:
    def __init__(self, contexts: QuoteEvidenceContextReader,
                 scopes: NeedUnitScopeReader,
                 opportunity_authorizer: OpportunityAuthorizer) -> None: ...
    async def check(self, tenant_id: TenantId, need_id: ValidatedNeedId,
                    actor_id: EmployeeId, *, action: NeedUnitAction) -> NeedUnitAccess: ...
    def guard(self, tenant_id: TenantId, need_id: ValidatedNeedId,
              actor_id: EmployeeId, *, action: NeedUnitAction
              ) -> AsyncContextManager[NeedUnitAccess]: ...
```

workflow每次先校验action为原read/confirm、真实当前actor的tenant/ID/active，再调用costing.service.require_pricing_source_access（原C矩阵），之后调用真实机会域authorizer。此窄交集传入从同一真实员工事实构造的`Actor(str(actor_id), OpportunityScope(level=ScopeLevel.TENANT), role=fact.role)`，固定action=OPPORTUNITY_READ；不把role改成boss、system，不从请求或配置提供allowed。当前原矩阵仅boss+TENANT能同时满足C，其他组合拒绝；以后矩阵若改变，需重新核对本用途，不承诺自动获得新scope。工厂注入既有Phase1OpportunityAuthorizer(真实tenant)实现，不放默认allow。

初检成功后进入scopes.open，逐值核返回tenant/need/actor并用锁内actor再次执行同一两个域规则。通过才构造NeedUnitAccess，account/opportunity取真实lease，authorization_ref取本次机会authorizer的固定规则标识；无fake允许票据。check完整进入并退出guard后返回即时事实，不声称返回后仍持锁；guard保持到调用方Need UoW结束。两函数不调用原件/解析器/模型，取消原样。

SQL设置core显式超时后，先按tenant+actor锁真实Employee SHARE，再按tenant+need锁唯一Opportunity SHARE；该表已有tenant+need唯一约束，不新增迁移。随后只读同tenant+need的Need.account_id（不取Need行锁）；必须与Opportunity.account_id一致，缺记录need_not_found，关系或typed事实损坏facts_corrupt。返回原员工完整QuoteEmployeeFact和精确绑定，不要求owner、issuer、现unit或非终态；终态确认/数量规则仍由原NeedUnitService判断。NeedService在内层FOR UPDATE后已有account/当前事实重验，SQLlease不能提前锁Need导致跨连接自等待。finally独立尽力rollback/close、保留原取消；锁超时固定NeedUnitUnavailableError(lock_timeout)，其他存储故障storage_unknown，原业务错误不得改成facts_corrupt；不输出SQL/原消息。

缺actor或两域拒权→NeedUnitPermissionError(permission_denied)；输入错误→NeedUnitError(invalid_input)；损坏绑定→NeedUnitError(facts_corrupt)；reader意外失败→NeedUnitUnavailableError(dependency_unavailable)，取消不捕为普通异常。C函数的固定QuoteEvidenceError(permission_denied)精确转换，不将任意错误当拒权。HTTP依§2将facts_corrupt映射503，旧单位错误和历史语义保持。

首个失败测试使用实际两个域矩阵和隔离PG：

```python
async def test_unit_guard_preserves_unassigned_opportunity_access(unit_access_case):
    c = unit_access_case
    await c.make_owner_missing_and_unit_missing()
    async with c.authorizer.guard(c.tenant, c.need_id, c.boss_id, action="confirm") as access:
        assert (access.need_id, access.account_id) == (c.need_id, c.account_id)
        assert await c.other_connection_cannot_change_actor_role()
        assert await c.other_connection_cannot_change_opportunity_binding()
        assert await c.need_row_is_not_locked_by_authorizer()
```

fixture新建于该组，三个并发断言用独立连接、NOWAIT或pg_stat_activity锁等待证据；不靠固定sleep。补失活/未知/跨tenant、各角色交集、owner失活但boss原读权仍有效、无opportunity、错account、返回陈旧actor/错Need、SQL超时/取消清理；真实NeedUnitService确认在内层提交后才释放授权锁，来源reader断言所有锁外执行。先运行这两个新文件取得RED，最小实现后同组GREEN，再纳入§8.1旧Need/context回归及§8.4真实API/worker同链；无新迁移、没有新部署默认值。

### 4.2 实际组合与发布顺序

本层装配的静态类型从公共service获取：costing.service仅重导出现有CostingUnitOfWorkFactory与CostingFreezeUowFactory，demand.service仅重导出现有NeedUnitUnitOfWork，保留同class与原Protocol/实现/事务语义，不另造重复协议。API/worker两个composition不得直接导入域repository来做类型cast；同class/独立导入契约及结构自检通过后保留此公开出口，ADR0022记录。只是现有注入契约的可用公开路径，不增加业务能力或HTTP字段。

已替换主计划原示意`build_quotation_composition(..., tool_gateway, ...)`的一步式签名：它要求先有Gateway，但新handlers/readers又依赖quotation。采用本进程两个明确阶段，来源Gateway独立，文件Gateway后建；不修改ToolGateway/WorkflowEngine核心，不把新工具塞入旧email/DNS注册表。

下文SessionFactory=async_sessionmaker[AsyncSession]、Clock=Callable[[],datetime]，只是本地类型别名。拟议API本地`apps/api/composition/quotations.py`输出frozen `QuotationDomainComposition(context_provider:QuoteContextProvider,quotations:QuotationVersionService,costing_quotes:CostingQuoteService,costing_freeze:CostingFreezeService,need_units:NeedUnitService,creation:QuoteApplicationService,preparation:QuotePreparationApplication,preparation_reads:QuotePreparationReadService,files:QuoteFileService|None,approval_access:QuoteApprovalAccess)`；files仅files配置缺失时None。构造函数`build_quotation_domains(factory:SessionFactory,settings:QuotationRuntimeSettings,*,tenant_id:TenantId,evidence:QuotationEvidenceComposition,run_reader:QuoteWorkflowRunReader,artifact_reader:QuoteGeneratedArtifactReader|None,now:Clock)->QuotationDomainComposition`，**不接Gateway或approvals**；需要的仓储/当前身份/纯policy在该本地factory明示构造。
`QuotationEvidenceComposition`为本层frozen bundle：`pricing:PricingEvidenceReader,need_units:NeedUnitEvidenceReader,scope_access:CostScopeSourceAccess,preview_reader:QuoteEvidenceReader,parser:LinuxEvidenceTextParser`；其构造先组T8A access/独立registry/handler/Gateway/slot，再组两个domain reader。它不持quotation service，不引用旧manual_gateway。上传/Need当前authorizer必须真实装配，不用测试reader。
source bundle的本地构造口为`build_quotation_evidence(factory:SessionFactory,settings:QuotationRuntimeSettings,*,tenant_id:TenantId,raw:QuoteEvidenceRawReader,uploads:WorkIntakeService,need_authorizer:NeedUnitAuthorizer,fingerprints:HmacFingerprintProvider,lease_duration:timedelta,lease_owner:str,now:Clock)->QuotationEvidenceComposition`；raw是infra adapter而不是从workflow直接import ArtifactStore。scope_access适配T3B公开require，仅对持久证据重新做T8A access元数据/本人资料授权绑定，不把它伪装成冻结票据或复制价款规则。
另`build_quotation_http(domain:QuotationDomainComposition,*,factory:SessionFactory,approvals:ApprovalService,engine:WorkflowEngine,settings:QuotationRuntimeSettings,evidence:QuotationEvidenceComposition,generated:GeneratedDocumentStore|None,metadata_only:GeneratedDocumentMetadataReader|None,fingerprints:HmacFingerprintProvider,now:Clock)->QuotationHttpComposition`构造文件access/事实adapter、独立文件registry/Gateway/应用及HTTP工作流适配；不会再建approvals/engine。该函数在现engine构造和闭包发布后调用，不参与handlers建立。

source与domain工厂的tenant_id为必填keyword，由API既有settings.tenant_id显式转TenantId、worker既有config.tenant_id原样传入同一运行租户；worker本层等价两个工厂亦如此。它分别供真实QuoteEvidenceTenantCheck和Phase1OpportunityAuthorizer绑定，不能从请求、私有adapter/UoW属性或默认常量猜测，不往QuotationRuntimeSettings JSON/业务DTO新增第二租户字段。HTTP工厂不加该参数：真实QuoteFileTenantCheck仅接slot、核canonical tenant并由域核归属，保持B1规则。测试证明缺参数拒绝、来源跨绑定租户零raw IO、单位用途跨绑定租户拒权，合法当前租户仍可走真实链；不改Gateway核心或增默认权限。

报价结果通知只新增NotificationKind.QUOTE_APPROVAL_RESULT='quote_approval_result'与固定模板，不借用APPROVAL_DECIDED或伪造ApprovalId/DomainEvent。固定source_event='QuoteApprovalResult'、priority=LOW、context.primary_id=quote_id、secondary_id=run_id、reason_code仅approved/rejected/expired/obsolete、level=None；固定中文标题/下一步及相对路径`/costing-quotes/quotes/{quote_id}`（T9补该明确版本入口）。原workflow给定的幂等键原样作为dedup_key，稳定指纹从带命名空间的该键计算，不能加当前时间/新随机数；API经原仅structured_log的NotificationRouter，worker经原NotificationJobStore持久enqueue，不能称API已投站内。现worker对NORMAL会投email，因此此新kind必须LOW且真实模板→原RoutingPolicy只选in_app；不改旧渠道选择/邮箱目录/Gateway，不增加客户通知或价格正文。现通知表kind是String(64)，无需迁移。

仅该新kind兼容现持久短员工编号：在notification_gateway内以单一窄校验函数复用到新notifier、模板及InAppChannel，接受原样ASCII safe-label `[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}` 且拒绝现secret marker；不截断、hash或转换身份。其他kind仍用原canonical emp_ULID校验，邮箱收件目录不改；中文、空白、控制字符、凭证形态、33–40字符继续拒绝，不承诺完整fact_identity40通知兼容。此例外只用于服务器给定的本租户起草人元数据，不取请求收件地址/自由正文、不给新权限。补canonical与合法短编号真实job→claim→模板→原router→in_app持久链、重复通知一次、跨tenant隔离、非法编号/伪kind/源事件/优先级/结果拒绝、email零调用及旧通知回归；新增kind/template/兼容边界记ADR0022及通知就近规则。来源和文件Gateway的既有身份限制不变。

metadata_only必须由API/worker各自实际调用点以B1的GeneratedStoreDocumentMetadataReader(真实GeneratedArtifactStore)独立构造并显式传入；正常文件路径另用GeneratedStoreDocumentAdapter(store,bounded)。不能把宽generated对象换窄注解传入恢复handler，也不能在本HTTP工厂私取adapter._store。files=None时两参数均None；启用files而缺任一真实端口时文件组固定unavailable，不注册半套。测试核恢复对象无公开put/get_bounded/delete/renderer能力，恢复只调用独立metadata方法；两对象可共享同一个底层真实Store的metadata，但不把宽对象交给恢复执行器。worker本层等价装配遵守同约束，不import API工厂。
最终`ConfiguredApiDependencies.quotation:QuotationHttpComposition|None=None`；其字段`domain:QuotationDomainComposition,approval_starter:QuoteApprovalStarter,customer_versions:QuoteCustomerVersionsService|None,files_application:QuoteFilesApplication|None,evidence:QuotationEvidenceComposition,lifecycle:QuotationRuntimeLifecycle`；只有两个文件字段按files分组可空。workflow技术`QuoteApprovalStarter.start(tenant_id:TenantId,quote_id:QuoteId,*,actor_id:EmployeeId)->QuoteApprovalStartResult`为async，当前actor reader→T5 start_quote_approval；不会从请求造QuotationActor。API只从自己composition导入；scheduler可有同责任的本层helper，直接复用域/工作流真实类，不import apps.api。

实际装配顺序（API与worker各自执行；“唯一”指同一进程内所有引用同实例，不要求跨进程共享Python对象）：

1. 解析纯settings，建立session factory；创建真实当前员工/Need/机会reader、原NeedUnitAuthorizer、T5 policy selection reader、T6 metadata adapter及本域UoW。S3 secret引用仅保存；将现有技术HMAC provider初始化必要部分提前供来源Gateway使用（API:993、worker:1039），复用而不重复resolve，不搬动整段依赖approvals的旧发送组合。旧work_uploads按本计划§7改注入DeferredS3ObjectBlobTransport；新来源bounded和QUOTE_PDF writer仍各自实例。
2. 创建本地未发布`engine_ref:WorkflowEngine|None=None`；T5 `WorkflowQuoteRunReader(lambda:engine_ref)`。补窄`DeferredQuoteIssuerReader(quotations:Callable[[],QuotationVersionService|None])`到workflow issuer_reader.py，get_confirmed仅委托真实服务；未绑定固定dependency_unavailable，保留现PersistentQuoteIssuerReader构造兼容。创建未发布`quote_ref`→context→quotation服务→一次绑定quote_ref，运行期不允许HTTP替换。
3. 来源独立Gateway先建，不调用invoke/probe；依赖是T8A纯context+WorkIntake+Need authorizer+raw Store，不依赖报价服务。以真实T8A reader构造T2；quotation已存在后才构造PersistentQuoteCreationCompletionReader→T3B freeze→T4 creation/准备应用。T6 file service只依赖本域UoW/actor/scope/metadata/run reader，可先于quotation门面构造，门面仅委托。
4. 用quotation公共open_approval_access构造T5 workflow QuoteApprovalAccess adapter；在现API:857或worker:970的**唯一**ApprovalServiceImpl构造时传入quote_access。旧router/campaign/playbook/country-policy与quote应用全引用此实例；缺quotation时保持T5新namespace fail closed，不另建有guard的第二实例。
5. 构造T5 QuoteApprovalApplication+真实QuoteApprovalNotifier（现通知出口，仅metadata），得到build_quote_approval_handlers；合入现handlers再构造一次PostgresWorkflowEngine。没有getter求值、DB IO或handler运行可发生在engine_ref发布前。
6. 发布同一真实engine_ref；register_quote_approval(engine,outbox,approvals)注册definition/事件订阅，保留旧注册；调用新HTTP阶段工厂构造文件插件与应用。文件canonical/预留/恢复沿rulings，不注册真实发送。QuoteSendReceiptReader显式固定失败关闭，绝不以Outreach sent/下载自证T4发送绑定。
7. 验证新独立registry工具集合与所启用能力精确一致；现scheduler DNS registry“只含DNS”断言继续成立。最后返回frozen dependencies/runtime，才允许lifespan yield/worker cycle；不得在返回后修补handlers/approvals或改变当前actor共享字段。

其中source与domain阶段可在本地函数内交错组装，但依赖图必须保持上述方向；不能把nullable reader当allow-all，不能在infra写QuoteApprovalAccess、file ABAC或来源业务规则。确切T5/T6构造名/导出由控制器在派发前按最终代码对齐。

## 5. startup、取消与expiry

本层`QuotationRuntimeLifecycle.startup()->None`、`aclose()->None`为async，构造只保存本parser/资源；startup一次await T8A parser.probe，普通能力失败保留unavailable并按§3降级，不能fake ready。配置不一致/意外启动异常清理已建资源后固定失败；取消清理后原样传播，未知错误不得被当作正常platform降级。不在HTTP调用startup，不向HTTP开放probe/fault。
API在真实lifespan里先schema current，再startup，成功完成启停判断后yield；finally先quotation.lifecycle.aclose（回收全部parser子进程/owned task），再engine.dispose，保留primary异常，cleanup只记固定类型。before-yield异常与取消同样执行。同步build失败发生在probe之前，不能留下已启动子进程/SDK；不能用run_until_complete清理或宣称仍握有连接已释放。
worker在本层构造`_QuoteRuntimeActivation(existing_activation,quotation_lifecycle)`：沿既有singleton获取成功且backend校验后，先现activation再await quotation startup，第一轮cycle前完成；未获锁不probe不expiry。worker._resources finally先aclose quotation，再既有health/DB清理；activation取消/失败由现run_scheduler_worker finally解锁。普通parser能力失败不是关闭旧worker理由，quote source解析保持不可用。
`QuoteExpiryDriver(quotations:QuotationVersionService,tenant_id:TenantId,*,limit:int)`与async `scan_once()->int`，limit取core.expiry_batch_limit，只调现expire_overdue，不创建actor/WorkflowRun/审批回执。driver实现可放`workflows/quote_approval/expiry.py`，API不启动后台scanner。
SchedulerRuntime尾部追加`quote_expiry_driver:QuoteExpiryDriver|None=None`；_run_cycle在campaign之后、workflow之前插入独立try/except Exception和固定phase='quote_expiry'记录，不吞CancelledError；expiry失败仍执行其他driver。既有outbox二次drain条件保持，expiry自己的状态事件按原outbox机制后续投递。未获singleton/锁丢失零调用，停机完成当前cycle后释放，不另建cron进程。

## 6. 必须验证与已接受方向

HTTP unit使用受控服务只测wire；真实集成必须从实际factory走真实PG+T2/T3B/T4/T5/T6+Gateway+受控object transport，不把mock service当接线验收。目标文件：`tests/unit/test_quotation_router.py,test_api_runtime.py,test_quotation_runtime_config.py,test_deferred_s3_transport.py`与`tests/integration/test_quote_runtime.py`；scheduler测试在其既有runtime/main测试增量。运行§8列出的受影响旧目标，不扩无关测试重构。

- 首次Need unit=NULL/issuer缺失：U可获真实数量hash、C准备摘要有字段级缺项、正式context仍拒绝；quantity=None无数量hash，0有原hash但non_positive，正数未确认与unit未确认必须区分；缺/陈旧unit逐项覆盖。确认后GET刷新事实/receipt/hash，数量或其来源变更unit stale；原三函数及DemandNeedFactsValidator行为回归不变。
- 完整准备GET与同事实/同prepared_by的原QuoteBusinessContext的context_hash相等；固定prepared_by时只变runtime.current_actor/checked_at不改hash，更换起草人或完整Need来源会改hash。合法规格可带None且有真hash；三项规格均缺只阻context，不造规格；非法必填类别/文本、错误数量类型、损坏Provenance/hash、依赖失败分别硬失败而非blocker。只有一个lease，无嵌套open、原文IO或GET写入。
- 所有确认POST真实同key重放、异body/actor/terms/CAS冲突；scope refresh可发现ID，GET不产生确认/锁定/状态写；金额strings/None/有序terms合法，float/布尔批准/外来actor按现400约定拒绝且零副作用。
- sales/manager内部get403但自己scope客户版本页成功；product/sourcing/finance能读准备而无文件权；本人资料ACL与U消息交集未缩放。所有安全DTO递归检测source_quote、URL、完整Need/basis/runtime不会泄出；独立preview只给有原件ACL者。
- 真实SupplierPriceEvidenceView/ExpenseEvidenceView各投影一例，expense有效期None不被填充，供应商数量档不变单quantity；真实QuoteDetailView投影按上表取content/basis/嵌套scope ID，金额/line.rounding保持等值。禁止使用旧dataclass QuoteView或计划虚构字段作fixture；嵌套Provenance marker不泄出。
- 文件429/unknown/CONFLICT/显式恢复按真实result保留安全ID，original ID额外ledger核验；OpenAPI包含实际错误union，PDF无base64/永久URL，finally清槽；引用rulings完整零未授权IO与恢复测试，不在此另造语义。
- API factory和worker各自只有一个approvals实例；旧routes/handlers与quote共用guard；handlers先于engine，闭包未发布拒绝、发布后真实run读取不自等；issuer环无默认公司，T4真completion reader可恢复。
- 新factory、旧Deferred wrapper构造零S3 secret/SDK，非法参数与并发首建沿本计划§7；旧HMAC/退订技术key初始化按已知受控基线允许，不能把“新增Provider零调用”夸成完整factory完全零resolver。
- 缺env/缺files/畸形子配置/probe失败/取消各组表现精确；原API readiness无写、原旧costing仍用；startup与shutdown对同parser实例，aclose早于DB dispose，子进程归零。SDK timeout不宣称kill线程。
- singleton两连接只有持锁者expiry，独立expiry失败不封其他driver，真实expiry状态/事件和T4规则一致；无cron/API后台expiry。PG技术now/lease与业务now分离，所有资源fixture显式。

控制器已全文核对并接受以下方向（当前实施状态见顶部，整项独立验收仍待）：

1. **GET缺项与安全schema增量**：独立open_preparation_facts/安全投影及refresh端口，保持原open和C/U/F权限不变；本次补充demand唯一纯规则结果与用途adapter，不能从冻结错误猜页面缺项。共享ProvenanceSummary仅纯契约，若最终已有等效摘要则复用同class。
2. **HTTP一致性**：沿现400输入约定，明确本表只读POST、T5固定canonical与NONE例外；文件执行错误用409/429/503显式union，不修改全局错误处理。
3. **配置禁用粒度**：完整core+evidence为一组、files独立；parser普通probe失败仅解析能力降级，旧API/worker继续。畸形显式配置启动失败；缺必要已部署端口保持该组不可用。永久计划措辞由控制器同步，本任务不改。

本计划不改变T8A来源矩阵、不重复T8B1文件/限速/恢复设计、不开放发送或人工终态改写。前置任务通过后按实际公共接口实施；受控SDK不等于真实S3网络验收。

## 7. 旧上传仅延迟构造，不重写IO

Create `connectors/object_store/deferred.py::DeferredS3ObjectBlobTransport(settings:S3ObjectStoreSettings,secret_resolver:ObjectStoreSecretResolver)`，结构化实现旧ObjectBlobTransport。构造只保存受信依赖；首个合法put/get/delete才构造唯一旧S3ObjectBlobTransport并委托，并发首用也只有一个delegate。先校验原合法key/bytes，再构造；非法调用零secret/SDK。初始化失败保持未初始化，固定脱敏错误；取消原样。原put/get/delete返回值与错误语义保持，不改原S3类、无界旧get、重试配置或Raw/EMAIL_DRAFT补偿。
只替换实际API旧上传构造点；新取证始终T8A有界读，新QUOTE_PDF始终B1专属writer。不能因为旧wrapper变lazy而让新能力回退旧get。构造期零S3秘密解析/SDK不等于全factory零resolver：旧HMAC/退订技术key初始化按真实受控基线明确记录，不读取真实.env。新文件或来源未注册不能有隐式Provider动作。

## 8. TDD切片与一次完整任务审查

所有测试在本批worktree，使用显式Python环境、PYTHONPATH及PYTHON_DOTENV_DISABLED=1；真实PG命令加env -u TEST_DATABASE_URL，testcontainers隔离库，不连接生产。逐个行为先失败再最小实现；夹具/依赖缺失与真实行为RED分别报告，不把skip当通过。只提交本组文件；不改T8B1规则或开始T9。

### 8.1 安全投影、刷新口与首次准备

- [ ] 先写 `tests/unit/test_quote_http_projection.py`、`test_quote_preparation_read.py` 和 `tests/integration/test_quote_preparation_read.py`，覆盖§2全部白名单/typed wire、demand assessment、初次缺单位/issuer、合法None规格及损坏事实失败。
- [ ] RED后实现shared只读DTO、demand纯投影、quotation新用途Protocol/只读服务/就近纯投影、workflow适配、原SQL锁租约复用；所有跨域调用只经公共service，不复制分类或以泛异常返回缺项。
- [ ] 按§4.1先运行两个`test_need_unit_access.py`取得RED，再实现真实单位check/guard与Employee→Opportunity事实lease；GREEN须包括实际权限矩阵、独立连接锁证据、无owner/issuer/unit前置、Need内层提交及原件IO锁外。
- [ ] 增加price/coverage/scope/issuer刷新读口及精确同hash读回，证明确认重放不会误读并发新latest。GET零确认/冻结/对象读取；原get_facts/quantity hash/require_current_unit、原context与T4创建/恢复行为不变。
- [ ] 运行上述五个新文件与受影响旧demand/context/costing/quotation目标；保持完整数量来源hash与原context hash字节。提交 `feat: 增加安全报价准备摘要与确认资料刷新`。

### 8.2 HTTP与OpenAPI契约

- [ ] 先写 `tests/unit/test_quotation_router.py` 的实际ASGI请求，覆盖§2每个路径和身份矩阵、400/403/404/409/429/503、未装配能力、空body拒额外字段、Decimal字符串/日期/数组真实wire。
- [ ] 按§2路由表调用公共服务/工作流，业务DTO不放router；表内HTTP key例外严格执行，无默认actor、布尔approved/history或客户端template/key旁路。
- [ ] 按§2.2.4先补三个真实reader的缺项/跨租户与新旧HTTP分类测试，再窄改costing的errors.py、service.py重导出、quote_service.py两个缺项分支及service_impl.py的get_sheet缺项分支；费用CAS只改新HTTP映射。旧消息/400、政策选择、确认与冻结语义均保留；真实PG读取测试不等于下一片实际factory验收。
- [ ] 文件错误与B1真实调用ID绑定；全局ApiErrorResponse/400契约不改，OpenAPI显式安全union与application/pdf，响应不泄body/成本/原件。只读原文预览独立授权，不让普通安全GET替代原件权限。
- [ ] Unit可受控服务只证明wire；真正服务装配留下一组。生成OpenAPI核实际schema不含内部basis/Need原文/请求确认人，前端类型交T9生成。提交 `feat: 接入安全报价与文件HTTP契约`。

### 8.3 显式配置与进程生命周期

- [ ] 先写 `tests/unit/test_quotation_runtime_config.py`、`test_deferred_s3_transport.py`，及既有API/worker runtime测试增量；缺/错JSON、重复key/NaN/bool、全部子预算、expiry 1..1000、files关闭和parser降级必须覆盖。
- [ ] RED后实现纯infra settings及API/worker各自环境读取，旧上传§7 wrapper；按§4顺序形成真实域/来源/文件依赖。唯一approvals实例被旧router/旧workflow与新quote共用，handlers先于engine，延迟reader只发布一次，缺依赖不默认放行。
- [ ] 同parser实例在真实lifespan/worker activation受信probe并在DB dispose前aclose；取消、before-yield失败及singleton未获锁均验证。无同步run_until_complete、fake readiness、真实Provider或API后台expiry。
- [ ] 证明原上传Raw/EMAIL_DRAFT、旧API readonly readiness与旧worker仍兼容。提交 `feat: 装配报价运行配置与受控生命周期`。

### 8.4 真实链路、到期驱动与交接

Linux测试装配窄扩展：现build_parser_image新增quotation专用选择，仅在chain=True且显式复用已验收base image时合法；原parser/refreshed/chain默认stage与入口保持。原A白名单之外仅取apps/__init__.py（若存在；当前apps为namespace package，无此文件，不为打包新增它）及apps/api、apps/scheduler_worker、apps/notification_worker、agent_runtime、notification_gateway的*.py；connectors/dns_auth、object_store、openai、quote_pdf、tavily、web_search的*.py与connectors/search_contracts.py，全部排除AppleDouble。额外测试仅tests/integration/test_quote_runtime.py、test_api_runtime.py、test_scheduler_worker.py、tests/unit/test_api_runtime_config.py、tests/quotation_runtime_fixtures.py及新tests/integration/quotation_runtime_linux_cases.py、quotation_runtime_linux_support.py；前述三个短文件名均在tests/integration。Dockerfile加quotation stage，只COPY该白名单tar已有文件，不复制整工作树或所有apps，不带.env/.git/宿主凭证/挂载/端口。新固定B2入口只运行指定cases，不接受外部任意测试路径或命令；原A入口不变。复用run_chain_cases的internal PG、非root/只读/无cap与已有所有预算，pytest180s、runner240s不变，不运行pip。补tar白名单与原默认入口保护；额外import缺口需具名核实，不宽泛扩目录。环境预检/Mac降级不等于B2真实同链，最终单列实际image ID、原解析器/probe与真实factory业务链结果。

- [ ] `tests/integration/test_quote_runtime.py` 从实际API factory走真实PG+T2/T3B/T4/T5/T6/T8A/B1+Gateway+T7 renderer+受控对象边界；覆盖人工单位/价格/政策/FX/coverage/scope→报价→独立审批→文件、刷新和受限原文。不能把mock业务服务称真实接线。
- [ ] 涉及真实受限解析的完整链在T8A规定Linux runtime与同一隔离PG环境运行，沿其显式资源fixture；Mac解析失败关闭另测。SDK可受控，真实业务资料、真实S3网络与发送仍未运行。
- [ ] Worker真实singleton两连接仅持锁者expiry；expiry故障不阻其他driver，关闭/锁丢失零越权调用；真实状态事件与T4过期规则一致。
- [ ] 重跑全部新unit/integration及既有API/runtime/worker/上传/审批兼容目标；结构自检、原配置Ruff与本批类型检查。记录精确命令、pass/fail/skip和每层受控边界。
- [ ] 更新相关AGENTS/ADR与最小配置说明；`infra/.env.example`只注明可选配置用途及字段要求，不填测试数字或秘密。提交 `docs: 记录报价实际接线与运行边界`，整项由控制器一次独立审查。T9再进行Vue接线，T10才计最终浏览器与全量验收。

## 9. 整项审查 Fix1 裁定（2026-08-29）

首次整项审查为Needs fixes，三项Important全部修复后才进入T9；本节在冲突处优先于§4“本地实现”的措辞。独立进程入口不意味着逐字复制共享实现。整项BASE仍585cead69dc18045dc7b9b0a8d0077879bbda508，Fix1 BASE为9b05f709a9640aadbbdfab50830a3d3d252a8aac。

1. 文件组：不删除domain.files或替换域服务内部引用。在HTTP层以同一个私有_file_composition门同时核domain.files/files_application/customer_versions，六个文件入口（列表、生成、恢复、下载、历史、客户版本发现）共同使用；任一缺失沿原固定503，内部core与来源保持。generated/metadata_only分别缺失时，实际composition配合法身份HTTP的六路均503/零服务及对象IO；完整端口保持原行为。
2. singleton：保留专用连接和原_same_lock_backend。activation返回后、开始本轮业务前确认，另在expiry调用之前、普通phase异常捕获之外确认；可用私有固定失锁异常及显式异步回调。_run_cycle保留旧两参数无expiry消费者，新增keyword-only confirm_lock；有expiry而无确认回调必须失败关闭。主循环捕获失锁→LOCK_LOST、不递增未完成轮、不运行后续workflow/post-outbox。普通expiry错误仍隔离，取消原样传播。真实PG分别在activation/Campaign等待中终止锁backend，第二副本取得同锁后释放等待，旧副本expiry零调用。此检查不宣称对已开始的扫描做分布式fencing或回滚。
3. 共享机械装配：新增非进程库apps/composition_support/__init__.py和quotations.py，共享现生命周期、Domain/Evidence束、单位授权/来源/域factory及文件机械装配。API/worker保留原公开factory完整签名、最终本层dataclass和同class类型重导出；各root仍创建独立实例/slots/gateways/engine引用。新增build_quotation_runtime_parts返回actor_reader/customer_versions/files_application/lifecycle的纯装配bundle；文件lease_owner必填，由API/worker显式传原api-quote-files/scheduler-quote-files，来源owner保持原显式值。API额外创建原CurrentQuoteApprovalStarter，worker不创建。不得导入另一进程、搬业务到infra/workflows、建通用DI或共享全局运行实例。
4. apps/AGENTS明确“进程不得互导，非进程composition_support只能向下依赖”；新增就近规则及ADR0022说明职责，根九硬边界不变。结构测试覆盖绝对/相对import及反向进程依赖；现checker未检查apps内部方向，不把它通过当完整证明。真实两root构造、唯一approvals/engine、独立gateway/lease owner、关闭/清理/probe/取消均回归。
5. Linux quotation白名单仅增加上述两个共享.py，排除AppleDouble且只在quotation stage携带；原A默认stage/入口/Dockerfile依赖与全部资源预算不变。最终重跑原实际factory Linux完整链；不安装依赖、扩大挂载或借旧image成功替代当前代码。
6. 代价：新增一个非进程库与三字段统一文件门；缺端口从部分可读变整组503；新增两处锁往返及内部回调接缝；移动装配代码影响测试patch/import路径与打包。若共享提取带来隐式实例复用或锁检查错误，会影响两进程运行，须以真实构造/PG/清理/打包回归防护。只修本轮三项，T10全量验收与原静态债务不在此冒充完成。
7. 共享库纳入原59加新2共61生产文件mypy；apps当前namespace导致同一模块双名时，保留原报错并增加--explicit-package-bases正确绑定当前工作树，其他参数/文件不缩减，不新建apps/__init__或ignore。公开重导出可用__all__保持同class及全部既有出口，不为Ruff添加无效重复别名；该调整只明确模块解析，需原签名/导入与完整类型检查证明。
