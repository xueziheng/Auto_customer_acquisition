# QQ v4 离线格式准入与候选停止

本轮是原 connector/Gateway/UoW 的离线增量。没有新系统，没有真实邮箱或生产数据访问。

## 地址语法证据与候选处理

本机已知 Q02 证据目录只有四份摘要/结论；它们记录了回复 From/To 各两个 InvalidHeaderDefect，却没有原始字段字符串。Downloads 的 eml 是最初收到的测试信，不是该回复。因此没有实现地址容错，也没有用合成的双 defect 夹具冒充真实语法。真实 Q02 的格式兼容仍不能证明。

原 `reply_headers` 严格规则保留。受控 trial 中，若必需头不通过，但 In-Reply-To 头的完整 ID 列表含精确的已核验 provider ID，且没有一个可严格解释的单地址明确排除当前双方，则先持久记录 `reply_headers_unsafe` 并终止本次 trial。无正文、无新游标返回、无归档；不自动重新领取预算。当前租户、授权、原 SENT attempt、双方、UID 水位和预算检查保持原样。

明确不同的单地址、不同 ID 或只有包含/前缀匹配的非关联内容仍跳过。邮件引用本身不是身份认证；准确引用也可能被伪造，因此这里只授予“停止等待核对”，不授予正文读取或业务分类。终止作用于这次有期限的 trial，沿用原账本和停止事件，不是无限重试。未扩展无 trial 的旧读取路径。停止日志仅记录固定原因并关联原 trial/attempt/水位，不存原始异常或邮件正文；没有新增字段存放 UID/原始头，后续人工检查仍需另外明确范围。

## 同一次候选头 FETCH

在原受控 trial 的头请求中加入 BODYSTRUCTURE，仍为一次 HEADER.FIELDS FETCH，不新增 SEARCH 或其他抓取。没有 trial 的路径保持原查询。

IMAP 完整响应解析器支持 quoted string 与有界 literal，验证完整闭合、唯一属性、准确 UID、RFC822.SIZE 和完整 HEADER.FIELDS 标签。支持 BODYSTRUCTURE 位于头 literal 前后；literal 中的内容不会再作为 UID 或属性解释。重复/额外响应、部分读取标签、未知属性、声明长度不符、残余尾部均拒绝。

限额：头 literal ≤64 KiB，总头响应 ≤128 KiB，非头元数据 ≤64 KiB；原 socket readline 的 16 KiB 行上限继续生效；最多32片段、256语法节点、8层协议列表。正文 literal 必须正好等于批准的 RFC822.SIZE，仍不超过本 trial 的 body_bytes（最高256 KiB），且只有一次。完整正文 FETCH 同样核对响应闭合、UID、字节数，拒绝额外响应。

## 最小 MIME 范围

允许单体 text/plain、text/html，以及根 multipart/alternative 恰含按顺序排列的 text/plain 和 text/html 两个叶子（最多两层、总计三个 MIME 部分）。multipart 必须有合法、唯一 boundary。没有结构证据时 multipart 仍以 `mime_structure_unverified` 停止。

不支持 mixed、嵌套 multipart/message、重复类型/多余部分、附件、filename/name/扩展参数、未知 disposition/扩展、Content-ID/description/MD5/language/location。叶子只允许 charset 参数，当前字符集为 UTF-8、US-ASCII、GB18030、GB2312、GBK、ISO-8859-1；编码只允许 7bit、8bit、base64、quoted-printable。其他格式保守停止，不算“所有 QQ 客户端格式已支持”。

读取前核对顶层头与结构，Bcc、附件、重复/畸形 MIME 头及总大小限制保留。允许的一次 MIME 返回后，按原字节验证 boundary 分段、叶子数、各部分 MIME 头/参数/编码、编码语法、字符集解码、声明字节数及行数；拒绝前后附加内容、截断、隐藏部分和不一致。原 UID/Message-ID/In-Reply-To/双方/Date 一致性检查仍在。这里不把共享解析器的 CANDIDATE 当作“无附件”证明，也不重写原 MIME。

此实现要求严格 CRLF、完整闭合 boundary 且没有 preamble/epilogue；部分标准合法但未纳入最小范围的邮件会停止。真实 QQ 的 BODYSTRUCTURE 行数及格式尚未观察验证。结构与长度一致不等同于供应商内容真实性或端到端密码学证明；原件归档哈希沿用原链路，读前判断依赖受信协议响应，若正文与响应不符则停止且不归档。

## 验证与保留边界

新增测试使用固定合成数据、socketpair 和原隔离 PostgreSQL。标准 alternative 正向经过原 Gateway/原事务提交，只产生一个收件记录与一份不变的原字节归档；重复扫描不再次读取。地址不安全、附件结构和结构限额失败发生在正文之前；正文不一致消耗一次正文预算后永久停止，不退款、不提交游标、不归档、不分类。

v3 的标记折行和 recipient_export/sender_sent 门禁保留。未实现自动 alias collector 或生产装配，没有新迁移，已有36项完整运行时环境错误继续单列。没有服务器、对象存储或邮箱访问，没有真实模型、OAuth、发信或 Q01/Q02 重放。
