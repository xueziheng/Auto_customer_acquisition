# Hunter Contact Provider Design（联系人补全与邮箱验证）

> 输入：根/connector/tool-gateway/workflow/prospecting 的 `AGENTS.md`、
> `HANDBOOK.md`、`docs/architecture/04-tool-gateway.md`、既有 Prospecting 持久化规格，
> 以及 Hunter API v2 官方文档（核对日期：2026-08-20）。
>
> 本规格只完成 Phase 1 的单 Provider connector 与 Tool Gateway 插件点；
> `account_discovery` 状态机、Campaign 入组和 Demand Radar 接线是后续独立切片。

## 1. 决策与已核实事实

Phase 1 采用 **Hunter** 作为唯一联系人数据 Provider。Hunter 的同一 API v2 同时提供：

- Domain Search：按企业域名返回公开发现的邮箱、姓名、职位和来源；
- Email Verifier：返回 `valid / invalid / accept_all / webmail / disposable / unknown`；
- `X-API-KEY` header 鉴权，因此生产实现不把密钥放进 query string；
- Domain Search 每个邮箱最多返回 20 条来源，且有明确分页与速率限制；
- Verifier 的 `202` 表示仍在处理，同一验证轮询只计一次；`222` 表示远端 SMTP
  不确定失败；`451 claimed_email` 表示数据主体已要求停止处理。

官方参考：

- <https://hunter.io/api-documentation/v2#domain-search>
- <https://hunter.io/api-documentation/v2#email-verifier>
- <https://hunter.io/api-documentation/v2#authentication>

Hunter 返回的 `confidence` 和 `score` 是 Provider 自己的估计，不是 TradeOS 校准后的
测量值。实现必须在解析边界丢弃它们，公共 DTO、日志、数据库和 outbox 均不得出现这些
数字；这落实全局硬边界 3，而不是把 Provider 分数换个字段名保存。

Hunter 当前计费规则可能随套餐变化。代码只记录稳定的 endpoint/outcome 成本说明和
manifest `CostClass`，不把网页价格硬编码为可计算金额。未来若要核算真实货币成本，必须
从结算/usage 数据取得快照并使用 `Money`/`Decimal`；现有 `enrichment_cost_note` 仍只是
不可计算的来源备注。

## 2. 本切片范围与非目标

本切片交付：

1. 一个 provider-specific Hunter HTTP transport，统一鉴权、超时、响应大小和错误分类；
2. `contact_enrichment` 与 `email_verification` 两个 provider-neutral typed 契约及 Hunter
   适配器；
3. `contact.enrich` 与 `contact.verify` 两个 manifest、handler 和 trusted adapter；
4. 容量一、领取即删除的 typed 结果槽，确保 PII 不进入工具账本或模型；
5. Prospecting 的验证检查时间与验证成本备注，使 30 天缓存对全部结果可执行；
6. 连接器、handler、Gateway 集成、脱敏、故障与取消路径测试；
7. 文档与 composition 挂载点，启动时缺密钥或依赖配置固定失败。

本切片不做：第二家 Provider、多源瀑布、真实账号联调、自动购买额度、自动发现企业、
联系人持久化 workflow、Campaign 自动入组、UI、后台付费调用重试或成本钱包。

测试只使用注入的 fake HTTP transport 与官方的固定示例形状，不读取真实 API Key、
不访问 Hunter 网络、不产生费用。可选的人工 smoke test 不进入 CI，也不作为完成证据。

## 3. 组件与依赖方向

```text
trusted account-discovery workflow（后续切片）
        │ hypothesis_id / account_id / contact_point_id + role_hints
        ▼
ToolGatewayContactEnricher / ToolGatewayContactVerifier
        │ ToolCallContext：安全 ID；PII 仅活在 prepare payload
        ▼
Tool Gateway 通用检查管线
        │ manifest + handler，不修改 pipeline.py
        ▼
ContactEnrichHandler / ContactVerifyHandler
        │
        ├─ Demand/Prospecting/Organization 公共 View：绑定假设、企业与联系方式
        ├─ typed 容量一 slot：交接含 PII 结果
        └─ Hunter adapters
                ▼
          HunterTransport
                ▼
          https://api.hunter.io/v2
```

依赖保持：

- `tool_gateway` 只导入 `domains/prospecting/service.py` 的显式公共接口与 connector 契约；
- connector 不写数据库、不发布业务事件、不导入 domain repository/model；
- agent/model 不直接 import connector，也不能读取 slot；
- 新工具只增加 manifest + handler + composition，不按 tool id 修改通用 pipeline；
- 两个适配器共用一个 `HUNTER_API_KEY_REF`，不复制或派生第二份凭证。

既有 `CONTACT_ENRICH_API_KEY_REF` 和 `EMAIL_VERIFY_API_KEY_REF` 骨架名称改为
`HUNTER_API_KEY_REF`。这是单 Provider 的真实凭证归属，不保留两个看似独立 Provider 的
平行配置。Secret Resolver 只在 Gateway 判权通过后由 provider reader 配置 connector。

## 4. Provider-neutral typed 契约

公共 connector DTO 不包含原始 JSON，也不包含 Provider 概率：

```text
ContactSource(uri, first_seen_on, last_seen_on, still_on_page)
ContactCandidate(email, full_name, role_title, email_kind, sources)
ContactEnrichmentResult(candidates, provider, cost_note)

EmailVerificationResult(
  outcome, provider, checked_at, cost_note, privacy_claimed
)
```

约束：

- tuple/frozen DTO；输入构造时复制，禁止把可变 Provider dict 泄到上层；
- email、姓名、职位、source URI 都是 PII 或证据内容，DTO `repr=False`；
- `provider` 固定为 `hunter`；Hunter 两个 endpoint 没有可复用的单次结果 ID，不伪造
  Provider 业务引用；Gateway 自己生成的 slot handle 只用于同一调用栈交接；
- `cost_note` 是固定枚举化 label，不接受 Provider 任意文本；
- source 日期解析为 `date`，未知或畸形来源整条丢弃，不猜测日期；
- 无合法来源的具名邮箱候选丢弃，因为无法组装 LegalBasis/Provenance；
- generic role mailbox 也必须至少有一条合法来源；它仍是联系方式，不因 generic 而免除
  来源记录；
- Hunter response 只读取白名单字段，未知字段忽略；缺少 `data`、类型错误、超出响应上限
  都固定失败，不返回部分猜测结果。

`EmailVerificationOutcome` 确定性映射：

| Hunter status / response | TradeOS outcome | 可进入发送序列 |
|---|---|---|
| `valid` | `verified` | 是 |
| `invalid` | `invalid` | 否 |
| `accept_all` | `risky` | 否 |
| `webmail` | `risky` | 否 |
| `disposable` | `risky` | 否 |
| `unknown` | `unverified` | 否 |
| HTTP `222` 或轮询耗尽 | `unverified` | 否 |
| HTTP `451 claimed_email` | `unverified` + `privacy_claimed=true` | 否，并触发后续删除/抑制 |

`privacy_claimed` 不是普通验证状态。后续 workflow 必须调用 Prospecting 公共删除接口，
保留不可逆 hash suppression；本切片只把这个 typed 事实交给 trusted caller，不允许
handler 绕过域服务直接删表。

## 5. 输入最小化与联系人选择

### 5.1 `contact.enrich`

调用方只传 `hypothesis_id`、`account_id` 与至多 10 个 `role_hints`，不传任意企业对象。
playbook stage 通过 Demand 与 Prospecting 公共服务读取同租户假设/企业，验证
`hypothesis.account_id == account_id`，再用 Organization Playbook 检查真实 category 和
企业 country；country-policy stage 读取同一 invocation preflight。调用方不能靠伪造
category/country 绕过检查。无域名、假设不绑定该企业或政策包缺失都固定拒绝，Hunter
调用为 0。

本 Provider 切片实现上述 stage 所需的 typed preflight 与注入 Protocol，但不猜测任何国家
法律默认值。生产 composition 只有在真实 PlaybookCheck 和 CountryPolicyCheck 数据源均
配置后才注册 `contact.enrich`；否则启动时把能力明确标成未配置。国家政策包的持久化与
管理 UI 是后续独立切片，在它完成前不能宣称生产 enrichment 已启用。

Domain Search 固定 `limit=10, offset=0`，不自动翻页。结果按以下确定性规则处理：

1. 只保留邮箱域与查询企业域一致或属于 Hunter 明确返回的 `linked_domains`；
2. `role_hints` 非空时，对规范化后的 position/department/seniority 做 token 包含匹配；
3. 先保留 personal，再保留 generic；同类按 canonical email 排序；
4. 最多返回 5 个候选，防止一次调用扩大个人数据处理范围；
5. 不使用 `confidence`、`decision_maker` 或模型排序。

`role_hints` 只是确定性筛选条件，不被翻译成 Hunter 的推测性 confidence。无匹配返回空
结果是正常成功；workflow 可以调整策略后发起一次新的、可审计调用。

### 5.2 `contact.verify`

调用方只传 `contact_point_id`。handler 通过 Prospecting 公共读取接口取得同租户、kind 为
email、尚未删除抑制的 canonical value，以及既有 verification status/provider/checked_at；
邮箱不出现在 ToolCallContext 的公开参数、audit projection、日志或异常中。

Prospecting 增加 `verification_checked_at` 与 `verification_cost_note`。所有四种验证结果都
在域服务同一事务保存 UTC checked time、provider 和固定 cost label；只有 `verified` 额外
设置 `verified_at` 并按既有规则发布一次事件。缓存规则是
`now < verification_checked_at + 30 days`；命中时 handler 通过 typed slot 返回当前域状态和
`hunter.email_verifier.cache_hit`，HTTP 调用为 0。精确到期时视为过期。该规则覆盖
invalid/risky/unverified，避免只缓存 valid 后仍反复为失败地址付费。

Verifier 首次响应 `202` 时由同一次 connector 调用做有界轮询：最多 2 次后续请求，使用
注入 sleeper 和固定总时限 30 秒；官方说明该轮询只计一次。仍未完成时返回
`unverified`，上层不得自动开启新的调用。任何原始 Retry-After 只在合法有界整数时采用，
否则使用固定退避；测试使用零等待 sleeper。

## 6. Tool Gateway manifest 与 PII 交接

两个工具均为外部付费只读处理，设为 `RiskLevel.MEDIUM`，无需商业承诺审批：

```text
contact.enrich:
  permissions = contact:enrich
  checks = tenant → permission → playbook → country_policy → suppression → rate_limit
  idempotency = NONE
  cost_class = MEDIUM

contact.verify:
  permissions = contact:verify
  checks = tenant → permission → suppression → rate_limit
  idempotency = NONE
  cost_class = LOW
```

不启用调用者幂等的原因不是忽略重复扣费，而是 durable ledger 只能复用安全
`provider_ref`，不能复用已被领取并删除的 PII 结果。若使用 REQUIRED，重复命中会返回一个
已失效 handle，或者迫使系统持久化 PII；两者都错误。

`playbook` stage 负责建立 tenant-bound discovery preflight；`country_policy` 只消费该
preflight 并默认拒绝未知国家。`suppression` 通过 Outreach 公共服务检查 account/contact
point 当前抑制事实，命中即停止，避免为明确不可触达对象继续处理 PII 和付费。
`rate_limit` 使用 Provider 配额策略，不复用 Gmail
SendingIdentity 的 rate-limit 实现；composition 以策略表装配同名通用 stage，不在
`pipeline.py` 按 tool id 分支。

每次调用仍由既有 Gateway technical claim 提交
`RECEIVED → EXECUTING → SUCCEEDED/FAILED` 证据。handler 仅把 typed 结果放入各自容量一的
进程内 slot，ledger output 只保存随机 `ceb_`/`veb_` handle。slot 用 `ContextVar` 做
async-task-local 隔离，因此每个 invocation 容量一，但并发调用不会争用进程级单例；不使用
按 handle 无界增长的全局 dict。trusted adapter 在同一 async 调用栈 `take()`，读取一次即
删除；拒绝、异常、ledger completion 失败或 cancellation 都 `discard_all()`。

Artifact Store 不用于暂存 Provider JSON：它是不可变原始证据库，公共接口没有业务删除，
且当前 raw kinds 不包含联系人 API 响应。把邮箱响应写进去会让数据主体删除请求无法完成。

模型只能看到调用状态、固定失败分类和安全 ID，不能取得：

```text
email / full_name / role_title / source URI / Hunter raw JSON /
API key / Authorization header / X-API-KEY / request URL query /
confidence / score / provider error body
```

## 7. HTTP、安全与错误语义

`HunterTransport` 使用注入的窄 HTTP Protocol；生产实现必须：

- base URL 固定为 `https://api.hunter.io/v2`，禁止调用方覆盖 host 或传绝对 URL；
- API Key 只放 `X-API-KEY` header，request repr 与异常均不得包含 headers；
- connect/read/total timeout 有界；response body 有最大字节数；只接受 JSON object；
- 不记录完整 URL、query、response body或 Provider 原始异常；
- API key、邮箱与响应 DTO 字段全部 `repr=False`；
- health check 使用不含 PII 的账户/usage 能力或本地配置检查，不用真实邮箱探测。

错误分类：

| HTTP/情况 | 对外分类 | 自动再次付费调用 |
|---|---|---|
| 参数校验在本地失败 | validation | 否，且 HTTP 0 次 |
| 400/404/422 | provider_permanent | 否 |
| 401 | provider_auth_required | 否，人工恢复密钥 |
| 403/429 | rate_limited | 否；只给人工决定，保留合法 Retry-After |
| Verifier 451 `claimed_email` | typed privacy claim | 否，进入删除/抑制 |
| Domain Search 451 | provider_permanent policy refusal | 否；没有主体标识时不猜测删除目标 |
| 5xx、网络/超时且明确请求未发出 | provider_transient | 不由 workflow 自动重试 |
| 请求可能已到 Provider、响应丢失 | reconciliation_required | 否；人工判断，不能猜测是否扣费 |
| schema/响应过大/非 JSON | provider_permanent | 否，固定脱敏错误 |

Hunter Domain Search 和 Verifier 没有可供 TradeOS 查询单次历史结果的稳定业务引用，因此
不伪造 reconcile 能力。任何不确定结果都 fail closed；Phase 1 接受人工重试的操作成本，
换取不重复处理个人数据和不静默重复扣费。

## 8. 成本与审计

Gateway ledger 继续只持久化 manifest `cost_class`；成功 `ToolCallResult.cost_note` 仍是
该成本等级，不修改通用 pipeline，也不新增浮点列。endpoint/outcome 的逐次成本说明只在
typed slot 中交给受信 workflow，并最终写入 Prospecting/执行指标。typed 结果的
`cost_note` 只允许：

```text
hunter.domain_search.counted
hunter.domain_search.no_result
hunter.email_verifier.counted
hunter.email_verifier.privacy_refused
hunter.email_verifier.unknown
hunter.email_verifier.cache_hit
```

这些 label 说明“哪类调用发生了”，不是金额。联系人持久化时，后续 workflow 把 enrichment
label 原样写入 `enrichment_cost_note`，把 verification label 写入
`verification_cost_note`；cache-hit 明确表示本次外部成本为零。
不得根据官网当前套餐价在业务逻辑中计算货币成本，也不得把 `0.5` 之类 Provider 价格当作
稳定事实写进联系人记录。

审计投影只包含安全字段：tool/account/contact-point ID、候选数量、是否有结果、固定
provider label、耗时与固定 category。候选数量由确定性代码产生，不能被解释为置信度。

## 9. 测试与完成门槛

### 9.1 Connector contract tests

- configure 前调用失败；密钥只从 resolver 读取一次且不进入 repr/异常；
- `X-API-KEY` 使用、固定 host/path、参数编码、timeout/response size 上限；
- Domain Search 空结果、personal/generic、linked domain、role filter、5 条上限和稳定排序；
- 缺来源、非法 URI/日期、邮箱域不符、畸形/超大 JSON fail closed；
- 明确断言公共 DTO/object graph 不含 `confidence` 或 `score`；
- 六种验证状态、202 有界轮询、222、451、401/403/429/5xx 与网络确定性分类；
- 任意错误、日志与 repr 扫描不含 API Key、邮箱或 Provider body canary。

### 9.2 Handler/Gateway tests

- 检查拒绝时 connector 调用 0 次；凭证解析发生在判权之后；
- `EXECUTING` ledger/event 提交失败时 connector 调用 0 次；
- 成功 ledger 只含 `ceb_`/`veb_` handle 和固定 label，不含 PII；
- slot 容量一、take-once、错误/cancellation/ledger completion 失败均清空；
- 同一 asyncio task 容量一，不同 task 并发结果互不覆盖或串租户；
- 跨租户 account/contact-point 不可见且表现与不存在相同；
- verify 非 email、已删除/不存在 contact point 固定拒绝；
- 四种状态的 30 天缓存、精确到期、provider/checked_at/cost note 原子持久化；
- 同一请求并发调用各自有独立 invocation scope，不共享全局 slot；
- Gateway 不具备调用者 duplicate 复用语义，且 workflow 不自动重试不确定付费请求；
- 迁移后数据库敏感 canary 扫描证明 tool ledger/outbox 无邮箱、source URI 或 raw JSON。

### 9.3 仓库门禁

每个实现小任务至少运行目标 pytest、ruff、mypy 与：

```bash
python3 scripts/check_boundaries.py
```

完成整个切片前运行无 Docker 的本地全量门禁、API schema/type generation、Web typecheck/
lint/test/build，并推送等待精确 HEAD GitHub Actions 全绿。真实 Provider 网络调用不是 CI
条件；没有生产 API Key、PlaybookCheck 或 CountryPolicyCheck 数据源时，composition 必须
在启动期明确报告该能力未配置，而不是悄悄返回空结果或默认允许未知国家。

## 10. 后续接线条件

本切片完成后，`workflows/account_discovery` 才能实现：

```text
get account → contact.enrich → persist candidate + legal basis
→ contact.verify → record_verification → discard risky/unknown
```

后续 workflow 必须为 legitimate-interest 候选提供已有 `assessment_ref`，逐联系人保存来源
和 enrichment cost note，并在 `privacy_claimed` 时执行删除/抑制。只有 Prospecting 状态已经
转为 `verified` 的 ContactPoint 才能进入 Campaign；Hunter 的 `valid` 响应本身不能绕过域
服务直接入组。
