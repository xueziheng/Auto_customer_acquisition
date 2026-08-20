# Prospecting Persistence Design（企业、联系人、联系方式与合规门禁）

> 输入：`HANDBOOK.md` 切片 7、`ROADMAP.md` Phase 1、根/域级 `AGENTS.md`、
> `docs/architecture/08-compliance.md` 与 `10-database.md`，以及当前仓库缺口审计。
> 本规格只定义 prospecting 域的最小持久化闭环；外部补全 Provider、发现工作流、
> Campaign 入组和 UI 均在后续切片接入。

## 1. 已核实事实与本次范围

当前 `domains/prospecting` 只有模型和 Protocol 骨架；数据库没有
`prospect_accounts`、`prospect_contacts`、`contact_points`、
`contact_legal_basis`，也没有仓储、UoW 或服务实现。需求域已经持久化
`DemandSignal`、`NeedHypothesis` 与 `ValidatedNeed`，但 Demand Radar 不能安全地
显示企业名称或寻找联系人，因为 demand 域只有 `ProspectAccountId`，且硬边界 9
禁止它直接导入 prospecting 内部实现。

本切片交付：

1. 潜在企业、联系人、联系方式和法律依据四张权威表；
2. 域名优先的企业消歧，以及显式创建联系人；
3. 联系方式去重、验证状态机与 `VERIFIED` 入组门禁；
4. 删除请求的不可逆最小哈希抑制记录，防止删除后再次采集；
5. tenant-bound 仓储/UoW、原子 outbox 和公共 DTO；
6. PostgreSQL 并发、租户隔离、回滚和敏感内容零泄漏测试。

本切片不做：名称模糊自动合并、第二家联系人 Provider、外部验证 API、社交渠道、
Campaign 自动入组、需求雷达 API/UI。名称搜索只返回候选，永不自动合并。

## 2. 必须纠正的骨架问题

- `ProspectingService` 当前从公共 `service.py` 暴露内部 `ContactPoint` 与
  `ProspectAccount`；改为只接受/返回 `schemas.py` 的 Request/View DTO。
- 联系方式引用 `ProspectContactId`，但服务没有创建联系人的入口；新增
  `create_contact`，禁止调用方伪造悬空 ID。
- `handle_erasure_request` 只接收原始值且返回 `None`，现有 repository docstring
  声称“保留哈希抑制”却没有表；新增独立 append-only
  `prospecting_erasure_suppressions`。
- `ContactPointVerified` 尚未进入 outbox 白名单；本切片注册既有事件，不修改共享
  event schema。
- `ProspectAccountQualified` 没有确定性资格规则，创建企业不等于合格；本切片不发布
  该事件，避免把“发现”伪装成“通过硬门槛”。

## 3. 公共契约

`domains/prospecting/schemas.py` 定义并导出：

- `AccountResolveRequest(entity_name, country, website_domain=None,
  entity_type=None, industry=None, size_hint=None, source_signal_refs=())`；
- `ContactCreateRequest(account_id, full_name=None, role_title=None,
  language=None)`；
- `LegalBasisInput(basis, subject_type, contact_type, source, collected_at,
  source_url=None, assessment_ref=None)`；
- `ContactPointCreateRequest(contact_id, kind, value, legal_basis,
  enrichment_cost_note=None)`；
- `ProspectAccountView`、`ProspectContactView`、`ContactPointView`；
- 复出口 `VerificationStatus`、`LegalBasisType`、`SubjectType`、`ContactType`。

`ProspectingService` 的权威方法：

```text
resolve_account(tenant_id, request) -> ProspectAccountId
create_contact(tenant_id, request) -> ProspectContactId
add_contact_point(tenant_id, request) -> ContactPointId
record_verification(tenant_id, contact_point_id, result, provider) -> None
handle_erasure_request(tenant_id, contact_point_value) -> int
get_account(tenant_id, account_id) -> ProspectAccountView
list_verified_contact_points(tenant_id, account_id) -> list[ContactPointView]
```

删除返回实际删除的联系方式数量；未知值返回 0，但仍写入同一哈希抑制事实，以阻止
稍后重新采集。所有字符串拒绝空白和首尾空格，错误摘要固定且不得回显联系人值。

## 4. 确定性域规则

### 4.1 企业消歧

- `website_domain` 有值时，调用方必须传纯 host：无 scheme、path、query、fragment、
  port；服务用 IDNA ASCII、小写、去掉末尾点得到 canonical domain。
- `(tenant_id, website_domain)` 在非空时唯一；并发创建用 PostgreSQL
  `ON CONFLICT DO NOTHING` 后重读胜者，不做先查后插。
- 没有域名时，不自动按相似名称合并。重复调用可创建不同企业；这是有意的
  fail-safe，因为误合并两家同名企业比候选重复更难修。调用方需要幂等时必须提供域名。
- `search_by_name` 仅同租户、同国家，返回稳定排序候选；服务不调用它做自动决策。

### 4.2 法律依据

- 联系方式与一条法律依据在同一事务创建；缺失依据时不得先写联系方式。
- `basis=legitimate_interest` 时 `assessment_ref` 必填非空；其他依据允许为空。
- `collected_at` 必须为 UTC aware datetime；`source` 必填，`source_url` 若存在则非空。
- 法律依据独立成表，满足架构文档的 Phase 1 留痕要求；不是 JSON blob。

### 4.3 联系方式和验证

- Phase 1 `kind` 只允许 `email`、`phone`；不建社交渠道。
- 邮箱必须是单一地址，canonical value 为本地域保持原样、域名 IDNA 小写；电话必须
  是显式 `+` 开头的 E.164 形状。服务保存 canonical value。
- 去重键是 `(tenant_id, kind, value_hash)`；`value_hash` 由注入的确定性 HMAC-SHA256
  hasher 计算，数据库保存 64 位小写十六进制，不保存哈希密钥。该密钥是独立、稳定的
  privacy-suppression key；不得复用模型、邮箱或数据库凭证。密钥轮换必须先做双指纹
  迁移并保留旧 key 的查询能力，不能直接替换后让既有删除抑制失效。
- 新增前先查同租户 erasure suppression；命中即抛固定
  `ErasedContactPointError`，不得恢复或覆盖。
- 初始状态只能是 `unverified`。`record_verification` 接受四个枚举状态；provider
  必填。进入 `verified` 时记录 UTC `verified_at`，离开时清空。
- `ContactPointVerified` 只在“非 verified → verified”首次转换发布；重复 verified
  是幂等 no-op，不重复 outbox。其他结果不发布。
- `may_enter_sequence()` 当且仅当状态为 `verified`；`risky` 明确返回 False。

### 4.4 删除请求

- hasher 在进入事务前把输入变成安全 hash；原值不得进入日志、异常、outbox 或
  erasure 表。
- 同一事务：append-only 插入 `(tenant_id, value_hash, erased_at)`（冲突 no-op），
  找出并删除所有同 hash 的法律依据与联系方式，并删除已无联系方式的联系人。
- 企业不删除；它是法人业务事实，且可能仍有其他合法联系人。
- 已存在的 `outreach_suppressions` 不删除。prospecting 的 hash suppression 负责
  “不可再次采集”，outreach 的 typed suppression 负责“不可再次发送”，职责不同。
- suppression 表由 trigger 拒绝 UPDATE/DELETE；公共 API 没有恢复接口。

## 5. 数据库契约（迁移 0023）

### 5.1 `prospect_accounts`

- PK `(tenant_id, account_id)`；非空域名 partial unique
  `(tenant_id, website_domain)`；索引 `(tenant_id, country, name)`。
- 列：IDs、name、country、website_domain、entity_type、industry、size_hint、
  `source_signal_refs JSONB`、created_at。
- JSON 必须为 array；核心/可选字符串有 nonblank CHECK。

### 5.2 `prospect_contacts`

- PK `(tenant_id, contact_id)`；composite FK 到 account；索引
  `(tenant_id, account_id, created_at, contact_id)`。
- 列：IDs、full_name、role_title、language、created_at；可选字符串 nonblank。

### 5.3 `contact_points`

- PK `(tenant_id, contact_point_id)`；composite FK 到 contact；unique
  `(tenant_id, kind, value_hash)`；账户查询通过 contact join，不能冗余未受约束的
  account_id。
- 列：IDs、kind、value、value_hash、verification_status、verified_at、
  verification_provider、enrichment_cost_note、created_at。
- CHECK：kind/status 枚举；hash 64-lower-hex；`verified` 与 `verified_at` 成对；
  初始/状态转换由服务约束，数据库保证最终行一致。

### 5.4 `contact_legal_basis`

- PK/FK `(tenant_id, contact_point_id)`，删除联系方式时 cascade；列为 §3 DTO 的
  全部依据字段。
- CHECK：枚举、UTC timestamptz、核心非空、LI 与 assessment_ref 的蕴含关系。

### 5.5 `prospecting_erasure_suppressions`

- PK `(tenant_id, value_hash)`；列 `erased_at`；hash shape CHECK。
- trigger 禁止 UPDATE/DELETE；只允许 `ON CONFLICT DO NOTHING` 追加。

所有 PK/FK/unique 都含 tenant；所有查询显式 tenant predicate；仓储对象绑定 tenant，
入参越界抛 `TenantIsolationViolation` 并只记录安全 action 和绑定 tenant。

## 6. Repository、UoW 与事务语义

- `ProspectingUnitOfWork` 暴露 `accounts`、`contacts`、`bus`；SQLAlchemy UoW 与
  `PostgresEventBus` 共用一个 session。
- Account repository 提供原子 `add -> bool`、get、find_by_domain、search_by_name。
- Contact repository 提供 add_contact、原子 add_contact_point+legal_basis、get、
  verification 行锁转换、verified-by-account 查询、hash suppression 检查与 erasure。
- 同一联系方式的并发新增只在 contact、canonical value、法律依据和成本备注语义完全
  一致时返回既有 ID；任一不同都抛安全冲突错误，不把地址悄悄改挂到另一个联系人。
- service 所有校验先于开 UoW；业务写入与 outbox 同事务。bus 写失败必须回滚状态
  转换；数据库提交失败不得被解释为成功。
- 外键不存在、重复联系方式但语义冲突、跨租户不可见均 fail closed，使用固定安全
  错误摘要。

## 7. 验收与后续接线条件

必须证明：

1. 迁移与 ORM 表/约束 parity，upgrade/downgrade/upgrade 往返通过；
2. 同域名 20 路并发只产生一个 account；同联系方式 20 路并发只产生一条记录；
3. 跨租户同域名/同 value hash 可并存且互不可见；
4. 缺法律依据、LI 缺 assessment、risky/unverified、已删除 hash 全部 fail closed；
5. 验证首次转换只有一个业务状态和一个 metadata-only outbox；重复、并发与 bus
   失败分别证明幂等和回滚；
6. 删除后原值不在四张业务表、日志、异常、outbox、erasure 表中，再添加被拒绝；
7. boundary、ruff、mypy、全量 pytest、API schema/web 门禁无回归。

完成本切片后，`account_discovery` 才能安全落真实企业/联系人；Demand Radar API 才能
通过 prospecting 公共 View 获取 `account_name`，不得从 demand 表伪造。
