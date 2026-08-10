"""S2-3 迁移集成测试（RED 第五批）：schema 结构 + 触发器 + 约束 + FK 语义。

行为断言，不依赖实现细节：
- 六表存在且各含 ``tenant_id`` 列（硬边界 8 的 schema 落地）。
- roundtrip：独立 ``alembic downgrade base`` → 六表消失 → 再 ``upgrade head``
  恢复；无论成败 finally 都兜底恢复 head，不依赖测试执行顺序。
- ``(tenant_id, need_id)`` 唯一：异租户同 need_id 允许、同租户重复被拒。
- 三张审计表（score_snapshots/loss_records/provenance_records）UPDATE/DELETE
  均被 DB 触发器拒绝（只增）。
- outbox_events：status→delivered + delivered_at 更新成功；attempt=0 / 非法
  status 被 CHECK 拒绝；event_type/event_payload 等非白名单字段更新被拒。
- 金额成对（amount/currency）：opportunities 三组金额与 score_snapshots
  estimated_value 只给 amount 缺 currency → 均被 CHECK 拒绝（Decimal，非 float）。
- closed 约束：lost 缺闭环字段 / won 缺确认字段 / 非终态带 closed → 均被拒。
- loss_records：缺 confirmed_by / 缺 confirmed_at 各被 NOT NULL 拒绝。
- 复合 FK (tenant_id, opportunity_id)：跨租户 handoff/loss 引用被拒、同租户
  合法、DELETE parent 被 ON DELETE RESTRICT 拒绝。
- score_snapshots.opportunity_id 无 FK：不存在的机会 ID 可插入（失败门槛快照保留）。

RED 前置：``0002_opportunities`` 尚未创建，六表缺失 → 断言失败
（行为失败，非收集错误）。全部用本地引擎（``create_engine_from`` + try/finally
dispose），不触发 session 级 async fixture 的 teardown 事件循环问题。
时间用 SQL ``now()``；JSONB 参数走 ``CAST(:p AS jsonb)``，禁止值拼接。
禁止打印/记录任何连接串（``db_url`` 经 conftest 的 RedactedUrl 脱敏 repr）。
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    TextClause,
    UniqueConstraint,
    inspect,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

_REPO_ROOT = Path(__file__).resolve().parents[2]

# 六表（Schema 附录）：opportunities / score_snapshots / handoffs /
# loss_records / provenance_records / outbox_events。
EXPECTED_TABLES: tuple[str, ...] = (
    "opportunities",
    "score_snapshots",
    "handoffs",
    "loss_records",
    "provenance_records",
    "outbox_events",
)

SENDING_IDENTITY_TABLES: tuple[str, ...] = (
    "sending_domains",
    "sending_identities",
    "sending_auth_checks",
    "sending_reputation_events",
    "sending_daily_counters",
    "sending_send_reservations",
    "sending_identity_actions",
)

# 固定注入时钟（closed_at 绑定值；非业务逻辑数字）。
_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


def _run_alembic(db_url: str, *command: str) -> None:
    """在仓库根运行 ``alembic <command>``；仅经 env 注入 DATABASE_URL。

    捕获输出不打印；断言消息不含连接内容（DSN/密码一律不外泄）。
    """
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        ["alembic", *command],
        capture_output=True,
        check=False,
        env=env,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, f"alembic {' '.join(command)} 失败（不输出连接内容）"


def _sync_table_names(conn: Connection) -> list[str]:
    """同步 inspect：当前 schema 的表名列表。"""
    return inspect(conn).get_table_names()


def _sync_columns(conn: Connection, table: str) -> set[str]:
    """同步 inspect：指定表的列名集合。"""
    return {str(col["name"]) for col in inspect(conn).get_columns(table)}


async def _table_names(engine: AsyncEngine) -> set[str]:
    """经 ``engine.connect().run_sync`` 取当前 schema 的表名集合。"""
    async with engine.connect() as conn:
        return set(await conn.run_sync(_sync_table_names))


async def _columns(engine: AsyncEngine, table: str) -> set[str]:
    """经 ``engine.connect().run_sync`` 取指定表的列名集合。"""
    async with engine.connect() as conn:
        return await conn.run_sync(_sync_columns, table)


_INSERT_OPPORTUNITY = text(
    "INSERT INTO opportunities "
    "(opportunity_id, tenant_id, account_id, account_name, country, need_id, "
    "product_category) "
    "VALUES (:opportunity_id, :tenant_id, :account_id, :account_name, :country, "
    ":need_id, :product_category)"
)


def _opportunity_params(opportunity_id: str, tenant_id: str, need_id: str) -> dict[str, str]:
    """机会行必填列参数（Schema 附录；state/created_at 走默认值）。"""
    return {
        "opportunity_id": opportunity_id,
        "tenant_id": tenant_id,
        "account_id": "acc-1",
        "account_name": "Acme",
        "country": "US",
        "need_id": need_id,
        "product_category": "hinges",
    }


_INSERT_SCORE_SNAPSHOT = text(
    "INSERT INTO score_snapshots ("
    "snapshot_id, tenant_id, opportunity_id, scored_at, scorer_version, "
    "passed_gates, failed_gates, sort_evidence_rank, sort_value_band, "
    "sort_supply_rank, gate_reasons, rank_bucket) "
    "VALUES (:snapshot_id, :tenant_id, :opportunity_id, now(), :scorer_version, "
    "CAST(:passed_gates AS jsonb), CAST(:failed_gates AS jsonb), "
    ":sort_evidence_rank, :sort_value_band, :sort_supply_rank, "
    "CAST(:gate_reasons AS jsonb), :rank_bucket)"
)

_INSERT_LOSS_RECORD = text(
    "INSERT INTO loss_records ("
    "loss_record_id, tenant_id, opportunity_id, loss_reason, died_at_state, "
    "detail, confirmed_by, confirmed_at, recorded_at) "
    "VALUES (:loss_record_id, :tenant_id, :opportunity_id, :loss_reason, "
    ":died_at_state, :detail, :confirmed_by, now(), now())"
)

_INSERT_PROVENANCE = text(
    "INSERT INTO provenance_records ("
    "provenance_id, tenant_id, entity_type, entity_id, field_name, "
    "source_type, source_id, extracted_by, extracted_at) "
    "VALUES (:provenance_id, :tenant_id, :entity_type, :entity_id, :field_name, "
    ":source_type, :source_id, :extracted_by, now())"
)

_INSERT_OUTBOX = text(
    "INSERT INTO outbox_events ("
    "event_id, tenant_id, event_type, event_payload, attempt, published_at, "
    "trace_id, run_id, occurred_at, status) "
    "VALUES (:event_id, :tenant_id, :event_type, CAST(:event_payload AS jsonb), "
    ":attempt, now(), :trace_id, :run_id, now(), :status)"
)


def _snapshot_params(
    snapshot_id: str,
    tenant_id: str,
    opportunity_id: str,
) -> dict[str, object]:
    """score_snapshots 最小合法行参数（时间用 now()，JSONB 走 CAST）。"""
    return {
        "snapshot_id": snapshot_id,
        "tenant_id": tenant_id,
        "opportunity_id": opportunity_id,
        "scorer_version": "gates-v1",
        "passed_gates": json.dumps(["contactable"]),
        "failed_gates": json.dumps([]),
        "sort_evidence_rank": 1,
        "sort_value_band": 0,
        "sort_supply_rank": 0,
        "gate_reasons": json.dumps({}),
        "rank_bucket": "high",
    }


def _loss_params(
    loss_record_id: str,
    tenant_id: str,
    opportunity_id: str,
) -> dict[str, object]:
    """loss_records 最小合法行参数（confirmed_at/recorded_at 用 now()）。"""
    return {
        "loss_record_id": loss_record_id,
        "tenant_id": tenant_id,
        "opportunity_id": opportunity_id,
        "loss_reason": "price_too_high",
        "died_at_state": "quoted",
        "detail": "客户转向竞争对手",
        "confirmed_by": "emp-1",
    }


_INSERT_LOSS_RECORD_FULL = text(
    "INSERT INTO loss_records ("
    "loss_record_id, tenant_id, opportunity_id, loss_reason, died_at_state, "
    "detail, confirmed_by, confirmed_at, recorded_at) "
    "VALUES (:loss_record_id, :tenant_id, :opportunity_id, :loss_reason, "
    ":died_at_state, :detail, :confirmed_by, :confirmed_at, now())"
)


def _loss_full_params(
    loss_record_id: str,
    tenant_id: str,
    opportunity_id: str,
    *,
    confirmed_by: str | None = "emp-1",
    confirmed_at: datetime | None = _NOW,
) -> dict[str, object]:
    """loss_records 全字段参数（confirmed_by/confirmed_at 可置空测 NOT NULL）。"""
    return {
        "loss_record_id": loss_record_id,
        "tenant_id": tenant_id,
        "opportunity_id": opportunity_id,
        "loss_reason": "price_too_high",
        "died_at_state": "quoted",
        "detail": "客户转向竞争对手",
        "confirmed_by": confirmed_by,
        "confirmed_at": confirmed_at,
    }


_INSERT_HANDOFF = text(
    "INSERT INTO handoffs ("
    "handoff_id, tenant_id, opportunity_id, trigger, state, requested_at, "
    "account_name, country, why_valuable, customer_verbatim) "
    "VALUES (:handoff_id, :tenant_id, :opportunity_id, :trigger, 'requested', "
    "now(), :account_name, :country, :why_valuable, :customer_verbatim)"
)


def _handoff_params(
    handoff_id: str,
    tenant_id: str,
    opportunity_id: str,
) -> dict[str, object]:
    """handoffs 最小合法行参数（state 走 'requested'，requested_at 用 now()）。"""
    return {
        "handoff_id": handoff_id,
        "tenant_id": tenant_id,
        "opportunity_id": opportunity_id,
        "trigger": "quote_requested",
        "account_name": "Acme",
        "country": "US",
        "why_valuable": "正在扩建第二座工厂",
        "customer_verbatim": "we need hinges",
    }


def _prov_params(
    provenance_id: str,
    tenant_id: str,
    entity_type: str,
    entity_id: str,
) -> dict[str, object]:
    """provenance_records 最小合法行参数（多态 entity；extracted_at 用 now()）。"""
    return {
        "provenance_id": provenance_id,
        "tenant_id": tenant_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "field_name": "account_name",
        "source_type": "conversation",
        "source_id": "msg-1",
        "extracted_by": "model-v3",
    }


def _outbox_params(
    event_id: str,
    tenant_id: str,
    *,
    attempt: int = 1,
    status: str = "pending",
) -> dict[str, object]:
    """outbox_events 最小合法行参数（published_at/occurred_at 用 now()）。"""
    return {
        "event_id": event_id,
        "tenant_id": tenant_id,
        "event_type": "OpportunityWon",
        "event_payload": json.dumps({"opportunity_id": "opp-1", "closed_by": "emp-1"}),
        "attempt": attempt,
        "trace_id": f"trc-{event_id}",
        "run_id": f"run-{event_id}",
        "status": status,
    }


_INSERT_OPPORTUNITY_WITH_MONEY = text(
    "INSERT INTO opportunities ("
    "opportunity_id, tenant_id, account_id, account_name, country, need_id, "
    "product_category, state, closed_by, closed_at, "
    "target_price_amount, target_price_currency, "
    "estimated_cost_amount, estimated_cost_currency, "
    "estimated_profit_amount, estimated_profit_currency) "
    "VALUES (:opportunity_id, :tenant_id, :account_id, :account_name, :country, "
    ":need_id, :product_category, :state, :closed_by, :closed_at, "
    ":target_price_amount, :target_price_currency, "
    ":estimated_cost_amount, :estimated_cost_currency, "
    ":estimated_profit_amount, :estimated_profit_currency)"
)

_INSERT_SNAPSHOT_WITH_VALUE = text(
    "INSERT INTO score_snapshots ("
    "snapshot_id, tenant_id, opportunity_id, scored_at, scorer_version, "
    "passed_gates, failed_gates, sort_evidence_rank, sort_value_band, "
    "sort_supply_rank, gate_reasons, rank_bucket, "
    "estimated_value_amount, estimated_value_currency) "
    "VALUES (:snapshot_id, :tenant_id, :opportunity_id, now(), :scorer_version, "
    "CAST(:passed_gates AS jsonb), CAST(:failed_gates AS jsonb), "
    ":sort_evidence_rank, :sort_value_band, :sort_supply_rank, "
    "CAST(:gate_reasons AS jsonb), :rank_bucket, "
    ":estimated_value_amount, :estimated_value_currency)"
)


def _opportunity_full_params(
    opportunity_id: str,
    need_id: str,
    *,
    state: str = "qualified",
    closed_by: str | None = None,
    closed_at: datetime | None = None,
    target_price_amount: Decimal | None = None,
    target_price_currency: str | None = None,
    estimated_cost_amount: Decimal | None = None,
    estimated_cost_currency: str | None = None,
    estimated_profit_amount: Decimal | None = None,
    estimated_profit_currency: str | None = None,
) -> dict[str, object]:
    """带 state/closed/金额列的完整机会行参数（负测用；默认全 NULL/qualified）。"""
    return {
        "opportunity_id": opportunity_id,
        "tenant_id": "tSchema4",
        "account_id": "acc-1",
        "account_name": "Acme",
        "country": "US",
        "need_id": need_id,
        "product_category": "hinges",
        "state": state,
        "closed_by": closed_by,
        "closed_at": closed_at,
        "target_price_amount": target_price_amount,
        "target_price_currency": target_price_currency,
        "estimated_cost_amount": estimated_cost_amount,
        "estimated_cost_currency": estimated_cost_currency,
        "estimated_profit_amount": estimated_profit_amount,
        "estimated_profit_currency": estimated_profit_currency,
    }


async def _assert_dml_rejected(
    engine: AsyncEngine,
    table: str,
    statement: str,
    params: dict[str, object],
) -> None:
    """UPDATE/DELETE 应被 DB 拒绝（只增 / 字段白名单）；独立事务、失败后回滚。"""
    async with engine.connect() as conn:
        try:
            await conn.execute(text(statement), params)
        except DBAPIError:
            await conn.rollback()
        else:
            await conn.rollback()
            pytest.fail(f"{table}：该语句应被数据库拒绝")


async def _assert_statement_integrity_rejected(
    engine: AsyncEngine,
    statement: TextClause,
    params: dict[str, object],
    label: str,
) -> None:
    """INSERT 应被 CHECK/唯一约束拒绝（IntegrityError）；独立事务、失败后回滚。"""
    async with engine.connect() as conn:
        try:
            await conn.execute(statement, params)
        except IntegrityError:
            await conn.rollback()
        else:
            await conn.rollback()
            pytest.fail(label)


async def test_six_tables_exist_with_tenant_id(db_url: str) -> None:
    """六表存在，且每表含 tenant_id（硬边界 8 的 schema 落地）。

    本地引擎（try/finally dispose），避免 session 级 async fixture 在事件循环
    关闭后的 teardown 报错。
    """
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        names = await _table_names(engine)
        missing = set(EXPECTED_TABLES) - names
        assert not missing, f"缺失表：{sorted(missing)}"
        for table in EXPECTED_TABLES:
            cols = await _columns(engine, table)
            assert "tenant_id" in cols, f"{table} 缺 tenant_id（硬边界 8）"
    finally:
        await engine.dispose()


async def test_roundtrip_downgrade_base_then_upgrade_head(db_url: str) -> None:
    """迁移 round-trip：head 六表在 → downgrade base 消失 → upgrade head 恢复。

    用独立引擎（不共享 integration_engine 的会话级连接池，避免缓存 schema）；
    finally 兜底恢复 head，绝不因异常留下降级后的库。
    """
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        names = await _table_names(engine)
        assert set(EXPECTED_TABLES) <= names, "roundtrip 前置：head 应含六表"
        _run_alembic(db_url, "downgrade", "base")
        names = await _table_names(engine)
        assert not (set(EXPECTED_TABLES) & names), "downgrade base 后六表应消失"
        _run_alembic(db_url, "upgrade", "head")
        names = await _table_names(engine)
        assert set(EXPECTED_TABLES) <= names, "upgrade head 后六表应恢复"
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_opportunity_need_unique_per_tenant(db_url: str) -> None:
    """(tenant_id, need_id) 唯一：异租户同 need_id 允许，同租户重复被拒（并发幂等兜底）。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        # 前两行独立事务提交：tenant A 与 tenant B 同 need_id 均应成功
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_OPPORTUNITY,
                _opportunity_params("oppA1", "tA", "need-1"),
            )
            await conn.execute(
                _INSERT_OPPORTUNITY,
                _opportunity_params("oppB1", "tB", "need-1"),
            )
        # 第三行：tenant A 重复 need_id → UNIQUE(tenant_id, need_id) 拒绝
        async with engine.connect() as conn:
            try:
                await conn.execute(
                    _INSERT_OPPORTUNITY,
                    _opportunity_params("oppA2", "tA", "need-1"),
                )
                await conn.commit()
                pytest.fail("同租户重复 need_id 应被 UNIQUE 拒绝")
            except IntegrityError:
                await conn.rollback()
    finally:
        await engine.dispose()


async def test_append_only_tables_reject_update_and_delete(db_url: str) -> None:
    """score_snapshots/loss_records/provenance_records：UPDATE 与 DELETE 均被 DB 拒绝（只增）。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        tenant_id = "tAudit1"
        opp_id = "opp-audit-1"
        async with engine.begin() as conn:
            await conn.execute(_INSERT_OPPORTUNITY, _opportunity_params(opp_id, tenant_id, "need-audit-1"))
            await conn.execute(_INSERT_SCORE_SNAPSHOT, _snapshot_params("snap-audit-1", tenant_id, opp_id))
            await conn.execute(_INSERT_LOSS_RECORD, _loss_params("loss-audit-1", tenant_id, opp_id))
            await conn.execute(_INSERT_PROVENANCE, _prov_params("prov-audit-1", tenant_id, "opportunity", opp_id))

        cases: list[tuple[str, str, dict[str, object]]] = [
            (
                "score_snapshots",
                "UPDATE score_snapshots SET rank_bucket = 'low' WHERE snapshot_id = :id",
                {"id": "snap-audit-1"},
            ),
            (
                "score_snapshots",
                "DELETE FROM score_snapshots WHERE snapshot_id = :id",
                {"id": "snap-audit-1"},
            ),
            (
                "loss_records",
                "UPDATE loss_records SET loss_reason = 'lost_to_competitor' WHERE loss_record_id = :id",
                {"id": "loss-audit-1"},
            ),
            (
                "loss_records",
                "DELETE FROM loss_records WHERE loss_record_id = :id",
                {"id": "loss-audit-1"},
            ),
            (
                "provenance_records",
                "UPDATE provenance_records SET field_name = 'country' WHERE provenance_id = :id",
                {"id": "prov-audit-1"},
            ),
            (
                "provenance_records",
                "DELETE FROM provenance_records WHERE provenance_id = :id",
                {"id": "prov-audit-1"},
            ),
        ]
        for table, statement, params in cases:
            await _assert_dml_rejected(engine, table, statement, params)
    finally:
        await engine.dispose()


async def test_outbox_status_and_delivered_at_updatable(db_url: str) -> None:
    """outbox_events：status→delivered 与 delivered_at 更新成功（仅字段白名单，无只增触发器）。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        event_id = "evt-outbox-1"
        async with engine.begin() as conn:
            await conn.execute(_INSERT_OUTBOX, _outbox_params(event_id, "tOutbox"))
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE outbox_events SET status = 'delivered', delivered_at = now() "
                    "WHERE event_id = :id RETURNING status, delivered_at"
                ),
                {"id": event_id},
            )
            row = result.mappings().one()
            assert row["status"] == "delivered"
            assert row["delivered_at"] is not None
        async with engine.begin() as conn:
            result = await conn.execute(
                text("SELECT status, delivered_at FROM outbox_events WHERE event_id = :id"),
                {"id": event_id},
            )
            row = result.mappings().one()
            assert row["status"] == "delivered"
            assert row["delivered_at"] is not None
    finally:
        await engine.dispose()


async def test_outbox_invalid_attempt_and_status_rejected(db_url: str) -> None:
    """outbox_events：attempt=0 与非法 status 各被 CHECK 约束拒绝。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        await _assert_statement_integrity_rejected(
            engine,
            _INSERT_OUTBOX,
            _outbox_params("evt-bad-attempt", "tOutbox", attempt=0),
            "attempt=0 应被 CHECK(attempt >= 1) 拒绝",
        )
        await _assert_statement_integrity_rejected(
            engine,
            _INSERT_OUTBOX,
            _outbox_params("evt-bad-status", "tOutbox", status="shipped"),
            "非法 status 应被 CHECK(status IN ('pending','delivered')) 拒绝",
        )
    finally:
        await engine.dispose()


async def test_outbox_non_allowed_fields_not_updatable(db_url: str) -> None:
    """outbox_events：仅 status/delivered_at 可变——更新 event_type/event_payload 被 DB 拒绝。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        event_id = "evt-frozen-1"
        async with engine.begin() as conn:
            await conn.execute(_INSERT_OUTBOX, _outbox_params(event_id, "tOutbox"))
        await _assert_dml_rejected(
            engine,
            "outbox_events",
            "UPDATE outbox_events SET event_type = :event_type WHERE event_id = :id",
            {"event_type": "OpportunityQualified", "id": event_id},
        )
        await _assert_dml_rejected(
            engine,
            "outbox_events",
            "UPDATE outbox_events SET event_payload = CAST(:payload AS jsonb) WHERE event_id = :id",
            {"payload": json.dumps({"other": 1}), "id": event_id},
        )
    finally:
        await engine.dispose()


_MONEY_OPPORTUNITY_CASES: list[tuple[str, dict[str, object]]] = [
    (
        "target_price 只给 amount 缺 currency",
        _opportunity_full_params(
            "opp-money-tp",
            "need-money-tp",
            target_price_amount=Decimal("500.00"),
            target_price_currency=None,
        ),
    ),
    (
        "estimated_cost 只给 amount 缺 currency",
        _opportunity_full_params(
            "opp-money-ec",
            "need-money-ec",
            estimated_cost_amount=Decimal("100.00"),
            estimated_cost_currency=None,
        ),
    ),
    (
        "estimated_profit 只给 amount 缺 currency",
        _opportunity_full_params(
            "opp-money-ep",
            "need-money-ep",
            estimated_profit_amount=Decimal("200.00"),
            estimated_profit_currency=None,
        ),
    ),
]

_CLOSED_OPP_CASES: list[tuple[str, dict[str, object]]] = [
    (
        "state=lost 缺 loss_reason/died_at_state/closed_by/closed_at",
        _opportunity_full_params("opp-closed-lost", "need-closed-lost", state="lost"),
    ),
    (
        "state=won 缺 closed_by/closed_at",
        _opportunity_full_params("opp-closed-won", "need-closed-won", state="won"),
    ),
    (
        "非终态 qualified 带 closed_by/closed_at",
        _opportunity_full_params(
            "opp-closed-q",
            "need-closed-q",
            state="qualified",
            closed_by="emp-1",
            closed_at=_NOW,
        ),
    ),
]


@pytest.mark.parametrize(
    ("label", "params"),
    _MONEY_OPPORTUNITY_CASES,
    ids=[label for label, _ in _MONEY_OPPORTUNITY_CASES],
)
async def test_money_pair_amount_without_currency_rejected(
    db_url: str, label: str, params: dict[str, object]
) -> None:
    """金额成对：opportunities 三组金额只给 amount 缺 currency → 被 CHECK 拒绝（Decimal）。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        await _assert_statement_integrity_rejected(
            engine,
            _INSERT_OPPORTUNITY_WITH_MONEY,
            params,
            f"{label} 应被金额成对 CHECK 拒绝",
        )
    finally:
        await engine.dispose()


async def test_snapshot_value_amount_without_currency_rejected(db_url: str) -> None:
    """score_snapshots estimated_value 只给 amount 缺 currency → 被 CHECK 拒绝（Decimal）。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        params: dict[str, object] = {
            **_snapshot_params("snap-money-1", "tSchema4", "opp-dangling-1"),
            "estimated_value_amount": Decimal("300.00"),
            "estimated_value_currency": None,
        }
        await _assert_statement_integrity_rejected(
            engine,
            _INSERT_SNAPSHOT_WITH_VALUE,
            params,
            "score_snapshots estimated_value 只给 amount 应被金额成对 CHECK 拒绝",
        )
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("label", "params"),
    _CLOSED_OPP_CASES,
    ids=[label for label, _ in _CLOSED_OPP_CASES],
)
async def test_opportunity_closed_constraints_rejected(
    db_url: str, label: str, params: dict[str, object]
) -> None:
    """closed 约束：lost 缺闭环字段 / won 缺确认字段 / 非终态带 closed → 均被拒。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        await _assert_statement_integrity_rejected(
            engine,
            _INSERT_OPPORTUNITY_WITH_MONEY,
            params,
            f"{label} 应被 CHECK 约束拒绝",
        )
    finally:
        await engine.dispose()


async def test_loss_record_requires_confirmed_actor_and_time(db_url: str) -> None:
    """loss_records：缺 confirmed_by / 缺 confirmed_at 各被 NOT NULL 拒绝（人工确认必留痕）。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        tenant_id = "tLoss"
        opp_id = "opp-loss-1"
        async with engine.begin() as conn:
            await conn.execute(_INSERT_OPPORTUNITY, _opportunity_params(opp_id, tenant_id, "need-loss-1"))
        await _assert_statement_integrity_rejected(
            engine,
            _INSERT_LOSS_RECORD_FULL,
            _loss_full_params("loss-bad-confirmed-by", tenant_id, opp_id, confirmed_by=None),
            "缺 confirmed_by 应被 NOT NULL 拒绝",
        )
        await _assert_statement_integrity_rejected(
            engine,
            _INSERT_LOSS_RECORD_FULL,
            _loss_full_params("loss-bad-confirmed-at", tenant_id, opp_id, confirmed_at=None),
            "缺 confirmed_at 应被 NOT NULL 拒绝",
        )
    finally:
        await engine.dispose()


async def test_handoff_loss_composite_fk_tenant_isolation_and_restrict(db_url: str) -> None:
    """复合 FK (tenant_id, opportunity_id)：跨租户引用被拒、同租户合法、DELETE parent 被 RESTRICT。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        tenant_a = "tFK_a"
        opp_a = "opp-fk-a"
        async with engine.begin() as conn:
            await conn.execute(_INSERT_OPPORTUNITY, _opportunity_params(opp_a, tenant_a, "need-fk-a"))
        await _assert_statement_integrity_rejected(
            engine,
            _INSERT_HANDOFF,
            _handoff_params("ho-fk-cross-b", "tFK_b", opp_a),
            "跨租户 handoff 引用应被复合 FK 拒绝",
        )
        await _assert_statement_integrity_rejected(
            engine,
            _INSERT_LOSS_RECORD,
            _loss_params("loss-fk-cross-b", "tFK_b", opp_a),
            "跨租户 loss_record 引用应被复合 FK 拒绝",
        )
        async with engine.begin() as conn:
            await conn.execute(_INSERT_HANDOFF, _handoff_params("ho-fk-a-1", tenant_a, opp_a))
            await conn.execute(_INSERT_LOSS_RECORD, _loss_params("loss-fk-a-1", tenant_a, opp_a))
        await _assert_statement_integrity_rejected(
            engine,
            text("DELETE FROM opportunities WHERE opportunity_id = :id AND tenant_id = :tenant"),
            {"id": opp_a, "tenant": tenant_a},
            "删除仍被引用的机会应被 ON DELETE RESTRICT 拒绝",
        )
    finally:
        await engine.dispose()


async def test_score_snapshot_dangling_opportunity_allowed(db_url: str) -> None:
    """score_snapshots.opportunity_id 无 FK：不存在的机会 ID 也可插入（失败门槛快照保留）。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_SCORE_SNAPSHOT,
                _snapshot_params("snap-dangling-1", "tDangling", "opp-never-created"),
            )
    finally:
        await engine.dispose()


def _sync_sending_contract(conn: Connection) -> dict[str, dict[str, object]]:
    inspector = inspect(conn)
    contract: dict[str, dict[str, object]] = {}
    for table in SENDING_IDENTITY_TABLES:
        columns = inspector.get_columns(table)
        contract[table] = {
            "columns": {
                str(column["name"]): (
                    type(column["type"]).__name__.upper(),
                    getattr(column["type"], "length", None),
                    getattr(column["type"], "precision", None),
                    getattr(column["type"], "scale", None),
                    bool(column["nullable"]),
                )
                for column in columns
            },
            "pk": tuple(inspector.get_pk_constraint(table)["constrained_columns"]),
            "uniques": {
                str(item["name"]): tuple(item["column_names"])
                for item in inspector.get_unique_constraints(table)
            },
            "fks": {
                str(item["name"]): (
                    tuple(item["constrained_columns"]),
                    str(item["referred_table"]),
                    tuple(item["referred_columns"]),
                )
                for item in inspector.get_foreign_keys(table)
            },
            "checks": {str(item["name"]) for item in inspector.get_check_constraints(table)},
            "indexes": {
                str(item["name"]): tuple(item["column_names"])
                for item in inspector.get_indexes(table)
                if not item.get("duplicates_constraint")
            },
        }
    return contract


def _sync_sending_guards(conn: Connection) -> tuple[dict[str, set[str]], set[str]]:
    trigger_rows = conn.execute(
        text(
            "SELECT c.relname, t.tgname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid=t.tgrelid "
            "WHERE NOT t.tgisinternal AND c.relname = ANY(:tables)"
        ),
        {"tables": list(SENDING_IDENTITY_TABLES)},
    ).all()
    triggers: dict[str, set[str]] = {table: set() for table in SENDING_IDENTITY_TABLES}
    for table, trigger in trigger_rows:
        triggers[str(table)].add(str(trigger))
    functions = {
        str(row[0])
        for row in conn.execute(
            text("SELECT proname FROM pg_proc WHERE proname LIKE 'sending_%guard'")
        ).all()
    }
    return triggers, functions


def _orm_column_contract(table: object) -> dict[str, tuple[str, int | None, int | None, int | None, bool]]:
    columns = table.columns  # type: ignore[attr-defined]
    result: dict[str, tuple[str, int | None, int | None, int | None, bool]] = {}
    for column in columns:
        name = type(column.type).__name__.upper()
        name = {"DATETIME": "TIMESTAMP", "STRING": "VARCHAR"}.get(name, name)
        result[column.name] = (
            name,
            getattr(column.type, "length", None),
            getattr(column.type, "precision", None),
            getattr(column.type, "scale", None),
            bool(column.nullable),
        )
    return result


async def test_sending_identity_schema_and_orm_contract_are_exact(db_url: str) -> None:
    """0008 七表的列型、空值、键、约束、索引及 ORM 映射逐项一致。"""
    from infra.db.session import create_engine_from
    from infra.db.tables import Base

    expected_columns: dict[str, dict[str, tuple[str, int | None, int | None, int | None, bool]]] = {
        "sending_domains": {
            "tenant_id": ("VARCHAR", 32, None, None, False),
            "domain": ("VARCHAR", 253, None, None, False),
            "role": ("VARCHAR", 32, None, None, False),
            "created_at": ("TIMESTAMP", None, None, None, False),
        },
        "sending_identities": {
            "tenant_id": ("VARCHAR", 32, None, None, False),
            "identity_id": ("VARCHAR", 32, None, None, False),
            "domain": ("VARCHAR", 253, None, None, False),
            "address": ("VARCHAR", 320, None, None, False),
            "display_name": ("VARCHAR", 200, None, None, True),
            "state": ("VARCHAR", 32, None, None, False),
            "connector_ref": ("VARCHAR", 64, None, None, True),
            "warmup_started_on": ("DATE", None, None, None, True),
            "target_daily_volume": ("INTEGER", None, None, None, True),
            "activated_at": ("TIMESTAMP", None, None, None, True),
            "suspended_at": ("TIMESTAMP", None, None, None, True),
            "retired_at": ("TIMESTAMP", None, None, None, True),
            "sendable_state_before_restriction": ("VARCHAR", 32, None, None, True),
            "suspension_category": ("VARCHAR", 64, None, None, True),
            "version": ("INTEGER", None, None, None, False),
            "throttle_hard_bounce_rate": ("NUMERIC", None, 9, 6, False),
            "suspend_hard_bounce_rate": ("NUMERIC", None, 9, 6, False),
            "throttle_complaint_rate": ("NUMERIC", None, 9, 6, False),
            "suspend_complaint_rate": ("NUMERIC", None, 9, 6, False),
            "suspend_on_spam_trap": ("BOOLEAN", None, None, None, False),
            "suspend_on_blocklist": ("BOOLEAN", None, None, None, False),
            "minimum_sample": ("INTEGER", None, None, None, False),
            "created_at": ("TIMESTAMP", None, None, None, False),
        },
        "sending_auth_checks": {
            "tenant_id": ("VARCHAR", 32, None, None, False),
            "auth_check_id": ("VARCHAR", 32, None, None, False),
            "identity_id": ("VARCHAR", 32, None, None, False),
            "checked_at": ("TIMESTAMP", None, None, None, False),
            "spf_passed": ("BOOLEAN", None, None, None, False),
            "dkim_passed": ("BOOLEAN", None, None, None, False),
            "dmarc_passed": ("BOOLEAN", None, None, None, False),
            "failures": ("JSONB", None, None, None, False),
            "check_ref": ("VARCHAR", 64, None, None, False),
            "created_at": ("TIMESTAMP", None, None, None, False),
        },
        "sending_reputation_events": {
            "tenant_id": ("VARCHAR", 32, None, None, False),
            "reputation_event_id": ("VARCHAR", 32, None, None, False),
            "identity_id": ("VARCHAR", 32, None, None, False),
            "event_type": ("VARCHAR", 32, None, None, False),
            "occurred_at": ("TIMESTAMP", None, None, None, False),
            "dedup_key": ("VARCHAR", 200, None, None, False),
            "source_ref": ("VARCHAR", 64, None, None, False),
            "created_at": ("TIMESTAMP", None, None, None, False),
        },
        "sending_daily_counters": {
            "tenant_id": ("VARCHAR", 32, None, None, False),
            "identity_id": ("VARCHAR", 32, None, None, False),
            "on_day": ("DATE", None, None, None, False),
            "sent_attempts": ("INTEGER", None, None, None, False),
        },
        "sending_send_reservations": {
            "tenant_id": ("VARCHAR", 32, None, None, False),
            "reservation_id": ("VARCHAR", 32, None, None, False),
            "identity_id": ("VARCHAR", 32, None, None, False),
            "reservation_key": ("VARCHAR", 200, None, None, False),
            "on_day": ("DATE", None, None, None, False),
            "sequence": ("INTEGER", None, None, None, False),
            "created_at": ("TIMESTAMP", None, None, None, False),
        },
        "sending_identity_actions": {
            "tenant_id": ("VARCHAR", 32, None, None, False),
            "action_id": ("VARCHAR", 32, None, None, False),
            "identity_id": ("VARCHAR", 32, None, None, False),
            "action_key": ("VARCHAR", 200, None, None, False),
            "action": ("VARCHAR", 64, None, None, False),
            "before_state": ("VARCHAR", 32, None, None, True),
            "after_state": ("VARCHAR", 32, None, None, True),
            "actor_id": ("VARCHAR", 64, None, None, False),
            "scope": ("VARCHAR", 32, None, None, False),
            "rule": ("VARCHAR", 128, None, None, False),
            "note": ("TEXT", None, None, None, True),
            "occurred_at": ("TIMESTAMP", None, None, None, False),
        },
    }
    expected_pks = {
        "sending_domains": ("tenant_id", "domain"),
        "sending_identities": ("tenant_id", "identity_id"),
        "sending_auth_checks": ("tenant_id", "auth_check_id"),
        "sending_reputation_events": ("tenant_id", "reputation_event_id"),
        "sending_daily_counters": ("tenant_id", "identity_id", "on_day"),
        "sending_send_reservations": ("tenant_id", "reservation_id"),
        "sending_identity_actions": ("tenant_id", "action_id"),
    }
    expected_uniques = {
        "sending_identities": {"uq_sending_identities_tenant_address": ("tenant_id", "address")},
        "sending_auth_checks": {"uq_sending_auth_tenant_identity_ref": ("tenant_id", "identity_id", "check_ref")},
        "sending_reputation_events": {"uq_sending_reputation_tenant_dedup": ("tenant_id", "dedup_key")},
        "sending_send_reservations": {
            "uq_sending_reservation_tenant_identity_key": ("tenant_id", "identity_id", "reservation_key"),
            "uq_sending_reservation_tenant_identity_day_sequence": ("tenant_id", "identity_id", "on_day", "sequence"),
        },
        "sending_identity_actions": {"uq_sending_action_tenant_identity_key": ("tenant_id", "identity_id", "action_key")},
    }
    expected_fks = {
        "sending_identities": {"fk_sending_identities_domain": (("tenant_id", "domain"), "sending_domains", ("tenant_id", "domain"))},
        "sending_auth_checks": {"fk_sending_auth_identity": (("tenant_id", "identity_id"), "sending_identities", ("tenant_id", "identity_id"))},
        "sending_reputation_events": {"fk_sending_reputation_identity": (("tenant_id", "identity_id"), "sending_identities", ("tenant_id", "identity_id"))},
        "sending_daily_counters": {"fk_sending_counter_identity": (("tenant_id", "identity_id"), "sending_identities", ("tenant_id", "identity_id"))},
        "sending_send_reservations": {"fk_sending_reservation_identity": (("tenant_id", "identity_id"), "sending_identities", ("tenant_id", "identity_id"))},
        "sending_identity_actions": {"fk_sending_action_identity": (("tenant_id", "identity_id"), "sending_identities", ("tenant_id", "identity_id"))},
    }
    expected_checks = {
        "sending_domains": set(),
        "sending_identities": {"ck_sending_identity_target_volume", "ck_sending_identity_version", "ck_sending_identity_minimum_sample", "ck_sending_identity_warmup_pair", "ck_sending_identity_address_domain", "ck_sending_identity_restriction_state", "ck_sending_identity_suspension_category"},
        "sending_auth_checks": set(),
        "sending_reputation_events": set(),
        "sending_daily_counters": {"ck_sending_counter_nonnegative"},
        "sending_send_reservations": {"ck_sending_reservation_sequence"},
        "sending_identity_actions": set(),
    }
    expected_indexes = {
        "sending_reputation_events": {"ix_sending_reputation_tenant_identity_occurred": ("tenant_id", "identity_id", "occurred_at")}
    }

    engine = create_engine_from(db_url)
    try:
        async with engine.connect() as conn:
            db_contract = await conn.run_sync(_sync_sending_contract)
            triggers, functions = await conn.run_sync(_sync_sending_guards)
        assert set(db_contract) == set(SENDING_IDENTITY_TABLES)
        for table in SENDING_IDENTITY_TABLES:
            assert db_contract[table]["columns"] == expected_columns[table]
            assert db_contract[table]["pk"] == expected_pks[table]
            assert db_contract[table]["uniques"] == expected_uniques.get(table, {})
            assert db_contract[table]["fks"] == expected_fks.get(table, {})
            assert db_contract[table]["checks"] == expected_checks[table]
            assert db_contract[table]["indexes"] == expected_indexes.get(table, {})

            orm_table = Base.metadata.tables[table]
            assert _orm_column_contract(orm_table) == expected_columns[table]
            assert tuple(column.name for column in orm_table.primary_key.columns) == expected_pks[table]
            orm_uniques = {
                str(constraint.name): tuple(column.name for column in constraint.columns)
                for constraint in orm_table.constraints
                if isinstance(constraint, UniqueConstraint)
            }
            orm_fks = {
                str(constraint.name): (
                    tuple(column.name for column in constraint.columns),
                    next(iter(constraint.elements)).column.table.name,
                    tuple(element.column.name for element in constraint.elements),
                )
                for constraint in orm_table.constraints
                if isinstance(constraint, ForeignKeyConstraint)
            }
            orm_checks = {
                str(constraint.name)
                for constraint in orm_table.constraints
                if isinstance(constraint, CheckConstraint)
            }
            orm_indexes = {
                str(index.name): tuple(column.name for column in index.columns)
                for index in orm_table.indexes
            }
            assert orm_uniques == expected_uniques.get(table, {})
            assert orm_fks == expected_fks.get(table, {})
            assert orm_checks == expected_checks[table]
            assert orm_indexes == expected_indexes.get(table, {})

        assert triggers == {
            "sending_domains": {"trg_sending_domains_immutable"},
            "sending_identities": set(),
            "sending_auth_checks": {"trg_sending_auth_checks_append_only"},
            "sending_reputation_events": {"trg_sending_reputation_events_append_only"},
            "sending_daily_counters": {"trg_sending_daily_counters_guard"},
            "sending_send_reservations": {"trg_sending_send_reservations_append_only"},
            "sending_identity_actions": {"trg_sending_identity_actions_append_only"},
        }
        assert functions == {
            "sending_append_only_guard",
            "sending_domains_immutable_guard",
            "sending_daily_counters_guard",
        }
    finally:
        await engine.dispose()


async def test_sending_identity_roundtrip_0008_0007_0008(db_url: str) -> None:
    """0008→0007 删除七表，再 upgrade head 精确恢复。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        assert set(SENDING_IDENTITY_TABLES) <= await _table_names(engine)
        _run_alembic(db_url, "downgrade", "0007")
        assert not (set(SENDING_IDENTITY_TABLES) & await _table_names(engine))
        _run_alembic(db_url, "upgrade", "head")
        assert set(SENDING_IDENTITY_TABLES) <= await _table_names(engine)
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_sending_identity_database_guards(db_url: str) -> None:
    """真实 PG 拒绝跨租户 FK、重复键、非法计数及审计表改写。"""
    from infra.db.session import create_engine_from

    domain_sql = text("INSERT INTO sending_domains VALUES (:tenant,:domain,:role,:created)")
    identity_sql = text(
        "INSERT INTO sending_identities (tenant_id,identity_id,domain,address,state,version,"
        "throttle_hard_bounce_rate,suspend_hard_bounce_rate,throttle_complaint_rate,"
        "suspend_complaint_rate,suspend_on_spam_trap,suspend_on_blocklist,minimum_sample,created_at) "
        "VALUES (:tenant,:identity,:domain,:address,'created',0,.03,.05,.001,.003,true,true,50,:created)"
    )
    engine = create_engine_from(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(domain_sql, {"tenant": "tMigA", "domain": "cold.example.com", "role": "cold_outreach", "created": _NOW})
            await conn.execute(identity_sql, {"tenant": "tMigA", "identity": "sidA", "domain": "cold.example.com", "address": "sales@cold.example.com", "created": _NOW})

        await _assert_statement_integrity_rejected(engine, identity_sql, {"tenant": "tMigB", "identity": "sidCross", "domain": "cold.example.com", "address": "cross@cold.example.com", "created": _NOW}, "跨租户 identity→domain FK 应被拒绝")
        await _assert_statement_integrity_rejected(engine, identity_sql, {"tenant": "tMigA", "identity": "sidDup", "domain": "cold.example.com", "address": "sales@cold.example.com", "created": _NOW}, "同租户重复 address 应被拒绝")
        await _assert_statement_integrity_rejected(engine, domain_sql, {"tenant": "tMigA", "domain": "cold.example.com", "role": "primary_business", "created": _NOW}, "同域角色冲突应被拒绝")

        restricted_sql = text(
            "INSERT INTO sending_identities (tenant_id,identity_id,domain,address,state,"
            "sendable_state_before_restriction,suspension_category,version,"
            "throttle_hard_bounce_rate,suspend_hard_bounce_rate,throttle_complaint_rate,"
            "suspend_complaint_rate,suspend_on_spam_trap,suspend_on_blocklist,minimum_sample,created_at) "
            "VALUES ('tMigA',:identity,'cold.example.com',:address,:state,:saved,:category,"
            "0,.03,.05,.001,.003,true,true,50,:created)"
        )
        for params, reason in (
            ({"identity": "sidThrottleNull", "address": "tn@cold.example.com", "state": "throttled", "saved": None, "category": None, "created": _NOW}, "throttled+NULL saved 应被拒绝"),
            ({"identity": "sidActiveSaved", "address": "as@cold.example.com", "state": "active", "saved": "active", "category": None, "created": _NOW}, "非受限身份带 saved state 应被拒绝"),
            ({"identity": "sidSuspendNoCategory", "address": "snc@cold.example.com", "state": "suspended", "saved": "active", "category": None, "created": _NOW}, "suspended 缺 category 应被拒绝"),
            ({"identity": "sidThrottleCategory", "address": "tc@cold.example.com", "state": "throttled", "saved": "active", "category": "spam_trap", "created": _NOW}, "throttled 带 category 应被拒绝"),
            ({"identity": "sidBadCategory", "address": "bc@cold.example.com", "state": "suspended", "saved": "active", "category": "dns_investigation", "created": _NOW}, "自由 category 应被拒绝"),
        ):
            await _assert_statement_integrity_rejected(engine, restricted_sql, params, reason)
        await _assert_statement_integrity_rejected(
            engine,
            text(
                "UPDATE sending_identities SET state='suspended', "
                "sendable_state_before_restriction=NULL, suspension_category='spam_trap' "
                "WHERE tenant_id='tMigA' AND identity_id='sidA'"
            ),
            {},
            "suspended UPDATE 的 NULL saved 应被拒绝",
        )

        auth_sql = text("INSERT INTO sending_auth_checks VALUES ('tMigA',:id,'sidA',:at,true,true,true,'[]'::jsonb,:ref,:at)")
        rep_sql = text("INSERT INTO sending_reputation_events VALUES ('tMigA',:id,'sidA','delivered',:at,:key,:ref,:at)")
        reservation_sql = text("INSERT INTO sending_send_reservations VALUES ('tMigA',:id,'sidA',:key,:day,:sequence,:at)")
        action_sql = text("INSERT INTO sending_identity_actions VALUES ('tMigA',:id,'sidA',:key,'identity:register',NULL,'created','actor','tenant','rule',NULL,:at)")
        async with engine.begin() as conn:
            await conn.execute(auth_sql, {"id": "authMig", "at": _NOW, "ref": "auth_ref"})
            await conn.execute(rep_sql, {"id": "repMig", "at": _NOW, "key": "rep-key", "ref": "rep_ref"})
            await conn.execute(reservation_sql, {"id": "resMig", "key": "res-key", "day": _NOW.date(), "sequence": 1, "at": _NOW})
            await conn.execute(action_sql, {"id": "actMig", "key": "act-key", "at": _NOW})
            await conn.execute(text("INSERT INTO sending_daily_counters VALUES ('tMigA','sidA',:day,1)"), {"day": _NOW.date()})

        await _assert_statement_integrity_rejected(engine, reservation_sql, {"id": "resDupSeq", "key": "other-key", "day": _NOW.date(), "sequence": 1, "at": _NOW}, "重复 sequence 应被拒绝")
        await _assert_statement_integrity_rejected(engine, reservation_sql, {"id": "resNeg", "key": "negative-key", "day": _NOW.date(), "sequence": -1, "at": _NOW}, "负 sequence 应被拒绝")
        await _assert_statement_integrity_rejected(engine, text("INSERT INTO sending_daily_counters VALUES ('tMigA','sidA',:day,-1)"), {"day": date(2026, 8, 11)}, "负 counter 应被拒绝")

        for table, column, value in (
            ("sending_auth_checks", "check_ref", "changed"),
            ("sending_reputation_events", "source_ref", "changed"),
            ("sending_send_reservations", "reservation_key", "changed"),
            ("sending_identity_actions", "rule", "changed"),
        ):
            await _assert_dml_rejected(engine, table, f"UPDATE {table} SET {column}=:value WHERE tenant_id='tMigA'", {"value": value})
            await _assert_dml_rejected(engine, table, f"DELETE FROM {table} WHERE tenant_id='tMigA'", {})
        for statement in (
            "UPDATE sending_daily_counters SET sent_attempts=0 WHERE tenant_id='tMigA'",
            "UPDATE sending_daily_counters SET tenant_id='tOther' WHERE tenant_id='tMigA'",
            "UPDATE sending_daily_counters SET identity_id='sidOther' WHERE tenant_id='tMigA'",
            "UPDATE sending_daily_counters SET on_day=on_day + 1 WHERE tenant_id='tMigA'",
        ):
            await _assert_dml_rejected(engine, "sending_daily_counters", statement, {})
        await _assert_dml_rejected(
            engine,
            "sending_daily_counters",
            "DELETE FROM sending_daily_counters WHERE tenant_id='tMigA'",
            {},
        )
        for statement in (
            "UPDATE sending_domains SET role='transactional' WHERE tenant_id='tMigA'",
            "UPDATE sending_domains SET domain='other.example.com' WHERE tenant_id='tMigA'",
            "UPDATE sending_domains SET tenant_id='tOther' WHERE tenant_id='tMigA'",
        ):
            await _assert_dml_rejected(engine, "sending_domains", statement, {})
    finally:
        await engine.dispose()
