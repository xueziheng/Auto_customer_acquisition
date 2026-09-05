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
