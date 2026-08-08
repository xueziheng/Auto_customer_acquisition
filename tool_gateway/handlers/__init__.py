"""工具 handler 注册处。

每个工具一个 handler 模块，实现 manifest.ToolHandler，在应用启动时
连同 manifest 一起注册进 ToolRegistry。

Phase 1 需要的 handler 清单（实现时逐个补）：

    email.send            经 connectors/gmail 发送（幂等 REQUIRED、HIGH）
    email.fetch_replies   拉取回复
    contact.verify        经 connectors/email_verification 验证可达性
    contact.enrich        经 connectors/contact_enrichment 补全
    web.search            经 connectors/web_search 搜索
    web.read_page         读公开页面（带快照与哈希）
    dns.check_auth        SPF/DKIM/DMARC 校验
    artifact.store        存原始资料
    notify.send           经 notification_gateway 发通知

约定：handler 不含业务规则，只做参数组装、connector 调用、结果转换。
凭证在 handler 内部经密钥服务获取，不进日志（硬边界 1）。
"""
