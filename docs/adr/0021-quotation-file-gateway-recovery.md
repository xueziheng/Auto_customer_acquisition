# ADR 0021：报价文件Gateway、持久预留与有限metadata恢复

- 状态：实现完成，待Task8B1整项审查；不表示API/worker生产接线或真实商业验收。
- 日期：2026-08-28
- 范围：Phase 2 Task8B1；沿用ADR0019三hash/不可变文件和ADR0020有界对象读取。

## 背景

报价文件的历史归属、当前正式使用许可和实际bytes完整性是三个独立事实。文件actor可以
不是quote_send决策人；成本操作角色也不自动拥有客户文件权。对象提交与Gateway账本不能
原子提交，旧生成EXECUTING即使lease过期也可能仍在运行，不能靠重渲染或伪造旧成功恢复。

## 决策

### 文件用途与客户发现

真实文件scope只允许同租户在职boss、机会owner本人sales、当前owner直属manager；先排序
锁员工再锁Opportunity。历史scope不依赖Need、抬头或政策。正式授权另锁全部原决策员工、
原起草人（仍存在时）和当前Need，复用T5原请求/receipt/run、fresh依据及decider纯规则；
不调用仍要求actor=send_decider的旧apply入口，不更改apply/历史恢复语义。

每次正式生成、正式读取、恢复均重验当前context、全部批准、policy、quoted依据及有效期。
snapshot业务等值显式包含全部字段及完整customer，只排除checked_at；不能缓存许可。
只读policy lease显式正常退出以保留消费方业务拒绝，不称为报价业务提交；取消不能被cleanup
失败改成成功。外部IO之前释放业务锁，之后再次真实授权，撤权不交付bytes且不删除产物。

客户版本页只返回安全版本信息、文件metadata及固定业务blocker，不含价格/成本/完整客户DTO。
SQL按tenant/机会、version倒序取limit+1，真实后页才提供游标；读取故障/持久损坏使整页
失败，不能伪造待审批blocker。

### 四工具及唯一结果槽

| 工具 | risk / 幂等 | checks | 成功交付 |
| --- | --- | --- | --- |
| quotation.file.generate | MEDIUM / REQUIRED | tenant→permission→approval→idempotency→rate_limit | 空槽，provider_ref经T6重读和正式后置门成为metadata |
| quotation.file.read | LOW / NONE | tenant→permission→approval | 同task一次性bytes槽 |
| quotation.file.history.read | LOW / NONE | tenant→permission | 当前ABAC及历史归属后的bytes槽 |
| quotation.file.reconcile | MEDIUM / NONE | tenant→permission→approval | 新调用成功后的typed恢复槽 |

四工具皆FREE（技术分类不代表没有S3费用）、v1、不声明HIGH profile；strict输入只有quote/file/原call ID，不接受actor、
approved、run、key、template或history布尔等权限暗示。NONE是每次独立technical claim，
不是跨调用去重保证；reconcile是内部幂等关联写，不是只读工具，也不是对外发送。

唯一task-owned槽只容纳BytesPayload、RecoveryPayload或固定Failure之一，take即删除。
子task继承ContextVar不能领取父task结果；所有非成功/异常/取消必须清槽。generate成功槽
必须为空，不能塞空bytes冒充metadata；DUPLICATE及IN_PROGRESS另外核真实ledger的tool版本、
canonical key、HMAC及provider_ref/状态，不能假定核心claim已比较tool_version。

通用output仅provider_ref。现pipeline只支持output.status=validation_passed，不能为恢复
伪报该值或改核心；metadata_recovered_original_unresolved只在真实SUCCEEDED后严格核
RecoveryPayload的新/原ID、file provider_ref、原状态及UTC checked_at后成为应用wrapper。
代价是通用output自身没有恢复outcome，须结合专用工具、新requested审计及受信wrapper理解。

生成稳定key是`{quote_id}:{quote_version}:quote_pdf:{template}`。生成HMAC协议为quote-file-v1，
恢复协议为quote-file-reconcile-v1并绑定原生成key/digest/key_version；协议不是HMAC密钥版本。
actor不参与生成key/摘要，但每次当前授权和审计user仍使用真实员工。旧密钥不能核验即固定
冲突，不换key或模板重新生成。ToolCallContext的run/approval/campaign引用均为空；artifact的
workflow_run_id只来自真实批准receipt，不冒充本次正在执行的批准流程。

域员工DTO仍保留fact_identity的40字符规则，不迁移或截断历史ID。既有Employee表与Gateway
ledger user_id最多32字符，ledger还要求safe-label并拒绝分词后的token/bearer等保留词。
因此并非所有域合法ID都能执行Gateway：共享ValidationError发生在真实ToolCallResult之前
时应用固定invalid_input、两个call ID皆None、清槽、零IO；不复制私有validator或制造替代ID。
33/40测试仅受控actor事实到真实ledger拒绝，不能称为真实员工持久兼容；短旧ID审计原样保留。

### 有界对象与持久预留

shared中立GeneratedDocumentStore只提供metadata、put_pdf及get_bounded；恢复注入独立的
GeneratedStoreDocumentMetadataReader实例，不以宽Store换窄注解。读取先结束metadata事务，
按调用上限、Store上限及metadata大小取交集，最多多读一个哨兵，校验长度/SHA；没有bounded
依赖则失败，不退回旧get。旧Raw/EMAIL_DRAFT及Generated get契约保持。

新增S3QuotePdfObjectBlobTransport仅单次put/delete，构造零SDK/取秘密；每次操作显式有限
connect/read/total预算、attempts=1，无multipart或自动重试。开始写后的未知保留candidate，
只清理已确认loser，取消等待受限线程收口，不宣称Python能强杀线程。旧S3ObjectBlobTransport
构造期解析凭证的既有事实未被改写；新writer错误不含SDK文本、endpoint/key或凭证。

生成rate_limit是持久技术预留写：独立短事务按tenant advisory→本canonical行锁→锁后DB
clock_timestamp，核CLAIMED/版本/摘要/lease/owner，再统计窗口内reserved事件（含未来时间）。
成功追加真实canonical/new tce事件和本次actor；必须commit及close确定成功才放行，未知也不
退款。限流Retry-After取第N新事件窗口及当前lease较晚者，ceil并限制1..86400；同attempt不免费。
现CheckStage注释“只读，幂等占位例外”未涵盖该插件技术预留，本ADR披露窄偏差；不改核心
stage、表/索引/迁移，也不引入通用配额服务。

真实ledger/executing事件数0拒绝、1才可能首次、≥2只许metadata恢复。不能依靠会被限流
覆盖的error_category；显式reconcile hook及后续普通execute都受保护。延迟旧协程每次放行
仍须独立预留，但没有attempt fencing，不能声称杜绝所有并行陈旧执行。

### 有限恢复及不可改变的旧审计

恢复正式授权后只读取明确old ID，核同tenant/generate版本/原key/HMAC，且仍EXECUTING、
provider_ref为空。只按原key读取metadata，缺失不生成、不读bytes。record_file之前，公共
UoW必须核真实新reconcile EXECUTING行及全部技术绑定、原行完整投影，再仅向新call追加
recovery/requested、rule=original:{oldID}、真实本次员工和UTC时间。审计退出未知或取消零关联。

随后T6幂等补关联/重读、三hash和正式后置门，再plain读原call仍EXECUTING才放恢复槽。
requested只证明恢复请求，不证明关联已完成或旧生成成功；已关联后若最终审计失败不交付
成功，也不删除file。原call/events零写、无ack/complete原调用、无find_by_key枚举或旧call锁。
checked_at只是最后检查时点，不是fence；旧线程之后可以自行完成并取得同一file。

## 放弃的选项与代价

不以lease过期认定未执行、不覆盖旧审计、不自动重渲染、不用历史入口兜底正式失败，也不
以一次查不到metadata清理对象。代价是可能保留孤立bytes、旧EXECUTING仍需人工核对。
metadata恢复不保证实际对象存在；正式/历史下载各自仍要真实bounded完整性校验。

## 验证与B2交接

B1以隔离真实PostgreSQL、多连接锁/事件/并发、真实T4/T5/T6、实际T7 renderer及受控对象/SDK
验证；不重复T7 Marker或T8A固定Linux探针。真实S3/邮件/商业材料、生产部署均未执行。

B2显式注入所有reader、单一审批实例、延迟run reader、scope/access/customer versions、
GeneratedStoreDocumentAdapter及独立metadata-only实例、单slot、四handler/check和真实Gateway。
Rate/Write/Read/Renderer预算、HMAC及lease owner无生产默认；由B2负责factory顺序、settings、
HTTP/OpenAPI/Retry-After/真实call ID投影及API-worker资源生命周期。下载不等于sent；B1不
装配发送receipt reader、不启动B2。实际接线或密钥轮换、ledger身份规则变化、呈现模板升级、
增加对象清理或fencing需求时须重新审视本ADR。
