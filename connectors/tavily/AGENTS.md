# connectors/tavily/ —— Tavily 固定免费搜索能力

## 职责

只将 Tavily 的公开网页搜索与账户用量接口转换为供应商无关契约。它不决定何时搜索、是否有免费额度，也不写任何业务表；这些由后续 Tool Gateway 与持久额度模块负责。

## 硬边界

1. `TAVILY_API_KEY_REF` 仅在本 connector 通过 resolver 解析；密钥不得出现在参数、返回值、日志、错误或 `repr`。
2. transport 只能访问 `api.tavily.com` 的固定 `/search`、`/usage`，搜索固定为 `basic`，禁止答案、raw content、图片、自动参数与任何可选付费能力。
3. provider `score` 不是证据等级或置信度，必须丢弃。搜索结果只是定位，页面事实仍须走 `web_search` 的 SSRF 校验、不可变快照与哈希。标题和摘要只可合并空白并做有界展示截断；非空白控制字符须先拒绝，URL 不得截断或跳过校验，不得把展示摘要当作原件证据。
4. `/usage` 缺失、类型异常、未知计划、未知按量状态不得回填为免费；不得虚构计费周期或根据数值倒推套餐。按 [ADR 0076](../../docs/adr/0076-included-free-search-credits.md)，仅精确 `Researcher`、完整有效套餐计数、`paygo_usage` 严格整数零且 `paygo_limit` 字段存在并为 null 或整数零，才单独标记 `included_credits_free`。原 `UNKNOWN`/`None` 保留；实际免费余额仍由持久额度门禁预留，耗尽不允许转付费。
5. Tavily 未在此任务注册任何可由生产路径调用的工具；Task 2 才接入额度门禁和 Gateway。
