# Phase 2 免费来源获客 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 在同一 TradeOS 交付三线路免费来源研究获客，保留既有触达流程和 Phase 2 寻源/成本/报价范围。

**Architecture:** 供应商适配在 connectors，免费账户额度账本在基础设施且从既有 Tool Gateway handler 接入，核心检查管线不变。版本化探索配置与工作流承载 research_only；业务域保存可追溯信号与待验证假设，应用层组合与展示。

**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2、PostgreSQL；Vue 3、TypeScript、Ant Design Vue。

**Spec:** `docs/superpowers/specs/2026-08-27-phase2-free-discovery.md`

## Global Constraints

- 模型永不接触凭证；所有外部动作经过 Tool Gateway；不修改 Tool Gateway 核心检查管线。
- 金额使用 Decimal；置信度由证据等级确定；事实与推断分开，关键字段带 Provenance。
- 所有表带 tenant_id，查询强制租户过滤；域间零直接导入，shared 不反向依赖。
- 只研究模式不补联系人、不验证邮箱、不发信、不报价；旧工作流和旧提案保持原语义。
- Tavily 只用 basic，免费状态未知、计费开启或额度不足即阻断；不确定调用保留预留且不自动重试，不付费回退。
- 三线路为 importer、distributor、ecommerce；线路不等于企业身份、运输记录或采购意向。搜索摘要不能成为原网页证据。
- 内部 UI/文档/日志/说明用中文；前端类型从 OpenAPI 生成。
- 不新增 Phase、不实现商业供应商客户端、不改变 Phase 1 真实验收状态、不将本批算作 Phase 2 全部完成。
- 在隔离工作树实施；每个切片先 RED 后 GREEN，记录命令和结果；提交前跑结构检查。真实服务未配置时标为 not_run。

## 执行环境

工作树 `.worktrees/phase2-free-discovery`，分支 `codex/phase2-free-discovery`。使用现有 Python 环境，所有 Python 子进程显式 `PYTHONPATH` 指向当前工作树，避免 editable install 导入 main。测试用数据不接真实客户；不打印 DSN、secret 值或原始敏感网页。迁移用 `scripts/run_alembic.py`，以兼容 T7 AppleDouble 元数据。已有依赖可复用，不修改主工作树文件。

### Task 1: 搜索连接器公共契约与 Tavily 固定免费能力

**Files:** Create `connectors/search_contracts.py`、`connectors/tavily/{AGENTS.md,client.py,transport.py,manifest.py,__init__.py}`、`tests/unit/test_tavily_search.py`；按需兼容调整 `connectors/web_search/client.py`；更新 `connectors/AGENTS.md`；新增 `docs/adr/0016-free-search-provider-contract.md`。

**Interfaces:** 提供供应商无关 `SearchResult`（title/url/description）、`SearchCapabilities`、`SearchUsage`（计划/限额/使用/按量状态明确且可未知）、`SearchProvider` 和 `SearchUsageReader` Protocol。保留 `WebSearchResult` 旧导入/构造兼容。`TavilySearchConnector` 惰性从 secret resolver 取密钥，提供 `search(query, *, country, limit)` 和 `usage()`；固定主机 transport 仅接受强类型参数。Task 2 用这些契约并负责允许搜索与持久额度。

- [ ] RED：先新增行为测试，证明固定 basic/no extras、normalized result 不含 score、usage 严格解码（bool 不能伪装整数、缺失不能当零）、免费/付费/未知判别、timeout/429/auth 分类且 repr/error 无 secret。示例期望：
  ```python
  assert request_body['search_depth'] == 'basic'
  assert request_body['auto_parameters'] is False
  assert request_body['include_answer'] is False
  assert request_body['include_raw_content'] is False
  assert usage.paygo_enabled is False  # 仅实际返回关闭证据时成立
  ```
- [ ] 核实官方 search/usage wire schema（只访问公开文档，不调用真实 API）。不假定 Bootstrap 是免费计划、不猜免费 plan aliases；未知返回显式未知，Task 2 fail closed。API 无可靠周期字段则不虚构周期。
- [ ] GREEN：实现 connector/transport，限制响应大小/超时/固定 origin，不接受模型传 URL 或 credential；国家 boost 与企业证据分离，可省略不支持的国家偏好而保留原查询 country。使用现有安全 URL 验证契约，搜索摘要 repr=False，不保留 provider score 为置信度。
- [ ] 保持 Brave 行为测试，运行新 tests 与 `tests/unit/test_web_search_discovery.py`，结构检查与相关 lint/type；写 ADR/模块规则、报告并提交。

### Task 2: 持久化免费账户额度与 Tool Gateway 接入

**Files:** Create `infra/db/search_quota.py`、`tool_gateway/handlers/free_search.py`、`tests/unit/test_free_search_quota.py`、`tests/integration/test_search_quota.py`；扩展 `infra/db/tables.py` 与实际 Alembic versions 目录中的下一顺序迁移、`apps/scheduler_worker/web_discovery.py`、相关 runtime/config；必要时 `tool_gateway/handlers/web_search.py` 仅供应商注入边界。

**Interfaces:** 消费 Task 1 SearchProvider/SearchUsageReader；提供绑定租户及供应商账户的持久 reservation（reserved/consumed/uncertain），相同账户跨 Run 串行预留。Gateway reader 实现现有 `.search(tenant_id, query, country, limit)` 并复用既有安全结果槽。quota 状态仅返回安全元数据（不带 secret 或原 payload）。真实免费提供方必须显式选用 Tavily，不回退 Brave。

- [ ] RED：真实数据库仓储测试并发 2 Run 抢最后额度只有 1 次 dispatch、复用同账户不同绑定不能绕过、租户隔离、usage 失败/付费开启/免费未知均零 search；成功消费、timeout/429 保留不确定预留、进程重建不释放、无自动 retry。
  ```python
  assert search_transport.calls == 1
  assert await quota.remaining(tenant_id, account_id) == 0
  assert recovered_reservation.status == 'uncertain'
  ```
  上述为行为示例，仓储方法按实现公共契约写测试，不测试私有计数器。
- [ ] GREEN：用事务、唯一约束/行锁实现 account-bound 原子预留，usage 快照与本地未结清预留保守结合，不能把旧快照当新增额度；不以本机月份变化自动清除不确定预留。不能从 API 未返回的信息推断 paygo disabled。
- [ ] 配置存储引用而非 secret，避免不同 Run 直接共享额度计数；没有确认预算或有效免费账户即 fail closed。生产组合经过原 Gateway 管线、既有每 Run budget，不能绕过许可/来源/租户检查；不要注册未实现商业工具。
- [ ] 对迁移执行 upgrade/downgrade/upgrade，跑 quota/gateway/web_discovery focused regressions + boundaries + lint/type，写报告提交。

### Task 3: 研究计划、三线路与版本化执行闭环

**Files:** Modify `domains/directives/{models.py,schemas.py,service_impl.py}`、`apps/scheduler_worker/directive_reader.py`、`workflows/demand_discovery/{ports.py,flow.py,steps.py}`、`agent_runtime/demand_intelligence/agent.py`（以实际入口为准）、`domains/demand/{models.py,schemas.py,service_impl.py}`、`infra/db/repositories/{directives.py,demand.py,need_hypotheses.py}`、表与下一迁移；新增 `workflows/demand_discovery/research.py` 隔离研究专属逻辑；新增 `docs/adr/0017-research-discovery-workflow-version.md`；新 `tests/unit/workflows/test_research_discovery.py` 与相关 integration tests。

**Interfaces:** `execution_mode` 为 research_only 或 outreach_preparation；历史缺省保留 outreach_preparation。新查询 discovery_lane 显式 importer/distributor/ecommerce；旧解码可空。新 workflow version=2，同时保留 v1 定义与 handler 解释。来源归属附可信查询/线路元数据，模型不能伪造；公开证据只生成 Signal/Hypothesis/待核验企业。

**已核实的接线补充：** `agent_runtime/trade_manager/agent.py` 的严格 payload 与 prompt 当前要求 Campaign，必须同时更新并保留旧提案解码；补对应 agent tests/evals。`apps/scheduler_worker/runtime.py` 当前要求 demand_discovery 依赖 account_discovery，研究组合必须能在无联系人组合时启动且仍不可排队触达；API composition 同时注册 v1/v2。引擎 start 选最新版本，v2 handler 必须按确认计划模式保持历史提案的 outreach 行为，不能因最新版本默认为研究。无需为此修改引擎核心。

**企业身份风险：** 现有需求情报能力从页面 host 派生企业身份，不能照搬到行业目录/品牌经销商目录（目录站不是其列出的公司）。新研究路径须区分来源类型与企业身份，核实不到官网/国家时保存带不可变证据的待核验信号，不强行创建 ProspectAccount 或 NeedHypothesis。官网证据充分的三线路候选才沿原 resolve_account/create_hypothesis；保留旧模式行为。不要为处理待核验结果另建一套 Lead 中心；若需要新的来源/核验术语，同步 GLOSSARY。新增测试目录 host 不成为买家官网、目录跨线路命中保留证据。

**所在地证据测试：** 当前 `_hypothesis` 将 ISO2 以忽略大小写正则匹配摘录，`Contact us` 会被错当 US 证据。研究路径必须拒绝这种匹配，也不能拿网站后缀、配送目的地或目录搜索筛选当所在地；确实无法核实的国家保留待核验，不因目标 country 猜测补全。新增对应反例和有明确所在地陈述的正例。

- [ ] RED：新增确认前不生效、研究无需 Campaign、旧缺省依然要求原 Campaign/role/assessment、三线路国家品类预算/排除项、跨线路同域名复用但证据均保留、缺官网/国家不创建合格候选测试。当前模型 prompt 的修改必须重跑 evals。
- [ ] RED：参数化 research_only 流程，spy 所有 outbound/contact/quote 边界。
  ```python
  assert contact_calls == verification_calls == send_calls == quote_calls == 0
  assert result.signal_count > 0 and result.hypothesis_count > 0
  assert result.validated_need_count == result.qualified_opportunity_count == 0
  ```
- [ ] GREEN：只在确认后生成三线路查询策略（每条带 lane）；预算来自老板明确配置，不默认遍历全球。更新准入/序列化/reader，按版本选择 pipeline，research score 止于假设，不创建 account_discovery queue。保留旧 flow/replay。
- [ ] 原网页证据携带来源快照/URL/time/hash；目录描述和企业自述均为有归属的观察，推断独立，不造运输记录/采购/OEM。缺少字段证据保留待核验记录，禁止把 query.country 当 account.country。结构化结果区分无结果/免费耗尽/unsupported/page_disallowed/usage_unknown/uncertain。
- [ ] 增强安全页面读取器，阻止登录墙/验证码/禁止访问并保持 DNS/peerIP/每次 redirect 校验；从许可策略拒绝未获授权来源，不绕过反爬或登录。新增页面测试与 prompt injection evals。
- [ ] 运行新 workflow/domain/API integration tests、旧 demand_discovery/account_discovery/gateway 安全回归与 evals、迁移及结构检查；同步模块 AGENTS、ADR、报告提交。

### Task 4: 应用契约、确认界面与结果投影

**Files:** Modify `apps/api/routers/{command_center.py,demand_radar.py,customer_discovery.py,runs.py,settings.py}`、相关 composition/runtime、`apps/web/src/views/{command-center/CommandCenter.vue,demand-radar/DemandRadar.vue,customer-discovery/CustomerDiscovery.vue,runs/RunCenter.vue,settings/SettingsCenter.vue}`、生成 `apps/web/src/api/api.d.ts`；补 API 合同与对应 Vue tests。

**Interfaces:** 消费前序 execution_mode/discovery_lane/来源归属/quota/structured stop metadata；API schema 为唯一类型源。查询均按 tenant 过滤，不能向另一租户泄漏绑定账户或其用量。

**投影接线：** Run 公开 DTO 实际在 `workflows/engine/audit.py`，读取实现为 `infra/db/run_audit.py`；只增加白名单研究摘要，不返回完整 run.context/step.data。客户列表 DTO 在 `domains/prospecting/schemas.py`，按需要经应用层或基础设施读模型聚合信号来源，禁止域间 import。指挥中心旧 UI 使用 `max_pages_per_query`，后端实际是 `max_pages_read`，本次预算展示必须与真实契约一致。

**首次配置状态：** quota 账户记录在实际执行时才创建，不能因首次没有 snapshot 而永久禁止首次研究。展示必须区分未配置、已配置但账户状态尚未核实、实际用量读取失败/付费开启/额度不足；受信配置存在不等于真实账户已验证或生产 scheduler 已激活。UI 的未配置禁用必须有后端依据，不能仅靠前端判断，真实 `/usage` 和预留门禁仍在每次 Gateway 执行前生效。

**证据组件注意：** 现有 `apps/web/src/components/ProvenancePopover.vue` 标题旁固定写着「已验证事实」，不能直接用这个标签呈现新研究的企业自述或模型假设。沿用组件时须显式区分证据类别，或在研究界面使用适配的证据展示，保留既有已验证需求页面的语义。

- [ ] RED：API 确认提案中必须返回模式/三线路/市场/预算；用户确认前不能执行，历史提案可读。需求雷达/客户发现/Run Center 返回证据、核验状态、额度消耗和可区分停止原因；验证跨租户读取拒绝。
- [ ] RED：Vue 测试确认文案、缺预算/账户的禁用状态、research_only 无 Campaign 必填项、进口商候选与运输记录区别、quota exhausted 不渲染为没有买家。
  ```typescript
  expect(wrapper.text()).toContain('只研究')
  expect(wrapper.text()).toContain('进口商候选')
  expect(wrapper.text()).not.toContain('已验证采购需求')
  ```
- [ ] GREEN：沿现有组件设计接入，不重做界面；公开页面证据与模型推断有标签/跳转，Run 显示已用/未决预留和停止原因。来源未知使用待核验，未实现商业供应商不显示可启用。
- [ ] 执行 OpenAPI 生成，再次生成无差异；后端 API tests、前端 tests/typecheck/build/lint，浏览器测试手动查看确认→运行→结果界面，报告提交。

### Task 5: 全链路验收、部署说明与 Phase 2 文档

**Files:** Modify `ROADMAP.md`、`HANDBOOK.md`、涉及模块 AGENTS、`infra/.env.example`；新增 `docs/acceptance/2026-08-27-phase2-free-discovery.md`、受控端到端 tests 与显式 opt-in 的 research acceptance 脚本（在 scripts 下）。

**Interfaces:** 基于前序真实应用入口运行，不把 mock 或 web 搜索工具结果说成生产实现。免费来源密钥只接受配置引用；真实联网需要确认研究预算且不得触达真实联系人。

- [ ] RED/GREEN：受控端到端覆盖三线路发现→Signal→Hypothesis、后续受控联系人验证→发送→回复识别→Validated Need→接管；明确分开研究模式和受控旧触达模式。三类公开页面 opt-in 验收走安全 Gateway，不以 fixture 冒充 live。
- [ ] 未配置 Tavily 账户/确认预算时真实 Tavily 搜索报告 not_run，测试受控 usage/search 和真实页面读取分栏；不主动索取 secret，也不调用收费联系人/邮件。报告模型/基础设施成本与新增搜索免费限制的区别。
- [ ] ROADMAP 修订 Phase 2 范围、保留后续寻源/成本/报价；HANDBOOK 增加本批配置/运行/停止/验收步骤、旧版本兼容说明。保留 Phase 1 原真实验收状态。
- [ ] 运行 `ruff check .`、相关 mypy、`scripts/check_boundaries.py`、`scripts/scan_sensitive.py`、全量 backend（non-e2e + e2e 分开）、前端 test/typecheck/build/lint、OpenAPI 生成无漂移、迁移 U/D/U、浏览器验收；记录真实命令/结果，外部依赖不可用如实报告。
- [ ] 独立全分支审查与修复后提交验收结论；满足集成条件才进入主分支合并步骤，不推送、不部署、不发信。
