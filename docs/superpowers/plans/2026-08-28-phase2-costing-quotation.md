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

迁移基线为 0040。本计划预占 0041（成本政策/依据）、0042（客户数量单位）、0043（锁定）、0044（报价）、0045（QUOTE_PDF）；
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
| `QuoteBasis` / quotations | costing 公共结果的本域投影：同名 scalar 绑定、金额结果、证据摘要/hash、确认/有效期；不导入 costing 私有模型或 schemas |
| `QuoteApprovalFact` / quotations | `tenant_id, approval_id, quote_id, content_hash, approval_type, state, decided_by, proposed_by, owner_id: str, expires_at: datetime, decided_at: datetime\|None`；由持久 approvals reader 产生 |
| `CustomerQuoteView` / shared，quotations重导出 | `quote_id: str, version: int, issuer_name, issuer_address, issuer_contact, account_name, description, specification, unit, quantity_display, unit_price_display, total_display, currency, valid_until_display: str, approved_terms: tuple[str,...]`；全部确定性字符串，无成本、源引用、内部人名 |
| `QuoteFileView` / quotations | `file_id, quote_id, artifact_id, content_hash, template_version: str, quote_version: int, size_bytes: int, generated_at: datetime`；不给 bucket/key/永久 URL |

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
新增单位迁移占0042，原锁定/报价/文件迁移顺延0043/0044/0045；实施前再次检查head。

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

执行前交接补正：新入口返回严格`QuoteDetailView`（不可变content快照+state），旧QuoteView/QuoteLineView构造不变；新行带显式rounding。先按operation恢复已写入quote，再考虑当前context；恢复不是重新批准。创建采用报价专用creation session，在外层context lease内、freeze前持报价机会advisory锁并preflight，同session保持至报价写入提交，不跨连接对Opportunity取FOR UPDATE。replaces允许当前active或无active时的latest expired，均CAS预期版本；expired旧行不改。老板抬头仅接name/address/contact，服务端生成source_ref和三字段EMPLOYEE_INPUT Provenance。新增报价证据tenant复合FK关联表，采购/费用为本域判别联合；当前身份reader必填。完整接口在T3B交付后由T4专用brief核定，不允许实现者沿用下列旧简写猜缺失字段。

**Files**
- Create: `shared/schemas/quote_document.py`, `domains/quotations/service_impl.py`, `domains/quotations/permissions.py`, `infra/db/quotation_uow.py`, `infra/db/repositories/quotations.py`, `migrations/versions/0044_quotations.py`, `tests/unit/test_quotation_service.py`, `tests/integration/test_quotations.py`
- Modify: `domains/quotations/{service,schemas,models,repository,errors}.py`, `domains/quotations/AGENTS.md`, `infra/db/tables.py`, `workflows/quote_approval/application.py`, `tests/unit/test_quotation_models.py`

**Interfaces**
- Produces: `QuotationActor(employee_id: str, role: str, opportunity_scope: str)`，由身份/context 绑定。
- Produces: `QuotationService.create_from_basis(tenant_id, command: QuoteDraftCommand, basis: QuoteBasis, context: QuoteBusinessContext, *, operation_id: str, actor: QuotationActor) -> QuoteView`。
- Produces: `get(tenant_id,quote_id,*,actor)`, `list_versions(tenant_id,opportunity_id,*,actor)`；`expire_overdue(tenant_id,*,limit:int) -> int`；`project_customer(quote: QuoteView) -> CustomerQuoteView`（仅投影，外层另验批准）。
- `QuoteBasis` 精确持有 `basis_id,operation_id,request_hash,tenant_id,opportunity_id,context_hash,sheet_hash,basis_hash,policy_id,specification,unit,destination,base_currency,quote_currency: str; quantity:int; displayed_unit_price,displayed_total,unit_full_cost:Money; margin_rate,minimum_margin_rate:Decimal; evidence:tuple[QuotePriceEvidence,...]; evidence_valid_until:datetime; inputs_hash:str`，另含共享`NeedQuoteFacts`及本域等值`QuoteScopeConfirmation`（完整字段对应冻结子计划CostScopeConfirmationView，含原始证据ID/hash、人工映射和Provenance）；上层显式转换，不导入costing域DTO或只保留一个确认hash。
- `QuotePriceEvidence`（本域schemas）包含 `evidence_id,evidence_hash,kind,basis,specification,unit,currency,confirmed_by:str; quantity_min,quantity_max,moq:int|None; amount:Money; confirmed_at:datetime; valid_until:datetime|None`。报价域按kind再次校验采购quoted、规格数量单位及到期，不能只凭一组引用/hash认可价格。
- `confirm_issuer(tenant_id,command:QuoteIssuerCreate,*,actor:QuotationActor,idempotency_key:str)->QuoteIssuer`；`get_confirmed_issuer(tenant_id)->QuoteIssuer`（async）。`QuoteIssuerCreate(name,address,contact,source_ref:str)` 不接收confirmed字段；确认仅boss，记录不可变新版本，供T3 reader适配。
- Produces: `QuoteApplicationService.create(tenant_id,command:QuoteDraftCommand,*,actor_id:EmployeeId,idempotency_key:str)->QuoteView`；构造注入 context_provider、costing、quotations、clock。转换两个域的 DTO 在此显式进行。

- [ ] 先为现有状态表加失败测试：

```python
import importlib
import pytest
M = importlib.import_module('domains.quotations.models')

@pytest.mark.parametrize('state', [M.QuoteState.DRAFT, M.QuoteState.PENDING_APPROVAL,
                                  M.QuoteState.APPROVED])
def test_unpublished_versions_can_expire_without_becoming_sent(state):
    assert M.QuoteState.EXPIRED in M.ALLOWED_TRANSITIONS[state]
    if state is not M.QuoteState.APPROVED:
        assert M.QuoteState.SENT not in M.ALLOWED_TRANSITIONS[state]
```

- [ ] RED：`python3 -m pytest tests/unit/test_quotation_models.py -q`，再只补设计允许的 superseded/expired 边；保留原发送、终态测试。
- [ ] 0044 建 `quotations`, `quotation_lines`, `quotation_state_events`, `quotation_approval_bindings`, `quotation_issuers`，复用0043的basis/operation。业务 payload 的金额均字符串；state/version/tenant/到期/关联作显式列和约束；quotes 一条业务版本一条 quote_id。
- [ ] 同 opportunity/version 唯一；活跃部分唯一索引仅 `draft,pending_approval,approved,sent`；报价内容更新由 trigger 拒绝，状态只允许显式转换并追加审计，删除拒绝。basis不可变，operation以 tenant+key 唯一且同payload才可恢复。quoted refs必须tenantFK；原先只含字符串的假引用不能通过新路径。

```sql
CREATE UNIQUE INDEX uq_quotations_active ON quotations(tenant_id, opportunity_id)
WHERE state IN ('draft','pending_approval','approved','sent');
```

- [ ] create_from_basis 再验 tenant/机会/规格/数量/有效期、P和内容一致、已锁依据；单产品只允许一行。修订必须携带 expected_quote_version，在同事务标旧 superseded 并建新版本。并发冲突返回409，不在服务里无界重试分配新版本。
- [ ] application先校验完整QuoteDraftCommand（新增scope_confirmation_id），解析持久scope hash组成共享QuoteCreationIntent；新请求使用context lease → CostingFreezeService.freeze → quotations.create_from_basis → complete_creation。quote表必须UNIQUE(tenant_id,operation_id)并保存request_hash；新增真实QuoteCreationCompletionReader适配，从持久quote取得完整receipt。quote已写而operation未complete可按同key/operation回读恢复，不能用新幂等键绕过pending操作；已完成后显式修订按T3B复用规则。第二道规格校验核对完整scope人工映射及原始PriceEvidence，不能拿供应商自由文本和canonical JSON直接比较。补 receipt 接口 `record_verified_send(tenant_id,quote_id,receipt: QuoteSendReceipt,*,actor)`；`QuoteSendReceipt(attempt_id,quote_id,content_hash,tenant_id,sent_at)` 只由可信reader注入，本批不暴露手写sent HTTP。
- [ ] `project_customer` 逐字段白名单构造，不 `model_dump(exclude=...)`；报价内部视图单独持有成本信息，JSON序列化均可测试。历史保留 exact旧成本/规则/FX；Actual对照仅比较同规格数量币种，缺收入不生成 realized profit。
- [ ] GREEN：`env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quotation_service.py tests/unit/test_quotation_models.py tests/integration/test_quotations.py tests/integration/test_quote_cost_lock.py -q`；0044 roundtrip及结构检查通过后提交 `feat: 持久化不可变报价版本和安全客户视图`。

## Task 5：报价审批工作流与独立例外

**Files**
- Create: `workflows/quote_approval/flow.py`, `workflows/quote_approval/steps.py`, `workflows/quote_approval/approvals.py`, `tests/unit/test_quote_approval_workflow.py`, `tests/integration/test_quote_approval_postgres.py`
- Modify: `workflows/quote_approval/AGENTS.md`, `domains/quotations/service.py`, `domains/quotations/service_impl.py`, `domains/approvals/service.py`, `domains/approvals/service_impl.py`（固定安全错误 allowlist及只缩短期限参数）, `domains/quotations/schemas.py`

**Interfaces**
- Produces: `build_quote_approval_definition() -> WorkflowDefinition`；`register_quote_approval(engine, registry, approvals: ApprovalService) -> None`；`build_quote_approval_handlers(quotations,approvals,context_provider,system_actor)->Mapping[str,StepHandler]`。
- Produces: quotations `approval_snapshot(tenant_id,quote_id,*,actor)->QuoteApprovalSnapshot`；`bind_approval(tenant_id,quote_id,fact:QuoteApprovalFact,*,actor)->None`；`apply_approval_facts(tenant_id,quote_id,facts:tuple[QuoteApprovalFact,...],context:QuoteBusinessContext,*,actor)->QuoteView`。
- `QuoteApprovalSnapshot(quote_id,content_hash,context_hash,prepared_by,owner_id: str, required_types:tuple[str,...], internal_quote:QuoteDetailView, expires_at:datetime)`；所有 facts从 approvals.get 读取，event仅作唤醒。
- Produces pure `quote_change_set_ref(quote_id:str,content_hash:str,approval_type:str)->str`，严格形状 `quote:{quote_id}:{content_hash}:{approval_type}`。
- Existing `ApprovalService.submit` 新增可选 keyword `expires_at_limit: datetime|None=None`，仅可将既有类型期限缩短，不能延长；旧调用不变。提交时间之后的UTC限制才有效，已有包同key但新载荷/期限不一致报冲突；本变化写入ADR0018并补旧流程回归。

- [ ] 失败测试不因收到事件就批准，先做关联键测试：

```python
from workflows.quote_approval.approvals import quote_change_set_ref

def test_approval_type_is_part_of_exact_quote_binding():
    quote_id = 'quo_01M0PWRX23T9DP9ENM9PW5GFC8'
    h = 'a' * 64
    assert quote_change_set_ref(quote_id,h,'quote_send') != quote_change_set_ref(
        quote_id,h,'margin_floor_override')
```

- [ ] RED：`python3 -m pytest tests/unit/test_quote_approval_workflow.py -q`。
- [ ] 采用 `quote_approval` version1：assemble → submit → wait（入等待先读facts，防事件先到）→ apply → mark_applied → notify → complete；timeout → expire → notify。步骤只持quote/run/approvalID、hash、固定状态，不保存原文、金额明细或PDF。
- [ ] 由 quotes 域计算 required_types：`quote_send` 总是必须；跌破当前底线另需 `margin_floor_override`；折扣/交期/付款/认证分别对应已有类型。模板字段有限型，不支持类型的承诺拒绝，不通过自由文本“兜底正式批准”。每项都绑定同内容hash与owner/preparer，审批不创建多级路由。
- [ ] 对每种需要类型创建独立审批包；完整内部报价、依据和前后版本摘要在审批业务payload，不入outbox。期限取既有审批有效期与报价/依据有效期的最早值；不延长现有默认审批期限。

```python
def quote_change_set_ref(quote_id: str, content_hash: str, approval_type: str) -> str:
    if not re.fullmatch(r'quo_[0-7][0-9A-HJKMNP-TV-Z]{25}', quote_id):
        raise ValidationError('报价标识无效')
    if not re.fullmatch(r'[0-9a-f]{64}', content_hash):
        raise ValidationError('报价内容哈希无效')
    if approval_type not in {'quote_send','margin_floor_override','discount',
                             'delivery_commitment','payment_terms','certification_commitment'}:
        raise ValidationError('报价审批类型不支持')
    return f'quote:{quote_id}:{content_hash}:{approval_type}'
```

- [ ] apply须校验所有类型批准、当前员工在职和ABAC、非自批、当前context和规则仍适用、quote活跃/未过期。报价更新+QuoteApproved+状态审计在同UoW；outbox重复只有一条业务效果；先domain效果后approval.mark_applied。任何规则不符固定apply_failed，不能默认批准。
- [ ] 测试否决/过期/自批/员工离职/owner变化/旧版本晚到/错类型/错tenant/缺一种approval/低于底线/重复事件/应用后崩溃。workflow等待期间重启真实engine，审批仍可正确唤醒；晚到旧run事件终止且不影响新run。
- [ ] GREEN：`env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quote_approval_workflow.py tests/integration/test_quote_approval_postgres.py tests/unit/test_approval_service.py -q`；结构检查后提交 `feat: 接通报价独立审批与幂等应用工作流`。

## Task 6：报价派生文件存储契约

**Files**
- Create: `docs/adr/0019-quote-pdf-artifacts.md`, `migrations/versions/0045_quote_pdf_artifacts.py`, `tests/unit/test_quote_pdf_artifacts.py`, `tests/integration/test_quote_pdf_artifacts.py`
- Modify: `artifact_store/store.py`, `artifact_store/service_impl.py`, `artifact_store/AGENTS.md`, `infra/db/tables.py`, `infra/db/repositories/artifacts.py`, `domains/quotations/repository.py`, `infra/db/repositories/quotations.py`

**Interfaces**
- `GeneratedArtifactKind.QUOTE_PDF = 'quote_pdf'`；MIME仅application/pdf，subject_ref仅 `quo_...`。
- EMAIL_DRAFT 保持 `enr_...` 与 `{subject}:{sequence}:draft`；QUOTE_PDF 键固定 `{subject}:{sequence}:quote_pdf:{generated_by}`。`generated_by` 是受信注册的模板版本如 `quote_pdf_v1`，不是任意用户文本。
- `GeneratedArtifactStore.put/get/get_meta` 签名不变；新增 `QuotationService.record_file(tenant_id,quote_id,file:QuoteFileView,*,actor)->QuoteFileView` 和 `get_file(tenant_id,quote_id,file_id,*,actor)->QuoteFileView`。

- [ ] 编写合法邮件草稿仍有效、跨 kind 主体被拒的RED：

```python
from artifact_store.store import GeneratedArtifactKind, GENERATED_ARTIFACT_MIME_TYPES

def test_quote_pdf_is_a_generated_kind_not_a_raw_evidence_kind():
    kind = GeneratedArtifactKind.QUOTE_PDF
    assert GENERATED_ARTIFACT_MIME_TYPES[kind] == frozenset({'application/pdf'})
    assert kind.value != 'pdf'
```

- [ ] RED：`python3 -m pytest tests/unit/test_quote_pdf_artifacts.py -q`。
- [ ] 按 kind 明确分支校验subject/key/MIME，禁止放宽原正则为任意字符串。generated_by/sequence仍纳入winner比较，异内容同key拒绝，raw/generated保持分离。
- [ ] 0045将 artifacts 的 CHECK 改为互斥的两条合法分支；不改旧object_key模式。新增 `quotation_files` tenant复合FK到quotes/artifacts，唯一tenant+quote+template版本，保存客户contenthash、artifacthash、size和时间；只增不可改。
- [ ] downgrade 若存在新 kind/关联数据明确拒绝并提示先授权导出/处理，不自动删除业务文件；空新表场景验证完整roundtrip，邮件草稿数据全保留。
- [ ] 存储bytes不使用 RawArtifactKind.PDF；所有读取仍校验hash和length。上层先鉴权，Store不重复实现商业审批。先GeneratedStore完成，后报价关联，失败同key可查winner恢复；不自动换key。
- [ ] GREEN：`env -u TEST_DATABASE_URL python3 -m pytest tests/unit/test_quote_pdf_artifacts.py tests/integration/test_quote_pdf_artifacts.py tests/unit/test_artifact_store_contracts.py tests/integration/test_artifact_store_persistence.py -q`；结构检查后提交 `feat: 增加隔离的报价PDF派生文件类型`。

## Task 7：离线 PDF 渲染适配器

**Files**
- Create: `connectors/quote_pdf/{AGENTS.md,__init__.py,client.py,manifest.py}`, `tests/unit/test_quote_pdf_renderer.py`
- Modify: `domains/quotations/service.py`, `pyproject.toml`（runtime `reportlab==5.0.1`、`pypdf==6.16.2`；pypdf还供T8原件文本定位，不依赖dev安装泄漏）

**Interfaces**
- `QuotePdfRenderer.render(view:CustomerQuoteView,*,template_version:str)->bytes` Protocol 在 quotations.service 公开，实际 connector 只接收客户白名单DTO。
- `ReportLabQuotePdfRenderer(maximum_bytes:int,maximum_pages:int)` 实现；模板注册仅 `quote_pdf_v1`，上限必须由配置提供，不填生产默认值。页面几何和字距是排版常量，不是商业阈值。
- 依赖选型于2026-08-28核验：[ReportLab发行](https://pypi.org/project/reportlab/)、[pypdf发行](https://pypi.org/project/pypdf/)；使用 ReportLab 开源本地库，不使用商业 RML 服务。[官方排版文档](https://docs.reportlab.com/reportlab/userguide/ch5_platypus/)

- [ ] 创建失败测试：纯客户DTO可渲染；同DTO同模板bytes一致；缺字体字符失败而不是静默画方框；长文本换页、超最大页数拒绝。

```python
from io import BytesIO
from pypdf import PdfReader

def assert_customer_pdf(content: bytes, expected_total: str) -> None:
    reader = PdfReader(BytesIO(content))
    text = '\n'.join(page.extract_text() or '' for page in reader.pages)
    assert expected_total in text
    assert 'internal_cost_secret' not in text
    assert 'margin_floor' not in str(reader.metadata)
    assert '/JavaScript' not in str(reader.trailer)
```

- [ ] RED：`python3 -m pytest tests/unit/test_quote_pdf_renderer.py -q`。依赖未安装先按pyproject安装，不把ImportError当成最终业务RED；新增测试须实际调用renderer后再证明行为缺失。
- [ ] 使用本地 ReportLab `BytesIO`、固定纸张、受控字体和模板；金额已是字符串，render不做金额计算。Plain text转义后交Paragraph，不接收原生HTML/RML，不添加URL图片、file路径或PDF附件。可使用库自带本地 Vera 字体，字符覆盖检查不通过则返回受控错误。

```python
buffer = BytesIO()
document = SimpleDocTemplate(buffer, pagesize=A4, invariant=1)
story = [Paragraph(escape(view.issuer_name), styles['Heading1']),
         Paragraph(escape(view.description), styles['BodyText']),
         Paragraph(escape(view.total_display + ' ' + view.currency), styles['BodyText'])]
document.build(story)
content = buffer.getvalue()
```

- [ ] 完整模板加编号/版本、客户、规格、单位、数量、单价、总额、有效期、已批条款及页码；构建前限制文本大小，渲染中计页并中止超限，输出检查byte上限；不可先渲染无限页再判大小。
- [ ] 测试禁止 socket/HTTP、恶意 `<img>`/脚本/文件路径只当文字或拒绝；所有客户字段、PDF正文、metadata、outline、附件都扫描内部成本标记。客户端金额字符串含非金额格式时在域边界拒绝而非renderer补算。
- [ ] GREEN：`python3 -m pytest tests/unit/test_quote_pdf_renderer.py -q`，实际渲染视觉验收留到T10；结构检查后提交 `feat: 实现确定性离线客户报价PDF渲染`。

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
- [ ] 增加已确认资料的可恢复读取：`GET /opportunities/{id}/price-evidence`、`GET /quote-fx/{fx_id}`、`GET /cost-sheets/{id}/coverage`、`GET /quotes/{id}/files`；租户/角色/机会范围与写入一致。对应public service补 `list_price_evidence(tenant_id,opportunity_id,*,actor)`, `get_coverage(tenant_id,cost_sheet_id,*,actor)`, `list_files(tenant_id,quote_id,*,actor)`，分别返回typed证据列表、确认清单、文件列表；刷新页面不依赖内存缓存的来源ID。
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
- fixture字段：`tenant, actor_id, command, application, approvals, quotations, files, context_provider, gateway_calls`。方法 `submit_and_approve(quote_id)->None` 用真实审批service和engine推进，不直接改quote.state。返回的gateway_calls是按tool_id计数的只读dict。

- [ ] 写闭环失败测试，先证明真实composition链未完整接通：

```python
async def test_approved_pdf_does_not_send_or_create_a_won_deal(quote_case):
    q = await quote_case.application.create(quote_case.tenant, quote_case.command,
        actor_id=quote_case.actor_id, idempotency_key='controlled-quote-1')
    await quote_case.submit_and_approve(q.quote_id)
    f = await quote_case.files.generate(quote_case.tenant, q.quote_id,
        actor_id=quote_case.actor_id)
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
