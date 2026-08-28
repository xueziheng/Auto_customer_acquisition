# domains/quotations/ —— 报价域

## 职责

报价的版本化管理，以及**禁止自动承诺清单**的权威定义。

## 本域的核心资产：FORBIDDEN_AUTO_COMMITMENTS

设计稿第十节列出的「即使 Campaign 已批准也不能自动承诺」的清单，在本域落地为注册表。`tool_gateway` 和 `agent_runtime/guardrails` 都对着它检查。

判断规则一句话：**只要输出会构成对客户的商业承诺，就必须人工过目。**

一封邮件说"价格大约 $2.5"和说"我们的产品很耐用"是完全不同的事：前者客户会拿着截图来要求兑现，后者只是营销措辞。清单管的是前者。

## 状态机

```text
draft ──→ pending_approval ──→ approved ──→ sent ──→ accepted
              │                                 ├──→ rejected
              └──→ rejected（审批不通过）        ├──→ expired
                                                └──→ superseded（被新版本替代）
```

**只有当前有效且获批的报价可以发送**。`QuoteApproved`只记录当时成功，不是永久授权；正式文件或发送必须重新验证当前机会权限、报价/context/有效期及全部适用批准。

## 版本不可变

报价修改 = 新版本。旧版本永久保留，绑定它当时的成本表版本、汇率快照、价格快照。

原因和成本域一致：客户两周后回来砍价时，「我们上次报的 $2.80 是基于什么算的」必须能立刻回答。供应商中途改价不影响已发出的报价记录。

## 有效期必填

没有有效期的报价是开放式承诺：三个月后客户拿着旧价格下单，而汇率和供应商价格早变了。每个报价必须带 `valid_until`，过期自动转 `expired`。

## 价格基准校验

报价的每一行都要能追溯到 `QUOTED` 基准的价格快照（硬边界 7）。成本域的 `lock_for_quote` 是第一道门，本域创建报价时再验一次——两道门是因为代价不对称：多查一次很便宜，亏本报价很贵。

## 依赖白名单

```text
允许   shared.events, shared.schemas, shared.errors
禁止   任何其他 domains/*、任何外部 SDK
```

成本数据由上层从 costing 域取出后传入（`CostBreakdown` 的值），本域不直接调。

## 发布 / 订阅

发布：`QuoteApproved`
由上层workflow消费`ApprovalDecided`，读取真实决定后调用本域；事件本身不能推进批准状态。

## 禁止事项

- 不允许跳过审批直接 `sent`
- 不允许修改已发送的报价（开新版本）
- 不允许无 `valid_until` 的报价
- 不允许报价行引用 INDICATIVE 价格快照

## Phase 1 范围

报价模型、版本化、状态机、禁止承诺注册表、审批衔接。Phase 1 报价由人工起草，但走同一套结构和门禁。

不做：报价 PDF 生成（Phase 2）、多币种并列报价、自动折扣策略。

## Phase 2 报价准备用途

`QuoteBusinessContext`仅供可信内部应用；含完整Need原文摘录，不可直接序列化为HTTP/日志。
prepare/read_internal只开放当前在职boss/product/sourcing/finance，不借此扩大CRM、客户文件、
消息或原件权限。审批仍由approvals决定，客户文件仍按机会ABAC，不复用准备用途policy。

业务hash绑定完整Need来源、负责人、原起草人和抬头版本；不含本次actor、runtime或正常机会状态。
完整规格保留material/packaging等维度及None，供应商自由文本不能与canonical JSON猜测等价。
context lease按员工→机会→Need取SHARE，直到内部持久事务完成；外部bytes读取不得进入锁区间。

## Phase 2 不可变版本与内部入口

新入口是`QuotationVersionService`/`QuotationServiceImpl`；旧`QuotationService`和旧DTO只保留兼容，
不得把旧布尔审批/mark_sent接口转接为新路径的许可。`QuoteApplicationService`在上层编排真实冻结。
调用`open_creation`必须持有可信context lease：报价机会advisory锁在freeze之前取得，直到本次quote
显式提交；不能把Opportunity升级为FOR UPDATE。完整内容与line/证据引用只增，state变化须同事务审计。

active为draft/pending_approval/approved/sent，全部按有效期过期。修订必须精确引用latest版本；
到期active只落expired，latest expired按显式E2创建下一版，不能改成superseded。accepted/rejected
不接受replaces；无active时可用新成本表/新scope/新key创建新事实，旧终态不变。

老板`confirm_issuer`只作本次三字段人工确认，不宣称原件机器核验；抬头按版本只增，旧报价保留原快照。
`get_confirmed_issuer`只供可信reader，`expire_overdue`只供租户后台作业，不是免鉴权HTTP入口。
完成receipt只从真实持久报价投影；同key历史恢复仍检查当前内部读取权，但不追逐最新Need或有效期。

`project_customer`是共享唯一CustomerQuoteView的纯白名单投影，`approved_terms`字段名不是批准证明。
生成正式客户文件还须独立机会ABAC、全部适用审批、有效期及当前context门禁；不得返回内部成本DTO。
`record_verified_send`只接受可信reader的实际发送精确receipt，并要求当前未过期approved；下载不是发送。
Task4未装配T5审批/T8 Gateway及发送reader，无生产自动发送、人工accepted/rejected或HTTP状态改写入口。

## Phase 2 单轮独立审批

每个不可变quote只有一轮，每种required_type只绑定一个实际审批包。quote_send必需，低底线与四类
承诺分别独立审批；改变政策或报价需新basis/版本，不修改旧payload、hash或替换过期包。
安全payload逐字段白名单，包含真实FX引用与证据确认摘要，不含原文/来源地址/完整Need或Provenance。
审批read可读自己的包；decide/apply仅当前在职boss或owner直属manager，排除原起草人、提交owner、
当前owner，无老板自批豁免。不得用四成本角色权限代替审批权限或客户文件权限。

顺序固定：全部员工一次排序SHARE→Opportunity→Need→报价机会advisory/quote→政策集合shared。
全部锁后重取当前时钟、政策选择及第二道报价依据门；报价state/状态事件/QuoteApproved/receipt
同事务commit或rollback，之后才退出政策及外层context。历史access不查Need/issuer/policy。

真实run reader是必填内部依赖，逐字段验证tenant/run/type/version/subject及固定quote_version/hash；
executor不是员工身份或授权票据。成功receipt含完整不可变决定和首次run归属，只增且与真实包/绑定一致。
历史恢复只核receipt/绑定/决定hash并补记APPLIED，不再要求fresh context，不代表允许再次生成客户文件。
API/worker生产composition及通知/原件Gateway留T8，文件权限留T6，不增加默认actor或默认allow依赖。

## Phase 2 报价文件关联

独立`QuoteFileService`及门面四个文件方法只输出安全metadata/历史批准归属，不走内部
四成本角色policy；当前actor必须真实在职且同tenant，受信scope保护当前机会ABAC直到事务结束。
sales本人/manager当前直属owner/boss租户的真实guard装配留T8；T6受控scope不代表生产授权。
既有EmployeeId复用fact_identity，不升级身份格式；所有新文件DTO仍严格不可变、拒额外字段。

文件只从真实metadata、原quote内容与真实成功receipt产生；复用T5同一完整snapshot、
全组原请求/绑定、决定hash及真实run校验，不伪造executor、不补记APPLIED、不重新批准历史。
metadata adapter放infra，只用Store公开get_meta；本域不导入artifact_store或对象bytes。

record先scope→原quote/receipt/run→锁外metadata→T4同机会advisory→重读quote/receipt→
只增文件→commit→释放scope，不升级Opportunity锁、不持报价锁补员工。三个hash分别绑定
完整报价、客户投影和bytes；生成时间取真实metadata，唯一模板关联重试返回原file_id。
未知关联提交只用原artifact恢复，不删Store对象、不改quote state/outbox、更不标记sent。

get/list逐次重投影客户hash并重核真实metadata/receipt/run。历史quote过期或终态可读不等于
当前正式使用许可；T8还须当前context/适用批准及Store.get bytes完整性校验。未装配files时
四个文件入口全部dependency_unavailable，不以安全metadata/FK存在替代正式生成或下载授权。

## Phase 2 Task8B1文件用途增量

`ContextQuoteFileScopeAuthorizer`已提供真实sales本人/owner直属manager/boss scope端口，实际
API/worker接线仍留B2。`QuoteFileAccessService`正式用途独立于actor=send_decider的旧apply门；
一次锁齐actor、owner、存在的preparer及全部deciders再锁机会/Need，复用同域T5纯规则，
重验当前context、policy、quoted依据、所有批准及有效期。scope历史用途不查Need/抬头/policy。
snapshot等值只排除checked_at，不缓存许可；外部IO不持业务锁，交付前必须重新真实授权。

客户版本页只提供安全版本信息、文件metadata及固定业务blocker，不含价格/成本或完整客户DTO；
依赖失败/不可变损坏不能降为blocker。
无文件业务状态写、无新增批准、无自动history fallback、无把下载记sent。域员工DTO仍沿
40字符事实校验；现Employee持久表与Gateway ledger有更窄32字符/safe-label限制，域不复制
该限制、不截断ID，应用使用真实Gateway拒绝。ADR0021记录完整边界与受控验证范围。
