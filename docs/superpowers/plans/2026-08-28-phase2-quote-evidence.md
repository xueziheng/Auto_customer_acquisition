# Task 8A：有界来源读取与受限取证 Implementation Plan

> **实施状态（2026-08-28）：下层实现及独立复审完成，最终提交6938804。** 六切片原实现2646908经首审发现四项Important，Fix1修复后逐项复审通过；新增一项非阻塞测试失败清理Minor留最终审查。最终固定Linux资源48项、Linux/PG真实域同链264项、受影响兼容361项均通过，实际API/worker、真实商业资料仍未运行。下文资源预算与切片是实施要求/历史执行方案，不是生产默认值，也不代表整个Phase2完成。

> **For agentic workers:** REQUIRED SUB-SKILL: 遵守控制器SDD委派合同，使用 superpowers:test-driven-development 与 superpowers:verification-before-completion 逐切片完成实现与验证，不停留在计划。控制器在Task8A完整交付后安排一次独立审查；实现者不启动自己的reviewer或子代理。步骤以复选框追踪。

**Goal:** 实现真实raw metadata→有界对象读取→Linux受限解析→版本化定位→资料ACL/Gateway→两个域reader的可独立测试下层链，不声称API/worker已上线。
**Architecture:** additive bounded Store/transport；离线connector只解析，中立DTO只承载事实，域保留业务判断，workflow适配公开端口，Gateway仅加插件。所有原件IO在Gateway EXECUTING提交之后、业务锁之外。
**Tech Stack:** 既有backend Python3.12+不原地升级；两个新解析profile固定Linux CPython3.12.14+pypdf==6.16.2；Pydantic v2、SQLAlchemy/Postgres、既有boto3与标准库email/resource/asyncio；testcontainers仅用于真实测试环境。
**Spec:** `docs/superpowers/specs/2026-08-28-phase2-costing-quotation-design.md`；主计划`docs/superpowers/plans/2026-08-28-phase2-costing-quotation.md`的Task8A。本文已收束来源契约、用途隔离与资源裁定，运行环境和精确测试预算见§9；旧预查中的额外CRM门、pypdf dev和未指定Linux全链环境描述不再适用。

## 0. 前置与不可改变的边界

- T8A在T7审查通过后实施；控制器先核对T3A/T3B/T4–T7真实交付和本brief公共签名，不把尚未交付的接口当现成代码；按控制器生产写入独占安排派发，不与其他任务实现者并行改生产。
- 先读根AGENTS/HANDBOOK和所进目录全部AGENTS；本任务不改变九条边界。金额/单位不推断、不换算；不扩大旧Need词表、完整度、成本/CRM/原件角色。
- 无迁移；不改T4–T7表、receipt、scope、文件流程；T6 get_meta_by_key仅生成metadata恢复，不代替bounded Raw读取。
- T8B负责实际API/worker factory、所有HTTP（含预览/定位/单位确认）、真实NeedUnitAuthorizer装配、文件Gateway、expiry及全接线；T8A提供下层端口和受控真实链验收。
- 无真实客户/供应商请求、无生产秘密、无模型/OCR/网络解析、无通用沙箱/权限/事务框架。非Linux、非CPython3.12.14或pypdf不等6.16.2均parse_unavailable；`$`无需parser但仍须资料ACL+bounded完整性核验。此Python限定不改变T7 renderer或旧backend环境。
- 所有运行资源值必填正int且排bool；代码不能填生产限额。测试fixture使用§9精确已裁定预算，只为目标边界显式覆盖字段；不复制为部署默认。缺依赖/探针未成功时fail closed。

## 1. 文件责任（只增加窄能力，不重构大service）

| 文件 | 改动与责任 |
|---|---|
| 新`shared/schemas/evidence_read.py`、`shared/evidence_read.py` | 本文strict DTO/错误/纯locator编码及中立读取端口；无IO/业务规则 |
| 改`artifact_store/{transport,store,service_impl,errors}.py` | additive bounded能力、固定限额/缺能力错误；旧get/put构造语义保留 |
| 新`connectors/object_store/bounded.py`；改本目录AGENTS | 惰性、专属有界S3读取；不改旧S3取消/补偿行为；记录新限额错误例外 |
| 新`connectors/evidence_text/AGENTS.md`及`{__init__,manifest,client,worker,profiles,probe}.py` | 固定离线profile、Linux一次性worker、资源探针、取消回收；无数据库/业务授权 |
| 新`infra/quote_evidence_artifacts.py`、`infra/quote_evidence_settings.py`、`infra/db/quote_evidence_context.py` | Store中立metadata/bytes映射；纯非秘密配置；tenant-bound员工/消息/Need数量投影SQL |
| 新`domains/demand/unit_source_validation.py`；改`domains/demand/service.py` | 仅新增公开纯数量/原文必要条件验证函数，不改变T3A服务或旧词表 |
| 新`domains/costing/source_access.py`、`domains/conversations/source_access.py`；改两域`service.py` | 两个窄纯授权函数，分别复用成本矩阵和既有boss-only入站阅读下限；不是共享授权框架 |
| 新`workflows/quote_approval/{source_access,source_readers}.py` | 编排当前域权限/WorkIntake/引用投影；Gateway结果显式映射两个既有域reader |
| 新`tool_gateway/checks/quote_evidence.py`、`handlers/{quote_evidence,quote_evidence_slots}.py` | 独立LOW工具、tenant/permission检查、同task qev结果槽；不改pipeline或旧槽 |
| 新`docs/adr/0020-bounded-quotation-source-contracts.md` | 新shared契约、用途/权限、profile与Linux启用边界；0019由T6/T7占用，执行前若0020另有占用回控制器，不能覆盖 |
| 新下列测试文件及`tests/fixtures/quote_evidence/` | 仅受控PDF/RFC822和有限故障夹具；不复制真实资料/秘密 |

shared现无protocols目录，端口固定放`shared/evidence_read.py`，下文简称“shared端口”；不另建公共框架目录。
所有新测试造canonical tenant/actor/Need/message/upload/artifact ID；旧T3A的短ID/受控reader fixture不因此改写或被当作真实来源通过。

## 2. 精确共享契约

全部DTO为`BaseModel(strict=True,frozen=True,extra='forbid')`；所有可选字段仍必填None，无静默trim/coerce。ID使用现有NewType且验证canonical前缀/ULID；唯一身份兼容例外是actor_id/EmployeeId：沿T6及QuoteEmployeeFact复用shared.schemas.quote_facts.fact_identity，严格str、非空、无首尾空白/C0-C1控制、最多40字符，不新增emp_ULID要求。格式合法不授予权限，仍须真实reader核tenant/实际ID/在职及当前用途ACL。Hash为64位小写hex；aware时间规范成UTC。原文/bytes字段`repr=False`且不进入普通model_dump；若IPC需要序列化必须白名单显式取字段。

身份兼容测试除canonical正常样例外，增加真实存在的合法旧员工ID经过authorize→AuthorizedEvidenceReference→Gateway结果成功，以及不存在/失活/错tenant/撤权仍拒绝；空值、空白、bool、超40字符/控制字符在资料IO前拒绝。其他新来源/Need/tenant/消息/Artifact ID的严格格式不放松，不迁移或重写员工记录。

```python
class DTO(BaseModel):  # 本文件内部严格基座，不作为通用框架导出
    model_config = ConfigDict(strict=True, frozen=True, extra='forbid')
Hash = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
EvidenceProfile = Literal['pdf-text-v1', 'rfc822-plain-v1']
class PricingEvidenceScope(DTO):
    purpose: Literal['pricing']
class NeedUnitEvidenceScope(DTO):
    purpose: Literal['need_unit']
    need_id: ValidatedNeedId
    action: Literal['read', 'confirm']
EvidenceScope = Annotated[PricingEvidenceScope | NeedUnitEvidenceScope, Field(discriminator='purpose')]
class EvidenceRawMeta(DTO):
    tenant_id: TenantId; artifact_id: ArtifactId
    kind: Literal['pdf', 'email_raw']; mime_type: Literal['application/pdf', 'message/rfc822']
    content_hash: Hash; size_bytes: int; observed_at: datetime
class EvidenceRawContent(DTO):
    meta: EvidenceRawMeta; content: bytes  # repr=False/exclude=True，实际len/hash严格相符
class QuoteMessageReferenceFact(DTO):
    tenant_id: TenantId; message_id: MessageId; conversation_id: ConversationId
    account_id: ProspectAccountId; channel: str; direction: str; artifact_id: ArtifactId
class NeedQuantitySourceFact(DTO):
    tenant_id: TenantId; need_id: ValidatedNeedId; account_id: ProspectAccountId
    quantity: FactualField[int] | None  # 内部事实，repr=False/exclude=True
class AuthorizedEvidenceReference(DTO):
    tenant_id: TenantId; actor_id: EmployeeId; source_ref: str; scope: EvidenceScope
    raw: EvidenceRawMeta
    message_id: MessageId | None; conversation_id: ConversationId | None
    account_id: ProspectAccountId | None
class ParsedEvidenceText(DTO):
    profile: EvidenceProfile; page: int | None; text: str  # text repr=False/exclude=True
class EvidenceSelection(DTO):
    profile: EvidenceProfile; page: int | None; start: int; end: int; excerpt_hash: Hash
class EvidenceTextResult(DTO):
    reference: AuthorizedEvidenceReference; profile: EvidenceProfile | None; page: int | None
    text: str | None; text_hash: Hash | None  # text原文，仅本调用内；repr=False/exclude=True
    selection: EvidenceSelection | None; excerpt: str | None; locator: str | None
    # excerpt同样repr=False/exclude=True；verify('$')其余解析字段全None、locator='$'
```

source_ref只接受`upload:upl_<ULID>`或`message:msg_<ULID>`；pricing只upload/raw PDF，need_unit只message/raw EMAIL_RAW。upload授权结果message/conversation/account均None，不把WorkUpload可选历史scope当已验证归属；消息结果三项均必填，Need/account另比对。size_bytes为正int；严格校验kind/MIME对应关系。

```python
class EvidenceVerifyRequest(DTO):
    operation: Literal['verify']; source_ref: str; scope: EvidenceScope; locator: str
class EvidencePreviewRequest(DTO):
    operation: Literal['preview']; source_ref: str; scope: EvidenceScope
    profile: EvidenceProfile; page: int | None
class EvidenceLocateRequest(DTO):
    operation: Literal['locate']; source_ref: str; scope: EvidenceScope
    profile: EvidenceProfile; page: int | None; start: int; end: int
    expected_raw_hash: Hash; expected_text_hash: Hash
EvidenceReadRequest = Annotated[EvidenceVerifyRequest | EvidencePreviewRequest | EvidenceLocateRequest,
                                Field(discriminator='operation')]
```

verify和locate返回完整受限页/正文及选区，preview返回完整受限页/正文但selection/excerpt/locator为None。无任意URL/对象键/路径/tenant/actor/role/原文参数。`$`只verify+pricing合法；政策/FX业务调用已固定使用它，价格费用仍须片段locator，reader不能替调用域证明用途。

shared端口（所有async方法显式类型，缺失记录返回None而非伪造空DTO）：

```python
class QuoteEvidenceRawReader(Protocol):
    async def get_meta(self, tenant_id: TenantId, artifact_id: ArtifactId) -> EvidenceRawMeta: ...
    async def read(self, tenant_id: TenantId, artifact_id: ArtifactId, *, maximum_bytes: int) -> EvidenceRawContent: ...
class QuoteEvidenceContextReader(Protocol):
    async def read_actor(self, tenant_id: TenantId, actor_id: EmployeeId) -> QuoteEmployeeFact | None: ...
    async def read_message(self, tenant_id: TenantId, message_id: MessageId) -> QuoteMessageReferenceFact | None: ...
    async def read_need_quantity(self, tenant_id: TenantId, need_id: ValidatedNeedId) -> NeedQuantitySourceFact | None: ...
class QuoteEvidenceAccess(Protocol):
    async def authorize(self, tenant_id: TenantId, source_ref: str, *, actor_id: EmployeeId,
                        scope: EvidenceScope) -> AuthorizedEvidenceReference: ...
class EvidenceTextParser(Protocol):
    def capability(self) -> EvidenceParserCapability: ...
    async def parse(self, content: bytes, *, profile: EvidenceProfile, page: int | None) -> ParsedEvidenceText: ...
class QuoteEvidenceReader(Protocol):
    async def read(self, tenant_id: TenantId, request: EvidenceReadRequest, *, actor_id: EmployeeId) -> EvidenceTextResult: ...
```

纯locator API：`parse_evidence_locator(locator:str)->EvidenceSelection|None`（只有`$`返回None）；`make_evidence_locator(selection:EvidenceSelection)->str`；`select_evidence_text(parsed:ParsedEvidenceText,start:int,end:int,*,maximum_excerpt_bytes:int)->tuple[EvidenceSelection,str]`。没有业务单位/金额解释。

## 3. 固定profile和错误

- PDF canonical：`pdf-text-v1:p=1;c=20:140;h=<hash>`；RFC822：`rfc822-plain-v1:c=0:19;h=<hash>`。十进制坐标canonical（0或非零开头，无+号/前导0）；p从1开始，`0<=start<end<=len(text)`；Python Unicode code point，不是UTF-16/byte/像素。范围超出或excerpt hash不等均拒绝。
- 两个profile均限定Linux CPython3.12.14+pypdf6.16.2，版本不合parse_unavailable，不能自动升级或仅按主次版本通过；版本检查先于解析。PDF用`PdfReader(...,strict=True)`、`extract_text(extraction_mode='plain',orientations=(0,90,180,270))`；只CRLF/CR→LF，不trim、合并空白、Unicode规范化或OCR。加密/损坏/扫描/空白页失败；先页数门，再指定页提取，内存/CPU护栏处理解压过程中爆量。后续升级需新profile或先证明完全兼容再明确ADR，不能悄悄复用旧定位。
- RFC822保留旧`apps/scheduler_worker/adapters/rfc822.py:32`的MIME选择策略，但新profile显式严格解码（不跨apps import，不改旧实现）：`BytesParser(policy=policy.default)`，walk第一个非attachment、非空text/plain；必须`get_content(errors='strict')`按charset/transfer编码解码。标准库默认errors='replace'不适用证据；未知charset、非法字节/UnicodeDecodeError、非字符串/无可用plain固定parse_unsupported，不能替换字符后核验。HTML-only不剥标签，只统一换行；subject不输出；正文完整不截断。RFC822也必须受限worker，不能在主event loop解析。
- 页/正文UTF-8 bytes<=maximum_text_bytes，选区UTF-8 bytes<=maximum_excerpt_bytes；超限拒绝，不截断后验证。完整raw hash、完整页/正文text_hash、excerpt_hash三者分别SHA-256，不能互代。

`shared.schemas.evidence_read.QuoteEvidenceError(ConnectorError)`仅接受以下Literal code、固定中文消息、`is_retryable=False`；未知code构造拒绝，禁止自由message/context/异常链。此能力不自动重试，网络瞬断仍在Gateway正确归为临时类。
别名`QuoteEvidenceErrorCode`精确为下表十二个code的Literal联合；构造签名`QuoteEvidenceError(code:QuoteEvidenceErrorCode)`，无其他参数。Artifact两种新增错误同样固定消息/非重试。

| code | 固定消息 | Gateway类别 |
|---|---|---|
| invalid_input | 来源请求不合法 | VALIDATION |
| permission_denied | 当前无权读取来源 | PERMISSION_DENIED |
| source_unsupported | 不支持此来源 | PROVIDER_PERMANENT |
| source_unavailable | 来源暂不可用 | PROVIDER_TRANSIENT |
| source_limit_exceeded | 来源大小超过限制 | PROVIDER_PERMANENT |
| source_integrity_failed | 来源完整性核验失败 | PROVIDER_PERMANENT |
| parse_unavailable | 受限解析能力不可用 | PROVIDER_PERMANENT |
| parse_unsupported | 来源内容不可按此格式解析 | PROVIDER_PERMANENT |
| parse_timeout | 来源解析超时 | PROVIDER_PERMANENT |
| parse_limit_exceeded | 来源解析超过资源限制 | PROVIDER_PERMANENT |
| locator_mismatch | 来源定位不匹配 | PROVIDER_PERMANENT |
| gateway_unavailable | 来源工具审计不可用 | PROVIDER_TRANSIENT |

不存在/跨tenant/未经授权引用统一permission_denied，不枚举资料。其他资料库故障source_unavailable；原始异常`from None`并禁止打印locals/内容。constructor输入问题invalid_input；worker未知退出/协议损坏parse_unavailable，不用空文本表示成功。

## 4. bounded Store/S3和纯metadata适配

```python
# artifact_store/transport.py
class BoundedObjectBlobTransport(Protocol):
    async def get_bounded(self, object_key: str, *, maximum_bytes: int) -> bytes: ...
class BlobReadLimitExceeded(TradeOSError): ...  # 固定“Artifact 对象超过读取限制”，非重试
# artifact_store/store.py
class BoundedRawArtifactStore(Protocol):
    async def get_meta(self, tenant_id: TenantId, artifact_id: ArtifactId) -> RawArtifactMeta: ...
    async def get_bounded(self, tenant_id: TenantId, artifact_id: ArtifactId,
                          *, maximum_bytes: int) -> tuple[RawArtifactMeta, bytes]: ...
```

RawArtifactStoreImpl旧五个构造参数保持，新增keyword-only `bounded_transport:BoundedObjectBlobTransport|None=None`；None仅表示旧兼容，get_bounded抛新增ArtifactBoundedReadUnavailable（固定“Artifact 有界读取不可用”），绝不fallback。新增ArtifactReadLimitExceeded（固定“Artifact 超过读取限制”）映射预检/transport超限。
get_bounded先短metadata事务结束，`limit=min(caller maximum_bytes,store maximum_bytes)`；meta.size>limit零IO拒绝。实际transport限制`min(limit,meta.size_bytes)`且只额外读1哨兵；读完严格核完整len/hash，重用ArtifactIntegrityError及固定告警，返回真实meta+bytes。

shared DTO `ObjectReadLimits(connect_timeout_ms:int,read_timeout_ms:int,total_timeout_ms:int,chunk_bytes:int,maximum_attempts:int)`；所有必填，maximum_attempts包括首次调用。source专用`S3BoundedObjectBlobTransport(settings:S3ObjectStoreSettings,secret_resolver:ObjectStoreSecretResolver,*,limits:ObjectReadLimits)`构造只保存依赖，零resolver/SDK/网络；get_bounded执行阶段才解析凭证/建client，固定Config连接/读timeout及total_max_attempts。
有限read(n)、累计<=limit+1、finally关闭body/client，超限typed错误在宽泛SDK异常之前保留；NoSuchKey走既有not-found，其余SDK错误固定TransientError。总deadline覆盖初始化/重试/流循环；循环前后检查，SDK等待受连接/读timeout及有限attempts约束，不承诺Python可强杀SDK线程。取消设置本次stop标记，在有限读返回后退出并close，等待线程收口后传播取消；不得改旧`_shielded_thread`/put/delete补偿。
`RawQuoteEvidenceAdapter(store:BoundedRawArtifactStore)`实现shared RawReader，仅映射真实meta.kind/mime/content_hash/size_bytes/uploaded_at；observed_at是取得原件时间，不是供应商报价/客户发送时间。仅支持两种raw kind，绝不把generated映射为raw。infra不含ACL/SDK/locator。

## 5. Linux受限进程、能力预检与启用机制

`EvidenceParseLimits`完整字段：maximum_input_bytes、maximum_pages、maximum_text_bytes、maximum_excerpt_bytes、cpu_seconds、address_space_bytes、wall_timeout_ms、maximum_result_bytes、maximum_concurrency、queue_timeout_ms、termination_grace_ms，全部必填正int排bool。要求maximum_excerpt_bytes<=maximum_text_bytes；结果帧含JSON转义的实际字节数单独限额，不用字符数替代。
`EvidenceProbeLimits`：cpu_seconds、address_space_bytes、wall_timeout_ms、allocation_chunk_bytes、maximum_probe_bytes、maximum_result_bytes、termination_grace_ms，同样必填。探针有独立有限预算，不能为了测试生产大上限分配任意内存；probe内存分配总量不超过maximum_probe_bytes，若不足以证明限制有效则失败，不假成功。
`EvidenceParserCapability`只有`status:Literal['available','unavailable']`、`profile_version:Literal['evidence-worker-v1']`、`limits_hash:Hash`、`runtime_hash:Hash`、`failure:Literal['platform','resource','protocol','runtime']|None`；无原件/路径/环境字段。公开报告不是授权票据，不能从HTTP传回激活parser。
离线`manifest.py`采用现`connectors.base.ConnectorManifest`，connector_id='evidence_text'、capabilities=('evidence.pdf_text_v1','evidence.rfc822_plain_v1')、secret_refs=()，中文说明Linux资源门控/纯离线；不改尚为骨架的ConnectorRegistry或通用Connector生命周期来迁就本任务。

```python
class LinuxEvidenceTextParser:
    def __init__(self, *, limits: EvidenceParseLimits, probe_limits: EvidenceProbeLimits) -> None: ...
    async def probe(self) -> EvidenceParserCapability: ...
    def capability(self) -> EvidenceParserCapability: ...
    async def parse(self, content: bytes, *, profile: EvidenceProfile, page: int | None) -> ParsedEvidenceText: ...
    async def aclose(self) -> None: ...
```

构造/capability为纯本地检查，不启动进程/网络/读秘密，初始unavailable。受信应用startup显式await probe（T8B接）；精确Linux CPython3.12.14+pypdf6.16.2及探针全部通过后仅当前parser实例私有状态可启用。失败/取消清掉成功状态；再次probe先降为unavailable并禁止已有请求混入；aclose停止新请求并取消/回收子进程。不能注入ready=True/外部capability或用isinstance/hasattr当验收。
探针只运行固定受信模块的本地固定能力动作，无客户payload、不访问RawStore/secret resolver/socket。分别证明RLIMIT_CPU真实终止CPU工作、RLIMIT_AS在有限触页分配中触发MemoryError/受控限制、墙钟能够kill回收、IPC超限能关管道回收；期待明确exit/signal，父超时不算CPU证明，系统OOM杀死也不算AS证明。set/getrlimit仅是前置，不是成功证据。Darwin/不支持的Python实现或版本立即unavailable不spawn；固定worker另核pypdf版本，错误也不解析。
固定sys.executable `-I -m connectors.evidence_text.worker`，使用已安装本项目包，不接受客户端模块/程序/工作目录/PYTHONPATH；导入失败关闭。父以全新非秘密环境白名单启动，不继承DB/S3/Token/HOME代理等变量，原文/ID/路径不进argv；不接受exec字符串。
worker启动用最小标准库，取得有界控制头后在读内容/导入pypdf前设置CPU/AS限制（soft/hard均有限）并关闭core dump；仅parse协议对生产client开放。probe是私有固定模块入口，不出现在manifest/HTTP/生产parse IPC的mode枚举；测试fault入口只在tests中，不能公开任意代码注入。
IPC采用定长长度头+strict JSON控制头+有限raw bytes，父/子读写均有限；stdout只一个有界结果帧，stderr丢弃且不打原文，超长frame头立刻终止，不无界communicate。parent验证profile/page/形状/UTF-8/text上限；不能信worker自述原件hash。
实例Semaphore限制maximum_concurrency；排队最多queue_timeout_ms，超时parse_timeout；只在获取名额后spawn，计墙钟至进程退出/内容收完；caller取消或超时terminate→等待termination_grace_ms→kill→wait。finally释放名额/关闭pipes/清引用，所有子进程实际回收后才结束；不留下background task或共享原文process pool。
limits_hash绑定两套限额完整字段+profile；runtime_hash绑定Python完整版本、pypdf精确版本、worker协议版本与固定模块内容hash。缓存只存本进程实例；运行时发现限额设置失败/协议异常则降为unavailable，下次需重新显式probe。CPU/AS/页/文本超限是本文件拒绝，不伪造商业内容；普通子进程不宣称是任意代码执行沙箱。

## 6. 资料ACL与两个域reader

新`SqlAlchemyQuoteEvidenceContextReader(session_factory,*,statement_timeout_ms:int)`实现shared ContextReader，每次短事务tenant SELECT，无FOR UPDATE/SHARE、无跨域repository。员工用既有QuoteEmployeeFact全字段；消息JOIN Conversation两边tenant且只投影ID/channel/direction/raw_ref；Need仅读account与quantity完整FactualField。非法/多个绑定固定source_unavailable，不把原JSON异常透出；无bytes、业务规则或角色过滤SQL。
新纯域函数，service.py显式重导出：`costing.require_pricing_source_access(tenant_id:TenantId,actor:QuoteEmployeeFact)->None`核tenant/active，再复用Phase1CostingAuthorizer+EVIDENCE_CONFIRM（四成本角色）而不新增CRM gate；`conversations.require_inbound_source_access(tenant_id:TenantId,actor:QuoteEmployeeFact,message:QuoteMessageReferenceFact)->None`核当前boss、tenant、真实email+inbound，保持现API下限。它们不读库、不拓展旧router权限。

```python
class QuoteEvidenceAccessImpl:  # workflows/quote_approval/source_access.py
    def __init__(self, contexts: QuoteEvidenceContextReader, raw: QuoteEvidenceRawReader,
                 uploads: WorkIntakeService, need_authorizer: NeedUnitAuthorizer) -> None: ...
    async def authorize(self, tenant_id: TenantId, source_ref: str, *, actor_id: EmployeeId,
                        scope: EvidenceScope) -> AuthorizedEvidenceReference: ...
class GatewayPricingEvidenceReader:  # source_readers.py
    def __init__(self, reader: QuoteEvidenceReader) -> None: ...
    async def read_verified(self, tenant_id: TenantId, source_ref: str, locator: str,
                            *, actor_id: EmployeeId) -> SourceEvidence: ...
class GatewayNeedUnitEvidenceReader:
    def __init__(self, reader: QuoteEvidenceReader, access: QuoteEvidenceAccess,
                 contexts: QuoteEvidenceContextReader, need_authorizer: NeedUnitAuthorizer) -> None: ...
    async def read_verified(self, query: NeedUnitEvidenceQuery) -> VerifiedNeedUnitEvidence: ...
    async def authorize_reference(self, tenant_id: TenantId, need_id: ValidatedNeedId,
                                  actor_id: EmployeeId, source: VerifiedNeedUnitEvidence) -> None: ...
```

Access先读当前actor；pricing委托成本函数→`WorkIntake.get_upload(tenant,upl,actor)`→真实raw metadata，且upload.employee/tenant/ID再次精确核对。boss不能代读别人的upload；源引用支持矩阵不包含供应商消息。领域原操作（policy boss-only等）仍由原CostingQuoteService检查，source专用权限不批准任何业务写入。
need_unit委托`NeedUnitAuthorizer.check(...,action=scope.action)`核真实Need/account/actor/机会→read_message→会话域boss-only函数→raw metadata，再核Need/account/message/raw kind/MIME一致。四成本角色不能借pricing用途读message；Need用途不以成本路径名授予确认权。缺Need authorizer拒绝，不用缺unit的新报价context反过来阻断首次确认。
authorize每次metadata-only，prepare/返回前都新查；IO期间无员工/机会/Need/成本业务锁。返回前对比全部引用绑定（tenant/actor/scope/artifact/raw hash/size/message/account），变化拒绝；当前员工变inactive或权限丢失拒绝。
Pricing adapter自选`PricingEvidenceScope(purpose='pricing')`，不接外部purpose，精确映射SourceEvidence：tenant/source_ref/artifact_id/raw.content_hash/locator/raw.observed_at/source_type='upload'/source_url=None；不携body、不声称已验证签名没有的Need/opportunity。业务绑定由既有调用域/context保证。
Need adapter自选`NeedUnitEvidenceScope(purpose='need_unit',need_id=query.need_id,action='confirm')`；独立check读取本次真实account/quantity，与query完整数量hash相等才进入Gateway。Gateway只收到message source_ref+safe scope+locator，绝无query.quantity/source_quote。
返回后重查当前Need数量投影和权限；调用新纯域`validate_need_unit_source_text(query:NeedUnitEvidenceQuery,current:NeedQuantitySourceFact,*,body:str,excerpt:str)->None`，再逐字段构造既有VerifiedNeedUnitEvidence（tenant/need/account/msg/artifact/raw hash/locator/exact excerpt/unit/quantity hash/observed_at）。没有确认人，确认时间与Provenance仍由既有NeedUnitService在后续guard+事务中产生。
该纯函数只落已批准最小子集：query/current绑定和`quantity_fact_hash`完整相等；quantity为正int排bool、CONVERSATION、已human_confirmed、source_id恰当前msg；原quantity.provenance.source_quote非空且逐字在body中；excerpt恰query.source_quote且是Gateway核验片段。
整数/单位必要条件采用notes允许的明确相邻“数量 单位”形式：excerpt用`[0-9]+`找全部数字段，必须恰一段且逐字等于`str(quantity.value)`；含其他Unicode十进制数字拒绝。数值段左右不能紧邻字母/数字/下划线或`.,+-`，其后必须有一个或多个Unicode空白字符（Python re空白字符类）再接逐字unit，unit末端不能紧邻字母数字/下划线；同一literal unit作为完整token重复出现也拒绝。明确多数字/重复候选均source_mismatch。不casefold、不单复数/别名、不换算、不支持千分位/小数/科学计数。选区长度仍由资源限额控制。
数量与单位的token边界必须落在已验证locator的原文精确位置，而不只看截取后的excerpt。纯函数保持现签名，复用shared.schemas.evidence_read.parse_evidence_locator(query.locator)取得code point坐标，确认非根的rfc822-plain-v1/page=None、范围在body内且body[start:end]逐字等于excerpt；选中片段内的数字/单位offset加start后检查body中两侧字符，沿上句同一边界规则。不得用body.find或别处存在同文字替代本次坐标；所有无效绑定/裁剪均source_mismatch。增加150 pieces→选50 pieces、50 piecesXYZ→选50 pieces，以及原文另有合法同文字但实际选区仍裁剪的反例；完整50 pieces及非BMP前缀仍成功。不扩大为语义分析、单位词典或新授权，不改变NeedUnitService/receipt/旧词表。
这里只验证必要词法关系，不声称能识别所有不同单位词、否定、历史引用、其他产品或包装上下文；含糊关系须由员工拒绝并补证，确认动作承担语义责任。不建单位词典/语义解析器；精确token边界与反例在切片5先RED锁定，超出子集不扩展支持而回控制器。
历史authorize_reference使用scope.action='read'，只重验当前Need/account/msg/raw metadata和receipt artifact/hash/observed_at绑定，零bytes/parser；不要求当前quantity等于旧receipt，也不恢复stale单位。现NeedUnitService负责receipt本身及旧幂等；新reader不能改其行为。
Need错误映射：权限→既有NeedUnitPermissionError('permission_denied')；unsupported→NeedUnitError('source_unsupported')；摘录/绑定/token/完整性→NeedUnitError('source_mismatch')；源/解析资源/Gateway不可用→NeedUnitUnavailableError('source_unavailable')。旧CostingQuoteService将reader失败统一安全InvalidPricingEvidenceError，T8B不假定它已经暴露细粒度新code。

## 7. Gateway插件、qev槽与下层装配输出

manifest `quotation.evidence.read` version='v1'、LOW/FREE、requires_approval=False、NONE、required_permissions=('quotation:evidence_read',)、checks恰('tenant','permission')；strict输入为§2三操作union，output仅provider_ref。不存在文件生成/发送授权。
`QuoteEvidenceTenantCheck(tenant_id:TenantId)`核本实例tenant、canonical actor承载ID与strict请求（无外部IO）；`QuoteEvidencePermissionCheck(access:QuoteEvidenceAccess,slot:QuoteEvidenceResultSlot)`调用authorize，写本调用state.preflight（不跨task缓存）。check中的已分类错误也先put_failure固定code再抛匹配ToolGatewayError，不能丢失source_unsupported/source_unavailable。handler.prepare再授权+绑定对比，只metadata/能力快照/HMAC，不触bytes/parser/probe/凭证，失败同样写固定code并抛匹配错误。
TenantCheck唯一两个拒绝rule固定为`tenant:evidence_request`（请求/actor格式不合法，固定“来源请求不合法”）与`tenant:evidence_tenant`（本实例租户不匹配，固定“当前无权读取来源”）；wrapper无细码fallback只识别这两个精确值，未知rule不猜原因。
`QuoteEvidenceReadHandler(access:QuoteEvidenceAccess,raw:QuoteEvidenceRawReader,parser:EvidenceTextParser|None,slot:QuoteEvidenceResultSlot,fingerprints:HmacFingerprintProvider,*,maximum_raw_bytes:int,parser_limits:EvidenceParseLimits)`沿既有`prepare(ctx:ToolCallContext,preflight:object|None)->PreparedToolCall`与`execute(tenant_id:TenantId,prepared:PreparedToolCall)->Mapping[str,SafeScalar]`异步签名；parser可为None仅允许`$`，其余须capability.available否则parse_unavailable。prepare对parse请求以min(maximum_raw_bytes,parser_limits.maximum_input_bytes)核metadata大小，execute仍实际有界读取。HMAC输入版本+tenant+ctx.user_id+三操作全部字段（含purpose/action/need_id、None、范围、expected hashes）+授权raw artifact/hash，使用稳定JSON bytes；audit_projection仅安全ID、operation/profile/purpose，不放locator/文本/对象键。
execute在Gateway已持久EXECUTING后read raw→与prepare metadata相等→需要时parser→核profile/page/text hash/选区，locate另核两个expected hash并生成canonical locator→再次authorize且全绑定相等→put成功结果。`$`只hash，不parse；preview/locate每次后台重新读，不信客户端抽字/坐标。

```python
class QuoteEvidenceResultSlot:
    def __init__(self, id_factory: Callable[[str], str]) -> None: ...
    @property
    def is_empty(self) -> bool: ...  # 仅当前task；wrapper据此防同task重入
    def put(self, result: EvidenceTextResult) -> str: ...  # qev_<ULID>，容量一
    def take(self, handle: str) -> EvidenceTextResult: ...
    def put_failure(self, code: QuoteEvidenceErrorCode) -> None: ...  # 只固定code，绝无原文
    def take_failure(self) -> QuoteEvidenceErrorCode | None: ...
    def discard_all(self) -> None: ...
class ToolGatewayQuoteEvidenceReader:
    def __init__(self, gateway: QuoteEvidenceGatewayInvoker, slot: QuoteEvidenceResultSlot,
                 access: QuoteEvidenceAccess) -> None: ...
    async def read(self, tenant_id: TenantId, request: EvidenceReadRequest,
                   *, actor_id: EmployeeId) -> EvidenceTextResult: ...
# QuoteEvidenceGatewayInvoker位于handler模块：async invoke(ctx:ToolCallContext)->ToolCallResult
```

slot单一ContextVar记录(owner asyncio.current_task,成功payload或固定failure code)，不是全局dict；take只同task同handle且立即删，子task复制上下文不能领取/删除父内容；同task嵌套read若槽非空拒绝，不清掉外层。wrapper先确认空，随后owned invocation finally清槽。
permission check、handler.prepare和execute捕获已分类失败时只put_failure(code)，然后抛§3匹配ToolGatewayError；原文绝不进入failure分支。wrapper只在真实SUCCEEDED后take原文；任何非SUCCEEDED先take_failure（只有固定code，若槽存成功payload则返回None，绝不领取/删除该payload），有code则保留。没有code才按固定Gateway类别退化：VALIDATION→invalid_input、PERMISSION_DENIED→permission_denied、PROVIDER_TRANSIENT→source_unavailable；REJECTED无类别时仅`tenant:evidence_request`→invalid_input、`tenant:evidence_tenant`→permission_denied，其他未知类别/规则或DUPLICATE→gateway_unavailable。不能把所有REJECTED一概当permission_denied。
任何审计/ledger失败均不得领取成功内容。invoke抛异常或返回明确RECONCILIATION_REQUIRED时固定gateway_unavailable；但既有ToolCallResult不投影stage，EXECUTING提交失败可被核心管线归为PROVIDER_TRANSIENT且没有qev细码，此不可辨别路径沿上句固定fallback返回source_unavailable，不声称故障发生在外部来源。不得为细化错误改pipeline、扩展结果Protocol或读取历史ledger猜stage。真实PG用例必须证明该路径零raw/parser调用、无成功交付、单次invoke且无自动重试。取消传播CancelledError；finally均清槽。上述固定fallback和错误表由本插件单一纯映射函数复用，不改核心错误管线。真实失败细码传递只依赖当次task，不从历史ledger恢复。
wrapper按本次actor构造`ToolCallContext(tenant_id,UserId(str(actor_id)),'quotation.evidence.read',safe_params,run_id=None)`；沿已有内部EmployeeId承载约定，不造usr/run/defaultboss。拿到成功typed结果后再次access.authorize并核对，才交付domain adapter；所有路径finally清理，无跨调用缓存或ledger原文重放。
新`infra/quote_evidence_settings.py`仅`QuoteEvidenceSettings(raw_maximum_bytes:int,object_read:ObjectReadLimits,parser:EvidenceParseLimits,probe:EvidenceProbeLimits)`严格DTO，及`from_mapping(Mapping[str,object])->QuoteEvidenceSettings`纯解析；不读os.environ/凭证/文件，未知字段拒绝。与旧S3秘密引用配置分开。
T8B消费本节各明确ctor/Protocol，自建独立ToolRegistry/Gateway并注册manifest/handler、两个stage；厂内不得await probe/解析秘密/访问网络，startup显式本地probe后启用parse路径。无成功probe只可root能力或整体不注册，不可默默让文本工具走无限解析。Raw S3 connector惰性解析秘密在已授权EXECUTING后。

## 8. TDD切片与提交（同一Task8A，全部审完才交T8B）

命令中的真实profile/worker与全链测试必须在§9指定的Linux CPython3.12.14运行器执行；宿主只编排testcontainers及可独立运行的纯契约测试，不原地升级本机backend。下面的pytest路径也用于容器内选择用例；新增`tests/integration/quote_evidence_linux_support.py`负责两种受控环境，完整chain入口`tests/integration/test_quote_source_readers_linux.py`负责宿主调度真实容器内用例。构建/测试输出只固定安全结果。

### 切片1：中立契约、固定locator、additive bounded Store

Files：§1 shared/Store/infra metadata适配；新`tests/unit/test_quote_evidence_contracts.py`、`tests/unit/test_raw_artifact_bounded.py`、`tests/integration/test_raw_artifact_bounded.py`。

```python
async def test_missing_bounded_transport_does_not_fall_back(legacy_store, transport_spy, tenant, artifact):
    with pytest.raises(ArtifactBoundedReadUnavailable):
        await legacy_store.get_bounded(tenant, artifact, maximum_bytes=32)
    assert transport_spy.get_calls == 0
def test_locator_has_codepoint_coordinates():
    parsed = ParsedEvidenceText(profile='pdf-text-v1', page=1, text='A😀B')
    selection, excerpt = select_evidence_text(parsed, 1, 2, maximum_excerpt_bytes=4)
    assert excerpt == '😀'
    assert parse_evidence_locator(make_evidence_locator(selection)) == selection
```

- [ ] 新建显式fixture，补wrong ID/kind/MIME、bool/负限额、非canonical locator、UTF-8上限±1、repr/model_dump无文本的RED。
- [ ] 运行`python3 -m pytest tests/unit/test_quote_evidence_contracts.py tests/unit/test_raw_artifact_bounded.py -q`，记录目标行为失败；仅import错误不算业务RED。
- [ ] 按§2–4实现，真实PG raw metadata＋受控transport覆盖tenant隔离、实际对象比meta大、hash/len错、预检零IO、查询事务先退出；运行同unit及`python3 -m pytest tests/integration/test_raw_artifact_bounded.py -q`。
- [ ] GREEN后提交`feat: 增加有界原件读取与报价来源契约`；不改旧Raw/Generated幂等和T6恢复。

### 切片2：来源专用惰性S3有限流

Files：`connectors/object_store/bounded.py`及本目录AGENTS；新`tests/unit/test_quote_evidence_s3.py`、`tests/integration/test_quote_evidence_s3.py`。

```python
class FiniteBody:
    def __init__(self, data): self.data, self.closed = data, False
    def read(self, n):
        assert type(n) is int and n > 0
        result, self.data = self.data[:n], self.data[n:]
        return result
    def close(self): self.closed = True
async def test_lying_object_is_cut_off(s3_reader, oversized_body):
    with pytest.raises(BlobReadLimitExceeded):
        await s3_reader.get_bounded(TEST_RAW_KEY, maximum_bytes=4)
    assert oversized_body.closed
```

- [ ] RED `python3 -m pytest tests/unit/test_quote_evidence_s3.py -q`：构造resolver/SDK零调用；read(n)有限、limit/limit+1、ContentLength伪报、socket慢读/取消、有限attempts、deadline、永久错误不变Transient。
- [ ] GREEN按§4实现source独立client；真实临时MinIO复用`test_artifact_store_minio.py`的固定镜像/受控密钥模式，新测试至少一次真实put→bounded read＋错hash拒绝；慢流受控HTTP/SDK body验证时间与close，不触生产S3。
- [ ] 运行同unit和`python3 -m pytest tests/integration/test_quote_evidence_s3.py tests/unit/test_artifact_store_service.py -q`；提交`feat: 增加惰性有限S3来源传输`。

### 切片3：Linux worker/profile/真实资源能力

Files：connector evidence_text全部、settings；新`tests/unit/test_evidence_text_profiles.py`、`test_evidence_parser_client.py`、`tests/integration/test_evidence_parser_linux.py`、`tests/integration/evidence_parser_linux_cases.py`、`tests/fixtures/quote_evidence/linux/Dockerfile`。

```python
async def test_parser_is_closed_until_real_probe(parser, bounded_email):
    assert parser.capability().status == 'unavailable'
    with pytest.raises(QuoteEvidenceError) as caught:
        await parser.parse(bounded_email, profile='rfc822-plain-v1', page=None)
    assert caught.value.code == 'parse_unavailable'
async def test_cancel_reaps_worker(linux_harness):
    call, started_pid = await linux_harness.start_controlled_hanging_parse()
    call.cancel()
    with pytest.raises(asyncio.CancelledError): await call
    assert not linux_harness.is_child_alive(started_pid)
```

- [ ] RED `python3 -m pytest tests/unit/test_evidence_text_profiles.py tests/unit/test_evidence_parser_client.py -q`：profile字节/坐标、PDF四类拒绝、base64/quoted-printable/charset/MIME/HTML-only、非法charset/实际非法编码bytes必须parse_unsupported而非替换字符、全正文不截断；仅测试代码可调用纯profile，不给生产绕过client入口。
- [ ] GREEN按§5实现；协议测试覆盖未知field/超长IPC/短读/错误profile、错Python实现/patch或pypdf版本parse_unavailable、env清洁、argv无内容、queue/cancel/关闭无泄漏、失败probe不缓存成功；同真worker资源函数用于probe/normal/test故障路径。
- [ ] Linux验收必须真实执行同worker：CPU限额信号（不能父timeout冒充）、AS有限触页MemoryError（不能容器OOM冒充）、wall kill、IPC超限、并发上限/排队超时、取消后无活子进程、后续合法PDF/RFC822成功。`$`在parser不可用时仍可有界完整性读取。
- [ ] 本机Mac不能靠skip算完成；按§9已裁定Python3.12.14官方index digest/arm64目标和精确fixture预算构建专用临时Linux测试镜像。实际实施允许仅在专用构建阶段获取该镜像及当前项目已列runtime/dev依赖，不装全局环境、不取生产配置；运行阶段不联网安装。纯资源容器network_mode='none'，无端口/宿主挂载/秘密环境/docker socket，执行真实同client/worker。
- [ ] 运行`python3 -m pytest tests/integration/test_evidence_parser_linux.py -q`；宿主Docker不可用或image缺失需明确not_run且Task8A未验收，不能宽松skip通过。通过后提交`feat: 增加经真实探针门控的Linux受限来源解析`。

### 切片4：资料ACL、真实metadata投影、source Gateway/qev

Files：§1 infra context、域source_access、workflow source_access、Gateway三文件；新`tests/unit/test_quote_evidence_access.py`、`test_quote_evidence_gateway.py`、`tests/integration/test_quote_evidence_gateway.py`。

```python
async def test_finance_can_read_own_upload_without_crm_gate(access, finance, own_upload):
    result = await access.authorize(finance.tenant_id, own_upload.source_ref,
        actor_id=finance.employee_id, scope=PricingEvidenceScope(purpose='pricing'))
    assert result.raw.artifact_id == own_upload.artifact_id
async def test_executing_commit_failure_has_no_raw_io(gateway_case):
    gateway_case.fail_executing_commit()
    with pytest.raises(QuoteEvidenceError): await gateway_case.read_preview()
    assert gateway_case.raw_read_count == gateway_case.parse_count == 0
```

- [ ] RED上述unit：本人上传/他人上传/boss不能越权、当前inactive、错tenant、pricing message拒绝、Need无权限拒绝、错account/outbound/generated拒绝；prepare/结束后撤权、引用替换、purpose/need_id/HMAC篡改。固定code必须分别覆盖check的source_unsupported/source_unavailable、prepare的parse_unavailable/source_limit_exceeded、execute的parse/locator失败，以及无细码REJECTED的固定fallback。
- [ ] GREEN真实SQL ContextReader＋公开WorkIntake.get_upload；NeedUnitAuthorizer在此为显式受控依赖，仅用于验证check调用/精确绑定，不能作为生产默认。Gateway按§7接真实registry/PG ledger，不用fake invoke替代。
- [ ] 真实多连接撤权屏障证明IO前后查当前身份；raw read期间另一连接能获得员工/机会/Need锁（无业务长锁）。qev同task单次领取、嵌套/子task/取消拒绝，失败槽只有固定code，审计最终失败零内容返回。
- [ ] 运行`python3 -m pytest tests/unit/test_quote_evidence_access.py tests/unit/test_quote_evidence_gateway.py tests/integration/test_quote_evidence_gateway.py -q`；ledger/output/log搜索原文sentinel必须零命中；提交`feat: 接入资料权限与报价来源Gateway插件`。

### 切片5：两个域reader与T3A真实确认闭环

Files：`domains/demand/unit_source_validation.py`及service导出、workflow source_readers；新`tests/unit/test_quote_source_readers.py`、`tests/integration/test_quote_source_readers.py`。

```python
@pytest.mark.parametrize('excerpt', ['500 pieces', '5000 pieces', '50.0 pieces', '1,500 pieces'])
def test_integer_token_never_matches_subnumber(quantity_50_query, current_50_fact, excerpt):
    query = quantity_50_query.model_copy(update={'source_quote': excerpt})
    body = current_50_fact.quantity.provenance.source_quote + '\n' + excerpt
    with pytest.raises(NeedUnitError):
        validate_need_unit_source_text(query, current_50_fact, body=body, excerpt=excerpt)
async def test_history_reauth_does_not_read_or_reconfirm(history_case):
    await history_case.change_current_quantity()
    await history_case.read_old_confirmation()
    assert history_case.raw_read_count == 0
    assert history_case.current_unit_is_stale()
```

- [ ] RED同消息/完整Provenance hash/确认来源/原quantity摘录/逐字选区；合法`We need 50 pieces.`；50≠500、小数/千分位/科学记数/字母数字嵌入、重复候选、多数字、错误单位、跨消息同前均拒绝；不把否定句语义判断写成代码事实。
- [ ] GREEN既有read_verified签名不变，Pricing安全映射，Need公开纯验证及授权重查；authorize_reference只metadata，旧receipt quantity变化可读且不激活；撤权后receipt/同key拒绝。
- [ ] 真PG+RawStore+Gateway+受控files→实际Linux parser→真实CostingQuoteService确认来源/NeedUnitService确认receipt；T3A锁/幂等代码不重写。宿主testcontainers按§9创建专用internal网络，仅临时pgvector:pg16与固定Linux测试runner，无host网络/端口/宿主volume/Docker socket；真实测试DB连接由宿主fixture生成并注入TEST_DATABASE_URL。纯资源容器仍network none；不把Mac fake parser＋另一份Linux结果合称同链。
- [ ] 宿主运行`python3 -m pytest tests/integration/test_quote_source_readers_linux.py -q`；runner内先用本测试连接迁移，再执行`python3 -m pytest tests/unit/test_quote_source_readers.py tests/integration/test_quote_source_readers.py tests/unit/test_need_units.py tests/integration/test_need_units.py -q`。真实Linux链若环境故障未跑单独not_run；结束清理精确自建容器/network，不触其他项目。
- [ ] 提交`feat: 接通成本与客户单位的受信来源reader`。

### 切片6：兼容、边界与派发交接

- [ ] 跑上述新unit/integration及旧`tests/unit/test_artifact_store_contracts.py`、`tests/unit/test_artifact_store_service.py`、`tests/unit/test_artifact_store_config.py`、`tests/integration/test_message_content_reader.py`、`tests/unit/test_tool_gateway_pipeline.py`、`tests/integration/test_tool_gateway_email_feedback.py`、`tests/integration/test_need_units.py`，精确记录文件/命令/测试数/skip；不顺手重写旧上传、EMAIL_DRAFT或分类parser。
- [ ] 运行`python3 scripts/check_boundaries.py`；对新增文件跑ruff/mypy定向检查。ADR记录新shared契约、用途scope、Linux门控和安全错误，不修改核心依赖白名单来放行违规import。
- [ ] 手动自检无raw内容model_dump/异常/日志，无apps互相import、域跨域import、Gateway/workflow import artifact_store、infra业务判权、工厂secret/network IO、可公开fault/probe参数。
- [ ] 提交`test: 验证来源读取边界并交接T8B下层端口`；向控制器交付最终真实签名、RED/GREEN、Linux镜像/探针/实际worker记录、未运行T8B/真实资料清单，等待一次Task8A完整独立审查。

验收结论必须分别陈述：有界Store/S3测试、Linux实际资源执行、真实PG/Gateway/受控原件链、实际API/worker接线（本任务not_run）、真实商业资料核验（not_run）。任何一项不能用另一项代替。

## 9. 运行环境与派发交接

本附录记录本批运行环境、资源预算和接口交接。下述OS可行性探针不代表新解析器或完整取证链已验收。

### 已核实公共接口与依据

- `artifact_store/store.py:219`为旧Raw get/get_meta；`service_impl.py:198`全量后验hash；`transport.py:21`仅旧get。T8A必须additive，不把T6生成metadata查询等同来源bounded读取。
- `connectors/object_store/s3.py:38/59/109`为等待线程取消、构造即取密钥、body.read()无界；因此来源用独立惰性bounded adapter，旧写入补偿不改。
- `workflows/employee_work_intake/service.py:49`公开get_upload；`service_impl.py:150`本人上传ACL；WorkUploadView虽有可空机会/Need字段，不代表本次资料已获业务归属核验。
- `domains/costing/service.py:268`现PricingEvidenceReader必填keyword actor_id；`quote_service.py:123`重取当前actor，权限来自Phase1CostingAuthorizer；四成本角色不额外套CRM销售范围。
- `domains/demand/service.py:52–98`现NeedUnitAuthorizer/reader/service；`schemas.py:138–170`已有完整query与verified结果；`unit_service.py:119`锁外读原文、随后guard+Need事务。T8A不改这些签名/历史行为。
- `infra/db/tables.py:2232/2259`真实Message→Conversation关系，tenant复合键；`shared/schemas/quote_facts.py:105`现中立QuoteEmployeeFact可复用；现EmployeeService.get_employee本身要actor，不能用它构造伪系统身份绕过bootstrap。
- `apps/scheduler_worker/adapters/rfc822.py:32`固定MIME选择；控制器核实标准库get_text_content默认errors='replace'，新profile必须get_content(errors='strict')，非法charset/字节固定parse_unsupported，不用替换字符改原文；旧app parser不改、不跨apps导入。
- `tool_gateway/pipeline.py:92/185/304/336`现context/result/preflight/Gateway ctor；失败result无细code/正文，新qev固定failure分支覆盖permission check、prepare、execute。wrapper任何非SUCCEEDED先取固定code，无code才按明确类别/rule退化，REJECTED不一概判无权限；成功payload在审计失败时绝不领取，不改pipeline。
- `tool_gateway/AGENTS.md`、`workflows/AGENTS.md`均未授权artifact_store导入；Store→中立raw adapter在infra，业务ACL在域函数/workflow编排。
- `tests/integration/conftest.py:102`支持TEST_DATABASE_URL测试库，Docker fallback可能skip；skip不是来源真实DB验收。测试不能打印连接值。

### 固定解析runtime与已核实官方镜像

新pdf-text-v1/rfc822-plain-v1仅Linux CPython3.12.14+pypdf6.16.2，精确版本不合parse_unavailable；不自动升级，不原地升级既有本机backend，不把该限定扩到T7 renderer。后续升级需新profile或先证明完全兼容再明确ADR，不能静默改旧定位。
来源为控制器已核实的[Python3.12.14官方发布](https://www.python.org/downloads/release/python-31214/)（2026-08-12安全版）与[Docker官方清单](https://raw.githubusercontent.com/docker-library/official-images/master/library/python)。选择该patch也包含MemoryError后解压状态修复；本文复用裁定，不另联网核验。
控制器2026-08-28先以只读imagetools inspect取得以下精确信息（当时未pull/build/run），其后已做下述单独OS可行性探针：

- image index：`python:3.12.14-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579`
- Linux arm64/v8 manifest：`sha256:457e0286fc132c4531ea071629ae6959095aa4074f172cd271aedb6950714ae6`
- Linux amd64 manifest：`sha256:4427763a1ba36f5aa8f656a03e5d00f3b8d61f5dd950c73df6c14f8c7640f8ab`
- 官方source revision：`f2c5d1b8a6adecb5b00b3c9331d4f863beade6b3`；本宿主目标arm64/v8，不用模拟amd64冒充目标验证。

Dockerfile显式ARG PYTHON_IMAGE输入上述固定index，不用mutable tag；构建记录目标manifest、最终产物image ID和所有解析后包版本。实际T8A实施已获准仅在专用构建阶段获取该官方image及当前项目已列runtime/dev测试依赖；不安装全局环境、不取生产配置。未锁上限依赖不能宣称完全可复现；运行阶段不联网安装。

控制器后续已pull上述固定官方index，镜像缓存保留；未安装或构建项目。纯OS临时容器使用network none/read-only/uid65534/cap_drop ALL/no-new-privileges、32PID/256MiB无swap/1CPU，无宿主挂载/端口，结束自动移除。实测Linux aarch64 CPython(3,12,14)，child RLIMIT_AS=32MiB后48MiB bytearray被MemoryError阻断（exit0）；CPU soft1s/hard2s、有限父5s期限下由SIGXCPU结束（returncode -24）；两个stderr均0，命令exit0。
该记录只证明目标OS限额的环境可行性，未运行新client/worker/probe/profile/原件/PG链，不替代T8A真实实现验收，也不改变下面fixture预算。该OS预检当时未创建PDF；后续T7首个实际PDF作者命令前，控制器已成功执行本批marker恰一次，后续T8/T10不得重复。记录见本计划进度账本。

### 显式测试fixture预算（不是部署默认）

| fixture | 精确字段和值 |
|---|---|
| normal EvidenceParseLimits | maximum_input_bytes=2097152；maximum_pages=8；maximum_text_bytes=262144；maximum_excerpt_bytes=8192；cpu_seconds=2；address_space_bytes=268435456；wall_timeout_ms=8000；maximum_result_bytes=2097152；maximum_concurrency=2；queue_timeout_ms=2000；termination_grace_ms=200 |
| EvidenceProbeLimits | cpu_seconds=1；address_space_bytes=67108864；wall_timeout_ms=5000；allocation_chunk_bytes=1048576；maximum_probe_bytes=100663296；maximum_result_bytes=65536；termination_grace_ms=200 |
| parser容器与全链runner | memory=1073741824 bytes；pids=128；cpus=2；tmpfs=67108864 bytes |
| 独立PG | memory=512MiB（536870912 bytes），仅测试数据库 |
| source ObjectReadLimits | connect_timeout_ms=1000；read_timeout_ms=1000；total_timeout_ms=5000；chunk_bytes=65536；maximum_attempts=1；外层raw_maximum_bytes=2097152 |

全部正int排bool。资源用例可为目标边界显式覆盖某字段，不暗改公共fixture或生产配置。若实际baseline不能形成明确AS/CPU限制证明，先报告实测调整，maximum_probe始终有限；不以OOM/父timeout伪造通过。

### 两种临时Linux测试环境（执行方案，尚未运行）

1. 宿主使用现testcontainers/DockerContainer模式（`tests/integration/test_artifact_store_minio.py:16/63`），新增`tests/integration/quote_evidence_linux_support.py`统一只编排本次资源容器及internal PG全链容器。准备失败明确not_run，不复用其他项目镜像/容器，不触生产网络。
2. 构建只COPY本项目白名单必要包、受控tests/fixture及需要的迁移脚本；不是整个工作树。纯parser镜像只需shared/connector等最小依赖；全链runner另包含domains/workflows/tool_gateway/artifact_store/infra、migrations、alembic.ini、scripts所需迁移入口和明确选中的测试。绝不COPY.env/.git/Codex/用户目录；运行时不挂宿主volume。
3. 包在构建期安装，支持`python -I -m connectors.evidence_text.worker`，不使用请求或运行期PYTHONPATH改模块。宿主只把明确产物image ID交测试夹具；运行fixture不隐式pull/build，缺产物报准备缺失而非升级tag。
4. 纯parser资源容器network_mode='none'、无port/volume/socket，非root、read-only rootfs、cap_drop=ALL/no-new-privileges，tmpfs/PID/CPU/memory用上表。它真实执行同client/worker和私有固定probe，不能以容器OOM代替worker AS限制或父超时代替CPU限制。
5. 全链由宿主创建专用临时internal Docker network，仅本次`pgvector/pgvector:pg16`（沿现夹具）和固定Linux测试runner。无host network、无端口暴露、无Docker socket/宿主volume，PG独立上表内存限额，runner使用上表资源防护；网络仅供本次真实DB协作，不接生产库、不外呼provider。
6. 宿主fixture生成本测试PG身份/连接并内部注入runner的TEST_DATABASE_URL，复用现`tests/integration/conftest.py:102`路径，不能在runner内再启动Docker。迁移先由runner用同一测试连接执行（内部为迁移子进程绑定DATABASE_URL，不输出值），随后跑明确选定的Gateway/reader/NeedUnit真链测试；未指定测试连接立即失败，不回落生产env。
7. runner环境只显式本次测试需要的键；parser child进一步重建非秘密白名单，不继承TEST_DATABASE_URL/DATABASE_URL或其他密钥。无原件/IPC/环境dump，pytest禁--showlocals；输出只测试名/count/固定结果，子进程stderr丢弃。测试数据库连接由夹具内部使用，Agent不读取或打印其值。
8. 宿主在finally精确清理本次容器/network及临时构建输入，不清理其他项目，不新增生产服务/常驻daemon。纯parser网络none与真实DB链internal网络是两个明确配置，不能相互替代；也不能用Mac fake parser的PG测试加独立Linux结果冒充真链。
9. T8B ASGI全链可复用该internal测试环境；T10浏览器API另按计划提供仅loopback映射，本T8A不提前开放端口。
10. 记录正常PDF/RFC822、非法编码、四类资源故障、取消、并发/排队、预检失败及失效缓存、真实PG/Gateway/reader同链结果与运行架构/版本hash。环境或新实现探针失败仍明确not_run/未验收，不以只读inspect或单独OS探针成功替代实现验收。

### 预检启用与T8B边界

工厂构造纯settings/惰性S3/未激活parser/下层reader，不读原件/秘密/网络；受信startup显式核精确runtime并运行本地probe。available是该实例私有状态，不从DB或HTTP装载自证。失败不能保留旧成功状态；新进程、修改limit/runtime需要新probe。生产parse入口拒绝probe/fault/client程序字段。
T8B最终连接真实NeedUnitAuthorizer（当前机会交集与guard锁）、实际身份及两个factory/HTTP；本brief用受控authorizer验证调用和绑定不代表上线。T8A共享source factory不应藏默认actor，也不得将pricing四角色变CRM角色。
控制器已同意mandatory typed scope增量：verify/preview/locate全带用途并HMAC绑定；pricing只成本域操作权＋本人原件ACL，need_unit带真实Need ID＋既有权限交集。两个reader自行选scope；T8B不能接受客户端actor/role或用随意purpose绕过域动作。
`$`无需Linux parser，可在缺解析能力时提供授权原件hash用途；只要请求要求text profile就parse_unavailable。T8B可选择未完整配置时不注册整个工具，不能把该可选禁用策略误写成无限fallback级回退。

### 已裁定与派发门槛

typed用途、相邻整数+逐字单位首批子集、qev固定failure分支（含check）、严格解码、固定runtime/image、internal PG全链环境和fixture预算均已裁定，无上述设计选择待定；单位词法检查仍不是否定/历史/其他产品语义验证，也不自动产生Validated Need。
ADR0020在控制器本次核对时未占号，0019属T6/T7；派发前再核无冲突。必须T7完成且真实公共签名对齐后由控制器派发；实施时资源probe若不能证明限额须报告实测调整，不自行放宽或缓存成功。
剩余是执行验证门槛，不是缺失方案：项目构建产物、实际client/worker/probe与真链尚未运行；目标OS可行性已有上述有限证据，但不得将本次文档/镜像inspect/纯OS探针计作T8A验收或T8B上线。
