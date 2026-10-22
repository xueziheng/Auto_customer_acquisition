# 服务器 QQ 获客源保留与接入审计

证据：服务器精确源码快照 `5b6474a`、升级输入 `682c541`。本表是代码接入审计，不是联网可用性测试，也不读取服务器凭证或运行数据。

54 项由 33 个已选、14 个备选、7 个目录示例组成。原静态目录标注为 13 项部分接入、39 项待接入、2 项已有连接器。这些标注不代表当前账号有权限、剩余额度或实际获客成功。

## 配置与 UI 保留

逐文件忽略 CRLF 比较，以下目录/组件/接口/配置存储在 QQ 与昨日升级之间内容一致；集成结果仍需回核：

- `shared/schemas/discovery_sources.py`
- `apps/web/src/components/PlatformDataSources.vue`
- `apps/web/src/components/DataSourceConnection.vue`
- `apps/web/src/components/DataSourceUsage.vue`
- `apps/api/routers/platform_data_sources.py`
- `apps/api/platform_data_source_usage.py`
- `infra/data_source_credentials.py`
- `infra/data_source_notes.py`
- `infra/data_source_usage.py`
- `domains/organization/data_sources.py`
- `domains/organization/schemas.py`

原始配置保存在服务器部署私有目录，本任务没有复制、修改或推送这些内容；GitHub 保存源码及其读取协议。没有数据库和密钥备份就不能声称 GitHub 能完整恢复业务环境。

## 逐来源现状

| ID | 来源 | 类别 | 原声明 | 实际代码路径 | 本次联网测试 |
|---|---|---|---|---|---|
| hunter | Hunter | 已选 | 部分接入 | 未接入库存采集；Hunter 域名邮箱/验证/账户额度路径 | 未运行 |
| apollo | Apollo | 已选 | 接口待接入 | 未接入库存采集 | 未运行 |
| pdl | People Data Labs | 已选 | 接口待接入 | 未接入库存采集 | 未运行 |
| snov | Snov.io | 已选 | 接口待接入 | 未接入库存采集 | 未运行 |
| ocp | OCP Data Registry | 已选 | 接口待接入 | 未接入库存采集 | 未运行 |
| ted | TED | 已选 | 接口待接入 | 未接入库存采集 | 未运行 |
| find_a_tender | 英国 Find a Tender | 已选 | 接口待接入 | 未接入库存采集 | 未运行 |
| contracts_finder | 英国 Contracts Finder | 已选 | 接口待接入 | 未接入库存采集 | 未运行 |
| sam_gov | SAM.gov Opportunities | 已选 | 接口待接入 | 未接入库存采集 | 未运行 |
| canadabuys | CanadaBuys | 已选 | 接口待接入 | 未接入库存采集 | 未运行 |
| pncp | 巴西 PNCP | 已选 | 接口待接入 | 未接入库存采集 | 未运行 |
| compras_gov_br | 巴西 Compras.gov.br | 已选 | 接口待接入 | 未接入库存采集 | 未运行 |
| world_bank | World Bank Procurement Notices | 已选 | 接口待接入 | 未接入库存采集 | 未运行 |
| ungm | UNGM | 已选 | 接口待接入 | 未接入库存采集；仅专用会话登录检查 | 未运行 |
| importyeti | ImportYeti | 已选 | 接口待接入 | 未接入库存采集 | 未运行 |
| volza | Volza | 已选 | 接口待接入 | 未接入库存采集 | 未运行 |
| panjiva | Panjiva | 已选 | 接口待接入 | 未接入库存采集 | 未运行 |
| importgenius | ImportGenius | 已选 | 接口待接入 | 未接入库存采集 | 未运行 |
| company_websites | 企业官网、联系页与公开店铺 | 已选 | 部分接入 | 通用搜索→原站读取→证据投影 | 未运行 |
| industry_directories | 行业企业名录 | 已选 | 部分接入 | 通用搜索→原站读取→证据投影 | 未运行 |
| trade_associations | 行业协会会员目录 | 已选 | 部分接入 | 通用搜索→原站读取→证据投影 | 未运行 |
| chambers | 商会会员网络 | 已选 | 部分接入 | 通用搜索→原站读取→证据投影 | 未运行 |
| trade_fairs | 展会展商目录 | 已选 | 部分接入 | 通用搜索→原站读取→证据投影 | 未运行 |
| dealer_networks | 品牌授权经销商网络 | 已选 | 部分接入 | 通用搜索→原站读取→证据投影 | 未运行 |
| company_procurement | 企业采购、供应商招募与 RFQ | 已选 | 部分接入 | 通用搜索→原站读取→证据投影 | 未运行 |
| public_procurement_pages | 公开采购公告网页检索 | 已选 | 部分接入 | 通用搜索→原站读取→证据投影 | 未运行 |
| company_news | 企业新闻、公告与公开媒体 | 已选 | 部分接入 | 通用搜索→原站读取→证据投影 | 未运行 |
| rss | 企业与公告 RSS 订阅 | 已选 | 接口待接入 | 未接入库存采集 | 未运行 |
| linkedin_public | 公开 LinkedIn 公司页面 | 已选 | 部分接入 | 通用搜索→原站读取→证据投影 | 未运行 |
| trade_record_indexes | 公开贸易记录索引 | 已选 | 部分接入 | 通用搜索→原站读取→证据投影 | 未运行 |
| codex_cli | Codex CLI | 已选 | 部分接入 | 工具规划/提取，不是独立客户库 | 未运行 |
| brave | Brave Search | 已选 | 已有连接器 | 未接入库存采集；Brave 搜索连接器路径 | 未运行 |
| tavily | Tavily | 已选 | 已有连接器 | 库存搜索后端，不是独立客户库 | 未运行 |
| zerobounce | ZeroBounce | 备选 | 接口待接入 | 未接入库存采集 | 未运行 |
| singapore_data | 新加坡 data.gov.sg | 备选 | 接口待接入 | 未接入库存采集 | 未运行 |
| gebiz | 新加坡 GeBIZ | 备选 | 接口待接入 | 未接入库存采集 | 未运行 |
| gdelt | GDELT | 备选 | 接口待接入 | 未接入库存采集 | 未运行 |
| greenhouse | Greenhouse Job Board API | 备选 | 接口待接入 | 未接入库存采集 | 未运行 |
| openstreetmap | OpenStreetMap / Overpass | 备选 | 接口待接入 | 未接入库存采集 | 未运行 |
| common_crawl | Common Crawl | 备选 | 接口待接入 | 未接入库存采集 | 未运行 |
| gleif | GLEIF | 备选 | 接口待接入 | 未接入库存采集 | 未运行 |
| companies_house | 英国 Companies House | 备选 | 接口待接入 | 未接入库存采集 | 未运行 |
| insee | 法国 INSEE Sirene | 备选 | 接口待接入 | 未接入库存采集 | 未运行 |
| opencorporates | OpenCorporates | 备选 | 接口待接入 | 未接入库存采集 | 未运行 |
| serper | Serper | 备选 | 接口待接入 | 未接入库存采集 | 未运行 |
| exa | Exa | 备选 | 接口待接入 | 未接入库存采集 | 未运行 |
| serpapi | SerpApi | 备选 | 接口待接入 | 未接入库存采集 | 未运行 |
| blum | Blum 经销商目录 | 目录示例 | 接口待接入 | 未接入库存采集 | 未运行 |
| nfda | NFDA 会员目录 | 目录示例 | 接口待接入 | 未接入库存采集 | 未运行 |
| efda | EFDA | 目录示例 | 接口待接入 | 未接入库存采集 | 未运行 |
| sicci | SICCI 商会会员目录 | 目录示例 | 接口待接入 | 未接入库存采集 | 未运行 |
| smcci | SMCCI 商会会员目录 | 目录示例 | 接口待接入 | 未接入库存采集 | 未运行 |
| eisenwarenmesse | 科隆五金展展商目录 | 目录示例 | 接口待接入 | 未接入库存采集 | 未运行 |
| ish | ISH 展商目录 | 目录示例 | 接口待接入 | 未接入库存采集 | 未运行 |

## 执行路径与需验证的行为

- 配置入口：`PlatformDataSources.vue` → `apps/api/routers/platform_data_sources.py` → `domains/organization/data_sources.py` → `infra/data_source_credentials.py`；保留原 source_id、凭证字段名、版本和平台权限。
- QQ 原库存链：`standalone_inventory.build_inventory_driver` → `InventorySources` → `InventoryAcquisitionDriver` → `CodexInventoryCollector` → Tool Gateway 搜索/页面读取 → 证据投影。11 类来源是检索策略，不能折算成 11 个独立 API。
- 升级增加搜索后端绑定与恢复；应验证 DeepSeek/Tavily 当前许可、配置版本与恢复预留，不能因为来源显示 available 就把尚无真实结果的任务记为成功。
- Hunter 账户额度检查不等于 Discover 或 Finder 已接入；当前 `PlatformDataSourceUsage.supports` 只允许 Hunter。
- UNGM 登录会话检查不等于公告采集已接入。
- `scripts/diagnose_free_sources.py` 中的 TED 样本诊断是独立诊断入口，不能称为已接入自动客户库存。
- 测试应覆盖已有配置在轮换/删除/跨租户后失效、11 种通用来源受同一任务和 Gateway 约束、失败不会记为成功/重复付费。

## 本次任务边界

本次合并保留既有能力、修复合并兼容问题，不将 39 项待接入来源凭空标成可用。它们仍需要按实际目标市场、账号权限和返回数据设计连接器、接入库存投影并逐项验收。真实联网、收费权限、额度、网站访问限制均未在本次确认。
