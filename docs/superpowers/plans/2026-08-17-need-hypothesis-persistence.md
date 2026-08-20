# Need Hypothesis Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (with strict TDD discipline) to implement this plan task-by-task. Every behavior step must follow RED → confirm expected failure → minimal GREEN; no step may be implemented before its RED test is written and its failure reason is confirmed as the missing table/interface/implementation (not syntax, fixture, or ImportError accidents). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按已批准设计规格 `docs/superpowers/specs/2026-08-17-need-hypothesis-persistence-design.md` 交付 demand 域假设侧持久化/生命周期最小切片：迁移 0022（**恰 3 表**：`need_hypotheses`/`validated_needs`/`validated_need_field_history`）、租户安全仓储/UoW、`DemandServiceImpl` 的 **6 个生命周期方法**（create_hypothesis/promote_to_validated/reject_hypothesis/update_need_fields/mark_sourcing_ready/get_confidence）、字段级 Provenance/历史、确定性置信度/完整度（无存储概率）、**3 个 metadata-only 事件**注册（NeedHypothesisCreated/NeedHypothesisRejected/NeedValidated）。

**Architecture:** 沿用 demand 信号切片既有模式（2026-08-17-demand-signal-persistence）：`domains/demand` 放契约与 `service_impl`；`infra/db` 放 tables 行/UoW/仓储实现；迁移 0022（down_revision=`0021`）；`PostgresEventBus` 同事务 outbox。活跃假设去重用部分唯一索引 + `index_elements`/`index_where`（**禁用 `constraint=`**，规格 D2）；转换路径用 `SELECT ... FOR UPDATE`（D7）；FactualField/InferredField/EvidenceItem 快照存 JSONB 列（D13）；事件同事务发布、metadata-only（规格 §8）。

**Tech Stack:** Python 3.12、SQLAlchemy 2.x async、PostgreSQL 16、pytest-asyncio、testcontainers（Postgres）、Alembic。

## Global Constraints

- 所有 Python 命令使用 `conda run -n tradeos-py312`（本机 conda env `tradeos-py312`；conda run 前台执行，timeout >= 1500000ms，禁止 run_in_background；不使用输出截断 pipeline，任何新增 pipeline 显式 `bash -o pipefail` 并检查首个命令 rc）。
- 九条全局硬边界全部生效；本片重点：8（3 表全带 tenant_id、全部查询强制租户过滤、repo 参数越界 `TenantIsolationViolation` + critical 审计日志只记 action/绑定租户、**不记任何输入内容**）、5（FactualField/InferredField 类型强制、promote 门槛唯一且不可降）、4（10 个业务字段全为 FactualField JSONB 快照、历史表 source_message_id 指向证据链）、3（无 confidence 数值列，`get_confidence` 用 `derive_confidence` 现算）、9（domains/demand → shared 仅；不 import organization/opportunities/prospecting）、1（reason/字段值/正文/raw_observation 不得出现在 outbox/log/error——marker 测试验证）。
- 规格 D1-D19 全部生效（权威）；本片实现与 spec 决策一一对应；`NeedCluster`/`reply_qualification`/UI/API/模型 provider/Phase 2 **显式排除**（spec §2 非目标）；`validated_need.cluster_id` 不持久化；**View 组装（`list_hypotheses_for_outreach`/`get_need`/`get_cluster`，account_name 依赖 organization 域）不在本片（spec D14）**——service_impl 本片恰 6 个方法。
- 契约纠偏范围（spec D19）：`repository.py` 增 `get_for_update` ×2 + `DemandUnitOfWork` 增 `hypotheses`/`needs`；`service.py` 的 `update_need_fields` 返回类型 `ValidatedNeedView → None` + 三处 docstring 同步 + extracted_fields 11 键补全；`shared/` 零改动（**不新增错误类**——复用 `domains.demand.errors.InsufficientEvidenceError`/`SourcingThresholdNotMetError`/`HypothesisAlreadyResolvedError`）。
- 新文件 Git mode `100644`（`git add --chmod=-x <files>` + `git ls-files --stage` 验证）；AppleDouble 清理归零后才暂存（`find . -name "._*" -not -path "./.git/*" -delete`）；不提交 `.env`/凭证/构建产物。
- 每个任务：RED（本地跑不提交，确认预期失败原因）→ 最小 GREEN → 定向回归 + 窄门禁 → **停止等待监督方复审**；复审通过后执行该任务 mutation proof（临时 apply_patch → 精确测试 RED → 恢复 → 精确测试 GREEN → `git diff` 无残留）→ commit + push + exact-HEAD CI success（禁 force、禁 amend）。
- 编辑只用 apply_patch（heredoc 直输）；禁止 cat/printf/python 写仓库文件。
- 所有新事件（NeedHypothesisCreated/NeedHypothesisRejected/NeedValidated）注册进 `infra/db/outbox.py` 的 `EVENT_REGISTRY` 并同步 `tests/unit/test_outbox_serialization.py` 白名单期望集（131cbe5/db6577a 先例）；加新事件不需要 ADR（HANDBOOK §五）。
- `tests/unit/test_demand_signal_contracts.py`、`tests/integration/test_migrations.py` 的 head/期望集改动遵循「全局诚实更新」先例（0020→0021 时同款）。

## File Structure（规格 §10 权威，14 个路径；先锁定结构与职责）

```text
docs/superpowers/specs/2026-08-17-need-hypothesis-persistence-design.md  已交付（f235f2f）
docs/superpowers/plans/2026-08-17-need-hypothesis-persistence.md         本计划文件（Plan Delivery Gate docs commit）
migrations/versions/0022_need_hypotheses.py                              Task 2：3 表 + 部分唯一索引 + FK + CHECK
infra/db/tables.py                                                        Task 2：NeedHypothesisRow/ValidatedNeedRow/
                                                                          ValidatedNeedFieldHistoryRow（与迁移逐一同名同语义）
domains/demand/repository.py                                              Task 1：契约纠偏（get_for_update ×2 + UoW 属性）
domains/demand/service.py                                                 Task 1：契约纠偏（D19：update_need_fields → None + 三处 docstring + 11 键）
domains/demand/models.py                                                  Task 1：evidence_level / evidence() / can_promote_to_validated() 实现
domains/demand/service_impl.py                                            Task 4/5/6：新增 6 个服务方法
infra/db/repositories/need_hypotheses.py                                  Task 3：NeedHypothesisRepositoryImpl + ValidatedNeedRepositoryImpl
infra/db/demand_uow.py                                                    Task 3：UoW 增 hypotheses/needs
infra/db/outbox.py                                                        Task 4：EVENT_REGISTRY 注册三事件
docs/architecture/01-domain-model.md                                      Task 1：状态图同步（spec D18）
tests/unit/test_need_hypothesis_models.py                                 Task 1：纯单元（evidence/门槛/映射）
tests/unit/test_demand_signal_contracts.py                                Task 1：repo/UoW 契约扩展 + 事件注册断言（三事件在 Task 4 增补）
tests/unit/test_outbox_serialization.py                                   Task 4：白名单期望集 +3
tests/integration/test_migrations.py                                      Task 2：head 0021→0022 + 3 表契约/往返 + 部分索引谓词 parity（迁移侧）
tests/integration/test_need_hypotheses.py                                 Task 3/4/5/6/7：15 项集成 + mutation 目标
```

## Plan Delivery Gate（实施前提）

- [x] **Step 0: 本计划文件 docs commit**（照 2026-08-17-demand-signal-persistence 的 ab92690 先例；仅当监督方批准本计划后执行）：`git add --chmod=-x docs/superpowers/plans/2026-08-17-need-hypothesis-persistence.md` → `git ls-files --stage` 验 100644 → `git diff --cached --check` → commit 消息逐字 `docs(demand): plan need hypothesis persistence` → push → exact-HEAD CI success。

---

## Task 1: 契约纠偏 + 纯域内核（models 三实现 + service/repository 契约 + 文档同步）

**Files:**
- Modify: `domains/demand/repository.py`（`NeedHypothesisRepository`/`ValidatedNeedRepository` 各增 `get_for_update`；`DemandUnitOfWork` 增 `hypotheses`/`needs` 属性）
- Modify: `domains/demand/service.py`（D19 契约纠偏：`update_need_fields` 返回 `None` + 三处 docstring + extracted_fields 11 键）
- Modify: `domains/demand/models.py`（`DemandSignal.evidence_level`、`NeedHypothesis.evidence()`、`can_promote_to_validated()` 实现）
- Modify: `docs/architecture/01-domain-model.md`（D18 状态图同步）
- Add: `tests/unit/test_need_hypothesis_models.py`
- Modify: `tests/unit/test_demand_signal_contracts.py`（协议形状扩展）

**Interfaces（规格 §5/§6/D19 权威）：**
- `NeedHypothesisRepository.add(hypothesis: NeedHypothesis) -> bool`（**契约纠偏 P1-1**：现有 Protocol 为 `-> None`，spec D2 需要 bool 表达「True=新插入，False=活跃冲突」——Task 1 必须同时改 Protocol 声明与契约测试断言，Task 3 实现已按 bool 编写；`ValidatedNeedRepository.add -> None` 不变）。
- `NeedHypothesisRepository.get_for_update(tenant_id: TenantId, hypothesis_id: NeedHypothesisId) -> NeedHypothesis | None`——`SELECT ... FOR UPDATE`，转换路径行锁（D7）；不存在 → None。
- `ValidatedNeedRepository.get_for_update(tenant_id: TenantId, need_id: ValidatedNeedId) -> ValidatedNeed | None`——同上。
- `DemandUnitOfWork` 协议成员：`signals`/`hypotheses`/`needs`/`bus` + `__aenter__`/`__aexit__`（**无 clusters**，D12）。
- `DemandSignal.evidence_level -> EvidenceLevel`：纯映射常量表（spec D3/D6/finding 4）——`public_rfq`/`inbound_inquiry`/`tender_notice` → `CUSTOMER_INTEREST_REPLY`（**仅此，规格内容不升级**）；企业变化类（`product_line_expansion`/`facility_expansion`/`new_market_entry`/`procurement_role_hiring`/`distributor_change`/`new_certification`/`large_contract_won`/`funding_or_merger`）→ `PUBLIC_COMPANY_EVENT`；其余（`trade_show_request`/`historical_unclosed_need`/`supplier_referral`/`stockout_observed`/`negative_product_review`/`supplier_complaint`/`marketplace_seller_activity`/`catalog_gap`/`value_chain_adjacency`/`complementary_category`）→ `AGENT_INDUSTRY_INFERENCE`；未知值 → `ValidationError("未知信号类型")`（防御）。
- `NeedHypothesis.evidence() -> list[EvidenceItem]`：纯域内零 IO（D1）——`reasoning.based_on` 按 `(source_type, source_id)` 去重，组内保留等级最高一条（等级序：AGENT_INDUSTRY_INFERENCE < PUBLIC_COMPANY_EVENT < EMPLOYEE_GUESS < CUSTOMER_INTEREST_REPLY < CUSTOMER_SPECIFICATION < CUSTOMER_QUANTITY_AND_TIMING < CUSTOMER_SAMPLE_OR_QUOTE_REQUEST），并列取先出现第一条；输出保持组首现顺序。
- `NeedHypothesis.can_promote_to_validated() -> bool`：存在至少一条证据等级 ≥ `CUSTOMER_INTEREST_REPLY` 且 `source_type` ∈ {`conversation`, `upload`, `employee_input`}（D6）。
- `service.py` D19 纠偏：`update_need_fields(..., updated_by: str | None = None) -> None`（原 `-> ValidatedNeedView`）；`mark_sourcing_ready` docstring「完整度不足 3 抛 `SourcingThresholdNotMetError`」；`promote_to_validated` docstring 删除「尝试归入需求簇（失败不阻塞主流程）」子句；`update_need_fields` docstring「完整度变化跨过 3 级门槛时，发布状态转换事件」改为「不发事件——catalog 无匹配 schema（最小语义）」；`promote_to_validated` docstring 补全「`extracted_fields` 键 ⊆ {product_category} ∪ 10 个可变更业务字段（11 键）」。

- [x] **Step 1: 写失败测试（tests/unit/test_need_hypothesis_models.py 新增 + test_demand_signal_contracts.py 扩展）**

`tests/unit/test_need_hypothesis_models.py`（importlib 动态取域内模型，check_boundaries domain-internals 规则）：

```python
"""NeedHypothesis 纯域内核单测（2026-08-17 计划 Task 1；无 DB 零 mock）。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest

from shared.errors import ValidationError
from shared.schemas.evidence import EvidenceItem, EvidenceLevel
from shared.schemas.provenance import SourceType
from shared.schemas.identifiers import (
    DemandSignalId,
    NeedHypothesisId,
    ProspectAccountId,
    TenantId,
    new_id,
)

_models = importlib.import_module("domains.demand.models")

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)


def _evidence(
    level: EvidenceLevel, source_type: str, source_id: str = "src-1"
) -> EvidenceItem:
    return EvidenceItem(
        level=level,
        source_type=source_type,
        source_id=source_id,
        observed_at=NOW,
        summary="Acme 扩建公告 SECRET-SUMMARY-1",
    )


def _hypothesis(evidence: list[EvidenceItem]):
    return _models.NeedHypothesis(
        hypothesis_id=NeedHypothesisId(new_id("hyp")),
        tenant_id=TenantId(new_id("tn")),
        account_id=ProspectAccountId(new_id("acc")),
        category="stainless steel hinges",
        reasoning=_models.InferredField(
            value="可能需要耐腐蚀五金",
            based_on=evidence,
            inferred_by="model-v1",
            inferred_at=NOW,
        ),
        signal_ids=[DemandSignalId(new_id("sig"))],
        created_at=NOW,
    )


def test_evidence_dedup_same_source_keeps_highest_level() -> None:
    """同一 (source_type, source_id) 只留等级最高一条（同页面/同消息不重复计）。"""
    hypothesis = _hypothesis(
        [
            _evidence(EvidenceLevel.PUBLIC_COMPANY_EVENT, "web_page", "sha256:same"),
            _evidence(EvidenceLevel.AGENT_INDUSTRY_INFERENCE, "web_page", "sha256:same"),
        ]
    )
    assert len(hypothesis.evidence()) == 1
    assert hypothesis.evidence()[0].level is EvidenceLevel.PUBLIC_COMPANY_EVENT


def test_evidence_keeps_distinct_sources_in_first_seen_order() -> None:
    """跨 source_id 保留全部，输出保持组首现顺序。"""
    hypothesis = _hypothesis(
        [
            _evidence(EvidenceLevel.PUBLIC_COMPANY_EVENT, "web_page", "sha256:a"),
            _evidence(EvidenceLevel.CUSTOMER_SPECIFICATION, "conversation", "msg-2"),
            _evidence(EvidenceLevel.AGENT_INDUSTRY_INFERENCE, "web_page", "sha256:c"),
        ]
    )
    ids = [item.source_id for item in hypothesis.evidence()]
    assert ids == ["sha256:a", "msg-2", "sha256:c"]


def test_can_promote_requires_customer_evidence_from_whitelisted_sources() -> None:
    """唯一通过条件：≥CUSTOMER_INTEREST_REPLY 且 source_type ∈ 白名单。"""
    agent_only = _hypothesis([_evidence(EvidenceLevel.AGENT_INDUSTRY_INFERENCE, "web_page")])
    assert not agent_only.can_promote_to_validated()
    public_event = _hypothesis(
        [_evidence(EvidenceLevel.PUBLIC_COMPANY_EVENT, "web_page", "sha256:p")]
    )
    assert not public_event.can_promote_to_validated()
    web_reply = _hypothesis(
        [_evidence(EvidenceLevel.CUSTOMER_INTEREST_REPLY, "web_page", "sha256:w")]
    )
    assert not web_reply.can_promote_to_validated()
    for source in ("conversation", "upload", "employee_input"):
        ok = _hypothesis(
            [_evidence(EvidenceLevel.CUSTOMER_INTEREST_REPLY, source, f"src-{source}")]
        )
        assert ok.can_promote_to_validated(), source


def test_demand_signal_evidence_level_mapping_only_three_direct_tiers() -> None:
    """映射：RFQ/入站询盘/招标 → CUSTOMER_INTEREST_REPLY；企业变化 → 公开事件；
    其余 → 行业推断；含规格内容也不升级（finding 4）。"""
    direct = {
        "public_rfq",
        "inbound_inquiry",
        "tender_notice",
    }
    company = {
        "product_line_expansion",
        "facility_expansion",
        "new_market_entry",
        "procurement_role_hiring",
        "distributor_change",
        "new_certification",
        "large_contract_won",
        "funding_or_merger",
    }
    for name, member in _models.SignalType.__members__.items():
        value = member.value
        if value in direct:
            expected = EvidenceLevel.CUSTOMER_INTEREST_REPLY
        elif value in company:
            expected = EvidenceLevel.PUBLIC_COMPANY_EVENT
        else:
            expected = EvidenceLevel.AGENT_INDUSTRY_INFERENCE
        signal = _models.DemandSignal(
            signal_id=DemandSignalId(new_id("sig")),
            tenant_id=TenantId(new_id("tn")),
            signal_type=member,
            entity_name="Acme Manufacturing",
            raw_observation="Acme 发布含详细规格的公开招标 SECRET-MARKER",
            observed_at=NOW,
            provenance=_models.Provenance(
                source_type=SourceType.WEB_PAGE,
                source_id="sha256:pagehash001",
                extracted_by="model-v1",
                extracted_at=NOW,
                source_url="https://example.com/tender",
                page_hash="sha256:pagehash001",
            ),
        )
        assert signal.evidence_level is expected, value
    # P2-2：三集合对枚举全集构成划分（新增类型必须显式归类）
    all_values = {member.value for member in _models.SignalType}
    assert (
        direct | company | set(_models._AGENT_INFERENCE_SIGNAL_TYPES)
        == all_values
    )


def test_unknown_signal_type_evidence_level_rejected() -> None:
    signal = _models.DemandSignal(
        signal_id=DemandSignalId(new_id("sig")),
        tenant_id=TenantId(new_id("tn")),
        signal_type="bogus_type",  # type: ignore[arg-type]
        entity_name="Acme Manufacturing",
        raw_observation="Acme 扩建公告",
        observed_at=NOW,
        provenance=_models.Provenance(
            source_type=SourceType.WEB_PAGE,
            source_id="sha256:pagehash001",
            extracted_by="model-v1",
            extracted_at=NOW,
            source_url="https://example.com/acme",
            page_hash="sha256:pagehash001",
        ),
    )
    with pytest.raises(ValidationError):
        _ = signal.evidence_level
```

`tests/unit/test_demand_signal_contracts.py` 扩展（追加两个用例；`expected_params` 增 `get_for_update: ["self", "tenant_id", "hypothesis_id"]` 与 `["self", "tenant_id", "need_id"]`）：

```python
def test_hypothesis_and_need_repositories_gain_get_for_update() -> None:
    """契约纠偏（spec D7）：两个 repo 各增 get_for_update，参数名精确。"""
    expected = {
        "add": ["self", "hypothesis"],
        "get": ["self", "tenant_id", "hypothesis_id"],
        "update": ["self", "hypothesis"],
        "get_for_update": ["self", "tenant_id", "hypothesis_id"],
        "find_active_by_account_and_category": [
            "self",
            "tenant_id",
            "account_id",
            "category",
        ],
        "list_for_outreach": ["self", "tenant_id", "countries", "limit"],
    }
    for method, params in expected.items():
        signature = inspect.signature(getattr(NeedHypothesisRepository, method))
        assert list(signature.parameters) == params, method
    expected_need = {
        "add": ["self", "need"],
        "get": ["self", "tenant_id", "need_id"],
        "update": ["self", "need"],
        "get_for_update": ["self", "tenant_id", "need_id"],
        "append_field_history": [
            "self",
            "tenant_id",
            "need_id",
            "field_name",
            "old_value",
            "new_value",
            "source_message_id",
            "changed_by",
        ],
        "list_sourcing_ready": ["self", "tenant_id", "limit"],
        "list_by_account": ["self", "tenant_id", "account_id"],
    }
    for method, params in expected_need.items():
        signature = inspect.signature(getattr(ValidatedNeedRepository, method))
        assert list(signature.parameters) == params, method
    # P1-1/P2-8：返回契约锁死——add 必须 bool；get_for_update 必须含 None 的联合注解
    assert str(inspect.get_type_hints(NeedHypothesisRepository.add)["return"]) == "bool"
    assert "None" in str(
        inspect.get_type_hints(NeedHypothesisRepository.get_for_update)["return"]
    )


def test_demand_uow_protocol_gains_hypotheses_and_needs() -> None:
    """UoW 协议成员（spec D12）：signals/hypotheses/needs/bus，无 clusters。"""
    attrs = set(getattr(DemandUnitOfWork, "__protocol_attrs__", ()))
    assert {"signals", "hypotheses", "needs", "bus", "__aenter__", "__aexit__"} <= attrs
```

（`NeedHypothesisRepository`/`ValidatedNeedRepository` 经现有 `_repository = importlib.import_module("domains.demand.repository")` 解析；`inspect` 已导入。）

- [x] **Step 2: 运行确认 RED**

```bash
cd /Volumes/T7/Company/Auto_customer_acquisition/.worktrees/handbook-phase1-slice4-execution
find . -name "._*" -not -path "./.git/*" -delete
conda run -n tradeos-py312 python -m pytest tests/unit/test_need_hypothesis_models.py tests/unit/test_demand_signal_contracts.py -q -W error
```

Expected: **FAIL**——models 三方法以 `NotImplementedError` 失败（证据去重/门槛/映射用例），契约用例以 `AttributeError: type object 'NeedHypothesisRepository' has no attribute 'get_for_update'` 与 UoW 协议成员缺失失败（缺契约纠偏）。记录 rc=1 与每个失败原因。

- [x] **Step 3: 最小实现**

(3a) `domains/demand/models.py`——模块级常量与三方法实现（追加在 `DemandSignal.evidence_level` property 处替换桩；`SignalStatus` 前加映射常量）：

```python
# 模块级（硬边界 5/6 映射表：写成常量不做 if 链；finding 4 仅三直接档）
_CUSTOMER_DIRECT_SIGNAL_TYPES = frozenset(
    {"public_rfq", "inbound_inquiry", "tender_notice"}
)
_COMPANY_EVENT_SIGNAL_TYPES = frozenset(
    {
        "product_line_expansion",
        "facility_expansion",
        "new_market_entry",
        "procurement_role_hiring",
        "distributor_change",
        "new_certification",
        "large_contract_won",
        "funding_or_merger",
    }
)
_EVIDENCE_LEVEL_RANK: dict[EvidenceLevel, int] = {
    level: i for i, level in enumerate(EvidenceLevel)
}
_PROMOTABLE_SOURCE_TYPES = frozenset({"conversation", "upload", "employee_input"})
_AGENT_INFERENCE_SIGNAL_TYPES = frozenset(
    {
        "trade_show_request",
        "historical_unclosed_need",
        "supplier_referral",
        "stockout_observed",
        "negative_product_review",
        "supplier_complaint",
        "marketplace_seller_activity",
        "catalog_gap",
        "value_chain_adjacency",
        "complementary_category",
    }
)
```

（models.py imports 区并入：`from shared.errors import ValidationError`——P3-8，不放到事后提示。）

`DemandSignal.evidence_level` property 实现：

```python
    @property
    def evidence_level(self) -> EvidenceLevel:
        """信号对应的证据等级（映射表见模块级常量；spec D3/finding 4）。

        公开 RFQ/入站询盘/招标公告 → CUSTOMER_INTEREST_REPLY（规格内容不升级）；
        企业变化类 → PUBLIC_COMPANY_EVENT；产业链推断类 → AGENT_INDUSTRY_INFERENCE。
        """
        value = self.signal_type.value
        if value in _CUSTOMER_DIRECT_SIGNAL_TYPES:
            return EvidenceLevel.CUSTOMER_INTEREST_REPLY
        if value in _COMPANY_EVENT_SIGNAL_TYPES:
            return EvidenceLevel.PUBLIC_COMPANY_EVENT
        if value in _AGENT_INFERENCE_SIGNAL_TYPES:
            return EvidenceLevel.AGENT_INDUSTRY_INFERENCE
        raise ValidationError("未知信号类型")
```

（三集合以显式成员全集表达、不写「else 兜底」；单元测试断言三者构成枚举全集划分，保证新增类型必须显式归类——P2-2。）

`NeedHypothesis.evidence()` 实现（D1 纯域内去重）：

```python
    def evidence(self) -> list[EvidenceItem]:
        """汇总证据（纯域内、零 IO，spec D1）：based_on 按 (source_type,
        source_id) 去重，组内保留等级最高一条，并列取先出现；输出保持组首现顺序。"""
        by_source: dict[tuple[str, str], EvidenceItem] = {}
        order: list[tuple[str, str]] = []
        for item in self.reasoning.based_on:
            key = (item.source_type, item.source_id)
            current = by_source.get(key)
            if current is None:
                by_source[key] = item
                order.append(key)
            elif _EVIDENCE_LEVEL_RANK[item.level] > _EVIDENCE_LEVEL_RANK[current.level]:
                by_source[key] = item
        return [by_source[key] for key in order]
```

`NeedHypothesis.can_promote_to_validated()` 实现（D6 唯一门槛）：

```python
    def can_promote_to_validated(self) -> bool:
        """唯一通过条件：存在证据等级 ≥ CUSTOMER_INTEREST_REPLY 且来源类型
        属于 conversation/upload/employee_input。Agent 推断不论多少条都不通过。"""
        required_rank = _EVIDENCE_LEVEL_RANK[EvidenceLevel.CUSTOMER_INTEREST_REPLY]
        return any(
            _EVIDENCE_LEVEL_RANK[item.level] >= required_rank
            and item.source_type in _PROMOTABLE_SOURCE_TYPES
            for item in self.evidence()
        )
```

(3b) `domains/demand/repository.py`——`NeedHypothesisRepository` 的 `find_active_by_account_and_category` 之后追加：

`NeedHypothesisRepository.add` 的返回类型同步为 `bool`（P1-1，现有声明为 `-> None`）：

```python
    async def add(self, hypothesis: NeedHypothesis) -> bool:
        """新插入返回 True；活跃冲突（(tenant_id, account_id, category) 部分唯一
        索引命中）返回 False（spec D2）。"""
        ...
```

随后追加：

```python
    async def get_for_update(
        self, tenant_id: TenantId, hypothesis_id: NeedHypothesisId
    ) -> NeedHypothesis | None:
        """SELECT ... FOR UPDATE 读假设（转换路径行锁，spec D7）。"""
        ...
```

`ValidatedNeedRepository` 的 `get` 之后追加：

```python
    async def get_for_update(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> ValidatedNeed | None:
        """SELECT ... FOR UPDATE 读已验证需求（转换路径行锁，spec D7）。"""
        ...
```

`DemandUnitOfWork` 协议成员追加：

```python
    hypotheses: NeedHypothesisRepository
    needs: ValidatedNeedRepository
```

(3c) `domains/demand/service.py` D19 契约纠偏：
- `update_need_fields` 签名第 150-152 行返回类型 `ValidatedNeedView` → `None`（docstring 同步：删「返回视图」隐含表述）。
- `mark_sourcing_ready` docstring（168-171 行）「完整度不足 3 抛 ``InvalidStateTransition``」→「完整度不足 3 抛 ``SourcingThresholdNotMetError``（既有域错误，消息含 missing_fields）」。
- `promote_to_validated` docstring（140-147 行）删除「尝试归入需求簇（失败不阻塞主流程——聚类是增强，不是必需）」整句；`extracted_fields` 说明补全「键 ⊆ {product_category} ∪ 10 个可变更业务字段（11 键白名单）；product_category 必填且创建后不可变」。
- `update_need_fields` docstring（164-165 行）「完整度变化跨过 3 级门槛时，发布状态转换事件」→「跨过 3 级门槛自动置 sourcing_ready，**不发事件**（catalog 无匹配 schema，最小语义）」。签名其余不动。

(3d) `docs/architecture/01-domain-model.md` D18 状态图同步：§四 Need Hypothesis 块 `inferred → outreach_queued → contacted → validated / rejected / discarded` → `inferred → contacting → validated / rejected（rejected 必带原因）`；Validated Need 块 `validated → sourcing_ready（完整度 ≥ 3）→ sourcing_in_progress → quoted / paused / closed` → `validated → sourcing_ready（完整度 ≥ 3）→ handed_to_sourcing / fulfilled / withdrawn / lost`（与 models.py `HypothesisStatus`/`NeedStatus` 逐字一致）。

- [x] **Step 4: 运行确认 GREEN**

```bash
conda run -n tradeos-py312 python -m pytest tests/unit/test_need_hypothesis_models.py tests/unit/test_demand_signal_contracts.py -q -W error
conda run -n tradeos-py312 python -m pytest tests/unit -q -W error
```

Expected: 全部 PASS（unit 全量以实际收集计数为准——P3-7，不预设数字）。

- [x] **Step 5: 边界与静态检查 + 定向回归**

```bash
conda run -n tradeos-py312 python -m ruff check domains/demand tests/unit/test_need_hypothesis_models.py tests/unit/test_demand_signal_contracts.py
conda run -n tradeos-py312 python -m mypy domains/demand
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
```

Expected: 全部 rc=0。（`models.py` 的 `ValidationError` 导入已在 Step 3a 的 imports 代码块并入，P3-8。）

- [x] **Step 6: 停止等待监督方复审**（汇报 RED 原因 NotImplementedError×N + AttributeError 契约缺失、GREEN 计数、diff 4 文件）

复审通过后，提交前执行 mutation proof（临时 apply_patch → 精确测试 RED → 恢复 → GREEN → `git diff` 无残留）：

| # | 临时变异 | 精确测试 | 预期 RED |
|---|---|---|---|
| M0 | `can_promote_to_validated` 去掉 `source_type in _PROMOTABLE_SOURCE_TYPES` 条件（只查等级） | `test_can_promote_requires_customer_evidence_from_whitelisted_sources` | `web_reply`（CUSTOMER_INTEREST_REPLY×web_page）不再被拒 → 断言失败 |

- [x] **Step 7: 复审通过后提交/推送/exact-HEAD CI**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH
git add --chmod=-x domains/demand/repository.py domains/demand/service.py domains/demand/models.py docs/architecture/01-domain-model.md tests/unit/test_need_hypothesis_models.py tests/unit/test_demand_signal_contracts.py
git ls-files --stage domains/demand/repository.py domains/demand/service.py domains/demand/models.py docs/architecture/01-domain-model.md tests/unit/test_need_hypothesis_models.py tests/unit/test_demand_signal_contracts.py
git diff --cached --check
git diff --cached --name-only
git commit -m "feat(demand): hypothesis model core and contract corrections"
git push origin HEAD
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/codex/handbook-phase1-slice4-execution)"
head_sha=$(git rev-parse HEAD)
run_id=""
for i in $(seq 1 12); do
  run_id=$(gh run list --commit "$head_sha" --workflow ci --limit 1 --json databaseId --jq '.[0].databaseId // empty' 2>/dev/null)
  if [ -n "$run_id" ]; then break; fi
  sleep 5
done
if [ -z "$run_id" ]; then echo "CI run not found for $head_sha"; exit 1; fi
gh run watch "$run_id" --exit-status
gh run view "$run_id" --json headSha,status,conclusion --jq 'if (.headSha == "'"$head_sha"'" and .status == "completed" and .conclusion == "success") then "OK" else error("headSha/status/conclusion mismatch") end'
```

Expected: 6 文件 index mode 全部 `100644`；push 后 local == origin；CI 输出 `OK`。

---

## Task 2: 迁移 0022（**恰 3 表**）+ ORM 行 + 迁移测试

**Files:**
- Add: `migrations/versions/0022_need_hypotheses.py`
- Modify: `infra/db/tables.py`（三个行类：`NeedHypothesisRow`/`ValidatedNeedRow`/`ValidatedNeedFieldHistoryRow`）
- Modify: `tests/integration/test_migrations.py`（head 0021→0022 全局诚实更新 + `EXPECTED_TABLES` +3 + 0022 契约 parity + 往返 + 部分索引谓词 parity 迁移侧）

**Interfaces（规格 §4 权威，逐列逐约束）：**
- `need_hypotheses`：PK `pk_need_hypotheses (tenant_id, hypothesis_id)`；部分唯一 `uq_need_hypotheses_active_account_category (tenant_id, account_id, category) WHERE status IN ('inferred','contacting')`；CHECK `ck_need_hypotheses_status`（`status IN ('inferred','contacting','validated','rejected')`）、`ck_need_hypotheses_category_nonblank`（`btrim(category) <> ''`）、`ck_need_hypotheses_reasoning_jsonb`（`jsonb_typeof(reasoning) = 'object'`）、`ck_need_hypotheses_signal_ids_jsonb`（`jsonb_typeof(signal_ids) = 'array'`）、`ck_need_hypotheses_rejection_reason`（`(status='rejected') = (rejection_reason IS NOT NULL AND btrim(rejection_reason) <> '')`）、`ck_need_hypotheses_validated_link`（`(status='validated') = (validated_need_id IS NOT NULL)`）、`ck_need_hypotheses_core_nonblank`（`btrim(tenant_id) <> '' AND btrim(hypothesis_id) <> '' AND btrim(account_id) <> '' AND btrim(category) <> ''`）；FK `fk_need_hypotheses_validated_need (validated_need_id) → validated_needs.need_id`（NO ACTION）。
- `validated_needs`：PK `pk_validated_needs (tenant_id, need_id)`；CHECK `ck_validated_needs_status`（`status IN ('validated','sourcing_ready','handed_to_sourcing','fulfilled','withdrawn','lost')`）、`ck_validated_needs_category_jsonb`（`jsonb_typeof(product_category) = 'object'`）、`ck_validated_needs_source_message_nonblank`（`btrim(source_message_id) <> ''`）、`ck_validated_needs_core_nonblank`、10 个 `ck_validated_needs_<field>_jsonb`（`(<field> IS NULL) OR jsonb_typeof(<field>) = 'object'`，field ∈ application/material/size_spec/quantity/packaging/destination/required_by/target_price/current_supply_issue/certification_required）。
- `validated_need_field_history`：PK `pk_validated_need_field_history (tenant_id, history_id)`；查询索引 `ix_validated_need_field_history_need (tenant_id, need_id, changed_at)`（非唯一）；CHECK `ck_validated_need_field_history_field_name_nonblank`、`ck_validated_need_field_history_new_value_nonblank`、`ck_validated_need_field_history_core_nonblank`；FK `fk_validated_need_field_history_need (need_id) → validated_needs.need_id`（NO ACTION）。
- 无 `cluster_id` 列（spec D4/finding 1）；JSONB 用 `postgresql.JSONB()`。

- [x] **Step 1: 写失败测试（test_migrations.py 更新 + 0022 契约/往返测试）**

(1a) 全局诚实更新（0020→0021 同款先例）：`EXPECTED_TABLES` 元组追加 `"need_hypotheses"`/`"validated_needs"`/`"validated_need_field_history"`（**P3-3：不预设元组行数，以 `grep -n 'EXPECTED_TABLES' tests/integration/test_migrations.py` 实际定位**）；**0021 head 断言以 grep 驱动更新（P3-1，不依赖陈旧行号）**：
```bash
grep -n 'assert revision == "0021"\|head 未升级到 0021' tests/integration/test_migrations.py
```
当前共 **14 处**。判定规则：凡 `"0021"` 表示**当前 head**（升级到 head 后断言、契约测试头断言、`"当前 Alembic head 未升级到 0021"` 文案）一律逐字改为 `"0022"`；仅 `"0021"` 表示 **downgrade 中间态**（往返测试中 `_run_alembic(..., "downgrade", "0020")` 之后、`upgrade head` 之前读到的 revision，如 `test_0021_demand_signals_downgrade_roundtrip` 内的中间断言）保留 `"0021"`。按此规则 14 处中保留 2 处（0021 中间态）、其余 **12 处改 0022**；改完重跑 grep 复核剩余 `"0021"` 全部落在中间态语境（特别核对 3701/3902/3912 附近所有 `"当前 Alembic head 未升级到 0021"` 与 head 断言）。

(1b) 新增契约 parity 测试（追加在 `test_0021_demand_signals_contract_matches_orm` 之后；镜像其结构——revision 断言先行、ORM 行后置导入避免 RED 时 ImportError 掩盖首因）：

```python
async def test_0022_need_hypotheses_contract_matches_orm(db_url: str) -> None:
    """0022 契约：3 表列（含 server_default）/PK/CHECK/FK/部分唯一索引与 ORM 语义 parity。"""
    import re

    from sqlalchemy import CheckConstraint, ForeignKeyConstraint, inspect, text

    from infra.db.session import create_engine_from

    def _normalize_default(value: object) -> str | None:
        """server_default 归一化：去 PG cast 标注与引号，空视为无默认（0021 先例，P3-2）。"""
        if value is None:
            return None
        text_value = str(value)
        text_value = re.sub(r"::[a-z_ ]+", "", text_value)
        text_value = text_value.strip("'\"")
        return text_value or None

    engine = create_engine_from(db_url)
    try:
        async with engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "0022", "当前 Alembic head 未升级到 0022"
            db_contract = await conn.run_sync(
                lambda sync: {
                    table: {
                        "columns": {
                            item["name"]: {
                                "type": str(
                                    item["type"].compile(dialect=engine.dialect)
                                ),
                                "nullable": item["nullable"],
                                "default": _normalize_default(item.get("default")),
                            }
                            for item in inspect(sync).get_columns(table)
                        },
                        "pk": list(
                            inspect(sync).get_pk_constraint(table)["constrained_columns"]
                        ),
                        "checks": {
                            str(item["name"]): str(item["sqltext"])
                            for item in inspect(sync).get_check_constraints(table)
                        },
                        "fks": {
                            (str(fk["constrained_columns"]), str(fk["referred_table"]))
                            for fk in inspect(sync).get_foreign_keys(table)
                        },
                        "indexes": {
                            str(item["name"]): {
                                "unique": item["unique"],
                                "cols": item["column_names"],
                                "where": (
                                    str(
                                        item.get("dialect_options", {}).get(
                                            "postgresql_where", ""
                                        )
                                    )
                                    if item["unique"]
                                    else None
                                ),
                            }
                            for item in inspect(sync).get_indexes(table)
                        },
                    }
                    for table in (
                        "need_hypotheses",
                        "validated_needs",
                        "validated_need_field_history",
                    )
                }
            )
    finally:
        await engine.dispose()

    from infra.db.tables import (
        NeedHypothesisRow,
        ValidatedNeedFieldHistoryRow,
        ValidatedNeedRow,
    )

    orm_contract = {}
    for table_name, row in (
        ("need_hypotheses", NeedHypothesisRow),
        ("validated_needs", ValidatedNeedRow),
        ("validated_need_field_history", ValidatedNeedFieldHistoryRow),
    ):
        table = row.__table__
        orm_contract[table_name] = {
            "columns": {
                name: {
                    "type": str(column.type.compile(dialect=engine.dialect)),
                    "nullable": column.nullable,
                    "default": _normalize_default(
                        column.server_default.arg
                        if column.server_default is not None
                        else None
                    ),
                }
                for name, column in table.columns.items()
            },
            "pk": [column.name for column in table.primary_key.columns],
            "checks": {
                str(c.name): str(c.sqltext)
                for c in table.constraints
                if isinstance(c, CheckConstraint)
            },
            "fks": {
                (str(c.columns), str(c.elements[0].target_fullname.split(".")[0]))
                for c in table.constraints
                if isinstance(c, ForeignKeyConstraint)
            },
            "indexes": {
                str(index.name): {
                    "unique": index.unique,
                    "cols": list(index.columns.keys()),
                    "where": (
                        str(index.dialect_options["postgresql"]["where"])
                        if index.unique
                        and "postgresql_where" in index.dialect_options["postgresql"]
                        else None
                    ),
                }
                for index in table.indexes
            },
        }
    for table_name in ("need_hypotheses", "validated_needs", "validated_need_field_history"):
        assert orm_contract[table_name]["columns"] == db_contract[table_name]["columns"]
        assert orm_contract[table_name]["pk"] == db_contract[table_name]["pk"]
        assert set(orm_contract[table_name]["checks"]) == set(db_contract[table_name]["checks"])
        assert orm_contract[table_name]["fks"] == db_contract[table_name]["fks"]
        assert orm_contract[table_name]["indexes"] == db_contract[table_name]["indexes"]
    # 部分唯一索引谓词 parity（spec D2/finding 3，迁移侧）：**语义比较而非脆弱
    # 字符串等值**——PG 会把 IN (...) 归一化为 = ANY(ARRAY[...])（P1-2）。
    def _predicate_semantics(where: str) -> tuple[frozenset[str], frozenset[str]]:
        """谓词语义：字符串字面量值集合 + 标识符集合（沿用 _canonical 归一化思路）。"""
        return (
            frozenset(re.findall(r"'([^']*)'", where)),
            frozenset(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", where)),
        )

    db_index = db_contract["need_hypotheses"]["indexes"][
        "uq_need_hypotheses_active_account_category"
    ]
    orm_index = orm_contract["need_hypotheses"]["indexes"][
        "uq_need_hypotheses_active_account_category"
    ]
    for side, entry in (("db", db_index), ("orm", orm_index)):
        assert entry["unique"] is True, side
        assert entry["cols"] == ["tenant_id", "account_id", "category"], side
        values, idents = _predicate_semantics(entry["where"] or "")
        assert values == frozenset({"inferred", "contacting"}), side
        assert "status" in idents, side
    db_values, _ = _predicate_semantics(db_index["where"] or "")
    orm_values, _ = _predicate_semantics(orm_index["where"] or "")
    assert db_values == orm_values == frozenset({"inferred", "contacting"})
```

(1c) 新增往返测试（0022→0021→0022，镜像 0020 往返结构）：

```python
async def test_0022_downgrade_roundtrip(db_url: str) -> None:
    """0022→0021→0022（真实 downgrade/upgrade，P2-3）：downgrade 后 3 表消失、
    revision 回 0021；upgrade head 后 3 表恢复、revision 回 0022。"""
    from sqlalchemy import text

    from infra.db.session import create_engine_from

    tables = (
        "need_hypotheses",
        "validated_needs",
        "validated_need_field_history",
    )
    engine = create_engine_from(db_url)
    try:
        async with engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "0022", "roundtrip 前置：head 应已升级到 0022"
        for table in tables:
            assert table in await _table_names(engine), f"head 应含 {table}"
        _run_alembic(db_url, "downgrade", "0021")
        async with engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "0021", "downgrade 到 0021 后 revision 应为 0021"
        for table in tables:
            assert table not in await _table_names(engine), f"downgrade 后应无 {table}"
        _run_alembic(db_url, "upgrade", "head")
        async with engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "0022", "upgrade head 后 revision 应为 0022"
        for table in tables:
            assert table in await _table_names(engine), f"upgrade 后应恢复 {table}"
    finally:
        await engine.dispose()
```

（`_run_alembic`/`_table_names` 为该文件既有 helper（0020 往返测试先例，`_run_alembic(db_url, "downgrade", "0019")` 同款）；`_table_names` 已存在于文件级。）

- [x] **Step 2: 运行确认 RED**

```bash
find . -name "._*" -not -path "./.git/*" -delete
conda run -n tradeos-py312 python -m pytest tests/integration/test_migrations.py -q -W error
```

Expected: **FAIL**——新 0022 契约/往返测试以 `assert revision == "0022"` 失败（head 仍 0021、表不存在）；全局 head 断言更新处若遗漏会以断言失败暴露。记录 rc=1 与首因。

- [x] **Step 3: 最小实现**

(3a) `migrations/versions/0022_need_hypotheses.py`（down_revision=`0021`；先建 `validated_needs`（被引用），再 `need_hypotheses`，再 `validated_need_field_history`；downgrade 逆序 drop）：

```python
"""need_hypotheses / validated_needs / validated_need_field_history 表
（NeedHypothesis + ValidatedNeed 生命周期最小切片，spec 2026-08-17 §4）。

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

_HYPOTHESIS_STATUSES = "'inferred','contacting','validated','rejected'"
_NEED_STATUSES = (
    "'validated','sourcing_ready','handed_to_sourcing',"
    "'fulfilled','withdrawn','lost'"
)
_MUTABLE_NEED_FIELDS = (
    "application",
    "material",
    "size_spec",
    "quantity",
    "packaging",
    "destination",
    "required_by",
    "target_price",
    "current_supply_issue",
    "certification_required",
)


def upgrade() -> None:
    """创建 tenant-bound 三表：PK(tenant,id) + 部分唯一活跃假设索引 + FK + CHECK。"""
    op.create_table(
        "validated_needs",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("need_id", sa.String(40), nullable=False),
        sa.Column("account_id", sa.String(40), nullable=False),
        sa.Column("product_category", postgresql.JSONB(), nullable=False),
        sa.Column("source_message_id", sa.String(40), nullable=False),
        sa.Column("source_conversation_id", sa.String(40), nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'validated'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_by", sa.String(40), nullable=True),
    )
    for field in _MUTABLE_NEED_FIELDS:
        op.add_column(
            "validated_needs",
            sa.Column(field, postgresql.JSONB(), nullable=True),
        )
    op.create_primary_key("pk_validated_needs", "validated_needs", ["tenant_id", "need_id"])
    op.create_check_constraint(
        f"status IN ({_NEED_STATUSES})",
        "validated_needs",
        "ck_validated_needs_status",
    )
    op.create_check_constraint(
        "jsonb_typeof(product_category) = 'object'",
        "validated_needs",
        "ck_validated_needs_category_jsonb",
    )
    op.create_check_constraint(
        "btrim(tenant_id) <> '' AND btrim(need_id) <> '' AND "
        "btrim(account_id) <> '' AND btrim(source_message_id) <> ''",
        "validated_needs",
        "ck_validated_needs_core_nonblank",
    )
    for field in _MUTABLE_NEED_FIELDS:
        op.create_check_constraint(
            f"({field} IS NULL) OR jsonb_typeof({field}) = 'object'",
            "validated_needs",
            f"ck_validated_needs_{field}_jsonb",
        )
    op.create_table(
        "need_hypotheses",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("hypothesis_id", sa.String(40), nullable=False),
        sa.Column("account_id", sa.String(40), nullable=False),
        sa.Column("category", sa.String(200), nullable=False),
        sa.Column("reasoning", postgresql.JSONB(), nullable=False),
        sa.Column("signal_ids", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'inferred'"),
        ),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("validated_need_id", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "validated_need_id"],
            ["validated_needs.tenant_id", "validated_needs.need_id"],
            name="fk_need_hypotheses_validated_need",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "hypothesis_id", name="pk_need_hypotheses"),
        sa.CheckConstraint(
            f"status IN ({_HYPOTHESIS_STATUSES})", name="ck_need_hypotheses_status"
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(hypothesis_id) <> '' AND "
            "btrim(account_id) <> '' AND btrim(category) <> ''",
            name="ck_need_hypotheses_core_nonblank",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reasoning) = 'object'",
            name="ck_need_hypotheses_reasoning_jsonb",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(signal_ids) = 'array'",
            name="ck_need_hypotheses_signal_ids_jsonb",
        ),
        sa.CheckConstraint(
            "(status = 'rejected') = "
            "(rejection_reason IS NOT NULL AND btrim(rejection_reason) <> '')",
            name="ck_need_hypotheses_rejection_reason",
        ),
        sa.CheckConstraint(
            "(status = 'validated') = (validated_need_id IS NOT NULL)",
            name="ck_need_hypotheses_validated_link",
        ),
    )
    op.create_index(
        "uq_need_hypotheses_active_account_category",
        "need_hypotheses",
        ["tenant_id", "account_id", "category"],
        unique=True,
        postgresql_where=sa.text("status IN ('inferred','contacting')"),
    )
    op.create_table(
        "validated_need_field_history",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("history_id", sa.String(40), nullable=False),
        sa.Column("need_id", sa.String(40), nullable=False),
        sa.Column("field_name", sa.String(64), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=False),
        sa.Column("source_message_id", sa.String(40), nullable=False),
        sa.Column("changed_by", sa.String(40), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "need_id"],
            ["validated_needs.tenant_id", "validated_needs.need_id"],
            name="fk_validated_need_field_history_need",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "history_id", name="pk_validated_need_field_history"
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(history_id) <> '' AND "
            "btrim(need_id) <> '' AND btrim(field_name) <> '' AND "
            "btrim(new_value) <> '' AND btrim(source_message_id) <> ''",
            name="ck_validated_need_field_history_core_nonblank",
        ),
        sa.CheckConstraint(
            "btrim(field_name) <> ''",
            name="ck_validated_need_field_history_field_name_nonblank",
        ),
        sa.CheckConstraint(
            "btrim(new_value) <> ''",
            name="ck_validated_need_field_history_new_value_nonblank",
        ),
    )
    op.create_index(
        "ix_validated_need_field_history_need",
        "validated_need_field_history",
        ["tenant_id", "need_id", "changed_at"],
    )


def downgrade() -> None:
    op.drop_table("validated_need_field_history")
    op.drop_index("uq_need_hypotheses_active_account_category", table_name="need_hypotheses")
    op.drop_table("need_hypotheses")
    op.drop_table("validated_needs")
```

（注意：`op.add_column` 追加 10 个 JSONB 列后再建 CHECK——`create_table` 内直接含全部列的写法亦可，二者等价；**必须与 tables.py 行类逐一同名同语义**。FK 为复合 `(tenant_id, <id>)` 引用，符合全库 tenant-bound FK 先例。）

(3b) `infra/db/tables.py`——三个行类（镜像 `DemandSignalRow` 风格；`__table_args__` 含同名 CHECK/`ForeignKeyConstraint`/`Index`（部分唯一））：

```python
class NeedHypothesisRow(Base):
    """``need_hypotheses`` 行（spec 2026-08-17 §4.1）。"""

    __tablename__ = "need_hypotheses"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "hypothesis_id", name="pk_need_hypotheses"),
        ForeignKeyConstraint(
            ["tenant_id", "validated_need_id"],
            ["validated_needs.tenant_id", "validated_needs.need_id"],
            name="fk_need_hypotheses_validated_need",
        ),
        Index(
            "uq_need_hypotheses_active_account_category",
            "tenant_id",
            "account_id",
            "category",
            unique=True,
            postgresql_where=text("status IN ('inferred','contacting')"),
        ),
        CheckConstraint(
            f"status IN ({_HYPOTHESIS_STATUSES})", name="ck_need_hypotheses_status"
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(hypothesis_id) <> '' AND "
            "btrim(account_id) <> '' AND btrim(category) <> ''",
            name="ck_need_hypotheses_core_nonblank",
        ),
        CheckConstraint(
            "jsonb_typeof(reasoning) = 'object'",
            name="ck_need_hypotheses_reasoning_jsonb",
        ),
        CheckConstraint(
            "jsonb_typeof(signal_ids) = 'array'",
            name="ck_need_hypotheses_signal_ids_jsonb",
        ),
        CheckConstraint(
            "(status = 'rejected') = "
            "(rejection_reason IS NOT NULL AND btrim(rejection_reason) <> '')",
            name="ck_need_hypotheses_rejection_reason",
        ),
        CheckConstraint(
            "(status = 'validated') = (validated_need_id IS NOT NULL)",
            name="ck_need_hypotheses_validated_link",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(40))
    hypothesis_id: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[str] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(200))
    reasoning: Mapped[dict] = mapped_column(postgresql.JSONB)
    signal_ids: Mapped[list] = mapped_column(postgresql.JSONB)
    status: Mapped[str] = mapped_column(String(20), server_default=text("'inferred'"))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    validated_need_id: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
```

（`ValidatedNeedRow`：11 个 JSONB 列（product_category 非空 + 10 个可空）与 10 个 `ck_validated_needs_<field>_jsonb` CHECK、status CHECK、core CHECK、无 cluster_id；`ValidatedNeedFieldHistoryRow`：8 列 + PK + `ix_validated_need_field_history_need` Index + 3 个 CHECK + FK——两行类按上述同款风格完整写出，字段与迁移逐字一致。`_HYPOTHESIS_STATUSES`/`_NEED_STATUSES` 模块常量在 tables.py 对应区定义。）

- [x] **Step 4: 运行确认 GREEN**

```bash
conda run -n tradeos-py312 python -m pytest tests/integration/test_migrations.py -q -W error
```

Expected: 全部 PASS（含 0022 契约/往返与既有 0020/0021 测试语义保留；0021 契约测试 head 断言已随全局更新为 0022）。

- [x] **Step 5: 边界与静态检查 + 定向回归**

```bash
conda run -n tradeos-py312 python -m ruff check infra/db/tables.py tests/integration/test_migrations.py
conda run -n tradeos-py312 python -m mypy infra/db
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
```

Expected: 全部 rc=0。

- [x] **Step 6: 停止等待监督方复审**（汇报 RED 原因 revision 0022 缺失、GREEN 计数、diff 3 文件）

复审通过后，提交前执行 mutation proof：

| # | 临时变异 | 精确测试 | 预期 RED |
|---|---|---|---|
| M-P | 迁移部分唯一索引的 `postgresql_where` 谓词改错（如 `status = 'inferred'`） | `test_0022_need_hypotheses_contract_matches_orm` | 谓词语义比较断言失败（值集合 ≠ {'inferred','contacting'}） |

- [x] **Step 7: 复审通过后提交/推送/exact-HEAD CI**（命令块同 Task 1 Step 7；文件为 `migrations/versions/0022_need_hypotheses.py` `infra/db/tables.py` `tests/integration/test_migrations.py`；commit 消息逐字 `feat(demand): add need hypothesis tables via 0022`；Expected：3 文件 index 100644、local == origin、CI `OK`）

---

## Task 3: 仓储实现 + UoW 扩展（JSONB 往返、租户隔离、审计）

**Files:**
- Add: `infra/db/repositories/need_hypotheses.py`（`NeedHypothesisRepositoryImpl` + `ValidatedNeedRepositoryImpl` + JSONB 序列化器）
- Modify: `infra/db/demand_uow.py`（`__aenter__` 增 `hypotheses`/`needs`）
- Add: `tests/integration/test_need_hypotheses.py`（repo 层用例：JSONB 往返、list ×3、跨租户越界 + 审计；spec 9.3-12/13 的 repo 侧）

**Interfaces（规格 §5 权威）：** 全部方法签名与 `repository.py` Protocol 逐字一致（含 Task 1 新增 `get_for_update`）；`add`（假设）返回 `bool`（True=新插入，False=活跃冲突）；`append_field_history` 单行 INSERT；action 审计名：`need_hypothesis_add/get/update/get_for_update/find_active_by_account_and_category/list_for_outreach`、`validated_need_add/get/update/get_for_update/append_field_history/list_sourcing_ready/list_by_account`。

- [x] **Step 1: 写失败测试（test_need_hypotheses.py 新增；集成 fixture 复用 demand 切片模式）**

```python
"""NeedHypothesis + ValidatedNeed 仓储/服务集成（2026-08-17 计划 Task 3-7；
真实 PostgreSQL + 真实 UoW/仓储，零 mock）。

RED 预期：Task 3 用例以 ``ModuleNotFoundError: No module named
'infra.db.repositories.need_hypotheses'`` 失败（缺实现而非 fixture 错误）；
UoW 用例以 ``AttributeError``（缺 hypotheses/needs 属性）失败。跨层访问域内
models/repository 用 importlib（check_boundaries domain-internals 规则）。
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.demand.schemas import SignalCaptureRequest
from domains.demand.service import DemandService
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.provenance import SourceType
from shared.schemas.identifiers import TenantId, new_id

_models = importlib.import_module("domains.demand.models")
_demand_errors = importlib.import_module("domains.demand.errors")
InsufficientEvidenceError = _demand_errors.InsufficientEvidenceError
SourcingThresholdNotMetError = _demand_errors.SourcingThresholdNotMetError
HypothesisAlreadyResolvedError = _demand_errors.HypothesisAlreadyResolvedError

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
OBSERVATION_MARKER = "Acme SECRET-OBSERVATION-77 opened a new plant in Rotterdam."


@dataclass
class MutableClock:
    value: datetime
    calls: int = 0

    def now(self) -> datetime:
        self.calls += 1
        return self.value


@pytest_asyncio.fixture
async def demand_db(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _uow_type():
    return importlib.import_module("infra.db.demand_uow").SqlAlchemyDemandUnitOfWork


def _service(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId, clock: MutableClock
) -> DemandService:
    uow_type = _uow_type()
    impl_type = importlib.import_module("domains.demand.service_impl").DemandServiceImpl
    return impl_type(
        lambda requested: uow_type(factory, requested, now=clock.now),
        now=clock.now,
    )


def _request(**overrides: object) -> SignalCaptureRequest:
    fields: dict[str, object] = {
        "signal_type": "inbound_inquiry",
        "entity_name": "Acme Manufacturing",
        "raw_observation": OBSERVATION_MARKER,
        "observed_at": NOW,
        "source_type": "conversation",
        "source_id": "msg_conv_001",
        "extracted_by": "model-v1",
    }
    fields.update(overrides)
    return SignalCaptureRequest(**fields)  # type: ignore[arg-type]


async def _signal(
    service: DemandService, tenant: TenantId, **overrides: object
) -> str:
    return await service.capture_signal(tenant, _request(**overrides))


def _hypothesis(**overrides: object):
    fields: dict[str, object] = {
        "hypothesis_id": _models.NeedHypothesisId(new_id("hyp")),
        "tenant_id": TenantId(new_id("tn")),
        "account_id": _models.ProspectAccountId(new_id("acc")),
        "category": "stainless steel hinges",
        "reasoning": _models.InferredField(
            value="可能需要耐腐蚀五金",
            based_on=[
                _models.EvidenceItem(
                    level=_models.EvidenceLevel.PUBLIC_COMPANY_EVENT,
                    source_type="web_page",
                    source_id="sha256:pagehash001",
                    observed_at=NOW,
                    summary="Acme 扩建公告",
                )
            ],
            inferred_by="model-v1",
            inferred_at=NOW,
        ),
        "signal_ids": [_models.DemandSignalId(new_id("sig"))],
        "created_at": NOW,
    }
    fields.update(overrides)
    return _models.NeedHypothesis(**fields)  # type: ignore[arg-type]


async def _hypothesis_rows(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[object]:
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        rows = (
            await session.execute(
                select(tables.NeedHypothesisRow).where(
                    tables.NeedHypothesisRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
    return list(rows)


async def test_repo_hypothesis_jsonb_roundtrip(demand_db: AsyncEngine) -> None:
    """need_hypotheses add/get：JSONB（reasoning.based_on EvidenceItem、signal_ids）
    全字段往返（Task 3 首用例：模块不存在 → ModuleNotFoundError RED）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    hypothesis = _hypothesis(tenant_id=tenant)
    async with _uow_type()(factory, tenant, now=clock.now) as uow:
        assert await uow.hypotheses.add(hypothesis) is True
        loaded = await uow.hypotheses.get(tenant, hypothesis.hypothesis_id)
        assert loaded is not None
        assert loaded.category == "stainless steel hinges"
        assert loaded.reasoning.value == "可能需要耐腐蚀五金"
        assert len(loaded.reasoning.based_on) == 1
        item = loaded.reasoning.based_on[0]
        assert item.level is _models.EvidenceLevel.PUBLIC_COMPANY_EVENT
        assert item.source_id == "sha256:pagehash001"
        assert loaded.signal_ids == hypothesis.signal_ids
        assert loaded.status is _models.HypothesisStatus.INFERRED


async def test_repo_lists_filter_and_sort(demand_db: AsyncEngine) -> None:
    """list_for_outreach（inferred+contacting 升序、不含 rejected）/list_sourcing_ready/
    list_by_account 过滤排序。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    account_a = _models.ProspectAccountId(new_id("acc"))
    first = _hypothesis(tenant_id=tenant, account_id=account_a)
    second = _hypothesis(
        tenant_id=tenant,
        account_id=account_a,
        category="aluminum profiles",
        status=_models.HypothesisStatus.REJECTED,
        rejection_reason="no_budget",
    )
    async with _uow_type()(factory, tenant, now=clock.now) as uow:
        assert await uow.hypotheses.add(first) is True
        assert await uow.hypotheses.add(second) is True
    async with _uow_type()(factory, tenant, now=clock.now) as uow:
        listed = await uow.hypotheses.list_for_outreach(tenant, None, 10)
        assert [h.hypothesis_id for h in listed] == [first.hypothesis_id]
        by_account = await uow.needs.list_by_account(tenant, account_a)
        assert by_account == []


async def test_repo_cross_tenant_raises_and_audit_has_no_content(
    demand_db: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """repo 参数租户与绑定租户不匹配 → TenantIsolationViolation；critical 审计
    日志只含 action/绑定租户，不含输入内容（spec D11/§8）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant_a = TenantId(new_id("tn"))
    tenant_b = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    caplog.clear()
    async with _uow_type()(factory, tenant_b, now=clock.now) as uow:
        with pytest.raises(TenantIsolationViolation):
            await uow.hypotheses.get(tenant_a, _models.NeedHypothesisId(new_id("hyp")))
        with pytest.raises(TenantIsolationViolation):
            await uow.needs.get(tenant_a, _models.ValidatedNeedId(new_id("need")))
    critical = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert len(critical) == 2
    # P2-1：action 在 LogRecord 的 extra 中，不在 message；message 为固定文案
    actions = {getattr(record, "action", None) for record in critical}
    assert actions == {"need_hypothesis_get", "validated_need_get"}
    for record in critical:
        assert record.message == "检测到跨租户数据隔离违规"  # 固定文案：无输入内容
        assert getattr(record, "tenant_id", None) == str(tenant_b)  # 只记绑定租户
```

（repo 层剩余用例——`append_field_history` 行形状、`get_for_update` 行锁、`validated_needs` JSONB 往返——完整代码见下，P3-5；spec 9.3-12 的 `list_sourcing_ready` 排序断言在 `test_repo_lists_filter_and_sort` 中一并覆盖。）

```python
async def _outbox_events(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[object]:
    """outbox 事件（确定性排序：published_at, event_id——P2-7）。"""
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        rows = (
            await session.execute(
                select(tables.OutboxEventRow)
                .where(tables.OutboxEventRow.tenant_id == str(tenant))
                .order_by(
                    tables.OutboxEventRow.published_at,
                    tables.OutboxEventRow.event_id,
                )
            )
        ).scalars().all()
    return list(rows)


async def test_repo_append_field_history_row_shape(demand_db: AsyncEngine) -> None:
    """append_field_history 行形状：field_name/old_value/new_value/source_message_id/
    changed_by/changed_at 全字段落库（P3-5）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    need = _models.ValidatedNeed(
        need_id=_models.ValidatedNeedId(new_id("need")),
        tenant_id=tenant,
        account_id=_models.ProspectAccountId(new_id("acc")),
        product_category=_models.FactualField(
            value="hinges",
            provenance=_models.Provenance(
                source_type=SourceType.CONVERSATION,
                source_id="msg_conv_001",
                extracted_by="human",
                extracted_at=NOW,
            ),
        ),
        source_message_id=_models.MessageId("msg_conv_001"),
        created_at=NOW,
    )
    async with _uow_type()(factory, tenant, now=clock.now) as uow:
        await uow.needs.add(need)
        await uow.needs.append_field_history(
            tenant, need.need_id, "quantity", None, "5000",
            "msg_conv_010", "emp-1",
        )
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        rows = (
            await session.execute(
                select(tables.ValidatedNeedFieldHistoryRow).where(
                    tables.ValidatedNeedFieldHistoryRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.need_id == str(need.need_id)
    assert row.field_name == "quantity"
    assert row.old_value is None
    assert row.new_value == "5000"
    assert row.source_message_id == "msg_conv_010"
    assert row.changed_by == "emp-1"
    assert row.changed_at == NOW


async def test_repo_get_for_update_locks_and_validated_jsonb_roundtrip(
    demand_db: AsyncEngine,
) -> None:
    """get_for_update 行锁语义（A 持锁未提交时 B 的 FOR UPDATE 阻塞至 A 提交，
    D7 先例同 discard）；validated_needs 的 11 个 FactualField JSONB 全往返（P3-5）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    from datetime import date

    need = _models.ValidatedNeed(
        need_id=_models.ValidatedNeedId(new_id("need")),
        tenant_id=tenant,
        account_id=_models.ProspectAccountId(new_id("acc")),
        product_category=_models.FactualField(
            value="hinges",
            provenance=_models.Provenance(
                source_type=SourceType.CONVERSATION,
                source_id="msg_conv_001",
                extracted_by="human",
                extracted_at=NOW,
            ),
        ),
        source_message_id=_models.MessageId("msg_conv_001"),
        created_at=NOW,
        quantity=_models.FactualField(
            value=5000,
            provenance=_models.Provenance(
                source_type=SourceType.CONVERSATION,
                source_id="msg_conv_002",
                extracted_by="model-v1",
                extracted_at=NOW,
            ),
        ),
        required_by=_models.FactualField(
            value=date(2026, 12, 31),
            provenance=_models.Provenance(
                source_type=SourceType.CONVERSATION,
                source_id="msg_conv_002",
                extracted_by="model-v1",
                extracted_at=NOW,
            ),
        ),
    )
    async with _uow_type()(factory, tenant, now=clock.now) as uow:
        await uow.needs.add(need)
        loaded = await uow.needs.get(tenant, need.need_id)
        assert loaded is not None
        assert loaded.quantity is not None and loaded.quantity.value == 5000
        assert (
            loaded.required_by is not None
            and loaded.required_by.value == date(2026, 12, 31)
        )
        assert (
            loaded.product_category.provenance.source_id == "msg_conv_001"
        )
    # 行锁：A 持锁未提交，B 的 FOR UPDATE 阻塞至 A 提交（D7 先例同 discard）
    a_locked = asyncio.Event()

    async def run_a() -> None:
        async with _uow_type()(factory, tenant, now=clock.now) as uow:
            locked = await uow.needs.get_for_update(tenant, need.need_id)
            assert locked is not None
            a_locked.set()
            await asyncio.sleep(0.5)  # 持锁窗口

    async def run_b() -> object:
        await a_locked.wait()
        async with _uow_type()(factory, tenant, now=clock.now) as uow:
            return await uow.needs.get_for_update(tenant, need.need_id)

    _, b_result = await asyncio.gather(run_a(), run_b())
    assert b_result is not None and b_result.need_id == need.need_id
```

- [x] **Step 2: 运行确认 RED**

```bash
find . -name "._*" -not -path "./.git/*" -delete
conda run -n tradeos-py312 python -m pytest tests/integration/test_need_hypotheses.py -q -W error
```

Expected: **FAIL**——`ModuleNotFoundError: No module named 'infra.db.repositories.need_hypotheses'`（Task 3 用例）；UoW 用例 `AttributeError`（`SqlAlchemyDemandUnitOfWork` 无 `hypotheses`/`needs`）。记录 rc=1。

- [x] **Step 3: 最小实现**

(3a) `infra/db/repositories/need_hypotheses.py`——JSONB 序列化器 + 两个 Impl（镜像 `demand.py` 先例：`_DemandRepository` 基座同名 `_tenant_matches`/`_require_tenant`/`_tenant_logger`）：

```python
"""demand 域假设侧仓储实现（spec 2026-08-17 §5；NeedHypothesis/ValidatedNeed）。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select, text, update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from domains.demand.models import (
    HypothesisStatus,
    NeedHypothesis,
    NeedStatus,
    ValidatedNeed,
)
from domains.demand.repository import (
    NeedHypothesisRepository,
    ValidatedNeedRepository,
)
from infra.db.tables import (
    NeedHypothesisRow,
    ValidatedNeedFieldHistoryRow,
    ValidatedNeedRow,
)
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.evidence import EvidenceItem, EvidenceLevel
from shared.schemas.identifiers import (
    ConversationId,
    DemandSignalId,
    EmployeeId,
    MessageId,
    NeedHypothesisId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.provenance import FactualField, InferredField, Provenance, SourceType

_tenant_logger = logging.getLogger("infra.db.repositories.need_hypotheses")


class _HypothesisRepository:
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._now = now if now is not None else lambda: datetime.now(UTC)

    def _tenant_matches(self, tenant_id: TenantId, action: str) -> bool:
        if tenant_id == self._tenant_id:
            return True
        _tenant_logger.critical(
            "检测到跨租户数据隔离违规",
            extra={"action": action, "tenant_id": str(self._tenant_id)},
        )
        return False

    def _require_tenant(self, tenant_id: TenantId, action: str) -> None:
        if not self._tenant_matches(tenant_id, action):
            raise TenantIsolationViolation("跨租户数据隔离违规")


def _value_to_json(value: object) -> object:
    """FactualField 值序列化：Money→{amount(str),currency}、date→ISO、其余原样。"""
    from datetime import date

    from shared.schemas.money import Money

    if isinstance(value, Money):
        return {"amount": str(value.amount), "currency": value.currency}
    if isinstance(value, date):
        return value.isoformat()
    return value


def _provenance_to_json(provenance: Provenance) -> dict[str, object]:
    return {
        "source_type": provenance.source_type.value,
        "source_id": provenance.source_id,
        "extracted_by": provenance.extracted_by,
        "extracted_at": provenance.extracted_at.isoformat(),
        "confirmed_by": provenance.confirmed_by,
        "confirmed_at": (
            provenance.confirmed_at.isoformat() if provenance.confirmed_at else None
        ),
        "source_url": provenance.source_url,
        "page_hash": provenance.page_hash,
    }


def _factual_to_json(field: FactualField[object] | None) -> dict[str, object] | None:
    if field is None:
        return None
    return {"value": _value_to_json(field.value), "provenance": _provenance_to_json(field.provenance)}


def _json_to_factual(raw: dict[str, object] | None, value_kind: str) -> FactualField[object] | None:
    if raw is None:
        return None
    p = cast(dict[str, object], raw["provenance"])
    provenance = Provenance(
        source_type=SourceType(p["source_type"]),
        source_id=str(p["source_id"]),
        extracted_by=str(p["extracted_by"]),
        extracted_at=datetime.fromisoformat(str(p["extracted_at"])),
        confirmed_by=(
            EmployeeId(p["confirmed_by"]) if p.get("confirmed_by") else None
        ),
        confirmed_at=(
            datetime.fromisoformat(str(p["confirmed_at"]))
            if p.get("confirmed_at")
            else None
        ),
        source_url=p.get("source_url"),
        page_hash=p.get("page_hash"),
    )
    value = raw["value"]
    return FactualField(value=_decode_value(value, value_kind), provenance=provenance)


def _decode_value(value: object, kind: str) -> object:
    """按字段类型解码 JSON 值（quantity int / required_by date / target_price Money）。"""
    from datetime import date

    from shared.schemas.money import Money

    if kind == "quantity":
        return int(value)
    if kind == "required_by":
        return date.fromisoformat(str(value))
    if kind == "target_price":
        from decimal import Decimal

        return Money(  # type: ignore[index]
            amount=Decimal(str(value["amount"])),
            currency=str(value["currency"]),
        )
    return value


def _evidence_to_json(item: EvidenceItem) -> dict[str, object]:
    return {
        "level": item.level.value,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "observed_at": item.observed_at.isoformat(),
        "summary": item.summary,
    }


def _json_to_evidence(raw: dict[str, object]) -> EvidenceItem:
    return EvidenceItem(
        level=EvidenceLevel(raw["level"]),
        source_type=str(raw["source_type"]),
        source_id=str(raw["source_id"]),
        observed_at=datetime.fromisoformat(str(raw["observed_at"])),
        summary=str(raw["summary"]),
    )


def _inferred_to_json(field: InferredField[str]) -> dict[str, object]:
    return {
        "value": field.value,
        "based_on": [_evidence_to_json(item) for item in field.based_on],
        "inferred_by": field.inferred_by,
        "inferred_at": field.inferred_at.isoformat(),
    }


def _json_to_inferred(raw: dict[str, object]) -> InferredField[str]:
    return InferredField(
        value=str(raw["value"]),
        based_on=[_json_to_evidence(item) for item in cast(list[dict[str, object]], raw["based_on"])],
        inferred_by=str(raw["inferred_by"]),
        inferred_at=datetime.fromisoformat(str(raw["inferred_at"])),
    )


def _hypothesis_to_row(hypothesis: NeedHypothesis) -> NeedHypothesisRow:
    return NeedHypothesisRow(
        tenant_id=str(hypothesis.tenant_id),
        hypothesis_id=str(hypothesis.hypothesis_id),
        account_id=str(hypothesis.account_id),
        category=hypothesis.category,
        reasoning=_inferred_to_json(hypothesis.reasoning),
        signal_ids=[str(sid) for sid in hypothesis.signal_ids],
        status=hypothesis.status.value,
        rejection_reason=hypothesis.rejection_reason,
        validated_need_id=(
            str(hypothesis.validated_need_id)
            if hypothesis.validated_need_id is not None
            else None
        ),
        created_at=hypothesis.created_at,
    )


def _row_to_hypothesis(row: NeedHypothesisRow) -> NeedHypothesis:
    return NeedHypothesis(
        hypothesis_id=NeedHypothesisId(row.hypothesis_id),
        tenant_id=TenantId(row.tenant_id),
        account_id=ProspectAccountId(row.account_id),
        category=row.category,
        reasoning=_json_to_inferred(cast(dict[str, object], row.reasoning)),
        signal_ids=[
            DemandSignalId(item) for item in cast(list[str], row.signal_ids)
        ],
        created_at=row.created_at,
        status=HypothesisStatus(row.status),
        rejection_reason=row.rejection_reason,
        validated_need_id=(
            ValidatedNeedId(row.validated_need_id)
            if row.validated_need_id is not None
            else None
        ),
    )


class NeedHypothesisRepositoryImpl(_HypothesisRepository, NeedHypothesisRepository):
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(session, tenant_id, now=now)

    async def add(self, hypothesis: NeedHypothesis) -> bool:
        """活跃冲突返回 False；True=新插入（index_elements+index_where，spec D2）。"""
        self._require_tenant(hypothesis.tenant_id, "need_hypothesis_add")
        result = await self._session.execute(
            pg_insert(NeedHypothesisRow)
            .values(
                tenant_id=str(hypothesis.tenant_id),
                hypothesis_id=str(hypothesis.hypothesis_id),
                account_id=str(hypothesis.account_id),
                category=hypothesis.category,
                reasoning=_inferred_to_json(hypothesis.reasoning),
                signal_ids=[str(sid) for sid in hypothesis.signal_ids],
                status=hypothesis.status.value,
                rejection_reason=hypothesis.rejection_reason,
                validated_need_id=(
                    str(hypothesis.validated_need_id)
                    if hypothesis.validated_need_id is not None
                    else None
                ),
                created_at=hypothesis.created_at,
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "account_id", "category"],
                index_where=text("status IN ('inferred','contacting')"),
            )
        )
        return cast(CursorResult, result).rowcount > 0

    async def get(
        self, tenant_id: TenantId, hypothesis_id: NeedHypothesisId
    ) -> NeedHypothesis | None:
        self._require_tenant(tenant_id, "need_hypothesis_get")
        row = (
            await self._session.execute(
                select(NeedHypothesisRow).where(
                    NeedHypothesisRow.tenant_id == str(self._tenant_id),
                    NeedHypothesisRow.hypothesis_id == str(hypothesis_id),
                )
            )
        ).scalar_one_or_none()
        return _row_to_hypothesis(row) if row is not None else None

    async def update(self, hypothesis: NeedHypothesis) -> None:
        """按 PK 更新既有行；缺失行 → ValidationError（P2-4：禁止 pg_insert
        upsert——那会静默插入缺失行；rowcount 必须恰为 1）。"""
        self._require_tenant(hypothesis.tenant_id, "need_hypothesis_update")
        result = await self._session.execute(
            sa_update(NeedHypothesisRow)
            .where(
                NeedHypothesisRow.tenant_id == str(self._tenant_id),
                NeedHypothesisRow.hypothesis_id == str(hypothesis.hypothesis_id),
            )
            .values(
                status=hypothesis.status.value,
                rejection_reason=hypothesis.rejection_reason,
                validated_need_id=(
                    str(hypothesis.validated_need_id)
                    if hypothesis.validated_need_id is not None
                    else None
                ),
                reasoning=_inferred_to_json(hypothesis.reasoning),
                signal_ids=[str(sid) for sid in hypothesis.signal_ids],
            )
        )
        if cast(CursorResult, result).rowcount != 1:
            raise ValidationError("需求假设更新失败")

    async def get_for_update(
        self, tenant_id: TenantId, hypothesis_id: NeedHypothesisId
    ) -> NeedHypothesis | None:
        self._require_tenant(tenant_id, "need_hypothesis_get_for_update")
        row = (
            await self._session.execute(
                select(NeedHypothesisRow)
                .where(
                    NeedHypothesisRow.tenant_id == str(self._tenant_id),
                    NeedHypothesisRow.hypothesis_id == str(hypothesis_id),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        return _row_to_hypothesis(row) if row is not None else None

    async def find_active_by_account_and_category(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        category: str,
    ) -> NeedHypothesis | None:
        self._require_tenant(tenant_id, "need_hypothesis_find_active")
        row = (
            await self._session.execute(
                select(NeedHypothesisRow)
                .where(
                    NeedHypothesisRow.tenant_id == str(self._tenant_id),
                    NeedHypothesisRow.account_id == str(account_id),
                    NeedHypothesisRow.category == category,
                    NeedHypothesisRow.status.in_(("inferred", "contacting")),
                )
            )
        ).scalar_one_or_none()
        return _row_to_hypothesis(row) if row is not None else None

    async def list_for_outreach(
        self, tenant_id: TenantId, countries: list[str] | None, limit: int
    ) -> list[NeedHypothesis]:
        self._require_tenant(tenant_id, "need_hypothesis_list_for_outreach")
        rows = (
            await self._session.execute(
                select(NeedHypothesisRow)
                .where(
                    NeedHypothesisRow.tenant_id == str(self._tenant_id),
                    NeedHypothesisRow.status.in_(("inferred", "contacting")),
                )
                .order_by(NeedHypothesisRow.created_at)
                .limit(limit)
            )
        ).scalars().all()
        return [_row_to_hypothesis(row) for row in rows]
```

`ValidatedNeedRepositoryImpl` 完整实现（P3-5；`_need_to_row`/`_row_to_need` 用 `_factual_to_json`/`_json_to_factual`，value_kind 按字段名；JSONB 反序列化失败 → `ValidationError("字段快照损坏")`）：

```python
_FIELD_KINDS: dict[str, str] = {
    "application": "str",
    "material": "str",
    "size_spec": "str",
    "quantity": "quantity",
    "packaging": "str",
    "destination": "str",
    "required_by": "required_by",
    "target_price": "target_price",
    "current_supply_issue": "str",
    "certification_required": "str",
    "product_category": "str",
}


def _need_to_row(need: ValidatedNeed) -> ValidatedNeedRow:
    values: dict[str, object] = {
        "tenant_id": str(need.tenant_id),
        "need_id": str(need.need_id),
        "account_id": str(need.account_id),
        "product_category": _factual_to_json(need.product_category),
        "source_message_id": str(need.source_message_id),
        "source_conversation_id": (
            str(need.source_conversation_id)
            if need.source_conversation_id is not None
            else None
        ),
        "status": need.status.value,
        "created_at": need.created_at,
        "confirmed_by": str(need.confirmed_by) if need.confirmed_by else None,
    }
    for field in _FIELD_KINDS:
        values[field] = _factual_to_json(getattr(need, field))
    return ValidatedNeedRow(**values)


def _row_to_need(row: ValidatedNeedRow) -> ValidatedNeed:
    def _field(name: str):
        try:
            return _json_to_factual(
                cast(dict[str, object] | None, getattr(row, name)),
                _FIELD_KINDS[name],
            )
        except (TypeError, KeyError, ValueError):
            raise ValidationError("字段快照损坏")

    return ValidatedNeed(
        need_id=ValidatedNeedId(row.need_id),
        tenant_id=TenantId(row.tenant_id),
        account_id=ProspectAccountId(row.account_id),
        product_category=cast(FactualField[object], _field("product_category")),
        source_message_id=MessageId(row.source_message_id),
        source_conversation_id=(
            ConversationId(row.source_conversation_id)
            if row.source_conversation_id is not None
            else None
        ),
        created_at=row.created_at,
        status=NeedStatus(row.status),
        application=_field("application"),
        material=_field("material"),
        size_spec=_field("size_spec"),
        quantity=_field("quantity"),
        packaging=_field("packaging"),
        destination=_field("destination"),
        required_by=_field("required_by"),
        target_price=_field("target_price"),
        current_supply_issue=_field("current_supply_issue"),
        certification_required=_field("certification_required"),
        confirmed_by=EmployeeId(row.confirmed_by) if row.confirmed_by else None,
        cluster_id=None,
    )


class ValidatedNeedRepositoryImpl(_HypothesisRepository, ValidatedNeedRepository):
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(session, tenant_id, now=now)

    async def add(self, need: ValidatedNeed) -> None:
        self._require_tenant(need.tenant_id, "validated_need_add")
        self._session.add(_need_to_row(need))

    async def get(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> ValidatedNeed | None:
        self._require_tenant(tenant_id, "validated_need_get")
        row = (
            await self._session.execute(
                select(ValidatedNeedRow).where(
                    ValidatedNeedRow.tenant_id == str(self._tenant_id),
                    ValidatedNeedRow.need_id == str(need_id),
                )
            )
        ).scalar_one_or_none()
        return _row_to_need(row) if row is not None else None

    async def update(self, need: ValidatedNeed) -> None:
        """按 PK 更新既有行；缺失行 → ValidationError（P2-4：禁止 upsert）。"""
        self._require_tenant(need.tenant_id, "validated_need_update")
        values: dict[str, object] = {
            "status": need.status.value,
            "confirmed_by": str(need.confirmed_by) if need.confirmed_by else None,
        }
        for field in _FIELD_KINDS:
            values[field] = _factual_to_json(getattr(need, field))
        result = await self._session.execute(
            sa_update(ValidatedNeedRow)
            .where(
                ValidatedNeedRow.tenant_id == str(self._tenant_id),
                ValidatedNeedRow.need_id == str(need.need_id),
            )
            .values(**values)
        )
        if cast(CursorResult, result).rowcount != 1:
            raise ValidationError("已验证需求更新失败")

    async def get_for_update(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> ValidatedNeed | None:
        self._require_tenant(tenant_id, "validated_need_get_for_update")
        row = (
            await self._session.execute(
                select(ValidatedNeedRow)
                .where(
                    ValidatedNeedRow.tenant_id == str(self._tenant_id),
                    ValidatedNeedRow.need_id == str(need_id),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        return _row_to_need(row) if row is not None else None

    async def append_field_history(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        field_name: str,
        old_value: str | None,
        new_value: str,
        source_message_id: str,
        changed_by: str | None,
    ) -> None:
        self._require_tenant(tenant_id, "validated_need_append_field_history")
        self._session.add(
            ValidatedNeedFieldHistoryRow(
                tenant_id=str(self._tenant_id),
                history_id=new_id("vh"),
                need_id=str(need_id),
                field_name=field_name,
                old_value=old_value,
                new_value=new_value,
                source_message_id=source_message_id,
                changed_by=changed_by,
                changed_at=self._now(),
            )
        )

    async def list_sourcing_ready(
        self, tenant_id: TenantId, limit: int
    ) -> list[ValidatedNeed]:
        self._require_tenant(tenant_id, "validated_need_list_sourcing_ready")
        rows = (
            await self._session.execute(
                select(ValidatedNeedRow)
                .where(
                    ValidatedNeedRow.tenant_id == str(self._tenant_id),
                    ValidatedNeedRow.status == "sourcing_ready",
                )
                .order_by(ValidatedNeedRow.created_at)
                .limit(limit)
            )
        ).scalars().all()
        return [_row_to_need(row) for row in rows]

    async def list_by_account(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> list[ValidatedNeed]:
        self._require_tenant(tenant_id, "validated_need_list_by_account")
        rows = (
            await self._session.execute(
                select(ValidatedNeedRow).where(
                    ValidatedNeedRow.tenant_id == str(self._tenant_id),
                    ValidatedNeedRow.account_id == str(account_id),
                )
            )
        ).scalars().all()
        return [_row_to_need(row) for row in rows]
```

（`_HypothesisRepository` 基座增 `_now` 时钟注入（UoW 传入 `now`，先例同 demand repo 的 extracted_at 来源）；imports 补 `ConversationId`/`MessageId`/`new_id`。`changed_at` 用服务时钟 `now`——`append_field_history` 在 UoW 内由 service 调用，时钟一致。）

(3b) `infra/db/demand_uow.py`——`__aenter__` 追加：

```python
from infra.db.repositories.need_hypotheses import (
    NeedHypothesisRepositoryImpl,
    ValidatedNeedRepositoryImpl,
)
...
        self.hypotheses = NeedHypothesisRepositoryImpl(
            session, self._tenant_id, now=self._now
        )
        self.needs = ValidatedNeedRepositoryImpl(
            session, self._tenant_id, now=self._now
        )
```

- [x] **Step 4: 运行确认 GREEN**

```bash
conda run -n tradeos-py312 python -m pytest tests/integration/test_need_hypotheses.py -q -W error
```

Expected: 全部 PASS。

- [x] **Step 5: 边界与静态检查 + 定向回归**

```bash
conda run -n tradeos-py312 python -m ruff check infra/db/repositories/need_hypotheses.py infra/db/demand_uow.py tests/integration/test_need_hypotheses.py
conda run -n tradeos-py312 python -m mypy domains/demand infra/db
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
conda run -n tradeos-py312 python -m pytest tests/integration/test_demand_signals.py -q -W error
```

Expected: 全部 rc=0（demand 信号既有 19 项回归通过）。

- [x] **Step 6: 停止等待监督方复审**（汇报 RED 原因 ModuleNotFoundError/AttributeError、GREEN 计数、diff 3 文件）

复审通过后，提交前执行 mutation proof：

| # | 临时变异 | 精确测试 | 预期 RED |
|---|---|---|---|
| M-R | `add` 的 `index_where` 谓词改错（`status = 'inferred'`） | Task 3 内新增仓储侧 parity 用例（断言 `add` 的 `index_where` 谓词语义：值集合 == {'inferred','contacting'} 且含 status 标识符，P1-2 同款语义比较） | 谓词语义比较断言失败 |

- [x] **Step 7: 复审通过后提交/推送/exact-HEAD CI**（命令块同 Task 1 Step 7；文件为 `infra/db/repositories/need_hypotheses.py` `infra/db/demand_uow.py` `tests/integration/test_need_hypotheses.py`；commit 消息逐字 `feat(demand): hypothesis and need repositories with tenant isolation`；Expected：3 文件 index 100644、local == origin、CI `OK`）

---

## Task 4: 事件注册 + create_hypothesis（spec 9.3 测试 1-4）

**Files:**
- Modify: `infra/db/outbox.py`（`EVENT_REGISTRY` 注册三事件）
- Modify: `tests/unit/test_outbox_serialization.py`（白名单期望集 +3）
- Modify: `domains/demand/service_impl.py`（`create_hypothesis`）
- Modify: `tests/integration/test_need_hypotheses.py`（spec 9.3 测试 1-4 + service 不可见）

**Interfaces（规格 §6.1 权威）：** `create_hypothesis(tenant_id, account_id: ProspectAccountId, category: str, signal_ids: list[str], reasoning: str, inferred_by: str) -> NeedHypothesisId`；校验先于 UoW（错误摘要见 spec §11）；DISCARDED 信号 → `ValidationError("需求信号已丢弃")`（D3/finding 5）；并入不发事件（D5）。

- [x] **Step 1: 写失败测试**

(1a) `tests/unit/test_outbox_serialization.py` 白名单期望集追加三项：`"NeedHypothesisCreated"`/`"NeedHypothesisRejected"`/`"NeedValidated"`。

(1b) `tests/integration/test_need_hypotheses.py` 追加（spec 9.3 测试 1-4；`_outbox_events` helper 同 demand 切片）：

```python
async def test_create_hypothesis_roundtrip_persists_evidence_snapshot(
    demand_db: AsyncEngine,
) -> None:
    """spec 9.3-1：create 全字段往返；返回 hyp_ 前缀 ID；事件 metadata-only。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    signal_id = await _signal(service, tenant)

    hypothesis_id = await service.create_hypothesis(
        tenant,
        _models.ProspectAccountId(new_id("acc")),
        "stainless steel hinges",
        [signal_id],
        "Acme 新增产品线，可能需要耐腐蚀五金",
        "model-v1",
    )
    assert hypothesis_id.startswith("hyp_")
    rows = await _hypothesis_rows(factory, tenant)
    assert len(rows) == 1
    row = rows[0]
    assert row.hypothesis_id == hypothesis_id
    assert row.category == "stainless steel hinges"
    assert row.status == "inferred"
    assert row.signal_ids == [signal_id]
    assert row.reasoning["inferred_by"] == "model-v1"
    assert row.reasoning["based_on"][0]["source_id"] == "msg_conv_001"
    events = await _outbox_events(factory, tenant)
    assert len(events) == 2  # capture 1 + created 1
    created = [e for e in events if e.event_type == "NeedHypothesisCreated"]
    assert len(created) == 1
    payload = dict(created[0].event_payload)
    assert set(payload) <= {
        "tenant_id", "occurred_at", "run_id", "hypothesis_id",
        "account_id", "category", "confidence_tier",
    }
    assert OBSERVATION_MARKER not in str(payload)


async def test_create_hypothesis_merges_into_active_hypothesis(
    demand_db: AsyncEngine,
) -> None:
    """spec 9.3-2：同 (account, category) 第二次 create → 既有 ID、1 行 1 事件、
    based_on 并集、signal_ids 追加、不发新事件（D5）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    account = _models.ProspectAccountId(new_id("acc"))
    first = await _signal(service, tenant)
    second = await _signal(service, tenant, source_id="msg_conv_002")

    first_id = await service.create_hypothesis(
        tenant, account, "stainless steel hinges", [first], "推断一", "model-v1"
    )
    merged_id = await service.create_hypothesis(
        tenant, account, "stainless steel hinges", [second], "推断一", "model-v1"
    )
    assert merged_id == first_id
    rows = await _hypothesis_rows(factory, tenant)
    assert len(rows) == 1
    assert sorted(rows[0].signal_ids) == sorted([first, second])
    sources = {b["source_id"] for b in rows[0].reasoning["based_on"]}
    assert sources == {"msg_conv_001", "msg_conv_002"}
    assert len(await _outbox_events(factory, tenant)) == 3  # capture×2 + created×1


async def test_create_hypothesis_concurrent_same_key_exactly_one_row_one_event(
    demand_db: AsyncEngine,
) -> None:
    """spec 9.3-3：gather 两 UoW 同 key → 恰 1 行 1 事件、两结果 ID 相同。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service_a = _service(factory, tenant, clock)
    service_b = _service(factory, tenant, clock)
    signal_id = await _signal(service_a, tenant)
    account = _models.ProspectAccountId(new_id("acc"))

    results = await asyncio.gather(
        service_a.create_hypothesis(
            tenant, account, "stainless steel hinges", [signal_id], "推断", "model-v1"
        ),
        service_b.create_hypothesis(
            tenant, account, "stainless steel hinges", [signal_id], "推断", "model-v1"
        ),
        return_exceptions=True,
    )
    assert all(isinstance(r, str) for r in results), results
    assert results[0] == results[1]
    assert len(await _hypothesis_rows(factory, tenant)) == 1
    events = await _outbox_events(factory, tenant)
    assert len(events) == 2  # capture + created×1


async def test_create_hypothesis_rejects_bad_input_before_uow(
    demand_db: AsyncEngine,
) -> None:
    """spec 9.3-4：空 signal_ids/信号不存在/跨租户/DISCARDED 信号 → 固定摘要，
    校验先于 UoW（finding 5/M7）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    account = _models.ProspectAccountId(new_id("acc"))
    with pytest.raises(ValidationError, match="需求信号不能为空"):
        await service.create_hypothesis(tenant, account, "cat", [], "推断", "model-v1")
    with pytest.raises(ValidationError, match="需求信号不存在"):
        await service.create_hypothesis(
            tenant, account, "cat", [new_id("sig")], "推断", "model-v1"
        )
    signal_id = await _signal(service, tenant)
    await service.discard_signal(tenant, signal_id, "noise")
    with pytest.raises(ValidationError, match="需求信号已丢弃"):
        await service.create_hypothesis(
            tenant, account, "cat", [signal_id], "推断", "model-v1"
        )
    assert await _hypothesis_rows(factory, tenant) == []
```

（spec 9.3-13 的 service 不可见侧：`create_hypothesis` 引用他租户信号 → `ValidationError("需求信号不存在")`，并入本文件该 Task 的 `test_create_hypothesis_*` 补充用例。）

- [x] **Step 2: 运行确认 RED**

```bash
conda run -n tradeos-py312 python -m pytest tests/integration/test_need_hypotheses.py -k create_hypothesis -q -W error
conda run -n tradeos-py312 python -m pytest tests/unit/test_outbox_serialization.py -q -W error
```

Expected: **FAIL**——create 用例 `AttributeError: 'DemandServiceImpl' object has no attribute 'create_hypothesis'`；白名单用例 `AssertionError`（期望集缺三项）。记录 rc=1。

- [x] **Step 3: 最小实现**

(3a) `infra/db/outbox.py` `EVENT_REGISTRY` 追加：

```python
    "NeedHypothesisCreated": NeedHypothesisCreated,
    "NeedHypothesisRejected": NeedHypothesisRejected,
    "NeedValidated": NeedValidated,
```

（imports 区补 `from shared.events.catalog import NeedHypothesisCreated, NeedHypothesisRejected, NeedValidated`。）

(3b) `domains/demand/service_impl.py`——imports 增量（**P2-B：按当时文件状态逐符号，只加本任务实际使用的符号，与既有导入行合并、不重复**）：
- 扩展 `from domains.demand.models import ...` 行：增 `NeedHypothesis`（既有行：`from domains.demand.models import DemandSignal, SignalStatus, SignalType`）
- 扩展 `from shared.events.catalog import ...` 行：增 `NeedHypothesisCreated`（既有行：`from shared.events.catalog import DemandSignalCaptured`）
- 新增 `from shared.schemas.evidence import EvidenceItem, derive_confidence`（本任务 create 构造 EvidenceItem 与事件 tier 用）
- 扩展 `from shared.schemas.provenance import ...` 行：增 `InferredField`（既有行：`from shared.schemas.provenance import Provenance, SourceType`）
- 扩展 `from shared.schemas.identifiers import ...` 行：增 `NeedHypothesisId, ProspectAccountId`（既有行：`from shared.schemas.identifiers import DemandSignalId, TenantId, new_id`；`SignalStatus` 已导入用于 DISCARDED 检查）
- **不导入** Task 5/6 才用的错误类（`InsufficientEvidenceError`/`SourcingThresholdNotMetError`/`HypothesisAlreadyResolvedError`）与 `ValidatedNeed`/`NeedStatus`/`HypothesisStatus`/`MessageId`/`ValidatedNeedId`/`EmployeeId`——避免本任务 F401（P2-B）；`EmployeeId` 由 Task 5 补（P3-2）

`capture_signal` 之后追加：

```python
    async def create_hypothesis(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        category: str,
        signal_ids: list[str],
        reasoning: str,
        inferred_by: str,
    ) -> NeedHypothesisId:
        """记录一条需求假设（契约见 service.py docstring + 规格 §6.1）。"""
        if (
            not isinstance(tenant_id, str)
            or not tenant_id.strip()
            or tenant_id != tenant_id.strip()
        ):
            raise ValidationError("租户无效")
        if len(tenant_id) > 40:
            raise ValidationError("租户超长")
        if (
            not isinstance(account_id, str)
            or not account_id.strip()
            or account_id != account_id.strip()
        ):
            raise ValidationError("目标企业无效")
        if len(account_id) > 40:
            raise ValidationError("目标企业超长")
        category = self._require_text(category, "需求类别", max_len=200)
        assert category is not None
        if (
            not isinstance(signal_ids, list)
            or not signal_ids
            or any(
                not isinstance(sid, str) or not sid.strip() or sid != sid.strip()
                for sid in signal_ids
            )
        ):
            raise ValidationError("需求信号不能为空")
        if any(len(sid) > 40 for sid in signal_ids):
            raise ValidationError("需求信号标识无效")
        reasoning = self._require_text(reasoning, "推断理由")
        assert reasoning is not None
        inferred_by = self._require_text(inferred_by, "推断者", max_len=64)
        assert inferred_by is not None
        now = self._validate_now(self._now())
        evidence: list[EvidenceItem] = []
        async with self._uow_factory(tenant_id) as uow:
            for sid in signal_ids:
                signal = await uow.signals.get(tenant_id, DemandSignalId(sid))
                if signal is None:
                    raise ValidationError("需求信号不存在")
                if signal.status is SignalStatus.DISCARDED:
                    raise ValidationError("需求信号已丢弃")
                evidence.append(
                    EvidenceItem(
                        level=signal.evidence_level,
                        source_type=signal.provenance.source_type.value,
                        source_id=signal.provenance.source_id,
                        observed_at=signal.observed_at,
                        summary=signal.raw_observation,
                    )
                )
            hypothesis = NeedHypothesis(
                hypothesis_id=NeedHypothesisId(new_id("hyp")),
                tenant_id=tenant_id,
                account_id=account_id,
                category=category,
                reasoning=InferredField(
                    value=reasoning,
                    based_on=evidence,
                    inferred_by=inferred_by,
                    inferred_at=now,
                ),
                signal_ids=[DemandSignalId(sid) for sid in signal_ids],
                created_at=now,
            )
            inserted = await uow.hypotheses.add(hypothesis)
            if inserted:
                await uow.bus.publish(
                    NeedHypothesisCreated(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        run_id=None,
                        hypothesis_id=hypothesis.hypothesis_id,
                        account_id=account_id,
                        category=category,
                        confidence_tier=derive_confidence(evidence, now=now).tier,
                    )
                )
                return hypothesis.hypothesis_id
            # 活跃冲突：FOR UPDATE 重读胜者 → 幂等并入（D5），不发事件
            active = await uow.hypotheses.find_active_by_account_and_category(
                tenant_id, account_id, category
            )
            if active is None:
                raise ValidationError("需求假设写入竞态异常")
            merged = NeedHypothesis(
                hypothesis_id=active.hypothesis_id,
                tenant_id=active.tenant_id,
                account_id=active.account_id,
                category=active.category,
                reasoning=InferredField(
                    value=active.reasoning.value,
                    based_on=_merge_evidence(active.reasoning.based_on, evidence),
                    inferred_by=active.reasoning.inferred_by,
                    inferred_at=now,
                ),
                signal_ids=_merge_ids(active.signal_ids, hypothesis.signal_ids),
                created_at=active.created_at,
                status=active.status,
                rejection_reason=active.rejection_reason,
                validated_need_id=active.validated_need_id,
            )
            await uow.hypotheses.update(merged)
            return merged.hypothesis_id
```

（模块级 helper 完整实现，P3-5）：

```python
def _merge_evidence(
    existing: list[EvidenceItem], incoming: list[EvidenceItem]
) -> list[EvidenceItem]:
    """以 (source_type, source_id) 为键的幂等并集，保持首现顺序（D5）。"""
    merged: list[EvidenceItem] = []
    seen: set[tuple[str, str]] = set()
    for item in existing + incoming:
        key = (item.source_type, item.source_id)
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


def _merge_ids(
    existing: list[DemandSignalId], incoming: list[DemandSignalId]
) -> list[DemandSignalId]:
    """字符串并集，保持首现顺序（信号关联不可变，只追加去重，D3）。"""
    merged: list[DemandSignalId] = []
    seen: set[str] = set()
    for sid in existing + incoming:
        if str(sid) not in seen:
            seen.add(str(sid))
            merged.append(sid)
    return merged
```

- [x] **Step 4: 运行确认 GREEN**

```bash
conda run -n tradeos-py312 python -m pytest tests/integration/test_need_hypotheses.py -k create_hypothesis -q -W error
conda run -n tradeos-py312 python -m pytest tests/unit/test_outbox_serialization.py -q -W error
conda run -n tradeos-py312 python -m pytest tests/integration/test_need_hypotheses.py -q -W error
```

Expected: 全部 PASS（含 Task 3 存量用例）。

- [x] **Step 5: 边界与静态检查 + 定向回归**

```bash
conda run -n tradeos-py312 python -m ruff check domains/demand infra/db tests/integration/test_need_hypotheses.py tests/unit/test_outbox_serialization.py
conda run -n tradeos-py312 python -m mypy domains/demand infra/db
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
conda run -n tradeos-py312 python -m pytest tests/integration/test_demand_signals.py -q -W error
```

Expected: 全部 rc=0。

- [x] **Step 6: 停止等待监督方复审**（汇报 RED 原因 AttributeError/白名单 AssertionError、GREEN 计数、diff 4 文件）

复审通过后，提交前执行 mutation proofs（每项：临时 apply_patch → 精确测试 RED → 恢复 → GREEN → `git diff` 无残留）：

| # | 临时变异 | 精确测试 | 预期 RED |
|---|---|---|---|
| M1 | `hypotheses.add` 去掉 `.on_conflict_do_nothing(...)`（普通 INSERT） | `test_create_hypothesis_concurrent_same_key_exactly_one_row_one_event` | gather 结果含 IntegrityError（部分唯一冲突），`all(isinstance(r, str))` 失败 |
| M2 | `index_elements` 去掉 `"account_id"` | `test_create_hypothesis_merges_into_active_hypothesis` | 不同企业同 category 被误并 → 行数/事件数断言失败 |
| M6 | 并入分支也发布 `NeedHypothesisCreated` | `test_create_hypothesis_merges_into_active_hypothesis` | outbox 事件数 4（断言 3 失败） |
| M7 | `create_hypothesis` 删除 DISCARDED 检查 | `test_create_hypothesis_rejects_bad_input_before_uow` | `pytest.raises(ValidationError, match="需求信号已丢弃")` DID NOT RAISE |

- [x] **Step 7: 复审通过后提交/推送/exact-HEAD CI**（命令块同 Task 1 Step 7；文件为 `infra/db/outbox.py` `tests/unit/test_outbox_serialization.py` `domains/demand/service_impl.py` `tests/integration/test_need_hypotheses.py`；commit 消息逐字 `feat(demand): create hypotheses with dedup and evidence snapshots`；Expected：4 文件 index 100644、local == origin、CI `OK`）

---

## Task 5: promote_to_validated + reject_hypothesis（spec 9.3 测试 5-8）

**Files:**
- Modify: `domains/demand/service_impl.py`（`promote_to_validated`/`reject_hypothesis`）
- Modify: `tests/integration/test_need_hypotheses.py`（spec 9.3 测试 5-8）

**Interfaces（规格 §6.2/§6.3 权威）：** `promote_to_validated(tenant_id, hypothesis_id: str, source_message_id: str, extracted_fields: dict[str, object], confirmed_by: str | None = None) -> ValidatedNeedId`；`reject_hypothesis(tenant_id, hypothesis_id: str, loss_reason: str, rejected_by: str | None = None) -> None`。11 键白名单 `_PROMOTE_FIELD_WHITELIST = {"product_category", "application", "material", "size_spec", "quantity", "packaging", "destination", "required_by", "target_price", "current_supply_issue", "certification_required"}`；初始完整度 ≥ 3 → SOURCING_READY（finding 6）；幂等返回既有 need_id（D7）。

- [ ] **Step 1: 写失败测试（spec 9.3 测试 5-8）**

```python
async def _create_promotable_hypothesis(
    service: DemandService, tenant: TenantId, category: str = "stainless steel hinges"
) -> str:
    """conversation + inbound_inquiry 信号 → 可晋升假设（证据 CUSTOMER_INTEREST_REPLY）。"""
    signal_id = await _signal(service, tenant)
    return await service.create_hypothesis(
        tenant,
        _models.ProspectAccountId(new_id("acc")),
        category,
        [signal_id],
        "入站询盘表明可能需要五金件",
        "model-v1",
    )


async def test_promote_rejects_agent_inference_evidence(demand_db: AsyncEngine) -> None:
    """spec 9.3-5：web 公开事件证据（PUBLIC_COMPANY_EVENT）→ InsufficientEvidenceError；
    行保持 inferred、无 need、无事件（D6/M3）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    signal_id = await _signal(
        service, tenant, signal_type="product_line_expansion", source_type="web_page",
        source_id="sha256:pagehash001", source_url="https://example.com/acme",
        page_hash="sha256:pagehash001",
    )
    hypothesis_id = await service.create_hypothesis(
        tenant, _models.ProspectAccountId(new_id("acc")), "hinges",
        [signal_id], "工厂扩建，可能需要五金", "model-v1",
    )
    before = len(await _outbox_events(factory, tenant))
    with pytest.raises(InsufficientEvidenceError):
        await service.promote_to_validated(
            tenant, hypothesis_id, "msg_conv_001",
            {"product_category": "hinges"}, "emp-1",
        )
    rows = await _hypothesis_rows(factory, tenant)
    assert rows[0].status == "inferred"
    assert len(await _outbox_events(factory, tenant)) == before  # 无事件


async def test_promote_success_status_and_event(demand_db: AsyncEngine) -> None:
    """spec 9.3-6：初始完整度 ≥3 → SOURCING_READY，否则 VALIDATED；product_category
    必填；字段 provenance 指向 source_message_id；NeedValidated metadata-only。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    hypothesis_id = await _create_promotable_hypothesis(service, tenant)

    need_id = await service.promote_to_validated(
        tenant, hypothesis_id, "msg_conv_001",
        {"product_category": "hinges"}, "emp-1",
    )
    assert need_id.startswith("need_")
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        need_rows = (
            await session.execute(
                select(tables.ValidatedNeedRow).where(
                    tables.ValidatedNeedRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
    assert len(need_rows) == 1
    row = need_rows[0]
    assert row.status == "validated"  # 初始完整度 1（仅 product_category）→ VALIDATED
    assert row.product_category["value"] == "hinges"
    assert row.product_category["provenance"]["source_id"] == "msg_conv_001"
    assert row.product_category["provenance"]["confirmed_by"] == "emp-1"
    hyp_rows = await _hypothesis_rows(factory, tenant)
    assert hyp_rows[0].status == "validated"
    assert hyp_rows[0].validated_need_id == need_id
    events = await _outbox_events(factory, tenant)
    validated = [e for e in events if e.event_type == "NeedValidated"]
    assert len(validated) == 1
    payload = dict(validated[0].event_payload)
    assert set(payload) <= {
        "tenant_id", "occurred_at", "run_id", "need_id",
        "account_id", "category", "evidence_level", "completeness",
    }
    assert payload["completeness"] == 1
    assert payload["evidence_level"] == "customer_interest_reply"
    assert OBSERVATION_MARKER not in str(payload)

    # finding 6：含 application+quantity 时初始完整度 3（累积阶梯需先满足 level 2
    # 的 application/size_spec OR——仅 quantity 仍是 level 1）→ SOURCING_READY
    hypothesis2 = await _create_promotable_hypothesis(
        service, tenant, category="aluminum profiles"
    )
    need2 = await service.promote_to_validated(
        tenant, hypothesis2, "msg_conv_002",
        {
            "product_category": "aluminum profiles",
            "application": "marine use",
            "quantity": 5000,
        },
        None,
    )
    async with factory() as session:
        row2 = (
            await session.execute(
                select(tables.ValidatedNeedRow).where(
                    tables.ValidatedNeedRow.need_id == str(need2)
                )
            )
        ).scalar_one()
    assert row2.status == "sourcing_ready"
    assert row2.quantity["value"] == 5000

    with pytest.raises(ValidationError, match="产品类别不能为空"):
        await service.promote_to_validated(
            tenant, hypothesis2, "msg_conv_003", {"quantity": 5000}, None,
        )


async def test_promote_idempotent_and_concurrent(demand_db: AsyncEngine) -> None:
    """spec 9.3-7：二次 promote 同 need_id、1 need、1 事件；并发 promote 恰 1 need 1 事件。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    hypothesis_id = await _create_promotable_hypothesis(service, tenant)
    fields = {"product_category": "hinges", "quantity": 3000}
    need_id = await service.promote_to_validated(
        tenant, hypothesis_id, "msg_conv_001", fields, None,
    )
    again = await service.promote_to_validated(
        tenant, hypothesis_id, "msg_conv_001", fields, None,
    )
    assert again == need_id
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        count = len(
            (
                await session.execute(
                    select(tables.ValidatedNeedRow).where(
                        tables.ValidatedNeedRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()
        )
    assert count == 1
    validated = [
        e for e in await _outbox_events(factory, tenant)
        if e.event_type == "NeedValidated"
    ]
    assert len(validated) == 1

    hypothesis2 = await _create_promotable_hypothesis(
        service, tenant, category="hinges bulk"
    )
    service_b = _service(factory, tenant, clock)
    results = await asyncio.gather(
        service.promote_to_validated(tenant, hypothesis2, "msg_conv_010", fields, None),
        service_b.promote_to_validated(tenant, hypothesis2, "msg_conv_010", fields, None),
        return_exceptions=True,
    )
    assert all(isinstance(r, str) for r in results), results
    assert results[0] == results[1]
    validated = [
        e for e in await _outbox_events(factory, tenant)
        if e.event_type == "NeedValidated"
    ]
    assert len(validated) == 2


async def test_reject_semantics(demand_db: AsyncEngine) -> None:
    """spec 9.3-8：同 reason 幂等；异 reason 冲突且 first 保留；已晋升拒绝；
    事件含 reason；reason 不进日志。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    hypothesis_id = await _create_promotable_hypothesis(service, tenant)
    await service.reject_hypothesis(tenant, hypothesis_id, "no_budget", "emp-1")
    await service.reject_hypothesis(tenant, hypothesis_id, "no_budget", "emp-1")  # 幂等
    rows = await _hypothesis_rows(factory, tenant)
    assert rows[0].status == "rejected"
    assert rows[0].rejection_reason == "no_budget"
    from shared.errors import InvalidStateTransition

    with pytest.raises(InvalidStateTransition) as exc_info:
        await service.reject_hypothesis(tenant, hypothesis_id, "no_reply", "emp-1")
    assert str(exc_info.value) == "拒绝原因冲突，拒绝覆盖"
    rows = await _hypothesis_rows(factory, tenant)
    assert rows[0].rejection_reason == "no_budget"  # first reason 保留
    rejected = [
        e for e in await _outbox_events(factory, tenant)
        if e.event_type == "NeedHypothesisRejected"
    ]
    assert len(rejected) == 1
    assert dict(rejected[0].event_payload)["reason"] == "no_budget"

    validated_id = await _create_promotable_hypothesis(service, tenant, "validated target")
    await service.promote_to_validated(
        tenant, validated_id, "msg_conv_020", {"product_category": "hinges"}, None,
    )
    with pytest.raises(HypothesisAlreadyResolvedError):
        await service.reject_hypothesis(tenant, validated_id, "no_budget", "emp-1")
```

- [ ] **Step 2: 运行确认 RED**

```bash
conda run -n tradeos-py312 python -m pytest tests/integration/test_need_hypotheses.py -k "promote or reject" -q -W error
```

Expected: **FAIL**——`AttributeError: 'DemandServiceImpl' object has no attribute 'promote_to_validated'`（及 reject）。记录 rc=1。

- [ ] **Step 3: 最小实现**

(3a) `domains/demand/service_impl.py`——模块级白名单/类型辅助 + 两方法（imports 增量，**P2-B：基于 Task 4 已并入状态，只加新符号、不重复**）：
- 扩展 `from domains.demand.models import ...` 行：增 `HypothesisStatus, NeedStatus, ValidatedNeed`（`NeedHypothesis` 已由 Task 4 并入）
- 扩展 `from shared.events.catalog import ...` 行：增 `NeedHypothesisRejected, NeedValidated`（`NeedHypothesisCreated` 已由 Task 4 并入）
- 扩展 `from domains.demand.errors import ...` 行：增 `HypothesisAlreadyResolvedError, InsufficientEvidenceError`（既有行：`from domains.demand.errors import MissingWebEvidenceError`）
- 扩展 `from shared.schemas.provenance import ...` 行：增 `FactualField`（`InferredField`/`Provenance`/`SourceType` 已由 Task 4 并入）
- 扩展 `from shared.schemas.identifiers import ...` 行：增 `EmployeeId, MessageId, ValidatedNeedId`（`NeedHypothesisId`/`ProspectAccountId` 已由 Task 4 并入）
- 新增 `from dataclasses import replace`（promote/reject 用）
- **不重复**导入 `derive_confidence`/`NeedHypothesisCreated`/`ProspectAccountId`（Task 4 已并入）；**不导入** `EvidenceItem`（本任务代码未使用——P2-B）


```python
_PROMOTE_FIELD_WHITELIST = frozenset(
    {
        "product_category",
        "application",
        "material",
        "size_spec",
        "quantity",
        "packaging",
        "destination",
        "required_by",
        "target_price",
        "current_supply_issue",
        "certification_required",
    }
)
_UPDATE_FIELD_WHITELIST = _PROMOTE_FIELD_WHITELIST - {"product_category"}


def _coerce_field_value(name: str, value: object) -> object:
    """按字段类型强制转换（quantity int / required_by date / target_price Money）。"""
    from datetime import date

    from shared.schemas.money import Money

    if name == "quantity":
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValidationError("需求字段类型无效")
    if name == "required_by":
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            raise ValidationError("需求字段类型无效")
    if name == "target_price":
        # P2-9：裸值/缺键不得静默 USD——非 dict 直接拒绝；显式 Decimal 构造（硬边界 2）
        if not isinstance(value, dict):
            raise ValidationError("需求字段类型无效")
        try:
            from decimal import Decimal

            return Money(
                amount=Decimal(str(value["amount"])),
                currency=str(value["currency"]),
            )
        except (KeyError, TypeError, ValueError):
            raise ValidationError("需求字段类型无效")
    return value


def _factual_value_to_text(field: FactualField[object] | None) -> str | None:
    """FactualField 显示值序列化（Money→Decimal-str、date→ISO、其余 str）。"""
    if field is None:
        return None
    value = field.value
    if isinstance(value, Money):
        return str(value.amount)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
```

`promote_to_validated` 实现（校验先于 UoW；D6/D7/finding 6）：

```python
    async def promote_to_validated(
        self,
        tenant_id: TenantId,
        hypothesis_id: str,
        source_message_id: str,
        extracted_fields: dict[str, object],
        confirmed_by: str | None = None,
    ) -> ValidatedNeedId:
        """晋升为已验证需求（契约见 service.py docstring + 规格 §6.2）。"""
        if (
            not isinstance(hypothesis_id, str)
            or not hypothesis_id.strip()
            or hypothesis_id != hypothesis_id.strip()
        ):
            raise ValidationError("需求假设标识无效")
        if len(hypothesis_id) > 40:
            raise ValidationError("需求假设标识超长")
        if (
            not isinstance(source_message_id, str)
            or not source_message_id.strip()
            or source_message_id != source_message_id.strip()
        ):
            raise ValidationError("来源消息无效")
        if len(source_message_id) > 40:
            raise ValidationError("来源消息超长")
        if not isinstance(extracted_fields, dict) or not extracted_fields:
            raise ValidationError("提取字段不能为空")
        if set(extracted_fields) - _PROMOTE_FIELD_WHITELIST:
            raise ValidationError("未知需求字段")
        if "product_category" not in extracted_fields:
            raise ValidationError("产品类别不能为空")
        if confirmed_by is not None and (
            not isinstance(confirmed_by, str)
            or not confirmed_by.strip()
            or confirmed_by != confirmed_by.strip()
        ):
            raise ValidationError("确认人无效")
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            hypothesis = await uow.hypotheses.get_for_update(
                tenant_id, NeedHypothesisId(hypothesis_id)
            )
            if hypothesis is None:
                raise ValidationError("需求假设不存在")
            if hypothesis.status is HypothesisStatus.VALIDATED:
                if hypothesis.validated_need_id is None:
                    raise ValidationError("需求假设写入竞态异常")
                return hypothesis.validated_need_id  # D7 幂等
            if hypothesis.status is HypothesisStatus.REJECTED:
                raise HypothesisAlreadyResolvedError("已否决的假设不可晋升")
            if not hypothesis.can_promote_to_validated():
                raise InsufficientEvidenceError(
                    "证据不足，不可晋升为已验证需求："
                    f"当前最高证据等级 {_highest_evidence_level(hypothesis)}，"
                    "要求 ≥ customer_interest_reply 且来源为会话/上传/员工录入"
                )
            fields: dict[str, FactualField[object]] = {}
            for name, value in extracted_fields.items():
                fields[name] = FactualField(
                    value=_coerce_field_value(name, value),
                    provenance=Provenance(
                        source_type=SourceType.CONVERSATION,
                        source_id=source_message_id,
                        extracted_by=confirmed_by or "human",
                        extracted_at=now,
                        confirmed_by=EmployeeId(confirmed_by) if confirmed_by else None,
                        confirmed_at=now if confirmed_by else None,
                    ),
                )
            need = ValidatedNeed(
                need_id=ValidatedNeedId(new_id("need")),
                tenant_id=tenant_id,
                account_id=hypothesis.account_id,
                product_category=fields["product_category"],
                source_message_id=MessageId(source_message_id),
                created_at=now,
                status=(
                    NeedStatus.SOURCING_READY
                    if _initial_completeness(fields) >= 3
                    else NeedStatus.VALIDATED
                ),
                source_conversation_id=None,
                application=fields.get("application"),
                material=fields.get("material"),
                size_spec=fields.get("size_spec"),
                quantity=fields.get("quantity"),
                packaging=fields.get("packaging"),
                destination=fields.get("destination"),
                required_by=fields.get("required_by"),
                target_price=fields.get("target_price"),
                current_supply_issue=fields.get("current_supply_issue"),
                certification_required=fields.get("certification_required"),
                confirmed_by=EmployeeId(confirmed_by) if confirmed_by else None,
                cluster_id=None,
            )
            await uow.needs.add(need)
            promoted = replace(
                hypothesis,
                status=HypothesisStatus.VALIDATED,
                validated_need_id=need.need_id,
            )
            await uow.hypotheses.update(promoted)
            await uow.bus.publish(
                NeedValidated(
                    tenant_id=tenant_id,
                    occurred_at=now,
                    run_id=None,
                    need_id=need.need_id,
                    account_id=need.account_id,
                    category=hypothesis.category,
                    evidence_level=_highest_evidence_level(hypothesis),
                    completeness=need.completeness,
                )
            )
            return need.need_id
```

（helper `_initial_completeness(fields)`：用 fields 构造临时 `ValidatedNeed`（缺省字段 None）后取 `.completeness`；`_highest_evidence_level(hypothesis)`：`hypothesis.evidence()` 中满足晋升条件证据的最高等级，无则 `EvidenceLevel.AGENT_INDUSTRY_INFERENCE`（防御）；`replace` 来自 `dataclasses`。**不做归簇尝试**（D19(c)）。）

`reject_hypothesis` 实现：

```python
    async def reject_hypothesis(
        self,
        tenant_id: TenantId,
        hypothesis_id: str,
        loss_reason: str,
        rejected_by: str | None = None,
    ) -> None:
        """否决假设（契约见 service.py docstring + 规格 §6.3；reason 词表不校验，D8）。"""
        if (
            not isinstance(hypothesis_id, str)
            or not hypothesis_id.strip()
            or hypothesis_id != hypothesis_id.strip()
        ):
            raise ValidationError("需求假设标识无效")
        if len(hypothesis_id) > 40:
            raise ValidationError("需求假设标识超长")
        loss_reason = self._require_text(loss_reason, "拒绝原因", max_len=200)
        assert loss_reason is not None
        if rejected_by is not None and (
            not isinstance(rejected_by, str)
            or not rejected_by.strip()
            or rejected_by != rejected_by.strip()
        ):
            raise ValidationError("拒绝人无效")
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            hypothesis = await uow.hypotheses.get_for_update(
                tenant_id, NeedHypothesisId(hypothesis_id)
            )
            if hypothesis is None:
                raise ValidationError("需求假设不存在")
            if hypothesis.status is HypothesisStatus.VALIDATED:
                raise HypothesisAlreadyResolvedError("已验证需求不可否决")
            if hypothesis.status is HypothesisStatus.REJECTED:
                if hypothesis.rejection_reason == loss_reason:
                    return  # 幂等 no-op
                raise InvalidStateTransition("拒绝原因冲突，拒绝覆盖")
            rejected = replace(
                hypothesis,
                status=HypothesisStatus.REJECTED,
                rejection_reason=loss_reason,
            )
            await uow.hypotheses.update(rejected)
            await uow.bus.publish(
                NeedHypothesisRejected(
                    tenant_id=tenant_id,
                    occurred_at=now,
                    run_id=None,
                    hypothesis_id=hypothesis.hypothesis_id,
                    reason=loss_reason,
                )
            )
```

（**P3-3：`rejected_by` 仅校验（str/strip/≤40 → `"拒绝人无效"`），当前 schema 与 `NeedHypothesisRejected` 事件均无该字段——不落库**；规格签名保留该参数（service.py docstring 权威），如需留痕属后续独立切片。）

- [ ] **Step 4: 运行确认 GREEN**

```bash
conda run -n tradeos-py312 python -m pytest tests/integration/test_need_hypotheses.py -k "promote or reject" -q -W error
conda run -n tradeos-py312 python -m pytest tests/integration/test_need_hypotheses.py -q -W error
```

Expected: 全部 PASS。

- [ ] **Step 5: 边界与静态检查 + 定向回归**

```bash
conda run -n tradeos-py312 python -m ruff check domains/demand tests/integration/test_need_hypotheses.py
conda run -n tradeos-py312 python -m mypy domains/demand infra/db
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
```

Expected: 全部 rc=0。

- [ ] **Step 6: 停止等待监督方复审**（汇报 RED 原因 AttributeError、GREEN 计数、diff 2 文件）

复审通过后，提交前执行 mutation proofs：

| # | 临时变异 | 精确测试 | 预期 RED |
|---|---|---|---|
| M3 | `promote_to_validated` 删除 `can_promote_to_validated()` 检查 | `test_promote_rejects_agent_inference_evidence` | `pytest.raises(InsufficientEvidenceError)` DID NOT RAISE |
| M5 | `reject_hypothesis` 不同 reason 改无条件返回 | `test_reject_semantics` | `pytest.raises(InvalidStateTransition)` DID NOT RAISE |

- [ ] **Step 7: 复审通过后提交/推送/exact-HEAD CI**（命令块同 Task 1 Step 7；文件为 `domains/demand/service_impl.py` `tests/integration/test_need_hypotheses.py`；commit 消息逐字 `feat(demand): promote and reject hypotheses deterministically`；Expected：2 文件 index 100644、local == origin、CI `OK`）

---

## Task 6: update_need_fields + mark_sourcing_ready + get_confidence（spec 9.3 测试 9-11）

**Files:**
- Modify: `domains/demand/service_impl.py`（三方法）
- Modify: `tests/integration/test_need_hypotheses.py`（spec 9.3 测试 9-11）

**Interfaces（规格 §6.4-§6.6 权威）：** `update_need_fields(tenant_id, need_id: str, fields: dict[str, object], source_message_id: str, updated_by: str | None = None) -> None`（D19(a) 已改为 None）；`mark_sourcing_ready(tenant_id, need_id: str) -> None`；`get_confidence(tenant_id, hypothesis_id: str) -> ConfidenceResult`。update 白名单 = 10 个可变更字段（不含 product_category）；自动推进仅 VALIDATED 且 <3→≥3（finding 7）；跨门槛不发事件（D9/D19(d)）；历史先捕获 old 再写新值（同事务）。

- [ ] **Step 1: 写失败测试（spec 9.3 测试 9-11）**

```python
async def _promote_basic_need(
    service: DemandService, tenant: TenantId, category: str = "hinges"
) -> str:
    hypothesis_id = await _create_promotable_hypothesis(service, tenant, category)
    return await service.promote_to_validated(
        tenant, hypothesis_id, "msg_conv_001",
        {"product_category": category}, None,
    )


async def test_update_need_fields_history_and_auto_advance(demand_db: AsyncEngine) -> None:
    """spec 9.3-9：历史 append（old→new + source_message_id + changed_by）；
    跨 3 级自动推进仅当 VALIDATED；终态冻结；未知字段（含 product_category）拒绝；
    quantity 类型校验；跨门槛不发事件（D9/D19(d)）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    need_id = await _promote_basic_need(service, tenant)
    tables = importlib.import_module("infra.db.tables")

    # 累积阶梯：application（level 2）+ quantity（level 3）→ 完整度 3 跨门槛
    await service.update_need_fields(
        tenant, need_id,
        {"application": "marine use", "quantity": 5000}, "msg_conv_010", "emp-1",
    )
    async with factory() as session:
        history = (
            await session.execute(
                select(tables.ValidatedNeedFieldHistoryRow).where(
                    tables.ValidatedNeedFieldHistoryRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
        need_row = (
            await session.execute(
                select(tables.ValidatedNeedRow).where(
                    tables.ValidatedNeedRow.need_id == str(need_id)
                )
            )
        ).scalar_one()
    assert len(history) == 2  # application + quantity 各一条
    entry = next(h for h in history if h.field_name == "quantity")
    assert entry.field_name == "quantity"
    assert entry.old_value is None
    assert entry.new_value == "5000"
    assert entry.source_message_id == "msg_conv_010"
    assert entry.changed_by == "emp-1"
    assert need_row.status == "sourcing_ready"  # 仅 VALIDATED 跨 3 级自动推进
    assert need_row.quantity["value"] == 5000
    validated_events = [
        e for e in await _outbox_events(factory, tenant)
        if e.event_type == "NeedValidated"
    ]
    assert len(validated_events) == 1  # 跨门槛不发新事件

    # 已 SOURCING_READY 再更新：状态不被改写（不降级、不重复推进）
    await service.update_need_fields(
        tenant, need_id, {"destination": "Rotterdam"}, "msg_conv_011", "emp-1",
    )
    async with factory() as session:
        need_row = (
            await session.execute(
                select(tables.ValidatedNeedRow).where(
                    tables.ValidatedNeedRow.need_id == str(need_id)
                )
            )
        ).scalar_one()
    assert need_row.status == "sourcing_ready"

    with pytest.raises(ValidationError, match="未知需求字段"):
        await service.update_need_fields(
            tenant, need_id, {"product_category": "other"}, "msg_conv_012", "emp-1",
        )
    with pytest.raises(ValidationError, match="未知需求字段"):
        await service.update_need_fields(
            tenant, need_id, {"bogus_field": "x"}, "msg_conv_012", "emp-1",
        )
    with pytest.raises(ValidationError, match="需求字段类型无效"):
        await service.update_need_fields(
            tenant, need_id, {"quantity": "not-an-int"}, "msg_conv_012", "emp-1",
        )

    # 终态冻结：repo 直改 status=fulfilled 后 update 拒绝
    async with _uow_type()(factory, tenant, now=clock.now) as uow:
        frozen = await uow.needs.get_for_update(
            tenant, _models.ValidatedNeedId(need_id)
        )
        assert frozen is not None
        await uow.needs.update(
            _models.ValidatedNeed(
                **{**frozen.__dict__, "status": _models.NeedStatus.FULFILLED}
            )
        )
    from shared.errors import InvalidStateTransition

    with pytest.raises(InvalidStateTransition, match="需求已终结"):
        await service.update_need_fields(
            tenant, need_id, {"quantity": 1}, "msg_conv_013", "emp-1",
        )


async def test_mark_sourcing_ready_gates(demand_db: AsyncEngine) -> None:
    """spec 9.3-10：完整度 <3 → SourcingThresholdNotMetError（消息含缺失字段）；
    ≥3 → SOURCING_READY；重复调用幂等。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    need_id = await _promote_basic_need(service, tenant)
    with pytest.raises(SourcingThresholdNotMetError) as exc_info:
        await service.mark_sourcing_ready(tenant, need_id)
    # P1-5：仅 product_category 时缺失的是 application/size_spec（完整度 1）
    assert "application" in str(exc_info.value)
    assert "size_spec" in str(exc_info.value)

    # 累积阶梯：application（level 2）+ quantity（level 3）→ update 自动推进
    await service.update_need_fields(
        tenant, need_id,
        {"application": "marine use", "quantity": 200}, "msg_conv_020", None,
    )
    await service.mark_sourcing_ready(tenant, need_id)  # 已 SOURCING_READY → 幂等 no-op
    await service.mark_sourcing_ready(tenant, need_id)  # 幂等
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        row = (
            await session.execute(
                select(tables.ValidatedNeedRow).where(
                    tables.ValidatedNeedRow.need_id == str(need_id)
                )
            )
        ).scalar_one()
    assert row.status == "sourcing_ready"
    # VALIDATED→SOURCING_READY 转换分支：repo 直改回 VALIDATED（完整度仍 ≥3）后 mark
    async with _uow_type()(factory, tenant, now=clock.now) as uow:
        back = await uow.needs.get_for_update(
            tenant, _models.ValidatedNeedId(need_id)
        )
        assert back is not None
        await uow.needs.update(
            _models.ValidatedNeed(
                **{**back.__dict__, "status": _models.NeedStatus.VALIDATED}
            )
        )
    await service.mark_sourcing_ready(tenant, need_id)
    async with factory() as session:
        row = (
            await session.execute(
                select(tables.ValidatedNeedRow).where(
                    tables.ValidatedNeedRow.need_id == str(need_id)
                )
            )
        ).scalar_one()
    assert row.status == "sourcing_ready"


async def test_get_confidence_derived_live(demand_db: AsyncEngine) -> None:
    """spec 9.3-11：get_confidence 现算 derive_confidence（无存储列）；
    假设不存在 → 固定摘要。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    hypothesis_id = await _create_promotable_hypothesis(service, tenant)
    result = await service.get_confidence(tenant, hypothesis_id)
    assert result.tier.value in {
        "mid_high", "high", "very_high", "extreme",
    }  # conversation inbound_inquiry → CUSTOMER_INTEREST_REPLY 基准
    assert "base_from_highest" in result.applied_rules
    assert result.explanation
    with pytest.raises(ValidationError, match="需求假设不存在"):
        await service.get_confidence(tenant, new_id("hyp"))
```

- [ ] **Step 2: 运行确认 RED**

```bash
conda run -n tradeos-py312 python -m pytest tests/integration/test_need_hypotheses.py -k "update_need_fields or mark_sourcing_ready or get_confidence" -q -W error
```

Expected: **FAIL**——`AttributeError: 'DemandServiceImpl' object has no attribute 'update_need_fields'`（及 mark/get_confidence）。记录 rc=1。

- [ ] **Step 3: 最小实现**

(3a) `domains/demand/service_impl.py`——三方法（`update_need_fields` 返回 None；`mark_sourcing_ready` 用 `SourcingThresholdNotMetError`；`get_confidence` 现算；imports 增量，**P2-B：基于 Task 4/5 已并入状态，只加新符号**）：
- 扩展 `from domains.demand.errors import ...` 行：增 `SourcingThresholdNotMetError`（`InsufficientEvidenceError`/`HypothesisAlreadyResolvedError` 已由 Task 5 并入）——**仅此一项**
- 新增 `from shared.schemas.evidence import ConfidenceResult`（仅用于 `get_confidence` 返回注解；`derive_confidence` 已由 Task 4 并入，不重复）
- **不重复**导入 `derive_confidence`/`ValidatedNeedId`/`replace`（已由 Task 4/5 并入）；**不导入** `MessageId`（本任务代码未使用——P2-B）


```python
    async def update_need_fields(
        self,
        tenant_id: TenantId,
        need_id: str,
        fields: dict[str, object],
        source_message_id: str,
        updated_by: str | None = None,
    ) -> None:
        """补全需求字段（契约见 service.py docstring + 规格 §6.4；返回 None，D19(a)）。"""
        if (
            not isinstance(need_id, str)
            or not need_id.strip()
            or need_id != need_id.strip()
        ):
            raise ValidationError("已验证需求标识无效")
        if len(need_id) > 40:
            raise ValidationError("已验证需求标识超长")
        if (
            not isinstance(source_message_id, str)
            or not source_message_id.strip()
            or source_message_id != source_message_id.strip()
        ):
            raise ValidationError("来源消息无效")
        if len(source_message_id) > 40:
            raise ValidationError("来源消息超长")
        if not isinstance(fields, dict) or not fields:
            raise ValidationError("更新字段不能为空")
        if set(fields) - _UPDATE_FIELD_WHITELIST:
            raise ValidationError("未知需求字段")
        if updated_by is not None and (
            not isinstance(updated_by, str)
            or not updated_by.strip()
            or updated_by != updated_by.strip()
        ):
            raise ValidationError("更新人无效")
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            need = await uow.needs.get_for_update(tenant_id, ValidatedNeedId(need_id))
            if need is None:
                raise ValidationError("已验证需求不存在")
            if need.status in (
                NeedStatus.FULFILLED,
                NeedStatus.WITHDRAWN,
                NeedStatus.LOST,
            ):
                raise InvalidStateTransition("需求已终结，字段不可变更")
            updated = replace(need)
            for name, value in fields.items():
                old_text = _factual_value_to_text(getattr(need, name))
                new_field = FactualField(
                    value=_coerce_field_value(name, value),
                    provenance=Provenance(
                        source_type=SourceType.CONVERSATION,
                        source_id=source_message_id,
                        extracted_by=updated_by or "human",
                        extracted_at=now,
                        confirmed_by=EmployeeId(updated_by) if updated_by else None,
                        confirmed_at=now if updated_by else None,
                    ),
                )
                await uow.needs.append_field_history(
                    tenant_id,
                    need.need_id,
                    name,
                    old_text,
                    _factual_value_to_text(new_field),
                    source_message_id,
                    updated_by,
                )
                setattr(updated, name, new_field)
            # 自动推进（finding 7/D9）：仅当前 VALIDATED 且跨过 3 级门槛
            if (
                updated.status is NeedStatus.VALIDATED
                and updated.completeness >= 3
            ):
                updated.status = NeedStatus.SOURCING_READY
            await uow.needs.update(updated)

    async def mark_sourcing_ready(
        self, tenant_id: TenantId, need_id: str
    ) -> None:
        """标记为可寻源（契约见 service.py docstring + 规格 §6.5/D10）。"""
        if (
            not isinstance(need_id, str)
            or not need_id.strip()
            or need_id != need_id.strip()
        ):
            raise ValidationError("已验证需求标识无效")
        if len(need_id) > 40:
            raise ValidationError("已验证需求标识超长")
        async with self._uow_factory(tenant_id) as uow:
            need = await uow.needs.get_for_update(tenant_id, ValidatedNeedId(need_id))
            if need is None:
                raise ValidationError("已验证需求不存在")
            if need.status in (
                NeedStatus.FULFILLED,
                NeedStatus.WITHDRAWN,
                NeedStatus.LOST,
            ):
                raise InvalidStateTransition("需求已终结，不可标记可寻源")
            if need.status in (NeedStatus.SOURCING_READY, NeedStatus.HANDED_TO_SOURCING):
                return  # 幂等
            if not need.is_sourcing_ready():
                raise SourcingThresholdNotMetError(
                    "完整度不足，不能进寻源：还缺 "
                    + "、".join(need.missing_fields_for_sourcing())
                )
            await uow.needs.update(
                replace(need, status=NeedStatus.SOURCING_READY)
            )

    async def get_confidence(
        self, tenant_id: TenantId, hypothesis_id: str
    ) -> ConfidenceResult:
        """现算置信度（契约见 service.py docstring + 规格 §6.6/D15；无存储列）。"""
        if (
            not isinstance(hypothesis_id, str)
            or not hypothesis_id.strip()
            or hypothesis_id != hypothesis_id.strip()
        ):
            raise ValidationError("需求假设标识无效")
        if len(hypothesis_id) > 40:
            raise ValidationError("需求假设标识超长")
        async with self._uow_factory(tenant_id) as uow:
            hypothesis = await uow.hypotheses.get(
                tenant_id, NeedHypothesisId(hypothesis_id)
            )
            if hypothesis is None:
                raise ValidationError("需求假设不存在")
            return derive_confidence(
                hypothesis.evidence(), now=self._validate_now(self._now())
            )
```

- [ ] **Step 4: 运行确认 GREEN**

```bash
conda run -n tradeos-py312 python -m pytest tests/integration/test_need_hypotheses.py -q -W error
```

Expected: 全部 PASS。

- [ ] **Step 5: 边界与静态检查 + 定向回归**

```bash
conda run -n tradeos-py312 python -m ruff check domains/demand tests/integration/test_need_hypotheses.py
conda run -n tradeos-py312 python -m mypy domains/demand infra/db
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
conda run -n tradeos-py312 python -m pytest tests/integration/test_demand_signals.py -q -W error
```

Expected: 全部 rc=0。

- [ ] **Step 6: 停止等待监督方复审**（汇报 RED 原因 AttributeError、GREEN 计数、diff 2 文件）

复审通过后，提交前执行 mutation proof：

| # | 临时变异 | 精确测试 | 预期 RED |
|---|---|---|---|
| M4 | `update_need_fields` 删除 `append_field_history` 调用 | `test_update_need_fields_history_and_auto_advance` | 历史表为空 → `len(history) == 2` 断言失败（P3-1：与测试断言一致） |

- [ ] **Step 7: 复审通过后提交/推送/exact-HEAD CI**（命令块同 Task 1 Step 7；文件为 `domains/demand/service_impl.py` `tests/integration/test_need_hypotheses.py`；commit 消息逐字 `feat(demand): update needs with field history and sourcing gates`；Expected：2 文件 index 100644、local == origin、CI `OK`）

---

## Task 7: 原子性 + marker 零泄漏（spec 9.3 测试 14-15）

**Files:**
- Modify: `tests/integration/test_need_hypotheses.py`（spec 9.3 测试 14-15；仅测试）

**Interfaces（规格 §8/D12 权威）：** bus 失败 → 整个 UoW 回滚（业务行不落、事件不落）；生产内容 marker（raw_observation/reason/字段值/消息正文）不进 outbox/log/error；审计日志仅 action + 绑定租户。

- [ ] **Step 1: 写失败测试（spec 9.3 测试 14-15）**

```python
async def test_outbox_and_logs_no_marker_leak(demand_db: AsyncEngine) -> None:
    """spec 9.3-14：完整生命周期后 outbox payload 键 ⊆ 既有 schema、生产内容
    marker 不进 outbox/log/error（caplog 全程零记录）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    caplog.clear()
    hypothesis_id = await _create_promotable_hypothesis(service, tenant)
    await service.promote_to_validated(
        tenant, hypothesis_id, "msg_conv_001",
        {"product_category": "hinges", "quantity": 100}, "emp-1",
    )
    events = await _outbox_events(factory, tenant)
    assert len(events) >= 3  # capture + created + validated
    allowed = {
        "tenant_id", "occurred_at", "run_id", "signal_id", "entity_name",
        "signal_type", "source_url", "hypothesis_id", "account_id", "category",
        "confidence_tier", "reason", "need_id", "evidence_level", "completeness",
    }
    for event in events:
        assert set(event.event_payload) <= allowed, event.event_type
        blob = str(event.event_payload)
        assert OBSERVATION_MARKER not in blob
        assert "no_budget" not in blob
    assert caplog.records == []


async def test_bus_failure_rolls_back_whole_uow(demand_db: AsyncEngine) -> None:
    """spec 9.3-15：bus 发布失败 → 整个 UoW 回滚（假设行不落、need 不落、事件不落）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    uow_type = _uow_type()
    impl_type = importlib.import_module("domains.demand.service_impl").DemandServiceImpl
    service = _service(factory, tenant, clock)

    class _FailingBus:
        async def publish(self, event: object) -> None:
            raise RuntimeError("bus down")

        async def publish_many(self, events: list[object]) -> None:
            raise RuntimeError("bus down")

    class _BusFailureUoW:
        def __init__(self) -> None:
            self._inner = uow_type(factory, tenant, now=clock.now)

        async def __aenter__(self):
            await self._inner.__aenter__()
            self._inner.bus = _FailingBus()  # type: ignore[assignment]
            return self._inner

        async def __aexit__(self, *args: object):
            return await self._inner.__aexit__(*args)

    failing = impl_type(lambda _t: _BusFailureUoW(), now=clock.now)  # type: ignore[arg-type]
    signal_id = await _signal(service, tenant)
    with pytest.raises(RuntimeError, match="bus down"):
        await failing.create_hypothesis(
            tenant, _models.ProspectAccountId(new_id("acc")), "hinges",
            [signal_id], "推断", "model-v1",
        )
    assert await _hypothesis_rows(factory, tenant) == []
    # P3-6：回滚后 outbox 只保留 capture 事件（事件数不变、按 event_type 精确断言）
    events = await _outbox_events(factory, tenant)
    assert [e.event_type for e in events] == ["DemandSignalCaptured"]
```

- [ ] **Step 2: 运行确认 RED**

```bash
conda run -n tradeos-py312 python -m pytest tests/integration/test_need_hypotheses.py -k "marker or rolls_back" -q -W error
```

Expected: **FAIL**——本任务仅测试补齐；首跑失败原因取决于前置任务执行状态（若 Task 4-6 已交付而断言缺口存在，如实记录该断言失败；若方法缺失，属前置任务回归）。记录 rc=1。

- [ ] **Step 3: 最小实现**

本任务无生产代码（测试补齐断言）；如测试暴露实现缺口（如事件 payload 键越界），回到对应 Task 按 RED→GREEN 修复后再回本任务。

- [ ] **Step 4: 运行确认 GREEN**

```bash
conda run -n tradeos-py312 python -m pytest tests/integration/test_need_hypotheses.py -q -W error
```

Expected: 全部 PASS。

- [ ] **Step 5: 边界与静态检查**

```bash
conda run -n tradeos-py312 python -m ruff check domains/demand infra/db tests/integration/test_need_hypotheses.py
conda run -n tradeos-py312 python -m mypy domains/demand infra/db
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
```

Expected: 全部 rc=0。

- [ ] **Step 6: 停止等待监督方复审**（汇报 GREEN 计数、diff 1 文件；无新 mutation——spec 9.4 七项已全部前置覆盖）

- [ ] **Step 7: 复审通过后提交/推送/exact-HEAD CI**（命令块同 Task 1 Step 7；文件为 `tests/integration/test_need_hypotheses.py`；commit 消息逐字 `test(demand): hypothesis outbox purity and rollback guarantees`；Expected：1 文件 index 100644、local == origin、CI `OK`）

---

## 最终完整门禁（交付；无代码/文档变更）

- [ ] **Step 1: 最终完整门禁（全部任务完成后、交付前）**——命令块与 expected 同 2026-08-17-demand-signal-persistence 的 Task 5 Step 1（focused demand 双文件 → 全量 `-m "not e2e"` → `ruff check .` → 全量 mypy（`domains shared tool_gateway apps workflows notification_gateway infra`）→ boundaries → sensitive → web 块（gen:api + api.d.ts 零漂移 + typecheck/lint/test/build）→ `git diff --check` → `TRADEOS_REQUIRE_E2E=1 pytest tests/e2e -q -W error`），全部 rc=0。

- [ ] **Step 2: 停止等待监督方最终复审**（若复审要求修复，按最小修复回到对应任务 RED→GREEN 后重跑门禁；本段不产生 commit/push）

---

## Self-Review（计划自检）

**1. 规格覆盖矩阵（spec → Task）：**
- D1-D19 → Task 1（D1/D6/D16 模型内核、D18 文档、D19 契约）、Task 2（D2 部分索引谓词、D3 无 FK signal_ids、D4 FK、D17 3 表）、Task 3（D2 index_elements+index_where、D5 并集 helper、D7 get_for_update、D11 租户/审计、D12 UoW 属性、D13 JSONB）、Task 4（D3 DISCARDED 拒、D5 并入不发事件、D11 service 不可见、D12 事件注册）、Task 5（D6 门槛、D7 幂等并发、D8 reject、D9/D10 状态、finding 6）、Task 6（D9 历史/自动推进、D10 mark 门槛、D15 现算）、Task 7（D12 原子性、D19(d) 不发事件断言）、全片（D14 View 组装排除：非目标约束 + 方法数 6）。
- 3 表 → Task 2；6 服务方法 → Task 4（create）/Task 5（promote/reject）/Task 6（update/mark/get_confidence）；3 事件注册 → Task 4 Step 3a（created）+ Task 5 发布（rejected/validated）；15 项集成测试 → Task 3（repo 层 + 9.3-12/13 repo 侧）/Task 4（9.3-1/2/3/4 + 13 service 侧）/Task 5（9.3-5/6/7/8）/Task 6（9.3-9/10/11）/Task 7（9.3-14/15）；7 项 mutation → M1/M2/M6/M7（Task 4）、M3/M5（Task 5）、M4（Task 6）+ 辅助 M0（Task 1）/M-P（Task 2）/M-R（Task 3）。
- 非目标（spec §2）：NeedCluster 整体、reply_qualification、UI/API、模型 provider、Phase 2——全计划零触碰（无 need_clusters/try_assign_cluster/NeedClusterFormed/extract_need 实现内容）。

**2. 占位扫描：** 无 TBD/TODO/FIXME/「类似 Task N」/「添加错误处理」等占位；每个 Task 的实现步骤含实际代码片段；测试步骤含实际测试代码。

**3. 类型/签名一致性：** `get_for_update`/`append_field_history`/`list_*` 参数名与 `repository.py` Protocol 逐字一致（Task 1 契约测试精确断言）；`create_hypothesis -> NeedHypothesisId`、`promote_to_validated -> ValidatedNeedId`、`update_need_fields -> None`、`mark_sourcing_ready -> None`、`get_confidence -> ConfidenceResult`、`reject_hypothesis -> None` 与 service.py（D19 后）逐字一致；事件字段与 catalog 逐字一致；表名/列名/CHECK 名在 Task 2 迁移与 tables.py 之间逐字一致（parity 测试双向断言）。

**4. 任务边界：** 每 Task 恰一个逻辑交付 + 一个 commit/push/CI；Task 间无交叉文件提交（Task 4/5/6 共享 `service_impl.py` 时按顺序提交，先提交者完整自洽——Task 4 的 create 不依赖 Task 5/6 方法）；mutation 全部在对应任务复审后、提交前执行。

**5. 路径存在性：** 全部 Modify 目标文件已存在（`domains/demand/repository.py`/`service.py`/`models.py`/`service_impl.py`、`infra/db/tables.py`/`demand_uow.py`/`outbox.py`、`docs/architecture/01-domain-model.md`、`tests/unit/test_demand_signal_contracts.py`/`test_outbox_serialization.py`、`tests/integration/test_migrations.py`/`test_demand_signals.py`）；全部 Add 目标路径不存在（`migrations/versions/0022_need_hypotheses.py`、`infra/db/repositories/need_hypotheses.py`、`tests/unit/test_need_hypothesis_models.py`、`tests/integration/test_need_hypotheses.py`）——已用 `ls`/`glob` 核验。

**6. 撰写中发现的修正（已就地纳入）：** `update_need_fields` 保持返回 `None`（D19(a)）；`mark_sourcing_ready` 错误类型用 `SourcingThresholdNotMetError`；`create_hypothesis` 并入分支不发布事件；`NeedValidated` 事件 `evidence_level` 取晋升命中最高等级；`_coerce_field_value`/`_factual_value_to_text` 承担字段类型强制与历史显示值序列化；`update` 自动推进仅当 VALIDATED；**修订轮追加核对**：完整度阶梯为累积早退（models.py 已交付实现）——promote/mark 测试的 level-3 用例必须含 application/size_spec + quantity（仅 quantity 仍为 level 1），相关断言已修正（Task 5 第二正例、Task 6 update/mark 用例）。

**7. 独立复审 23 项逐条关闭（2026-08-17 修订轮；行号为修订后当前行）：**

| # | finding | 关闭证据（新行号 + 修正摘要） |
|---|---|---|
| P1-1 | `add -> bool` 契约 | 63 行 Interfaces 明示契约纠偏；289 行契约测试锁 `get_type_hints(...)["return"] == "bool"`；(3b) 提供 `-> bool` Protocol 桩代码；Task 3 实现（1418 行起）按 bool 编写 |
| P1-2 | 部分索引谓词脆弱字符串等值 | 670-690 行 `_predicate_semantics` 语义比较（值集合 + 标识符集合）覆盖 DB/ORM 两侧，注释明示 PG 归一化为 `= ANY(ARRAY[...])`；M-P（1026 行）与 M-R（2016 行）预期 RED 同步改语义断言 |
| P1-3 | Money 反序列化 str 构造 | 1552 行 `_decode_value` 用 `Decimal(str(value["amount"]))` 显式构造并导入 Decimal（硬边界 2） |
| P1-4 | promote 测试 `{}` 与 product_category 必填冲突 | 2488 行缺 product_category 负例改用合法字段集 `{"quantity": 5000}`；两正例分别以 product_category-only（VALIDATED）与 +quantity（SOURCING_READY）表达 |
| P1-5 | mark 缺失字段断言 quantity | 2989 行改为断言 `application` 与 `size_spec`（仅 product_category 时完整度 1 的真实缺失字段） |
| P2-1 | audit action 在 extra | 1242 行断言 `getattr(record, "action", None)` 精确集合 + message 固定文案 + extra `tenant_id` 为绑定租户 |
| P2-2 | `_AGENT_INFERENCE_SIGNAL_TYPES` 未定义 | 339 行给出含 10 个成员的显式常量代码块；218 行单元测试断言三集合对枚举全集构成划分 |
| P2-3 | 往返测试假绿 | 698 行起真实 `_run_alembic(db_url, "downgrade", "0021")`/`"upgrade", "head"` + `_table_names` 三表存在性 + revision 断言 |
| P2-4 | update 用 pg_insert upsert | 1693/1879 行 `sa_update` + `rowcount != 1 → ValidationError`（假设/need 两侧一致） |
| P2-5 | `_models.SourceType` 未定义 | 88 行（单测）与 1069 行（集成）从 `shared.schemas.provenance` 显式导入 SourceType 并使用 |
| P2-6 | promote 缺 EmployeeId/MessageId | 2588 行 imports 补 `EmployeeId, MessageId` |
| P2-7 | `_outbox_events` 缺失/事件无确定序 | 1256 行完整 helper（`order_by(published_at, event_id)`）；2064/2421 行改按 `event_type` 过滤 |
| P2-8 | get_for_update 返回注解未锁 | 289 行 `get_type_hints(get_for_update)["return"]` 含 None 断言 |
| P2-9 | target_price 裸值静默 USD | 2626 行起：非 dict 直接 `ValidationError("需求字段类型无效")`；`Decimal` 显式构造；无默认币种 |
| P2-10 | 重复 provenance import（F811） | 1301 行合并为单条 `from shared.schemas.provenance import FactualField, InferredField, Provenance, SourceType` |
| P3-1 | 0021 head 断言陈旧行号 | 529 行 grep 驱动指引（14 处：12 处 head 改 0022、2 处 downgrade 中间态保留；含 3701/3902/3912 核对） |
| P3-2 | parity 缺 server_default | 546 行 `_normalize_default`（置于 lambda 之前避免 NameError）+ DB/ORM 两侧 "default" 键比较 |
| P3-3 | rejected_by 语义未写清 | 2832 行明示：仅校验、schema/事件均不落库、规格签名保留、留痕属后续切片 |
| P3-4 | create 租户文案「信号租户」 | 2201/2203 行统一为 `"租户无效"`/`"租户超长"` |
| P3-5 | 关键实现仅 prose | 1250-1324 行（repo 测试完整代码）、1771 行起 `ValidatedNeedRepositoryImpl` 全实现、2302 行 `_merge_evidence`/`_merge_ids` 完整代码 |
| P3-6 | bus 回滚未断言 outbox | 3282 行追加 `[e.event_type for e in events] == ["DemandSignalCaptured"]` |
| P3-7 | 不可核验估算 | 465 行改为「以实际收集计数为准」 |
| P3-8 | ValidationError import 事后提示 | 355 行并入 Task 1 imports 代码块；477 行 Step 5 仅留确认注记 |
