# ADR 0019：报价PDF派生文件、历史归属与未知提交恢复

- 状态：已采纳
- 日期：2026-08-28
- 范围：Phase 2 Tasks 6–7；不包含正式生成/下载装配或发送

## 背景

原始PDF是外部证据；系统生成的报价PDF是派生产物，不能作为客户原话、供应商报价
证据或Provenance终点。文件存在、历史批准成功和当前允许正式使用是三个不同事实。
对象存储与PostgreSQL不能以单个本地事务原子提交，commit成功后的响应丢失或close失败
也不能当作“没有提交”而删除对象。

## 决定

### 三个hash与唯一客户形状

继续使用shared唯一的CustomerQuoteView，不复制客户DTO或从当前Need重建旧报价。
`quote_content_hash`绑定T4完整不可变报价；`customer_content_hash`对客户DTO逐字段、
有序条款和原展示字符串作版本化规范JSON SHA-256；文件输出的`content_hash`仅表示
PDF bytes hash，持久列明确命名为`artifact_hash`。任意一个都不能替代其他两个。

客户hash编码版本是`customer-quote-v1`，不包含模板、actor、state或内部成本依据。
它证明客户投影身份，不证明PDF文字正确；该验收属于后续作者及Gateway管线。

### 派生类型与呈现版本

GeneratedArtifactKind增加`quote_pdf`，只允许`application/pdf`。RawArtifactKind.PDF
保持原始证据语义。原邮件草稿的enrollment主体、draft键、MIME及producer正则不变。
QUOTE_PDF严格绑定quote主体、正版本序号、真实归属run及
`{quote_id}:{quote_version}:quote_pdf:{template}`幂等键。

模板注册唯一位于shared，目前仅`quote_pdf_v1`。模板是呈现版本契约，不是商业批准；
未来呈现升级必须用新受信模板，不覆盖旧文件或偷偷改变报价内容。0046将本期模板字面值
冻结在迁移内，不导入未来可变注册表。

### 真实metadata、稳定run与只增关联

infra的GeneratedStoreQuoteArtifactReader只调用公共Store.get_meta，逐字段保留真实
kind/MIME，禁止读取bytes、对象地址或私有artifact仓储。域按原报价版本、receipt、
全组原审批请求/不可变绑定、决定hash以及真实engine run验证历史归属，不伪造executor，
不以APPLIED或QuoteApproved事件推导成功。completed/failed run不否定已存在的合法历史。

quotation_files以tenant+quote+template唯一；复合外键绑定报价、真实artifact和批准receipt，
INSERT trigger核对冗余version/hash/run/MIME/subject/sequence/template/key/size/time，
UPDATE/DELETE一律拒绝。客户hash只在DB做格式约束；服务每次写入和读取重算真实客户投影。
所有查询受tenant约束，无通用upsert、update、delete、跨报价list或业务清扫接口。

record_file仅接受真实actor及artifact_id，先核当前actor并打开机会scope，再读取原批准与
metadata，之后取得T4同机会advisory锁、重读原报价/receipt并新增关联。metadata读取位于
报价写锁外，scope保持到关联事务退出；不升级Opportunity为FOR UPDATE、不持报价锁补员工锁。

员工ID复用既有fact_identity规则（严格str、最长40、无空白/控制字符），由当前员工reader
和scope核真实身份；不要求旧员工迁移为ULID。新文件/产物/报价/run/tenant DTO保留严格ULID。

### 历史读取与当前正式授权分离

独立文件服务不复用prepare/read_internal四成本角色权限。它要求受信scope保护当前机会ABAC：
sales本人、manager当前直属owner、boss本租户；成本角色不隐式获得客户文件权。
T6只用受控guard证明协议与失败关闭，T8才装配真实scope、文件用途context及当前批准门。

get/list/get_file_approval仅返回安全metadata或历史归属，旧Need变化、报价过期或终态不阻断
合法历史读取。历史hash/receipt保持有效不代表当前可正式生成、下载、发送或承诺。
未装配files时门面保留旧非文件构造兼容，但四个文件方法都返回dependency_unavailable。
不注册任意key的HTTP查找，不开放原件读取，也不把下载记作sent。

### 未知结果的保守恢复

仅新增QUOTE_PDF路径：尝试object put之后，transport、UoW退出、commit或close发生不确定
异常时保留candidate bytes，固定返回artifact_commit_unknown；取消原样传播。rollback成功
或暂时SELECT不到metadata都不能证明未提交。写对象之前的基础设施故障固定artifact_unavailable。

只有成功确认EXISTING结果后才可清理本次未引用candidate，不删除winner、不修改幂等键。
受信调用方只在已知可安全再次put时，用原key/bytes/run/subject/sequence/template显式恢复；
异常处理本身不自动重试、不重新渲染、不换key。get_meta_by_key提供原稳定键的窄只读恢复，
不读写删对象、不分配candidate；None不证明未执行。实际bytes读取仍必须Store.get重验hash和长度。

报价关联提交未知固定storage_unknown，只按原artifact重试查唯一winner，不能删除已持久产物。
Raw和既有EMAIL_DRAFT补偿行为保持兼容；本次不改变其恢复策略。

## 后果与验收边界

未知失败可能留下未被metadata引用的孤立bytes，这是避免误删已提交winner的保守代价。
本期不建清扫器；后续清理须独立审计并证明不可达，不能用一次“查不到”判断。
0046 downgrade遇到任何新kind或文件关联即固定拒绝，不删除业务数据换取降级；无新数据时
可往返且保留全部旧Raw/email数据。

T6使用隔离testcontainers PostgreSQL、真实T5批准/engine身份、真实Store metadata与受控
object transport/bytes验证恢复和绑定。没有运行PDF作者、外部Provider、生产迁移、
正式客户授权装配或发送；不把受控scope、受控单step run当作生产七步流程已验收。

## Task 7 补充：离线作者的固定边界

报价域定义 `QuotePdfRenderer` Protocol，但具体 ReportLab connector 不导入 domains 或该
Protocol；它仅接收 shared 唯一 `CustomerQuoteView`，不接收内部报价、成本、Need、actor、
授权票据、路径或 URL。正式链仍必须是可信报价投影校验、当前授权与适用批准之后才调用，
由 Task 8 装配。renderer 返回 bytes 不代表可生成、下载、发送或承诺。

模板注册继续唯一位于 shared；当前仅 `quote_pdf_v1`。跨层渲染错误同样位于 shared，使用
受限的 `QuotePdfRenderError` code 与固定中文消息，不携带客户文字、文件路径或原始异常。
三个资源上限（文本 UTF-8、页面、输出 bytes）均由部署显式配置且拒绝非法值；输出 sink 的
大小检查不构成进程 RSS 沙箱，Task 7 不伪造线程/进程隔离承诺。

作者固定 A4、Vera 包内字体、稳定 metadata 与 invariant 输出；先验证 DTO/模板/文本上限，
再加载字体并按真实 TTFont glyph 映射验证、构造已转义的可分页 flowables、在 N+1 页绘制前
停止、最后受限输出。它不加载图片、文件、链接、附件、表单、JavaScript 或动作；对象图
安全与字节确定性有单测，逐页视觉验收和真实授权链验收仍分别属于 Task 10 与 Task 8。
