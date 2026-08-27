# Phase 2 Task 3 实现报告

状态：实现完成，交根代理独立审查。日期：2026-08-27。

工作树：/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery
分支：codex/phase2-free-discovery。任务指定BASE：906491d71586d24a0dcc0166731ddcd4c00a0a30；本次提交前HEAD：b0eb619（包含前序接口与根代理文档提交）。
没有改主checkout、没有push/deploy/send、没有读取生产.env或真实凭证、没有真实Provider/模型调用。
按要求使用TDD、verification-before-completion和实现者约定；没有启动子代理。根/就近AGENTS及HANDBOOK已读，同步了触及模块规则。

## 实现与接口

- 确认前提案不生效；确认后reader才返回计划。研究计划无需Campaign/role/assessment，仍校验国家、品类、排除、全部预算。研究查询必须显式覆盖importer/distributor/ecommerce三线路。
- 生产TradeManager生成的新查询，两种模式均由严格schema要求非空lane；不只靠prompt。历史持久JSON缺mode/lane解码仍是outreach_preparation/None，内部旧DTO和旧集成调用保留兼容。历史默认仍要求Campaign/role/assessment。新增直接历史JSON解码测试，不只依赖新fixture缺省。
- demand_discovery同时注册v1/v2。v1定义和handler ref保留；v2 ref独立，每步读取已确认计划决定模式，所以新引擎选最高版本不改变历史提案语义。没有改引擎核心。
- research.py隔离研究编排和终止评分；研究最终handler没有联系人、验证、发送、报价端口，旧排队handler也显式拒绝研究。高置信档位研究仍零排队；旧提案v2仍按原门槛排队。scheduler真实组合测试证明无需account_discovery组合。
- 研究入口只允许受信组合显式Tavily（前序WebComposition已要求exclusive_account_confirmed及Tavily transport），默认Brave返回unsupported、零搜索，不fallback。没有用“首次无quota snapshot”推断付费或拒绝首次运行。没有重做已审查quota/HMAC。
- ResearchEvidence由确认query+原页面计算，模型不见也不能提供lane、来源类型、身份状态。workflow在应用变更前重新绑定URL/hash/artifact/元数据及原文摘录。原始正文不进入Run context。
- directory/不明来源只保留Signal，pending_verification不能resolve_account/create_hypothesis；域服务也阻止pending信号创建Hypothesis。明确本企业自述和所在地才调用原resolve_account/create_hypothesis，跨线路同域复用企业/假设并保留全部signal refs。
- 研究公开RFQ/inbound/tender不冒充客户回复；confidence继续由现有确定性证据规则推导，重复同页不是独立加分来源。没有修改原need_hypotheses仓储，因为已有合并/去重逻辑满足需求。
- Tavily前缀合成误贴防护：CredentialMarkerGuard覆盖body/subject；TradeManager入口model_calls=0；DemandIntelligence先检原文再脱敏，避免长数字被当电话遮盖后漏过输入阻断；错误不回显合成值。

### Task 4 可用字段

domains/demand/schemas.py：ResearchEvidence（frozen Pydantic、strict、extra forbid），DemandSignalView.research_evidence可空（历史null）。
字段：
proposal_id, query, discovery_lane, query_country, query_category, source_kind, identity_status,
company_name?, website_domain?, country?, identity_quote?, country_quote?, source_url。

source_kind：company_self_description / directory_listing / unverified_public_page。
identity_status：self_described / pending_verification。self_described不是工商真实性已核实。

计划：domains/directives/{models,schemas}.py与workflows/demand_discovery/ports.py的execution_mode，query.discovery_lane。
Run研究context：
execution_mode, completion_reason, searches_used, pages_used, signal_count, hypothesis_count,
pending_verification_count, validated_need_count=0, qualified_opportunity_count=0, queued_count=0,
discovery_lanes, planned_discovery_lanes, signal_ids, hypothesis_ids；完成评分另有confidence_tiers、空queue/run id列表。
fix round1后planned_discovery_lanes是计划三线，discovery_lanes只列实际持久化信号
线路，不可用计划线路当完成情况；历史Run可能无planned字段，不回填旧Run。

searches_used/pages_used沿原预算**尝试计数**语义（含拒绝），不是计费credits；免费实际用量、是否不确定必须读取前序quota snapshot/run_state。已告知根代理，不应UI标成已消耗额度。

completion_reason区分plan_completed/budget_exhausted/no_results/page_disallowed/no_readable_pages/
pending_verification/no_supported_signals，以及FreeSearchError原reason：
quota_exhausted/usage_unknown/paid_enabled/request_uncertain/unsupported等。已有页面再额度停止仍保留先前证据。

## 页面安全

- 沿用已激活国家政策、Playbook、权限和Gateway批准搜索结果来源，不建立逐host静态白名单或新法律默认。
- 仅访问搜索结果指定页；redirect只能原scheme/netloc；robots和正文每跳保持URL/DNS/实际peer IP验证。可信部署可deny hosts。
- robots经同一safe transport、有限跳数/超时/响应大小；解析最多512KiB；404/410只表示未提供robots。失败、不明状态、畸形、未支持crawl-delay/request-rate均关闭。支持本agent/通配组、最长路径、Allow同长优先、*与$，拒绝空/非法User-agent覆盖禁令。
- 登录/验证码墙、403/451/明确禁止自动抓取在快照保存前拒绝；普通登录导航或公开联系表单CAPTCHA不构成墙。Account Portal只有用户名/密码表单的反例也拒绝；有公开正文的嵌入登录表单保留。
- robots允许不是访问授权，不替代法律与站点许可策略；不是完整自动条款审查或完备挑战识别。没有登录/Cookie/验证码绕过。

## 持久化与迁移

新增0040接0039：demand_signals新增research_evidence JSONB(nullable)与discovery_key(非空默认空串)，来源唯一键加discovery_key；trigger阻止修改两列。旧行保持null/空键，不改旧身份/所在地或旧提案。
discovery_key由proposal/query/lane/query-country/query-category/source-URL确定性生成；相同提案相同来源输出重放Signal不重复，跨线路保留。真实Postgres验证跨线路三Signal、一个Account/一个Hypothesis、引用3Signal、重放稳定、租户隔离、pending服务拒绝及元数据不可变。
回滚0039如果已有跨线路同来源数据，恢复旧唯一键会失败关闭；不自动删证据。需人工导出并制定迁移方案。空/兼容数据的全迁移roundtrip与ORM契约通过。
新head预期同步test_migrations、test_search_quota、test_work_intake_migration_head、test_alembic_appledouble，保留T7 AppleDouble兼容。

## RED记录（输出为相关断言摘要，未保存连接信息）

下表命令均在本工作树运行，Python前缀是：
`PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3`。
integration命令另外前置`env -u TEST_DATABASE_URL`，使用既有testcontainers。

|阶段|命令尾部|RED输出|
|---|---|---|
|模式/线路/v2契约|`-m pytest tests/unit/workflows/test_research_discovery.py -q`|7 failed, 2 passed；缺discovery_lane/version参数|
|研究提案|`-m pytest tests/unit/agent_runtime/test_research_proposal.py -q`|1 failed, 1 passed；严格payload拒绝research字段/无Campaign|
|来源身份|`-m pytest tests/unit/workflows/test_research_discovery.py -q`|6 failed, 9 passed；ResearchEvidence未实现|
|研究Agent|`-m pytest tests/unit/agent_runtime/test_demand_intelligence_agent.py -q`|新增3例失败；研究输入被拒绝导致空changes|
|持久来源|`-m pytest tests/integration/test_research_discovery.py -q`|1 failed；SignalCaptureRequest不接受research_evidence；随后pending服务检查DID NOT RAISE|
|v2执行|`-m pytest tests/unit/workflows/test_research_discovery.py -q`|2 failed；新handler key缺失|
|页面墙/robots|`-m pytest tests/unit/test_web_search_discovery.py -q`|4 failed, 10 passed；墙/robots未拒绝|
|公开RFQ等级|`-m pytest tests/unit/workflows/test_research_discovery.py -q`|预期public_company_event，实际customer_interest_reply|
|新outreach缺lane|`-m pytest tests/unit/agent_runtime/test_research_proposal.py -q --tb=short`|2 failed, 4 passed；缺失/null lane都DID NOT RAISE|
|普通标题登录墙|`-m pytest tests/unit/test_web_search_discovery.py tests/unit/agent_runtime/test_research_proposal.py -q --tb=short`|1 failed, 31 passed；Account Portal纯密码表单未拒绝|
|畸形robots|`-m pytest tests/unit/test_web_search_discovery.py tests/unit/workflows/test_research_discovery.py -q --tb=short`|1 failed, 53 passed；空User-agent错误覆盖通配Disallow|
|Tavily误贴|`-m pytest tests/unit/agent_runtime/test_research_proposal.py -q --tb=short`|3 failed, 6 passed；TradeManager/body/subject未拒绝|
|原网页预检|研究unit+integration+guard+eval组合（后述最终收尾组的前版）|1 failed, 127 passed；长数字先脱敏使model_calls非0，现改为原文先check|

开发中另遇并修正：迁移多SQL单prepared statement、JSONB命名、测试poll_due参数/无get_run方法、空证据derive_confidence调用等，均属实现/测试接线错误，不掩饰为环境问题。

## 相关fullsuite首轮与失败修复

实际运行一次的相关fullsuite命令：

```bash
env -u TEST_DATABASE_URL PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 -m pytest tests/unit/workflows/test_research_discovery.py tests/unit/workflows/test_demand_discovery.py tests/unit/workflows/test_account_discovery.py tests/unit/agent_runtime/test_demand_intelligence_agent.py tests/unit/agent_runtime/test_account_discovery_agent.py tests/unit/agent_runtime/test_research_proposal.py tests/unit/test_demand_signal_contracts.py tests/unit/test_demand_completeness.py tests/unit/test_web_search_discovery.py tests/unit/test_country_policy_web_gateway.py tests/unit/test_free_search_quota.py tests/unit/test_tavily_search.py tests/integration/test_research_discovery.py tests/integration/test_demand_signals.py tests/integration/test_need_hypotheses.py tests/integration/test_prospecting_repositories.py tests/integration/test_scheduler_worker.py tests/integration/test_api_runtime.py tests/integration/test_phase1_closed_loop.py tests/integration/test_search_quota.py tests/integration/test_tool_gateway_contact_enrichment.py tests/integration/test_tool_gateway_contact_verification.py tests/integration/test_tool_gateway_pipeline.py tests/integration/test_migrations.py tests/evals -q
```

输出：`23 failed, 353 passed in 52.41s`。不能称为全量一次通过。
1个失败是旧Protocol精确签名期望未包含新discovery_key；已更新并断言默认空串。
其余22个是研究跨线路记录留在session共享测试库，后续downgrade正确拒绝恢复旧唯一键。
新研究测试改为test-local外层事务+create_savepoint，测试完成rollback，不动生产downgrade保护、不删除真实证据。

首次定向复验：`2 failed, 99 passed`。一个是外层事务最初未设create_savepoint被UOW rollback撤销；另一个是search_quota测试head仍写0039。均修复。
第二次修复范围（research proposal/page/contracts + research/API/quota/migrations integration + evals）：
`146 passed in 64.18s`。这是分组结果，不与首轮相加伪造单次全量通过。

命令：
```bash
env -u TEST_DATABASE_URL PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 -m pytest tests/unit/agent_runtime/test_research_proposal.py tests/unit/test_web_search_discovery.py tests/unit/test_demand_signal_contracts.py tests/integration/test_research_discovery.py tests/integration/test_api_runtime.py tests/integration/test_search_quota.py tests/integration/test_migrations.py tests/evals -q --tb=short
```

## 最终GREEN与静态检查

最终追加guard/迁移头/历史codec等改动后：

```bash
PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 -m pytest tests/unit/agent_runtime tests/unit/test_guardrail_checker.py tests/unit/test_work_intake_migration_head.py tests/unit/test_alembic_appledouble.py tests/unit/workflows/test_research_discovery.py tests/unit/test_web_search_discovery.py tests/evals -q --tb=short
```

输出：`145 passed in 2.57s`。包含旧Agent、两个迁移head文件、新研究eval；没有改旧eval样本。每次prompt修改均重跑受控evals。

```bash
env -u TEST_DATABASE_URL PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 -m pytest tests/integration/test_research_discovery.py -q --tb=short
```

输出：`2 passed in 5.89s`（真实隔离Postgres，最终新增不可变trigger断言已包含）。

```bash
PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 -m mypy agent_runtime/guardrails/input_guard.py agent_runtime/demand_intelligence agent_runtime/trade_manager apps/api/composition/runtime.py apps/scheduler_worker connectors/web_search domains/demand domains/directives infra/db/repositories/demand.py infra/db/repositories/directives.py infra/db/tables.py workflows/demand_discovery
```

输出：`Success: no issues found in 58 source files`。
ruff对本次全部变更Python文件check通过；最终部分新文件format；git diff --check通过。
结构命令：`PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 scripts/check_boundaries.py`；分层、金额float、置信度、事件、租户、AGENTS覆盖、域结构全绿。
全库最后回归留Task5，本文不声称已跑全库。

## 自审与已知限制

- 独立审查由根代理进行，本报告不是独立review通过声明。
- 原页身份目前仅英文句首“We are <ASCII公司名>, a/an/the ... .”，以及同主体“We are / <公司名> is headquartered/based/located in <国家>”。复审后明确所在地原文接受完整已分配ISO2代码；英文全名别名暂限US/DE/GB/CA/AU/NZ/FR/ES/IT/NL，不是全球多语言自动核验。原先误将代码范围限制为10国是实现缺陷，不是已批准的市场范围，详见后续修正记录。
- 原URL host + 本企业经营自述 + 所在地原文只是有归属的网页自述，不保证网页主体诚实，不证明工商真实性/买家需求。目录标志/目录路径保守pending；无标志、伪装自述仍可能误判，需UI标清证据等级和人工复核。
- 不拿Contact us、TLD、配送国家、分支地址、搜索筛选或query.country补企业国家。未知身份/地点为pending信号；模型未提供有效摘录时no_supported_signals，不能称no_results。
- 登录墙判定是有限heuristic：密码表单外公开文字不足12词且不足80字符，或明确拦截标题/禁止文字；有误拒和未识别挑战的可能，不解验证码/不登录。
- robots实现是有界策略子集；未知格式/额外速率约束保守拒绝，严格same-origin也会漏读正常跨来源跳转。成本是漏收与多一次robots请求；不声称法规/条款自动许可。
- 控制搜索、页面网络返回与模型输出测试只证明边界和确定性契约，不代表真实Tavily连通性、真实模型准确率或真实公司核验率。真实部分是数据库、迁移、域服务、workflow engine、scheduler/API组合。
- 研究不产生ValidatedNeed、合格TradeOpportunity或外发授权；统计为零符合阶段边界，不把企业数量当北极星指标。
- 相同确认提案/相同来源的确定性输出重放稳定；真实搜索重放还受前序持久HMAC配额保护。未修改额度或偷偷重试不确定调用。
- ADR0017记载页面策略、mode兼容、识别边界与有数据downgrade风险；模块AGENTS和GLOSSARY已同步。

## 文件清单

- GLOSSARY.md
- agent_runtime/demand_intelligence/AGENTS.md
- agent_runtime/demand_intelligence/agent.py
- agent_runtime/guardrails/input_guard.py
- agent_runtime/trade_manager/AGENTS.md
- agent_runtime/trade_manager/agent.py
- apps/api/composition/runtime.py
- apps/scheduler_worker/AGENTS.md
- apps/scheduler_worker/directive_reader.py
- apps/scheduler_worker/runtime.py
- connectors/web_search/AGENTS.md
- connectors/web_search/client.py
- connectors/web_search/transport.py
- domains/demand/AGENTS.md
- domains/demand/models.py
- domains/demand/repository.py
- domains/demand/schemas.py
- domains/demand/service_impl.py
- domains/directives/AGENTS.md
- domains/directives/models.py
- domains/directives/schemas.py
- domains/directives/service_impl.py
- infra/db/repositories/demand.py
- infra/db/repositories/directives.py
- infra/db/tables.py
- tests/integration/test_api_runtime.py
- tests/integration/test_migrations.py
- tests/integration/test_scheduler_worker.py
- tests/integration/test_search_quota.py
- tests/unit/agent_runtime/test_demand_intelligence_agent.py
- tests/unit/test_alembic_appledouble.py
- tests/unit/test_demand_signal_contracts.py
- tests/unit/test_web_search_discovery.py
- tests/unit/test_work_intake_migration_head.py
- workflows/demand_discovery/AGENTS.md
- workflows/demand_discovery/flow.py
- workflows/demand_discovery/ports.py
- workflows/demand_discovery/steps.py
- connectors/web_search/page_policy.py
- docs/adr/0017-research-discovery-workflow-version.md
- migrations/versions/0040_research_evidence.py
- tests/evals/test_research_evals.py
- tests/integration/test_research_discovery.py
- tests/unit/agent_runtime/test_research_proposal.py
- tests/unit/workflows/test_research_discovery.py
- workflows/demand_discovery/research.py
- .superpowers/sdd/2026-08-27-phase2-free-discovery/task-3-report.md（本报告，显式force加入提交）

## 收尾复审修正：完整ISO2代码识别

基于78835867d833ef0d670496fbfb55ce084787c869继续修正，未触碰Task4。
根代理指出：有限英文国名表不应限制有效ISO2代码，之前10国代码限制未经批准。
库内检索无完整country集合，也未声明pycountry依赖；最小变更是在demand/schemas.py
加入私有完整已分配ISO 3166-1 alpha-2常量（249个国家/地区代码），不新增依赖、
运行时网络或框架，不修改compliance政策键、默认许可或旧记录。

来源核验：2026-08-27查看[ISO官方标准说明](https://www.iso.org/iso-3166-country-codes.html)，
并从[pycountry官方固定数据快照](https://raw.githubusercontent.com/pycountry/pycountry/e974d00d5ead823a48d6944a6df1696e95e507e3/src/pycountry/databases/iso3166-1.json)
只取alpha_2代码事实。固定提交e974d00d5ead823a48d6944a6df1696e95e507e3；代码与该
JSON逐项集合比对输出`ISO2 pinned-source equality verified: 249 codes`。
快照版本语义是当时的已分配代码，不包含任意两字母或保留/非正式代码；后续标准
分配变化需显式维护快照。国码有效只证明代码识别，不证明市场已获合规许可。

保留同主体明确location句式；JP、BR正例通过，ZZ、小写jp/us、Contact us、
shipping to JP均pending，不使用query国家填回。英文国名别名仍有限并单独说明。
真实Postgres验证来自JP与BR的同域名研究身份仍只有一个Account，合并来源引用，
不重写先前JP国家及其Provenance；没有改原resolve_account逻辑。

TDD命令（RED与首轮GREEN相同）：
`env -u TEST_DATABASE_URL PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 -m pytest tests/unit/workflows/test_research_discovery.py tests/integration/test_research_discovery.py -q --tb=short`

RED：`3 failed, 35 passed in 4.51s`，JP/BR国家为None及跨国测试被pending拦截。
GREEN：`38 passed in 5.36s`。

专项扩展验证命令：
`env -u TEST_DATABASE_URL PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 -m pytest tests/unit/workflows/test_research_discovery.py tests/unit/agent_runtime/test_demand_intelligence_agent.py tests/integration/test_research_discovery.py tests/integration/test_prospecting_repositories.py tests/unit/test_country_policy_web_gateway.py tests/evals -q --tb=short`

输出：`91 passed in 6.96s`。真实部分是隔离Postgres及域服务；没有真实搜索/模型调用，
网站访问仅开发时核实公开标准数据。未改prompt，仍跑既有与新增受控eval。
ruff本次三个Python变更文件通过（初次SIM905格式意见已改为字面列表）；
`PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 -m mypy domains/demand/schemas.py`输出`Success: no issues found in 1 source file`；结构自检全绿。
本修正文件为schemas.py、demand/AGENTS.md、ADR0017、研究unit/integration和本报告。

## Task 3 初审修复 round 1

FIX_BASE：2fd78e2d4f39ffb5216fda752135277612125806。三项Important均先RED后GREEN。
没有改Gateway核心、公开DTO、数据库唯一键或迁移；没有Task4改动、真实Provider调用
或新子代理。仅查阅公开RFC说明，不重审前序Tavily/quota。

### Finding 1：robots编码保留字符

connectors/web_search/page_policy.py:13的_normalized_path替换无条件unquote，
只解码unreserved ASCII；其他编码保留并规范十六进制大小写。%2F不成为路径分隔符，
%2A/%24不成为规则通配符/终止符；畸形%GG/不完整百分号失败关闭。字面非ASCII按
UTF-8编码比较。不宣称完整URL解释器或更宽的站点访问权限。
依据：[RFC9309 §2.2.2](https://www.rfc-editor.org/rfc/rfc9309.html#section-2.2.2)。
测试覆盖原复现Disallow /private/ + Allow /private%2Fpublic拒绝/private/public，
以及编码大小写等价、unreserved等价、编码*与$、畸形编码反例。

### Finding 2：模型合并重复原页后的来源保存

workflows/demand_discovery/research.py:54的_source_signals在持久化前验证所有原文
及受信页面引用，然后只对同URL+content_hash展开每份query/lane归属，原内容不同
hash时绝不复制。模型重复输出同一摘录会合并索引，不重复写同来源Signal；合法模型
只返回page0时，三次命中的三份归属仍保留。假设索引重连到实际落库的Signal IDs。

成功读取的页面条目容量受max_signals限制；页面失败/禁止不占该容量，但仍消耗
pages_used尝试预算。容量不足在后续读取前停止并返回budget_exhausted，不读取3份
再静默丢2份。每来源优先分配一条有效Signal，额外不同类型观察只有剩余容量才写；
被省略观察的假设不能引用不存在记录。没有放宽max_pages_read/max_signals。

产品代价：低max_signals可能提前结束原本三线查询；重复页面读取条目也保守占容量。
无有效模型摘录的独立页面不臆造Signal；实际线路只按持久记录统计，不声称三线已完成。
Task4接口补充：planned_discovery_lanes保持计划三线，discovery_lanes为本轮持久化
证据实际覆盖的线路；未配置/无有效Signal为空数组，历史Run可无planned字段。

### Finding 3：保留多行逐字证据

research.py:35的_evidence_text允许原文LF和TAB，其余控制符（包括CR/NUL/VT/DEL）
拒绝，长度/空白边界及逐字子串检查保留。不压平、重拼或改写摘录，不改v1文本规则。
真实HTML解析器两段<p>产生换行的流程测试现在完成，Postgres raw_observation与
解析后的原文严格相等。unit同时覆盖LF/TAB保留与其他控制符拒绝。

### RED与GREEN命令/输出

第一组RED命令：
`env -u TEST_DATABASE_URL PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 -m pytest tests/unit/test_web_search_discovery.py tests/integration/test_research_discovery.py -q --tb=short`

输出：`8 failed, 33 passed in 20.20s`；5个编码反例、合并页只存1份、真实HTML换行
Run failed、预算1却读3页均重现。

第二组RED命令：
`PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 -m pytest tests/unit/workflows/test_research_discovery.py -q --tb=short`

输出：`18 failed, 32 passed in 1.47s`；三线路分别覆盖低预算、合并页、LF/TAB、
额外观察超容量及不同hash不能复用摘录。GREEN后追加失败页不占成功容量与同键
不同摘录拒绝测试，未改旧eval样本或prompt。

最终专项命令（三个finding文件＋相关Agent与受控eval）：
`env -u TEST_DATABASE_URL PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 -m pytest tests/unit/test_web_search_discovery.py tests/unit/workflows/test_research_discovery.py tests/integration/test_research_discovery.py tests/unit/agent_runtime/test_demand_intelligence_agent.py tests/evals -q --tb=short`

输出：`129 passed in 13.09s`。其中真实Postgres研究组先独立验证为`6 passed in 11.19s`；
最终组合包括该组，不将两者相加。数据库/域服务/引擎真实，搜索和模型响应受控，
页面HTML解析器真实；没有真实Provider/模型准确率结论，也未跑无关全库套件。

静态检查：ruff检查本次两个实现文件及三个测试文件全部通过；
`PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 -m mypy workflows/demand_discovery/research.py connectors/web_search/page_policy.py`
输出`Success: no issues found in 2 source files`。初次局部变量Optional赋值类型错误
已通过赋值前非空断言修正。结构自检和git diff --check通过。

### 保持既有唯一键的失败关闭边界与自审

research.py:94：同一来源/hash/signal_type若有不同逐字摘录，在_source_signals
准备阶段抛ValidationError，尚未调用capture_signal，不擅自拼成新原文，也不把
未保存摘录的假设链接到已有Signal。此“冲突”是旧唯一键表示能力的冲突，不是判定
两条原文事实彼此矛盾。对应测试：
tests/unit/workflows/test_research_discovery.py::test_same_source_and_type_with_different_quotes_fails_before_any_persistence。
该边界已向根代理说明，独立复审判断是否接受；本轮不扩schema。

自审核对：同URL不同hash仅保留真实对应来源；三条已读同版本来源在预算3时全部
持久化且重放幂等；预算1只读取可保存的一份并准确列实际线路；额外模型索引没有
实际记录时不生成其假设。记录/预算行为均由确定性代码决定，未交给模型。
规则同步到connectors/web_search/AGENTS.md、workflows/demand_discovery/AGENTS.md
及ADR0017。此轮变更还包括两个实现文件、三个测试文件和本报告，共9文件。

## Task 3 复审修复 round 2

FIX_BASE：71fef6f2eaa8c50a3c288eba44825a5dc2dca8f9。
本轮只修复round1新增的编码星号回归，不修改已通过的研究来源归属、多行摘录、
DTO、迁移或Gateway核心，也没有Task4改动、子代理或真实Provider调用。

### Finding：编码规则与URI字面特殊字符

核对[RFC9309 §2.2.3 Figure6](https://www.rfc-editor.org/rfc/rfc9309.html#section-2.2.3)，
规则中的%2A须匹配URI中的字面星号；%24同理匹配字面美元符号。
page_policy.py的_normalized_path增加显式is_rule上下文：规则的裸星号保留通配语义、
末尾裸美元符号保留终止语义，URI里的两种字面字符规范为%2A/%24。规则内部非末尾
美元符号仍按字面匹配；编码规则不会被解成操作符。仅这两种特殊字符按上下文处理，
%2F仍与路径斜杠严格区分，未恢复无条件unquote，也没有扩大robots访问授权。

新增15个参数用例覆盖Figure6两个示例、编码大小写、编码星号不能匹配任意名字、
编码美元符号不能当终止符、原裸星号通配、原末尾美元符号、内部字面美元符号，
以及编码美元符号后再接终止锚点。原%2F隔离与畸形编码失败关闭用例保持通过。

### RED与GREEN

先新增测试、未改生产实现，运行：

`PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 -m pytest tests/unit/test_web_search_discovery.py -q --tb=short`

RED输出：`3 failed, 47 passed in 0.70s`，三个失败均为`AssertionError: assert True is False`：
规则/path/file-with-a-%2A.html对URI/path/file-with-a-*.html，规则/path/foo-%24
对URI/path/foo-$，以及规则/path/foo-%24$对URI/path/foo-$。

最小实现后运行同一命令，GREEN输出：`50 passed in 0.61s`。这些是受控页面传输和
真实页面策略函数单元测试，没有真实网站/搜索调用；未重跑round1的129项业务组合。

### 提交前检查与自审

`PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 -m ruff check connectors/web_search/page_policy.py tests/unit/test_web_search_discovery.py`

输出：`All checks passed!`

`PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 -m mypy connectors/web_search/page_policy.py`

输出：`Success: no issues found in 1 source file`。

`PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery /Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3 scripts/check_boundaries.py`

输出：分层与依赖方向、金额float、置信度数值、事件注册、租户过滤、AGENTS.md覆盖、
域结构完整七项全绿，`结构自检通过。`；`git diff --check`无输出、exit0。

本轮仅page_policy.py、test_web_search_discovery.py与本报告三个文件改变。
自审无新增阻塞疑虑；仍不宣称完整自动站点条款审查，robots允许不等于法律许可。
按TDD先验证反例、verification-before-completion读取本轮检查结果后才提交。
