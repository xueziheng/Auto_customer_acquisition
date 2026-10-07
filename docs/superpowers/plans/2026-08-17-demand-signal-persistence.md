# Demand Signal Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按已批准设计规格 `docs/superpowers/specs/2026-08-16-demand-signal-persistence-design.md` 交付 DemandSignal capture + discard 最小持久化切片：单表 `demand_signals`（0021 迁移）、真实 PostgreSQL 仓储/UoW、`DemandServiceImpl.capture_signal`/`discard_signal`、原子 outbox 事件、并发去重与 discard 快照语义。

**Architecture:** 沿用 conversations 域既有模式：`domains/demand` 只放契约（service/repository/schemas，service_impl 浅域实现子集）；`infra/db` 放 tables 行/UoW/仓储实现；迁移 0021（down_revision="0020"）；`PostgresEventBus` 同事务 outbox。去重 key 为全非空 5 列来源身份 `(tenant_id, entity_name, signal_type, source_type, source_id)`（规格 §4 纠偏，否决 page_hash 可空 tuple）；discard 用 `SELECT ... FOR UPDATE` 返回**转换前快照**（规格 §6.1）。

**Tech Stack:** Python 3.12、SQLAlchemy 2.x async、PostgreSQL 16（testcontainers）、Alembic、pytest-asyncio、tradeos-py312 conda 环境；**每个命令块首行显式 export 完整 PATH**（禁止依赖前一 shell 的 PATH）。

## Global Constraints

- 九条全局硬边界全部生效；本片重点：8（每表 tenant_id + 每查询租户过滤 + repo 参数越界 TenantIsolationViolation + critical 审计日志不含输入）、4（Provenance 全字段落库，网页 source_id==page_hash）、3（无 confidence/evidence_level 列）、1（raw_observation/possible_need 不进 outbox/log/error——测试 marker 验证）、9（依赖方向 demand→shared 仅；infra 独立）。
- 规格 §2 纠偏：`SignalCaptureRequest` 新增必填 `source_id`/`extracted_by`；去重 key 全非空 5 列；`DemandSignalRepository.add -> bool`、`find_duplicate` 新签名、新增原子 `discard`；`list_unlinked` 在 concrete 实现中显式 `raise NotImplementedError`（签名与 Protocol 一致，docstring 注明原因）。
- 所有 str 输入 `item == item.strip()` 校验，原值精确存储比较，不 trim/normalize；固定中文错误摘要不回显。
- 事件：`DemandSignalCaptured` 现有 schema（metadata-only）不改；**需注册进 `infra/db/outbox.py` 的 EVENT_REGISTRY**（当前未注册，事实）。
- 新文件 Git mode `100644`（`git add --chmod=-x` + `git ls-files --stage` 验证）；AppleDouble 清理归零后才暂存；不提交 `.env`/凭证/构建产物。
- 每个小任务：RED（本地跑不提交，确认预期失败原因）→ 最小 GREEN → 定向回归 + 窄门禁 → **停止等待监督方复审**；复审通过后才 commit + push + exact-HEAD CI success（禁 force）。任何失败立即停止报告，不扩大范围、不修改测试迎合实现。
- 所有命令前台、timeout >= 1500000ms、禁止 background；**本计划不使用输出截断 pipeline**；任何新增 pipeline 显式 `bash -o pipefail` 或直接避免 pipeline。
- 编辑只用 apply_patch（heredoc 直输）；禁止 cat/printf/python 写仓库文件。

## File Structure（规格 §10，12 个路径）

```text
domains/demand/schemas.py                  Task 2：SignalCaptureRequest 增 source_id/extracted_by
domains/demand/service.py                  Task 2：capture/discard docstring 更新（签名不变）
domains/demand/repository.py               Task 2：DemandSignalRepository 契约纠偏 + DemandUnitOfWork Protocol
domains/demand/service_impl.py             Task 3（capture）/ Task 4（discard）：DemandServiceImpl
infra/db/tables.py                         Task 1：DemandSignalRow（18 列 + 8 CHECK）
infra/db/repositories/demand.py            Task 3：DemandSignalRepositoryImpl（discard 在 Task 4 追加）
infra/db/demand_uow.py                     Task 3：SqlAlchemyDemandUnitOfWork
infra/db/outbox.py                         Task 2：EVENT_REGISTRY 注册 DemandSignalCaptured
migrations/versions/0021_demand_signals.py Task 1：迁移（down_revision="0020"）
tests/integration/test_demand_signals.py   Task 3（capture）/ Task 4（discard）集成测试
tests/integration/test_migrations.py       Task 1：head 0020→0021 + EXPECTED_TABLES + 0021 契约/往返
docs/superpowers/plans/2026-08-17-demand-signal-persistence.md  本计划文件（经下方 Plan Delivery Gate 单独 docs commit 后成为实施前提）
```

---

## Plan Delivery Gate（实施前提）

本计划必须先经当前复审通过，再作为**单独 docs commit** 推送并等待 exact-HEAD CI success，之后才能开始 Task 1。

- [ ] **Step 1: 计划复审**：监督方复审本计划（含本 Gate 与全部任务）。未通过 → 按意见修订后重新提交复审；通过 → 继续 Step 2。

- [ ] **Step 2: 单独 docs commit**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
find . -name "._*" -not -path "./.git/*" -delete
git add --chmod=-x docs/superpowers/plans/2026-08-17-demand-signal-persistence.md
git ls-files --stage docs/superpowers/plans/2026-08-17-demand-signal-persistence.md
git diff --cached --check
git diff --cached --name-only
git commit -m "docs(demand): plan signal persistence"
```

Expected: index mode `100644`；cached name-only 恰 1 文件；commit 后 `git status --short` 为空。

- [ ] **Step 3: push（禁 force）**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
git push origin HEAD
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/codex/handbook-phase1-slice4-execution)"
```

Expected: `git rev-parse HEAD` == `git rev-parse origin/codex/handbook-phase1-slice4-execution`。

- [ ] **Step 4: exact-HEAD CI**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
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

Expected: 输出 `OK`（headSha == 本计划 commit SHA，completed + success）。后续每个 Task 的 commit 步骤使用同一 exact-HEAD CI 块（见各 Task 的 Step 7）。

- [ ] **Step 5: 实施开始条件**：Step 4 输出 `OK` 后才允许开始 Task 1。

---

### Task 1: 迁移 0021 + DemandSignalRow + 迁移测试

**Files:**
- Modify: `infra/db/tables.py`（追加 `DemandSignalRow`，含模块级枚举字面量常量）
- Create: `migrations/versions/0021_demand_signals.py`
- Modify: `tests/integration/test_migrations.py`（head 0020→0021、EXPECTED_TABLES、0021 契约/往返测试）

**Interfaces:**
- Consumes: 既有迁移 helper `_run_alembic(db_url, *cmd)`（test_migrations.py:107）、`_table_names(engine)`（:133）、`_columns(engine, table)`（:139）、`_type_key(value)`（:56）；既有 Row 模式（`ConversationClassificationCorrectionRow`）。
- Produces: `DemandSignalRow`（tables.py，18 列、PK `pk_demand_signals`、UNIQUE `uq_demand_signals_source_identity`、8 个 CHECK：`ck_demand_signals_type/status/source_type/confirmed_pair/web_evidence/discard_reason/core_nonblank/optional_nonblank`）；迁移 `0021_demand_signals.py`（down_revision="0020"，downgrade drop 表）；`EXPECTED_TABLES` 含 `"demand_signals"`。

- [ ] **Step 1: 写失败测试（test_migrations.py 修改）**

用 apply_patch 依次执行以下 4 项修改（顺序执行，每项后确认成功）：

(1a) 全部 head 字面量 `revision == "0020"` → `"0021"`：
先运行 `rg -n 'revision == "0020"' tests/integration/test_migrations.py`（当前 11 处，全部是 head 检查——行 2472/2625/2771/2959/3097/3349/3441/3465 及 0020 roundtrip 内等）；对每一处：`"0020"` → `"0021"`；带文案的两处 `"当前 Alembic head 未升级到 0020"` → `"当前 Alembic head 未升级到 0021"`；`"RED：0020 会话分类纠正迁移尚未创建"` → `"当前 Alembic head 未升级到 0021"`。**不改**任何 `revision == "0019"`/`"0018"` 等中间版本断言。

(1b) 重命名 `test_0020_classification_corrections_revision_present` → `test_0021_demand_signals_revision_present`，docstring 改为：
```text
"""已完成态契约：0021 迁移存在且为 alembic head。
校验 upgrade head 后 revision 为 "0021"；失败来源必须是缺失/错误版本的
迁移文件，而非语法/fixture/ImportError/环境错误。"""
```

(1c) `EXPECTED_TABLES`（test_migrations.py:61-71）增补：
```python
    "conversation_classification_corrections",
    "demand_signals",
)
```

(1d) 追加两个新测试函数（文件末尾）：

```python
async def test_0021_demand_signals_contract_matches_orm(db_url: str) -> None:
    """0021 契约：18 列/PK/UNIQUE/8 个 CHECK 与 ORM 语义 parity。

    enum CHECK 用字面量集合比对（Postgres 会把 IN 规范化为 ANY）；函数型
    CHECK（confirmed_pair/web_evidence/discard_reason/core/optional nonblank）
    用标识符集合比对（PG 反解析可能重排空白/括号，标识符集合稳定）。
    """
    from sqlalchemy import CheckConstraint, UniqueConstraint, inspect, text

    from infra.db.session import create_engine_from
    from infra.db.tables import DemandSignalRow

    engine = create_engine_from(db_url)
    try:
        async with engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "0021", "当前 Alembic head 未升级到 0021"
            db_contract = await conn.run_sync(
                lambda sync: {
                    "columns": {
                        item["name"]: _type_key(str(item["type"]))
                        for item in inspect(sync).get_columns("demand_signals")
                    },
                    "pk": list(
                        inspect(sync).get_pk_constraint("demand_signals")[
                            "constrained_columns"
                        ]
                    ),
                    "unique": {
                        str(item["name"]): sorted(item["column_names"])
                        for item in inspect(sync).get_unique_constraints("demand_signals")
                    },
                    "checks": {
                        str(item["name"]): str(item["sqltext"])
                        for item in inspect(sync).get_check_constraints("demand_signals")
                    },
                }
            )
    finally:
        await engine.dispose()

    orm_columns = {
        name: _type_key(column.type.compile(dialect=engine.dialect))
        for name, column in DemandSignalRow.__table__.columns.items()
    }
    orm_pk = [column.name for column in DemandSignalRow.__table__.primary_key.columns]
    orm_unique = {
        str(c.name): sorted(c.columns.keys())
        for c in DemandSignalRow.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }
    orm_checks = {
        str(c.name): str(c.sqltext)
        for c in DemandSignalRow.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    assert db_contract["columns"] == orm_columns
    assert db_contract["pk"] == orm_pk == ["tenant_id", "signal_id"]
    assert db_contract["unique"] == orm_unique == {
        "uq_demand_signals_source_identity": [
            "entity_name",
            "signal_type",
            "source_id",
            "source_type",
            "tenant_id",
        ]
    }
    assert set(db_contract["checks"]) == set(orm_checks) == {
        "ck_demand_signals_type",
        "ck_demand_signals_status",
        "ck_demand_signals_source_type",
        "ck_demand_signals_confirmed_pair",
        "ck_demand_signals_web_evidence",
        "ck_demand_signals_discard_reason",
        "ck_demand_signals_core_nonblank",
        "ck_demand_signals_optional_nonblank",
    }
    from domains.demand.models import SignalStatus, SignalType
    from shared.schemas.provenance import SourceType

    def _quoted(sql: str) -> set[str]:
        return set(re.findall(r"'([a-z_]+)'", sql))

    def _idents(sql: str) -> set[str]:
        return set(re.findall(r"[a-z_]+", sql))

    for name, expected in [
        ("ck_demand_signals_type", {item.value for item in SignalType}),
        ("ck_demand_signals_status", {item.value for item in SignalStatus}),
        ("ck_demand_signals_source_type", {item.value for item in SourceType}),
    ]:
        assert _quoted(db_contract["checks"][name]) == expected
        assert _quoted(orm_checks[name]) == expected
    for name in [
        "ck_demand_signals_confirmed_pair",
        "ck_demand_signals_web_evidence",
        "ck_demand_signals_discard_reason",
        "ck_demand_signals_core_nonblank",
        "ck_demand_signals_optional_nonblank",
    ]:
        assert _idents(db_contract["checks"][name]) == _idents(orm_checks[name])


async def test_0021_demand_signals_downgrade_roundtrip(db_url: str) -> None:
    """0021→0020→0021：downgrade 后新表消失、revision 回 0020，
    upgrade head 后表与契约恢复。"""
    from sqlalchemy import text

    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        async with engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "0021"
            assert "demand_signals" in await _table_names(engine)
        _run_alembic(db_url, "downgrade", "0020")
        async with engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "0020"
        assert "demand_signals" not in await _table_names(engine)
        _run_alembic(db_url, "upgrade", "head")
        async with engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "0021"
        assert "demand_signals" in await _table_names(engine)
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()
```

（注意：`re` 已在 test_migrations.py 顶部 import（:32）；`SignalType`/`SignalStatus`/`SourceType` 用 importlib 或直接 import 均可——本文件已允许 import domains 内部（既有 0018 测试直接 import `from domains.conversations.schemas import ReplyCategory`、`from infra.db.tables import ConversationClassificationRow`，domain-internals 规则不适用本文件——以 check_boundaries 实际结果为准，若被拦则改用 importlib。）

- [ ] **Step 2: 运行确认 RED**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
find . -name "._*" -not -path "./.git/*" -delete
python -m pytest tests/integration/test_migrations.py::test_0021_demand_signals_revision_present \
  tests/integration/test_migrations.py::test_0021_demand_signals_contract_matches_orm \
  tests/integration/test_migrations.py::test_0021_demand_signals_downgrade_roundtrip -q -W error
```

Expected: **FAIL**——`assert revision == "0021"` 处实际为 `"0020"`（失败原因：0021 迁移尚不存在，非 fixture/import/环境）；新增契约测试同时因表不存在（UndefinedTable）失败。记录 rc=1 与失败摘要。

- [ ] **Step 3: 最小实现（tables.py + 迁移）**

`infra/db/tables.py` 追加（放在 `ConversationClassificationCorrectionRow` 之后；`Text`/`text` 已在文件顶部 import）：

```python
_DEMAND_SIGNAL_TYPES = (
    "'public_rfq','tender_notice','inbound_inquiry','trade_show_request',"
    "'historical_unclosed_need','supplier_referral','product_line_expansion',"
    "'facility_expansion','new_market_entry','procurement_role_hiring',"
    "'distributor_change','new_certification','large_contract_won',"
    "'funding_or_merger','stockout_observed','negative_product_review',"
    "'supplier_complaint','marketplace_seller_activity','catalog_gap',"
    "'value_chain_adjacency','complementary_category'"
)
_DEMAND_SIGNAL_STATUSES = "'captured','linked_to_hypothesis','discarded'"
_DEMAND_SOURCE_TYPES = (
    "'conversation','web_page','upload','employee_input',"
    "'agent_inference','external_api'"
)


class DemandSignalRow(Base):
    """``demand_signals`` 行（规格 2026-08-16 §5）：模型字段 + Provenance 展开列。"""

    __tablename__ = "demand_signals"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "signal_id", name="pk_demand_signals"),
        UniqueConstraint(
            "tenant_id",
            "entity_name",
            "signal_type",
            "source_type",
            "source_id",
            name="uq_demand_signals_source_identity",
        ),
        CheckConstraint(
            f"signal_type IN ({_DEMAND_SIGNAL_TYPES})", name="ck_demand_signals_type"
        ),
        CheckConstraint(
            f"status IN ({_DEMAND_SIGNAL_STATUSES})", name="ck_demand_signals_status"
        ),
        CheckConstraint(
            f"source_type IN ({_DEMAND_SOURCE_TYPES})",
            name="ck_demand_signals_source_type",
        ),
        CheckConstraint(
            "(confirmed_by IS NULL) = (confirmed_at IS NULL)",
            name="ck_demand_signals_confirmed_pair",
        ),
        CheckConstraint(
            "source_type <> 'web_page' OR "
            "(source_url IS NOT NULL AND btrim(source_url) <> '' AND "
            "page_hash IS NOT NULL AND btrim(page_hash) <> '' AND "
            "source_id = page_hash)",
            name="ck_demand_signals_web_evidence",
        ),
        CheckConstraint(
            "(status = 'discarded') = "
            "(discard_reason IS NOT NULL AND btrim(discard_reason) <> '')",
            name="ck_demand_signals_discard_reason",
        ),
        CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(signal_id) <> '' AND "
            "btrim(entity_name) <> '' AND btrim(raw_observation) <> '' AND "
            "btrim(source_id) <> '' AND btrim(extracted_by) <> ''",
            name="ck_demand_signals_core_nonblank",
        ),
        CheckConstraint(
            "(possible_need IS NULL OR btrim(possible_need) <> '') AND "
            "(source_url IS NULL OR btrim(source_url) <> '') AND "
            "(page_hash IS NULL OR btrim(page_hash) <> '')",
            name="ck_demand_signals_optional_nonblank",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(32))
    signal_id: Mapped[str] = mapped_column(String(32))
    signal_type: Mapped[str] = mapped_column(String(40))
    entity_name: Mapped[str] = mapped_column(String(200))
    raw_observation: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), server_default=text("'captured'"))
    possible_need: Mapped[str | None] = mapped_column(Text)
    account_id: Mapped[str | None] = mapped_column(String(32))
    discard_reason: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(200))
    extracted_by: Mapped[str] = mapped_column(String(64))
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(String(32))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_url: Mapped[str | None] = mapped_column(String(2000))
    page_hash: Mapped[str | None] = mapped_column(String(200))
```

`migrations/versions/0021_demand_signals.py`（新文件，`down_revision = "0020"`）：

```python
"""demand_signals 表（DemandSignal capture/discard 最小切片）。

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_SIGNAL_TYPES = (
    "'public_rfq','tender_notice','inbound_inquiry','trade_show_request',"
    "'historical_unclosed_need','supplier_referral','product_line_expansion',"
    "'facility_expansion','new_market_entry','procurement_role_hiring',"
    "'distributor_change','new_certification','large_contract_won',"
    "'funding_or_merger','stockout_observed','negative_product_review',"
    "'supplier_complaint','marketplace_seller_activity','catalog_gap',"
    "'value_chain_adjacency','complementary_category'"
)
_SIGNAL_STATUSES = "'captured','linked_to_hypothesis','discarded'"
_SOURCE_TYPES = (
    "'conversation','web_page','upload','employee_input',"
    "'agent_inference','external_api'"
)


def upgrade() -> None:
    """创建 tenant-bound 信号表：PK(tenant,signal_id) + 来源身份 UNIQUE + 8 CHECK。"""
    op.create_table(
        "demand_signals",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("signal_id", sa.String(32), nullable=False),
        sa.Column("signal_type", sa.String(40), nullable=False),
        sa.Column("entity_name", sa.String(200), nullable=False),
        sa.Column("raw_observation", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default=sa.text("'captured'")
        ),
        sa.Column("possible_need", sa.Text(), nullable=True),
        sa.Column("account_id", sa.String(32), nullable=True),
        sa.Column("discard_reason", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(200), nullable=False),
        sa.Column("extracted_by", sa.String(64), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_by", sa.String(32), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_url", sa.String(2000), nullable=True),
        sa.Column("page_hash", sa.String(200), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "signal_id", name="pk_demand_signals"),
        sa.UniqueConstraint(
            "tenant_id",
            "entity_name",
            "signal_type",
            "source_type",
            "source_id",
            name="uq_demand_signals_source_identity",
        ),
        sa.CheckConstraint(
            f"signal_type IN ({_SIGNAL_TYPES})", name="ck_demand_signals_type"
        ),
        sa.CheckConstraint(
            f"status IN ({_SIGNAL_STATUSES})", name="ck_demand_signals_status"
        ),
        sa.CheckConstraint(
            f"source_type IN ({_SOURCE_TYPES})", name="ck_demand_signals_source_type"
        ),
        sa.CheckConstraint(
            "(confirmed_by IS NULL) = (confirmed_at IS NULL)",
            name="ck_demand_signals_confirmed_pair",
        ),
        sa.CheckConstraint(
            "source_type <> 'web_page' OR "
            "(source_url IS NOT NULL AND btrim(source_url) <> '' AND "
            "page_hash IS NOT NULL AND btrim(page_hash) <> '' AND "
            "source_id = page_hash)",
            name="ck_demand_signals_web_evidence",
        ),
        sa.CheckConstraint(
            "(status = 'discarded') = "
            "(discard_reason IS NOT NULL AND btrim(discard_reason) <> '')",
            name="ck_demand_signals_discard_reason",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(signal_id) <> '' AND "
            "btrim(entity_name) <> '' AND btrim(raw_observation) <> '' AND "
            "btrim(source_id) <> '' AND btrim(extracted_by) <> ''",
            name="ck_demand_signals_core_nonblank",
        ),
        sa.CheckConstraint(
            "(possible_need IS NULL OR btrim(possible_need) <> '') AND "
            "(source_url IS NULL OR btrim(source_url) <> '') AND "
            "(page_hash IS NULL OR btrim(page_hash) <> '')",
            name="ck_demand_signals_optional_nonblank",
        ),
    )


def downgrade() -> None:
    op.drop_table("demand_signals")
```

- [ ] **Step 4: 运行确认 GREEN**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
python -m pytest tests/integration/test_migrations.py::test_0021_demand_signals_revision_present \
  tests/integration/test_migrations.py::test_0021_demand_signals_contract_matches_orm \
  tests/integration/test_migrations.py::test_0021_demand_signals_downgrade_roundtrip -q -W error
python -m pytest tests/integration/test_migrations.py -q -W error
```

Expected: 3 个 0021 测试 PASS，随后全文件 PASS（39→41 项，0020 专项语义保留）。rc=0。

- [ ] **Step 5: 边界与静态检查**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
python -m ruff check infra/db/tables.py migrations/versions/0021_demand_signals.py tests/integration/test_migrations.py
python -m mypy domains/demand infra/db
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
```

Expected: 全部 rc=0。

- [ ] **Step 6: 停止等待监督方复审**

汇报：RED 命令与 rc、失败原因（0021 不存在）、GREEN 计数、diff 范围（3 文件）、无提交。**不 commit**。

- [ ] **Step 7: 复审通过后提交/推送/exact-HEAD CI**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
find . -name "._*" -not -path "./.git/*" -delete
git add --chmod=-x infra/db/tables.py migrations/versions/0021_demand_signals.py tests/integration/test_migrations.py
git ls-files --stage infra/db/tables.py migrations/versions/0021_demand_signals.py tests/integration/test_migrations.py
git diff --cached --check
git diff --cached --name-only
git commit -m "feat(demand): add demand_signals table via 0021"
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

Expected: 3 文件 index mode 全部 `100644`；push 后 local == origin；CI 输出 `OK`。

---

### Task 2: 契约基座（schemas / repository / service docstring / outbox 注册）

**Files:**
- Modify: `domains/demand/schemas.py:20-40`（`SignalCaptureRequest` 增两必填字段）
- Modify: `domains/demand/repository.py:20-55`（纠偏签名 + 新增 `DemandUnitOfWork` Protocol）
- Modify: `domains/demand/service.py:36-56`（capture/discard docstring 更新）
- Modify: `infra/db/outbox.py:36-60`（import + EVENT_REGISTRY 注册）
- Test: `tests/unit/test_demand_signal_contracts.py`（新）

**Interfaces:**
- Consumes: `SignalCaptureRequest` 既有字段；`DemandSignalRepository` 既有骨架。
- Produces: 新 `SignalCaptureRequest` 必填 `source_id: str`、`extracted_by: str`（置于可选字段前）；`DemandSignalRepository.add(signal) -> bool`、`find_duplicate(tenant_id, entity_name, signal_type, source_type, source_id)`、`discard(tenant_id, signal_id, reason) -> DemandSignal | None`、`list_unlinked` 保留；`DemandUnitOfWork` Protocol（`signals: DemandSignalRepository`、`bus: EventBus`、`__aenter__ -> Self`、`__aexit__`）；`EVENT_REGISTRY["DemandSignalCaptured"]`。

- [ ] **Step 1: 写失败测试**

```python
"""DemandSignal 持久化切片契约基座（2026-08-17 计划 Task 2；纯单元）。

RED 预期：本文件模块级 import 了 ``domains.demand.repository`` 的
``DemandUnitOfWork``——它尚未定义，pytest 在 **collection 阶段**即以
``ImportError: cannot import name 'DemandUnitOfWork'`` 终止，整文件不收集。
一次运行只会看到这一个失败（TypeError/KeyError 用例要到 GREEN 阶段才会执行），
但 collection ImportError 足以证明契约未实现；三个用例在实现后全部通过。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domains.demand.repository import DemandSignalRepository, DemandUnitOfWork
from domains.demand.schemas import SignalCaptureRequest
from infra.db.outbox import EVENT_REGISTRY
from shared.events.catalog import DemandSignalCaptured

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)


def test_signal_capture_request_requires_source_id_and_extracted_by() -> None:
    request = SignalCaptureRequest(
        signal_type="product_line_expansion",
        entity_name="Acme Manufacturing",
        raw_observation="Acme announced a new production facility.",
        observed_at=NOW,
        source_type="web_page",
        source_id="sha256:pagehash001",
        extracted_by="model-v1",
        source_url="https://example.com/acme",
        page_hash="sha256:pagehash001",
        possible_need="stainless steel hinges",
    )
    assert request.source_id == "sha256:pagehash001"
    assert request.extracted_by == "model-v1"
    # 必填性（GREEN 阶段执行）：省略任一必填字段都必须 TypeError——
    # 若实现给了默认值，以下断言即失败
    base = {
        "signal_type": "product_line_expansion",
        "entity_name": "Acme Manufacturing",
        "raw_observation": "Acme announced a new production facility.",
        "observed_at": NOW,
        "source_type": "web_page",
    }
    with pytest.raises(TypeError):
        SignalCaptureRequest(**base)
    with pytest.raises(TypeError):
        SignalCaptureRequest(**base, source_id="sha256:pagehash001")
    with pytest.raises(TypeError):
        SignalCaptureRequest(**base, extracted_by="model-v1")


def test_demand_unit_of_work_protocol_declared() -> None:
    assert DemandUnitOfWork is not None
    assert DemandSignalRepository is not None


def test_demand_signal_captured_registered_in_event_registry() -> None:
    assert EVENT_REGISTRY["DemandSignalCaptured"] is DemandSignalCaptured
```

- [ ] **Step 2: 运行确认 RED**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
python -m pytest tests/unit/test_demand_signal_contracts.py -q -W error
```

Expected: **FAIL**——collection 阶段 `ImportError: cannot import name 'DemandUnitOfWork' from 'domains.demand.repository'`，整文件不收集（rc=1）。此失败原因即「契约未实现」，足以进入 Step 3。

- [ ] **Step 3: 最小实现**

(3a) `domains/demand/schemas.py`——在 `observed_at`/`source_type` 之后、可选字段之前插入：

```python
    source_id: str
    """来源身份：网页类 = 页面哈希；非网页类 = message/upload/provider/
    员工录入记录 identity（调用方提供）。"""
    extracted_by: str
    """提取者（模型版本标识或 "human"；Provenance 要求具体版本）。"""
```

(3b) `domains/demand/repository.py`——`DemandSignalRepository` 改签名（docstring 同步说明纠偏理由：来源身份才是独立证据判据；page_hash 可空 tuple 方案已否决）：

```python
@runtime_checkable
class DemandSignalRepository(Protocol):
    async def add(self, signal: DemandSignal) -> bool:
        """来源身份冲突返回 False（不入库）；True=新插入。

        dedup identity = (tenant_id, entity_name, signal_type, source_type,
        source_id)——全非空五列（规格 §4；page_hash 可空 tuple 方案已否决）。
        """
        ...

    async def get(
        self, tenant_id: TenantId, signal_id: DemandSignalId
    ) -> DemandSignal | None: ...

    async def find_duplicate(
        self,
        tenant_id: TenantId,
        entity_name: str,
        signal_type: str,
        source_type: str,
        source_id: str,
    ) -> DemandSignal | None:
        """按来源身份 5 列查重复信号（同事务重读胜者用）。"""
        ...

    async def discard(
        self,
        tenant_id: TenantId,
        signal_id: DemandSignalId,
        reason: str,
    ) -> DemandSignal | None:
        """tenant-bound SELECT ... FOR UPDATE，返回转换前快照（规格 §6.1）：
        不存在 → None；CAPTURED → 同事务 UPDATE 为 discarded+reason 后返回
        更新前 snapshot；DISCARDED/LINKED_TO_HYPOTHESIS → 不改动返回当前
        snapshot。调用方不得把返回对象当作 DB 当前态。"""
        ...

    async def list_unlinked(
        self, tenant_id: TenantId, limit: int
    ) -> list[DemandSignal]:
        """列出尚未关联到假设的信号，供假设生成任务消费。"""
        ...
```

文件末尾追加（imports 增 `from shared.events.bus import EventBus`；`Self` 已在 typing 导入区）：

```python
@runtime_checkable
class DemandUnitOfWork(Protocol):
    """demand 域事务边界（域级接口；实现为 SqlAlchemyDemandUnitOfWork）。"""

    signals: DemandSignalRepository
    bus: EventBus

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None: ...
```

(3c) `domains/demand/service.py`——`capture_signal` docstring 增补：

```text
        - 输入（SignalCaptureRequest）由调用方提供 source_id/extracted_by；
          WEB_PAGE 时 source_id == page_hash 且 source_url/page_hash 必填，
          缺失抛 MissingWebEvidenceError
        - 去重 key = (tenant_id, entity_name, signal_type, source_type,
          source_id)（全非空 5 列）：同一来源身份视为同一信号，返回已有
          ID，不重复计费也不重复计数、不重复发布事件
```

`discard_signal` docstring 增补：

```text
        - 不存在/跨租户不可见 → ValidationError("需求信号不存在")
        - LINKED_TO_HYPOTHESIS → InvalidStateTransition（保护证据链）
        - 已 DISCARDED：同 reason 幂等 no-op；不同 reason → InvalidStateTransition
          （拒绝覆盖 first reason）
        - 不发布事件
```

(3d) `infra/db/outbox.py`——import 列表（:36-52 的 `from shared.events.catalog import (...)`）增 `DemandSignalCaptured,`（按字母序插在 `ComplaintReceived,` 前）；`EVENT_REGISTRY` 增：

```python
    # demand 信号捕获即发布（切片 7 producer）：metadata-only（signal_id/
    # entity_name/signal_type/source_url），不含 raw_observation/possible_need
    "DemandSignalCaptured": DemandSignalCaptured,
```

- [ ] **Step 4: 运行确认 GREEN**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
python -m pytest tests/unit/test_demand_signal_contracts.py -q -W error
python -m ruff check domains/demand/schemas.py domains/demand/repository.py domains/demand/service.py infra/db/outbox.py tests/unit/test_demand_signal_contracts.py
python -m mypy domains/demand infra/db
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
```

Expected: 全部 rc=0。

- [ ] **Step 5: 停止等待监督方复审**（汇报 RED 原因（collection ImportError）、GREEN、diff 4 文件 + 1 新测试文件；不 commit）

- [ ] **Step 6: 复审通过后提交/推送/exact-HEAD CI**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
git add --chmod=-x domains/demand/schemas.py domains/demand/repository.py \
  domains/demand/service.py infra/db/outbox.py tests/unit/test_demand_signal_contracts.py
git ls-files --stage domains/demand/schemas.py domains/demand/repository.py domains/demand/service.py infra/db/outbox.py tests/unit/test_demand_signal_contracts.py
git diff --cached --check
git diff --cached --name-only
git commit -m "feat(demand): contract base for signal persistence (schemas, uow protocol, event registry)"
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

Expected: 5 文件 index mode 全部 `100644`；push 后 local == origin；CI 输出 `OK`。

---

### Task 3: capture 垂直切片（repo 实现 + UoW + DemandServiceImpl.capture_signal）

**Files:**
- Create: `infra/db/repositories/demand.py`（`DemandSignalRepositoryImpl`：add/get/find_duplicate/list_unlinked 桩；discard 在 Task 4 追加）
- Create: `infra/db/demand_uow.py`（`SqlAlchemyDemandUnitOfWork`）
- Create: `domains/demand/service_impl.py`（`DemandServiceImpl.capture_signal`）
- Create: `tests/integration/test_demand_signals.py`（capture 测试 1-12）

**Interfaces:**
- Consumes: Task 1 `DemandSignalRow`；Task 2 `SignalCaptureRequest`（含 source_id/extracted_by）、`DemandUnitOfWork`、`EVENT_REGISTRY` 注册；既有 `PostgresEventBus`、`_ConversationsRepository` 租户检查模式（infra/db/repositories/conversations.py:45-62）。
- Produces: `DemandServiceImpl(uow_factory: Callable[[TenantId], DemandUnitOfWork], *, now: Callable[[], datetime])` 与 `capture_signal(tenant_id, request) -> str`（重复返回既有 signal_id、不重复事件）；`DemandSignalRepositoryImpl(session, tenant_id)`（`add -> bool`、`get`、`find_duplicate`、`list_unlinked` 显式 NotImplementedError 桩）；`SqlAlchemyDemandUnitOfWork(session_factory, tenant_id, *, now=None)`（`.signals`/`.bus`）。

- [ ] **Step 1: 写失败测试（tests/integration/test_demand_signals.py，capture 部分）**

```python
"""demand 信号 capture/discard 集成（2026-08-17 计划 Task 3/4；真实 PostgreSQL +
真实 UoW/仓储，零 mock）。

RED 预期：Task 3 各测试在运行时经 ``importlib.import_module("infra.db.demand_uow")``
加载不存在的模块，以 ``ModuleNotFoundError: No module named 'infra.db.demand_uow'``
失败（文件可收集，失败发生在测试体内——缺实现而非 fixture/环境错误）；Task 4
以 ``discard_signal`` 缺失（AttributeError）失败。跨层访问域内 models/repository
用 importlib（check_boundaries domain-internals 规则）；schemas/service 可静态 import。
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.demand.schemas import SignalCaptureRequest
from domains.demand.service import DemandService
from shared.errors import (
    InvalidStateTransition,
    TenantIsolationViolation,
    ValidationError,
)
from shared.schemas.identifiers import TenantId, new_id

_models = importlib.import_module("domains.demand.models")
_demand_errors = importlib.import_module("domains.demand.errors")
MissingWebEvidenceError = _demand_errors.MissingWebEvidenceError

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)

#: 生产内容 marker：outbox/log/error 均不得出现（规格 §5/§7/§9.2）
OBSERVATION_MARKER = "Acme SECRET-OBSERVATION-42 opened a new plant in Rotterdam."
NEED_MARKER = "stainless steel hinges SECRET-NEED-42"


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


def _service(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId, clock: MutableClock
) -> DemandService:
    uow_type = importlib.import_module(
        "infra.db.demand_uow"
    ).SqlAlchemyDemandUnitOfWork
    impl_type = importlib.import_module(
        "domains.demand.service_impl"
    ).DemandServiceImpl
    return impl_type(
        lambda requested: uow_type(factory, requested, now=clock.now),
        now=clock.now,
    )


def _request(**overrides: object) -> SignalCaptureRequest:
    fields: dict[str, object] = {
        "signal_type": "product_line_expansion",
        "entity_name": "Acme Manufacturing",
        "raw_observation": OBSERVATION_MARKER,
        "observed_at": NOW,
        "source_type": "web_page",
        "source_id": "sha256:pagehash001",
        "extracted_by": "model-v1",
        "source_url": "https://example.com/acme-expansion",
        "page_hash": "sha256:pagehash001",
        "possible_need": NEED_MARKER,
    }
    fields.update(overrides)
    return SignalCaptureRequest(**fields)  # type: ignore[arg-type]


async def _outbox_events(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[object]:
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        rows = (
            await session.execute(
                select(tables.OutboxEventRow).where(
                    tables.OutboxEventRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
    return list(rows)


async def _signal_rows(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[object]:
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        rows = (
            await session.execute(
                select(tables.DemandSignalRow).where(
                    tables.DemandSignalRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
    return list(rows)


async def test_capture_roundtrip_persists_all_columns(demand_db: AsyncEngine) -> None:
    """capture 落库：18 列全往返（含 provenance 展开列、confirmed pair None、
    possible_need/account_id/discard_reason None）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    signal_id = await service.capture_signal(tenant, _request())
    assert signal_id.startswith("sig_")
    rows = await _signal_rows(factory, tenant)
    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == str(tenant)
    assert row.signal_id == signal_id
    assert row.signal_type == "product_line_expansion"
    assert row.entity_name == "Acme Manufacturing"
    assert row.raw_observation == OBSERVATION_MARKER
    assert row.observed_at == NOW
    assert row.status == "captured"
    assert row.possible_need == NEED_MARKER
    assert row.account_id is None
    assert row.discard_reason is None
    assert row.source_type == "web_page"
    assert row.source_id == "sha256:pagehash001"
    assert row.extracted_by == "model-v1"
    assert row.extracted_at == NOW
    assert row.confirmed_by is None and row.confirmed_at is None
    assert row.source_url == "https://example.com/acme-expansion"
    assert row.page_hash == "sha256:pagehash001"


async def test_capture_web_evidence_fail_closed(demand_db: AsyncEngine) -> None:
    """WEB_PAGE 缺 url/hash 或 source_id != page_hash → MissingWebEvidenceError。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    cases = [
        {"source_url": None},
        {"page_hash": None},
        {"source_id": "sha256:other", "page_hash": "sha256:pagehash001"},
    ]
    for overrides in cases:
        with pytest.raises(MissingWebEvidenceError):
            await service.capture_signal(tenant, _request(**overrides))
    assert await _signal_rows(factory, tenant) == []


async def test_capture_non_web_different_source_ids_are_distinct(
    demand_db: AsyncEngine,
) -> None:
    """非网页：同 entity/type 不同 source_id → 两独立行、两事件。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    first = await service.capture_signal(
        tenant,
        _request(
            source_type="conversation",
            source_id="msg_conv_001",
            source_url=None,
            page_hash=None,
        ),
    )
    second = await service.capture_signal(
        tenant,
        _request(
            source_type="conversation",
            source_id="msg_conv_002",
            source_url=None,
            page_hash=None,
        ),
    )
    assert first != second
    assert len(await _signal_rows(factory, tenant)) == 2
    events = await _outbox_events(factory, tenant)
    assert len(events) == 2


async def test_capture_duplicate_replay_returns_first_id_single_event(
    demand_db: AsyncEngine,
) -> None:
    """同来源串行重放：返回首条 signal_id、1 行、1 事件、首条内容保留。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    first_id = await service.capture_signal(tenant, _request())
    replayed = await service.capture_signal(
        tenant, _request(raw_observation="DIFFERENT CONTENT")
    )
    assert replayed == first_id
    rows = await _signal_rows(factory, tenant)
    assert len(rows) == 1
    assert rows[0].raw_observation == OBSERVATION_MARKER  # 首条内容保留
    assert len(await _outbox_events(factory, tenant)) == 1


async def test_capture_concurrent_same_key_exactly_one_row_one_event(
    demand_db: AsyncEngine,
) -> None:
    """asyncio.gather 两个独立 UoW 同 key → 恰 1 行 1 事件、两结果 id 相同。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service_a = _service(factory, tenant, clock)
    service_b = _service(factory, tenant, clock)

    results = await asyncio.gather(
        service_a.capture_signal(tenant, _request()),
        service_b.capture_signal(tenant, _request()),
        return_exceptions=True,
    )
    assert all(isinstance(r, str) for r in results), results
    assert results[0] == results[1]
    assert len(await _signal_rows(factory, tenant)) == 1
    assert len(await _outbox_events(factory, tenant)) == 1


async def test_capture_cross_tenant_same_key_two_rows(demand_db: AsyncEngine) -> None:
    """A/B 同 (entity,type,source_type,source_id) → 各 1 行（不互相去重）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant_a = TenantId(new_id("tn"))
    tenant_b = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service_a = _service(factory, tenant_a, clock)
    service_b = _service(factory, tenant_b, clock)

    await service_a.capture_signal(tenant_a, _request())
    await service_b.capture_signal(tenant_b, _request())
    assert len(await _signal_rows(factory, tenant_a)) == 1
    assert len(await _signal_rows(factory, tenant_b)) == 1


async def test_capture_optional_fields_none_and_present(demand_db: AsyncEngine) -> None:
    """possible_need None / 非 None 两态往返。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    await service.capture_signal(tenant, _request(possible_need=None))
    rows = await _signal_rows(factory, tenant)
    assert len(rows) == 1 and rows[0].possible_need is None


async def test_demand_signals_db_checks_fail_closed(demand_db: AsyncEngine) -> None:
    """8 个 CHECK 违例 → IntegrityError（直插行验证约束语义）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    tables = importlib.import_module("infra.db.tables")
    models = _models

    def base() -> dict[str, object]:
        return {
            "tenant_id": str(tenant),
            "signal_id": new_id("sig"),
            "signal_type": "product_line_expansion",
            "entity_name": "Acme Manufacturing",
            "raw_observation": OBSERVATION_MARKER,
            "observed_at": NOW,
            "status": "captured",
            "source_type": "web_page",
            "source_id": "sha256:pagehash001",
            "extracted_by": "model-v1",
            "extracted_at": NOW,
            "source_url": "https://example.com/acme",
            "page_hash": "sha256:pagehash001",
        }

    bad_cases = [
        ("ck_demand_signals_type", {"signal_type": "not_a_type"}),
        ("ck_demand_signals_status", {"status": "bogus"}),
        ("ck_demand_signals_source_type", {"source_type": "bogus"}),
        (
            "ck_demand_signals_confirmed_pair",
            {"confirmed_by": "emp-1", "confirmed_at": None},
        ),
        (
            "ck_demand_signals_web_evidence",
            {"source_url": None, "page_hash": "sha256:pagehash001"},
        ),
        (
            "ck_demand_signals_web_evidence",
            {"source_id": "sha256:other", "page_hash": "sha256:pagehash001"},
        ),
        (
            "ck_demand_signals_discard_reason",
            {"status": "captured", "discard_reason": "noise"},
        ),
        (
            "ck_demand_signals_discard_reason",
            {"status": "discarded", "discard_reason": None},
        ),
        (
            "ck_demand_signals_core_nonblank",
            {"entity_name": "   "},
        ),
        (
            "ck_demand_signals_optional_nonblank",
            {"possible_need": "   "},
        ),
    ]
    for _label, overrides in bad_cases:
        values = base()
        values.update(overrides)
        async with factory() as session:
            with pytest.raises(IntegrityError):
                session.add(tables.DemandSignalRow(**values))  # type: ignore[arg-type]
                await session.flush()
            await session.rollback()
    assert await _signal_rows(factory, tenant) == []


async def test_capture_repo_tenant_mismatch_raises(demand_db: AsyncEngine) -> None:
    """repo 参数租户与绑定租户不一致 → TenantIsolationViolation。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant_a = TenantId(new_id("tn"))
    tenant_b = TenantId(new_id("tn"))
    uow_type = importlib.import_module("infra.db.demand_uow").SqlAlchemyDemandUnitOfWork
    clock = MutableClock(NOW)
    signal = _models.DemandSignal(
        signal_id=_models.DemandSignalId(new_id("sig")),
        tenant_id=tenant_a,
        signal_type=_models.SignalType.PRODUCT_LINE_EXPANSION,
        entity_name="Acme Manufacturing",
        raw_observation=OBSERVATION_MARKER,
        observed_at=NOW,
        provenance=_provenance_for(signal=None),  # 见下方辅助
    )
    # 辅助：构造真实 Provenance（与 _request 同源）
    async with uow_type(factory, tenant_b, now=clock.now) as uow:
        with pytest.raises(TenantIsolationViolation):
            await uow.signals.add(signal)
        with pytest.raises(TenantIsolationViolation):
            await uow.signals.get(tenant_a, signal.signal_id)
        with pytest.raises(TenantIsolationViolation):
            await uow.signals.find_duplicate(tenant_a, "Acme Manufacturing", "product_line_expansion", "web_page", "sha256:pagehash001")


def _provenance_for(signal: object):
    from shared.schemas.provenance import Provenance, SourceType

    return Provenance(
        source_type=SourceType.WEB_PAGE,
        source_id="sha256:pagehash001",
        extracted_by="model-v1",
        extracted_at=NOW,
        source_url="https://example.com/acme",
        page_hash="sha256:pagehash001",
    )


async def test_capture_input_validation_precedes_uow(demand_db: AsyncEngine) -> None:
    """输入校验先于 UoW/DB：空白/超长/非法枚举/naive 时间 → 固定摘要。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    cases: list[tuple[str, object]] = [
        ("空白 tenant", "   "),
        ("tenant 超 32", "t" * 33),
        ("空白 entity", "   "),
        ("entity 超 200", "e" * 201),
        ("空白 raw_observation", "   "),
        ("空白 source_id", "   "),
        ("source_id 超 200", "s" * 201),
        ("空白 extracted_by", "   "),
        ("extracted_by 超 64", "x" * 65),
        ("非法 signal_type", "bogus_type"),
        ("非法 source_type", "bogus_source"),
        ("前后空白 source_id", " sha256:pagehash001 "),
    ]
    for label, tenant_or_request in cases:
        with pytest.raises(ValidationError):
            if label == "空白 tenant" or label == "tenant 超 32":
                await service.capture_signal(tenant_or_request, _request())  # type: ignore[arg-type]
            else:
                await service.capture_signal(tenant, _request(**{"source_id": tenant_or_request}))  # type: ignore[arg-type]
    # naive observed_at
    with pytest.raises(ValidationError):
        await service.capture_signal(tenant, _request(observed_at=NOW.replace(tzinfo=None)))
    assert await _signal_rows(factory, tenant) == []


async def test_capture_outbox_metadata_only_no_marker_leak(
    demand_db: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """outbox payload 只含既有 schema 键；生产内容 marker 不进 outbox/log/error。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    caplog.clear()

    await service.capture_signal(tenant, _request())
    events = await _outbox_events(factory, tenant)
    assert len(events) == 1
    payload = dict(events[0].event_payload)
    assert set(payload) <= {
        "tenant_id",
        "occurred_at",
        "run_id",
        "signal_id",
        "entity_name",
        "signal_type",
        "source_url",
    }
    blob = str(payload)
    assert OBSERVATION_MARKER not in blob
    assert NEED_MARKER not in blob
    assert "pagehash001" not in blob
    assert caplog.records == []


async def test_capture_publish_failure_rolls_back_signal(demand_db: AsyncEngine) -> None:
    """bus 发布失败 → 整个 UoW 回滚，业务行不落库、outbox 不变。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    uow_type = importlib.import_module("infra.db.demand_uow").SqlAlchemyDemandUnitOfWork
    impl_type = importlib.import_module("domains.demand.service_impl").DemandServiceImpl

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

    service = impl_type(lambda _t: _BusFailureUoW(), now=clock.now)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="bus down"):
        await service.capture_signal(tenant, _request())
    assert await _signal_rows(factory, tenant) == []  # 业务行回滚
    assert await _outbox_events(factory, tenant) == []
```

- [ ] **Step 2: 运行确认 RED**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
python -m pytest tests/integration/test_demand_signals.py -q -W error
```

Expected: **FAIL**——各测试在 `_service` 构造处因 `importlib.import_module("infra.db.demand_uow")` 抛 `ModuleNotFoundError: No module named 'infra.db.demand_uow'`（缺实现，非 fixture/import 静态错误/环境）。记录 rc=1 与失败摘要。

- [ ] **Step 3: 最小实现**

(3a) `infra/db/repositories/demand.py`（新文件）：

```python
"""demand 域仓储实现（规格 2026-08-16 §6）。"""

from __future__ import annotations

import logging
from typing import cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from domains.demand.models import DemandSignal, SignalStatus, SignalType
from domains.demand.repository import DemandSignalRepository
from infra.db.tables import DemandSignalRow
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import (
    DemandSignalId,
    ProspectAccountId,
    TenantId,
)
from shared.schemas.provenance import Provenance, SourceType

_tenant_logger = logging.getLogger("infra.db.repositories.demand")


class _DemandRepository:
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        self._session = session
        self._tenant_id = tenant_id

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


def _signal_to_row(signal: DemandSignal) -> DemandSignalRow:
    return DemandSignalRow(
        tenant_id=str(signal.tenant_id),
        signal_id=str(signal.signal_id),
        signal_type=signal.signal_type.value,
        entity_name=signal.entity_name,
        raw_observation=signal.raw_observation,
        observed_at=signal.observed_at,
        status=signal.status.value,
        possible_need=signal.possible_need,
        account_id=(
            str(signal.account_id) if signal.account_id is not None else None
        ),
        discard_reason=signal.discard_reason,
        source_type=signal.provenance.source_type.value,
        source_id=signal.provenance.source_id,
        extracted_by=signal.provenance.extracted_by,
        extracted_at=signal.provenance.extracted_at,
        confirmed_by=signal.provenance.confirmed_by,
        confirmed_at=signal.provenance.confirmed_at,
        source_url=signal.provenance.source_url,
        page_hash=signal.provenance.page_hash,
    )


def _row_to_signal(row: DemandSignalRow) -> DemandSignal:
    return DemandSignal(
        signal_id=DemandSignalId(row.signal_id),
        tenant_id=TenantId(row.tenant_id),
        signal_type=SignalType(row.signal_type),
        entity_name=row.entity_name,
        raw_observation=row.raw_observation,
        observed_at=row.observed_at,
        status=SignalStatus(row.status),
        possible_need=row.possible_need,
        account_id=(
            ProspectAccountId(row.account_id) if row.account_id is not None else None
        ),
        discard_reason=row.discard_reason,
        provenance=Provenance(
            source_type=SourceType(row.source_type),
            source_id=row.source_id,
            extracted_by=row.extracted_by,
            extracted_at=row.extracted_at,
            confirmed_by=row.confirmed_by,
            confirmed_at=row.confirmed_at,
            source_url=row.source_url,
            page_hash=row.page_hash,
        ),
    )


class DemandSignalRepositoryImpl(_DemandRepository, DemandSignalRepository):
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        super().__init__(session, tenant_id)

    async def add(self, signal: DemandSignal) -> bool:
        """来源身份冲突返回 False；True=新插入（ON CONFLICT DO NOTHING）。"""
        self._require_tenant(signal.tenant_id, "demand_signal_add")
        result = await self._session.execute(
            pg_insert(DemandSignalRow)
            .values(
                tenant_id=str(signal.tenant_id),
                signal_id=str(signal.signal_id),
                signal_type=signal.signal_type.value,
                entity_name=signal.entity_name,
                raw_observation=signal.raw_observation,
                observed_at=signal.observed_at,
                status=signal.status.value,
                possible_need=signal.possible_need,
                account_id=(
                    str(signal.account_id) if signal.account_id is not None else None
                ),
                discard_reason=signal.discard_reason,
                source_type=signal.provenance.source_type.value,
                source_id=signal.provenance.source_id,
                extracted_by=signal.provenance.extracted_by,
                extracted_at=signal.provenance.extracted_at,
                confirmed_by=signal.provenance.confirmed_by,
                confirmed_at=signal.provenance.confirmed_at,
                source_url=signal.provenance.source_url,
                page_hash=signal.provenance.page_hash,
            )
            .on_conflict_do_nothing(constraint="uq_demand_signals_source_identity")
        )
        return cast(CursorResult, result).rowcount > 0

    async def get(
        self, tenant_id: TenantId, signal_id: DemandSignalId
    ) -> DemandSignal | None:
        self._require_tenant(tenant_id, "demand_signal_get")
        row = (
            await self._session.execute(
                select(DemandSignalRow).where(
                    DemandSignalRow.tenant_id == str(self._tenant_id),
                    DemandSignalRow.signal_id == str(signal_id),
                )
            )
        ).scalar_one_or_none()
        return _row_to_signal(row) if row is not None else None

    async def find_duplicate(
        self,
        tenant_id: TenantId,
        entity_name: str,
        signal_type: str,
        source_type: str,
        source_id: str,
    ) -> DemandSignal | None:
        self._require_tenant(tenant_id, "demand_signal_find_duplicate")
        row = (
            await self._session.execute(
                select(DemandSignalRow).where(
                    DemandSignalRow.tenant_id == str(self._tenant_id),
                    DemandSignalRow.entity_name == entity_name,
                    DemandSignalRow.signal_type == signal_type,
                    DemandSignalRow.source_type == source_type,
                    DemandSignalRow.source_id == source_id,
                )
            )
        ).scalar_one_or_none()
        return _row_to_signal(row) if row is not None else None

    async def list_unlinked(
        self, tenant_id: TenantId, limit: int
    ) -> list[DemandSignal]:
        """本切片不实现：无假设消费者，不发明排序/limit 语义（规格 §6.1）。"""
        raise NotImplementedError
```

(3b) `infra/db/demand_uow.py`（新文件）：

```python
"""demand 域 UoW：信号仓储 + 事件总线（同事务）。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.outbox import PostgresEventBus
from infra.db.repositories.demand import DemandSignalRepositoryImpl
from shared.schemas.identifiers import TenantId


class SqlAlchemyDemandUnitOfWork:
    """每次进入创建新 session，退出时统一提交、回滚与关闭。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._factory = session_factory
        self._tenant_id = tenant_id
        self._now = now

    async def __aenter__(self) -> Self:
        session = self._factory()
        self._session = session
        self.signals = DemandSignalRepositoryImpl(session, self._tenant_id)
        self.bus = PostgresEventBus(session, self._tenant_id, now=self._now)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        session = self._session
        try:
            if exc_type is None:
                await session.commit()
            else:
                await session.rollback()
        finally:
            await session.close()
```

(3c) `domains/demand/service_impl.py`（新文件，capture；discard 在 Task 4 追加）：

```python
"""需求域服务实现（浅域：2026-08-17 计划 Task 3/4 只实现 capture/discard）。

捕获语义（规格 §5/§7）：输入校验全部在开 UoW 前完成；所有 str 输入
item == item.strip()；WEB_PAGE 强约束（url+hash 非空且 source_id ==
page_hash）；去重 key 全非空 5 列；重复返回既有 ID 不重复发事件；
业务插入 + outbox 同事务。事件只用共享契约 DemandSignalCaptured
（metadata-only，不含 raw_observation/possible_need/provenance）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from domains.demand.errors import MissingWebEvidenceError
from domains.demand.models import DemandSignal, SignalStatus, SignalType
from domains.demand.repository import DemandUnitOfWork
from domains.demand.schemas import SignalCaptureRequest
from shared.errors import ValidationError
from shared.events.catalog import DemandSignalCaptured
from shared.schemas.identifiers import DemandSignalId, TenantId, new_id
from shared.schemas.provenance import Provenance, SourceType


class DemandServiceImpl:
    """``DemandService`` 的 Postgres 实现（浅域子集）。"""

    def __init__(
        self,
        uow_factory: Callable[[TenantId], DemandUnitOfWork],
        *,
        now: Callable[[], datetime],
    ) -> None:
        if not callable(uow_factory) or not callable(now):
            raise ValidationError("需求服务依赖无效")
        self._uow_factory = uow_factory
        self._now = now

    @staticmethod
    def _validate_now(value: datetime) -> datetime:
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != UTC.utcoffset(value)
        ):
            raise ValidationError("服务时钟必须为 UTC")
        return value

    @staticmethod
    def _require_text(
        value: object,
        label: str,
        *,
        max_len: int | None = None,
        can_be_none: bool = False,
    ) -> str | None:
        if can_be_none and value is None:
            return None
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
        ):
            raise ValidationError(f"{label}无效")
        if max_len is not None and len(value) > max_len:
            raise ValidationError(f"{label}超长")
        return value

    async def capture_signal(
        self, tenant_id: TenantId, request: SignalCaptureRequest
    ) -> str:
        """记录一条需求信号（契约见 service.py docstring + 规格 §5/§7）。"""
        if (
            not isinstance(tenant_id, str)
            or not tenant_id.strip()
            or tenant_id != tenant_id.strip()
        ):
            raise ValidationError("信号租户无效")
        if len(tenant_id) > 32:
            raise ValidationError("信号租户超长")
        entity_name = self._require_text(request.entity_name, "信号企业名", max_len=200)
        raw_observation = self._require_text(request.raw_observation, "信号观察内容")
        source_id = self._require_text(request.source_id, "信号来源", max_len=200)
        extracted_by = self._require_text(request.extracted_by, "信号提取者", max_len=64)
        possible_need = self._require_text(
            request.possible_need, "信号可能需求", can_be_none=True
        )
        source_url = self._require_text(
            request.source_url, "信号来源 URL", max_len=2000, can_be_none=True
        )
        page_hash = self._require_text(
            request.page_hash, "信号页面哈希", max_len=200, can_be_none=True
        )
        try:
            signal_type = SignalType(request.signal_type)
        except ValueError:
            raise ValidationError("信号类型无效")
        try:
            source_type = SourceType(request.source_type)
        except ValueError:
            raise ValidationError("信号来源类型无效")
        observed_at = request.observed_at
        if (
            not isinstance(observed_at, datetime)
            or observed_at.tzinfo is None
            or observed_at.utcoffset() != UTC.utcoffset(observed_at)
        ):
            raise ValidationError("信号观察时间必须为 UTC")
        if source_type is SourceType.WEB_PAGE and (
            not source_url or not page_hash or source_id != page_hash
        ):
            raise MissingWebEvidenceError(
                "网页来源信号缺少 URL/page_hash 或 source_id 与 page_hash 不一致"
            )
        now = self._validate_now(self._now())
        signal = DemandSignal(
            signal_id=DemandSignalId(new_id("sig")),
            tenant_id=tenant_id,
            signal_type=signal_type,
            entity_name=entity_name,
            raw_observation=raw_observation,
            observed_at=observed_at,
            status=SignalStatus.CAPTURED,
            possible_need=possible_need,
            provenance=Provenance(
                source_type=source_type,
                source_id=source_id,
                extracted_by=extracted_by,
                extracted_at=now,
                confirmed_by=None,
                confirmed_at=None,
                source_url=source_url,
                page_hash=page_hash,
            ),
        )
        async with self._uow_factory(tenant_id) as uow:
            inserted = await uow.signals.add(signal)
            if inserted:
                await uow.bus.publish(
                    DemandSignalCaptured(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        run_id=None,
                        signal_id=signal.signal_id,
                        entity_name=signal.entity_name,
                        signal_type=signal.signal_type.value,
                        source_url=signal.provenance.source_url,
                    )
                )
                return str(signal.signal_id)
            winner = await uow.signals.find_duplicate(
                tenant_id,
                entity_name,
                signal_type.value,
                source_type.value,
                source_id,
            )
            if winner is None:
                raise ValidationError("信号写入竞态异常")
            return str(winner.signal_id)
```

- [ ] **Step 4: 运行确认 GREEN**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
python -m pytest tests/integration/test_demand_signals.py -q -W error
```

Expected: capture 测试全 PASS（Task 4 的 discard 测试尚未写入，此文件当前只含 Task 3 测试）。rc=0。

- [ ] **Step 5: 边界与静态检查**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
python -m ruff check infra/db/repositories/demand.py infra/db/demand_uow.py domains/demand/service_impl.py tests/integration/test_demand_signals.py
python -m mypy domains/demand infra/db
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
```

Expected: 全部 rc=0（若 ruff 对 `# type: ignore` 或长行有意见，按 ruff 提示最小修正后重跑）。

- [ ] **Step 6: 停止等待监督方复审**（汇报 RED 原因 ModuleNotFoundError、GREEN 计数 12、diff 4 文件）

复审通过后，**在提交前**按规格 §9.3 执行以下三项 mutation proof（每项：临时 apply_patch → 运行精确测试确认 RED → 立即恢复 → 运行精确测试确认 GREEN → `git diff` 确认无残留）：

| # | 临时变异 | 精确测试 | 预期 RED |
|---|---|---|---|
| M1 | `infra/db/repositories/demand.py` 的 `add` 去掉 `.on_conflict_do_nothing(...)`（普通 INSERT） | `test_capture_concurrent_same_key_exactly_one_row_one_event` | `asyncio.gather` 结果含 `IntegrityError`（唯一约束冲突），`all(isinstance(r, str))` 失败 |
| M2 | `add` 的冲突目标改错：`.on_conflict_do_nothing(index_elements=["tenant_id", "signal_id"])` | `test_capture_concurrent_same_key_exactly_one_row_one_event` | ON CONFLICT 只覆盖 PK（tenant_id, signal_id），两并发插入的 signal_id 不同故不冲突 → 第二次插入命中来源身份 UNIQUE → 未处理 `IntegrityError`，`asyncio.gather` 结果含异常（不是两行） |
| M5 | `service_impl.capture_signal` 把 `uow.bus.publish(...)` 移到 `if inserted:` 之外（重复时也发布） | `test_capture_duplicate_replay_returns_first_id_single_event` | outbox 事件数为 2（断言 1 事件失败） |

- [ ] **Step 7: 复审通过后提交/推送/exact-HEAD CI**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
git add --chmod=-x infra/db/repositories/demand.py infra/db/demand_uow.py \
  domains/demand/service_impl.py tests/integration/test_demand_signals.py
git ls-files --stage infra/db/repositories/demand.py infra/db/demand_uow.py domains/demand/service_impl.py tests/integration/test_demand_signals.py
git diff --cached --check
git diff --cached --name-only
git commit -m "feat(demand): capture demand signals with dedup and atomic outbox"
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

Expected: 4 文件 index mode 全部 `100644`；push 后 local == origin；CI 输出 `OK`。

---

### Task 4: discard 状态机（repo.discard + service.discard_signal）

**Files:**
- Modify: `infra/db/repositories/demand.py`（追加 `discard` 方法）
- Modify: `domains/demand/service_impl.py`（追加 `discard_signal`）
- Modify: `tests/integration/test_demand_signals.py`（追加 discard 测试 13-18）

**Interfaces:**
- Consumes: Task 2 `DemandSignalRepository.discard(tenant_id, signal_id, reason) -> DemandSignal | None`（转换前快照）；Task 3 全部实现。
- Produces: `DemandServiceImpl.discard_signal(tenant_id, signal_id: str, reason: str) -> None`（规格 §8 四分支：不存在/不可见→"需求信号不存在"；LINKED→InvalidStateTransition；DISCARDED 同 reason 幂等/不同 reason 冲突；CAPTURED→成功）。

- [ ] **Step 1: 写失败测试（追加到 test_demand_signals.py）**

```python
async def test_discard_captured_to_discarded_persists_first_reason(
    demand_db: AsyncEngine,
) -> None:
    """captured→discarded：落 first reason；status/reason 往返。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    signal_id = await service.capture_signal(tenant, _request())
    await service.discard_signal(tenant, signal_id, "noise")
    rows = await _signal_rows(factory, tenant)
    assert len(rows) == 1
    assert rows[0].status == "discarded"
    assert rows[0].discard_reason == "noise"


async def test_discard_idempotent_same_reason_and_conflict_on_different(
    demand_db: AsyncEngine,
) -> None:
    """同 reason 幂等 no-op；不同 reason → InvalidStateTransition 且 first 保留。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    signal_id = await service.capture_signal(tenant, _request())
    await service.discard_signal(tenant, signal_id, "noise")
    await service.discard_signal(tenant, signal_id, "noise")  # 幂等
    rows = await _signal_rows(factory, tenant)
    assert len(rows) == 1 and rows[0].discard_reason == "noise"
    with pytest.raises(InvalidStateTransition) as exc_info:
        await service.discard_signal(tenant, signal_id, "duplicate")
    assert str(exc_info.value) == "丢弃原因冲突，拒绝覆盖"
    rows = await _signal_rows(factory, tenant)
    assert rows[0].discard_reason == "noise"  # first reason 保留


async def test_discard_linked_rejected(demand_db: AsyncEngine) -> None:
    """LINKED_TO_HYPOTHESIS → InvalidStateTransition（保护证据链）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    tables = importlib.import_module("infra.db.tables")
    models = _models
    # 直插 linked 行（模拟假设已关联）
    signal_id = new_id("sig")
    async with factory() as session:
        session.add(
            tables.DemandSignalRow(
                tenant_id=str(tenant),
                signal_id=signal_id,
                signal_type="product_line_expansion",
                entity_name="Acme Manufacturing",
                raw_observation=OBSERVATION_MARKER,
                observed_at=NOW,
                status="linked_to_hypothesis",
                source_type="web_page",
                source_id="sha256:pagehash001",
                extracted_by="model-v1",
                extracted_at=NOW,
                source_url="https://example.com/acme",
                page_hash="sha256:pagehash001",
            )
        )
        await session.commit()
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    with pytest.raises(InvalidStateTransition) as exc_info:
        await service.discard_signal(tenant, signal_id, "noise")
    assert str(exc_info.value) == "已关联假设的信号不可丢弃"
    rows = await _signal_rows(factory, tenant)
    assert rows[0].status == "linked_to_hypothesis"  # 未改动


async def test_discard_missing_or_cross_tenant_invisible(
    demand_db: AsyncEngine,
) -> None:
    """不存在/跨租户不可见 → 统一 ValidationError("需求信号不存在")。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant_a = TenantId(new_id("tn"))
    tenant_b = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service_a = _service(factory, tenant_a, clock)
    service_b = _service(factory, tenant_b, clock)

    ghost = new_id("sig")
    with pytest.raises(ValidationError) as exc_info:
        await service_a.discard_signal(tenant_a, ghost, "noise")
    assert str(exc_info.value) == "需求信号不存在"

    signal_id = await service_a.capture_signal(tenant_a, _request())
    with pytest.raises(ValidationError) as exc_info:
        await service_b.discard_signal(tenant_b, signal_id, "noise")
    assert str(exc_info.value) == "需求信号不存在"
    rows_a = await _signal_rows(factory, tenant_a)
    assert rows_a[0].status == "captured"  # B 的操作无副作用


async def test_discard_concurrent_same_reason_both_succeed_single_reason(
    demand_db: AsyncEngine,
) -> None:
    """并发同 reason → 双成功且单一 reason 落库。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service_a = _service(factory, tenant, clock)
    service_b = _service(factory, tenant, clock)

    signal_id = await service_a.capture_signal(tenant, _request())
    results = await asyncio.gather(
        service_a.discard_signal(tenant, signal_id, "noise"),
        service_b.discard_signal(tenant, signal_id, "noise"),
        return_exceptions=True,
    )
    assert all(r is None for r in results), results
    rows = await _signal_rows(factory, tenant)
    assert len(rows) == 1
    assert rows[0].status == "discarded" and rows[0].discard_reason == "noise"


async def test_discard_concurrent_different_reasons_one_wins_one_conflict(
    demand_db: AsyncEngine,
) -> None:
    """并发不同 reason → 一胜一 InvalidStateTransition，first reason 保留。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service_a = _service(factory, tenant, clock)
    service_b = _service(factory, tenant, clock)

    signal_id = await service_a.capture_signal(tenant, _request())
    results = await asyncio.gather(
        service_a.discard_signal(tenant, signal_id, "noise"),
        service_b.discard_signal(tenant, signal_id, "duplicate"),
        return_exceptions=True,
    )
    assert [r for r in results if r is not None]  # 恰一方冲突
    assert any(isinstance(r, InvalidStateTransition) for r in results)
    rows = await _signal_rows(factory, tenant)
    assert rows[0].status == "discarded"
    assert rows[0].discard_reason in {"noise", "duplicate"}


async def test_discard_snapshot_semantics_and_rollback(demand_db: AsyncEngine) -> None:
    """repo 返回转换前快照；失败注入 → 整事务回滚（行保持 captured 无 reason）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    uow_type = importlib.import_module("infra.db.demand_uow").SqlAlchemyDemandUnitOfWork

    # 快照语义：repo 直接调用，captured 行返回快照 status=captured，但 DB 已更新
    signal = _models.DemandSignal(
        signal_id=_models.DemandSignalId(new_id("sig")),
        tenant_id=tenant,
        signal_type=_models.SignalType.PRODUCT_LINE_EXPANSION,
        entity_name="Acme Manufacturing",
        raw_observation=OBSERVATION_MARKER,
        observed_at=NOW,
        provenance=_provenance_for(None),
    )
    async with uow_type(factory, tenant, now=clock.now) as uow:
        assert await uow.signals.add(signal) is True
    async with uow_type(factory, tenant, now=clock.now) as uow:
        snapshot = await uow.signals.discard(tenant, signal.signal_id, "noise")
        assert snapshot is not None
        assert snapshot.status is _models.SignalStatus.CAPTURED  # 转换前快照
        rows = await _signal_rows(factory, tenant)
        assert rows[0].status == "discarded"  # DB 已是 discarded

    # 回滚：事务内失败 → 行保持 captured 且无 reason
    async with uow_type(factory, tenant, now=clock.now) as uow:
        await uow.signals.discard(tenant, signal.signal_id, "noise2")
        raise RuntimeError("boom")
    rows = await _signal_rows(factory, tenant)
    assert rows[0].status == "discarded" and rows[0].discard_reason == "noise"
```

- [ ] **Step 2: 运行确认 RED**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
python -m pytest tests/integration/test_demand_signals.py -k discard -q -W error
```

Expected: **FAIL**——`AttributeError: 'DemandServiceImpl' object has no attribute 'discard_signal'`（缺实现）。记录 rc=1。

- [ ] **Step 3: 最小实现**

(3a) `infra/db/repositories/demand.py`——在 `find_duplicate` 之后、`list_unlinked` 之前追加：

```python
    async def discard(
        self,
        tenant_id: TenantId,
        signal_id: DemandSignalId,
        reason: str,
    ) -> DemandSignal | None:
        """tenant-bound SELECT ... FOR UPDATE，返回转换前快照（规格 §6.1）。

        不存在 → None；CAPTURED → 先捕获 snapshot 再同事务 UPDATE 为
        discarded+reason，返回 snapshot（调用方不得当 DB 当前态）；
        DISCARDED / LINKED_TO_HYPOTHESIS → 不改动，返回当前 snapshot。
        """
        self._require_tenant(tenant_id, "demand_signal_discard")
        row = (
            await self._session.execute(
                select(DemandSignalRow)
                .where(
                    DemandSignalRow.tenant_id == str(self._tenant_id),
                    DemandSignalRow.signal_id == str(signal_id),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        snapshot = _row_to_signal(row)
        if row.status == SignalStatus.CAPTURED.value:
            row.status = SignalStatus.DISCARDED.value
            row.discard_reason = reason
            await self._session.flush()
        return snapshot
```

(3b) `domains/demand/service_impl.py`——`capture_signal` 之后追加（imports 增 `InvalidStateTransition`、`DemandSignalId` 已导入）：

```python
    async def discard_signal(
        self, tenant_id: TenantId, signal_id: str, reason: str
    ) -> None:
        """丢弃信号（契约见 service.py docstring + 规格 §8）。

        service 只用 repo 返回的转换前快照判定：None=不存在/跨租户不可见；
        LINKED=拒绝；DISCARDED 同 reason=幂等 no-op、不同 reason=冲突；
        CAPTURED=本次完成转换（DB 已更新）。
        """
        if (
            not isinstance(tenant_id, str)
            or not tenant_id.strip()
            or tenant_id != tenant_id.strip()
        ):
            raise ValidationError("信号租户无效")
        if len(tenant_id) > 32:
            raise ValidationError("信号租户超长")
        if (
            not isinstance(signal_id, str)
            or not signal_id.strip()
            or signal_id != signal_id.strip()
        ):
            raise ValidationError("信号标识无效")
        if len(signal_id) > 32:
            raise ValidationError("信号标识超长")
        reason = self._require_text(reason, "丢弃原因")
        async with self._uow_factory(tenant_id) as uow:
            snapshot = await uow.signals.discard(
                tenant_id, DemandSignalId(signal_id), reason
            )
            if snapshot is None:
                raise ValidationError("需求信号不存在")
            if snapshot.status is SignalStatus.LINKED_TO_HYPOTHESIS:
                raise InvalidStateTransition("已关联假设的信号不可丢弃")
            if snapshot.status is SignalStatus.DISCARDED:
                if snapshot.discard_reason == reason:
                    return
                raise InvalidStateTransition("丢弃原因冲突，拒绝覆盖")
```

（service_impl imports 增 `from shared.errors import InvalidStateTransition, ValidationError`。）

- [ ] **Step 4: 运行确认 GREEN**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
python -m pytest tests/integration/test_demand_signals.py -q -W error
```

Expected: 全部 capture + discard 测试 PASS（18 项）。rc=0。

- [ ] **Step 5: 边界与静态检查 + 定向回归**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
python -m pytest tests/unit/test_demand_signal_contracts.py -q -W error
python -m pytest tests/unit -q -W error
python -m pytest tests/integration/test_conversations_suggest.py \
  tests/integration/test_conversations_correction.py \
  tests/integration/test_conversations_classification.py -q -W error
python -m ruff check domains/demand infra/db tests/integration/test_demand_signals.py tests/unit/test_demand_signal_contracts.py
python -m mypy domains/demand infra/db
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
git diff --check
```

Expected: 全部 rc=0。

- [ ] **Step 6: 停止等待监督方复审**（汇报 RED 原因 AttributeError、GREEN 计数、diff 3 文件）

复审通过后，**在提交前**按规格 §9.3 执行以下两项 mutation proof（每项：临时 apply_patch → 运行精确测试确认 RED → 立即恢复 → 运行精确测试确认 GREEN → `git diff` 确认无残留）：

| # | 临时变异 | 精确测试 | 预期 RED |
|---|---|---|---|
| M3 | `service_impl.discard_signal` 删除「DISCARDED 且 reason 不同 → 抛 InvalidStateTransition」分支（改为无条件返回） | `test_discard_idempotent_same_reason_and_conflict_on_different` | 不同 reason 不再抛错 → `pytest.raises(InvalidStateTransition)` 失败 |
| M4 | `infra/db/repositories/demand.py` 的 `discard` 去掉 `.with_for_update()` | `test_discard_concurrent_different_reasons_one_wins_one_conflict` | 双事务都读到 captured 并各自 UPDATE → 两结果皆非异常，「恰一方冲突」断言失败 |

- [ ] **Step 7: 复审通过后提交/推送/exact-HEAD CI**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
git add --chmod=-x infra/db/repositories/demand.py domains/demand/service_impl.py \
  tests/integration/test_demand_signals.py
git ls-files --stage infra/db/repositories/demand.py domains/demand/service_impl.py tests/integration/test_demand_signals.py
git diff --cached --check
git diff --cached --name-only
git commit -m "feat(demand): discard demand signals with snapshot semantics"
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

Expected: 3 文件 index mode 全部 `100644`；push 后 local == origin；CI 输出 `OK`。

---

### Task 5: 最终完整门禁（交付；无代码/文档变更）

**Files:** 无（本计划文件已在 Plan Delivery Gate 单独 docs commit；本 Task 只做验证，不再产生提交）。

- [ ] **Step 1: 最终完整门禁（全部任务完成后、交付前）**

```bash
export PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/ChatGPT.app/Contents/Resources:$PATH
find . -name "._*" -not -path "./.git/*" -delete
python -m pytest tests/integration/test_demand_signals.py tests/integration/test_migrations.py -q -W error
python -m pytest -q -W error -m "not e2e"
ruff check .
mypy domains shared tool_gateway apps workflows notification_gateway infra
python3 scripts/check_boundaries.py
python3 scripts/scan_sensitive.py
(
  set -e
  cd apps/web
  npm run gen:api
  git diff --exit-code -- src/api/api.d.ts
  npm run typecheck
  npm run lint
  npm run test
  npm run build
)
git diff --check
TRADEOS_REQUIRE_E2E=1 python -m pytest tests/e2e -q -W error
```

Expected: 全部 rc=0。web 命令块不使用输出截断 pipeline（直接运行，无需 tail）；若确需任何 pipeline，显式 `bash -o pipefail` 并检查首个命令 rc。

- [ ] **Step 2: 停止等待监督方最终复审**（若复审要求修复，按最小修复回到对应任务 RED→GREEN 后重跑门禁；本 Task 不产生 commit/push）

---

## Self-Review（计划自检）

**1. Spec coverage（规格 §1-§14 → 任务）：**
- §2 纠偏（DTO 扩展）→ Task 2；§3 Provenance 缺口 → Task 2/3；§4 去重 key 纠偏 → Task 1（UNIQUE）/Task 3（find_duplicate/ON CONFLICT）；§5 单表 18 列 + 8 CHECK → Task 1；§6 Repository/UoW/service 组织 + list_unlinked 桩 → Task 2/3；§7 capture 校验 → Task 3；§8 discard 快照语义 → Task 4；§9 测试与原子性（迁移 parity 全 8 CHECK、集成 15 项、mutation proofs、门禁纪律）→ Task 1/3/4/5；§10 文件地图 12 路径 → 全部任务覆盖；§12 明确不做 → 无任务触碰；§13 九条硬边界 → Global Constraints + 各任务门禁。
- mutation proofs（规格 §9.3 五项：M1 ON CONFLICT 去掉、M2 冲突目标改错、M3 discard 覆盖 first reason、M4 FOR UPDATE 去掉、M5 事件在重复时发布）→ **已预写**在 Task 3 Step 6（M1/M2/M5）与 Task 4 Step 6（M3/M4）的精确测试映射表；每项在复审通过后、提交前执行（临时 apply_patch → 精确测试 RED → 恢复 → 精确测试 GREEN → `git diff` 无残留）。

**2. Placeholder scan：** 无 TBD/TODO/「implement later」；每个代码步骤含实际代码；无「类似 Task N」引用（Task 3/4 的代码各自完整给出）。

**3. Type consistency：** `DemandSignalRepository.add(signal: DemandSignal) -> bool`、`find_duplicate(tenant_id, entity_name: str, signal_type: str, source_type: str, source_id: str)`、`discard(tenant_id, signal_id: DemandSignalId, reason: str) -> DemandSignal | None`、`DemandUnitOfWork.signals/bus`、`DemandServiceImpl(uow_factory, *, now)`、`capture_signal -> str`、`discard_signal -> None` 在 Task 2/3/4 的接口块与代码块中逐字一致；`DemandSignalRow` 18 列名在 Task 1 表格、tables.py 代码、迁移代码、集成测试 `base()` 字典、`_signal_to_row`/`_row_to_signal` 中一致；8 个 CHECK 名称在 Task 1 契约测试与两处实现中一致。
