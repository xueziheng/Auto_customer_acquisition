# Phase 2 成本与报价闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将人工确认的供应资料接入确定性成本/利润、不可变报价版本、独立审批和安全客户 PDF，不自动发送。

**Architecture:** 复用 costing、approvals、Postgres workflow 和 Artifact Store。业务规则留在 costing/quotations，跨域只由 workflow/application 调公开契约；API/worker 装配真实适配器。新增报价文件工具通过插件挂载，Gateway 核心不变。

**Tech Stack:** Python 3.12+、Pydantic v2、SQLAlchemy 2.x、Postgres、FastAPI；Vue 3 + TypeScript + Vite + Ant Design Vue；ReportLab 本地 PDF、pypdf 原件文本定位与测试。

**Spec:** `docs/superpowers/specs/2026-08-28-phase2-costing-quotation-design.md`（2026-08-28 用户已确认）。

## Global Constraints

- 所有 Money、比例和汇率仅接收有限 Decimal，HTTP 用十进制字符串。
- 一个报价版本对应一个产品规格和数量档、一张成本表、一个报价币种。
- 本批主流程止于 approved 与文件交付，不自动进入 sent。
- 审批人不得等于起草人或机会负责人。
- 所有记录有 tenant_id；外键采用 tenant+ID；仓储查询强制租户过滤。
- 未确认模型金额不计入，规则/精度未配置不定价。
- 保留旧成本 API/readiness、已有研究 v1/v2 与 Campaign 工作流。
- 不改 Tool Gateway 核心检查管线，不跨域读取私有 model/repository。
- 不购买来源，不发真实邮件，不启用生产进程；不把工程通过计为 Phase 2 全部完成。
- 真实业务参数必须由用户配置；测试样例里的数量/利润率/汇率仅是受控数据。

---

## 0. 执行环境和状态记录

执行基线：`809d7b6`，主工作区 `/Volumes/T7/Company/Auto_customer_acquisition`。
已使用 using-git-worktrees 创建 `codex/phase2-costing-quotation`，工作树为
`.worktrees/phase2-costing-quotation`；恢复时核对本计划进度账本和提交，不重新创建或覆盖。不在 main 实现功能。
所有命令在该工作树根运行；环境安装不能修改生产部署。

```bash
export PATH="/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/Users/xueziheng/.nvm/versions/node/v24.15.0/bin:$PATH"
export PYTHONPATH="$PWD"
env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_costing_calculation.py tests/unit/test_quotation_models.py -q
python3 scripts/check_boundaries.py
```

先确认环境与导入路径指向当前工作树。每任务按 RED → 最小实现 → GREEN → 回归 → 自审与commit → 独立审查；
若任务较大，每个明确行为重复此循环，不先写整域再补测试。测试缺 Docker 的 skip 不计为数据库验收通过。
迁移只在隔离容器跑，使用 `scripts/run_alembic.py`，不直接执行 alembic 扫入 AppleDouble 文件。

实施前阅读根 AGENTS、HANDBOOK、相关目录 AGENTS、规格及本计划。进入新目录先读规则。
所有新增方法完整类型和中文 docstring。当前计划中的新文件、类型、方法均为拟实现，不代表已存在。

## 1. 文件职责与依赖

| 任务 | 文件边界 | 输出 |
|---|---|---|
| T1 | costing 的 `calculation.py`、`schemas.py`、`service.py` | 可复现纯计算、稳定公开 DTO |
| T2 | costing 的 `quote_service.py`、`quote_repository.py`；infra 对应适配 | 老板政策、价格依据、场景确认的持久化 |
| T3 | costing `quote_lock.py`；infra `quote_context.py`；workflow `application.py` | 当前上下文保护、成本锁定、可恢复操作 |
| T4 | quotations service/repository/UoW/ORM | 报价建立、修订、过期、客户投影 |
| T5 | `workflows/quote_approval/{flow,steps,approvals}.py` | 独立审批、例外和幂等应用 |
| T6 | Artifact Store kind 分支和迁移 | QUOTE_PDF，不放宽 EMAIL_DRAFT |
| T7 | `connectors/quote_pdf/` | 离线、确定性、受限的 PDF bytes |
| T8 | Gateway 文件插件、API/worker composition | 可真实装配的服务/文件读取/到期处理 |
| T9 | 现有成本报价/审批/Run 页及小组件 | 类型生成后的 UI 接线 |
| T10 | 跨进程/多连接/E2E/验收文档 | 整体验收及真实/受控边界 |

依赖：T1 → T2 → T3 → T4 → T5；T4 → T6 → T7；T3–T7 → T8 → T9 → T10。
文件拆分只在本批触及的责任内，不顺便整理全库大文件。`service.py` / `schemas.py` 是公开入口，
实现可以分文件，公开类型须显式重导出，禁止上层导入内部模型。

迁移基线为 0040。本计划预占 0041（成本政策/依据）、0042（客户数量单位）、0043（锁定）、0044（报价）、0045（审批契约）、0046（QUOTE_PDF）；
实施时若编号被其他工作占用，按新 head 顺延并同步本计划，不重写他人的迁移。
ADR 拟使用 `0018-costing-quotation-contracts.md`、`0019-quote-pdf-artifacts.md`，同样先查冲突。

## 2. 公共类型词典（任务定义的字段不得各自改名）

下列 HTTP 模型全部 `ConfigDict(strict=True, frozen=True, extra="forbid")`。金额沿用
`CostingDecimalInput`；客户端无权传确认人、tenant、approved、locked。确认来源由服务端身份绑定。

| 类型 / 所属 schemas | 字段与边界 |
|---|---|
| `CostGroups` / costing | `goods: Decimal, variable: Decimal, fixed: Decimal`，单位成本、有限且非负 |
| `ProfitMetrics` / costing | `unit_full_cost, minimum_price, target_price, gross_profit, contribution_profit, full_cost_profit, margin_rate, discount_headroom, additional_acquisition_headroom: Decimal` |
| `PricingPolicyCreate` / costing | `category: str\|None, minimum_margin_rate, target_margin_rate: Decimal, cost_groups: dict[str, Literal['goods','variable','fixed']], effective_from: datetime, source_ref: str`；22 类型全覆盖 |
| `PricingPolicyView` / costing | 上述字段 + `policy_id, content_hash, confirmed_by: str, confirmed_at: datetime` |
| `RoundingPolicy` / costing | `unit_places: int, total_places: int, strategy: str`；两精度 0..12，策略为 Money 已允许集合，无默认 |
| `PricingOptions` / costing | `mode: Literal['target','manual'], unit_price: Money\|None, rounding: RoundingPolicy, quote_fx: FxRate\|None, algorithm_version: Literal['costing-v1']`；manual 必须有单价，target 不接收伪实际售价 |
| `CalculationSnapshot` / costing | `cost_sheet_id, policy_id, inputs_hash, context_hash: str, version_number: int, computed_at: datetime, base_currency, quote_currency: str, metrics: ProfitMetrics, effective_unit_revenue: Money, displayed_unit_price, displayed_total: Money` |
| `SourceEvidence` / costing | `tenant_id, source_ref, artifact_id, content_hash, locator: str, observed_at: datetime, source_type: str, source_url: str\|None`；WEB_PAGE须有source_url，仅可信 reader 产生，非 HTTP |
| `PriceEvidenceCreate` / costing | 按 `kind` 判别的 `SupplierPriceEvidenceCreate \| ExpenseEvidenceCreate`，具体字段见T2；不能要求每一笔管理分摊费用都有供应商MOQ |
| `PriceEvidenceView` / costing | 创建字段 + `evidence_id, evidence_hash: str, source: SourceEvidence, field_provenance: dict[str, Provenance]` |
| `CostCoverageCreate` / costing | `expected_sheet_hash: str, decisions: tuple[CostCoverageDecision,...], acquisition_mode: Literal['summary','detail']` |
| `CostCoverageDecision` / costing | `item_type: str, applicable: bool, reason: str, item_bindings: tuple[CostItemBinding,...]`；不适用必须理由，适用必须绑定金额 |
| `CostItemBinding` / costing | `item_sequence: int, evidence_id, source_line_ref, allocation_scope: str`；按费用明细和分摊范围去重，不按整个 artifact 去重 |
| `QuoteBusinessContext` / quotations | 标量身份/客户/规格及`account_id, opportunity_state, need_facts:NeedQuoteFacts, need_facts_hash, specification_hash, issuer:QuoteIssuer, runtime:QuoteRuntimeFacts`；context_hash为只读派生。全部精确字段见冻结子计划§1.2，内部可信事实不直接HTTP序列化 |
| `QuoteIssuer` / quotations | `issuer_id, content_hash, name, address, contact, source_ref: str, confirmed_by: EmployeeId, confirmed_at: datetime, field_provenance: dict[str,Provenance]`；老板EMPLOYEE_INPUT确认、服务端来源ID，不设样例默认值 |
| `QuoteDraftCommand` / quotations | `opportunity_id, cost_sheet_id, expected_context_hash, expected_sheet_hash, scope_confirmation_id: str, valid_until: datetime, unit_price: Money, rounding: QuoteRoundingInput, quote_fx_ref: str\|None, terms: tuple[QuoteTerm,...], replaces_quote_id: str\|None, expected_quote_version: int\|None` |
| `QuoteTerm` / shared，quotations重导出 | `kind: Literal['discount','delivery_commitment','payment_terms','certification_commitment'], text: str`；客户英文，领域再验文本/类型和独立审批映射 |
| `FrozenCostBasis` / costing | 完整字段见冻结子计划§1.4；除原计算/价格证据外，保存request_hash、完整Need事实、scope确认、policy、coverage、pricing_options及报价FX，不能只存引用 |
| `QuoteBasis` / quotations | FrozenCostBasis完整本域等值投影，精确字段见报价版本子计划；采购/费用判别联合、完整scope/Need/政策/FX，不导入 costing 域类型 |
| `QuoteDetailView` / quotations | 新严格不可变content快照与state；旧QuoteView构造保持，新生产服务使用QuotationVersionService |
| `QuoteApprovalFact` / quotations | 完整字段以报价审批子计划为准；安全typed payload、原request_hash/limit、当前决定与应用状态，由持久approvals.read_fact显式投影，不再使用旧展示姓名代替身份 |
| `CustomerQuoteView` / shared，quotations重导出 | `quote_id: str, version: int, issuer_name, issuer_address, issuer_contact, account_name, description, specification, unit, quantity_display, unit_price_display, total_display, currency, valid_until_display: str, approved_terms: tuple[str,...]`；全部确定性字符串，无成本、源引用、内部人名 |
| `QuoteFileView` / quotations | 文件子计划的安全输出；content_hash为PDF bytes，另含quote_content_hash/customer_content_hash；file_id/版本/时间取真实记录，不给bucket/key/永久URL |

`QuoteDraftCommand` 不跨域 import `RoundingPolicy`，使用shared纯形状的 `QuoteRoundingInput` 并在quotations重导出，
上层显式转换；generated OpenAPI 类型各自生成，不在前端重写。`QuoteBasis` 字段详情在 T4
实现为本域 DTO，禁止只留 `dict` 或一个 `cost_sheet_locked: bool`。

所有服务签名中简写 `tenant_id` 均标注 `TenantId`，cost_sheet_id/quote_id/opportunity_id 分别用
已有强类型ID；actor必须是对应域typed actor。未展开 `->None` 的签名不得猜测返回HTTP对象。
测试import的领域私有模块继续用本库既有 importlib 方式，不能以计划例子放宽结构检查。

### 执行前接口补正（2026-08-28）

核对基础表后，T3须先补客户数量单位：现有Need有数量事实，没有结构化unit。
在demand新增可空unit事实、对应quantity事实hash绑定与窄的人工确认入口；不改旧完整度、
旧可提取/更新字段词表、研究或Campaign解释，不回填存量单位。新报价缺单位或缺客户来源阻断，
不能从供应商按件报价推断客户数量按件计。数量/规格/目的地的完整Provenance一并进入冻结上下文，
不能只保留Opportunity摘要或单个hash。T3分为“客户单位事实”与“上下文冻结”两个审查子切片。
新增单位迁移占0042，冻结/报价为0043/0044；T5审批契约占0045，文件迁移为0046；实施前再次检查head。

T4的CustomerQuoteView唯一纯展示定义放shared/schemas/quote_document.py，域schemas显式重导出；
T7的QuotePdfRenderer Protocol仍在quotations.service，connector仅导入shared DTO并结构化实现。
这是落实横切设施不导入domains的既有边界，不放宽硬边界。相关公共契约追加ADR0018。
T7 Files补domains/quotations/service.py；T8/T9同时接上单位确认接口及无默认单位的表单。
客户单位子切片的完整接口、来源核验、绑定失效和多连接测试见
`docs/superpowers/plans/2026-08-28-phase2-need-quantity-unit.md`，不以本段代替验收。

## Task 1：确定性计算、契约和 ADR

**Files**
- Create: `domains/costing/calculation.py`, `tests/unit/test_costing_breakdown.py`, `docs/adr/0018-costing-quotation-contracts.md`
- Modify: `domains/costing/schemas.py`, `domains/costing/service.py`, `domains/costing/models.py`, `domains/costing/AGENTS.md`, `GLOSSARY.md`
- Regression: `tests/unit/test_costing_calculation.py`, `tests/unit/test_costing_models.py`

**Interfaces**
- Consumes: existing `CostSheet`, `MarginRule`, `Money`, `FxRate` inside costing.
- Produces: §2 的 `CostGroups/ProfitMetrics/PricingPolicyCreate/PricingPolicyView/RoundingPolicy/PricingOptions/CalculationSnapshot`；其余成本证据DTO由T2提供。`compute_metrics(costs: CostGroups, minimum: Decimal, target: Decimal, price: Decimal) -> ProfitMetrics`.
- Produces: `compute_breakdown(sheet: CostSheet, margin_rule: MarginRule, *, policy: PricingPolicyView, options: PricingOptions, coverage_hash: str, context_hash: str, now: datetime) -> CalculationSnapshot`，从 `service.py` 导出。旧函数尚为 stub；旧 `compute_unit_full_cost` 和 readiness 保持原行为。
- Produces: `canonical_pricing_hash(payload: Mapping[str, object]) -> str`；只接受已确定类型的 JSON 化数据，Decimal/日期规范化，不读取时钟。

- [x] 写首个失败测试及精确公式样例（仅测试数字，不是生产政策）：

```python
from decimal import Decimal as D
from domains.costing.calculation import compute_metrics
from domains.costing.schemas import CostGroups

def test_manual_price_is_used_instead_of_target_price():
    value = compute_metrics(CostGroups(goods=D('6'), variable=D('1'), fixed=D('1')),
                            D('0.20'), D('0.36'), D('10'))
    assert value.unit_full_cost == D('8')
    assert value.minimum_price == D('10')
    assert value.target_price == D('12.5')
    assert value.gross_profit == D('4')
    assert value.contribution_profit == D('3')
    assert value.full_cost_profit == D('2')
    assert value.margin_rate == D('0.2')
    assert value.additional_acquisition_headroom == D('0')
```

- [x] 跑 `python3 -m pytest tests/unit/test_costing_breakdown.py -q`，记录 RED（新接口尚无实现）。
- [x] 先实现上述纯公式，再逐条加测试和实现校验：有限 Decimal、非负成本、0≤底线≤目标<1、正售价、总成本为零固定阻断；不借返回零掩盖错误。

```python
with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
    full = costs.goods + costs.variable + costs.fixed
    floor = full / (Decimal(1) - minimum)
    target_price = full / (Decimal(1) - target)
    profit = price - full
    margin = profit / price
    discount = max(Decimal(0), Decimal(1) - floor / price)
    acquisition = max(Decimal(0), price * (Decimal(1) - minimum) - full)
```

- [x] 实现适配成本表的聚合和报价换算：逐项原币→核算币，整单项/Q；按老板已确认归类累计。先舍入客户单价、再行额；使用最终总额/Q/显式报价汇率恢复有效核算收入。所有计算固定 `costing-v1` 50 位上下文；舍入只在客户展示量化。
- [x] 加失败测试再实现 hash：同值 `1.0/1.00` 相同；重复项不能被 set 去掉；规则、数量、报价、精度、来源或汇率变更 hash 改变；时钟变更 hash 不变。使用 `json.dumps(..., sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)` 的 UTF-8 SHA-256。
- [x] 测试缺汇率、反向汇率、待确认金额、不同精度、低于底线、非正正式售价；单位/整单结果必须分别断言。旧 Money/成本测试不删不放宽。
- [x] 记录 ADR 0018：全成本利润率不是毛利率；可折扣空间不是许可；新增获客空间不是总预算；schema 和 workflow 版本兼容；同步 GLOSSARY/本域规则。
- [x] GREEN：`python3 -m pytest tests/unit/test_costing_breakdown.py tests/unit/test_costing_calculation.py tests/unit/test_costing_models.py -q`；结构检查后提交 `feat: 实现可追溯的确定性成本利润计算`。

## Task 2：政策、价格依据和完整性确认持久化

**Files**
- Create: `domains/costing/quote_service.py`, `domains/costing/quote_repository.py`, `infra/db/repositories/costing_quote.py`, `migrations/versions/0041_costing_quote_evidence.py`, `tests/unit/test_costing_quote_evidence.py`, `tests/integration/test_costing_quote_evidence.py`
- Modify: `domains/costing/service.py`, `domains/costing/schemas.py`, `domains/costing/permissions.py`, `domains/costing/errors.py`, `infra/db/costing_uow.py`, `infra/db/tables.py`, `tests/integration/test_repositories.py`, `tests/integration/test_migrations.py`

**Interfaces**
- Consumes: T1 DTO、既有 `CostingActor`、`RawArtifactMeta` 的安全字段（经来源 reader 投影）。
- Produces: `PricingEvidenceReader.read_verified(tenant_id: TenantId, source_ref: str, locator: str, *, actor_id: EmployeeId) -> SourceEvidence`（async Protocol，在 service.py 公开）。服务传入当前已核验身份；reader仍独立执行原件读取权限，不因成本角色扩大收件箱或附件可读范围。
- Produces: `CostingQuoteService.confirm_policy(tenant_id, command: PricingPolicyCreate, *, actor: CostingActor) -> PricingPolicyView`；`get_policy(tenant_id, category: str|None, *, actor) -> PricingPolicyView`。
- Produces: `confirm_price(tenant_id, command: PriceEvidenceCreate, *, actor) -> PriceEvidenceView`；`confirm_coverage(tenant_id, cost_sheet_id: CostSheetId, command: CostCoverageCreate, *, actor) -> str`（返回覆盖内容 hash）；均为 async。
- Produces: `confirm_quote_fx(tenant_id, command: QuoteFxCreate, *, actor)->QuoteFxView`、`get_quote_fx(tenant_id,fx_id,*,actor)->QuoteFxView`；`QuoteFxCreate(base_currency,quote_currency,source_ref:str,rate:Decimal,observed_at:datetime)`，view另含 `fx_id,content_hash,confirmed_by,confirmed_at`。独立保存核算→报价方向，不改旧成本汇率元组。
- `SupplierPriceEvidenceCreate`：`kind='supplier_price'`；`opportunity_id,need_id,supplier_ref,specification,unit,destination,currency,basis,source_ref,locator:str`，`quantity_min,quantity_max,moq:int`，`amount:Decimal`，`quoted_at,valid_until:datetime`。
- `ExpenseEvidenceCreate`：`kind='confirmed_expense'`；`opportunity_id,item_type,allocation_scope,currency,basis,source_ref,locator:str`，`amount:Decimal, is_per_unit:bool, quantity:int`（适用订单数量，正整数），`observed_at:datetime`，`valid_until:datetime|None`；无MOQ/供应商产品字段。不将actual凭证重标quoted；适用范围和确认事实仍须核实。新报价采购只接受quoted；非采购费用接受已发生凭证或确认价目依据，indicative费用本批不开放人工例外。
- Constructor: `CostingQuoteServiceImpl(uow_factory, evidence_reader: PricingEvidenceReader, *, actor_reader: CostingActorReader, now: Callable[[], datetime])`；只在 `service.py` 导出 Protocol，应用装配导入实现。`CostingActorReader.read_current(tenant_id:TenantId,actor_id:EmployeeId)->CostingActor|None`为async，入口及来源IO后写入前复核当前身份，失效拒绝。
- 来源DTO补`source_url:str|None`，WEB_PAGE必需；政策/FX视图补可信source和field_provenance。旧T1纯计算fixture允许缺该扩展，但正式持久路径强制完整并校验确认身份/时间，不将缺来源历史政策当作当前授权。
- coverage核对金额、币种、基准、类型、source_ref及分摊口径；费用quantity须等于sheet.quantity。供应商单件项amount等于其单价，整单项amount等于单价×数量（固定Decimal上下文，不自动改值/舍入）；两者都须满足数量范围/MOQ。

- [x] 用 schema 失败测试先锁定：老板确认参数不能来自请求体，归类不覆盖22项被拒，quoted 无数量范围/有效期/来源被拒。

```python
import pytest
from pydantic import ValidationError
from domains.costing.schemas import ExpenseEvidenceCreate

def test_expense_evidence_cannot_omit_its_source_and_scope():
    with pytest.raises(ValidationError):
        ExpenseEvidenceCreate.model_validate({'kind': 'confirmed_expense'})

def test_expense_evidence_has_no_client_confirmed_actor_field():
    assert 'confirmed_by' not in ExpenseEvidenceCreate.model_fields

def test_expense_evidence_does_not_require_product_moq():
    assert 'moq' not in ExpenseEvidenceCreate.model_fields

def test_expense_evidence_does_not_accept_hidden_actor_override():
    with pytest.raises(ValidationError):
        ExpenseEvidenceCreate.model_validate({'kind': 'confirmed_expense', 'confirmed_by': 'boss'})
```

- [x] 跑 `python3 -m pytest tests/unit/test_costing_quote_evidence.py -q`，确认 RED 是缺失新约束而非依赖配置。
- [x] 扩展 UoW 为 `policies/prices/coverage/quote_fx` 仓储；方法统一 `add`, `get(tenant_id,id)`, `get_for_update`，政策另有 `get_effective(tenant_id,category,at)`。每个主键和关联均含 tenant。
- [x] 0041 增量建四表：`costing_policies(tenant_id,policy_id,category,effective_from,content_hash,payload,confirmed_by,confirmed_at)`；`costing_price_evidence(tenant_id,evidence_id,opportunity_id,artifact_id,evidence_hash,payload,confirmed_by,confirmed_at)`；`costing_coverage(tenant_id,coverage_id,cost_sheet_id,sheet_hash,content_hash,payload,confirmed_by,confirmed_at)`；`costing_quote_fx(tenant_id,fx_id,content_hash,payload,confirmed_by,confirmed_at)`。payload 仅业务结构化字段，金额字符串；原始 bytes 不入库。
- [x] 唯一 `(tenant_id,id)`；确认方法均额外接 `idempotency_key:str` keyword，持久记录key和payload hash，同key同内容返回同记录；对四表 UPDATE/DELETE 加 append-only trigger；引用 cost_sheets/opportunities/raw_artifacts 用复合外键。不改旧 margin_rules 记录；新路径只读有确认事实的 costing_policies。

```sql
CREATE FUNCTION reject_costing_evidence_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'immutable costing evidence';
END;
$$ LANGUAGE plpgsql;
```

- [x] 服务先通过当前员工身份校验权限（新 `POLICY_CONFIRM` 仅 boss），再验证可信 source 的 tenant/hash/locator，逐字段构造 Provenance；非 quoted 可保留作估算，但正式路径不得用。未知来源类型固定拒绝，不调用模型。
- [x] 覆盖清单以 `item_sequence` 绑定当前 sheet hash；22项需全覆盖。`(source_ref, source_line_ref, allocation_scope)` 重复拒绝，同 artifact 不同行可通过；summary/detail 获客口径互斥。配置不适用但存在相应确认成本也拒绝，防隐藏费用。
- [x] 为 `CostSheetView` 增加服务端 `content_hash`，`CostItemView` 增加持久 `item_sequence`，不让前端按数组位置猜费用ID。hash覆盖数量/币种/成本项/已确认来源/汇率，不含locked_at或读取时间；并发追加后旧expected hash失效，单纯锁定不会改变原内容身份。旧字段及旧接口含义保留，新字段从真实数据计算。
- [x] PostgreSQL 测试四表 tenant FK、追加不可改、源 artifact 跨租户、历史政策未确认、新旧生效时间、精度超限拒绝及 0041 往返。用 `integration_engine` 真连接；来源 reader 可受控，但不得把 fake reader 当成真实原文核验。
- [x] GREEN：`env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_costing_quote_evidence.py tests/integration/test_costing_quote_evidence.py -q`；结构检查后提交 `feat: 持久化成本政策与供应商价格确认依据`。

## Task 3：客户单位、完整上下文与可恢复成本冻结

本任务分为先后两个独立审查子切片，全部通过才算Task3完成；不增加Phase。

- [x] T3A：客户单位事实、数量来源绑定、旧流程兼容与0042已完成（d77f292、aa98790），独立审查及嵌套确认人修复复审通过；并发测试屏障Minor留最终审查。真实来源/授权装配仍归T8。
- [ ] T3B：按 `docs/superpowers/plans/2026-08-28-phase2-quote-freeze.md` 完成四个TDD提交：共享事实/意图及两个minor、真实context lease、0043与人工成本适用性、冻结/恢复/显式修订复用；独立审查通过。

**T3B公共契约与边界（完整精确字段以冻结子计划为准）**

- 完整Need事实和中立身份/创建意图放shared，demand保留原公开名与单位业务规则；同值来源变化仍使绑定失效。QuoteBusinessContext携带完整事实和当前runtime，但业务hash排除本次审批人/runtime，包含原prepared_by、owner和issuer版本。
- `QuoteContextProvider.open(tenant_id, opportunity_id, actor_id, *, prepared_by)` 在同session按员工ID排序→机会→Need持FOR SHARE；bootstrap变化即释放冲突，不补锁新owner。原件IO在锁外；当前时间在实际取得锁后读取，不能用等待前的旧时间判有效。
- 新增独立 `CostingFreezeService`，不把冻结塞进T2的证据服务。公开 `prepare_scope_access/confirm_scope/get_scope/calculate/freeze/get_frozen/get_creation/complete_creation`；报价FX按显式quote_fx_ref读取持久事实。
- 0043新增 `cost_scope_confirmations/costing_quote_bases/quote_creation_operations`。人工scope绑定完整Need/规格/条款/期限、sheet/coverage/所有原始证据；不能自动把旧覆盖清单升级为适用。供应商自由文本保留，以明确人工映射对应完整目标规格，数量/单位/目的地/quoted/期限硬检查不绕过。
- freeze接收完整共享 `QuoteCreationIntent` 与幂等键，首次winner分配稳定operation，basis/operation/首次locked_at同事务。完成receipt只由真实报价reader提供；同键异完整请求冲突，未知提交只原键恢复。
- 未完成operation独占sheet；完成后显式修订可以复用未变锁表并生成新scope/basis。表内成本/核算FX变化须新成本版本；独立报价FX可绑定新的确认记录并重算，不改旧记录。
- 报价准备与内部读取仅四个既有成本角色；这不授予通用CRM、原件或客户文件权限。context/basis的原文摘录不得直接序列化到HTTP。审批和客户文件分别沿用既有独立授权。
- 保留旧readiness/研究/Campaign，修复T1同币种无关FX和T2EmployeeId类型minor；真实多连接/0043往返/兼容/结构验收见子计划，T4/T8真实装配不在本任务冒充完成。

## Task 4：报价版本、状态、客户投影与迁移

本任务的完整接口、五个TDD提交及验收以 [报价版本子计划](2026-08-28-phase2-quotation-versions.md) 为准。
T3B交付后须核对最终接口再派发；下面只列交接摘要，不用旧骨架DTO推测缺失字段。

- [ ] 新`QuoteDetailView/QuoteContentSnapshot/QuoteContentLine`严格快照，旧QuoteView/QuoteLineView/Quote/QuoteLine构造不改；新行带显式rounding。
- [ ] 新`QuotationVersionService`与窄creation session；旧无actor/布尔批准/手写sent骨架不注册为生产后门。
- [ ] 0044真实报价、行、状态审计、抬头、审批绑定、证据引用和发送receipt；tenant复合FK、内容不可变、唯一active及operation绑定。
- [ ] 在context lease内、freeze前取报价专用机会锁并preflight，同quotation session保持至创建提交；不跨连接对Opportunity取FOR UPDATE。
- [ ] 显式replaces支持当前active或无active时latest expired的expected version CAS，expired旧版不改；latest accepted/rejected可用全新成本表/新scope/无replaces开下一版本，原终态不可改写。
- [ ] 按真实operation先恢复已commit quote并补complete；当前Need/抬头/到期变化不触发重复freeze/报价，也不延续批准。
- [ ] 老板抬头仅接name/address/contact，服务器生成source_ref及三字段EMPLOYEE_INPUT Provenance；不冒称外部原件已核验。
- [ ] 采购/费用本域判别联合，完整冻结投影和scope第二道门；费用不伪造规格/MOQ；shared唯一客户白名单。
- [ ] 五次TDD提交、自审后一次独立任务审查；真实T5批准链及T8装配另行验收，不把受控receipt当真实发信。

## Task 5：报价审批工作流与独立例外

完整接口、四个功能TDD提交及文档/最终审查以 [报价审批子计划](2026-08-28-phase2-quote-approvals.md) 为准。
T3B/T4交付后先核对真实接口与新增构造依赖，再派发；不能按旧骨架布尔审批接口推测实现。

- [ ] 每quote一轮、每type一包；quote_send、低价例外和四类条款精确独立绑定，全部binding与pending原子。
- [ ] 新quote:与quote-approval-v1命名空间双标记严格验证，原limit/request_hash持久跨状态幂等；旧邮件quote_send保持原解释。
- [ ] 安全审批payload白名单，显示实际成本/报价FX方向、比率和时间；不含来源原文、URL、locator或整份Need/basis。
- [ ] boss租户/manager当前直属owner范围，read允许本人起草/负责包，decide/apply禁止起草人及提交/当前owner自批；原件ACL独立。
- [ ] access历史读取不依赖当前单位有效性；fresh apply一次排序保护全部员工→机会→Need，再持报价锁及当前policy选择锁直至提交。
- [ ] 当前policy id/hash严格匹配；quote/state-event/QuoteApproved/成功receipt同事务，事件不作授权。拒绝/到期不自动重提，需新报价版本。
- [ ] receipt优先恢复mark_applied，facts_hash不受应用状态改变；成功之后身份/事实变化不破坏历史补记，也不延续客户文件许可。
- [ ] 稳定approval_run_id绑定真实workflow run；engine新增内部tenant只读get_run，经公开adapter校验类型/主体/版本/hash，不假扮老板或锁run行自等。
- [ ] 0045保存新审批契约/唯一binding/成功receipt，QuoteApproved注册持久outbox；真实engine重启/多连接/旧流程回归后统一独立审查。

## Task 6：报价派生文件存储契约

完整接口、0046与四个TDD提交以 [报价文件子计划](2026-08-28-phase2-quote-files.md) 为准。
T4/T5实际交付后核对quote/receipt/run reader及构造依赖再派发；不将计划接口视为已实现。

- [ ] 新QUOTE_PDF严格kind/MIME/quo主体/key/已注册模板分支，EMAIL_DRAFT原enr/:draft行为不变；shared唯一模板版本常量。
- [ ] 区分quote_content_hash、customer_content_hash和PDF artifact_hash；QuoteFileView.content_hash仅指bytes，所有值由真实来源推导。
- [ ] record_file只接artifact_id，真实metadata reader在infra适配Store.get_meta；get/list仅安全metadata，不代表正式下载许可。
- [ ] get_file_approval按quote读取真实成功receipt并核稳定run，解决先知道executor才能查run的循环；不补造批准或新执行。
- [ ] 文件/历史metadata走当前机会ABAC，不复用四成本角色门；真实scope与当前正式授权归T8，T6受控guard明确失败关闭。
- [ ] 0046双kind及文件关联、复合FK/一致性trigger/唯一quote+template/只增；有新数据downgrade拒绝。
- [ ] QUOTE_PDF未知commit保留可能已持久的bytes，原key/全部绑定恢复；旧Raw/EMAIL_DRAFT补偿不重写，孤立bytes后续审计、不自动清扫。
- [ ] 真实PG提交成功后异常/取消、并发winner、三hash/真实run/meta绑定、历史只读与旧草稿回归后统一独立审查。

## Task 7：离线 PDF 渲染适配器

完整接口与三个TDD提交以 [离线PDF子计划](2026-08-28-phase2-quote-pdf-renderer.md) 为准。
T4客户投影验证、T6shared模板实际交付后再派发；首次实际PDF作者命令前按已读PDF技能执行marker，T10另做逐页视觉验收。

- [ ] QuotePdfRenderer Protocol在quotations.service；connector仅消费shared唯一CustomerQuoteView，固定错误跨层同class，不导入域。
- [ ] runtime固定reportlab==5.0.1与pypdf==6.16.2；离线本地Vera，实际glyph缺字失败，不替换已批准文字。
- [ ] 必填maximum_bytes/maximum_pages/maximum_text_bytes；文本在story前、页数在下一页绘制前、bytes在有界sink拒超限，无生产默认值。
- [ ] 正式客户DTO由真实quote唯一投影并逐字段验证；renderer不重算或修正金额，不自授批准/访问权。
- [ ] 固定模板/invariant/metadata，全字段及有序条款、可分页长文；无URL图片/附件/动作。
- [ ] 同实例/新实例/其他文档后/新进程bytes一致；实际PDF对象图/反例检查，不靠IndirectObject字符串声称安全。
- [ ] 真实ReportLab+pypdf受控测试与结构检查；不把文本提取当布局验收，也不把输出大小限制称为硬内存沙箱。

## Task 8：Gateway 文件插件、API 和 scheduler 实际装配

**Files**
- Create: `tool_gateway/handlers/quote_files.py`, `tool_gateway/checks/quote_files.py`, `apps/api/composition/quotations.py`, `apps/api/routers/quotation_actions.py`, `tests/unit/test_quote_file_gateway.py`, `tests/unit/test_quotation_router.py`, `tests/integration/test_quote_runtime.py`
- Modify: `tool_gateway/manifest.py`（注册工厂，不改pipeline）, `apps/api/{dependencies,main}.py`, `apps/api/composition/runtime.py`, `apps/scheduler_worker/{runtime,main}.py`, `domains/quotations/service.py`, `infra/.env.example`

**Interfaces**
- `QuoteFileAccessService.authorize(tenant_id,quote_id,*,actor_id,history:bool)->CustomerQuoteView`（async）；读取T3 context、T5持久facts和当前政策，委托域检查；history不能返回当前正式下载许可。
- `QuoteFilesApplication.generate(tenant_id,quote_id,*,actor_id)->QuoteFileView`；`download(tenant_id,quote_id,file_id,*,actor_id)->tuple[QuoteFileView,bytes]`，无通用URL/路径。
- `build_quotation_composition(session_factory, settings, raw_store, generated_store, tool_gateway, *, now)` 返回明确 `QuotationComposition(application,costing,quotations,files,workflow_handlers)`；API和worker分别调用本层装配，不互相import。
- `QuoteExpiryDriver.scan_once()->int` 调域 `expire_overdue`；SchedulerRuntime 增加可选 `quote_expiry_driver`，为None维持旧cycle行为，不新增定时进程。

- [ ] 先写HTTP DTO不接受布尔授权的失败测试：

```python
from domains.quotations.schemas import QuoteDraftCommand

def test_public_quote_command_cannot_claim_approval_or_cost_lock():
    fields = set(QuoteDraftCommand.model_fields)
    assert fields.isdisjoint({'approved','cost_sheet_locked','tenant_id','prepared_by'})
```

- [ ] RED：`python3 -m pytest tests/unit/test_quotation_router.py tests/unit/test_quote_file_gateway.py -q`；随后用ASGITransport加真实请求422/403、跨tenant404、未装配503测试。
- [ ] 两个私有文件工具 `quotation.file.generate` / `quotation.file.read`，以及只读价格资料工具 `quotation.evidence.read`；显式 tenant/permission gate。generate 为 MEDIUM、本地文件生成、不发送，启用approval/idempotency/rate_limit；read 为 LOW且每次重验真实授权。新gate只调用公共服务，不复制利润规则；不增HIGH发送profile。
- [ ] handler以 quote_id/file_id 或source_ref/locator取受信数据；参数和ledger不含正文/成本/bytes。PDF下载bytes及原始资料用一次性typed槽，同调用栈取走并finally清理。生成返回安全artifact/fileID，持久去重由T6；拒绝时存储/renderer调用为零。
- [ ] 实现PricingEvidenceReader与NeedUnitEvidenceReader：仅已授权raw source，Gateway读取后验证实际hash/locator，消息来源先经现有消息阅读权限转换成raw artifact；不接受generated artifact为证据。未知结果结构化失败，不把空内容当核验完成。最小来源支持和定位契约见下段；未接入来源固定source_unsupported。
- [ ] 为人工选择原文位置提供同profile的受鉴权、受限文本预览及locator生成入口；只能访问已授权upload/message，不接受任意URL/路径。选定范围由后端生成/核验canonical locator，正文只通过本次Gateway结果槽送已授权HTTP界面，不进入ledger/日志/模型。不要求用户自行从另一PDF引擎猜字符偏移或手算片段hash。
- [ ] API端点（均在 `/costing-quotes`）：`GET/POST /policies`、`GET/POST /issuer`、`POST /quote-fx`、`POST /price-evidence`、`POST /cost-sheets/{id}/coverage`、`POST /cost-sheets/{id}/calculate`、`GET/POST /opportunities/{id}/quotes`、`GET /quotes/{id}`、`POST /quotes/{id}/submit`、`POST /quotes/{id}/revisions`、`POST /quotes/{id}/files`、`GET /quotes/{id}/files/{file_id}`。另加 `GET /opportunities/{id}/quote-context` 返回创建前所需当前hash和可编辑资料，不暴露凭证/成本给无权角色；不能把context查询设计成必须先有quote才能调用。
- [ ] 增加已确认资料的可恢复读取：`GET /opportunities/{id}/price-evidence`、`GET /quote-fx/{fx_id}`、`GET /cost-sheets/{id}/coverage`、`GET /quotes/{id}/files`；租户/角色/机会范围与各用途一致。对应public service补 `list_price_evidence(tenant_id,opportunity_id,*,actor)`、`get_coverage(tenant_id,cost_sheet_id,*,actor)`，文件列表复用T6 `list_files(tenant_id,quote_id,*,actor_id)`；分别返回typed证据列表、确认清单、文件列表，刷新不依赖内存缓存的来源ID。文件metadata当前机会ABAC与四成本角色读权分开，不错误复用同一actor门。
- [ ] 文件生成通过T6 get_file_approval按quote取得真实稳定approval_run_id；正式授权须文件用途专属当前事实租约，不能用四角色get或prepare-only context假装CRM读取。独立history入口只读已存PDF并展示真实状态，不重渲染、不将裸HTTP history布尔值直接作为当前下载门的豁免；全部bytes仍经Gateway并校验artifact hash/size。
- [ ] 接入T3B的`POST /cost-sheets/{id}/scope-confirmations`与对应已确认记录GET；确认前呈现完整目标规格/需求、条款、期限和每条来源的人工适用性说明。prepare_scope_access在context lease外，确认/冻结在受保护事实下提交。报价准备上下文用专门HTTP投影，不直接model_dump内部Need/context/basis中的source_quote；原件展开仍独立鉴权。新计算请求显式quote_fx_ref，不能提交自证的FxRate对象。API/worker组合分别注入T2证据服务和T3B冻结服务，不注册受控替身。
- [ ] POST绑定HTTP幂等键、当前员工；失败返回固定code与可展示原因，旧请求readiness不新增写。当前规则提高底线/证据失效/quote过期时正式file拒绝；文件返回前再验quote状态，不在鉴权前取bytes。

```python
# 放在 scheduler 的 singleton lock 已获准的 cycle 内；异常沿用脱敏分类。
if runtime.quote_expiry_driver is not None:
    await runtime.quote_expiry_driver.scan_once()
```

- [ ] API和scheduler真实factory均构造仓储/服务，注册quote_approval definition/handlers/outbox与expiry；缺任一依赖保持能力unavailable且无工具注册。expiry故障单独隔离，不吞掉其他workflow；未获singleton锁零expiry调用。不在构造时读取provider凭证或访问网络。
- [ ] 测试真实factory→真实Postgres→Gateway→renderer→受控object transport→持久generated metadata，包含锁顺序。API伪服务仅用于unit，不替代这一集成。预算/大小/页数未配置时禁用文件能力，错误脱敏。
- [ ] GREEN：`env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quote_file_gateway.py tests/unit/test_quotation_router.py tests/integration/test_quote_runtime.py tests/unit/test_api_runtime.py -q`；结构检查后提交 `feat: 装配报价审批和受控客户文件接口`。

### T8来源接线补充（执行中核对，2026-08-28）

- 最小生产支持：价格/费用为`upload:upl_<ULID>`，复用WorkIntake本人上传ACL并取得raw PDF；客户单位为真实入站Message→Conversation→account/Need→EMAIL_RAW，保持当前收件箱boss-only。成本角色、同租户或知道artifact ID都不授予资料阅读权；机会范围由上层和域服务另验，不以reader签名中没有scope而默认通过。
- PDF定位为`pdf-text-v1:p=1;c=20:140;h=<64位小写hex>`，邮件定位为`rfc822-plain-v1:c=0:19;h=<64位小写hex>`。p从1开始，c为确定性文本Unicode code point半开区间，h为该片段UTF-8哈希；完整raw bytes另核对原件hash/长度。profile固定parser版本和CRLF/CR→LF规则，不trim/换算/猜文字；前端选取使用同profile，不能从另一PDF文本引擎猜坐标。
- 政策/FX的`$`特指整份授权原件根引用：验证实际原件hash，业务值仍由员工确认；它不是供应商费用明细locator的替代物。报价/费用片段须能定位人工所依据的内容；同一来源不代表全字段已由解析器证实。
- 邮件只用首个非附件非空text/plain，不用旧reader截断正文。最小单位路径要求quantity为已确认CONVERSATION事实且source_id等于该消息；逐字摘录/原数量来源、整数token/单位关系均须匹配，复杂或含糊表达要求补证。人工确认仍负责语义，不将parser成功、否定句或历史引用推定为新需求。
- 文字PDF可解析；扫描、加密、损坏、超限及未实现Word/Excel/OCR/网页来源明确拒绝。运行配置显式提供原件bytes、页数、文本长度和解析资源上限；不可信PDF在可终止受限解析单元中处理，线程wait_for不充当资源隔离。原件读取上限须由transport保证，事后len校验不得宣称有界分配。
- 新增中立evidence DTO/离线connector、专属Gateway handler/check与一次性槽、上层typed reader；不改通用槽已有联系人前缀语义，不跨apps导入。原文只进调用内槽，ledger/log仅安全handle。当前身份显式逐次传入，不能固定boss/system或全局可变actor。
- 最低测试包含真实受控PDF/RFC822解析、真正raw store与持久metadata、跨actor/tenant/account拒绝零读取、篡改/越界/超限拒绝、槽取消清理、源码与日志无原文，以及原邮件/上传兼容。真实商业资料核验仍须单独标记，受控解析不证明客户/供应商真实承诺。

## Task 9：前端接线、权限展示和类型生成

**Files**
- Create: `apps/web/src/views/costing-quotes/PricingPolicyForm.vue`, `apps/web/src/views/costing-quotes/PriceEvidenceForm.vue`, `apps/web/src/views/costing-quotes/CostCoverageForm.vue`, `apps/web/src/views/costing-quotes/NeedUnitConfirmationForm.vue`, `apps/web/src/views/costing-quotes/CostScopeConfirmationForm.vue`, `apps/web/src/views/costing-quotes/QuoteVersions.vue`, `apps/web/src/views/costing-quotes/QuoteIssuerForm.vue`, `apps/web/src/views/costing-quotes/quote-input.ts`, `apps/web/tests/quotation-flow.test.ts`
- Modify: `apps/web/src/views/costing-quotes/CostingQuotes.vue`, `apps/web/src/views/approvals/ApprovalCenter.vue`, `apps/web/src/views/runs/RunCenter.vue`, `apps/web/src/api/api.d.ts`, `apps/web/tests/costing-quotes.test.ts`

**Interfaces**
- 仅使用生成 `components['schemas'][...]`，不手写DTO；组件Props/Emits为UI组合类型。
- 报价页显示后端 `allowed_actions`、`blockers`、版本/hash、审批和file状态；最终权限仍在后端。

- [ ] 先在新测试文件加入payload保真测试，再加入整页fake fetch行为测试（沿用现有createApp+router方式，不引入另一个测试框架）：

```typescript
import { expect, it } from 'vitest';
import { createQuotePriceBody } from '../src/views/costing-quotes/quote-input';
it('keeps precise money text without Number coercion', () => {
  expect(createQuotePriceBody('0.123456789012', 'USD'))
    .toEqual({ amount: '0.123456789012', currency: 'USD' });
});
```

- [ ] RED：`npm --prefix apps/web test -- tests/quotation-flow.test.ts`；生成类型 `npm --prefix apps/web run gen:api`，确认使用当前PYTHONPATH。
- [ ] 新建 `apps/web/src/views/costing-quotes/quote-input.ts` 导出 `createQuotePriceBody(amount:string,currency:string): components['schemas']['Money']`；trim字符串，拒绝空值，不做金额运算。

```typescript
export function createQuotePriceBody(amount: string, currency: string): components['schemas']['Money'] {
  if (!amount.trim() || !currency.trim()) throw new Error('请填写金额和币种');
  return { amount: amount.trim(), currency: currency.trim() };
}
```

- [ ] 拆分表单：老板政策与22项归类无预填利润率；老板确认本公司报价抬头；人工填写核算→报价汇率及来源；原价证据显示确认来源；适用清单区分缺失/零/不适用；目标和实际报价收益分列。报价输入数量/单位/币种不从网页猜，现有页面的示例默认值不能作为新方案确认值。
- [ ] 接上客户单位确认与成本适用性确认两表单。缺单位/绑定失效明确显示；无消息读取权不能确认但不扩收件箱权限。原文选择使用后端同profile预览和locator；scope表单展示完整目标规格/目的地/时间、条款、期限及每条来源的人工映射说明。提交后保留确认ID，需求/条款变化导致旧确认失效时要求重新确认，不自动刷新hash冒充已确认；幂等未知结果仍保留原键。
- [ ] 版本列表显示draft/等待/approved/expired/superseded及先前成本引用；修订确认明确旧版停用；未知请求结果显示待核对，保留原幂等键，不“一键重试”生成新单。下载按钮只取后端授权文件，没有自动发送按钮。
- [ ] 审批页一屏看必要信息、证据与低于底线例外，明确批准不发送；Run只展示安全摘要。客户文件预览不混内部成本数据；空数据/503/403/409/过期原因单独展示。
- [ ] GREEN：`npm --prefix apps/web test -- tests/costing-quotes.test.ts tests/quotation-flow.test.ts`；再typecheck/build；提交 `feat: 接通成本政策证据和报价审批界面`。

## Task 10：跨进程验收、回归、审查和交付记录

**Files**
- Create: `tests/integration/test_costing_quote_closed_loop.py`, `tests/e2e/test_costing_quote_browser.py`, `docs/acceptance/2026-08-28-phase2-costing-quotation.md`, `docs/operations/costing-quotation.md`
- Modify: `HANDBOOK.md`, `ROADMAP.md`, 必要模块AGENTS；`tests/e2e/conftest.py` 只增加隔离环境接线，不能降低现有就绪检测门槛。

**Interfaces**
- 使用T8真实composition。T10 fixture `quote_case` 放新integration测试文件：持有tenant/current actors、已验证需求/机会、raw artifact、显式测试policy、price evidence、完整coverage和QuoteDraftCommand；全部通过公开服务建立，只有来源bytes和对象存储transport受控。
- fixture字段：`tenant, actor_id, file_actor_id, command, application, approvals, quotations, files, context_provider, gateway_calls`。actor_id是当前四成本角色中的起草人；file_actor_id是另经当前机会ABAC授权的客户文件读取人，不假设成本角色天然可下载。方法 `submit_and_approve(quote_id)->None` 用真实独立审批service和engine推进，不直接改quote.state。返回的gateway_calls是按tool_id计数的只读dict。

- [ ] 写闭环失败测试，先证明真实composition链未完整接通：

```python
async def test_approved_pdf_does_not_send_or_create_a_won_deal(quote_case):
    q = await quote_case.application.create(quote_case.tenant, quote_case.command,
        actor_id=quote_case.actor_id, idempotency_key='controlled-quote-1')
    await quote_case.submit_and_approve(q.content.quote_id)
    f = await quote_case.files.generate(quote_case.tenant, q.content.quote_id,
        actor_id=quote_case.file_actor_id)
    assert f.size_bytes > 0
    assert all(quote_case.gateway_calls.get(k, 0) == 0 for k in
               ('email.send', 'contact.enrich', 'contact.verify', 'web.search'))
```

- [ ] RED：`env -u TEST_DATABASE_URL python3 -m pytest tests/integration/test_costing_quote_closed_loop.py -q`；记录失败原因，修复仅属于本链的实际装配缺口。
- [ ] fixture实现先通过原Need/Opportunity/员工服务建立已验证需求和可访问机会；上传受控supplierPDF为RawArtifact，T2确认，T3–T5实际创建/审批。fixture不提供默认生产利润政策，不调用真实联系人。最后另断言机会未won、需求未fulfilled、quote未sent，PDF可提取合计与域结果一致。
- [ ] 多连接并发与重启套件：最后一次owner变更、两个修订、追加和freeze、报价已写operation未记、审批已应用mark_applied未记、PDF已存quote关联未记、unknown存储结果。通过Events/Barrier控制时序，不靠长sleep；每例必须断言最终记录数、状态及零重复副作用。
- [ ] 真实Uvicorn+Vite+Chromium E2E：老板配政策、员工录证据与成本、独立审批人批准、下载PDF；窄屏390与桌面1280浏览，内容不溢出；自批不可用、API直调仍403；刷新和深链选中正确quote而不是列表第一条。
- [ ] 用pdf技能做实际生成文件的文本/metadata检查和逐页视觉检查；保留受控样例、hash与截图。浏览器操作使用当前可用browser技能；不能将手工查看源代码当作浏览器验收。
- [ ] 执行以下命令，记录每个实际结果；有失败先systematic-debugging，不能把失败tests删掉换绿色：

```bash
python3 scripts/check_boundaries.py
python3 -m ruff check .
env -u TEST_DATABASE_URL python3 -m pytest -m 'not e2e' -q --tb=short
env -u TEST_DATABASE_URL python3 -m pytest -m e2e -q --tb=short
npm --prefix apps/web test
npm --prefix apps/web run typecheck
npm --prefix apps/web run lint
npm --prefix apps/web run build
npm --prefix apps/web run gen:api
git diff --exit-code -- apps/web/src/api/api.d.ts
```

- [ ] 独立审查重点：单/整单口径、低价例外、自批、basis/当前事实可信性、并发唯一、文件信息泄露及Gateway不变；修复后重跑受影响和全量。未启用真实来源/发送必须明确not_run；受控DB/S3 transport不描述为真实供应商或生产对象存储。
- [ ] HANDBOOK新增本批启用/验收步骤，ROADMAP只标本批结果，不勾完Phase2。报告分别列工程、受控、真实资料、真实发送、剩余Phase2；记录金额/报价阈值仍需用户填；不修改Phase1运营完成状态。
- [ ] 提交 `docs: 记录Phase2成本报价闭环实际验收`；使用finishing-a-development-branch提供合并方式，未获选择不push、不部署、不清理其他工作树。

## 3. 规格覆盖自检

| 规格章节 | 实施任务 |
|---|---|
| §1 单规格数量档/不发送/不改Phase1 | T4、T8、T9、T10 |
| §2–3 复用与域边界、内部可信DTO | T1–T5、T8 |
| §4 来源/字段Provenance/三版本/22项归类/重复费用 | T2–T4、T9 |
| §5 公式/显式政策/FX/精度/hash/Actual收入区别 | T1–T4、T9、T10 |
| §6 锁定恢复/报价状态/独立审批/低于底线/过期 | T3–T5、T8、T10 |
| §7 客户白名单/PDF/模板版本/下载实时权限 | T4、T6–T9 |
| §8 租户/迁移/结构化失败/兼容/真实装配 | T2–T6、T8、T10 |
| §9 全量/浏览器/PDF/真实受控区分 | T10 |

计划保存不意味着任何任务已完成。所有任务初始未勾选；进入实施后只依据本次测试和审查证据更新。
每任务交接必须携带规格、全局约束和本任务完整Interfaces；类型词典是实现承诺，不能私自改名或删字段。
