# ADR 0026：邮件正文独立游标与Gateway内不可变原件

状态：已采纳；2026-09-05；依据本轮获批Web-first Task5子规格。

## 决定

采用[Task5a子规格](../superpowers/specs/2026-09-05-email-inbound-5a.md)所列shared frozen typed DTO/窄Protocol、gic1水位锚定和固定预算、独立email.inbound.fetch插件及task-owned一次槽。新增契约不改变feedback接口、事件、根九条边界或原审批。

所有原件IO在Gateway内，通过infra适配Raw.put后bounded get完整性验证。Raw与后续PG业务事务不是分布式事务；原件可孤立，事务回滚不得删除。Raw旧未知提交补偿局限保留，写入/读回不确定整页失败，不把metadata当原件存在证明。

v1只取唯一精确Message-ID/In-Reply-To和带时区Date，不猜客户账户。完整候选guard后再候选大小判定，marker先归档后隔离。Provider/archived DTO敏感内容不在repr、ledger、模型或默认序列化。

5a只实现读取归档候选；5b独立实现cursor/receipt/review三张tenant技术表、bound UoW整页提交、当前出站SENT关联及boss只读review/source授权入口。需要新公开review权限时沿本ADR登记，不新增结果事件。workflow不得通过逐条commit冒称整页原子。

## 代价与兼容

两套Gmail扫描和Raw写后读增加IO；严格关联、超限与marker会增加人工待核对。profile只锚定不消费消息，history过期固定阻断；不得自动跳水位。gic1不接受旧gfc1；config/解析策略版本纳入route，轮换需显式迁移，不重解释旧receipt。Task5b完成前不能声称需求验证闭环已交付。

## 纯Raw内容解析与后续消费

新增 `parse_inbound_content(raw_mime: bytes) -> InboundContent` 纯解析口（connectors/gmail/inbound_mime.py），
`parse_inbound_message`复用它。InboundContent只有固定disposition/parser_version和进程内subject/body/guard_body，
没有tenant、发件人、关联或Provider元数据。Task6在Gateway授权Raw读取后复用此口，不得伪造labels/internalDate。
解析不等于已过guard；受信调用层必须分别检查完整subject+guard_body（包含原HTML）和subject+body（确定性文本），
然后才执行256KiB判定。这样既拒绝隐藏在被去除标签中的marker，也拒绝用HTML标签拆开的marker。

5b receipt fingerprint必须显式规范化投影route所有绑定/版本、provider摘要、固定disposition/parser_version、
原值Message-ID/In-Reply-To、UTC时间、Raw ID/hash/size；不得对已排除敏感字段的model_dump直接求hash。
这些输入只在受信事务计算摘要，不进入日志/业务普通字段/模型。initial起点重启时沿耐久初值，不能用新时间重建。

## Task5b耐久事务与人工绑定

采纳[Task5b子规格](../superpowers/specs/2026-09-05-email-inbound-5b.md)。0058之后0059新增三张tenant技术表，
整页tenant锁/cursor CAS/receipt预检与bound Conversations及Outreach UoW唯一commit。未知提交以新session
核耐久cursor及完整receipt，不删Raw、不以内存成功替代。独立driver沿原singleton同backend生命周期。
绑定仅真人明确指定真实sid；SendingIdentity公开窄boss授权，受信单mailbox/route/configversion不受请求覆盖，
cursor保留confirmed_by/confirmed_at及固定bootstrap起点。同owner重启原行恢复，无行disabled，禁止换身份或重置。
Conversations新增当前员工事实式固定read/retry权限端口，复用active boss下限，review不伪造Message。
retry只接受expected_version，原位CAS；未到期Retry-After不得人工越过。永久协议/history/poison固定blocked，
不自动重试、不编辑cursor/跳最新/自由关联。安全API供Task8/9消费，本批不提供Web页面。

### 具名机械装配例外

API与scheduler可复用唯一apps/composition_support/email_inbound.py，仅构造本批fetch/raw.read
具名Gateway/registry/slot/Store/wrapper。各进程typed显式传入tenant、route、当前授权port、lease owner、
sessionfactory和外部port；不建engine/workflow/全系统registry，不读环境或启动循环，无全局实例。
构造无IO，资源对称释放仍归原进程生命周期；ADR0025四reader的禁registry规则不变。

### 5b最终消费者与日志口径

原StandardAuditLogger为独立运行日志，不是持久业务表；保持原TransactionAwareAudit的allow缓冲与提交后flush。
提交前缓冲/deny sink异常、Message、事件/outbox、receipt/review写失败均整页回滚。提交后日志sink失败记录固定错误，
已提交业务不得伪称回滚或重做。

自动入站driver以前述原InboundMessageStored消费者已在同进程完整注册为前提；reply_factory=None的
受控环境仅可人工绑定/状态/待核对查询，scheduler inbound_body能力明确disabled/required_ports_missing。
绑定active仅是绑定技术状态，运行能力必须读取原scheduler健康投影；Task8不得把两者合并为已运行。
无消费者时零fetch、零cursor推进、零新入站事件；Task6补齐原回复组合后自然启用，不造临时队列或空ack。
