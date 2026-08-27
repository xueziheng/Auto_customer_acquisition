# Phase 2 首批免费来源获客验收

实际验收日期：2026-08-28（Asia/Shanghai）。文件名沿用2026-08-27计划日期。
工作分支：`codex/phase2-free-discovery`；Task5起点：`cdcd6522985213f00b13fa413944bb955f499849`。

## 结论与边界

工程实现和受控验收完成；命令结果见下表。Task1–5已完成各自独立审查及复审；
**最终全分支审查和最新树全量复验尚待root完成，不能将本报告视为合并批准。**
没有推送、部署、真实发信或商业数据API调用；main未改动。

本批是Phase2获客增强，不是整个Phase2完成。后续寻源、成本、报价保留。
Phase1真实Campaign、客户原话/Provenance、健康发件信誉与接管SLA运营验收仍为 `not_run`。

| 层次 | 实际执行与结论 |
|---|---|
| 免费搜索与安全页面单元/集成 | 受控usage/search/page transport，真实代码/Gateway/持久quota通过；不等于联网 |
| 三线路业务闭环 | 真实隔离Postgres + 受控搜索/页面/模型：确认→Signal→Hypothesis，跨线路同企业消歧、重放幂等、来源不可变；研究链零联系人/验证/发送/报价 |
| 旧触达后半链 | 独立受控链：联系人补全→邮箱验证→发送→回复识别→Validated Need→接管；不是研究模式自动晋升或真实发信 |
| 实际装配补验 | 新增真实多连接engine→Gateway→受控Tavily/原页transport→真实Artifact metadata；不使用同连接savepoint隐藏锁等待 |
| 真实Tavily账户/usage/search | `not_run`：本任务未取得用户自行配置、独占账户、确认研究预算与真实政策齐备的执行证据；不索取secret、不以fixture冒充live |
| 三类公开页面真实读取 | `not_run`：需显式opt-in且经过相同真实Gateway；没有用web工具抓到页面替代产品实现 |
| 真实模型/完整研究scheduler激活 | `not_run`；API配置声明不等于worker启用 |
| 浏览器人工桌面 | root以In-app Browser对真实路由+合成内存fixture作手动QA；详情见浏览器小节 |
| 浏览器自动研究窄屏 | 正式Playwright跨源HTTP，独立context，1280×900与实际390×844；交互通过，业务fixture明确受控 |

## 最终验证命令与结果

所有Python进程显式 `PYTHONPATH=/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-free-discovery`，
解释器为 `/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python3`。
Node为 `/Users/xueziheng/.nvm/versions/node/v24.15.0/bin/node`。
后端命令均先 `env -u TEST_DATABASE_URL`，测试自建Docker隔离Postgres，不读生产数据。
以下省略重复前缀，不省略被测scope；前端命令工作目录为 `apps/web`。

| 命令 | 结果 |
|---|---|
| `python3 -m ruff check .` | All checks passed |
| `python3 -m mypy --explicit-package-bases connectors/tavily connectors/search_contracts.py connectors/web_search tool_gateway/free_search_contracts.py tool_gateway/handlers/free_search.py tool_gateway/handlers/web_search.py infra/db/search_quota.py apps/scheduler_worker/web_discovery.py apps/scheduler_worker/free_web_discovery.py apps/scheduler_worker/research_acceptance.py apps/scheduler_worker/research_acceptance_dependencies.py scripts/accept_research_discovery.py workflows/demand_discovery apps/api/research.py apps/api/research_schemas.py` | 26 source files，成功 |
| `python3 scripts/check_boundaries.py` | 七项通过 |
| `python3 scripts/scan_sensitive.py` | exit0，无输出 |
| `python3 -m pytest -m 'not e2e' -q` | **4216 passed / 8 deselected，462.65s**；全后端只跑一轮 |
| `TRADEOS_REQUIRE_E2E=1 python3 -m pytest -m e2e -q` | 首轮1 failed / 7 passed / 4216 deselected，115.85s；原跨源六项+新增研究两项，失败根因与范围重跑见下文 |
| `TRADEOS_REQUIRE_E2E=1 python3 -m pytest tests/e2e/test_phase1_browser.py::test_phase1_browser_visible_reply_to_handoff_chain tests/e2e/test_research_browser.py -q` | 修旧API投影夹具后1 failed / 2 passed，34.77s；研究两项及稳定截图通过，旧链暴露第二处注册夹具问题 |
| `TRADEOS_REQUIRE_E2E=1 python3 -m pytest tests/e2e/test_phase1_browser.py::test_phase1_browser_visible_reply_to_handoff_chain -q --tb=short` | 修旧版本注册夹具后1 passed，21.51s；8项范围均有通过证据，**不是单轮8 passed** |
| `npm test` | 20 files / 170 tests passed |
| `npm run typecheck` | exit0 |
| `npm run build` | exit0，Vite 109 modules |
| `npm run lint` | **0 errors / 133原有warnings**；非pristine，无本次新增Vue改动 |
| `npm run gen:api && git diff --exit-code -- src/api/api.d.ts` | exit0，生成无漂移 |
| 迁移U/D/U | 全量包含真实隔离Postgres初始upgrade0040→downgrade base→upgrade head及0039 quota roundtrip，全部通过；由 `scripts/run_alembic.py` 执行，避开T7 AppleDouble |
| `python3 scripts/accept_research_discovery.py` | exit0：`status=not_run, reason=explicit_opt_in_required, model=not_run, outreach=not_run`；无真实配置读取、live未运行 |

新功能前基线4007 passed/6 deselected、前端19files/151tests不是本次最终结果。
中间专项47 passed也不与最终全量相加。初次测试失败、修复及重跑范围见下文。

## Task5 TDD与实际装配修复

- CLI默认/缺配置：新增2测试先RED（脚本缺失），实现后通过；另覆盖缺预算、非法参数不回显。
- 显式验收workflow：原组合不接受`workflow_type`参数，RED；加入仅允许
  `demand_discovery|research_source_acceptance`的可信组合限定，默认行为不变。
  非本租户、任意其他type、取消/失败/完成Run均拒绝。
- 多连接锁循环：RED准确停在`PostgresWebProviderQuotaGuard.reserve`对Run的第二次
  `FOR UPDATE`，3秒超时、Provider零调用。engine持Run锁执行handler，旧预算guard另连接
  重取同锁；前序“PG研究闭环”使用假search，没有覆盖这个真实组合缝隙。
  修复为独立`tradeos:web-run-budget:v1` tenant+run事务advisory锁；保留tenant/type/status/
  execute_search/budget及ledger计数，不改engine或Gateway核心。真实链GREEN，query/page
  两个并发最后预算最多一个dispatch；可能保守双拒绝，不承诺公平性。
- 来源专用Run不能冒充业务研究：RED发现沿用`execution_mode=research_only`使
  RunResearchView误读`pages_only`并校验失败。专用计划步骤仅复用预算校验，不带业务研究
  投影标记；实际审计读取成功且`research=None`。
- CLI并发：RED第二个入口将尚在进行的Run读成自己的running结果；现用独立
  tenant级事务try-advisory锁，返回`not_run/acceptance_in_progress`，结束自动释放。
- 空来源不能标成功：RED空结果仍`pages_only`；现为`no_results`，缺三线路为
  `partial_sources`，CLI只有完整来源验收成功才返回成功完成码。
- 研究浏览器初期失败是新增test harness遗漏身份/探针配置，按生产客户端契约补齐
  tenant+employee和真实路由探针后通过；未改产品测试期望来掩盖产品缺陷。
- 最终E2E首轮旧链失败：受控模型生成POST夹具直接以域ProposalView冒充API投影，缺少
  `can_confirm`导致按钮正确禁用。现通过真实GET提案接口读取投影作为受控生成响应，
  不硬编码许可。再次重跑到确认后发现该旧fixture只注册v1，API已创建v2，poll未领取。
  改用当前产品的`register_demand_discovery`与已有双版handlers后完整旧触达链通过。
  未改旧outreach计划含义、生产门禁或断言；初次错误与两次范围重跑均保留，不重复全后端。

## 可执行真实入口与停止

完整配置步骤见[HANDBOOK第九节](../../HANDBOOK.md)。脚本
`scripts/accept_research_discovery.py`默认安全返回not_run，只有
`--live --budget-confirmed --proposal-id <已确认提案> --actor-id <确认老板>`才进入真实装配。

真实依赖来自原部署环境和数据库：Tavily密钥引用/显式独占声明、HMAC引用/版本、tenant、
工具lease、S3配置；原已生效Playbook与精确国家政策；仍在职的确认老板；当前active、
已确认research_only提案和原预算。脚本不会创建老板、提案、法律许可、免费账户或模型。
先停止普通scheduler推进，避免同时消耗该提案的业务研究预算；来源验收本身另需明确预算确认。

专用`research_source_acceptance`只在脚本引擎注册，以正常引擎生命周期结束。
普通scheduler按已注册type/version领取，真实测试证明不领取专用pending Run。
页面只从获准查询结果读取，Tavily固定basic/无收费回退，经过安全HTTP与Artifact Store。
原文只在不可变Artifact，Run保留URL/观察时间/hash/artifact引用/线路。
`pages_only`是来源验收，不产生Signal/Hypothesis/Validated Need/联系人，也不自动发送。
同提案固定幂等键；不重建quota槽、清表、放掉未决预留或自动重试已可能执行搜索。

Ctrl-C尽力取消专用Run；硬杀进程可能留下专用在途Run与保守未决预留，普通worker仍不领取。
恢复前运维须核实账户、ledger与原提案；不得用新key引用/新tenant/新临时DB伪造额度恢复。

Task5首轮审查发现并修复：CLI原先将执行后异常也报not_run，无法证明未执行。
现仅参数拒绝或已证明的预检拒绝使用not_run；真实入口调用后未分类异常输出固定脱敏
`unknown/execution_status_unknown`（exit3），包括结果查询失败及资源关闭失败。
能从engine.start取得时保留格式校验过的Run ID，不读取异常原文或不可信context猜ID。
真实多连接集成覆盖已完成3次搜索后结果聚合查询失败，Run仍completed、已消费仍持久，
安全Run ID仍可供排查；未知状态不自动重试，先人工查tenant+Run/提案/ledger。
该审查修复相关CLI/来源单元与真实集成19项通过（9.20s），ruff、相关mypy三文件、
结构七项、敏感扫描及diff检查通过。上表全量/E2E为修复前c24e80f的实际结果；
本轮按风险仅重跑相关scope，未重复无关全量或浏览器验收；独立复审确认原问题关闭、无新增问题。

## 浏览器证据

root手动QA使用合成`tests/research_ui_preview.py`，API8184/Vite5184；
因原跨源出现间歇接收失败，手动后续仅测试配置使用同源/api代理，未改生产CORS。
原后端POST/GET200且CORS正常；不能据同源通过断言跨源问题根因已修复。

手动1280×720通过：指挥中心空输入禁用；研究提案预算4/6/6/3、三线路、US/hinges；
拒绝后0Run；新提案确认后1Run；雷达3信号/1待核验/0假设、来源及推断分离；
客户发现2个不同合成域名、零已验证地址；Run额度/停止摘要；Settings研究状态卡。
雷达标签曾被摘要压成0高，Task4修复后root实测72.49px、可切换。
五路由console error/warn为空，关键截图已通过Browser runtime显示。
Task4复审版本cdcd652正常确认→只读刷新同Run→雷达刷新再次通过；异常恢复由自动测试覆盖。

手动未通过/未覆盖：viewport.set390×844后实际DOM仍1280，因此手动窄屏not_run；
Settings旧Playbook/国家政策未装配503，不能称完整Settings通过；fixture0假设不是研究闭环，
fixture consumed3不是Provider消费。原跨源瞬时接收失败根因仍未定位。
新增正式Playwright实际新context390×844与1280×900，独立API/Vite origin，验证确认/拒绝/
只读刷新和四雷达标签在视口内可点击；不操作root浏览器。原六项跨源E2E保留，不替为同源。
自动截图路径：`/tmp/tradeos-phase2-acceptance-screenshots/research-390.png`和`research-1280.png`。
首张桌面截图处在刷新中间态，未作为稳定页面证据；增加刷新按钮恢复可用、三来源重新渲染、
loading消失的条件等待后重跑两尺寸，通过并重新截图。实现者实际view_image复核稳定结果；
窄屏摘要不挤压，四标签由自动测试逐个滚动到视口并点击，截图不声称一屏容纳全部内容。

## 上游关键证据与限制

- Task1连接器已独立review clean；Tavily仅固定basic，usage/search响应/密钥均脱敏，
  capability未知/付费/usage失败关闭。官方依据：[Search](https://docs.tavily.com/documentation/api-reference/endpoint/search)、
  [Usage](https://docs.tavily.com/documentation/api-reference/endpoint/usage)。
- Task2持久账户槽/请求预留已独立review clean；HMAC轮换在consumed交付缝隙可能重放的
  Important问题已用Run持久指纹版本绑定修复，专项回归保留在`test_search_quota.py`。
- Task3已独立review clean；修复robots编码%2F、%2A/%24，模型合并同URL/hash丢线路，
  多行逐字原文被单行校验拒绝等问题。ISO2支持完整已分配代码快照，英文国名别名有限；
  英文自述句式+明确所在地才可能建企业，不是工商验证或全球多语识别。Contact us、
  Shipping to、目录筛选国家不是所在地证据。未知页面保持pending。
- robots是保守有界子集，**不是访问授权**，同源重定向限制可能漏读；
  [RFC9309](https://www.rfc-editor.org/rfc/rfc9309.html)是协议依据而非法律判断。
- 0040跨线路唯一键：空/兼容数据U/D/U可过；若同页多线路证据已存在，
  downgrade恢复旧唯一键会失败关闭以保护证据。不能删数据让降级“通过”。
- Task4已独立review clean：精确tenant+原幂等键的只读execution状态、
  confirmed但未启动的显式恢复、摘要刷新失败可见、负面可读quota快照允许显式重新核验；
  未配置、预算缺失、DB快照故障继续禁用。确认不等于Run已启动，不自动重发POST。
- Account.remaining是保守下界，无跨部署协调、无自动月度恢复。Run尝试数不是实际credit，
  consumed/reserved/uncertain独立按tenant+Run统计。低signal容量可先于page预算停止，
  planned_discovery_lanes与实际持久证据discovery_lanes不能混淆。

## 成本与未测变量

新增搜索/数据来源遵守免费限制，不表示系统成本为零。真实模型token/延迟/质量、
数据库/Docker/S3存储与网络、运行维护和人工核验/接管时间仍有成本，本次未测量。
受控测试无法证明真实网页覆盖率、真实转化率、法规适用或“每单位成本的合格贸易机会数”
改善。不得把搜索次数、页数、候选数、合成3credits当北极星指标结果。

免费账户永久保守预留可能降低可用性；硬杀进程、账户共享、真实供应商usage不一致、
政策撤回/凭证轮换仍需运维核验。未引入商业数据/付费fallback，未扩建新Phase。

## root裁定归档（含理由与代价）

以下11条按本批裁定出现顺序保留，避免scratch清理后丢失风险判断：

1. API 未提供可验证账户周期时，不按本机月历自动释放不确定额度 — 防止未知扣费被重新花掉 — 若判断过保守，免费额度需要人工核实后恢复，会降低可用性但不引入付费。

2. 搜索接口国家偏好不支持某个 ISO2 时省略 provider boost，保留老板原查询 country 与查询文本约束 — 国家偏好不是所在地证据 — 可能减少检索精确度，必须用原网页证据核实所在地。

3. Tavily 首批采用单数据库单账户槽，由一个 tenant 持有，密钥引用/轮换不新增额度槽 — /usage 无真实账户 ID，不能安全识别任意别名是否属于同账户 — 若业务需要同库多个独立免费账户会被保守阻断，后续需可验证账户身份契约；不同部署共享账户须由运维禁止，不能宣称数据库之外具备协调。

4. 研究企业候选采用原页URL host、本企业身份/经营自述原文、明确所在地原文的组合证据；不把正文是否包含域名作为充分或唯一必要条件，目录第三方无法核实官网时只保留待核验signal — 正文域名会漏掉普通官网且目录页脚也有域名，不能证明主体 — 若判据过宽可能收录不实企业自述，过严会漏收；必须保留企业自述来源标签，不宣称工商真实性已核实。

5. 不为自动发现引入必须逐host预批准的静态白名单；沿用人工激活国家政策、Playbook和批准搜索结果的公开研究授权，增加有界robots/明确站点禁令/登录验证码阻断与初始来源范围重定向约束 — 架构08第七节允许公开企业页/目录，静态名单会使发现退化为已知站点读取 — 若漏识别站点限制可能读取不适合自动化的公开页面，须保留受信禁止策略与人工条款核实，不能宣称robots是法律授权；同源重定向与保守robots会漏读部分合法站点。

6. 研究页收集同时受可持久来源容量max_signals限制，每份已读来源优先保留一条有效Signal，同URL+hash的模型合并摘录由代码展开归属，额外观察只在剩余预算内保存；容量/额外观察超限明确budget_exhausted，planned_discovery_lanes与实际已保存证据discovery_lanes分开 — 不新增来源表或突破确认预算，也不因模型去重静默丢线路 — 若容量策略过保守，低signal预算会比page预算更早停止读取并漏掉后续有效页面，必须在界面与验收说明。

7. 手动UI预览改用仅测试配置的Vite同源/api代理，以继续隔离验证界面交互；不改生产身份或CORS，原跨源浏览器瞬时接收失败保留未定位记录 — 后端实际HTTP与CORS均正常，同源预览是标准本地装配且不需要放宽门禁 — 若跨源失败属于产品问题，同源QA可能掩盖它，须明确手动验收范围，并由Task5保留既有跨源E2E验证与风险说明。

8. demand_radar.py与runs.py已有response_model自动消费扩展的DemandSignalView/RunSummaryView/RunDetailView，保持路由文件不改；契约通过生成OpenAPI与API测试验证 — 避免为计划文件清单制造无行为的改动 — 若自动序列化假设错误会漏字段，须以API返回/生成schema而非文件改动数量验收。

9. 受信配置存在、持久快照可读且预算确认后，允许老板显式请求“重新核验后研究”，即使上次快照为usage_unknown/paid_enabled/quota_exhausted；快照如实展示，实际搜索仍由原Gateway当次usage与持久额度决定，未配置/数据库读取失败继续阻断 — 否则一次暂时用量失败会让唯一核验执行入口永久关闭；不新增旁路或释放预留 — 可能增加被门禁拒绝的Run及usage读取，UI必须明确并非免费可用声明；保守历史预留仍可能使额度无法恢复，不能承诺重试就能继续搜索。

10. 真实联网验收仅验证Tavily与三线路安全原页取证，真实模型仍可not_run；采用独立来源验收workflow_type/引擎生命周期，明确pages_only，不把手工跳步的demand_discovery v2记为完整研究完成；复用同库账户额度和真实Gateway，可信composition允许显式限定验收workflow_type且业务默认不变，不在普通scheduler注册验收定义 — 用户要求三类真实页证据与受控后续链路分开，不需要为验收虚构真实模型或全装配scheduler — 增加一个需维护的受限验收入口和审计类型，真实模型质量/完整生产调度仍未验证，必须分栏披露且不能使普通worker误领取半成品Run。

11. 由Task5当前实现者在预算guard边界修复真实装配互等，使用独立命名空间tenant+run事务advisory锁串行预算检查，不锁workflow主行；保留tenant/type/status/step/budget校验及已提交Tool ledger计数，engine与Gateway核心不改 — 真实handler运行时engine已持主行锁，独立session重复取锁无法完成，必须消除该锁循环 — 若新串行计数有误会超发或过度阻断，必须用真实engine→Gateway组合与并发query/page最后预算测试核实，另由Task5和最终全分支审查检查；不以弱化预算或同session假集成解围。

## 待root填写

- Task5独立审查：初审1项Important，修复1a834e5后scoped复审通过，无新增问题。
- 最终全分支审查：pending。
- 是否满足本地主分支集成条件：pending；本任务不合并、不推送、不部署、不发信。
