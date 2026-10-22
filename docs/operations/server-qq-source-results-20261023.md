# QQ 环境 33 个来源最终验收（2026-10-23）

源码运行提交：`f60563024997e01b8fa6d6cc12f66e47f9888f3e`。下面区分受控执行、真实接口诊断与接入状态；不能将配置存在或测试替身通过当作实际客户开发成功。

原目录54项完整保留，其中已选33项、备选14项、示例7项；已选项中的18项原本仍待接入自动获客链。本轮没有删除这些登记或把它们标成可用。11类网页来源共享搜索/原页读取链，分别验证了实际调度、来源策略、用户产品与地区、配额请求ID、事实证据哈希，以及排除模型虚构页面和目录冒充官网。

服务器来源受控组合88项通过。真实诊断共3次供应商HTTP：DeepSeek账户元数据1次、TED公告1次、Hunter免费账户1次；页面请求0、模型请求0、业务写入0、真实邮件0。诊断会写Tool Gateway账本与一次性journal。

原主环境4组来源配置（Hunter、Apollo、SAM.gov、Snov）和23条备注已通过受信宿主迁入QQ控制租户；原配置保持不变。新绑定重新记录版本与审计，不继承旧测试“通过”状态或使用量。密钥和备注正文未进入Git、模型输出或报告。

| 来源ID | 受控验证 | 实际接口结果 | 当前状态 |
|---|---|---|---|
| hunter | 账户、Domain Search、Verifier连接器及Gateway受控测试通过 | 免费账户接口认证/额度通过；未调用真实Domain Search或Verifier | contact_helpers_only |
| apollo | 目录/配置契约通过；无完整库存采集实现 | 未运行 | not_integrated |
| pdl | 目录/配置契约通过；无完整库存采集实现 | 未运行 | not_integrated |
| snov | 目录/配置契约通过；无完整库存采集实现 | 未运行 | not_integrated |
| ocp | 目录/配置契约通过；无完整库存采集实现 | 未运行 | not_integrated |
| ted | 有界公共公告诊断测试通过；未装配库存链 | 公开API返回1条样本，公告号/标题存在；未验证产品适配 | diagnostic_only |
| find_a_tender | 目录/配置契约通过；无完整库存采集实现 | 未运行 | not_integrated |
| contracts_finder | 目录/配置契约通过；无完整库存采集实现 | 未运行 | not_integrated |
| sam_gov | 目录/配置契约通过；无完整库存采集实现 | 未运行 | not_integrated |
| canadabuys | 目录/配置契约通过；无完整库存采集实现 | 未运行 | not_integrated |
| pncp | 目录/配置契约通过；无完整库存采集实现 | 未运行 | not_integrated |
| compras_gov_br | 目录/配置契约通过；无完整库存采集实现 | 未运行 | not_integrated |
| world_bank | 目录/配置契约通过；无完整库存采集实现 | 未运行 | not_integrated |
| ungm | 浏览器登录状态检查受控测试通过；未装配库存链 | 未运行：部署绑定未启用 | not_integrated_inventory |
| importyeti | 目录/配置契约通过；无完整库存采集实现 | 未运行 | not_integrated |
| volza | 目录/配置契约通过；无完整库存采集实现 | 未运行 | not_integrated |
| panjiva | 目录/配置契约通过；无完整库存采集实现 | 未运行 | not_integrated |
| importgenius | 目录/配置契约通过；无完整库存采集实现 | 未运行 | not_integrated |
| company_websites | 逐ID实际Collector/DiscoverySession调度、产品/地区绑定、官网证据投影通过 | 未运行真实搜索 | blocked_search_configuration |
| industry_directories | 逐ID实际Collector/DiscoverySession调度、产品/地区绑定、官网证据投影通过 | 未运行真实搜索 | blocked_search_configuration |
| trade_associations | 逐ID实际Collector/DiscoverySession调度、产品/地区绑定、官网证据投影通过 | 未运行真实搜索 | blocked_search_configuration |
| chambers | 逐ID实际Collector/DiscoverySession调度、产品/地区绑定、官网证据投影通过 | 未运行真实搜索 | blocked_search_configuration |
| trade_fairs | 逐ID实际Collector/DiscoverySession调度、产品/地区绑定、官网证据投影通过 | 未运行真实搜索 | blocked_search_configuration |
| dealer_networks | 逐ID实际Collector/DiscoverySession调度、产品/地区绑定、官网证据投影通过 | 未运行真实搜索 | blocked_search_configuration |
| company_procurement | 逐ID实际Collector/DiscoverySession调度、产品/地区绑定、官网证据投影通过 | 未运行真实搜索 | blocked_search_configuration |
| public_procurement_pages | 逐ID实际Collector/DiscoverySession调度、产品/地区绑定、官网证据投影通过 | 未运行真实搜索 | blocked_search_configuration |
| company_news | 逐ID实际Collector/DiscoverySession调度、产品/地区绑定、官网证据投影通过 | 未运行真实搜索 | blocked_search_configuration |
| rss | 目录/配置契约通过；无完整库存采集实现 | 未运行 | not_integrated |
| linkedin_public | 逐ID实际Collector/DiscoverySession调度、产品/地区绑定、官网证据投影通过 | 未运行真实搜索 | blocked_search_configuration |
| trade_record_indexes | 逐ID实际Collector/DiscoverySession调度、产品/地区绑定、官网证据投影通过 | 未运行真实搜索 | blocked_search_configuration |
| codex_cli | 真实Linux Codex CLI/bwrap + 合成本地Provider：5项通过 | 未调用真实模型 | runner_verified_search_configuration_missing |
| brave | 独立搜索连接器/Gateway受控测试通过；未装配库存链 | 未运行：无来源配置 | not_integrated_inventory |
| tavily | 搜索/配额预留/恢复契约通过 | 未运行：缺少专用免费账户配置 | not_configured |

## DeepSeek 与搜索配置

已核对[DeepSeek官方Search文档](https://api-docs.deepseek.com/guides/search/)，搜索API确实存在。常规成功请求标价为$0.75/1000；内置工具临时免费说明不能推导为直接REST调用永久免费。本次账户检查没有证明诊断脚本要求的赠金额度条件，因此query_count=0；没有把“余额可用”当作“免费”。

QQ当前实际模型配置原来缺少`research`及`inventory_search`。已有DeepSeek平台连接和密钥引用已保留，但这不等于搜索预算与后端绑定已配置；真实库存网页搜索目前仍被配置门禁阻止。Tavily仍保留为可选后端。未擅自开启付费搜索或改动企业额度。

Hunter只调用官方明确免费的[`/v2/account`](https://hunter.io/api-documentation.md)，证明认证和额度读取正常；没有证明客户发现或邮箱可达。TED样本只证明公告接口返回了字段完整的记录，不证明其与本企业产品相关或采购人可联系。

18个待接入项仍需要连接器、Gateway装配和统一库存投影的后续实现；TED/UNGM现有诊断不替代这些工作。详见机器可读[33项结果](server-qq-source-results-20261023.json)。
