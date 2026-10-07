# ADR 0018：成本与报价计算契约

状态：已接受（Phase 2 Task 1—5分段补充）

## 决策

成本报价计算使用版本标识为 `costing-v1` 的纯函数。调用方必须显式传入成本表、
利润规则、已确认核算政策、报价方案、覆盖/业务上下文哈希和计算时间；计算函数不读取
数据库、环境默认值、当前汇率或系统时钟。中间计算固定使用精度 50、
`ROUND_HALF_EVEN` 的 Decimal 上下文，只有客户展示单价和总额按已锁定的舍入策略量化。

报价展示固定遵循“先客户单价、后行额”：先舍入客户单价，乘确定数量后舍入客户总额，
再用最终总额除以数量得到有效客户单位收入。报价币种不同于核算币种时，只接受冻结的
“核算币种 → 报价币种”直连汇率；还原核算收入是该已声明关系的代数逆运算，不查询或
推测反向汇率。利润指标必须以该有效核算收入重算。

成本归入货品直接、其他变动和固定/间接三组的映射由老板确认、版本化，且 22 类成本
必须恰好覆盖一次。未确认项目不参与计算；零总完整成本、缺少直连汇率、非法利润率、
非正客户价或缺少政策均阻断生成报价计算快照。低于底线和亏损是待审批报价的真实计算
结果，必须保留其负利润与钳零后的空间；后续冻结/报价/审批流程负责要求对应的例外，
Task 1 不以数学可折扣空间代替批准。

`inputs_hash` 是规范化 JSON 的 UTF-8 SHA-256：键排序、无空格、Decimal 去除表示歧义、
含时区的时间归一为 UTC，列表保留顺序与重复项。它覆盖成本/价格依据、币种、数量、
单位口径、汇率、政策与归类、报价价格和精度、覆盖与上下文、算法版本；显式计算时间
不参与哈希。因此相同业务输入可重现相同数字和哈希，而重算时钟不同不改变输入依据。

既有 `compute_unit_full_cost` 与 `assess_quote_readiness` 的读取语义保持不变。新的
`compute_breakdown` 以显式 typed 参数替代原不足的二参数 stub，返回不可变的
`CalculationSnapshot`。

## 口径澄清

- 全成本利润率是 `(售价 − 全部成本) / 售价`，不是毛利率；毛利只扣货品直接成本。
- 可折扣空间仅是底线以上的数学空间，不是折扣授权。
- 可追加获客成本空间是已计入全部成本后的单件新增空间，不是可重复花费的总预算。
- 原始成本、汇率和规则输入须在既有数据库 Numeric 精度内，避免静默舍入；派生利润
  结果保留 `costing-v1` 的计算精度，不能按输入列精度截断。

## 兼容与后续

Schema 和算法版本进入输入哈希。未来新增字段、算法或工作流解释时必须以新版本保持旧
快照可读、可重现，不能覆写历史数字。政策、证据、冻结、例外审批和报价持久化由后续
任务实现；本 ADR 不授予自动发送、自动承诺或自动批准权限。

## Task 2：人工确认与持久化契约补充

新增 `CostingQuoteService` 与四张只增表：政策、价格/费用依据、完整性清单、报价汇率。
每类确认持久保存 tenant、幂等键、请求 hash、结构化业务 payload 与逐字段 Provenance；
金额以十进制字符串存 JSON，原文 bytes 不进入这些表。复合外键关联同租户原始资料、机会和成本表；
数据库 UPDATE/DELETE 触发器禁止改写确认历史。历史 `margin_rules` 不自动获得新路径确认资格。

`CostingQuoteServiceImpl` 构造增加必填 `actor_reader: CostingActorReader`。其
`read_current(tenant_id, actor_id)` 必须按当前在职员工事实返回角色/范围，缺失、离职或角色
变化默认拒绝；每次操作先检查，原文读取完成后且写入前再次检查。政策确认仅 boss；其余
成本角色不扩大到销售等角色。确认人不能由请求体提供，幂等请求 hash 绑定实际确认人。
T8 须装配真实员工服务；本轮受控 reader 验证不能替代生产装配或事务级身份撤销保证。

`PricingEvidenceReader.read_verified(tenant_id, source_ref, locator, *, actor_id: EmployeeId)`
增加必填 actor_id，以便原件 ACL 按当前员工校验；成本角色和持有 source_ref 不代表原件读取权限。
可信 reader 须核验原件租户、内容 hash、定位与授权，再返回 `SourceEvidence` 安全投影。
`SourceEvidence.source_url` 对 WEB_PAGE 必填，网页不能伪装上传来源；来源类型采用明确 allowlist。
政策和汇率使用根定位 `$`，表示整份授权原件，并非逐字段机器语义证明；采购/费用使用可复核片段定位。
原文和真正外部 IO 留给 T8 的 Gateway 装配，本轮不实现解析器、不访问真实原文、不确认供应商价款真实性。

费用依据增加必填 `is_per_unit: bool` 与 `quantity: int`（本次人工确认适用的订单数量），
防止从自由文本 allocation_scope 猜量纲；无产品 MOQ。采购依据记录供应商单价及 MOQ/数量范围，
可绑定单件项或整单项：前者金额须等于单价，后者须等于单价×成本表数量，以固定50位 Decimal
上下文核对且不改写金额。新报价采购只接受 quoted，费用允许 quoted/actual，不开放 indicative 费用例外。

完整性清单逐项覆盖22类，绑定持久 `item_sequence`、已确认价格依据、原文费用行及分摊范围。
以可信 artifact 身份归一 source_ref 别名后去重；同原件不同行允许，同明细同分摊范围不得重复；
获客汇总与数据/广告/API明细互斥。不适用但已有确认成本、金额/币种/计价口径/适用数量错配均拒绝。
费用范围确认不代替 T3 对当前 Need 规格、单位、目的地和机会归属的核验。

`CostItemView.item_sequence` 从真实持久序号恢复；新增项在现有最大序号后分配，不按数组位置重排。
`CostSheetView.content_hash` 包含数量、币种、成本项/确认来源和汇率，排除创建时间与 locked_at，
所以追加成本令旧清单失效，而单纯锁定不改变原内容身份。旧 API 与 readiness 仍保留原含义。
PricingPolicyView 可兼容纯计算 fixture 的缺来源形状，但正式仓储读写均验证来源、所有叶字段
Provenance、确认人/时间与内容 hash 一致；缺确认事实不能作为当前有效政策。

## Task 3A：客户数量单位事实契约

客户数量与供应商计价单位不是可互相推断的事实。为ValidatedNeed增加可空`unit`事实、
`unit_quantity_fact_hash`、`unit_confirmation_id`，不回填默认单位、不修改旧完整度或模型
可提取/可更新字段词表。窄的`NeedUnitService`按当前员工身份人工确认单位，不确认数量，
也不代替任何价格、交期等审批。`NeedQuoteFacts`是带完整Provenance的公开事实投影。

`quantity_fact_hash`使用`need-quantity-fact-v1`，覆盖tenant、Need、严格整数数量和全部
Provenance（含None）；来源或确认时间变化即使数量同值也失效。`need_quote_facts_hash`
使用`need-quote-facts-v1`覆盖全部事实/绑定；canonical JSON键排序、紧凑分隔、UTF-8、
时间统一UTC、Decimal字符串和date ISO表示，无读取时间。历史0数量可读/hash但不能确认单位。

确认顺序固定为当前权限check→短读事务→零锁客户原件核验→权限guard→锁Need→重验
当前数量来源/旧确认ID/客户/状态→插receipt/写三列/追加旧history→提交→退出guard。
来源reader按真实客户入站消息核对原件hash、定位、逐字摘录和数量单位关系；任何不匹配
不写入。guard不能只返回过时allowed，而必须保护当前在职/权限及机会范围直至内层提交。

持久幂等请求hash以`need-unit-confirm-request-v1`绑定tenant、Need、actor、command全部
字段，不含时间/reader输出/幂等键。相同键只返回首次receipt，数量变化后重放也不恢复旧
单位。重放/历史读取额外在锁外重验当前来源阅读权；并发同expected ID最多一次成功。

0042新增只增`need_unit_confirmations`，tenant复合外键约束Need/原件/当前确认；触发器
核对三单位列与receipt一致。旧数量更新保留单位三列，以hash差异派生stale。所有失败
回滚三个写入；未知提交返回`storage_unknown`，只用原键核对，不声称未写入或自动换键。
有确认业务记录时迁移拒绝降级；必须取得授权并归档处理，不能无声删除原始商业证据。

本切片仅验证真实隔离Postgres和受控权限/来源端口；真实Gateway原文、员工/机会锁适配、
HTTP/UI及生产装配不在T3A中。后续冻结应在自己持锁连接投影完整facts，不另调用get_facts
产生旁路连接；成本/报价通过上层转换DTO，不能跨域读取demand私有模型。

## Task 3B：共享事实与创建意图

`NeedQuoteFacts`唯一类迁至`shared/schemas/quote_facts.py`，demand原名同class重导出；
字段、确认人ID校验、UTC规则及旧事实hash字节均保留。数量正整数、人工单位绑定有效性
和旧错误码仍由demand公共函数决定，shared只包含中立形状与编码，不承载业务规则。

`QuoteCreationIntent`以`quote-create-request-v1`绑定完整请求，包括原起草人、条款原顺序
与重复项、期限、scope确认及显式修订版本。其Decimal规范化不依赖运行时精度，且与保留
尾零的T3A事实编码隔离；不能为了统一格式而改变历史事实身份。operation/completion为
中立内部DTO，不接受客户端自证完成；真实报价存在性仍须由后续可信报价reader证明。

报价准备采用用途隔离：四成本角色只获得内部最小机会/Need事实，不获得通用CRM/原件/客户文件
访问权。SQL adapter按排序员工→机会→Need的SHARE锁同session投影；bootstrap关联变化整轮失败，
不能持机会锁再补锁新负责人。员工FK KEY SHARE与SHARE兼容，成本持久提交发生在lease内。
上下文业务hash排除runtime与机会正常生命周期状态，但保留原起草人/owner和所有事实来源。

人工成本适用性scope把完整Need、所有持久依据ID/hash与逐项人工说明、条款和有效期绑定到
只增确认。供应商原始自由规格保留，不能与客户规范JSON做机器等价判断；材质、包装、来源
等变化即使金额不变也需新scope。T2与scope共用22项覆盖规则；单位、目的地、MOQ、数量档、
quoted及有效期仍逐项硬核验。来源阅读授权在所有业务锁外完成，确认提交处于context lease内。

0043只增加scope、成本basis与创建operation三表，均有tenant复合外键与完整JSON快照。
scope/basis不可更新删除；operation仅首次frozen→completed。basis与operation双向外键延迟
到提交检查，不关闭约束；pending按tenant/sheet部分唯一，不把sheet永久占给一个报价。
非空降级明确拒绝，避免抹掉商业确认历史；不建假quotation表或假生产回执。

冻结以tenant/kind/key咨询事务锁覆盖操作尚未存在的并发窗口，随后锁成本表，再共享锁当前
政策集合，之后读取一次注入时钟。政策确认按同tenant集合独占锁→确认键固定顺序；原件
读取在短重放检查事务结束后执行，保存时再次竞争键及核验当前员工。冻结重新验证完整
scope、Need单位、22项清单和全部持久依据，T1只接收已确认直连FX及当前政策，无隐式默认。

完整意图幂等包括条款顺序/重复项、原起草人、期限、scope和修订引用。basis、operation与
首次locked_at原子提交；同键仅在当前事实仍适用时返回原basis，不把历史通过当作当前有效。
commit回包未知固定storage_unknown；只能原键恢复。完成回执在零锁阶段由可信reader读取，
锁内精确复验tenant/operation/request/basis/修订链；重复同回执无写效果，另一个回执拒绝。

完成后可用新key、显式旧quote/version及新scope复用未变成本表；旧items、locked_at和basis
不修改。`FrozenCostBasis.cost_fx_rates`必填，原样保存锁内表内核算FX元组（含显式空元组），
与独立已确认`quote_fx`分别进入basis哈希；新修订可以选择新报价FX，但不得改写表内核算FX。
T3B完成回执只证明旧创建事实；当前active或latest expired版本的CAS、真实报价唯一约束、
实际报价抬头/原件reader、审批与客户文件授权均由后续切片分别验收，不由该回执替代。

新创建编码在定点展开前检查Decimal系数位数、绝对指数及预估编码长度，分别以4096为纯资源
上限；零值同样先检查系数/指数，再规范化为0。该上限不代表金额业务许可，亦不修改旧Need
事实编码或全局Money。成本域calculate/freeze的人工原始单价在请求hash与成本事务前复用
既有Numeric(28,12)无损输入边界，并要求正值；越界固定invalid_input，无新增持久效果。
T1的50位确定性计算与派生快照精度保持不变，shared不套28位商业金额限制。

## Task 4：真实不可变报价版本与恢复

新增`QuotationVersionService`及0044七表，保留旧报价构造和旧Protocol。完整quote内容保存真实operation、
basis、人工scope、政策/计算、采购及费用证据、成本FX与报价FX、原起草人/owner、老板抬头和单产品行。
金额JSON为十进制字符串；内容hash排除created_at和自身hash，其他嵌套字段全部绑定。读取重验hash，
SQL绑定同租户真实父行和operation/basis完整载荷；内容/子行不可修改删除，state变更与事件原子落库。
非空降级拒绝，不删除报价商业历史。

创建顺序是外层员工→机会→Need SHARE lease，内层报价机会advisory→preflight→真实freeze→quote写入及
显式commit。Opportunity不能升级写锁，避免与外层lease自等待。机会版本/active唯一约束与同事务CAS
令不同成本表的并发输方在freeze前拒绝，不占其成本表。到期active保持expired；latest expired需显式E2。
accepted/rejected不能被替换，但新成本表/新scope/新key可在无active时建立下一版事实，不修改旧终态。

同key先按原prepared_by/scope_hash重建完整意图比对，再寻找真实quote；存在即在无业务锁时完成原operation。
当前Need/抬头/期限变化不阻断历史恢复，当前内部读取权仍必须有效。quote未写时成本冻结仍pending，
写后complete失败或提交回包未知只按原key恢复。真实completion reader精确返回持久版本及首次CAS替换版本；
成本有expected才检查expected+1，无replaces不能假设version永远为1，其顺序由真实报价机会锁和reader证明。
所有tenant/operation/request/basis/replaces绑定及已完成receipt精确等值约束不变。

老板抬头采用租户锁和连续版本，只确认本次name/address/contact原输入，完整逐字段Provenance绑定老板。
新确认不撤销旧记录，创建使用该次context选定的真实版本，不追逐latest，不影响历史报价。正式客户文件
仍由后续当前context门禁处理新抬头差异。

共享`CustomerQuoteView`是唯一客户白名单；纯投影不证明权限、批准或可发送，旧内部视图未知姓名保持None。
后台expiry覆盖四active状态。发送记录必须有可信reader精确实际receipt、真实attempt FK且当前未过期approved，
receipt/sent/event同事务，重复receipt只返回当前状态。测试中的审批状态/发送reader受控，T5真实审批/outbox、
T8 Gateway/客户文件ABAC/HTTP及生产装配均未完成，本决策不授予自动承诺或发送权限。

## Task 5：单轮审批、当前授权与原子成功

一个不可变quote只有一轮，六类required_type按固定顺序派生，每类一个真实包。quote_send必需，低于底线
另需margin_floor_override，折扣/交期/付款/认证分别确认。严格新namespace与payload同时匹配才进入新路径；
半标记/空白/大小写伪装失败关闭，不影响legacy邮件quote_send。请求hash覆盖原始expires_at_limit与全部
原输入，跨状态同key返回原ID，不能因重启延长期限或替换被拒/过期包。

quotation拥有唯一业务ABAC与安全payload；approvals经注入guard保护决定事务，workflow只适配公开DTO。
当前active boss或当前owner直属manager可决定/应用，但任何起草人、提交owner、当前owner都禁止自批。
own read只给读权；可信reader.role在lease内必须等于当前员工角色，角色漂移不能被list静默跳过。
历史读取只保护员工/机会关联，不追逐Need单位、issuer或policy。payload保留安全商业金额、FX实际引用与
证据确认摘要，不包含source_url、locator、原文或完整Provenance；这不是原件ACL授权。

fresh提交/应用顺序为全部员工一次排序SHARE→Opportunity→Need→报价机会advisory及quote→政策集合shared。
全体decider必须同时受锁保护，不能在Opportunity后补员工锁。政策选择复用costing公开selection，按当前
业务category及每次新时钟选择；全锁后再检查policy id/hash、context及T4第二道依据门，不重算历史价格。
报价state、状态事件、QuoteApproved与完整决定receipt同一事务提交/回滚，之后才释放policy及context。

0045新增namespace/hash/原limit、跨状态唯一、每quote/type唯一和成功receipt；所有tenant复合FK保留。
审批原请求及首次决定事实不可修改，binding只增并比对实际包的完整原请求事实；receipt只增不可删改，
其完整决定必须匹配绑定和实际包的决定人/时间/备注。存在新审批历史时拒绝降级，不删除商业事实。
SQL仅保护存在性/绑定一致，不复制角色或业务适用性规则。ORM同步CHECK/unique形状。

首次成功receipt固定保存真实quote_approval run归属。quotation必填run reader核tenant/run/type/version/
subject/quote_version/content_hash；executor不是员工身份，FK存在也不是授权证明。engine公开get_run仅供
受信workflow内部，以独立MVCC plain SELECT读取包含终态的run；完整context不进入quotation或HTTP。

真实PG复现揭示：跨await handler的Run FOR UPDATE会阻止独立报价事务receipt外键取得KEY SHARE。
控制器核定执行只修改非键字段后，仅poll_due/deliver_event两处改FOR NO KEY UPDATE，SQLAlchemy使用
`with_for_update(key_share=True)`且不设read=True。step FOR UPDATE/SKIP LOCKED、step→run顺序、cancel/
失败收尾的Run锁保持不变；run_id、tenant_id、idempotency_key在执行期间不可改变。真实barrier测试证明
该锁允许FK校验，仍阻塞独立非键写/键修改/删除，cancel等待后不复活终态，poll/event不重复应用。
依据：[PostgreSQL 16行锁兼容矩阵](https://www.postgresql.org/docs/16/explicit-locking.html#LOCKING-ROWS)、
[SQLAlchemy锁参数](https://docs.sqlalchemy.org/en/20/core/selectable.html#sqlalchemy.sql.expression.GenerativeSelect.with_for_update)。

七步流程assemble→submit→wait→apply→mark_applied→notify→complete只持久metadata。事件仅以approval_id
唤醒原run，真实包决定每步重读；早到事件、重启、部分submit/bind失败仍使用原组。wait进入即poll，超时
只能终止，旧版本晚到事件不批准新版本。receipt优先恢复，混合APPROVED/APPLIED按稳定key逐个补记；
此时Need/policy/在职变化不阻历史记账。无receipt不能由APPLIED或事件反造成功；迟到确定性失败在报价锁
内先查receipt，成功后只补记，不写apply_failed。transient/未知存储结果不转永久失败。

本任务验证真实隔离PG、审批/报价/outbox/engine与当前权限/政策锁；原件ACL使用受控测试端口，通知仅
受控metadata Protocol。T8才装配API/worker/真实通知及Gateway依赖；T6/T7客户文件仍需当前文件actor、
context与适用批准，成功receipt和QuoteApproved都不是永久文件或发送授权。本任务不生成PDF、不外发。
