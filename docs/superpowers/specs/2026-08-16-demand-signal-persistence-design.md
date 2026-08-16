# Demand Signal Persistence Design（capture + discard 最小切片）

> 输入：只读审计报告（2026-08-16）+ 用户对「DemandSignal 单表、租户隔离、Provenance、去重、capture_signal 与 discard_signal 最小切片」方向的口头批准（视为设计方向批准）。
> 本规格是后续实施计划的输入；本文件**单独先提交**，再写实施计划。
> 约定：每节标注「已有契约」= 代码/文档既有事实；「本次纠偏」= 本规格做出的确定性决策（含对审计报告中已被否定方案的修正）。

## 1. 决策记录

- **方案 B（capture + discard 状态闭环）被选中**；A（capture-only）与 C（更大 aggregate）的比较见 §11。
- 审计报告中的 `page_hash` 可空去重 tuple 方案**被否定**，原因见 §3（NULLS NOT DISTINCT 会错误合并独立非网页来源；默认 NULL distinct 又无法幂等重放）。
- 审计发现 `DemandSignalCaptured` **尚未注册进 `infra/db/outbox.py` 的 `EVENT_REGISTRY` 发布白名单**（事实：`infra/db/outbox.py:58-78` 现含 16 个事件，无 DemandSignalCaptured）——本切片必须新增该白名单条目（一行注册，不改事件 schema）。

## 2. 范围与边界（已有契约 vs 本次纠偏）

| 项 | 已有契约（事实） | 本次纠偏（决策） |
|---|---|---|
| `SignalCaptureRequest`（`domains/demand/schemas.py:20-40`） | signal_type/entity_name/raw_observation/observed_at/source_type 必填；possible_need/source_url/page_hash 可空 | **新增必填 `source_id: str` 与 `extracted_by: str`**（公共 DTO 扩展，见 §3）；当前无任何调用方（事实：全库零引用），方法签名不变 |
| `DemandSignal` 模型（`domains/demand/models.py:85-103`） | 字段固定（signal_id/tenant_id/signal_type/entity_name/raw_observation/provenance/observed_at/status/possible_need/account_id/discard_reason） | **不改模型** |
| `DemandSignalRepository`（`domains/demand/repository.py:20-55`） | add/get/find_duplicate(tenant, entity, type, page_hash\|None)/list_unlinked 骨架签名 | **骨架契约纠偏**：`add` 返回 `bool`；`find_duplicate` 改为 (tenant, entity_name, signal_type, source_type, source_id)；新增原子 `discard` 方法（§6）；无现有实现/调用方（事实） |
| `DemandService`（`domains/demand/service.py:36-56`） | capture_signal/discard_signal 签名与 docstring 骨架 | **仅 docstring 更新**（去重 key、状态语义）；签名不变 |
| `DemandSignalCaptured`（`shared/events/catalog.py:60-70`） | 字段：signal_id/entity_name/signal_type/source_url + DomainEvent 基类；metadata-only | **不改 shared 事件**；仅注册进 EVENT_REGISTRY |
| `DemandSignal.evidence_level`（`models.py:119-135`） | 骨架 NotImplementedError | **本切片不调用、不实现、不持久化**（假设形成切片现算；硬边界 3：代码推导不存模型输出） |

## 3. Provenance 输入缺口修复（§1 要求）

- `SignalCaptureRequest` 新增两个必填字段（置于可选字段之前）：
  - `source_id: str` —— 来源身份：网页类 = 页面哈希；非网页类 = message/upload/provider/employee-input 记录 identity，由调用方提供。
  - `extracted_by: str` —— 提取者（模型版本标识或 "human"；Provenance docstring 要求具体版本，不写裸 "model"）。
- 服务注入 UTC `now` 作为 `Provenance.extracted_at`（service_impl 的 `_now()`，沿用 `_validate_now` 契约：naive / 非 UTC offset → 固定 `ValidationError("服务时钟必须为 UTC")`）。
- `confirmed_by` / `confirmed_at`：capture 初始均为 None（未人工确认）；请求不扩展这两个字段。
- `source_type` 仍是公共 DTO 的 `str`：服务精确 `SourceType(source_type)` 转换，非法值 fail-closed；`signal_type` 同理精确 `SignalType(signal_type)` 转换。
- **WEB_PAGE 强约束**：必须 `source_url` 与 `page_hash` 非空，且 `source_id == page_hash`（Provenance 文档明确网页 `source_id` 即页面哈希；`shared/schemas/provenance.py` 的 `__post_init__` 只保证 url+hash 成对，source_id==page_hash 的一致性由本服务层强制）。
- 缺网页证据（WEB_PAGE 但缺 url/hash，或 source_id != page_hash）→ 抛既有 `MissingWebEvidenceError`（`domains/demand/errors.py:22`），固定摘要、不回显输入。
- 本次纠偏理由：这是必要公共 DTO 扩展；当前无调用方，签名方法不变，不破坏任何既有代码（事实核验：全库无 `SignalCaptureRequest` 构造点）。

## 4. 并发幂等 key 纠偏（§2 要求）

- **去重 identity（dedup key）= `(tenant_id, entity_name, signal_type, source_type, source_id)`**，五列全部非空。
- 原审计方案（`page_hash` 可空 tuple）被否定的精确理由：
  - 若 `NULLS NOT DISTINCT`：两个不同的非网页来源（如两条不同 message）page_hash 都为 NULL → 被错误合并为同一信号，破坏「独立证据」语义（`derive_confidence` 规则 2 靠独立证据数上浮）。
  - 若默认 NULL distinct：同来源重放（网络重试/Worker 重启）无法命中冲突 → 无法幂等。
  - 来源身份 `source_id`（网页=页面哈希，非网页=message/upload/provider/input identity）才符合「同一观察只算一次」的独立证据判据。
- **DB 层**：普通 `UniqueConstraint("tenant_id","entity_name","signal_type","source_type","source_id", name="uq_demand_signals_source_identity")`（全非空列，无 NULLS NOT DISTINCT）。
- **写入**：`pg_insert(...).on_conflict_do_nothing(constraint="uq_demand_signals_source_identity")`；`rowcount > 0` → `True`（新插入）；`False` → **同事务内** `find_duplicate(tenant_id, entity_name, signal_type, source_type, source_id)` 重读胜者 → 返回其 `signal_id`（ingest_inbound「add → 冲突后重读胜者」先例）。**绝不先查后插**（TOCTOU 消除）。
- 重复 capture：返回既有 ID、**不重复发布事件**、**首条内容保留**（重放带不同 raw_observation 时原行不变——有测试证明）。
- `entity_name` / `signal_type` / `source_type` / `source_id`：fail-closed 校验后**精确匹配存储，不 trim、不 normalize**（external_message_id 精确匹配先例）。

## 5. 单表 demand_signals（§3 要求）

### 5.1 列映射（**共 18 列：10 个模型字段 + 8 个 Provenance 展开列**；列长对齐 `provenance_records` 先例 `infra/db/tables.py:1424-1452`）

| 列 | 类型 | 空 | 来源 |
|---|---|---|---|
| tenant_id | String(32) | 否 | 模型 |
| signal_id | String(32) | 否 | 模型（`new_id("sig")` = 30 字符 ≤32） |
| signal_type | String(40) | 否 | 模型枚举值 |
| entity_name | String(200) | 否 | 模型（**明确标注的最小存储决定**：对齐 provenance_records.source_id 先例；文档无权威长度） |
| raw_observation | Text | 否 | 模型；域事实短文本可随行；网页全文/文件本体仍在 artifact-store，仅 source ref/hash |
| observed_at | DateTime(timezone=True) | 否 | 模型 |
| status | String(32) | 否 | 模型枚举值，默认 'captured' |
| possible_need | Text | 可 | 模型；自由文本推断，非结论 |
| account_id | String(32) | 可 | 模型；初始 None |
| discard_reason | Text | 可 | 模型；仅 discarded 非空 |
| source_type | String(32) | 否 | Provenance 展开 |
| source_id | String(200) | 否 | Provenance 展开 |
| extracted_by | String(64) | 否 | Provenance 展开 |
| extracted_at | DateTime(timezone=True) | 否 | Provenance 展开（服务注入 UTC now） |
| confirmed_by | String(32) | 可 | Provenance 展开；capture 初始 None |
| confirmed_at | DateTime(timezone=True) | 可 | Provenance 展开；capture 初始 None |
| source_url | String(2000) | 可 | Provenance 展开；WEB_PAGE 非空 |
| page_hash | String(200) | 可 | Provenance 展开；WEB_PAGE 非空 |

- **无 created_at**（模型没有）；**无 evidence_level / confidence 列**（硬边界 3）。
- repo roundtrip 必须完整保留 confirmed pair 的 None 值（capture 初始即 None，测试断言往返不变）。

### 5.2 约束（**共 8 个 CHECK**；ORM `CheckConstraint` 与迁移 `op.create_table` 必须逐一同名同语义，parity 计划覆盖全部 8 个）

```text
PK      pk_demand_signals                     (tenant_id, signal_id)
UNIQUE  uq_demand_signals_source_identity     (tenant_id, entity_name, signal_type, source_type, source_id)
CHECK   ck_demand_signals_type                signal_type IN (SignalType 全 21 枚举值)
CHECK   ck_demand_signals_status              status IN (SignalStatus 全 3 枚举值：
                                               'captured','linked_to_hypothesis','discarded')
CHECK   ck_demand_signals_source_type         source_type IN (SourceType 全 6 枚举值)
CHECK   ck_demand_signals_confirmed_pair      (confirmed_by IS NULL) = (confirmed_at IS NULL)
CHECK   ck_demand_signals_web_evidence        source_type <> 'web_page' OR
                                               (btrim(source_url) <> '' AND btrim(page_hash) <> ''
                                                AND source_id = page_hash)
CHECK   ck_demand_signals_discard_reason      (status = 'discarded') =
                                               (discard_reason IS NOT NULL AND btrim(discard_reason) <> '')
CHECK   ck_demand_signals_core_nonblank       btrim(tenant_id) <> '' AND btrim(signal_id) <> ''
                                               AND btrim(entity_name) <> '' AND btrim(raw_observation) <> ''
                                               AND btrim(source_id) <> '' AND btrim(extracted_by) <> ''
CHECK   ck_demand_signals_optional_nonblank   (possible_need IS NULL OR btrim(possible_need) <> '')
                                               AND (source_url IS NULL OR btrim(source_url) <> '')
                                               AND (page_hash IS NULL OR btrim(page_hash) <> '')
```

- 全部时间列 timestamptz（`DateTime(timezone=True)`）。
- 枚举口径（事实）：当前 `SignalType` = **21**、`SourceType` = **6**、`SignalStatus` = **3**；CHECK 字面量由迁移常量从枚举生成，实施时以枚举为准。
- `raw_observation` / `possible_need` / `discard_reason` 不进 outbox / log / error（生产内容约束，测试 marker 验证）。

## 6. Repository / UoW / service 组织（§4 要求）

### 6.1 契约（`domains/demand/repository.py`）

```text
DemandSignalRepository（纠偏后骨架契约，无现有实现/调用方）：
  async def add(self, signal: DemandSignal) -> bool            # True=新插入；False=来源身份冲突（不入库）
  async def get(self, tenant_id, signal_id) -> DemandSignal | None
  async def find_duplicate(self, tenant_id, entity_name, signal_type,
                           source_type, source_id) -> DemandSignal | None
  async def discard(self, tenant_id, signal_id, reason) -> DemandSignal | None
      # tenant-bound SELECT ... FOR UPDATE，返回**转换前快照**（snapshot）：
      # - 不存在 → None
      # - CAPTURED → 持锁事务内先捕获 snapshot（status=captured），再同事务
      #   UPDATE status='discarded', discard_reason=reason，返回该 snapshot
      # - DISCARDED / LINKED_TO_HYPOTHESIS → 不改动，返回当前 snapshot
      # 并发安全由 FOR UPDATE 行锁保证（不同 reason 后到者读到已 discarded 行）。
      # 调用方（service）只用 snapshot 判定，不得把返回对象当作 DB 当前态
      # （status=captured 的快照不代表行仍为 captured——DB 已被本调用更新）。
  async def list_unlinked(self, tenant_id, limit) -> list[DemandSignal]   # 契约保留

DemandUnitOfWork（新 Protocol，同文件）：
  signals: DemandSignalRepository
  bus: EventBus
  async __aenter__ / __aexit__（同事务提交/回滚）
```

**为何返回「转换前快照」即可区分三种结果、无需额外枚举**：service 判定完全
基于 snapshot——`None` = 不存在；`status=captured` = 本次完成首次转换（DB
已更新为 discarded + reason）；`status=discarded` 且 reason 与传入相同 = 幂等
no-op；`status=discarded` 且 reason 不同 = 冲突（拒绝覆盖）；`status=linked`
= 拒绝。DB 当前态只由 status/discard_reason 列承载，返回值不是 DB 状态代理，
因此不需要第三方结果枚举。**失败/异常**：repo 抛错（约束/锁/连接异常）→ UoW
`__aexit__` 回滚整个事务（含已执行的 UPDATE 与 outbox 写入），调用方收到异常，
不存在部分状态。

- **list_unlinked 的类型检查安全选择**：concrete 实现（`infra/db/repositories/demand.py`）显式定义 `list_unlinked` 并 `raise NotImplementedError`（签名与 Protocol 完全一致，mypy 通过），docstring 注明「本切片不实现：无假设消费者，不发明排序/limit 语义」。不静默省略（省略会让结构检查在运行时失败且掩盖契约缺口）。

### 6.2 实现（`infra/db/repositories/demand.py` 新文件）

- `DemandSignalRepositoryImpl`，镜像 `_ConversationsRepository` 先例（`infra/db/repositories/conversations.py:45-62`）：`_tenant_matches`/`_require_tenant`，违规 → `TenantIsolationViolation` + 既有 critical 审计日志模式（只记 action/绑定租户，不记输入内容）。
- 所有查询显式 tenant 过滤（硬边界 8）。

### 6.3 UoW（`infra/db/demand_uow.py` 新文件）

- `SqlAlchemyDemandUnitOfWork`，镜像 `conversations_uow`：每次进入新建 session；`signals = DemandSignalRepositoryImpl(session, tenant_id)`；`bus = PostgresEventBus(session, tenant_id, now=...)`；退出统一 commit/rollback/close。

### 6.4 service（`domains/demand/service_impl.py` 新文件）

- `DemandServiceImpl(uow_factory, *, now)`，**只实现 `capture_signal` 与 `discard_signal`**（浅域先例：ConversationServiceImpl 只实现 Protocol 子集；其余方法不定义，保持 Protocol 骨架）。
- 依赖方向：`domains/demand → shared` 仅；infra 独立层。

## 7. Capture 语义与校验（§5 要求）

**校验全部在开 UoW 前完成**（固定中文 `ValidationError` 摘要，不回显输入）：

**统一输入规则（本次纠偏定稿）**：所有提供的 str 输入
（`tenant_id` / `source_id` / `signal_type` / `entity_name` /
`raw_observation` / `extracted_by` / `possible_need` / `source_url` /
`page_hash`）一律要求 `item == item.strip()`——**拒绝任何首尾空白**；
通过校验后**原值精确存储与比较，不 trim、不 normalize**（external_message_id
精确匹配先例）。`signal_type` / `source_type` 用枚举精确转换（非法值
`ValidationError`）；WEB 的 `source_id == page_hash` 精确等值比较。

```text
tenant_id        str、strip 非空、item == item.strip()、len <= 32     → "信号租户无效/超长"
source_id        str、strip 非空、item == item.strip()、len <= 200    → "信号来源无效/超长"
signal_type      str、精确 SignalType(signal_type)（strip 后精确值）   → "信号类型无效"
entity_name      str、strip 非空、item == item.strip()、len <= 200    → "信号企业名无效/超长"
raw_observation  str、strip 非空、item == item.strip()（Text 无自造 max）→ "信号观察内容无效"
extracted_by     str、strip 非空、item == item.strip()、len <= 64     → "信号提取者无效/超长"
possible_need    非 None 时同 raw_observation 合法文本                → "信号可能需求无效"
source_type      str、精确 SourceType(source_type)                   → "信号来源类型无效"
source_url       非 None 时 str、strip 非空、item == item.strip()、
                 len <= 2000                                          → "信号来源 URL 无效/超长"
page_hash        非 None 时 str、strip 非空、item == item.strip()、
                 len <= 200                                           → "信号页面哈希无效/超长"
observed_at      必须 datetime、UTC aware（naive/非 UTC → 拒）        → "信号观察时间必须为 UTC"
now（服务时钟）   同 _validate_now 契约                                → "服务时钟必须为 UTC"
WEB_PAGE         必须 source_url+page_hash 非空 且 source_id == page_hash（精确等值）
                 缺 → MissingWebEvidenceError（固定摘要，不回显）
```

- 构造一次 `DemandSignal`（status=CAPTURED、account_id=None、discard_reason=None、provenance 完整：source_type/source_id/extracted_by/extracted_at=now/confirmed_by=None/confirmed_at=None/source_url/page_hash）。
- `await uow.signals.add(signal)`：`True` → 同事务 `uow.bus.publish(DemandSignalCaptured(...))`（occurred_at=now、run_id=None、metadata-only 现有 schema）；`False` → 同事务 `find_duplicate` 重读胜者返回其 signal_id，**不发布**。
- 业务插入 + outbox 同事务原子（UoW commit；bus 失败 → 整事务回滚，业务行不落——测试用同 session wrapper 注入失败证明）。
- 返回 `str(signal_id)`（service.py 契约返回 str）。

## 8. Discard 状态语义（§6 要求）

- `tenant_id` / `signal_id` / `reason` 在开 UoW 前校验：str、strip 非空、无首尾空白；signal_id len<=32；reason Text 无自造 max（固定摘要）。
- `await uow.signals.discard(tenant_id, signal_id, reason)`：
  - 返回 `None`（不存在 / 跨租户不可见）→ `ValidationError("需求信号不存在")`（统一固定摘要；跨租户视为不存在，不抛 TenantIsolationViolation——那是仓储参数越界语义）。
  - 返回行 status == `LINKED_TO_HYPOTHESIS` → `InvalidStateTransition("已关联假设的信号不可丢弃")`（保护证据链；`InvalidStateTransition` 为既有共享错误）。
  - status == `DISCARDED`：
    - `row.discard_reason == reason` → **幂等 no-op**（返回 None，成功）。
    - 不同 reason → `InvalidStateTransition("丢弃原因冲突，拒绝覆盖")`（**不覆盖 first reason**）。
  - status == `CAPTURED` → repo 已置 DISCARDED 并写入 reason，成功返回。
- **不发布 discard 事件**（catalog 无 schema；不改 shared——诚实标注为最小语义决策）。
- 不记录输入日志（reason 不进日志；caplog 测试验证）。
- 并发：不同 reason 一胜一固定冲突（后到者 FOR UPDATE 读到已 discarded 行 → InvalidStateTransition）；同 reason 皆成功且仅单一 reason 落库。

## 9. 测试与原子性（§7 要求）

### 9.1 迁移测试（`tests/integration/test_migrations.py` 增补）

- 新迁移 `0021_demand_signals.py`，`down_revision = "0020"`，downgrade drop 表。
- 所有「当前 head = 0020」的陈述/断言**诚实更新为 0021**（revision 字面量与 RED 文案），**保留 0020 专项测试语义**（0020 表契约/往返不变）。
- `EXPECTED_TABLES` 增补 `demand_signals`。
- 0021 契约测试：DB↔ORM 列/类型/PK/UNIQUE/CHECK 语义 parity（沿用 0020 契约测试模式，CHECK 语义与枚举集比对；**覆盖全部 8 个 CHECK**：3 个 enum、confirmed pair、web evidence、discard reason、core nonblank、optional nonblank）；0021→0020→0021 downgrade roundtrip。

### 9.2 集成测试（`tests/integration/test_demand_signals.py` 新文件，真实 PostgreSQL + 真实 UoW + 真实仓储，零 mock）

1. roundtrip：capture 落库，全列（含 provenance 展开列、confirmed pair None、possible_need/account_id/discard_reason None）往返一致。
2. web 证据：WEB_PAGE 缺 url/hash、source_id != page_hash → `MissingWebEvidenceError` 固定摘要；合法 WEB 信号落库且 source_id==page_hash。
3. 非网页：两个不同 `source_id`（两条不同 message）同 entity/type → **两独立行、两事件**（独立证据语义）。
4. 同来源串行重放：同 key 第二次 → 返回首条 signal_id、1 行、**1 事件**、首条 raw_observation 保留（重放带不同内容不覆盖）。
5. 并发：`asyncio.gather` 两个独立 UoW 同 key → 恰 1 行 1 事件、两结果 signal_id 相同。
6. 跨租户：A/B 同 (entity,type,source_type,source_id) → 各 1 行（不互相去重）。
7. 可选字段：possible_need None / 非 None 两态往返。
8. DB 约束：status/signal_type/source_type 非法值、confirmed pair 不同步、WEB 行 url/hash 缺失或 source_id != page_hash、非 discarded 带 reason、discarded 无 reason 或全空白 reason、**core 列（tenant_id/signal_id/entity_name/raw_observation/source_id/extracted_by）全空白**、**optional 文本（possible_need/source_url/page_hash）非 NULL 但全空白** → 违反对应 CHECK 失败（覆盖全部 8 个 CHECK）。
9. repo 租户越界：以不匹配绑定租户调 add/get/find_duplicate/discard → `TenantIsolationViolation`。
10. capture 输入校验先于 UoW：空白/超长/非法枚举/naive observed_at/非 UTC now → 固定摘要（即使表不存在也先于 DB 路径）。
11. outbox 内容：`DemandSignalCaptured` payload 键 ⊆ 现有 schema；**生产内容 marker（raw_observation/possible_need/provenance 值）不得出现在 outbox/log/error**（caplog 零记录断言）。
12. 原子性：同 session wrapper 注入 bus 失败 → 整个 UoW 回滚，业务行不落库。
13. discard 全状态：captured→discarded 落 first reason；同 reason 幂等；不同 reason 固定冲突且 first reason 保留；linked 拒绝；跨租户不可见 → "需求信号不存在"；并发同 reason 双成功单 reason、并发不同 reason 一胜一冲突。
14. caplog：capture/discard 全程零日志；TenantIsolationViolation 的审计日志不含输入内容（只含 action/绑定租户）。
15. discard snapshot 语义与回滚：repo 返回**转换前快照**（captured 快照返回后 DB 已为 discarded+reason；service 不得把快照当 DB 当前态）；注入失败 → 整个 UoW 回滚，行保持 captured 且无 reason、outbox 不变。

- 仅失败注入可 mock（bus 用同 session wrapper）；业务行为一律真实对象。

### 9.3 纪律

- 分阶段 RED→GREEN（capture → discard → 迁移/约束 → 事件/原子性），每阶段 mutation proof（去重 key 改错、ON CONFLICT 去掉、discard 覆盖 first reason、FOR UPDATE 去掉、事件在重复时发布）后恢复。
- 完整门禁（前台、pipefail 真实 rc、timeout>=1500000ms）：ruff/mypy 全域/boundaries/sensitive/全库 non-e2e/web gen:api+typecheck+lint+test+build/diff-check/e2e。
- 新文件 `git add --chmod=-x` 全部 100644；AppleDouble 清理归零后才暂存；单 commit + push + exact-HEAD CI + 独立 pre/post review。

## 10. 文件地图（§8 要求）

```text
domains/demand/schemas.py                  SignalCaptureRequest 增 source_id/extracted_by（必填）
domains/demand/service.py                  capture/discard docstring 更新（去重 key、状态语义）；签名不变
domains/demand/repository.py               DemandSignalRepository 契约纠偏（add→bool、find_duplicate 新签名、discard 新增）
                                          + 新 DemandUnitOfWork Protocol
domains/demand/service_impl.py             【新】DemandServiceImpl（仅 capture_signal/discard_signal）
infra/db/tables.py                         【增】DemandSignalRow（§5 列 + 约束）
infra/db/repositories/demand.py            【新】DemandSignalRepositoryImpl（含 list_unlinked NotImplementedError 桩）
infra/db/demand_uow.py                     【新】SqlAlchemyDemandUnitOfWork
infra/db/outbox.py                         【增】EVENT_REGISTRY 注册 DemandSignalCaptured（一行 + 注释）
migrations/versions/0021_demand_signals.py 【新】迁移（down_revision="0020"）
tests/integration/test_demand_signals.py   【新】集成测试（§9.2）
tests/integration/test_migrations.py       head 0020→0021 更新 + EXPECTED_TABLES + 0021 契约/往返测试
docs/superpowers/plans/2026-08-16-demand-signal-persistence.md   【后续】实施计划（本规格单独先提交）
```

- 文件计数（诚实口径）：§10 共 **12 个路径**——实现/代码/测试/迁移/既有文档 **11 个** + 后续计划文档 **1 个**（第 12 个）；本规格文件本身不计入。
- 不改：`domains/demand/models.py`、`shared/`（事件 schema/identifiers/provenance）、`apps/`、UI、artifact-store blob 写入。

## 11. 方案 A/B/C 比较

| 维度 | A capture-only | B capture+discard（**选中**） | C 更大 aggregate |
|---|---|---|---|
| 范围 | 表+迁移+repo(capture)+service capture_signal+事件 | A + discard 状态机（FOR UPDATE + CHECK 约束 + 幂等/冲突语义） | 信号+假设+已验证需求+簇 全部持久化 |
| 文件 | ≈10 处（与 B 同基建——表/迁移/UoW/repo/service/事件/两测试文件；仅少 discard repo 方法、discard 状态测试与 CHECK 中 discard 分支） | ≈11 处实现+测试变更 + 1 计划文档（第 12 路径；同一迁移同一 UoW） | 多表多迁移多 UoW（≥3 表、≥3 迁移） |
| 独立可验收 | 是 | 是 | 否（依赖 `evidence()` 仓储设计未知、can_promote_to_validated、prospecting、员工确认流程） |
| 风险 | status 列无消费者、discard_signal 骨架悬空、状态机半开、与已批准方向不符 | 三个消歧决策（linked 拒绝、重复幂等 no-op、不发 discard 事件）需复审确认 | 整片 HANDBOOK 切片 7，远超「最小未阻塞」 |
| 结论 | 不选（不完整） | **选**（与批准方向一致、闭环、单表单迁移单 UoW） | 不选（过大） |

## 12. 明确不做（§9 要求）

- ❌ hypothesis / validated need / cluster 持久化；`NeedHypothesis.evidence()`/`can_promote_to_validated()`、`DemandSignal.evidence_level` 映射
- ❌ `list_unlinked` 业务实现（concrete 桩 NotImplementedError，docstring 说明）
- ❌ account_id 关联（prospecting 域）、`ReplyReceived` 订阅处理（extract_need）
- ❌ workflow / qualification 措辞 / 生产模型 provider（ADR 未决，与本切片无关）
- ❌ 修改 `shared/` 事件 schema；artifact blob 写入；API wiring；UI
- ❌ 字段值业务校验（presence/长度按 §7，不发明 value 语义）

## 13. 九条硬边界逐项（§10 要求）

| # | 边界 | 本切片适用性 |
|---|---|---|
| 1 | 模型永不接触凭证 | 适用：capture 输入无凭证；marker 测试证明不进 outbox/log/error |
| 2 | 金额只用 Decimal | 不适用（本切片无 Money 字段） |
| 3 | 置信度代码推导 | 适用：无 confidence 列；evidence_level 不实现不持久化 |
| 4 | Provenance | 适用：Provenance 全字段展开列落库；网页 source_id==page_hash 强约束；confirmed pair 成对 |
| 5 | 事实与推断分离 | 适用：raw_observation=事实；possible_need=推断自由文本，模型字段已区分，不合并 |
| 6 | 未验证联系人不得发送 | 不适用（无发送） |
| 7 | 只有 quoted 价格进客户可见报价 | 不适用 |
| 8 | 所有表 tenant_id + 查询租户过滤 | 适用：PK(tenant,id)、UNIQUE 含 tenant、全部查询显式 tenant、repo 参数越界 TenantIsolationViolation、服务跨租户视为不存在 |
| 9 | 依赖方向单一 | 适用：domains/demand → shared 仅；infra 独立；零跨域 import |

## 14. 自查

- **placeholder**：全文无 TBD/TODO/待定；消歧决策均给出确定结论与理由（§3/§4/§8）。
- **dedup key 一致性**：§4 契约（5 列）＝§5 UNIQUE 约束＝§6 find_duplicate 签名＝§7 校验后精确存储；无 page_hash 可空 tuple 残留。
- **状态机一致性**：§8 四态（CAPTURED/DISCARDED 幂等与冲突/LINKED 拒绝/不存在）与 §5 CHECK `ck_demand_signals_discard_reason`、repo.discard 返回语义、并发行为一一对应。
- **字段映射一致性**：§5 表列 **18 列**（10 模型 + 8 Provenance 展开）↔ 模型字段 + Provenance 展开列一一对应；全文无 17/其他列数；无 created_at/evidence_level/confidence 列（模型无、边界 3）。
- **CHECK 计数一致性**：§5.2 共 **8 个 CHECK**；§9.1 parity 计划与 §9.2 第 8 项测试均覆盖全部 8 个（enum×3、confirmed pair、web evidence、discard reason、core/optional nonblank）。
- **文件范围**：§10 共 **12 个路径**（11 实现+测试变更 + 1 计划文档），无 models/shared/apps/UI 改动；EVENT_REGISTRY 一行注册已含。
- 迁移 head 链：0020 → 0021（down_revision="0020"），downgrade drop；EXPECTED_TABLES 同步。
