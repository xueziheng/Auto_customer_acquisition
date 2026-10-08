# 四账号真实落地实施计划

> **For agentic workers:** 按用户已明确的实现要求，采用并行子任务实现并独立审查，所有源码只在阿里云开发目录修改。

**Goal:** xue 进入独立平台概况；jslt、qihao、qikai 进入 JSLT，各自权限有效；旧账号与旧会话失效。
**Architecture:** 保留固定企业 API；新增独立控制 tenant、授权/目录/审计及平台只读 API。同一前端构建通过 /platform 与 / 完整导航选择相互隔离的认证状态。
**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / PostgreSQL RLS / Vue 3 / TypeScript。
**Spec:** ../specs/2026-10-07-jslt-enterprise-access-design.md；本批取四账号专项，变更边界见 ../../adr/0082-platform-account-boundary.md。

## Global Constraints
- 仅在阿里云 /srv/tradeos/development/TradeOS 写源代码；重型测试使用 CI。
- 所有表、读写、审计都绑定 tenant；保留 Origin/CSRF/Web Locks 及后台权限。
- 平台不加入 JSLT、不执行企业写入/审批/模型任务；未知统计不冒充零。
- 新账号验证后取消其余账号和会话；历史员工保留但停用。
- 不发送邮件、不调用模型、不新增备份；临时测试资源验完清理。

## Review Focus
- 企业 Cookie 误用于平台、平台 Cookie 误用于业务：双方必须拒绝。
- 平台 grant 撤销后旧会话继续访问：每次读取重新检查。
- 只读事务或 tenant reader 错配：启动验证 RLS，运行查询限定目标 tenant。
- 无统计来源与服务失败：显示未知，禁止把 5xx 画成零。
- 初始化重复或失败：事务回滚，不留下半套账号；账号停用与会话同时撤销。

### 1. 平台后端与持久化
- [ ] 新增 domains/organization/platform_access.py，DTO/Protocol/授权服务。
- [ ] 新增 infra/db/platform_access.py，固定 reader 与只读统计、控制 tenant 审计。
- [ ] 新增三个 ORM 表及 0071 迁移，RLS 两策略、复合身份引用、迁移往返。
- [ ] 新增 apps/api/platform_access.py::create_platform_app(control_tenant,engine,origin,readers)；独立 Session DTO，固定 /api/platform Cookie。
- [ ] 单元与真实 PostgreSQL 集成验证身份、CSRF、撤权、越租户拒绝。

### 2. 平台前端
- [ ] /platform 使用独立 PlatformConsole 与平台认证模块；原业务会话不运行。
- [ ] 入口链接、平台身份、企业概况与成员、未知/失败态，手机尺寸不溢出。
- [ ] 平台类型由独立 OpenAPI schema 生成；认证并发/登出失败/401 清理测试。

### 3. 受信初始化与装配
- [ ] 新增私有 platform 配置与应用装配；父 lifespan 同时管理平台和业务。
- [ ] 可信命令创建控制身份/grant、JSLT 三账号与经理绑定、平台目录。
- [ ] 先 dry-run 核对旧账号与负责人；应用时事务停用旧账号/员工并撤销会话，重复配置拒绝或幂等核实。
- [ ] 密码仅私有文件输出给用户，不出现在命令行/日志/Git。
- [ ] 迁移、旧租户角色新表授权、平台角色、配置文件与新 release 明确执行；不启动时自动迁移。

### 4. 验收、部署与交付
- [ ] 结构自检、敏感扫描、类型/lint、相关单元与集成、迁移 roundtrip、前端构建、CI。
- [ ] 独立复查平台越权、客户负责人范围、旧账号撤销。
- [ ] 新 release 切换；健康、四账号真实 HTTPS 登录/恢复/退出、旧账号失败、各权限反向检查。
- [ ] 浏览器核验平台与企业页面；提交 push 核对远端；清理临时文件/测试 Docker。
