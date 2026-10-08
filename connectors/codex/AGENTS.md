# Codex 单次资料处理连接器

继承 connectors 与根总纲。仅由 model.generate Gateway 调用；不查业务库、不发邮件、不写企业知识库。
每个实例绑定受信 KnowledgeTaskScope；调用身份不得来自模型正文。每次独立临时目录、只读任务快照、
CODEX_HOME 与 PID/网络/文件系统命名空间，无跨任务历史、缓存、工具或额外网络。
父进程持有 DeepSeekClient 和凭证。CLI 只有隔离 loopback HTTP → 单任务 Unix socket 通道；
broker 固定当前 ModelRequest、最多一次实际 Provider 请求，拒绝 CLI 修改请求或重试。
只接收完成的最终 JSON，CLI 成功且最终 JSON 与原 Provider 完全一致才交付，计量来自真实 Provider。
超时/取消杀整个独立进程组，清理仅本次创建的目录。未知发送不自动重发；异常不含原文。
bwrap/固定 CLI/系统 Python 必须部署方显式配置并离线验证；缺失时失败关闭，无无沙箱降级。
此入口不提供多轮 shell Agent；原件解析、权限、Provenance、确认发布与持久 vault 写入由受信上游负责。
