# ADR 0016：免费搜索使用供应商无关契约与保守 Tavily 用量判别

- 状态：已接受
- 日期：2026-08-27

## 背景

需求发现需要可替换的公开搜索来源，但 provider 的结果分数、套餐命名和账期语义都不能成为
商业事实。Tavily 的 [Search API 文档](https://docs.tavily.com/documentation/api-reference/endpoint/search)
明确支持 `basic`、禁用答案/raw content/自动参数等请求字段，并说明 `basic` 为 1 API Credit；
[Usage API 文档](https://docs.tavily.com/documentation/api-reference/endpoint/usage) 仅返回 key/account
的 usage、limit、current_plan 与 paygo 字段，未给可信周期 ID。其示例的 Bootstrap 不是免费套餐。
官方 [API credits 页面](https://docs.tavily.com/documentation/api-credits) 明确 Researcher 为免费，
Project、Bootstrap、Startup、Growth 为付费。

若把 provider score 存作置信度、根据 `1000` 等数值猜套餐、把缺失字段填零，或让连接器绕过
额度门禁，都会把不确定的可收费调用当成免费调用，并破坏事实/推断分离。

## 决策

1. `connectors/search_contracts.py` 定义供应商无关的 `SearchResult`、`SearchCapabilities`、
   `SearchUsage`、`SearchProvider` 和 `SearchUsageReader`。`SearchResult` 只定位页面，不包含
   provider score；`WebSearchResult` 保持旧导入和三参数构造兼容。
2. Tavily transport 固定为 `https://api.tavily.com/search` 与 `/usage`，不接受调用方 URL、headers、
   credential 或可选收费参数。搜索请求恒为 `basic`，禁用自动参数、答案、raw content、图像与
   inline usage，设置响应体上限和有限 timeout。
3. Tavily 当前不宣称可可靠地把本系统两位国家码映射到 provider country 语义。因此 connector
   保留传入的 `country` 参数和老板原始 query，但不发送 country boost；能力声明为不支持，不能把
   偏好伪装成搜索证据。
4. `/usage` 只读取官方 account 的 `current_plan`、`plan_usage`、`plan_limit`、
   `paygo_usage`、`paygo_limit`。缺失字段保留 `None`，布尔值不能作为整数，且不创建周期 ID。
   仅服务端精确返回 `Researcher` 且明确返回两个 paygo 值均为零时判为免费；Project、Bootstrap、
   Startup、Growth 或明确开启 paygo 判为付费，其他计划名或任意未知字段均为未知。
5. 本任务不向生产 Tool Gateway 注册 Tavily 搜索工具。后续任务必须把 `UNKNOWN` 作为不可免费、
   以持久 reservation 合并 `/usage` 快照后才允许调用；不得回退到 Brave 以规避检查。

## 放弃的选项

- **按固定额度（如 1,000）识别免费。** 数额不是套餐身份，未来套餐调整会静默放宽门禁。
- **接受 Free/Basic/Starter 等猜测别名。** 官方 wire schema 没有免费套餐枚举，模糊匹配会误判。
- **把 Bootstrap 当免费。** 官方 usage 示例使用 Bootstrap，credits 页面将其列为付费。
- **在本任务注册直连 Gateway 工具。** 会在持久额度和 fail-closed 决策实现前形成生产绕过路径。
- **使用 provider score 作为置信度。** 它是检索排序信号，不是可审计的商业证据。

## 后果与复审条件

Tavily 账户未配置、usage 缺失、计划名变化或按量状态不完整时，系统后续必须停止而非猜测；这会
降低可用性，但避免意外收费。真实 API 调用不属于本 ADR 验收范围。若 Tavily 发布稳定的套餐
wire enum、显式 paygo enabled 字段或可信账期 ID，应先核实官方文档并新增 ADR，再扩展解码。
