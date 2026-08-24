# Company Playbook 版本化审批设计

> 输入：根/目录级 `AGENTS.md`、`HANDBOOK.md`、
> `docs/architecture/04-tool-gateway.md`、`08-compliance.md`，以及当前仓库对
> account discovery、approvals、organization、Settings API/UI 的事实核查。
> 本规格只定义 Company Playbook 的不可变版本、独立审批与自动生效闭环。

## 1. 已核实事实与优先级纠正

HANDBOOK 中“下一步应先完成账户发现工作流”的进度说明已经被后续提交覆盖：
`AccountDiscoveryAgent`、Postgres 工作流、Hunter Tool Gateway、Campaign 入组、API 和
Web 页面均已存在。当前生产闭环的真实阻断点是 Company Playbook 与国家政策包尚未
持久化和装配；`apps/api/routers/settings.py` 与 Settings 页面仍明确标记为契约占位。

`domains/organization` 已有 `CompanyPlaybook` 模型与公共 Protocol，但没有服务实现、
UoW、Repository、迁移或 API。现有 `update_playbook` 签名也无法证明调用方持有哪一条
精确审批事实。直接实现该方法会形成审批旁路。

本切片交付：

1. 不可变、tenant-scoped 的 Company Playbook 候选版本和激活历史；
2. 首次配置与后续修改统一走独立审批，提交人不得自批；
3. `playbook_change` 持久工作流，审批通过后自动、幂等生效；
4. Settings API/UI 的读取、差异、候选提交和审批状态；
5. 明确的配置阻断状态，不提供默认 Playbook，也不提前宣称联系人补全可用；
6. PostgreSQL 并发、不可变、租户隔离与崩溃重放测试。

本切片不做：国家政策包的数据模型、Connector 密钥编辑、Hunter 生产凭证装配、
产品/寻源页面、利润规则、报价锁定、多级审批或 Phase 2 自动化。Playbook 生效只解除
“Playbook 未配置”阻断；联系人补全仍因“国家政策包未配置”保持不可用。

## 2. 方案选择

采用“版本化 Playbook + 独立审批 + 自动生效”。

不采用环境变量保存 Playbook：环境变量无法提供版本历史、审批包、字段差异或租户级
审计，也会让 Settings 页面成为无法安全保存的假界面。

不在本切片同时建设完整国家政策库和全部 Connector readiness：这些是独立子系统，
一起实现会违反 HANDBOOK“一次一个域的一个切片”，也会把法律政策事实与公司的商业
边界错误地混在同一模型。

首次配置不设 bootstrap 旁路。候选提交人必须由另一位 boss 或 manager 审批；因此只有
一个具备审批能力的员工时，租户不能启用探索。这是明确的治理要求，不是实现缺陷。

## 3. 领域边界与 ADR

### 3.1 组织域

`domains/organization` 是 Playbook 内容、版本、基准版本和激活事实的唯一归属地。
它不导入 approvals，也不复制审批状态机。

新增公共 DTO 和服务方法会扩展跨域契约。实现前新增
`docs/adr/0009-playbook-version-approval-contract.md`，记录：

- 为什么弃用“直接 update”语义；
- 为什么审批事实由 workflow 读取后通过窄 DTO 传入组织域；
- 为什么 payload 与 activation 分表并全部 append-only；
- 为什么首次配置也不允许自批。

### 3.2 审批域

`domains/approvals` 新增 `ApprovalType.PLAYBOOK_CHANGE`，有效期为 7 天。审批域继续负责
pending、approved、rejected、expired、applied、apply_failed、自批禁止与应用幂等。
组织域不解释这些状态。

审批包必须包含完整旧值/新值、内容哈希、基准版本、影响范围，以及批准和拒绝后果。
`proposed_by_employee` 与 `owner_employee` 均绑定候选提交人，确保提交人不能审批。审批
Router 继续只允许 boss/manager 做决定。

### 3.3 工作流层

新增 `workflows/playbook_change`，只编排 organization 与 approvals 的公共服务。
workflow context 只保存 typed ID、内容哈希、`change_set_ref`、审批 ID 和固定状态，不复制
完整 Playbook，不保存凭证或自由错误文本。

## 4. 数据模型

### 4.1 `CompanyPlaybookVersion`

每次提交产生一个不可变候选版本：

```text
tenant_id
playbook_version_id       pbv_<ULID>
version_number            租户内严格递增
content_hash              规范化内容的 SHA-256
base_version_id           提交时的当前生效版本；首次配置为 null
base_content_hash         与 base_version_id 成对；首次配置为 null
company_type
minimum_deal_value        Money：Decimal amount + currency
excluded_categories
sourcing_regions
excluded_countries
monthly_budget_credits
approval_requirements
supply_capabilities_note
proposed_by
proposed_at
idempotency_key
```

版本内容规范化后计算哈希：

- 金额 amount 使用规范 Decimal 字符串，不经过 float；
- 币种使用三位大写代码；
- 列表项拒绝空白和控制字符，去首尾空格、稳定去重并按规范值排序；
- 可选文本保留语义内容但拒绝仅空白；
- canonical JSON 使用固定字段顺序和 UTF-8，再计算 SHA-256；
- `tenant_id`、版本 ID、版本号、提交人和时间不进入内容哈希。

相同业务内容可以在不同基准上重新提交，因版本 ID 和基准不同仍是不同候选。相同
`(tenant_id, idempotency_key)` 只能对应一个候选；重试返回既有版本，payload 不同则抛
幂等冲突。

### 4.2 `PlaybookActivation`

激活历史独立成 append-only 事实：

```text
tenant_id
activation_id             pba_<ULID>
playbook_version_id
content_hash
approval_id
change_set_ref
activated_by
activated_at
```

当前生效版本是租户最新的一条 activation 所引用的版本。某版本是否被替代及替代时间，
由后续 activation 推导，不更新旧版本或旧 activation。这样候选内容、批准依据和生效
顺序均可复原，不需要在“不可变版本”行上开更新例外。

每个版本最多一条 activation，每个 approval ID 最多应用一次。相同审批重放返回既有
activation；同一版本绑定不同审批或同一审批绑定不同版本均为安全冲突。

### 4.3 审批事实 DTO

组织域公共 schema 新增窄 `PlaybookApprovalFact`：

```text
approval_id
approval_type             必须精确等于 playbook_change
change_set_ref
decided_by
decided_at
```

该 DTO 不证明自身真实性。只有 `playbook_change` workflow 可以通过审批服务读取
`approved` 事实后构造它；API 不接受客户端提交的审批事实，普通员工 actor 也没有激活
权限。

`change_set_ref` 固定为：

```text
playbook:<playbook_version_id>:<content_hash>
```

激活时逐字段核对版本 ID、内容哈希、审批类型和 change set，禁止“批准旧内容后替换
候选内容”。

## 5. 领域服务和权限

新增 `domains/organization/permissions.py`，采用 API + service 双重门禁：

- `PLAYBOOK_READ`：boss；
- `PLAYBOOK_PROPOSE`：boss；
- `PLAYBOOK_ACTIVATE`：仅 composition root 构造的 system actor。

组织域公共服务提供：

```text
get_playbook(tenant_id, *, actor) -> CompanyPlaybook
get_version(tenant_id, playbook_version_id, *, actor) -> PlaybookVersionView
list_versions(tenant_id, *, actor, limit) -> list[PlaybookVersionView]
propose_playbook(tenant_id, command, *, actor, idempotency_key)
    -> PlaybookProposalResult
activate_playbook(tenant_id, playbook_version_id, approval_fact, *, actor)
    -> PlaybookActivationView
```

`get_playbook` 在没有 activation 时抛 `PlaybookNotConfiguredError`，不返回隐含默认值。
原有无审批上下文的 `update_playbook` 从公共契约移除；仓库内当前没有实现或调用者，
因此迁移不会保留不安全兼容层。

`approval_requirements` 只表示叠加在全局 MUST_APPROVE 之上的额外动作。服务和 UI 不提供
移除全局注册表条目的表达方式；未知动作仍遵循 approvals 的 fail-closed 规则。

## 6. 提交、审批与自动生效

### 6.1 候选提交

1. Settings Router 从 `RequestIdentity` 构造 boss actor，不接受客户端身份字段。
2. 组织服务在 tenant advisory transaction lock 下读取当前 activation，分配版本号，
   保存候选及其 base version/hash。
3. API 以候选 ID 为 subject 启动 `playbook_change` workflow；workflow start 使用
   `playbook-change:<tenant>:<idempotency_key>` 幂等键。
4. API 返回 202、候选版本 ID 与 Run ID。审批 ID 由工作流创建后写入 workflow context，
   客户端通过版本视图读取，不伪造同步成功。

如果候选已保存而 workflow start 暂时失败，重试相同 idempotency key 会重用同一候选和
同一 workflow run，不产生孤儿重复版本。

### 6.2 工作流状态

```text
assemble_package
→ submit_approval
→ wait_decision
   ├─ rejected / expired → complete（不写 activation）
   └─ approved → apply_playbook → mark_applied → complete
```

`submit_approval` 使用精确 `change_set_ref`；approvals 已有的 pending 幂等规则保证重放
不会创建第二个审批包。`wait_decision` 由 `ApprovalDecided` 唤醒；同时按审批
`expires_at` 设置到期检查。到期 handler 先调用 `expire_overdue(tenant_id)`，再按
`change_set_ref` 重读该审批；只有读到 `expired` 才结束流程，从而覆盖过期不发布决定
事件的情况，也不把暂时读取失败解释为过期。

### 6.3 基准版本保护

`activate_playbook` 在 tenant advisory transaction lock 内读取最新 activation：

- 首次配置候选要求当前仍没有 active version；
- 后续候选要求当前 version ID 和 hash 同时等于候选的 base；
- 两个候选从同一 base 出发时，先激活者胜出；后激活者固定失败，不能覆盖新版本；
- 冲突要求基于最新版本重新提交，旧审批不复用。

确定性的基准冲突调用 `approvals.mark_apply_failed`，使用固定、无敏感内容的错误摘要。
拒绝、过期和 apply_failed 候选永久保留但永不生效。

### 6.4 崩溃与重放顺序

工作流必须先提交 organization activation，再调用 approvals `mark_applied`：

- activation 提交前失败：没有配置变化，outbox 可重放；
- activation 已提交但响应丢失：重放识别相同 approval/version 的 activation，返回既有
  事实，再补写 `mark_applied`；
- `mark_applied` 暂时失败：activation 不重复，后续重放只补审批状态；
- 业务基准冲突：标记 apply_failed，不自动重试到成功；
- PostgreSQL 暂时不可用：保留 outbox 重试，不把基础设施故障伪装成业务拒绝。

不得先 `mark_applied` 再激活，因为后续组织写入失败时会留下“审批已应用但配置未变化”
且无法安全恢复的事实。

## 7. PostgreSQL 契约（迁移 0031）

### 7.1 `company_playbook_versions`

- PK `(tenant_id, playbook_version_id)`；
- unique `(tenant_id, version_number)` 与 `(tenant_id, idempotency_key)`；
- self composite FK `(tenant_id, base_version_id)`；base ID/hash 必须同时为空或同时非空；
- `minimum_deal_amount NUMERIC(28, 12)`、三位币种、非负金额；
- 列表用 JSONB array，禁止 null 元素，服务负责规范化；
- `monthly_budget_credits` 为空或非负整数，不提供默认值；
- 核心字符串 nonblank，content hash 为 64 位小写十六进制；
- trigger 拒绝 UPDATE/DELETE。

### 7.2 `company_playbook_activations`

- PK `(tenant_id, activation_id)`；composite FK 到版本；
- unique `(tenant_id, playbook_version_id)` 与 `(tenant_id, approval_id)`；
- 索引 `(tenant_id, activated_at, activation_id)`，以稳定顺序读取当前版本；
- 版本 hash、change set、决定人和 UTC 时间必填；
- trigger 拒绝 UPDATE/DELETE。

两个表的所有查询显式 tenant predicate，Repository 构造时绑定 tenant。跨租户入参抛
`TenantIsolationViolation`，安全日志不记录 Playbook 正文。

迁移必须验证 upgrade → downgrade → upgrade，并更新 SQLAlchemy metadata parity、单一
Alembic head 与 AppleDouble 文件检查。

## 8. API 契约

Settings Router 替换当前统一 status 占位，提供：

```text
GET  /settings/playbook
GET  /settings/playbook/versions?limit=50
POST /settings/playbook/proposals
```

`GET /settings/playbook` 返回 `configured` 和可选 active version。未配置是正常的显式
状态，不用空对象冒充 Playbook；组织域内部仍以 `PlaybookNotConfiguredError` 守住业务
消费者。

proposal request 使用 Pydantic v2 strict/frozen/extra-forbid 模型：金额 amount 只接受
Decimal 或 JSON 十进制字符串，拒绝 float、int、bool、NaN 和 Infinity；身份、租户、
审批 ID、激活时间均不在请求体。字段名包含 password、secret、token、cookie 的额外输入
因 `extra="forbid"` 被拒绝。

不存在以下接口或参数：

```text
PUT /settings/playbook
DELETE /settings/playbook/*
force
override
apply_now
skip_approval
```

版本列表由 API composition 聚合 organization 版本事实与 approvals 的
`get_by_change_set` 视图，返回 pending、approved、rejected、expired、applied、
apply_failed。组织域本身不存这些状态。

## 9. Settings 页面

页面只向 boss 展示完整 Playbook：

- 当前生效版本、版本号、更新时间、提交人和批准人；
- company type、金额底线、排除品类、货源区域、排除国家、预算、额外审批要求和供应
  能力说明；
- 基于当前版本初始化的候选表单；首次配置时使用空表单，不填任何商业默认值；
- 提交前逐字段显示旧值与新值；
- 提交后展示候选版本、Run 和 Approval Center 关联；
- 每个候选展示审批/应用状态及固定失败说明；
- “联系人补全：阻断——国家政策包未配置”的明确提示。

页面没有即时保存、强制生效、删除历史或 Connector 密钥输入框。连接卡只显示安全
readiness 事实；密钥仍由部署配置持有，模型、API 和浏览器均不能读取。

## 10. 错误和安全边界

- 未配置：固定 `PLAYBOOK_NOT_CONFIGURED`；任何探索消费者 fail closed；
- 角色不符：API 与 service 两道 `PermissionDenied`；
- 自批：沿用 approvals 的 `SelfApprovalError`；
- 过期：重新提交新候选，不补批；
- 基准冲突：固定 `PLAYBOOK_BASE_VERSION_CONFLICT` 并 mark apply_failed；
- 幂等 key 同 payload：返回既有候选；不同 payload：固定幂等冲突；
- approval/type/hash/change set 不匹配：拒绝激活并记录安全审计，不回显全文；
- Repository/审批读取不可用：fail closed，不把“查不到”解释为可生效；
- 金额全链路使用 Decimal/Money，数据库使用 NUMERIC，模型不参与金额产生；
- 所有表、PK、FK、unique 和查询包含 tenant；
- Playbook 内容不包含凭证，日志只记录 typed ID、版本号和固定 reason code。

## 11. 测试与验收

### 11.1 单元测试

必须覆盖：

1. canonical hash 对列表顺序、重复项和 Decimal 等价表示稳定；真实内容变化改变 hash；
2. float/int/bool/非有限金额在 API 边界被拒绝；
3. 未配置时不返回默认 Playbook；
4. 相同 idempotency key 的同 payload 返回同一候选，不同 payload 冲突；
5. approval type、version、hash、change set 任一不匹配都不能激活；
6. 首次配置要求无 active version；后续配置要求 base ID/hash 精确匹配；
7. `approval_requirements` 只能叠加，不能表达移除全局条目；
8. boss 只能 read/propose，system actor 才能 activate。

### 11.2 审批和工作流测试

必须覆盖：

1. 首次配置提交人自批被拒绝；不同 boss/manager 批准后自动生效；
2. rejected、expired 不产生 activation；
3. 两个候选从同一 base 出发，后批准的旧基准候选进入 apply_failed；
4. activation 成功后 `mark_applied` 失败，重放只补审批状态；
5. 重复 `ApprovalDecided` 不重复激活；
6. workflow context、outbox 和日志不包含完整 Playbook 或任何凭证形态字段。

### 11.3 PostgreSQL 与 API 测试

必须覆盖：

1. 迁移/metadata parity、roundtrip、单 head；
2. 并发分配版本号无重复，并发激活同一 base 只有一个成功；
3. 两表 UPDATE/DELETE 均被数据库 trigger 拒绝；
4. 跨租户版本、idempotency key、approval ID 可各自存在且互不可见；
5. API 角色门禁、strict body、无 PUT/force/apply 接口；
6. Settings 页面展示当前版本、差异、审批状态和国家政策阻断；
7. desktop 与窄屏页面无横向溢出，浏览器 console 无新增错误或警告。

### 11.4 完成门槛

提交实现前运行：

```bash
python3 scripts/check_boundaries.py
make check
```

前端运行测试、定向 ESLint、`vue-tsc` 与 Vite build。金额、跨域导入、tenant filter、
审批旁路和敏感内容扫描必须全绿。

本切片完成不等于 Phase 1 完成。它只让 Company Playbook 成为真实、可审计的生效配置；
下一独立切片仍需实现国家政策包和 Connector readiness，随后才能讨论生产注册
`contact.enrich`。
