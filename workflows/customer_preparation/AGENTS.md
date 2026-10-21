# 客户自动准备编排

本目录消费精确获批 Campaign 与当前企业确认资料，编排账户分配、公开证据、需求假设、联系方式依据、实际验证和受批入组。硬边界1/4/5/6/8/9全部适用。

只调用域公共service/schema与显式端口，不导入apps、域内部repository/models或SDK。不得伪造人工确认、法律评估、客户需求或验证结果。每阶段重新检查当前租户、员工、owner、Campaign版本、确认资料与政策。外部未知结果只核对，不重新派发。准备项只保存安全引用及固定状态，不保存原文、邮箱或凭证。

同事务Account/Ownership机械桥位于infra/db/customer_assignment.py，由进程各自注入。流程不持数据库连接，不自建HTTP客户端，不扩Campaign授权。
