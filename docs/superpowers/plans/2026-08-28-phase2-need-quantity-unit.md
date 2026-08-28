# Task 3A：客户数量单位事实 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 控制器自检后交实现代理；编写 brief 不执行代码、不派代理。

**Goal:** 给已有 Validated Need 增加可空客户单位事实，原子绑定当前数量来源与人工确认记录，为 T3 提供完整可冻结事实。
**Architecture:** 新增独立 NeedUnitServiceImpl；权限和客户消息证据由注入 Protocol 提供；Gateway 原文读取在所有持锁区间外。单位/绑定/确认/幂等/历史同一 demand 事务提交。
**Tech Stack:** Python 3.12+、Pydantic v2、SQLAlchemy 2.x、Postgres；不加依赖。
**Spec:** `docs/superpowers/specs/2026-08-28-phase2-costing-quotation-design.md` §十一实施核对补充；属于主计划T3的前置子切片，不扩大业务授权。

## Global Constraints

- 所有记录有 tenant_id；外键采用 tenant+ID；仓储查询强制租户过滤。
- 一个报价版本对应一个产品规格和数量档、一张成本表、一个报价币种。
- 保留旧成本 API/readiness、已有研究 v1/v2 与 Campaign 工作流；不改 Tool Gateway 核心检查管线，不跨域读取私有 model/repository。
- 不改旧完整度/寻源门槛、DemandService 晋升/更新 API 或旧 promotable/mutable 模型字段词表；不改 prompt。
- 无默认单位、箱件换算、假客户来源、force 或客户端确认人；供应商报价单位不能证明客户数量单位；确认单位不批准商业承诺。

## 0. 前置、文件与迁移

工作树 `/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-costing-quotation`。T2 提交交接后执行本切片，再执行 T3；不与 T2 同时修改 tables/迁移测试。先读根 AGENTS/HANDBOOK、domains/demand/infra/shared/tests 的上级及就近 AGENTS、规格与本 brief。所有新增方法完整类型及中文 docstring。

迁移链：T2 `0041` → **单位 `0042`** → 冻结 `0043` → 报价 `0044` → PDF `0045`。本任务不改其他任务实现；后续编号由控制器同步。

| 操作 | 精确文件 | 职责 |
|---|---|---|
| Create | `domains/demand/unit_service.py` | 新服务实现，不膨胀旧 service_impl |
| Create | `domains/demand/unit_facts.py` | 纯 hash、绑定检查，无 IO |
| Create | `domains/demand/unit_repository.py` | 专用 Repository/UoW Protocol，不扩旧 UoW |
| Modify | `domains/demand/models.py` | ValidatedNeed 新增三个可空字段 |
| Modify | `domains/demand/schemas.py`、`service.py`、`errors.py` | 新 DTO/公共端口/固定错误，旧词表不变 |
| Create | `infra/db/need_unit_uow.py`、`infra/db/repositories/need_units.py` | 新事务与持久适配，严格原始 JSON 解码 |
| Modify | `infra/db/repositories/need_hypotheses.py`、`infra/db/tables.py` | 旧读写保留新字段，ORM/约束 |
| Create | `migrations/versions/0042_need_quantity_unit.py` | down_revision=0041，增量迁移 |
| Create | `tests/unit/test_need_units.py`、`tests/integration/test_need_units.py`、`tests/integration/test_need_unit_migration.py` | 行为、真实 DB、迁移往返 |
| Modify | `tests/integration/test_migrations.py` | head=0042 与新增表约束，保留旧断言 |
| Modify | `domains/demand/AGENTS.md`、`docs/adr/0018-costing-quotation-contracts.md` | 追加契约和兼容纪律，不放宽硬边界 |

T3A 不实现真实 Gateway reader、HTTP/UI、PDF 或生产装配；权限/来源注入受控实现，真实数据库验收。T8 负责真实装配，不能把 fake 注册生产。

## 1. 完整类型与接口

### DTO（全部在 demand.schemas）

新 DTO 均为 Pydantic，`ConfigDict(strict=True,frozen=True,extra="forbid")`；FactualField/Provenance/Money 复用 shared，不复制。ID 类型复用 shared identifiers；确认 ID 为 `str`，用 `new_id("nuc")`。`NeedUnitAction=Literal["read","confirm"]`。

| 类型 | 精确字段 |
|---|---|
| NeedUnitConfirmationCommand | `unit:str, source_message_id:MessageId, locator:str, source_quote:str, expected_quantity_fact_hash:str, expected_unit_confirmation_id:str\|None`；最后字段必填、初次为 None |
| NeedUnitAccess | `tenant_id:TenantId, need_id:ValidatedNeedId, opportunity_id:OpportunityId, account_id:ProspectAccountId, actor_id:EmployeeId, authorization_ref:str` |
| NeedUnitEvidenceQuery | `tenant_id:TenantId, need_id:ValidatedNeedId, account_id:ProspectAccountId, actor_id:EmployeeId, quantity:FactualField[int], quantity_fact_hash:str, unit:str, source_message_id:MessageId, locator:str, source_quote:str` |
| VerifiedNeedUnitEvidence | `tenant_id:TenantId, need_id:ValidatedNeedId, account_id:ProspectAccountId, source_message_id:MessageId, artifact_id:ArtifactId, content_hash:str, locator:str, source_quote:str, unit:str, quantity_fact_hash:str, observed_at:datetime` |
| NeedUnitConfirmationView | `tenant_id:TenantId, need_id:ValidatedNeedId, confirmation_id:str, quantity_fact_hash:str, unit:FactualField[str], source:VerifiedNeedUnitEvidence, confirmed_by:EmployeeId, confirmed_at:datetime` |
| NeedQuoteFacts | `tenant_id:TenantId, need_id:ValidatedNeedId, account_id:ProspectAccountId, status:str, product_category:FactualField[str]`；另见下面完整事实字段 |
| NeedUnitStoredConfirmation | `view:NeedUnitConfirmationView, idempotency_key:str, request_hash:str`；内部存储 DTO，不挂 HTTP |

NeedQuoteFacts 的其余字段全部必填但可 None：`application,material,size_spec,packaging,destination,current_supply_issue,certification_required,unit:FactualField[str]|None`；`quantity:FactualField[int]|None, required_by:FactualField[date]|None, target_price:FactualField[Money]|None, unit_quantity_fact_hash:str|None, unit_confirmation_id:str|None`。事实完整带 Provenance，不从旧 NeedFieldView 摘要补造 metadata；不存可伪造的 verified/approved 布尔值或重复派生 hash。

command 不接 quantity 值、actor、tenant、role、确认人/时间/来源类型。字符串非空、无首尾空白；unit≤64、locator≤256、摘录≤4096、ID≤40 字符，禁止控制字符（摘录允许换行）。unit 原样保留，不归一别名或自动换算；`unit/units/unknown/未知/待确认`（casefold）是未明确口径，拒绝。hash 严格64位小写hex。时间必须 aware 并规范 UTC。资源长度是输入保护，不是商业默认。

### 新公开端口（在 demand.service 声明）

```python
class NeedUnitAuthorizer(Protocol):
    async def check(self, tenant_id: TenantId, need_id: ValidatedNeedId,
                    actor_id: EmployeeId, *, action: NeedUnitAction) -> NeedUnitAccess: ...
    def guard(self, tenant_id: TenantId, need_id: ValidatedNeedId,
              actor_id: EmployeeId, *, action: NeedUnitAction
              ) -> AsyncContextManager[NeedUnitAccess]: ...

class NeedUnitEvidenceReader(Protocol):
    async def read_verified(self, query: NeedUnitEvidenceQuery) -> VerifiedNeedUnitEvidence: ...
    async def authorize_reference(self, tenant_id: TenantId, need_id: ValidatedNeedId,
                                  actor_id: EmployeeId, source: VerifiedNeedUnitEvidence) -> None: ...

class NeedUnitService(Protocol):
    async def confirm(self, tenant_id: TenantId, need_id: ValidatedNeedId,
                      command: NeedUnitConfirmationCommand, *, actor_id: EmployeeId,
                      idempotency_key: str) -> NeedUnitConfirmationView: ...
    async def get_facts(self, tenant_id: TenantId, need_id: ValidatedNeedId,
                        *, actor_id: EmployeeId) -> NeedQuoteFacts: ...
    async def get_confirmation(self, tenant_id: TenantId, need_id: ValidatedNeedId,
                               confirmation_id: str, *, actor_id: EmployeeId
                               ) -> NeedUnitConfirmationView: ...
```

构造：`NeedUnitServiceImpl(uow_factory:Callable[[TenantId],NeedUnitUnitOfWork], authorizer:NeedUnitAuthorizer, evidence_reader:NeedUnitEvidenceReader, *, now:Callable[[],datetime])`。无默认依赖。check 为来源读取前即时检查；guard 必须保护当前员工在职/权限与机会范围直至内层 Need UoW 提交，不能只返回过时 allowed。返回 Access 与 tenant/need/actor/account 必须匹配。T8 使用现有成本角色 boss/product/sourcing/finance **并且**机会访问权；来源另验消息读取权，demand 不 import 其他域。

reader 只接受客户入站消息：同租户同客户、真实消息→raw artifact、实际 hash/locator/逐字摘录、所指 quantity-unit 关系均须核验。单独出现 pieces 不证明它修饰当前数量；无法确定返回 source_mismatch，不调用模型猜测。供应商消息/任意上传 ID 不得冒充客户消息。原文通过 Gateway 读取，reader 仅返回上述 typed 证据元数据与必要摘录。authorize_reference 只用持久元数据重验当前消息资料阅读权，不取原文；重放/读取旧receipt前调用，拒绝则permission_denied，不能借幂等重放泄露已撤权来源摘录。

### 纯函数与内部持久接口

unit_facts 实现、service 显式重导出：
`quantity_fact_hash(tenant_id:TenantId,need_id:ValidatedNeedId,quantity:FactualField[int])->str`；
`need_quote_facts_hash(facts:NeedQuoteFacts)->str`；
`require_current_unit(facts:NeedQuoteFacts)->FactualField[str]`。
数量 hash 为 canonical JSON 的 SHA256：版本 `need-quantity-fact-v1`、tenant、need、整数 value、全部 Provenance（含 None）；JSON 固定 sort_keys/separators、UTF-8，aware datetime→UTC ISO8601。拒 bool/float/字符串，不用 int() 修正。0 的历史事实可读取/hash，但确认和 require_current_unit 拒非正数量。完整 hash 版本 `need-quote-facts-v1`，覆盖全部 facts/source/绑定；Money 用 Decimal 字符串、date用ISO，无读取时间。
require_current_unit 检查正整数、quantity/unit 人工确认、unit/confirmation_id 存在、绑定等于当前 quantity hash；不检查供应商成本规则。数量来源任一字段变化，即使值相同也失效。

```python
class NeedUnitRepository(Protocol):
    async def read_facts(self, tenant_id: TenantId, need_id: ValidatedNeedId) -> NeedQuoteFacts | None: ...
    async def lock_facts(self, tenant_id: TenantId, need_id: ValidatedNeedId) -> NeedQuoteFacts | None: ...
    async def find_operation(self, tenant_id: TenantId, need_id: ValidatedNeedId,
                             idempotency_key: str) -> NeedUnitStoredConfirmation | None: ...
    async def get_confirmation(self, tenant_id: TenantId, need_id: ValidatedNeedId,
                               confirmation_id: str) -> NeedUnitConfirmationView | None: ...
    async def add_confirmation(self, tenant_id: TenantId, record: NeedUnitStoredConfirmation) -> None: ...
    async def apply_current_unit(self, tenant_id: TenantId, need_id: ValidatedNeedId,
                                 confirmation: NeedUnitConfirmationView) -> None: ...
    async def append_unit_history(self, tenant_id: TenantId, need_id: ValidatedNeedId,
                                  previous_unit: FactualField[str] | None,
                                  confirmation: NeedUnitConfirmationView) -> None: ...

class NeedUnitUnitOfWork(Protocol):
    units: NeedUnitRepository
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, exc_type: type[BaseException] | None,
                        exc: BaseException | None, tb: object) -> None: ...
```

仓储只解码/租户过滤/存储；lock_facts 为 FOR UPDATE；apply_current_unit 只写三单位列。history 复用旧表，field_name="unit"，前后显示值、真实消息ID、确认人/时间；完整来源永久在确认表。旧 repo 编解码也保留三新字段，不改旧字段提交词表；旧 update 不应抹掉单位。
构造：`NeedUnitRepositoryImpl(session:AsyncSession,tenant_id:TenantId)`；`SqlAlchemyNeedUnitUnitOfWork(session_factory:async_sessionmaker[AsyncSession],tenant_id:TenantId,*,lock_timeout_ms:int,statement_timeout_ms:int)`。超时正整数显式配置，无生产默认；参数化事务级 set_config，不拼接 SQL、不读取环境凭证。
request_hash 使用版本 `need-unit-confirm-request-v1` + tenant/need/actor/command 全字段 canonical JSON；不含 now、reader输出和幂等键。key 1..128、无控制字符/首尾空白；持久仅key/hash，不存原始请求正文。

### 错误（demand.errors；code类型在 schemas）

`NeedUnitError(ValidationError)`、`NeedUnitPermissionError(PermissionDenied)`、`NeedUnitUnavailableError(TradeOSError)` 构造均 `(code:NeedUnitErrorCode)`，.code固定字符串、中文消息查表、不可自动重试，不接受自由异常消息。NeedUnitErrorCode 为下表所有 code 的 Literal 并集。

| code | 类 / T8 HTTP |
|---|---|
| invalid_input, unit_unspecified, quantity_invalid, source_mismatch, source_unsupported | NeedUnitError / 422 |
| need_not_found, confirmation_not_found | NeedUnitError / 404，不暴露其他租户存在性 |
| need_terminal, quantity_changed, unit_changed, idempotency_conflict, unit_missing, unit_stale, fact_unconfirmed | NeedUnitError / 409 |
| permission_denied | NeedUnitPermissionError / 403 |
| source_unavailable, dependency_unavailable, lock_timeout, storage_unknown, facts_corrupt | NeedUnitUnavailableError / 503，未知提交不可声称未写入 |

Pydantic形状错误保留其 ValidationError；内部仓储收到不匹配 tenant 参数沿用 TenantIsolationViolation 与安全审计，不吞成404。不得记录DSN、原文、凭证或原始SQL异常。

## 2. 核心流程、幂等与持久布局

顺序不可变：check权限 → 短UoW读历史幂等/当前facts并关闭 → 已有同键核对hash返回原receipt → 校验数量/旧unitID → reader读来源（零锁）→ 核对返回绑定与请求 → guard当前授权 → 新UoW锁Need → 再查同键 → 重验quantity hash/旧unitID/account/status → 插confirmation → 更新三列 → 插history → **提交UoW** → 退出guard → 返回。
锁顺序：授权guard相关行→Need→确认/历史；原文读取不在guard/UoW中。新确认只允许非fulfilled/withdrawn/lost；当前quantity须已人工确认，否则fact_unconfirmed，仍由旧需求确认路径补证，不在此暗改数量来源。

Provenance：CONVERSATION、真实message ID/摘录，extracted_by=actor ID，extracted_at=confirmed_at=单次注入now，confirmed_by=actor。这是人工入口，不冒充模型提取；source保存artifact/hash/locator/observed_at。业务值与reader绑定任何不一致零写。
同key同payload返回首次receipt，不重写确认时间、不重读原文、不重激活旧unit；当前权限仍check。不同actor/command均hash不同→idempotency_conflict。不同key同expected_unit_confirmation_id并发最多一写，另一unit_changed。旧数量更新后保留原单位/receipt，以hash差异派生unit_stale；同key重放不得恢复有效。新确认需刷新facts、明确新key。
所有失败回滚确认/三列/history；提交不确定storage_unknown，仅同key核对恢复，不换key重试。get_facts/get_confirmation均check当前权限且租户过滤；check须覆盖完整Need事实读取，get_confirmation及同键receipt重放另在锁外authorize_reference。锁内发现并发winner时先退出UoW/guard再authorize_reference并返回，不能在锁里调用原文reader；读取历史不等于当前有效。

Need新增 `unit:FactualField[str]|None=None, unit_quantity_fact_hash:str|None=None, unit_confirmation_id:str|None=None`；ORM分别 nullable JSONB(none_as_null=True)/VARCHAR(64)/VARCHAR(40)，全NULL或全有、JSON对象、严格hash；旧行不回填。
新表 `need_unit_confirmations` 列：tenant_id/confirmation_id/need_id/ artifact_id/source_message_id/confirmed_by VARCHAR(40)，idempotency_key VARCHAR(128)，request_hash/quantity_fact_hash VARCHAR(64)，confirmed_at TIMESTAMPTZ，payload JSONB（完整NeedUnitConfirmationView，无bytes/对象键）。全部非空、hash/JSON检查。
PK(tenant,confirmation)；UNIQUE(tenant,need,confirmation)、UNIQUE(tenant,need,key)；复合FK到Need和raw_artifacts；Need(tenant,need,unit_confirmation_id)复合FK到确认表。确认表UPDATE/DELETE触发器拒绝；Need单位三列变化时触发器验证与所引用receipt的unit/quantity_fact_hash一致。先插确认再更新Need，可用立即FK，无须关闭约束。旧quantity更新不删除/重写单位。
0042 downgrade若有单位确认业务记录先拒绝并要求授权归档处理，不静默丢失证据；空新记录场景先删Need FK/trigger/三列，再删确认表/trigger函数。仅在隔离测试库验证往返和有数据拒绝，不改0041或已合并迁移。

## 3. RED代码与分步实施

- [ ] 新增以下单测，先跑到缺契约RED，再补声明/stub跑到行为RED；第三方依赖错误不算最终业务RED。

```python
from dataclasses import replace
from datetime import UTC, datetime
import importlib
import pytest
from pydantic import ValidationError as SchemaError
from domains.demand.schemas import NeedUnitConfirmationCommand
from domains.demand.service import quantity_fact_hash, mutable_need_field_names
from shared.schemas.identifiers import TenantId, ValidatedNeedId, EmployeeId
from shared.schemas.provenance import FactualField, Provenance, SourceType

def test_new_source_changes_quantity_hash():
    now = datetime(2026, 8, 28, tzinfo=UTC)
    old = FactualField(500, Provenance(
        SourceType.CONVERSATION, "msg_customer_1", "human", now,
        EmployeeId("emp_test"), now, source_quote="We need 500 pieces."))
    new = replace(old, provenance=replace(old.provenance, source_id="msg_customer_2"))
    tenant, need = TenantId("tenant_test"), ValidatedNeedId("need_test")
    assert quantity_fact_hash(tenant, need, old) != quantity_fact_hash(tenant, need, new)

def test_unit_is_not_an_old_model_field_or_a_default():
    assert "unit" not in mutable_need_field_names()
    need = importlib.import_module("domains.demand.models").ValidatedNeed
    assert need.__dataclass_fields__["unit"].default is None

def test_command_cannot_claim_confirmation():
    with pytest.raises(SchemaError):
        NeedUnitConfirmationCommand.model_validate(dict(
            unit="pieces", source_message_id="msg_customer_1", locator="body:0:19",
            source_quote="We need 500 pieces.", expected_quantity_fact_hash="a"*64,
            expected_unit_confirmation_id=None, confirmed_by="boss"))
```

- [ ] RED：`python3 -m pytest tests/unit/test_need_units.py -q`；实现DTO/错误/纯hash/model字段。添加旧完整度0..5和quantity=0有/无unit对照、不改旧promotable/mutable返回、非法数量类型、来源/确认时间变化、unit_missing/stale/unconfirmed。
- [ ] unit_service测试使用实现全部Protocol的内存UoW（copy-on-enter、成功commit，失败rollback），受控authorizer/check/guard和reader记录调用深度；reader断言guard/UoW深度均零。先写失败测试：check拒绝零reader、guard拒绝零写、来源任一绑定不符零写、同键一receipt/history、异键同旧ID冲突、history失败回滚；再实现§2顺序。
- [ ] DB fixture `unit_db_case` 使用integration_engine+async_sessionmaker、真实专用UoW/repo/service；字段 `tenant,need_id,actor_id,service,command,demand,factory` 采用§1对应类型，`demand:DemandService` 为既有真实服务。测试内方法 `confirmation_count()->int`、`unit_history_count()->int` 每次开新session且tenant+need过滤。原Need/消息/artifact均明确受控fixture；权限/reader是注入受控实现，不声称真实Gateway核验。

```python
async def test_old_quantity_update_invalidates_binding(unit_db_case):
    from domains.demand.errors import NeedUnitError
    from domains.demand.service import require_current_unit
    c = unit_db_case
    first = await c.service.confirm(c.tenant, c.need_id, c.command,
        actor_id=c.actor_id, idempotency_key="unit-db-1")
    await c.demand.update_need_fields(c.tenant, c.need_id,
        {"quantity": {"value": 600, "quote": "We now need 600 pieces.",
                      "extracted_by": str(c.actor_id)}},
        source_message_id="msg_customer_changed", updated_by=str(c.actor_id))
    facts = await c.service.get_facts(c.tenant, c.need_id, actor_id=c.actor_id)
    assert facts.unit_confirmation_id == first.confirmation_id
    with pytest.raises(NeedUnitError) as caught:
        require_current_unit(facts)
    assert caught.value.code == "unit_stale"
    replay = await c.service.confirm(c.tenant, c.need_id, c.command,
        actor_id=c.actor_id, idempotency_key="unit-db-1")
    assert replay == first
    assert await c.confirmation_count() == await c.unit_history_count() == 1
```

- [ ] 上例changed消息须在受控客户消息fixture中存在；旧API包装键value/quote/extracted_by已核对，不为测试改原API。RED：`env -u TEST_DATABASE_URL python3 -m pytest tests/integration/test_need_units.py tests/integration/test_need_unit_migration.py -q`。
- [ ] 实现0042/ORM/UoW/repo/旧repo编解码；真实DB证明确认+绑定+历史原子、表append-only、直接篡改unit与receipt不符拒绝、跨tenant Need/artifact/confirmation FK拒绝。
- [ ] 多连接并发：同键同载荷一个receipt/history；同键异载荷冲突；不同key同expectedID最多一成功；reader暂停期间另一连接能锁Need；确认先锁旧quantity更新等待，更新后stale；quantity更新先提交则确认quantity_changed。用Events/Barrier，不同真实连接，不用savepoint伪并发或长sleep。
- [ ] 真实DB注入history失败三处全回滚；提交成功丢返回后重建service同key恢复；超时/未知提交固定code脱敏。受控guard只能证明调用顺序，真实员工/归属锁竞争由T8验证。
- [ ] migration使用现有scripts/run_alembic.py隔离harness：0041插旧Need→0042三列SQL NULL/旧读取不变→0041旧数据保留→0042，finally恢复head；schema与ORM一致。Docker缺失skip不是通过；不得访问环境生产DSN。

## 4. GREEN、交接与未运行边界

```bash
python3 scripts/check_boundaries.py
python3 -m ruff check domains/demand infra/db/need_unit_uow.py infra/db/repositories/need_units.py infra/db/repositories/need_hypotheses.py infra/db/tables.py migrations/versions/0042_need_quantity_unit.py tests/unit/test_need_units.py tests/integration/test_need_units.py tests/integration/test_need_unit_migration.py
env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_need_units.py tests/unit/test_demand_completeness.py tests/unit/test_need_hypothesis_models.py tests/unit/test_composed_reply_actions.py tests/integration/test_need_units.py tests/integration/test_need_unit_migration.py tests/integration/test_need_hypotheses.py tests/integration/test_migrations.py -q
env -u TEST_DATABASE_URL python3 -m pytest tests/integration/test_phase1_closed_loop.py tests/integration/test_research_discovery.py -q
```

使用common-context确认的Python3.12 runtime；不读取.env。检查git diff只含本任务，保留T1/T2他人修改。追加ADR/模块规则并完成测试、自审后提交 `feat: 增加客户数量单位事实与可追溯确认`，再交控制器独立审查；不push/部署。报告实际RED/GREEN，相关回归不宣称全库验收。

T3消费三列、NeedQuoteFacts、三个公开纯函数和immutable确认来源；在其自己的持锁连接投影完整facts，不调用get_facts另开连接。quotations/costing由上层转换本域DTO，保留完整Provenance与unit绑定；unit_missing/stale阻断新冻结，旧workflow不变。
T8实现并注入真实authorizer/check/guard和Gateway evidence reader；HTTP计划：GET `/costing-quotes/needs/{need_id}/quote-facts`，POST `/costing-quotes/needs/{need_id}/unit-confirmations`（command+Idempotency-Key），GET同路径加 `/{confirmation_id}`；tenant/actor由认证绑定，按上表错误映射。未装配dependency_unavailable，不能注册受控实现生产可用。
本任务可验真实Postgres/迁移/Need锁/持久幂等；真实客户来源、Gateway、员工/机会授权保护适配、HTTP/UI和生产激活均not_run，T8/T9/T10接续。搜索/模型/联系人/邮件/供应商Provider真实调用均零。本brief各项初始未完成。
