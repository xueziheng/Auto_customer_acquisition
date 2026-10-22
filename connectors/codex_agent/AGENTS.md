# 按公司绑定的 Codex Agent 连接器

继承根和 connectors 规则。通过 model.generate / model.connection.test Gateway 使用。
复用 codex 的隔离进程生命周期，但使用独立多轮 broker 和固定只读上下文 MCP。原单次连接器默认行为不变。
仅显式官方接口；凭证在父进程 Connector 内解析，不进入子进程、任务文件、MCP 或日志。
每轮请求固定模型、允许工具清单、存储关闭、无自动重发。总轮数、输出额度、输入字节和总时间有界。
工具只读取当前任务已授权材料；不持有数据库、邮箱、浏览器 Cookie 或任意网络权限。
取消、超时终止整个进程组；结果必须与最后 Provider 文本一致。未知结果不降级或自动重新调用。
