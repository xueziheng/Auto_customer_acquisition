# Task5a：邮件正文技术读取与不可变原件子规格

日期：2026-09-05。依据已批准Task5/inbound-design-report；此批只交付技术候选页，未实现Message入库、需求验证或商机。

## 契约与范围

新增shared frozen、extra=forbid DTO与窄Protocol：InboundRoute绑定tenant/mailbox/configured identity/route/config version；ProviderInboundItem仅进程内带原bytes与完整候选subject/body，ArchivedInboundItem仅保留必要原值关联header、时间、provider摘要、固定disposition及Raw元数据。所有cursor/header/content字段repr=False、序列化默认排除；不进入ledger/日志/模型。tenant及identity绝不取自邮件。

ToolEmailInboundReader.fetch(tenant_id, mailbox_alias, cursor: str, page_limit: int)返回ArchivedInboundPage；含route、starting_cursor、next_cursor、items。composition用initial_inbound_cursor(route, bootstrap_started_at, after_epoch)构造初态，5b保存后原样消费，无需workflow导入Gmail。Provider get_inbound_message返回typed raw/labels/internalDate或永久too_large/message_gone/malformed。

## 固定预算与cursor

gic1与gfc1互斥，最大32KiB，绑定完整route。初态只读取profile得到水位，返回空锚定页；持久化后固定bootstrap after_epoch，最多回看30天。bootstrap全部pending/分页处理完再从锚点进入history。history全部页处理完才采用返回水位；404 history阻断，不跳到最新profile。每调用最多读取一页refs，重复refs页内去重，空页可前进，重复pageToken固定拒绝。pending保存未交付ref，不保存内容，重放重新读原件。

每页1–20项、8MiB原bytes；每消息4MiB，header累计64KiB、parts100、深度20、解码候选4MiB、最终候选256KiB。HTTP读取有界哨兵、base64先校验编码长度再逐块解码；MIME遍历在构造子树前限额。永久超限固定隔离，不作为网络临时错误。页byte预算不够时当前ref留在pending。完整候选先经过注入CredentialMarkerGuard，再检查256KiB，超限隔离不裁剪。

## 解析与安全

排除SENT/DRAFT/SPAM/TRASH，不依赖From猜方向；顶层DSN/ARF及未知report固定跳过。自动回复和普通退订仍为候选，交后续分类。只接受唯一语法严格Message-ID及唯一精确自有In-Reply-To，保留大小写和尖括号；不从References/Subject/域猜关联。Date必须唯一且带明确timezone，转换UTC；internalDate不替代Date。不展开附件或嵌套转发成客户原话；HTML只确定性文本化，不执行脚本或访问网络。本批不查account/出站SENT，不宣称已验证关联。

email.inbound.fetch独立manifest/checks/handler，LOW/FREE/NONE，仅tenant→permission。prepare只参数验证和HMAC cursor指纹；受信route/identity/预算/endpoint/secret不可override。真实EXECUTING后reader才解析已配置凭证。生产endpoint固定gmail.googleapis.com；loopback必须显式controlled mode和端口；redirect拒绝。

容量一task-owned单次槽，ContextVar继承的child不得take/discard父槽。只有真实SUCCEEDED且页tenant/mailbox/cursor匹配才领取；错handle/重复/失败/取消清当前槽。ledger只安全handle和固定分类，不能复原page。

Gateway内通过注入infra archiver执行Raw.put(EMAIL_RAW)再bounded get，核验实际tenant/kind/mime/hash/size及bytes等于输入。任何put/未知commit/读回失败整个fetch失败；metadata去重不代替实际读取。凭证marker邮件先原样归档再隔离，零Message/model。业务事务回滚不删除不可变原件；保留Raw旧补偿行为及其未知提交局限，绝不假报成功。

## 5b消费与验收

5b负责三张技术表、tenant锁/CAS/整页唯一transaction、真实SENT关联、Message/event/receipt/review/cursor一起提交、待核对权限/原件下载API。5a只交typed archived候选，无schema迁移或正文worker。

真实受控ControlledGmailTransport持久SQLite只模拟Provider；真实Connector/Gateway/PG metadata/本owner MinIO验证。复用OwnedContainers/OwnedProcess，不启真实Gmail/OpenAI/客户动作，不读取既有凭证配置。覆盖解析矩阵/分页/读取预算/拒绝零凭证调用/slot继承重放取消/完成ledger失败/实际原件损坏及未知提交。最终跑直接旧兼容、改动ruff/mypy、结构与增量敏感门禁。

## 纯Raw内容解析与后续消费

新增 `parse_inbound_content(raw_mime: bytes) -> InboundContent` 纯解析口（connectors/gmail/inbound_mime.py），
`parse_inbound_message`复用它。InboundContent只有固定disposition/parser_version和进程内subject/body/guard_body，
没有tenant、发件人、关联或Provider元数据。Task6在Gateway授权Raw读取后复用此口，不得伪造labels/internalDate。
解析不等于已过guard；受信调用层必须分别检查完整subject+guard_body（包含原HTML）和subject+body（确定性文本），
然后才执行256KiB判定。这样既拒绝隐藏在被去除标签中的marker，也拒绝用HTML标签拆开的marker。

5b receipt fingerprint必须显式规范化投影route所有绑定/版本、provider摘要、固定disposition/parser_version、
原值Message-ID/In-Reply-To、UTC时间、Raw ID/hash/size；不得对已排除敏感字段的model_dump直接求hash。
这些输入只在受信事务计算摘要，不进入日志/业务普通字段/模型。initial起点重启时沿耐久初值，不能用新时间重建。
