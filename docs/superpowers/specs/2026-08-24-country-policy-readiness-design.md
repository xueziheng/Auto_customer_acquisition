# 国家政策包持久化与就绪设计

> 日期：2026-08-24
> 输入：`AGENTS.md`、`HANDBOOK.md`、`GLOSSARY.md`、
> `docs/architecture/02-boundaries.md`、`04-tool-gateway.md`、
> `08-compliance.md`、Hunter 联系人 Provider 规格、Company Playbook 已交付实现
> 范围：Phase 1 国家政策包的版本化配置、独立审批、激活、读取、Settings 管理与真实就绪状态

## 1. 背景与问题

Company Playbook 已成为不可变、需独立审批后生效的真实配置，但国家政策仍只有两类临时
实现：联系人发现使用部署层国家 allowlist，公开搜索使用注入的布尔 reader。它们不能回答
某个国家为什么允许处理、依据来自哪里、谁批准、何时生效，也无法安全处理政策更新。

这使生产 `contact.enrich` 仍必须保持未注册。仅把某个国家加入环境变量会绕过版本、证据、
审批、租户隔离和历史审计，违反“政策包是数据不是代码”的架构要求。

本设计建立独立的国家政策域。系统不内置、推断或推荐任何国家法律结论；所有决策值必须由
授权人员根据已核实材料显式录入，并由不同人员批准后才能生效。

## 2. 目标与非目标

### 2.1 目标

1. 每个租户、每个规范化国家键保存不可变的政策版本和 append-only activation。
2. 每个会影响外部处理的字段都保存独立 Provenance。
3. 首次配置和后续修订都经过不同身份的人工审批；API 没有直接激活旁路。
4. 为公开研究和联系人补全提供结构化、tenant-scoped、fail-closed 的政策判定。
5. Settings 展示生效政策、候选差异、字段来源、审批状态、历史版本与真实就绪原因。
6. 为后续生产 `contact.enrich` composition 提供真实数据源，但不在本切片注册该工具。

### 2.2 非目标

- 不预置 US、EU、UK 或任何其他市场的法律模板或允许值。
- 不调用模型、搜索引擎或第三方法律数据库自动填充政策。
- 不把 `DE`、`Germany`、`德国` 等标签自动判为同一国家。
- 不建设多级法律审批链、定时法律更新、政策到期提醒或跨租户共享政策库。
- 不配置或验证真实 Hunter API Key，不发起真实 Hunter 网络请求。
- 不注册生产 `contact.enrich`，不宣称联系人补全已上线。
- 不改变既有企业 `country` 自由文本契约；匹配不到已激活政策时固定拒绝。

## 3. 方案选择

### 3.1 采用：独立 `domains/compliance`

国家政策属于合规业务规则，不属于公司经营偏好的 Company Playbook，也不属于 Tool Gateway
设施。新增 `domains/compliance`，保持域间零直接导入；上层与 Tool Gateway 只依赖其
`service.py` 和 `schemas.py` 公共接口。

必须同步更新 `domains/AGENTS.md` 与 `docs/architecture/02-boundaries.md` 的域清单。新增公共
强类型 ID 会修改 `shared/schemas/identifiers.py`，因此先留 ADR，再写实现。

### 3.2 淘汰的方案

- 扩展 `domains/organization`：会把法律政策与公司经营配置耦合，权限、审批和更新频率不再
  独立。
- 环境变量 allowlist：无 Provenance、无历史、无审批、无租户级审计，不能作为生产事实源。

## 4. 国家键与匹配语义

政策以 `country_key` 作为稳定业务键。输入执行：

1. Unicode NFKC；
2. 去首尾空白；
3. 连续空白合并为单个空格；
4. `casefold`；
5. 拒绝空值、控制字符和超过 64 个字符的结果。

企业 preflight 的 `country` 使用完全相同的函数规范化后查询。系统不维护别名表，也不调用
地理库猜测等价关系。因此 `DE` 与 `Germany` 是两个不同键；只有精确键存在已激活版本时
才算“已配置”。这种保守行为可能增加人工配置成本，但避免错误国家映射变成合规放行。

同一租户、同一 `country_key` 同时只能有一个当前 activation；不同租户互不影响。

## 5. 政策内容契约

### 5.1 候选内容

`CountryPolicyProposalCreate` 必须显式提供以下字段，不设业务默认值：

| 字段 | 类型与约束 | 用途 |
|---|---|---|
| `country` | 原始显示文本，规范化得到 `country_key` | Settings 显示与精确匹配来源 |
| `public_research_allowed` | strict bool | 是否允许公开市场研究 |
| `contact_enrichment_allowed` | strict bool | 是否允许使用 Provider 处理联系人数据 |
| `cold_b2b_email_allowed` | strict bool | 是否允许该国家的冷 B2B 邮件 |
| `personal_data_basis_required` | strict bool | 是否要求个人数据处理依据 |
| `subject_type_affects_judgment` | strict bool | 主体类型是否影响判断 |
| `contact_type_affects_judgment` | strict bool | 联系方式类型是否影响判断 |
| `opt_out_deadline_days` | `int | None`；若有则 1..365 | 退订处理期限 |
| `local_representative_required` | strict bool | 是否要求本地代表 |
| `requirements` | 去重排序的固定 action codes，最多 100 项 | 具体执行要求 |
| `notes` | 1..4000 字符 | 人工可读说明，不作为放行逻辑 |
| `field_sources` | 决策字段到安全来源输入的精确映射 | 服务端生成字段级 Provenance |

`requirements` 仅接受 `[a-z][a-z0-9._:-]{0,127}`，不能通过自由文本创建隐含逻辑。需要数值
的要求必须使用显式字段，例如退订天数不能编码成 `honor_opt_out_within_days:10` 后再解析。

### 5.2 Provenance

下列每个决策字段都必须有独立 Provenance：

```text
public_research_allowed
contact_enrichment_allowed
cold_b2b_email_allowed
personal_data_basis_required
subject_type_affects_judgment
contact_type_affects_judgment
opt_out_deadline_days
local_representative_required
requirements
```

请求只允许为每个字段提交 `source_type`、安全 `source_id`、可选 `source_url` 与
`page_hash`；不接受 `extracted_by`、`extracted_at`、`confirmed_by` 或 `confirmed_at`。
`source_type` 只允许 `WEB_PAGE`、`UPLOAD`、`EMPLOYEE_INPUT`，不得为
`AGENT_INFERENCE` 或 `EXTERNAL_API`。

`source_id` 必须匹配 `[A-Za-z][A-Za-z0-9._:-]{0,199}`。`WEB_PAGE` 必须同时提供不含
userinfo、长度不超过 2048 的 HTTPS URL 与 64 位小写十六进制 `page_hash`；
`UPLOAD`、`EMPLOYEE_INPUT` 必须省略这两个网页专用字段。这样“安全来源”是可执行的输入
边界，而不是依赖调用者自觉的描述。

合规服务使用受信 actor 和服务器 UTC 时间生成 Provenance：`extracted_by` 固定为
`human:<actor_id>`，`extracted_at`、`confirmed_at` 固定为版本 `proposed_at`，
`confirmed_by` 固定为提交员工。请求体不能伪造身份或时间。每项因此都满足
`is_human_confirmed=true`；未经人工确认的提取结果不能控制合规放行。
`source_id` 只能是内部 artifact/法律评估引用等安全 ID，不在政策表保存网页全文、凭证、PII
或任意异常文本。`notes` 是解释性内容，不参与 Tool Gateway 放行，也不替代字段来源。

### 5.3 事实与推断

政策字段保存的是“公司已批准采用的操作规则”，不是对法律正确性的模型推断。模型概率、
confidence、风险分数和“可能允许”状态都不进入契约。布尔值必须明确；无法确认时人工应填
`false`，或不激活该国家政策。

## 6. 版本、审批与激活

### 6.1 状态事实分离

- `country_policy_versions`：不可变候选内容、内容哈希、国家键、基准版本、提交者、提交时间、
  字段 Provenance 和幂等键。
- `country_policy_activations`：append-only 生效事实，保存精确版本、审批 ID、批准人/时间、
  系统激活人/时间与 change-set reference。

候选版本本身不保存可变状态。审批状态来自审批域，当前版本来自最新 activation。拒绝、过期
或 apply failure 不改写候选内容。

新增强类型 ID：

```text
CountryPolicyVersionId      cpp_<ULID>
CountryPolicyActivationId   cpa_<ULID>
```

新增这些 shared 契约前必须提交 ADR 0010，说明独立域、版本/activation 分离和窄审批事实。

### 6.2 内容哈希与变更集

对规范化、稳定排序后的全部政策内容和安全字段来源输入生成 SHA-256。服务端派生的
`extracted_by/at`、`confirmed_by/at`、数据库创建时间和随机 ID 不进入哈希，保证同一
幂等请求跨时间重试仍得到相同内容哈希。change-set reference 固定为：

```text
country_policy:<version_id>:<content_hash>
```

国家键不进入 change-set reference，避免自由国家标签中的分隔符产生歧义；工作流从同租户
version 记录读取并核对国家键。同一租户内幂等键只可对应一份完全相同的候选；相同幂等键
不同内容固定冲突。

### 6.3 审批

审批域新增 `ApprovalType.COUNTRY_POLICY_CHANGE`，默认有效期 7 天。审批包必须完整展示：

- 国家显示值与规范化键；
- base/current/candidate 字段差异；
- 每个决策字段的安全来源引用；
- 批准后哪些 Gateway 能力可能放行；
- 拒绝后保持当前政策或继续默认拒绝；
- 变更可通过新版本逆转，但既有审计事实不可删除。

提交人与决定人必须是不同员工。Phase 1 沿用现有审批域授权规则，不新增“法律管理员”角色。
如果租户只有一个可审批身份，政策不能自批；这是安全边界，不提供 bootstrap 或 system 默认
批准。

### 6.4 工作流与幂等应用

`CountryPolicyVersionProposed` outbox 事件启动 `country_policy_change` 工作流：

```text
load_snapshot → request_approval → wait_for_decision → apply_approved_version
```

工作流只向合规域传入窄 `CountryPolicyApprovalFact`。激活前逐字段验证审批类型、审批 ID、
change-set reference、内容哈希、批准人、批准时间和当前 base 版本。

若候选基于的版本已不再是当前版本，应用进入稳定错误
`COUNTRY_POLICY_BASE_VERSION_CONFLICT`；审批事实不一致使用
`COUNTRY_POLICY_APPROVAL_FACT_INVALID`。重复事件和重复执行按 version + approval ID 幂等；
不同审批试图激活同一版本固定冲突。

首次配置要求候选 `base_version_id=None` 且该国家仍无当前版本。后续修订必须精确引用当前
版本和内容哈希。

## 7. 公共读取与 fail-closed 语义

合规域公共服务至少提供：

```python
get_country_policy_decision(tenant_id, country, action, *, actor)
get_active_policy(tenant_id, country, *, actor)
get_version(tenant_id, version_id, *, actor)
list_active_policies(tenant_id, *, actor, limit)
list_versions(tenant_id, country, *, actor, limit)
propose_country_policy(...)
activate_country_policy(...)
```

`CountryPolicyAction` Phase 1 只含：

```text
public_research
contact_enrichment
cold_b2b_email
```

`CountryPolicyDecision` 返回规范化国家键、action、`configured`、`allowed`、当前版本 ID、内容
哈希和与 action 相关的 requirements。必须满足：

- 无激活政策：`configured=false, allowed=false`；
- 已配置但明确禁止：`configured=true, allowed=false`；
- 已配置且允许：两者都为 true；
- 租户不匹配、数据形状无效或存储读取失败：抛出，不伪装成“未配置”。

Tool Gateway 将结果映射为结构化拒绝：

| 情况 | reason code |
|---|---|
| 无激活政策 | `country_policy:not_configured` |
| 政策明确禁止 action | `country_policy:action_not_allowed` |
| reader 故障或返回无效类型 | transient failure，调用外部 Provider 为 0 |

Gateway 不读取 repository，不解释政策字段，也不写合规表。

## 8. API、权限与 Settings

### 8.1 API

新增 Settings 路由：

```text
GET  /settings/country-policies
GET  /settings/country-policies/versions?country=<display-or-key>&limit=<1..200>
POST /settings/country-policies/proposals
```

Phase 1 只有同租户 active boss 可读和提交。请求身份、tenant 和幂等键由受信边界注入，payload
不得携带或覆盖它们。POST 要求 `Idempotency-Key` header，成功返回 version ID、run ID 和
change-set reference。API 不提供 activate、update、delete、force、apply-now 或默认模板端点。

版本列表组合审批域只读状态，固定显示 pending/approved/rejected/expired/applied/apply_failed
和稳定 application error code。未知内部异常继续走统一脱敏边界。

### 8.2 Settings UI

在现有 Settings Center 增加“国家政策包”区，不复制一套顶层导航：

- 空状态明确说明系统没有法律默认值；
- 生效政策按国家键列出，显示允许/禁止 action 和 activation 时间；
- 新建与修订表单要求每个决策字段填写安全来源；确认身份和时间由服务端绑定并回显；
- 提交前展示 base/current/candidate 差异；
- 历史记录显示审批状态、提交者、批准者、版本和来源入口；
- 无删除、直接激活或推荐模板按钮；
- 桌面与 390×844 移动视口不产生横向溢出。

面向客户内容仍不在本页面出现；内部 UI 使用中文，代码和 API 字段使用英文。

## 9. 就绪状态

Settings 不再硬编码单一 `COUNTRY_POLICY_NOT_CONFIGURED`。合规域提供只读 coverage summary，
API 将 Company Playbook、国家政策覆盖和 Connector composition 分层展示：

| 条件 | 状态与原因 |
|---|---|
| 没有任何激活政策 | blocked / `COUNTRY_POLICY_NOT_CONFIGURED` |
| 有政策，但没有任何 `contact_enrichment_allowed=true` | blocked / `CONTACT_ENRICHMENT_NOT_ALLOWED` |
| 至少一个国家允许 enrichment，但生产工具未组合 | blocked / `CONTACT_ENRICHMENT_NOT_COMPOSED` |

本切片的最终真实状态最多到第三行。它证明政策数据源已配置，但不代表 Hunter credential、
transport、配额和 production handler 已就绪。后续 composition 切片必须同时具备：

1. 已激活 Company Playbook；
2. 真实 `ComplianceService` reader；
3. Hunter secret resolver 与固定 host transport；
4. Prospecting、Demand、Outreach 和配额依赖；
5. 启动自检与真实 provider 运维验收。

只有后续条件全部满足，才可注册生产 `contact.enrich`。每次具体调用仍按目标国家再次读取
当前政策；全局“有一个允许国家”不能放行另一个未知国家。

## 10. 存储与迁移

新增单一 Alembic migration，保持单 head。所有表 `tenant_id NOT NULL`，所有唯一约束和 FK
包含 tenant：

```text
country_policy_versions
  PK (tenant_id, country_policy_version_id)
  UNIQUE (tenant_id, country_key, version_number)
  UNIQUE (tenant_id, idempotency_key)
  immutable payload + content hash + base refs + proposed facts

country_policy_field_provenance
  PK (tenant_id, country_policy_version_id, field_name)
  exact Provenance columns; FK includes tenant; ON DELETE RESTRICT

country_policy_activations
  PK (tenant_id, country_policy_activation_id)
  UNIQUE (tenant_id, country_key, activation_sequence)
  FK version includes tenant and country key
  append-only approval/application facts
```

数据库 trigger 拒绝 UPDATE/DELETE policy version、field provenance 和 activation。repository
所有方法签名要求 `tenant_id`，查询显式包含 tenant predicate。激活以租户 + 国家键 advisory
lock 或等价事务锁串行化，防止两个批准版本同时成为当前版本。

字段 Provenance 不使用 JSON blob；policy 的 bounded requirement codes 可使用确定性数组或
子表，但内容哈希与读取顺序必须稳定。金额和置信度字段不属于本模型。

## 11. 测试策略

严格按 RED → GREEN → REFACTOR：

1. **契约单测**：国家规范化、strict bool、全部字段必填、requirement code、退订天数边界、
   每字段安全来源、拒绝身份/时间输入、服务端 Provenance、无概率字段和稳定内容哈希。
2. **服务单测**：首次提案、修订、幂等冲突、租户权限、未知国家判定、明确禁止判定、窄审批
   事实、自批边界和陈旧 base 冲突。
3. **Postgres 集成**：迁移结构、租户复合 FK、不可变 trigger、并发版本号、activation 串行、
   repository tenant filter、outbox 原子性和重复应用。
4. **工作流集成**：提出事件、审批请求、重复事件、批准/拒绝/过期、apply failure、提交后崩溃
   恢复和批准后激活。
5. **Gateway 单测**：未配置、明确禁止、允许、无效返回、reader 异常全部 fail closed，并断言
   被拒时 Hunter transport 调用为 0。
6. **API 集成**：boss-only、tenant 绑定、幂等 header、列表、版本状态、无直接激活路由和真实
   readiness reason。
7. **前端单测与 E2E**：空状态、候选提交、差异/来源/历史、错误恢复、桌面与移动视口、无
   console error、无横向溢出。
8. **全库验收**：`make check`、前端完整 tests/typecheck/build、`python3 scripts/check_boundaries.py`、
   Alembic 单 head。

测试只使用合成国家标签与合成来源 ID，不写真实法律结论，不读取真实 Key 或真实网络。

## 12. 文档与真实状态

完成后更新：

- `HANDBOOK.md`：国家政策 persistence/readiness 已实现；production composition 仍未完成；
- `docs/architecture/02-boundaries.md`：登记 compliance 域；
- `docs/architecture/04-tool-gateway.md`：真实 reader 已存在，但 `contact.enrich` 仍未注册；
- `docs/architecture/08-compliance.md`：记录不可变版本、审批、字段 Provenance 与精确匹配；
- `domains/AGENTS.md`：域数量与职责清单；
- 新 `domains/compliance/AGENTS.md`：职责、状态、依赖白名单、禁止事项。

不得宣称 Phase 1 完成。此切片之后仍有两个独立条件：生产 `contact.enrich` composition 与
真实运营验收。运营验收仍按 HANDBOOK 的 Campaign、证据链、发件信誉和人工接管四项标准。

## 13. 验收标准

1. 系统中不存在内置国家法律默认值或环境变量 allowlist 生产旁路。
2. 只有带完整字段 Provenance、经不同身份批准的候选才能成为当前政策。
3. 未知国家、明确禁止、存储故障分别产生正确的 fail-closed 结果。
4. 当前政策可以追溯到精确候选、内容哈希、审批、来源和 activation。
5. Settings 能创建/修订、查看差异和历史，但不能删除或直接激活。
6. readiness 能区分政策未配置、全部禁止和 production composition 未完成。
7. 生产 `contact.enrich` 仍未注册，文档和 UI 不误报其可用性。
8. 全库自动化验收通过，且无跨域直接导入、金额 float、数值置信度或缺 tenant filter。
