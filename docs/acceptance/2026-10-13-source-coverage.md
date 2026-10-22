# 33项来源的代码覆盖与实际缺口

基于 `0686f12` 的只读代码调查；后续 `4868aff` 已修正Tavily/Codex角色展示，并已随 `934db85` 部署和真实UI验证，以下原始缺口保留用于核对，2026-10-13。未访问生产、凭证或外部网站，未调用供应商，也未修改实现。下文“公开/无需凭证”仅表示仓库注册表声明；实时端点、许可、免费额度及地域覆盖仍需后续官方文档和受控调用核实，不能据此承诺当前免费可用。

2026-10-14补记：第八轮jslt真实UI已通过共用官网采集链取得并交付Simba公司及1个公开邮箱，刷新和重启后持久保留；当前生产8b9e9b0。下列专用适配器缺口未因此消失，不能用1条成功替代33项逐项验收。完整运行记录见[候选库存验收](2026-10-13-candidate-inventory.md)。

## 核心判断

用户要求“除未付费/未配置外都有效使用”尚未完成；但不能把33项都当成33个独立企业数据库：

- **11项网页类别**共享同一个 Tavily 搜索→公开页/浏览器→官网核验collector。它们是查询策略，不是11个已完成逐站适配的连接器。可尝试采集也不等于实际取得有效企业。
- **3项基础设施**：Tavily已实际装配为上述类别的搜索后端；Codex CLI已装配为工具规划/提取执行者，但原登记的“CLI内置网页搜索”明确禁用；Brave已有旧搜索实现但未接本库存。原版本三者全部被库存状态表硬编码成not_integrated，至少前两者丢失了实际角色信息；4868aff已修正前两者，Brave仍明确未接本库存。
- **10项采购来源**：其中9项登记public，SAM.gov登记API key；均没有专用采集适配器。UNGM额外具有平台会话探测，但没有公告采集器。普通“公开采购公告网页”策略可能偶然检索到这些站点，不能记成官方接口接通。
- **1项RSS**：无RSS/Atom解析、订阅或增量游标实现。
- **8项凭证/商业来源**：Hunter有域名邮箱/验证及平台账户检查，其他7项只有注册表/凭证表单，没有专用执行路径。是否已付费、已配置在本轮不可判断。

在审计基线0686f12中，22项not_integrated = 9项public采购 + SAM.gov + RSS + 8项凭证/商业来源 + 3项基础设施。不能全部解释成“缺钱或缺key”。真正无需登记凭证但执行代码未完成的清单至少是 **9项公开采购 + RSS，共10项**；这只是代码缺口统计，不是免费服务承诺。

## 代码证据索引

以下路径均相对本工作树，可直接定位：

- **R**：`shared/schemas/discovery_sources.py`：准确33项selected；backup/example未混入统计。定义的integration_status是静态说明，不是运行事实。
- **W**：`infra/db/inventory_sources.py` 的WEB_INVENTORY_SOURCES及list_sources；`apps/scheduler_worker/inventory_collector.py` 的strategies/execute/project_candidate：11类共用搜索路径，非官网结果只作discovery_only；最终入库source_id固定company_websites，原探索类别保存在source attempt。
- **G**：`apps/scheduler_worker/inventory_web.py`、`standalone_inventory.py`：真实搜索固定注入Tavily/free_search，页面经Gateway，浏览器调用独立browser_worker。
- **P**：`connectors/web_search/transport.py:205` SafePublicPageHttpTransport：安全DNS/peer/robots/同源重定向，正文只允许text/html；不能直接拿来读取官方JSON/XML/CSV。`connectors/playwright/public_page.py`、`apps/browser_worker/public_page.py`：空上下文公开HTML及内联脚本读取，非登录会话复用，当前不加载外链JS/CSS。
- **T**：`connectors/tavily/client.py`、`transport.py`、`tool_gateway/handlers/free_search.py`、`infra/db/search_quota.py`：真实/search和/usage、固定basic、持久保守免费配额及未知请求不重发。
- **B**：`connectors/web_search/transport.py:119` BraveSearchApiTransport、`connectors/web_search/client.py`、`apps/scheduler_worker/web_discovery.py`：旧搜索可选择brave/tavily；库存G没有Brave选择入口及对应免费额度契约。
- **C**：`connectors/codex_agent/client.py`、`discovery.py`、`runner.py`，`apps/scheduler_worker/inventory_model.py`：已装配父进程Gateway工具；runner的`web_search='disabled'`，不等于登记的CLI内置搜索已启用。
- **H**：`connectors/hunter/client.py`（validate_account/find_contacts/verify）、`transport.py`、`usage.py`；`tool_gateway/handlers/contact_enrichment.py`、`contact_verification.py`等既有联系人路径。Hunter只允许/account、/domain-search、/email-verifier；Discover/Finder未实现。
- **A**：`domains/organization/data_sources.py`：目录、凭证状态、人工备注与探测结果分离；`apps/api/platform_data_source_usage.py`的supports仅hunter；`infra/data_source_usage.py`也仅hunter。其他来源没有平台通用API就绪检查。
- **U**：`connectors/playwright/login_probe.py`、`domains/organization/data_source_browser.py`、`apps/api/routers/platform_data_sources.py`：仅UNGM固定登录恢复探测及状态时效；不是公告分页、字段提取或库存采集。
- **E**：`domains/prospecting/public_evidence.py`：官网名称/域名、国家地址、产品文本及同域邮箱证据门。采购机构、公共公告和历史授标不能直接冒充这类官网企业候选。

对connectors、tool_gateway、apps、workflows和infra的Python代码按精确source_id检索，采购API、RSS及其他商业服务未找到专用执行代码；注册目录与展示不算适配器。`connectors/trade_data/__init__.py`为空，也不能算贸易数据接入。

## 基线逐项覆盖（33项，状态取自0686f12）

下表保留0686f12审计基线：“网页条件可选”指W/G已装配且额度/权限允许时可被计划器选择；“NI”指当时库存API投影not_integrated。后续4868aff已将Tavily标为搜索后端、Codex标为规划提取执行者，31和33项的展示失真已修复；表中其余专用适配缺口仍在。前六轮真实UI均未交付，第八轮已交付1条；这个结果只验证已实际执行的最小链路，不证明其余来源业务可用。

| # | 来源ID / 名称 | 实际角色 | 已有可复用执行代码 | 库存现状 | 真正缺口 / 最小去向 |
|---|---|---|---|---|---|
| 1 | hunter / Hunter | 企业发现（Discover未做）；联系人补全、邮箱验证 | H；A有账户额度检查 | NI；未调用Hunter | 先复用补全/验证到企业私有联系人链；Discover需新endpoint/授权。不得将商业联系人自动写公共池 |
| 2 | apollo / Apollo | 企业/人员发现、联系人补全 | 仅R/A凭证管理 | NI | 专用API适配、字段授权/额度/复用许可；账户情况未知 |
| 3 | pdl / People Data Labs | 企业发现及身份/人员补全 | 仅R/A | NI | Company Search适配、证据/许可、可用字段；注册表已提示免费测试邮箱限制 |
| 4 | snov / Snov.io | 企业域名联系人补全/验证 | 仅R/A OAuth字段管理 | NI | Token父进程解析、异步任务/结果轮询和计费不确定处理；非现有代码复用即接通 |
| 5 | ocp / OCP Data Registry | 采购信号及采购/中标主体线索 | P可读说明页，未实现数据集读取 | NI；public登记 | 数据集清单、OCDS/CSV有界下载、增量及与国家站去重；不能把目录当一条全球统一API |
| 6 | ted / TED | 欧洲采购信号/采购主体 | P可读允许的公告页，无Search API/XML实现 | NI；public登记 | 官方查询adapter、notice ID/版本、买方/获奖方分角色；欧洲覆盖不应为非洲任务默认优先 |
| 7 | find_a_tender / 英国Find a Tender | 英国采购/授标信号 | P通用页；无OCDS适配 | NI；public登记 | OCDS解析、增量游标、主体与公告关系 |
| 8 | contracts_finder / 英国Contracts Finder | 英国采购/授标及承包商线索 | P通用页；无OCDS适配 | NI；public登记 | 可与上一项复用OCDS规范化，但端点/分页独立；跨站按原始标识去重 |
| 9 | sam_gov / SAM.gov | 美国采购信号/采购机构 | 仅R/A key管理 | NI | 需显式key；读取API、限流、日期窗、公开联系人权限。不能未经核实称付费或免费可用 |
| 10 | canadabuys / CanadaBuys | 加拿大采购信号/机构 | P通用页；无CSV adapter | NI；public登记 | 有界CSV下载/编码/列契约、更新检查及notice去重 |
| 11 | pncp / 巴西PNCP | 巴西采购/合同信号 | P通用页；无公开查询API实现 | NI；public登记 | 查询API、分页、葡语字段、原始标识；与Compras去重 |
| 12 | compras_gov_br / Compras.gov.br | 巴西采购/授标信号 | P通用页；无REST adapter | NI；public登记 | REST读取及PNCP同主体/同采购关系消歧 |
| 13 | world_bank / 世行采购公告 | 多国项目采购信号/实施机构 | P通用页；无API/下载适配 | NI；public登记 | 先核实登记链接是否覆盖全采购或仅能源；项目/实施机构/国家/期限结构化。对非洲查询可优先核实覆盖 |
| 14 | ungm / UNGM | 联合国系统采购信号/采购机构 | U会话探测；P公开页浏览器 | NI；public登记 | 公告查询/分页/详情/变更采集缺失；探测成功不等于采集成功。优先公开页面，不自动继承登录态到模型 |
| 15 | importyeti / ImportYeti | 交易记录驱动企业发现 | 仅R/A key管理 | NI | 商业API/credits/字段适配；不能因网站可访问当作批量API免费 |
| 16 | volza / Volza | 交易记录驱动企业发现 | 仅R与备注，commercial_pending | NI | 商务API/国家字段/认证与实际额度尚须明确；不主动购买 |
| 17 | panjiva / Panjiva | 企业交易关系发现 | 仅R/A OAuth字段 | NI | OAuth父进程、授权范围、交易查询与计费/证据适配 |
| 18 | importgenius / ImportGenius | 交易主体发现 | 仅R/A access_token字段 | NI | 独立API权限及国家覆盖、记录查询/字段与共享许可 |
| 19 | company_websites / 企业官网联系页 | 企业发现与公开事实核验 | W/G/P/E | 网页条件可选；唯一最终入公共池source_id | 仍需真实产出验证；不是客户需求或邮箱可达性验证 |
| 20 | industry_directories / 行业名录 | 企业线索发现 | W/G/P，directory查询策略 | 网页条件可选 | 逐站目录/分页尚无adapter；须继续官网核验，不能将目录host当企业 |
| 21 | trade_associations / 协会会员 | 企业/行业身份线索 | W/G/P，association查询策略 | 网页条件可选 | 逐协会可读目录/导出尚待接；会员不代表采购意向 |
| 22 | chambers / 商会会员 | 当地企业发现 | W/G/P，chamber查询策略 | 网页条件可选 | 逐商会adapter/目录许可/覆盖；搜索结果并非已抓全会员 |
| 23 | trade_fairs / 展商目录 | 企业发现/品类线索 | W/G/P，exhibitor查询策略 | 网页条件可选 | 届次、展商角色、增量与官网绑定；展商不自动当买方 |
| 24 | dealer_networks / 经销网络 | 渠道商发现/授权关系核验 | W/G/P，dealer查询策略 | 网页条件可选 | 逐品牌定位器和地区分页未做；品牌授权需保留明确证据 |
| 25 | company_procurement / 企业采购RFQ | 采购信号、采购主体线索 | W/G/P，procurement查询策略 | 网页条件可选，但输出仅官网企业事实 | RFQ规格/期限/买方角色及采购信号持久链未装配到本库存；网站关键词不是已验证需求 |
| 26 | public_procurement_pages / 公共采购网页 | 采购信号/机构线索 | W/G/P，buyer notice查询策略 | 网页条件可选，非专用采购API | 同上；还要公告ID/版本去重，不应与TED等重复计来源增量 |
| 27 | company_news / 企业新闻公告 | 企业发现、事件型需求信号 | W/G/P，company news查询策略 | 网页条件可选，最终仍是官网候选 | 事件主体/时间/推断分离尚未成为本库存输出；新闻提到国家不等于公司地址 |
| 28 | rss / RSS与Atom | 增量公告/新闻信号入口 | 无RSS解析器；只能P读对应HTML | NI；public登记 | Feed白名单、XML安全解析、guid/链接/更新时间游标、原文快照及主题角色 |
| 29 | linkedin_public / LinkedIn公司页 | 企业身份/经营线索 | W/G/P，site检索 | 网页条件可选；未接联系人API | 访问限制明确失败；不能把搜索索引命中当页面已读，更不能绕登录 |
| 30 | trade_record_indexes / 公开贸易索引 | 企业发现及交易信号 | W/G/P，trade records查询策略 | 网页条件可选 | 逐站记录结构、主体角色/时间/许可；不等于商业交易API接通 |
| 31 | codex_cli / Codex CLI网页搜索 | 当前是工具规划与提取执行者；登记角色是基础搜索 | C/G | NI展示失真；C已参与；原生web_search禁用 | 拆开“CLI运行器已装配”和“内置搜索未启用”；不要额外跑一遍Codex当独立数据源计产出 |
| 32 | brave / Brave Search | 基础搜索定位 | B可复用 | NI准确于本库存；旧连接器已有 | 单独接库存provider选择、明确额度/成本策略、Gateway和当前授权；不能套用Tavily免费账本 |
| 33 | tavily / Tavily | 基础搜索定位 | T/G已为11类提供搜索 | NI展示失真；底层已用 | 以基础设施状态显示usage/Gateway/运行事实，避免虚增一次来源计划；注册表用途说明需与实际授权核对，但本轮不重审外部条款 |

## 平台检查与库存状态的断层

平台有“保存凭证/备注/额度检查/浏览器检查”，这些功能并不构成通用运行就绪服务。代码只有Hunter可做平台usage-check，只有UNGM可做browser-check；后者仅恢复登录并检查会话。Tavily库存用量来自独立业务租户账本，Codex模型配置来自企业模型服务，均不是平台目录中的同一状态。因此，不能拿平台“配置成功”或UNGM“会话有效”批量把所有来源设置available。

应分别呈现：角色、adapter是否实现、配置是否绑定、检查时间及结果、当前预算、最近一次真实执行结果、产生的证据/候选数。基础搜索和模型执行者展示依赖状态，不进入企业来源排名重复计算收益。已有source attempt保留探索类别，但候选最终只写company_websites；若以后比较原始目录/采购API收益，需保留来源链（发现公告/目录→官网核验），不能只看最终source_id。

## 最小后续实施批次（建议，不代表已实施）

1. **先完成可观察性与真实链路恢复。** 修正Tavily/Codex角色与状态投影，保留Brave未接库存的真实状态；用固定诊断定位当前真实Provider失败，完成一条真实公开企业证据入库/少量交付。先让现有链可靠，不能用增加来源数掩盖所有来源共用的模型故障。
2. **接一种结构化公开信号，形成可复用通路。** 专用固定endpoint connector + Gateway manifest/handler + 有界JSON/XML/CSV读取 + 原件/哈希/来源ID + 信号与企业角色分离 + durable增量游标。不能放宽现有HTML reader让任意格式直接通过。选择有目标市场覆盖的world_bank/UNGM先核实真实接口；若需先验证OCDS机械能力，可用Find a Tender/Contracts Finder，但不要把英国覆盖计成非洲收益。
3. **完成10项无凭证登记来源的实际缺口。** 按可复用协议拆最小波次：OCDS（Find a Tender/Contracts Finder及OCP逐数据集）；采购JSON（PNCP/Compras、TED、经核实的World Bank）；CSV（CanadaBuys）；公开页面专用adapter（UNGM）；RSS/Atom增量。每项均报告端点/解析/信号持久化/企业官网解析是否已验，不把一个OCDS parser等同三站可用。不同国家按查询相关性调度，不要求每个非洲任务调用欧洲/美洲来源。
4. **复用已有Hunter的正确位置。** 企业发现后按当前企业权限、商业许可和免费余额明确允许的范围调用现有补全/验证；结果留在企业私有联系人链，不自动共享到公共池，验证成功也不构成发信授权。Discover是额外新adapter且需更新当前Hunter端点边界，不能假借现有domain-search当企业搜索。
5. **独立接Brave及已获授权的其他来源。** Brave可先复用搜索transport，但要真实账户/额度证明和库存装配。SAM.gov与其余商业源按已明确配置/字段权限/免费包含额度逐项做受控读取；未知许可/付费成本保持阻断，不以本建议自动购买或启动计费。

所有批次共同验收：不是“能返回HTTP200”，而是可追溯有效记录、正确主体/国家/时间、跨查询及跨公司正确去重、并发额度、未知请求不重派、当前权限撤销、真实失败状态。采购信号可先产生线索再找到企业，不能为了凑候选数把采购机构名称、目录站或中标企业自动认作当前买家。

## 容易忽略的变量

免费API不等于零成本：带宽/存储、XML/CSV解析、模型核验和人工复核仍有成本；历史采购记录不等于活跃需求；公开可读不等于允许跨企业复用；公司/买方/供应商/公告发布者必须分角色。33项中的地理覆盖不均，供应商名录可能产生卖方偏差。目标仍应按合格贸易机会与成本评估，不能以调用完33项作为完成或效率指标。
