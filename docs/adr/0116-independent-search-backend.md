# ADR 0116：独立搜索后端与库存任务预算

日期：2026-10-22。状态：接受（真实来源与部署验收另记）。

## 背景

Codex CLI 负责选择公开搜索与页面工具，但模型生成的链接不是搜索结果。原库存及客户准备装配把 Tavily 搜索端口与公开页面、对象端口绑在一起，导致没有 Tavily 时连已授权页面的重新取证也无法装配。

## 决策

新增 DeepSeek 独立 Search 连接器，通过原 `web.search` handler、结果槽与 Gateway 调用。原六阶段管线不变。Codex 只能传公开条件，确定性组合器保留完整产品及国家锚点，并按后端能力限制128个 Unicode 字符；最多20条结果、`need_content=false`。搜索摘要只定位页面，证据仍由安全页面读取和当前企业原件归档产生。

只接受平台当前绑定的官方 DeepSeek 连接；库存搜索与模型沿同一 InvocationIdentity、run pin、目录版本和惰性 ModelKeyFileResolver。官方根 URL 与 `/v1` 两种现有形式均支持，实际请求固定 `/v1/tools/search`。兼容第三方地址在解析密钥前拒绝。HTTP 不重试、不跟随重定向、不读取环境代理。认证和余额拒绝为永久配置错误；超时和未知结果保守保留预留，不换查询、请求键或供应商重派。

`InventoryRuntimePorts` 只承载页面及对象接口。没有 Tavily 时，独立搜索可以装配，客户自动准备也可以只重读精确授权候选页面。原 Tavily 配置、引用、独占免费额度及全部54来源定义保留。显式配置选择一个后端，没有自动回退。检索后端与网站、协会、目录等证据来源不是同一概念；注册或保存凭证不等于实际采集通过。

平台部署配置 `inventory_search` 独占控制 DeepSeek 付费开关、政策版本、总请求与 Decimal 金额上限；默认无付费许可。独立 Search 每个成功非空请求当前价格人民币0.005元，本地对所有派发保守预留，不因空结果或未知结果退款。上限最多24次、1元；这些是安全边界，不是企业钱包或服务计划。

迁移0092在原 tenant-scoped acquisition_jobs 增加搜索绑定和请求预留。绑定持久保存后端、连接、完整政策及有效累计请求上限。请求受当前租约、来源计划/started记录、24小时截止与任务行锁保护。未知请求阻止同一来源换键派发；跨来源计数共用任务账本。旧已开始而无绑定任务保守阻断。恢复不能改后端、连接或政策。

受信组合可传 `InventorySearchBudget(..., trusted_maximum_requests=...)`，语义是该 tenant+job 的累计总请求上限，非剩余数。有效值取平台政策、已存上限及本次上限的最小值，独立提交后才检查新派发；漏传、增大、低于已用而拒绝请求均不能撤回收紧。后续服务计划可使用该接口，本变更不实现服务计划或企业可调预算。

## 费用依据与实测边界

官方资料核查于2026-10-22：[独立 Search](https://api-docs.deepseek.com/zh-cn/guides/search/)、[Responses](https://api-docs.deepseek.com/zh-cn/guides/responses_api/)、[定价](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[余额](https://api-docs.deepseek.com/zh-cn/api/get-user-balance/)。Responses 原生 web_search 只在 v4-pro 生效，Flash 忽略；max_tool_calls 不能提供本系统逐次授权边界，因此此路径禁用服务端原生搜索。服务端工具免费期截至2026-11-01北京时间零点，不表示独立 Search 或模型 tokens 免费。

`scripts/diagnose_free_sources.py` 是具名 SYSTEM 来源样本诊断，固定工具与公开查询，通过原 Gateway 和真实 HMAC/免费额度服务。一次性本地 journal 在请求前落盘，最多6次供应商 HTTP，最多各一次搜索，零页面、模型、联系人或 Campaign 写入。DeepSeek 只有新鲜 CNY 赠金额足够一次请求，且全部币种均无充值、金额一致有效时才可搜索；混合余额、未知币种和资格不明只报告 metadata/not_run。Tavily 沿原账户独占与免费 quota；TED 为匿名公开通知 API。技术账本与免费 quota 正常累计，不为诊断放宽通用 authorizer。

该诊断只证明来源连通或样本，不证明完整客户获取、需求验证或部署完成。合成 API/worker/数据库闭环、真实来源样本与未运行项须分别记录；不得把合成模型、页面或联系人传输当成真实供应商验收。
