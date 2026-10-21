# Task 1：QQ 普通回复传输报告

- 结果：实现缺失的三个模块并通过相关受控测试；仅传输实现，不代表生产启用。
- 任务开始基线：`c058a595d83ea298e668ba6bbd0977e50382d02b`。
- 实现提交父节点：`7715b40`（父任务补充导入夹具并排除旧构建备份）。
- 实现 HEAD：`3f7665de7bd77fd98a6edfbb3db0390c28a9b9a6`。

## 变更

`SMTPReplyRequest` 为 frozen DTO，拒绝不安全文本、非裸邮箱、可变/畸形线程和不安全幂等标记，按固定 JSON 的 SHA256 绑定双方、主题、正文与线程；repr 隐藏内容。

`parse_reply_headers` 只解析有界头，优先使用唯一 Reply-To，确认 To 包含绑定邮箱，拒绝重复关键字段、多人来源、群组、自回复、自动信、列表信、报告信、原始或解码后的控制字符与畸形 References。正常可重复 Received 头允许；线程去重保留大小写并追加来源 Message-ID。

`QQBusinessReplyConnector` 组合现有 QQ 连接器，复用固定 TLS 端点、认证、Sent 文件夹解析、SMTP 安全分类与关闭。发送 MIME 包含确定性标识、幂等标记、真实线程和内容摘要，不带营销 List 头。发送前精确匹配所有候选的单一关键头；冲突和不确定结果要求核对。reconcile_once 只读；SMTP DATA 超时不判为未发送；Sent append 失败不撤销 SMTP 接受事实。

## 验证

初始 RED：两个现有测试模块因缺失 reply_source/reply_client 收集失败，随后实现通过。额外 RFC2047 编码 NUL/CRLF 和重复 Received 用例均观察到失败后修复。

命令前缀：`PYTHONPATH=. /Volumes/T7/Company/Auto_customer_acquisition/.venv/bin/python`；工作目录为本工作树。

- `-m pytest tests/unit/test_qq_business_reply_source.py tests/unit/test_qq_business_reply_connector.py tests/unit/test_qq_mail_connector.py tests/unit/test_aliyun_mail_connector.py -q --basetemp=/tmp/tau-task1`：**95 passed**（60 个普通回复测试，35 个既有 QQ/Aliyun 测试）。
- `scripts/check_boundaries.py`：全部通过。
- 三个新增实现及两组关联测试 `ruff check`：通过。
- 三个新增实现 `mypy --follow-imports=silent`：通过。
- `git diff --check`：通过。

## 边界与未运行项

- 未发送真实邮件，未读真实凭证，未访问服务器或验收源码，未修改依赖、Gateway 核心、业务策略或部署装配。
- 来源头只提供路由和线程事实，不证明发件人身份、邮箱可达性或 Campaign 授权。调用方必须继续执行当前权限、已验证联系人、Campaign、抑制、商业承诺审批等检查。
- 连接器没有持久幂等存储；并发排他和未知结果的持久阻断由 Gateway/业务调用方负责。调用方必须将 unknown 后续请求送入 reconcile_once，不能再次当成新发送。
- Sent 对账使用精确头与内容摘要声明，不重新下载并计算已发送正文哈希；不将该摘要描述为独立内容真实性证明。
- 全库回归与依赖修复由父任务执行，本子任务仅报告上述实跑范围，不声称全库通过。父任务已报告冻结基线存在范围外失败，含旧 quotation lifecycle 夹具缺 customer_verification 和 macOS Unix socket 路径限制；本次未修改它们。
