# 企业隔离实施计划

> For agentic workers: 使用 superpowers:subagent-driven-development 分工实施和独立检查；用户已明确要求解决企业隔离，按本次授权在云端开发副本完成实现与验收。

**Goal:** 给现有贸易业务增加数据库强制隔离、固定企业 API 容器与独立知识任务资料边界，使同名产品和文件不能跨企业读取。

**Architecture:** 保留固定 tenant 的业务实例；同进程容器只分发到受信注册的独立子 app。PostgreSQL 每企业使用独立且无特权的登录角色，RLS 按数据库当前角色而非请求参数决定行归属；所有原有 WHERE tenant_id 仍保留。知识文件由已授权数据生成任务专属快照，模型无权读取共享父目录。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2、PostgreSQL、现有对象存储与 Codex CLI 受限执行环境。

**Spec:** ../specs/2026-10-07-jslt-enterprise-access-design.md 的 1.1、3、8、9、12 节；账号邀请和平台经营界面独立实施，不能用隔离基座冒充这些能力完成。

## Global Constraints

- 仅修改阿里云开发目录；原 WSL2 和 Mac 项目不动。
- 保留九条硬边界、企业内客户负责人范围、原业务审批与 Model Gateway；不发邮件、不进行收费模型测试、不恢复数据备份。
- 本企业已发布产品是客户探索起点；产品适配不能直接作为已验证需求。
- 生产只从验收后的新 release 发布；失败必须保留可恢复启动路径，未验证的多企业入口不开放。
- 数据库凭证不出现在模型、日志、报告、Git 或终端输出中。

## Review Focus

1. SQL 遗漏 WHERE、修改 GUC、SET ROLE 或 TRUNCATE 均不能逃离运行角色的企业。
2. 两企业同名产品、同文件名、同会话问题及错企业路径/ID 不串数据。
3. cookie、子 app lifespan、并发请求和启动失败清理不互相污染。
4. 软链接、硬链接、路径穿越、混合企业文档和任务取消不得读取或残留其他企业文件。
5. migration owner 与 runtime role 分离，真实 PostgreSQL roundtrip、上线 readiness 和旧账号登录不能用 SQLite 或假服务代替。

## Task 1: 数据库与仓储隔离

Files: infra/db/base.py; infra/db/tenant_security.py; migrations/versions/0070_tenant_row_security.py; tests/unit/test_infra_db.py; tests/unit/test_tenant_security.py; tests/integration/test_tenant_row_security.py.
Interfaces: tenant_database_role(tenant_id: str) -> str; assert_tenant_database_isolation(engine, tenant_id) -> None; provision_tenant_role(connection, tenant_id, password: SecretStr) -> None。

- [x] 先增加构造空企业、无过滤后门、两个真实 PG 登录角色和无 WHERE 跨企业读写的失败测试。
- [x] 关闭 unsafe_cross_tenant_query，运行角色名采用 tradeos_t_ + SHA256(tenant_id) 前 48 位 hex（长度 58）。
- [x] 0070 对所有 tenant_id 非空业务表启用 ENABLE/FORCE RLS；USING/WITH CHECK 比较 current_user 与行 tenant 对应角色，不能使用客户端可改 GUC 作为授权。
- [x] 可信角色准备仅授 SELECT/INSERT/UPDATE/DELETE、schema USAGE、版本表 SELECT；NOSUPERUSER/NOBYPASSRLS/NOCREATEDB/NOCREATEROLE/NOINHERIT，无 owner/TRUNCATE/其他角色成员资格。
- [x] 真实 PG 验证读写、改 tenant、SET ROLE、TRUNCATE、并发、无授权角色，以及 0069→0070→0069→0070。

## Task 2: 固定企业 API 容器及运行门禁

Files: apps/api/enterprise_container.py; apps/api/authentication.py; apps/api/routers/authentication.py; apps/api/main.py; tests/unit/test_enterprise_container.py。
Interfaces: 服务器受信 registry 提供固定 tenant app；cookie 命名空间和 Path 由装配决定，请求不得自报授权。

- [x] 写两个真实认证子 app 的路由、cookie 交换、CSRF、错企业与生命周期失败用例。
- [x] 增加显式 cookie name/path 配置且保留单企业默认行为。
- [x] 父 lifespan 用 AsyncExitStack 管理所有固定子 app；拒绝重复 tenant/key、未认证 app 或共享不合规 cookie。
- [x] 保留现有身份 tenant 精确断言；非注册企业路径拒绝，不动态复制 JSLT 配置。
- [x] 验证测试和 OpenAPI 兼容；未装配 worker 的企业不得宣称自动化已启用。

## Task 3: 企业知识资料任务边界

Files: connectors/obsidian/workspace.py; connectors/obsidian/AGENTS.md; tests/unit/test_obsidian_workspace.py。
Interfaces: 由上游权限检查后的文档和固定 tenant/employee/run 生成只读工作目录；connector 不决定业务授权，不接受客户端绝对路径。

- [x] 先写双企业、重复任务、路径/链接、混合企业、内容完整性及异常清理用例。
- [x] 生成任务专属目录和程序文件名；不读取企业父目录，不信任 .codex/AGENTS.md，清理仅限本次拥有的文件。
- [x] Codex 后续装配只能接受该工作目录；没有通过隔离验证的旧共享 vault 入口不得用于多企业任务。
- [x] 准确记录资料上传/检索/CLI 网页接线是否已存在，不以 connector 单测宣称知识中心已完成。

## Task 4: 云端运行角色切换及真实验收

Files: infra/pilot/config.py; API/scheduler/notification 实际 engine 装配；scripts/configure_enterprise_database.py 与 tests/integration/test_enterprise_api_isolation.py；docs/adr/0081-enterprise-product-isolation.md；AGENTS.md/HANDBOOK.md；docs/acceptance/2026-10-07-enterprise-isolation.md。

- [x] 配置支持固定企业 runtime 数据库角色，启动时验证角色/表策略；迁移账户与业务运行账户分离。
- [x] 使用合成企业和受限真实运行角色跑 API/资料/数据库隔离，现有业务读写和管理员认证回归。
- [x] 跑结构自检、相关单测和真实 PG 检查，再独立安全审查；发布前明确尚未接入的网页知识与平台账号功能。
- [ ] 显式迁移及切换新 release；验证网页、会话、worker 和 RLS 状态，保留旧 source release 但不创建业务数据备份。
