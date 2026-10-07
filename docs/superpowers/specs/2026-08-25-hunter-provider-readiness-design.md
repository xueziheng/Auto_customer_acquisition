# Hunter Provider 生产组合与就绪设计

> 日期：2026-08-25
> 输入：`AGENTS.md`、`HANDBOOK.md`、`GLOSSARY.md`、
> `docs/architecture/04-tool-gateway.md`、`08-compliance.md`、
> `docs/superpowers/specs/2026-08-20-hunter-contact-provider-design.md`、
> `docs/superpowers/specs/2026-08-24-country-policy-readiness-design.md`
> 范围：Phase 1 Hunter 安全配置声明、Provider 运维验证、生产工具注册与 Settings 真实就绪状态

## 1. 背景与当前事实

Hunter connector、固定主机 transport、`contact.enrich` / `contact.verify` manifest 与 handler、
一次性 typed 结果槽、账户发现 workflow、Company Playbook 和持久化国家政策 reader 已实现。
现有受控测试证明未知国家、明确禁止、读取故障和无效政策会在 Provider IO 前失败关闭。

生产仍没有可用的联系人补全组合。`apps/scheduler_worker/hunter_contacts.py` 只注册
`contact.verify`；返回给账户发现流程的 `contact.enrich` adapter 固定调用未注册工具。
Settings 的 `contact_enrichment_composed` 则是 API composition 内的硬编码布尔值，无法证明
scheduler 是否为同一租户、同一 Provider 配置注册了工具。

仅把两个进程改为读取同一个环境变量不能解决问题：配置可能漂移、没有历史、无法绑定一次
真实 Provider 验证，也不能回答“页面为什么显示 ready”。直接注册未验证的工具同样错误，
因为后台流程可能在运维验证前接触凭证、处理联系人数据或产生 Provider 调用。

## 2. 已确认的范围

### 2.1 目标

1. 保存 tenant-scoped、append-only 的 Provider 配置、验证与运行组合事实。
2. 将验证绑定到精确的安全配置版本；密钥轮换或配置变更自动使旧验证失效。
3. 通过 Tool Gateway 执行不含 PII 的 Hunter `/account` 人工验证。
4. 只有当前配置验证通过时，scheduler 才注册生产 `contact.enrich` 与 `contact.verify`。
5. API 与 scheduler 读取同一个持久事实源，Settings 不再依赖本地硬编码布尔值。
6. 对未配置、未验证、验证失败、不确定结果和运行时未组合给出不同的结构化原因。
7. 提供完全离线的受控 transport 验收和真实验证 runbook。

### 2.2 本切片明确不做

- 不读取、创建或提交真实 Hunter API Key。
- 不在实现与自动化验收中访问 Hunter 网络。
- 不写入任何真实国家法律结论，也不自动激活国家政策。
- 不自动运行 Provider 验证，不自动重试验证失败或不确定结果。
- 不建设 Connector 密钥编辑 UI、Vault 管理、多 Provider 路由或配额钱包。
- 不用 Hunter `score`、`confidence` 或套餐价格生成业务判断。
- 不因代码已具备组合能力而宣称 `contact.enrich` 已上线或 Phase 1 已完成。

本切片交付后，未配置部署仍应显示 `provider_not_configured`。只有运维人员显式声明安全配置
版本后才显示 `validation_pending`。测试会覆盖 pending 状态，但不能伪造真实部署配置。

## 3. 方案选择

### 3.1 采用：Tool Gateway 持有持久运行就绪事实

Provider readiness 是外部工具能否安全运行的操作事实，不是国家法律判断，也不是 Company
Playbook 的经营偏好。因此它由 `tool_gateway` 定义契约和状态推导，由 `infra/db` 实现持久化，
应用 composition root 注入。`domains/compliance` 继续只回答某个国家、某项动作是否允许。

API 与 scheduler 都消费同一个 tenant-scoped reader：

```text
人工配置命令声明安全配置
      ↓
ProviderReadinessService / Repository
      ↓
append-only configured event
      ↓
人工调用 provider.hunter.validate
      ↓
validation_started → validation_passed / validation_failed
      ↓
scheduler 重启读取当前精确配置
      ↓
注册两个 Hunter manifest → runtime_composed
      ↓
Settings 读取同一事实并推导状态
```

这不新增业务域，也不允许 `tool_gateway` 读取合规仓储。Gateway 的联系人调用仍通过现有
`CountryPolicyDecisionReader` 逐次读取当前国家政策。

### 3.2 淘汰的方案

**API 与 scheduler 共用布尔环境变量。** 改动小，但两个进程可能使用不同环境，且没有配置
版本、验证证据或运行组合事实。页面可以在工具未注册时误报 ready。

**未验证即注册生产工具。** Settings 可以显示“待验证”，但账户发现工作流已经可以调用
Hunter，违反本切片不接触真实网络的范围，也把运维验证变成事后动作。

## 4. 配置契约

scheduler 配置增加一组全有或全无的字段：

```text
TRADEOS_HUNTER_CONTACTS_ENABLED
TRADEOS_HUNTER_CONFIGURATION_VERSION
TRADEOS_HUNTER_API_KEY_SECRET_REF
TRADEOS_HUNTER_API_KEY_VERSION
```

规则如下：

1. `TRADEOS_HUNTER_CONTACTS_ENABLED` 只接受小写 `true` 或 `false`，不猜默认值。
2. 为 `false` 时，其他三个字段必须不存在或为空；不读取凭证、不声明配置、不注册工具。
3. 为 `true` 时，其他三个字段全部必填，任一缺失都使 scheduler 启动失败关闭。
4. `TRADEOS_HUNTER_API_KEY_SECRET_REF` 只保存密钥服务中的引用名，必须匹配已有安全引用格式；
   它不是密钥值。
5. `TRADEOS_HUNTER_API_KEY_VERSION` 是运维提供的非秘密轮换版本。密钥改变而版本不变属于
   运维错误，runbook 必须明确禁止。
6. `TRADEOS_HUNTER_CONFIGURATION_VERSION` 是显式配置世代。相同版本配不同内容固定冲突；
   回滚旧配置必须创建新的配置版本，不能复用旧验证。

运维人员通过 `scripts/configure_hunter_provider.py` 显式声明配置。该命令只读取上述安全
字段，不解析密钥值、不访问网络；它需要 `provider:configure` 权限并留下 actor 事实。
scheduler 启动时重新计算环境中的安全配置哈希，必须与当前 durable configured 事实精确
一致；enabled 但没有 matching fact 时启动失败关闭。这样未取得单副本锁的候选 worker
不会抢写“当前配置”。

配置内容哈希由确定性代码根据以下安全字段生成：

```text
provider = hunter
capabilities = contact.enrich, contact.verify
connector_profile_version
transport_profile = hunter_api_v2_fixed_host
configuration_version
api_key_version
```

哈希输入不含密钥值、密钥哈希、环境变量值、host 覆盖、URL query 或 HTTP header。
Provider host 继续由 `HunterApiHttpTransport` 固定，配置不能覆盖。

## 5. 持久化事件与当前状态

新增单表 `provider_readiness_events`。每行是不可变事实，至少包含：

```text
tenant_id
provider_readiness_event_id
provider                  # Phase 1 仅 hunter
capability_set            # 固定规范化集合
sequence
event_type
configuration_version
configuration_hash
connector_profile_version
transport_profile
api_key_version
validation_key            # 仅验证事件可有；与 Gateway canonical 幂等键一致
outcome_code              # 固定枚举，不保存异常文本
evidence_ref              # 可选安全引用
actor_id
occurred_at
idempotency_key
```

所有唯一约束和索引包含 `tenant_id`。`tenant_id + provider + capability_set` 使用 PostgreSQL
transaction advisory lock 分配单调 `sequence`。数据库 trigger 拒绝 UPDATE/DELETE；Repository
所有查询显式包含租户谓词。

事件类型固定为：

| 事件 | 语义 |
|---|---|
| `configured` | 人工运维命令声明新的安全配置版本 |
| `validation_started` | 已提交持久意图，即将访问 Provider |
| `validation_passed` | 精确配置通过人工 Provider 验证 |
| `validation_failed` | 得到确定的安全失败分类 |
| `runtime_composed` | scheduler 已为精确配置注册并核对两个 manifest |

`validation_started` 必须先于外部 IO 持久化。如果请求可能已到 Provider，但没有可靠的
`passed` 或 `failed` 结果，started 事实保留并推导为 `validation_inconclusive`，禁止自动重试。

同一配置版本与幂等键重复提交且内容相同是 no-op；内容不同固定冲突。出现新的
`configured` 事件后，旧配置的 validation/runtime 事件全部失效。`runtime_composed` 必须引用
当前配置和已存在的 `validation_passed`，否则 Repository 拒绝写入。

## 6. 状态推导

`ProviderReadinessSnapshot` 由确定性代码从当前事件流推导，不由模型或调用方提交状态：

| 当前事实 | Provider 状态 |
|---|---|
| 没有 `configured` | `provider_not_configured` |
| 当前配置没有 validation | `validation_not_run` |
| 最新验证只有 started | `validation_inconclusive` |
| 当前配置最新确定结果为 failed | `validation_failed` |
| 当前配置 passed、无 matching runtime | `runtime_not_composed` |
| 当前配置 passed 且有 matching runtime | `ready` |

Settings 的联系人补全状态继续先检查国家政策覆盖，再检查 Provider 状态：

```text
COUNTRY_POLICY_NOT_CONFIGURED
CONTACT_ENRICHMENT_NOT_ALLOWED
CONTACT_ENRICHMENT_PROVIDER_NOT_CONFIGURED
CONTACT_ENRICHMENT_PROVIDER_VALIDATION_PENDING
CONTACT_ENRICHMENT_PROVIDER_VALIDATION_FAILED
CONTACT_ENRICHMENT_PROVIDER_VALIDATION_INCONCLUSIVE
CONTACT_ENRICHMENT_RUNTIME_NOT_COMPOSED
ready
```

现有 `CONTACT_ENRICHMENT_NOT_COMPOSED` 被更精确的 Provider 原因替代。API schema、生成的
TypeScript 类型、前端映射和测试必须同步更新。`ready` 表示当前配置已验证且 scheduler 曾为
同一配置完成注册；进程实时存活仍由 scheduler health/readiness 监控，不混入业务 Settings。

## 7. 人工 Provider 验证工具

增加 Tool Gateway 插件 `provider.hunter.validate`，它只访问 Hunter 固定 `/account` endpoint，
不接受邮箱、姓名、企业、国家或任意 URL。它不经过联系人国家政策，因为没有处理联系人
数据；它也不能用于绕过后续每次 `contact.enrich` 的国家政策检查。

2026-08-25 核对 Hunter 官方 API v2 文档：Account Information endpoint 仍为
`GET https://api.hunter.io/v2/account`，通用鉴权允许 `X-API-KEY` header。官方页面当前称该
调用免费，但套餐规则属于外部可变事实，本系统仍不得把“免费”写进成本逻辑。

manifest 固定为：

```text
risk_level = MEDIUM
cost_class = LOW
permissions = provider:validate
idempotency = REQUIRED
checks = tenant → permission → idempotency → rate_limit
```

不把 `/account` 硬编码为免费。代码只记录稳定的 endpoint/outcome 成本标签，不记录金额或
套餐推断。验证只能由显式人工运维命令触发，不由 scheduler、Agent 或模型自动运行。

handler 的顺序为：

1. 读取当前 tenant-scoped 配置快照并核对请求中的配置版本。
2. Tool Gateway 完成全部检查、canonical claim，并提交 `EXECUTING` 证据。
3. handler 以 canonical 幂等键作为 `validation_key`，追加 `validation_started` 并提交。
4. 在 connector 内解析精确密钥引用，使用固定 Hunter transport 调用 `/account`。
5. 只检查认证成功和响应满足 bounded typed schema；丢弃账户明细与原始 JSON。
6. 追加 `validation_passed` 或带固定分类的 `validation_failed`。
7. 返回安全 ID、配置版本与固定状态；不返回 Provider payload。

readiness event 不要求 handler 获得 Gateway 内部生成的 `tool_call_id`。两边保存同一个
canonical `validation_key`，运维审计可按 tenant、tool ID 与该键关联 canonical ledger，
无需修改通用 handler Protocol 或 Tool Gateway pipeline。

固定失败分类至少包括：

```text
auth_required
rate_limited
provider_transient
provider_permanent
response_invalid
reconciliation_required
```

401 映射 `auth_required`；429 保留合法有界 Retry-After 供人工判断；5xx/明确未发出的网络失败
映射 transient；请求可能到达 Provider、响应丢失或结果提交不确定时保留 started 并显示
inconclusive。任何路径都不得自动再次调用 `/account`。

## 8. scheduler 生产组合

`build_hunter_contact_tools` 不再构造“一半注册”的 registry。它接收当前
`ProviderReadinessSnapshot`：

- disabled、not configured、not run、failed、inconclusive：两个 Hunter manifest 均不注册，
  返回的 trusted adapters 在 Gateway 边界得到固定未注册错误；凭证解析和 transport 调用为零。
- validation passed：注册 `contact.enrich` 与 `contact.verify`，使用同一个受限
  `_BoundHunterSecretResolver`、固定 transport 和 quota guard。
- registry 必须精确包含两个预期 tool ID，顺序与 manifest profile 必须通过现有校验。
- registry 核对成功只产生进程内候选，不立即写 `runtime_composed`。
- `run_scheduler_worker` 成功取得并复核单副本 advisory lock 后、进入第一个业务 cycle 前，
  调用 typed activation hook 追加 matching `runtime_composed`。这笔事实提交失败则释放锁并
  启动失败，不能一边运行工具一边让 Settings 继续显示未组合。
- 两个 Hunter Provider adapter 在创建 connector、解析凭证前，必须重新读取当前 readiness
  并核对注册时绑定的 `configuration_hash`。出现新 configured 事实、验证失效或 reader 故障
  时，旧 scheduler 即使尚未重启也固定在 Provider IO 前失败关闭。

`contact.enrich` 保持现有六阶段：

```text
tenant → permission → playbook → country_policy → suppression → rate_limit
```

`contact.verify` 保持现有四阶段：

```text
tenant → permission → suppression → rate_limit
```

Provider 验证只证明凭证与固定 transport 可工作，不能替代 Playbook、国家政策、租户、
抑制或可达性门禁。每次补全仍重新读取当前业务事实。

这个调用时 guard 属于 Hunter provider adapter 的运行配置绑定，不新增 Gateway stage，也不
把业务政策写入 connector。它只回答“当前进程绑定的精确 Provider 配置是否仍有效”。

验证成功后需要重启 scheduler。重启前 Settings 显示
`CONTACT_ENRICHMENT_RUNTIME_NOT_COMPOSED`；注册与 durable fact 完成后才显示 ready。

## 9. API 与 Settings

`ConfiguredApiDependencies.contact_enrichment_composed: bool` 替换为窄
`ProviderReadinessReader`。API composition 注入 Postgres 实现，不能从本进程环境猜 scheduler
状态。

Settings 继续同时在 Playbook overview 和国家政策 overview 展示同一联系人补全 readiness，
但文案按原因区分：

- Provider 未配置：部署尚未声明 Hunter 安全配置版本。
- 验证未运行：配置已声明，需要人工执行 Provider 运维验证。
- 验证失败：显示固定分类和安全发生时间，不显示异常文本。
- 验证不确定：禁止自动重试，要求人工检查 Tool Gateway ledger。
- runtime 未组合：验证已通过，需要重启或检查 scheduler 启动。
- ready：当前配置已验证并完成工具注册；仍需具体国家政策与每次调用门禁。

前端不提供密钥输入框、验证结果伪造按钮或直接写 readiness 的 API。人工验证属于部署运维
命令，不属于普通 Settings 用户动作。

## 10. 权限、安全与隐私

- 模型、Agent、API 浏览器、数据库和日志永远不获得 Hunter 密钥。
- Provider 验证命令需要 `provider:validate` 权限和显式 tenant/operator identity。
- 只有 connector 在实际验证或业务调用时解析密钥；配置声明与 Settings 读取不解析。
- API Key 只进入固定 host 请求的 `X-API-KEY` header，不进入 URL、DTO、repr 或异常。
- `/account` 原始响应、账户邮箱、订阅信息、额度细节和 Provider error body全部丢弃。
- readiness event 只保存固定分类、安全版本、ID、时间和 evidence reference。
- `contact.enrich` / `contact.verify` 的 PII、一次性 handle 和 ledger 约束保持不变。
- readiness 查询与写入全部 tenant-scoped；跨租户配置、验证或 runtime fact 固定拒绝。

人工 Provider 验证不是总纲中的商业承诺动作，不新增审批类型；但它必须由真人显式运行并
留下 actor/tool-call 证据。Agent 不得代替运维人员自动触发。

## 11. 故障与恢复

| 故障点 | 结果 | 是否自动访问 Provider |
|---|---|---|
| 配置字段缺失或冲突 | scheduler 启动失败或保持未配置 | 否 |
| configured event 提交失败 | 配置命令失败；scheduler 不接受该配置 | 否 |
| Gateway EXECUTING 提交失败 | 验证调用失败 | 否 |
| validation_started 提交失败 | 验证调用失败 | 否 |
| Hunter 明确认证/限流/永久失败 | append failed 固定分类 | 否 |
| 请求可能到达、响应丢失 | started 保留，inconclusive | 否 |
| Provider 成功、结果事实提交失败 | started 保留，inconclusive | 否 |
| runtime registry 不精确 | scheduler 启动失败 | 否 |
| runtime_composed 提交失败 | 已持有锁的 scheduler 释放锁并启动失败 | 否 |
| 运行中出现新配置或 readiness reader 故障 | 旧 adapter 在解析凭证前失败关闭 | 否 |
| readiness reader 故障 | Settings 503；scheduler 不注册工具 | 否 |

表中的“是否自动访问 Provider”表示故障后的自动重试；全部为否。人工重新验证必须使用新的
显式幂等键，并由操作者根据 ledger 与固定分类决定。

## 12. 迁移与依赖边界

新增一个 Alembic migration，保持单 head。`provider_readiness_events` 包含 tenant-scoped 主键、
唯一序列、配置版本幂等约束和 append-only trigger。

建议文件归属：

```text
tool_gateway/provider_readiness.py        契约、状态推导、service/repository Protocol
tool_gateway/handlers/provider_validation.py
infra/db/provider_readiness.py            SQLAlchemy row 与 repository/UoW 实现
apps/scheduler_worker/config.py           显式全组配置解析
apps/scheduler_worker/hunter_contacts.py  双工具条件注册
apps/scheduler_worker/runtime.py          配置读取、条件组合与 post-lock activation hook
apps/api/dependencies.py                  注入 readiness reader
apps/api/routers/settings.py              真实原因推导
apps/api/composition/runtime.py           Postgres reader composition
apps/web/src/views/settings/SettingsCenter.vue
scripts/configure_hunter_provider.py      安全配置声明入口（不读密钥、不联网）
scripts/validate_hunter_provider.py       人工 Provider 验证入口
docs/operations/hunter-provider-readiness.md
```

不修改 Tool Gateway 核心 pipeline，不新增 stage，不让 connector 写业务表，不让 apps 相互
import。新增验证能力只通过 manifest + handler 插件点接入。若实现需要修改全局 stage 顺序，
视为设计偏离，应停止并重新评审。

Provider readiness ID 在 `tool_gateway` 内定义，不修改 `shared` 公共 ID 契约；若实施发现必须
改 `shared`，应先补 ADR，不能顺手修改。

## 13. 测试策略

严格执行 RED → GREEN → REFACTOR：

1. **纯契约单测**：配置全有或全无、严格布尔、安全版本格式、确定性哈希、事件合法组合、
   当前配置失效旧验证、状态优先级、无密钥/概率/金额字段。
2. **Repository 集成**：tenant filter、复合唯一约束、append-only trigger、并发 sequence、
   幂等重放、同版本异内容冲突、runtime 必须绑定 current passed config。
3. **Gateway 单测/集成**：固定 `/account`、无 PII、权限/幂等/限流、401/429/5xx/schema/网络
   分类、started-before-IO、Provider 成功后持久化失败进入 inconclusive、敏感 canary 不落盘。
4. **scheduler 组合测试**：所有非 ready 状态均零凭证解析、零 transport、零 manifest；passed
   状态精确注册两个工具；runtime fact 失败使启动失败；运行中配置切换使旧 adapter 在凭证
   解析前失败关闭。
5. **联系人链路回归**：ready 状态下 `contact.enrich` 仍经过六阶段，未知/禁止国家和 reader
   故障均在 Hunter IO 前关闭；`contact.verify` 缓存与隐私声明语义不变。
6. **API 集成**：Settings 从 Postgres reader 获取状态，覆盖所有原因码、reader 故障和跨租户。
7. **前端单测与 E2E**：每个状态有准确中文说明，无密钥控件、无 ready 误报、桌面和移动视口
   无横向溢出或 console error。
8. **离线运维验收**：验证命令使用受控 transport；真实 Hunter smoke test 记录为 `not_run`。
9. **全库验收**：`make check`、前端 tests/typecheck/build/lint、敏感扫描、边界检查与 Alembic
   单 head。

所有测试使用合成 tenant、actor、配置版本、国家标签和 canary secret。canary 只能在进程内
connector 边界出现，测试必须证明数据库、ledger、日志、异常和 API 响应均不包含它。

## 14. 运维流程

真实部署的后续操作顺序固定为：

1. 在密钥服务配置 Hunter Key；不要把值粘贴到聊天、工单、数据库或 Git。
2. 设置新的配置版本与 API key 版本，启用 Hunter contacts。
3. 由授权运维人员执行配置声明命令；确认 Settings 显示 validation pending。
4. 启动 scheduler，确认环境安全配置与 durable configured 事实精确一致。
5. 由授权运维人员执行一次验证命令，保存安全 validation key 和时间。
6. 验证失败或不确定时停止，不自动重试，按固定分类排查。
7. 验证成功后重启 scheduler。
8. 确认取得单副本锁的 scheduler 写入 matching `runtime_composed`，Settings 再显示 ready。
9. 使用已批准的合成/内部测试目标完成一次受控业务旅程，再进行真实运营验收。

runbook 必须明确区分：代码验收、离线 Provider 验收、真实 Provider 验证和 Phase 1 运营验收。
任何一项未运行都写 `not_run`，不得用自动化测试替代。

## 15. 文档更新

实施完成后更新：

- `HANDBOOK.md`：生产组合代码已交付，但真实 Provider 验证与运营验收仍未运行；
- `docs/architecture/04-tool-gateway.md`：验证插件、条件注册和持久 readiness；
- `docs/architecture/08-compliance.md`：Provider readiness 不替代国家政策；
- `docs/architecture/11-deployment.md`：配置、重启与健康检查顺序；
- `tool_gateway/AGENTS.md`、`apps/scheduler_worker/AGENTS.md`：新增硬约束；
- `infra/.env.example`：只增加空引用和说明，不写示例密钥；
- 新运维手册：命令、固定状态、恢复和 `not_run` 记录格式。

README 若继续称“业务实现尚未开始”，应同步纠正为当前真实状态，但不得扩大 Phase 1 完成
声明。

## 16. 验收标准

1. API 与 scheduler 不再使用各自的布尔值猜测 Provider composition。
2. readiness 事实 tenant-scoped、append-only，可通过 canonical validation key 追溯到安全
   配置版本、actor 和 Tool Gateway ledger。
3. 密钥或配置版本变化后，旧验证与 runtime fact 不能继续放行。
4. 未配置、未验证、失败、不确定和未组合路径的凭证解析与 Hunter 调用均为零。
5. 人工验证只经 Tool Gateway 访问固定 `/account`，不处理 PII，不保存原始响应。
6. 只有精确当前配置 passed 后，scheduler 才同时注册 `contact.enrich` 与 `contact.verify`。
7. 只有取得单副本锁的 scheduler 能写 `runtime_composed`；写入失败时必须释放锁且不能进入
   第一个业务 cycle。
8. Settings 的原因码与持久事实一一对应，不能显示假 ready。
9. ready 后的联系人补全仍逐次执行 Playbook、国家政策、抑制和配额检查。
10. 全库自动化验收通过，Alembic 单 head，敏感 canary 不落盘、不出日志、不进异常。
11. 真实 Hunter smoke test 在本切片保持 `not_run`，文档不宣称联系人补全已上线。
12. Phase 1 仍需真实 Provider 验证、真实 Campaign 运营旅程和 HANDBOOK 四项完成标准。

## 17. 实施后的真实边界

本切片完成的是“可安全激活的生产组合”，不是“已经激活的生产 Provider”。它把最后一个
代码层阻断拆成可审计步骤，并保证缺少真实运维证据时系统保持关闭。

后续仍有两类外部工作：

1. 配置真实 Hunter 凭证并执行人工 `/account` 验证；
2. 按 HANDBOOK 完成真实 Campaign、证据链、发件信誉与人工接管运营验收。

只有这些外部事实真正产生后，才能分别宣称 Hunter Provider ready 和 Phase 1 完成。
