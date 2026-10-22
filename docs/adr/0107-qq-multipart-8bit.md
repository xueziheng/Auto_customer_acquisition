# ADR0107：QQ multipart/alternative 的8bit父层编码

日期：2026-10-19。状态：隔离修复中，生产未发布。

真实D回信在搜索和精确回复关联之后，因match_headers要求multipart父层编码必须7bit而停止。
独立最小头诊断证明：纯文本和HTML两个BASE64叶子、无附件、边界及参数精确匹配，
父层Content-Transfer-Encoding为8bit。原正文和模型计数均0，失败记录保持不变。

[RFC2045 §6.4](https://www.rfc-editor.org/rfc/rfc2045#section-6.4)允许multipart使用7bit、8bit或binary。
本次只补齐观测到的8bit标记，保留binary不支持；不对multipart做base64或quoted-printable解码。
子部分仍分别核验编码、字符集、原始字节大小、行数、边界及无附件约束。
不扩大正文额度、不重置停止试验、不把技术原件诊断当作真实自动闭环。

新增合成用例先证明8bit父层被原实现拒绝，再验证7bit/8bit可用、子部分大小不一致仍拒绝、
其他父层编码仍拒绝。只读技术格式诊断与后续新回信自动闭环分开记录。


## 整封格式诊断补充

D的独立技术诊断（原trial保持stopped、没有模型或业务写入）验证原件4795字节；
QQ有固定preamble `This is a multi-part message in MIME format.` 加两个CRLF，epilogue为空。
两个叶子的BODYSTRUCTURE分别计886/1556字节及13/22行，按MIME分隔规则切出的内容为
884/1554字节及12/21行，差值均为原件中分隔符前真实存在的一个CRLF。

只允许空前言或这个精确固定说明行，不允许任意前言/尾声。叶子校验允许QQ将这两个原始分隔字节
计入大小的方式：仅当声明大小恰好多2时，从原件切片纳入该CRLF，再通过同一递归验证复核行数、
解码和所有其他边界。没有加造字节、没有统一忽略差值、没有改写归档原件。
真实解析器/socketpair与原Gateway/PG整页事务测试覆盖两种计数方式及异常前言、尾声、单字节差、
单行差、截断边界仍拒绝。诊断原件不导入会话，不算自动业务验收。
