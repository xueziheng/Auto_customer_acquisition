# 公共候选库存实施计划

**Goal:** 实现可复用公开候选库存、公司级领取去重与额度、Codex 有界来源采集，并部署进行 jslt UI 验证。
**Architecture:** 控制租户公共事实目录 + 公司租户独立领取账本；领域纯规则、Postgres 原子事务、Gateway 外部工具、API/UI 组合。
**Tech Stack:** Python 3.12、Pydantic v2、SQLAlchemy/Postgres、FastAPI、Vue3、Codex CLI。
**Spec:** docs/superpowers/specs/2026-10-13-candidate-inventory-design.md
**Global Constraints:** 所有表与查询带 tenant_id；凭证仅 Connector 父进程可访问；外部动作走 Tool Gateway；不把候选当作已验证需求或已验证邮箱；公司私有数据不进入公共池；归属/额度/去重由确定性代码执行；不覆盖原工作区和服务器已有改动；不发送真实客户邮件；所有中文文档与内部 UI 使用术语表；只在实际测试通过后报告完成。
**Review Focus:** 并发重复交付与超额、跨租户泄露、来源伪就绪、采集预算绕过、来源证据伪造、Codex 凭证隔离、UI 真实错误状态。

### Task 1: 领域规则、库存持久化和来源调度

工作目录 /Users/xueziheng/.codex/worktrees/candidate-inventory/Auto_customer_acquisition。基线 10ef5fd。先读 root/HANDBOOK 及 domains/prospecting、infra、shared、tests 适用 AGENTS。不得读凭证。使用 /Volumes/T7/Company/Auto_customer_acquisition/.venv/bin/python。

先更新 GLOSSARY 和新增 ADR0091，定义公共候选、候选分配、采集任务与受信公共投影。创建 domains/prospecting/inventory.py 领域模型/协议/确定性规则（可拆 inventory_schemas.py 与 inventory_planning.py）；infra/db/prospect_inventory.py 仓储；infra/db/prospect_inventory_tables.py；迁移0073。登记 ORM 到现有 Base metadata。公共目录绑定 control_tenant，公司仓储绑定 business_tenant，所有查询显式 tenant 过滤和 RLS。

领域公共接口统一经 domains/prospecting/service.py 重导出；提供可供后续 API/工作流使用的 InventoryService：status(tenant_id)、request(tenant_id, employee_id, query, count, request_key)、list_allocations(tenant_id, allowed_owner_ids, limit)、get_job(tenant_id, job_id)。request 必须先尝试公共目录新鲜命中，原子独立公司分配、公司日额度默认50/平台可设、每次上限100、幂等参数冲突失败。公共目录候选至少 id、canonical_domain、name、country、product_tags、public_emails、source_id、source_url、evidence_text、observed_at、expires_at；模型严格校验公开来源、不接受私有数据自由推广。每家公司企业域名及邮箱去重跨查询跨员工。老板/经理读取的 allowed_owner_ids 由上层传入，None 仅上层已授权全公司。所有新 ID 增加强类型。提供平台 set_daily_limit 的公司绑定服务方法，平台 API 后续接线。

不足时在同事务创建 durable acquisition job，保存 query、requester、requested_count、remaining_count、lease/status/attempts，能领取任务、记来源尝试、保存公开结果、再次交付到原员工。可用补充端口由你确定清晰签名并在报告列出。不要在本任务写外部采集或 API/UI。

来源调度读取 shared/schemas/discovery_sources.py 注册表；确定性选择函数接受可用状态、历史新增有效数量、尝试次数、上次使用与请求预算，优先低成本/相关/有收益并轮换探索，至少一个探索名额（有其他可用源时），不会因前源已取得目标永远跳过后源。所有 source 状态真实区分，不能以 selected=可用。不要编造成本数字或置信度。

TDD：先写行为测试观察失败，再实现。重点：相同域名/邮箱重复、同公司两员工重试并发、另一公司独立、日额度原子、过期不分配、私有记录拒绝、provenance必填、来源轮转与不可用排除、任务租约重启恢复。Postgres 行锁与 unique 约束保证真实并发；SQLite 测试不算并发证据。为真实数据库准备 tests/integration 对应测试（可启动本地隔离 Postgres，不得用生产测试）。迁移 upgrade/downgrade/upgrade roundtrip。不要伪造通过。提交限定自身文件并给出真实测试证据。

### Task 2: Codex 采集工具与持久任务执行

依赖 Task1 接口。读取 AGENTS。新增 connector/gateway 插件而不改核心管线。保持现有 codex_agent 只读模式；新增可选 discovery 模式通过受信父进程 broker 执行 source_search、read_public_page、browse_public_page 工具，子进程永不收到凭证。复用现有 web.search / web.read_page 网关与安全 DNS 校验。公开浏览器连接器以独立进程隔离、超时与输出上限，浏览器只允许 public HTTP(S) 并阻断 localhost/私网/重定向 SSRF。

Codex 调用来源以注册表和调度允许列表为边界，限制工具轮数、请求数、模型 token 和时长。API 型连接器使用确定性适配器；未实现或未授权来源明确不可用。优先接通可用免费路径（公开网页/搜索 + 已有适配器），按实际测试结果报告覆盖；不假装33个都成功。来源证据必须来自实际工具结果，URL/邮箱从内容校验，不接受模型自造证据。将 acquisition job runner 接到既有 scheduler/composition，单实例/租约/幂等，失败和不确定状态显式可查。增加针对预算、证据、工具调用、取消与租约的测试和模型 eval。提交自身文件。

### Task 3: API、平台额度与用户页面

依赖Task1/2公共接口，在 apps/api 组合已授权身份和公司绑定仓储；新增 /prospects/inventory 的状态、请求、分配、任务/来源接口，平台管理员可改 daily_limit，普通员工不能。复用session和ABAC，不接受请求体tenant/owner。业务app只持公开投影端口。Vue CustomerDiscovery增加查询/数量/来源状态/任务进度/领取结果/证据/历史列表。中文明确标记未验证邮箱与候选事实，错误可重试且不重复扣额度。生成OpenAPI TS类型，不手写重复DTO。API租户鉴权/额度/幂等与UI交互测试、npm build。提交。

### Task 4: 集成审查、部署与 jslt UI 验证

检验全部新测试、相关回归、结构自检、迁移回滚往返、前端build。独立全分支审查后修复重要问题。发布新release保留现有配置/原release可回退；迁移显式执行，应用启动不迁移。生产仅最小真实查询和UI操作，控制服务器资源。

使用 https://trade.jslingte.com/ 的jslt账号进行UI验证；优先已有会话/浏览器自动填充，不读取密码文件。若登录需要用户输入，只请用户在页面直接登录并继续独立工作。验证库存、少量交付、第二次不重复、刷新持久、来源真实状态、报错反馈。联系人发现/邮箱验证/自动开发信/回复入站逐一报告实际运行状态，外部凭证/付费来源受限不伪造成功，真实客户发信不在本任务自动执行。保存中文验收报告（有截图、命令、结果、失败与未验证项），最终简要汇报实际结果与限制。
