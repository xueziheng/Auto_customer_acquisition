# ADR 0022：报价准备公共契约与运行时 HTTP 接线

日期：2026-08-29。状态：按已批准 T8B2 brief 实施；各切片验收以实施报告为准。

## 决策

准备页面与正式报价 context 是不同用途。新增共享只读
`NeedQuotePreparationAssessment`，只包含真实需求/hash 绑定和固定缺项状态；
分类实现唯一归 demand，quotation 通过 workflow adapter 调公开服务取得结果。
原数量 hash、完整事实 hash、单位有效性函数与正式 context 的语义保持不变。
缺失数量没有数量 hash；历史零值保留真实 hash 但不能报价；损坏事实失败关闭，
不得被伪装成普通待补字段。

后续安全 HTTP 投影只列明白名单字段，Provenance 摘要不包含原文、URL 或定位；
原件读取依然独立鉴权。成本角色 C、单位用途 U（C 与机会权限交集）、客户文件 F
互不替代。所有新配置显式输入，域、来源、文件组按已批准 brief 分组装配，
唯一审批服务和完整 handlers 在实际 engine 构造前形成。

安全价格依据集合先执行当前C权限，再用成本UoW同session的
`CostingOpportunityReferenceReader.exists`读取tenant+opportunity的真实SELECT EXISTS，
仅返回bool，不展开客户/owner/Need或调用CRM授权。真实存在但无依据返回空集合；
缺机会/跨租户使用新增`CostingQuoteNotFoundError(record_not_found)`固定中文
“成本报价记录不存在”；存储故障不能伪装缺项。旧读取/确认错误语义不迁移。
coverage可按原确认hash恢复，不以并发新latest取代回执；scope按确认时间/ID发现历史。

U授权独立组合原C与机会读取矩阵（当前只有boss交集），员工→机会SHARE租约
不要求owner或issuer、不提前锁Need；内层真实Need事务结束后才释放授权锁。
typed摘要保留原金额、字段可空性、真实规格及完整hash；不增加默认事实。

## 边界

HTTP采用独立router及局部错误转换，不修改旧成本或全局400/401。输入复用原DTO的
JSON语义，Decimal字符串/数组/日期无损，禁止float金额；确认key原样传递并显式入OpenAPI。
内部报价只显式投影真实content/basis字段，普通GET不输出原件或完整冻结依据。
文件403/404/409/429/503声明flat与技术错误union，只有真实结果保留call ID；无合法
retry提示就不加Retry-After。PDF响应只用已核quote/version/template构造attachment文件名，
固定private,no-store与nosniff，不提供base64或永久URL。
三个frozen本地composition束明确公共端口与独立文件组，实际工厂/生命周期在后续切片
完成；wire替身测试不证明真实PG、Gateway、解析器或Provider接线。

实施期核对批准三个旧读取缺项的最窄类型增量：T2 get_policy/get_quote_fx仅record=None
改为现CostingQuoteNotFoundError；新GET都是404，原政策有效期/默认fallback不动。
旧CostingService.get_sheet仅sheet=None改为CostSheetNotFoundError，保持原固定文本
“成本表不存在”及ValidationError父类，旧HTTP仍400/validation_error/请求参数无效；
新scope/calculate路径按具名类404。无额外查库，不动add_item/readiness或其ID规则。
原计算/冻结policy_missing/fx_missing仍409；费用确认缺表或sheet变化的原合并CAS
为409/coverage_stale，不是幂等冲突，也不拆分原子判断。

不增加 Phase，不启用发送，不改变金额/置信度/租户/审批硬边界，不修改 Gateway
核心或旧工作流解释。当前文档记录决策，不声明四切片已完成或生产已部署。

## 运行装配与生命周期

纯infra配置拒重复JSON key、NaN、bool整数、缺预算和未知字段；core/evidence齐备才装报价，
files显式为空则文件生成/读取/历史/恢复/客户版本页整组不可用。预算无部署默认值。
API与worker本地组合各自构造来源独立Gateway、真实域、唯一approvals、完整handlers、唯一
engine，最后文件Gateway；不跨apps导入、不修改原DNS registry或借手工发送Gateway。
source/domain两个工厂显式tenant_id，复用既有HMAC且只初始化一次，提前次序不扩秘密权限。
issuer/run延迟闭包在发布前失败关闭，只一次绑定；正式发送回执固定不可用。

DeferredS3ObjectBlobTransport只延迟旧上传delegate，不改旧无界get/线程补偿。新来源始终
有界读，新QUOTE_PDF始终B1专属writer；恢复只收独立metadata-only adapter。构造无S3密钥
解析/SDK调用，但旧技术HMAC/退订key仍按原配置初始化，不能称全factory零resolver。
真实lifespan先schema再同parser probe；worker仅原singleton持锁后activation。普通能力失败
保留真实unavailable，仅parse失败关闭；异常/取消不当作平台降级。清理先parser再DB，
保留primary，无primary时清理取消不吞成成功；不宣称能强杀SDK线程。

结果通知新增quote_approval_result，固定LOW、QuoteApprovalResult、quote/run、四终态reason
与空level、固定相对深链/costing-quotes/quotes/{quote_id}；沿workflow K与稳定指纹去重。
API只原structured_log，worker入原job→claim→固定模板→原LOW路由→站内，零email。
仅此kind的notifier/template/InApp共用单一ASCII safe-label<=32且无既有secret marker的短员工
校验；旧kind canonical门、邮箱目录及全局身份长度不变。真实持久短ID同链验收在8.4。

实际组合的类型端口经service公开重导出既有CostingUnitOfWorkFactory、CostingFreezeUowFactory、
NeedUnitUnitOfWork，保持同class，不修改Protocol/UoW；不靠导入私有repository绕过依赖检查。
T5步骤仅用同QuoteWorkflowRunFact.model_validate验证原七字段mapping、局部apply变量改名，
保留原strict验证、错误、workflow版本与控制流，未验证输入不提前cast。
