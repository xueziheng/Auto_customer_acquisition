# infra/ —— 部署与持久化适配

## 职责

本目录只承载部署配置、数据库会话/仓储适配、迁移与本地编排。业务规则必须留在
`domains/`，流程编排必须留在 `workflows/`，不得为了部署便利复制到 infra。

## 安全与数据边界

- 不得打印或记录 DSN、密码、Token、Secret 或其原始异常消息。
- 环境变量样例只能给不可运行的形状占位符，不得出现真实凭证或可误用的默认阈值。
- 所有业务 SQL 必须显式或通过 tenant-bound 基座强制 `tenant_id` 过滤。
- 模型与 Agent 不得接触本目录持有的任何凭证。

## 迁移纪律

修改迁移必须验证 upgrade → downgrade → upgrade roundtrip，且迁移前后 schema 与 ORM
契约一致。禁止在应用启动时自动升级数据库。
