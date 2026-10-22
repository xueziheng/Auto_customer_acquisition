# 企业 Agent 升级代码基线

日期：2026-10-21。本文件记录代码来源，不代表生产部署或业务验收完成。

- Git 基点：`origin/codex/jslt-enterprise-access`，`22ece5a86d374795437407a67154ceba2973e1b9`。
- 原工作区：`/Volumes/T7/Company/Auto_customer_acquisition`，HEAD `aebaa9b6c0ca2c148cc9ffc9d8c926e6f7f8f85f`，含用户未提交变更，完整保留。
- 导入源：`/Users/xueziheng/Documents/Codex/2026-10-19/tradeos-qq-acceptance/receipt-fix/src`。它是多轮开发累计目录，`.deployment-commit` 标记 `a7466856ea65c6fb68bcf4824fbc7fe47c1d75c5` 不能代表后来全部文件均部署。
- 两次逐文件哈希验证后冻结 2947 个源码/文档/测试文件；相对 Git 基点导入 704 个新增或内容变化文件。统一 CRLF/LF，不因行尾差异重写其余文件。没有删除原 Git 文件，没有读取或复制 private、凭证、运行数据、node_modules、dist 或缓存。
- 源包含 business-inbound、model-call-limits、ordinary-reply 等后续修改；ordinary-reply 尚有未完成测试。升级将其作为待整合实现，不能按目录名误认作已验收代码。
- 根总纲沿用受约束版本，不导入源码副本中的私有邮箱测试例外。此次不改变九条硬边界，产品起点与沿用 Codex CLI 见 ADR0113。

导入后结构检查通过；单元测试收集得到 9634 项和两个缺失模块错误：`connectors.qq_mail.reply_source`、`reply_client`，其共享请求 DTO 亦未实现。后续修复与完整结果记录在升级验收文档。

原工作区的 `0070_prospecting_service_plans.py` 与新版 `0070_tenant_row_security.py` 迁移号冲突；ADR0078/0079 亦有同号异义。必须按语义整合和新编号迁移，不按文件名覆盖。现有数据库、DeepSeek/API 连接、来源状态/限额/备注、员工邮箱、Obsidian 原件均不在代码导入范围内，不做重置。
