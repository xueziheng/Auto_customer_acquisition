# Tavily 免费账户额度边界

本能力尚未配置或验证真实 Tavily 账户。仅持久化代码与受控 transport 验收通过，不能据此宣称生产已激活或可以持续免费运行。

## 显式装配

生产 scheduler 的 `DemandDiscoveryComposition.web_tools` 使用现有 `WebDiscoveryToolComposition`。
免费模式必须显式传 `provider="tavily"`、`exclusive_account_confirmed=True`、Tavily transport、
部署密钥引用、页面 transport 与 Artifact Store；不允许省略账户独占确认，不回退 Brave。
配置只保存引用，`repr` 不显示引用；真实值只由 Connector 在 Gateway 全部检查之后解析。
旧 Brave 默认组合保留原行为，它不属于免费额度承诺。

`exclusive_account_confirmed` 是受信部署者对账户独占使用的显式声明，不是 `/usage` 核实出来的事实。
Tavily `/usage` 没有可验证的账户 ID，因此实现不伪造 ID，也不提供可由请求指定的账户别名。
同一数据库只有一个全局唯一 `provider=tavily` 账户槽，由首个受信租户持有；其他租户绑定失败关闭且不显示 owner。
更换 key 引用、重启进程、创建新 Run 均沿用同一额度。不能用此能力管理多个免费账户。
不同数据库或其他程序共用真实账户无法由此约束检测；部署者必须独占该账户，并防止绕开 Gateway 的消费或改动 paygo。

## 使用与停止

Gateway 仍强制租户、权限、Playbook、国家政策、每 Run 已确认 query/page budget、Tool ledger 和页面来源检查。
没有确认预算时不解析凭证、不读取 usage、不搜索；准备参数或任何检查拒绝不会消耗账户预留。
免费搜索注册 `web.search/v1.free`，ledger 成本类别为 `free`；联系人、邮件或其他商业工具不会因此注册。

每次 dispatch 前必须得到精确 `Researcher` 免费套餐、明确关闭 paygo 和有效 limit/used；缺失字段不是关闭。
`usage_remaining = max(0, limit-used)`；持久上限取历史有效快照余量最小值，安全剩余量为上限减累计本地预留（下限零）。
全部本地预留包括 reserved、uncertain、consumed；旧快照不能增加额度。
该下界可能重复扣除已计入 Provider 用量的消费，因而比实际剩余额度更少。
外部账户设置或用量在读取后改变仍是无法原子控制的风险，所以独占声明不可省略。

预留先提交 `reserved`，dispatch 前再提交 `uncertain`，成功后才提交 `consumed`。
timeout、429、未知故障、取消、结果持久化失败均不退款、不自动重试。
同一 Run 有 reserved/uncertain 时拒绝后续任何搜索；相同 Run/请求 HMAC 的 consumed 也不能再次 dispatch。
其他 Run 可使用剩余额度。每个 Run 首次执行时持久绑定 HMAC 指纹版本；
后续在凭证/usage 前及账户锁内预留时都核验该版本，不匹配或旧记录为 NULL 则固定停止为 `request_uncertain`，不覆盖原版本。
因此即使 consumed 后、结果交付前进程退出，重启并轮换 HMAC 也不能重放该 Run 的请求。
新 Run 可以使用新版本；正常密钥轮换仍必须使用新的非秘密版本号，不允许换密钥却复用版本号。

没有可靠账期，不按本机月份、Provider used 下降或额度增加清除预留。
本批不提供人工对账/解除预留/补充额度入口，也不提供自动月度补充；不要通过清表、重建绑定或更换数据库绕过保护。

## 下游安全状态

`SearchQuotaRepository.snapshot()` 返回 tenant-scoped 账户安全摘要；`run_state(run_id)` 返回固定停止原因。
`run_state` 优先从 reserved/uncertain 推导 `request_uncertain`，因此在记录停止原因前崩溃也不误报无结果。
公开契约为 `FreeSearchStopReason`：`quota_exhausted`、`usage_unknown`、`paid_enabled`、
`request_uncertain`、`unsupported`（供不支持的能力分支使用）。
免费 Gateway adapter 返回 `FreeSearchError.reason` 且 `is_retryable=False`；状态查询失败也按不确定停止。
插件内的错误先映射为 Gateway 合法技术分类，避免非重试业务标志破坏 ledger 的终态校验；
外层 adapter 再从持久 Run 原因恢复非自动重试的业务错误。ledger 的 `failed_transient/reconciliation_required` 不构成重新搜索授权。
Gateway core 不认识这些业务原因，仍保留原安全技术分类。任何数据库读取失败不能解释为 `no_results`。
接口不返回原始响应、查询、凭证引用、密钥、HMAC key 或真实账户身份。

Task 3/4 仍负责业务 workflow、API/UI 的停止原因呈现；这里不替代业务验收，也不解除联系人依赖。
