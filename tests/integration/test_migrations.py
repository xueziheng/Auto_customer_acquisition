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
import re
import subprocess
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

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

OUTREACH_TABLES: tuple[str, ...] = (
    "outreach_campaigns",
    "outreach_campaign_versions",
    "outreach_sequence_steps",
    "outreach_enrollments",
    "outreach_suppressions",
    "outreach_daily_quotas",
    "outreach_message_attempts",
    "outreach_actions",
)

EMAIL_FEEDBACK_TABLES: tuple[str, ...] = (
    "email_feedback_cursors",
    "email_feedback_receipts",
    "email_feedback_quarantines",
    "unsubscribe_tokens",
)

ARTIFACT_TABLES: tuple[str, ...] = ("raw_artifacts", "artifacts")

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


def _sync_email_feedback_contract(
    conn: Connection,
) -> tuple[dict[str, dict[str, object]], dict[str, set[str]], set[str]]:
    inspector = inspect(conn)
    contract: dict[str, dict[str, object]] = {}
    for table in EMAIL_FEEDBACK_TABLES:
        constraints = {
            str(inspector.get_pk_constraint(table)["name"]),
            *(str(item["name"]) for item in inspector.get_foreign_keys(table)),
            *(str(item["name"]) for item in inspector.get_unique_constraints(table)),
            *(str(item["name"]) for item in inspector.get_check_constraints(table)),
        }
        contract[table] = {
            "columns": {str(item["name"]) for item in inspector.get_columns(table)},
            "constraints": constraints,
            "indexes": {
                str(item["name"]): tuple(str(value) for value in item["column_names"])
                for item in inspector.get_indexes(table)
                if not item.get("duplicates_constraint")
            },
        }
    trigger_rows = conn.execute(
        text(
            "SELECT c.relname, t.tgname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid=t.tgrelid "
            "WHERE NOT t.tgisinternal AND c.relname = ANY(:tables)"
        ),
        {"tables": list(EMAIL_FEEDBACK_TABLES)},
    )
    triggers: dict[str, set[str]] = {
        table: set() for table in EMAIL_FEEDBACK_TABLES
    }
    for table, trigger in trigger_rows:
        triggers[str(table)].add(str(trigger))
    functions = {
        str(row[0])
        for row in conn.execute(
            text("SELECT proname FROM pg_proc WHERE proname = ANY(:names)"),
            {
                "names": [
                    "guard_email_feedback_append_only",
                    "guard_email_feedback_cursor_update",
                    "guard_unsubscribe_token_update",
                ]
            },
        )
    }
    return contract, triggers, functions


_INSERT_OPPORTUNITY = text(
    "INSERT INTO opportunities "
    "(opportunity_id, tenant_id, account_id, account_name, country, need_id, "
    "product_category) "
    "VALUES (:opportunity_id, :tenant_id, :account_id, :account_name, :country, "
    ":need_id, :product_category)"
)


def _opportunity_params(
    opportunity_id: str, tenant_id: str, need_id: str
) -> dict[str, str]:
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


async def _assert_deferred_integrity_rejected(
    engine: AsyncEngine,
    statement: TextClause,
    params: dict[str, object],
    constraint: str,
    label: str,
) -> None:
    """显式把 deferred FK 切到 IMMEDIATE，证明提交点一定拒绝。"""
    async with engine.connect() as conn:
        try:
            await conn.execute(statement, params)
            await conn.execute(text(f"SET CONSTRAINTS {constraint} IMMEDIATE"))
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
            await conn.execute(
                _INSERT_OPPORTUNITY,
                _opportunity_params(opp_id, tenant_id, "need-audit-1"),
            )
            await conn.execute(
                _INSERT_SCORE_SNAPSHOT,
                _snapshot_params("snap-audit-1", tenant_id, opp_id),
            )
            await conn.execute(
                _INSERT_LOSS_RECORD, _loss_params("loss-audit-1", tenant_id, opp_id)
            )
            await conn.execute(
                _INSERT_PROVENANCE,
                _prov_params("prov-audit-1", tenant_id, "opportunity", opp_id),
            )

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
                text(
                    "SELECT status, delivered_at FROM outbox_events WHERE event_id = :id"
                ),
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
            await conn.execute(
                _INSERT_OPPORTUNITY,
                _opportunity_params(opp_id, tenant_id, "need-loss-1"),
            )
        await _assert_statement_integrity_rejected(
            engine,
            _INSERT_LOSS_RECORD_FULL,
            _loss_full_params(
                "loss-bad-confirmed-by", tenant_id, opp_id, confirmed_by=None
            ),
            "缺 confirmed_by 应被 NOT NULL 拒绝",
        )
        await _assert_statement_integrity_rejected(
            engine,
            _INSERT_LOSS_RECORD_FULL,
            _loss_full_params(
                "loss-bad-confirmed-at", tenant_id, opp_id, confirmed_at=None
            ),
            "缺 confirmed_at 应被 NOT NULL 拒绝",
        )
    finally:
        await engine.dispose()


async def test_handoff_loss_composite_fk_tenant_isolation_and_restrict(
    db_url: str,
) -> None:
    """复合 FK (tenant_id, opportunity_id)：跨租户引用被拒、同租户合法、DELETE parent 被 RESTRICT。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        tenant_a = "tFK_a"
        opp_a = "opp-fk-a"
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_OPPORTUNITY, _opportunity_params(opp_a, tenant_a, "need-fk-a")
            )
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
            await conn.execute(
                _INSERT_HANDOFF, _handoff_params("ho-fk-a-1", tenant_a, opp_a)
            )
            await conn.execute(
                _INSERT_LOSS_RECORD, _loss_params("loss-fk-a-1", tenant_a, opp_a)
            )
        await _assert_statement_integrity_rejected(
            engine,
            text(
                "DELETE FROM opportunities WHERE opportunity_id = :id AND tenant_id = :tenant"
            ),
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
            "checks": {
                str(item["name"]) for item in inspector.get_check_constraints(table)
            },
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


def _sync_outreach_contract(conn: Connection) -> dict[str, dict[str, object]]:
    inspector = inspect(conn)
    contract: dict[str, dict[str, object]] = {}
    for table in OUTREACH_TABLES:
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
            "checks": {
                str(item["name"]) for item in inspector.get_check_constraints(table)
            },
            "indexes": {
                str(item["name"]): tuple(item["column_names"])
                for item in inspector.get_indexes(table)
                if not item.get("duplicates_constraint")
            },
        }
    return contract


def _sync_outreach_guards(conn: Connection) -> tuple[dict[str, set[str]], set[str]]:
    trigger_rows = conn.execute(
        text(
            "SELECT c.relname, t.tgname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid=t.tgrelid "
            "WHERE NOT t.tgisinternal AND c.relname = ANY(:tables)"
        ),
        {"tables": list(OUTREACH_TABLES)},
    ).all()
    triggers: dict[str, set[str]] = {table: set() for table in OUTREACH_TABLES}
    for table, trigger in trigger_rows:
        triggers[str(table)].add(str(trigger))
    functions = {
        str(row[0])
        for row in conn.execute(
            text("SELECT proname FROM pg_proc WHERE proname LIKE 'outreach_%guard'")
        ).all()
    }
    return triggers, functions


def _orm_column_contract(
    table: object,
) -> dict[str, tuple[str, int | None, int | None, int | None, bool]]:
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

    expected_columns: dict[
        str, dict[str, tuple[str, int | None, int | None, int | None, bool]]
    ] = {
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
        "sending_identities": {
            "uq_sending_identities_tenant_address": ("tenant_id", "address")
        },
        "sending_auth_checks": {
            "uq_sending_auth_tenant_identity_ref": (
                "tenant_id",
                "identity_id",
                "check_ref",
            )
        },
        "sending_reputation_events": {
            "uq_sending_reputation_tenant_dedup": ("tenant_id", "dedup_key")
        },
        "sending_send_reservations": {
            "uq_sending_reservation_tenant_identity_key": (
                "tenant_id",
                "identity_id",
                "reservation_key",
            ),
            "uq_sending_reservation_tenant_identity_day_sequence": (
                "tenant_id",
                "identity_id",
                "on_day",
                "sequence",
            ),
        },
        "sending_identity_actions": {
            "uq_sending_action_tenant_identity_key": (
                "tenant_id",
                "identity_id",
                "action_key",
            )
        },
    }
    expected_fks = {
        "sending_identities": {
            "fk_sending_identities_domain": (
                ("tenant_id", "domain"),
                "sending_domains",
                ("tenant_id", "domain"),
            )
        },
        "sending_auth_checks": {
            "fk_sending_auth_identity": (
                ("tenant_id", "identity_id"),
                "sending_identities",
                ("tenant_id", "identity_id"),
            )
        },
        "sending_reputation_events": {
            "fk_sending_reputation_identity": (
                ("tenant_id", "identity_id"),
                "sending_identities",
                ("tenant_id", "identity_id"),
            )
        },
        "sending_daily_counters": {
            "fk_sending_counter_identity": (
                ("tenant_id", "identity_id"),
                "sending_identities",
                ("tenant_id", "identity_id"),
            )
        },
        "sending_send_reservations": {
            "fk_sending_reservation_identity": (
                ("tenant_id", "identity_id"),
                "sending_identities",
                ("tenant_id", "identity_id"),
            )
        },
        "sending_identity_actions": {
            "fk_sending_action_identity": (
                ("tenant_id", "identity_id"),
                "sending_identities",
                ("tenant_id", "identity_id"),
            )
        },
    }
    expected_checks = {
        "sending_domains": set(),
        "sending_identities": {
            "ck_sending_identity_target_volume",
            "ck_sending_identity_version",
            "ck_sending_identity_minimum_sample",
            "ck_sending_identity_warmup_pair",
            "ck_sending_identity_address_domain",
            "ck_sending_identity_restriction_state",
            "ck_sending_identity_suspension_category",
        },
        "sending_auth_checks": set(),
        "sending_reputation_events": set(),
        "sending_daily_counters": {"ck_sending_counter_nonnegative"},
        "sending_send_reservations": {"ck_sending_reservation_sequence"},
        "sending_identity_actions": set(),
    }
    expected_indexes = {
        "sending_reputation_events": {
            "ix_sending_reputation_tenant_identity_occurred": (
                "tenant_id",
                "identity_id",
                "occurred_at",
            )
        }
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
            assert (
                tuple(column.name for column in orm_table.primary_key.columns)
                == expected_pks[table]
            )
            orm_uniques = {
                str(constraint.name): tuple(
                    column.name for column in constraint.columns
                )
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

    domain_sql = text(
        "INSERT INTO sending_domains VALUES (:tenant,:domain,:role,:created)"
    )
    identity_sql = text(
        "INSERT INTO sending_identities (tenant_id,identity_id,domain,address,state,version,"
        "throttle_hard_bounce_rate,suspend_hard_bounce_rate,throttle_complaint_rate,"
        "suspend_complaint_rate,suspend_on_spam_trap,suspend_on_blocklist,minimum_sample,created_at) "
        "VALUES (:tenant,:identity,:domain,:address,'created',0,.03,.05,.001,.003,true,true,50,:created)"
    )
    engine = create_engine_from(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                domain_sql,
                {
                    "tenant": "tMigA",
                    "domain": "cold.example.com",
                    "role": "cold_outreach",
                    "created": _NOW,
                },
            )
            await conn.execute(
                identity_sql,
                {
                    "tenant": "tMigA",
                    "identity": "sidA",
                    "domain": "cold.example.com",
                    "address": "sales@cold.example.com",
                    "created": _NOW,
                },
            )

        await _assert_statement_integrity_rejected(
            engine,
            identity_sql,
            {
                "tenant": "tMigB",
                "identity": "sidCross",
                "domain": "cold.example.com",
                "address": "cross@cold.example.com",
                "created": _NOW,
            },
            "跨租户 identity→domain FK 应被拒绝",
        )
        await _assert_statement_integrity_rejected(
            engine,
            identity_sql,
            {
                "tenant": "tMigA",
                "identity": "sidDup",
                "domain": "cold.example.com",
                "address": "sales@cold.example.com",
                "created": _NOW,
            },
            "同租户重复 address 应被拒绝",
        )
        await _assert_statement_integrity_rejected(
            engine,
            domain_sql,
            {
                "tenant": "tMigA",
                "domain": "cold.example.com",
                "role": "primary_business",
                "created": _NOW,
            },
            "同域角色冲突应被拒绝",
        )

        restricted_sql = text(
            "INSERT INTO sending_identities (tenant_id,identity_id,domain,address,state,"
            "sendable_state_before_restriction,suspension_category,version,"
            "throttle_hard_bounce_rate,suspend_hard_bounce_rate,throttle_complaint_rate,"
            "suspend_complaint_rate,suspend_on_spam_trap,suspend_on_blocklist,minimum_sample,created_at) "
            "VALUES ('tMigA',:identity,'cold.example.com',:address,:state,:saved,:category,"
            "0,.03,.05,.001,.003,true,true,50,:created)"
        )
        for params, reason in (
            (
                {
                    "identity": "sidThrottleNull",
                    "address": "tn@cold.example.com",
                    "state": "throttled",
                    "saved": None,
                    "category": None,
                    "created": _NOW,
                },
                "throttled+NULL saved 应被拒绝",
            ),
            (
                {
                    "identity": "sidActiveSaved",
                    "address": "as@cold.example.com",
                    "state": "active",
                    "saved": "active",
                    "category": None,
                    "created": _NOW,
                },
                "非受限身份带 saved state 应被拒绝",
            ),
            (
                {
                    "identity": "sidSuspendNoCategory",
                    "address": "snc@cold.example.com",
                    "state": "suspended",
                    "saved": "active",
                    "category": None,
                    "created": _NOW,
                },
                "suspended 缺 category 应被拒绝",
            ),
            (
                {
                    "identity": "sidThrottleCategory",
                    "address": "tc@cold.example.com",
                    "state": "throttled",
                    "saved": "active",
                    "category": "spam_trap",
                    "created": _NOW,
                },
                "throttled 带 category 应被拒绝",
            ),
            (
                {
                    "identity": "sidBadCategory",
                    "address": "bc@cold.example.com",
                    "state": "suspended",
                    "saved": "active",
                    "category": "dns_investigation",
                    "created": _NOW,
                },
                "自由 category 应被拒绝",
            ),
        ):
            await _assert_statement_integrity_rejected(
                engine, restricted_sql, cast(dict[str, object], params), reason
            )
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

        auth_sql = text(
            "INSERT INTO sending_auth_checks VALUES ('tMigA',:id,'sidA',:at,true,true,true,'[]'::jsonb,:ref,:at)"
        )
        rep_sql = text(
            "INSERT INTO sending_reputation_events VALUES ('tMigA',:id,'sidA','delivered',:at,:key,:ref,:at)"
        )
        reservation_sql = text(
            "INSERT INTO sending_send_reservations VALUES ('tMigA',:id,'sidA',:key,:day,:sequence,:at)"
        )
        action_sql = text(
            "INSERT INTO sending_identity_actions VALUES ('tMigA',:id,'sidA',:key,'identity:register',NULL,'created','actor','tenant','rule',NULL,:at)"
        )
        async with engine.begin() as conn:
            await conn.execute(
                auth_sql, {"id": "authMig", "at": _NOW, "ref": "auth_ref"}
            )
            await conn.execute(
                rep_sql,
                {"id": "repMig", "at": _NOW, "key": "rep-key", "ref": "rep_ref"},
            )
            await conn.execute(
                reservation_sql,
                {
                    "id": "resMig",
                    "key": "res-key",
                    "day": _NOW.date(),
                    "sequence": 1,
                    "at": _NOW,
                },
            )
            await conn.execute(
                action_sql, {"id": "actMig", "key": "act-key", "at": _NOW}
            )
            await conn.execute(
                text(
                    "INSERT INTO sending_daily_counters VALUES ('tMigA','sidA',:day,1)"
                ),
                {"day": _NOW.date()},
            )

        await _assert_statement_integrity_rejected(
            engine,
            reservation_sql,
            {
                "id": "resDupSeq",
                "key": "other-key",
                "day": _NOW.date(),
                "sequence": 1,
                "at": _NOW,
            },
            "重复 sequence 应被拒绝",
        )
        await _assert_statement_integrity_rejected(
            engine,
            reservation_sql,
            {
                "id": "resNeg",
                "key": "negative-key",
                "day": _NOW.date(),
                "sequence": -1,
                "at": _NOW,
            },
            "负 sequence 应被拒绝",
        )
        await _assert_statement_integrity_rejected(
            engine,
            text("INSERT INTO sending_daily_counters VALUES ('tMigA','sidA',:day,-1)"),
            {"day": date(2026, 8, 11)},
            "负 counter 应被拒绝",
        )

        for table, column, value in (
            ("sending_auth_checks", "check_ref", "changed"),
            ("sending_reputation_events", "source_ref", "changed"),
            ("sending_send_reservations", "reservation_key", "changed"),
            ("sending_identity_actions", "rule", "changed"),
        ):
            await _assert_dml_rejected(
                engine,
                table,
                f"UPDATE {table} SET {column}=:value WHERE tenant_id='tMigA'",
                {"value": value},
            )
            await _assert_dml_rejected(
                engine, table, f"DELETE FROM {table} WHERE tenant_id='tMigA'", {}
            )
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


async def test_0009_outreach_schema_and_roundtrip(db_url: str) -> None:
    """0009 八表必须可 0009→0008→0009 精确往返。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        assert set(OUTREACH_TABLES) <= await _table_names(engine)
        _run_alembic(db_url, "downgrade", "0008")
        assert set(OUTREACH_TABLES).isdisjoint(await _table_names(engine))
        _run_alembic(db_url, "upgrade", "0009")
        assert set(OUTREACH_TABLES) <= await _table_names(engine)
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_0009_outreach_schema_matches_orm_exactly(db_url: str) -> None:
    """0009 八表的列型、键、约束、索引及数据库护栏与 ORM 逐项一致。"""
    from infra.db.session import create_engine_from
    from infra.db.tables import Base

    engine = create_engine_from(db_url)
    try:
        async with engine.connect() as conn:
            db_contract = await conn.run_sync(_sync_outreach_contract)
            triggers, functions = await conn.run_sync(_sync_outreach_guards)
        assert set(db_contract) == set(OUTREACH_TABLES)
        for table in OUTREACH_TABLES:
            orm_table = Base.metadata.tables[table]
            orm_uniques = {
                str(constraint.name): tuple(
                    column.name for column in constraint.columns
                )
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
            assert db_contract[table] == {
                "columns": _orm_column_contract(orm_table),
                "pk": tuple(column.name for column in orm_table.primary_key.columns),
                "uniques": orm_uniques,
                "fks": orm_fks,
                "checks": orm_checks,
                "indexes": orm_indexes,
            }
        assert triggers == {
            "outreach_campaigns": set(),
            "outreach_campaign_versions": {
                "trg_outreach_campaign_versions_append_only"
            },
            "outreach_sequence_steps": {"trg_outreach_sequence_steps_append_only"},
            "outreach_enrollments": set(),
            "outreach_suppressions": {"trg_outreach_suppressions_append_only"},
            "outreach_daily_quotas": {"trg_outreach_daily_quotas_guard"},
            "outreach_message_attempts": set(),
            "outreach_actions": {"trg_outreach_actions_append_only"},
        }
        assert functions == {"outreach_append_only_guard", "outreach_daily_quota_guard"}
    finally:
        await engine.dispose()


async def test_0009_outreach_database_guards(db_url: str) -> None:
    """真实 PG 强制租户复合 FK、活动企业唯一、只增事实与单调额度。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    campaign_sql = text(
        "INSERT INTO outreach_campaigns "
        "(tenant_id,campaign_id,state,current_version,created_by,created_at,"
        "round_robin_cursor) VALUES "
        "(:tenant,:campaign,'draft',1,:actor,:created,-1)"
    )
    version_sql = text(
        "INSERT INTO outreach_campaign_versions "
        "(tenant_id,campaign_id,version,name,markets,target_entity_types,"
        "allowed_categories,sender_identity_ids,daily_new_contact_limit,"
        "daily_total_message_limit,handoff_triggers,stop_on_reply,created_by,created_at) "
        "VALUES (:tenant,:campaign,1,'Discovery','[\"US\"]'::jsonb,"
        "'[\"importer\"]'::jsonb,'[\"hardware\"]'::jsonb,"
        "'[\"sid_00000000000000000000000000\"]'::jsonb,5,10,"
        "'[\"quote_requested\"]'::jsonb,true,:actor,:created)"
    )
    step_sql = text(
        "INSERT INTO outreach_sequence_steps "
        "(tenant_id,campaign_id,version,step_number,intent,wait_days) "
        "VALUES (:tenant,:campaign,1,1,'discovery',0)"
    )
    try:
        async with engine.begin() as conn:
            params = {
                "tenant": "tn_migration_a",
                "campaign": "cmp_00000000000000000000000000",
                "actor": "emp_00000000000000000000000000",
                "created": _NOW,
            }
            await conn.execute(campaign_sql, params)
            await conn.execute(version_sql, params)
            await conn.execute(step_sql, params)

        await _assert_statement_integrity_rejected(
            engine,
            version_sql,
            {
                "tenant": "tn_migration_b",
                "campaign": "cmp_00000000000000000000000000",
                "actor": "emp_00000000000000000000000000",
                "created": _NOW,
            },
            "跨租户 Campaign version FK 应被拒绝",
        )

        enrollment_sql = text(
            "INSERT INTO outreach_enrollments "
            "(tenant_id,enrollment_id,campaign_id,campaign_version,account_id,"
            "contact_point_id,sending_identity_id,state,current_step,next_send_at,"
            "enrolled_at,idempotency_key) VALUES "
            "('tn_migration_a',:enrollment,'cmp_00000000000000000000000000',1,"
            "'acc_00000000000000000000000000',:contact,"
            "'sid_00000000000000000000000000','enrolled',0,:created,:created,:key)"
        )
        async with engine.begin() as conn:
            await conn.execute(
                enrollment_sql,
                {
                    "enrollment": "enr_00000000000000000000000000",
                    "contact": "cp_00000000000000000000000000",
                    "created": _NOW,
                    "key": "migration-enrollment-1",
                },
            )
        await _assert_statement_integrity_rejected(
            engine,
            enrollment_sql,
            {
                "enrollment": "enr_00000000000000000000000001",
                "contact": "cp_00000000000000000000000001",
                "created": _NOW,
                "key": "migration-enrollment-2",
            },
            "同企业第二条活动 Enrollment 应被拒绝",
        )

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO outreach_suppressions "
                    "(tenant_id,suppression_id,account_id,reason,occurred_at,source_ref,"
                    "idempotency_key,created_at) VALUES "
                    "('tn_migration_a','sup_00000000000000000000000000',"
                    "'acc_00000000000000000000000000','manual_block',:created,"
                    "'manual_record','migration-suppression-1',:created)"
                ),
                {"created": _NOW},
            )
            await conn.execute(
                text(
                    "INSERT INTO outreach_actions "
                    "(tenant_id,action_id,action_key,action,entity_id,actor_id,occurred_at) "
                    "VALUES ('tn_migration_a','act_00000000000000000000000000',"
                    "'migration-action-1','campaign:create',"
                    "'cmp_00000000000000000000000000',"
                    "'emp_00000000000000000000000000',:created)"
                ),
                {"created": _NOW},
            )

        immutable_targets = (
            ("outreach_campaign_versions", "name='Changed'", "version=1"),
            ("outreach_sequence_steps", "wait_days=2", "step_number=1"),
            ("outreach_suppressions", "source_ref='changed'", "true"),
            ("outreach_actions", "action='changed'", "true"),
        )
        for table, assignment, where in immutable_targets:
            async with engine.connect() as conn:
                with pytest.raises(DBAPIError):
                    async with conn.begin():
                        await conn.execute(
                            text(
                                f"UPDATE {table} SET {assignment} WHERE tenant_id="
                                "'tn_migration_a' AND " + where
                            )
                        )

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO outreach_daily_quotas VALUES "
                    "('tn_migration_a','cmp_00000000000000000000000000',"
                    "DATE '2026-08-11',2,3)"
                )
            )
        for statement in (
            (
                "UPDATE outreach_daily_quotas SET messages_reserved=1 "
                "WHERE tenant_id='tn_migration_a'"
            ),
            "DELETE FROM outreach_daily_quotas WHERE tenant_id='tn_migration_a'",
        ):
            async with engine.connect() as conn:
                with pytest.raises(DBAPIError):
                    async with conn.begin():
                        await conn.execute(text(statement))
    finally:
        await engine.dispose()


async def test_0010_tool_call_schema_is_safe_tenant_scoped_and_roundtrips(
    db_url: str,
) -> None:
    """0010 只新增安全账本两表，降级/升级后精确恢复。"""
    from infra.db.session import create_engine_from

    forbidden = {
        "params",
        "recipient",
        "sender",
        "subject",
        "body",
        "headers",
        "token",
        "authorization",
        "dsn",
        "unsubscribe_url",
        "exception",
    }
    expected_columns = {
        "tool_calls": {
            "tenant_id",
            "tool_call_id",
            "tool_id",
            "tool_version",
            "risk_level",
            "cost_class",
            "idempotency_key",
            "request_fingerprint",
            "fingerprint_version",
            "status",
            "duplicate_of",
            "lease_owner",
            "lease_expires_at",
            "attempt_count",
            "run_id",
            "user_id",
            "campaign_id",
            "message_attempt_id",
            "provider_ref",
            "error_category",
            "retry_after_at",
            "created_at",
            "updated_at",
            "completed_at",
        },
        "tool_call_events": {
            "tenant_id",
            "event_id",
            "tool_call_id",
            "stage",
            "outcome",
            "rule",
            "category",
            "actor_id",
            "run_id",
            "campaign_id",
            "message_attempt_id",
            "occurred_at",
            "duration_ms",
            "cost_note",
        },
    }

    def inspect_contract(conn):
        inspector = inspect(conn)
        result = {}
        for table in expected_columns:
            actual = {str(item["name"]) for item in inspector.get_columns(table)}
            result[table] = {
                "columns": actual,
                "pk": tuple(inspector.get_pk_constraint(table)["constrained_columns"]),
                "fks": {
                    str(item["name"]): (
                        tuple(item["constrained_columns"]),
                        str(item["referred_table"]),
                        tuple(item["referred_columns"]),
                    )
                    for item in inspector.get_foreign_keys(table)
                },
                "checks": {
                    str(item["name"]) for item in inspector.get_check_constraints(table)
                },
                "indexes": {
                    str(item["name"]): (
                        tuple(item["column_names"]),
                        bool(item["unique"]),
                    )
                    for item in inspector.get_indexes(table)
                    if not item.get("duplicates_constraint")
                },
            }
        return result

    engine = create_engine_from(db_url)
    try:
        async with engine.connect() as conn:
            before = await conn.run_sync(inspect_contract)
        for table, columns in expected_columns.items():
            assert before[table]["columns"] == columns
            assert not (before[table]["columns"] & forbidden)
        assert before["tool_calls"]["pk"] == ("tenant_id", "tool_call_id")
        assert before["tool_call_events"]["pk"] == ("tenant_id", "event_id")
        assert before["tool_calls"]["fks"]["fk_tool_calls_duplicate"] == (
            ("tenant_id", "duplicate_of"),
            "tool_calls",
            ("tenant_id", "tool_call_id"),
        )
        assert before["tool_call_events"]["fks"]["fk_tool_call_events_call"] == (
            ("tenant_id", "tool_call_id"),
            "tool_calls",
            ("tenant_id", "tool_call_id"),
        )
        assert before["tool_calls"]["indexes"]["uq_tool_calls_tenant_tool_key"] == (
            ("tenant_id", "tool_id", "idempotency_key"),
            True,
        )
        assert before["tool_calls"]["indexes"]["ix_tool_calls_tenant_status_retry"] == (
            ("tenant_id", "status", "retry_after_at", "updated_at"),
            False,
        )
        assert before["tool_call_events"]["indexes"][
            "ix_tool_call_events_tenant_call_occurred"
        ] == (("tenant_id", "tool_call_id", "occurred_at", "event_id"), False)
        assert {
            "ck_tool_calls_status",
            "ck_tool_calls_canonical_fields",
            "ck_tool_calls_result_fields",
            "ck_tool_calls_provider_ref",
            "ck_tool_calls_safe_labels",
        } <= before["tool_calls"]["checks"]
        assert "ck_tool_call_events_safe_labels" in before["tool_call_events"]["checks"]

        _run_alembic(db_url, "downgrade", "0009")
        async with engine.connect() as conn:
            names = set(
                await conn.run_sync(lambda sync: inspect(sync).get_table_names())
            )
        assert "tool_calls" not in names
        assert "tool_call_events" not in names
        _run_alembic(db_url, "upgrade", "0010")
        async with engine.connect() as conn:
            after = await conn.run_sync(inspect_contract)
        assert after == before
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_0010_tool_call_events_are_append_only(db_url: str) -> None:
    """事件行一经写入，数据库拒绝 UPDATE 与 DELETE。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    now = datetime(2026, 8, 11, 3, tzinfo=UTC)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tool_calls "
                    "(tenant_id,tool_call_id,tool_id,tool_version,risk_level,cost_class,"
                    "status,attempt_count,user_id,created_at,updated_at) VALUES "
                    "(:tenant,:call,'email.send','v1','high','low','received',0,"
                    ":actor,:now,:now)"
                ),
                {
                    "tenant": "tn_tool_guard",
                    "call": "tcl_00000000000000000000000000",
                    "actor": "usr_guard",
                    "now": now,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO tool_call_events "
                    "(tenant_id,event_id,tool_call_id,stage,outcome,actor_id,occurred_at,"
                    "duration_ms) VALUES (:tenant,:event,:call,'tenant','allowed',:actor,"
                    ":now,1)"
                ),
                {
                    "tenant": "tn_tool_guard",
                    "event": "tce_00000000000000000000000000",
                    "call": "tcl_00000000000000000000000000",
                    "actor": "usr_guard",
                    "now": now,
                },
            )
        for statement in (
            "UPDATE tool_call_events SET duration_ms=2 WHERE tenant_id=:tenant",
            "DELETE FROM tool_call_events WHERE tenant_id=:tenant",
        ):
            with pytest.raises(DBAPIError):
                async with engine.begin() as conn:
                    await conn.execute(text(statement), {"tenant": "tn_tool_guard"})
    finally:
        await engine.dispose()


async def test_0010_tool_call_database_guards_fail_closed(db_url: str) -> None:
    """状态字段、provider ref 与复合租户 FK 均由真实 PG 拒绝旁路。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    now = datetime(2026, 8, 11, 3, tzinfo=UTC)
    base = {
        "tenant": "tn_tool_db_guard",
        "call": "tcl_00000000000000000000000010",
        "actor": "usr_guard",
        "now": now,
    }
    received = text(
        "INSERT INTO tool_calls "
        "(tenant_id,tool_call_id,tool_id,tool_version,risk_level,cost_class,status,"
        "attempt_count,user_id,created_at,updated_at) VALUES "
        "(:tenant,:call,'email.send','v1','high','low','received',0,:actor,:now,:now)"
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(received, base)

        await _assert_statement_integrity_rejected(
            engine,
            text(
                "INSERT INTO tool_calls "
                "(tenant_id,tool_call_id,tool_id,tool_version,risk_level,cost_class,"
                "idempotency_key,request_fingerprint,fingerprint_version,status,"
                "attempt_count,user_id,created_at,updated_at) VALUES "
                "(:tenant,'tcl_00000000000000000000000011','email.send','v1','high',"
                "'low','guard-key',repeat('a',64),'fp-v1','claimed',1,:actor,:now,:now)"
            ),
            base,
            "CLAIMED 缺 lease 应被 CHECK 拒绝",
        )
        await _assert_statement_integrity_rejected(
            engine,
            text(
                "INSERT INTO tool_calls "
                "(tenant_id,tool_call_id,tool_id,tool_version,risk_level,cost_class,"
                "idempotency_key,request_fingerprint,fingerprint_version,status,"
                "attempt_count,user_id,provider_ref,created_at,updated_at,completed_at) "
                "VALUES (:tenant,'tcl_00000000000000000000000012','email.send','v1',"
                "'high','low','guard-key-2',repeat('b',64),'fp-v1','succeeded',1,"
                ":actor,'buyer@example.com',:now,:now,:now)"
            ),
            base,
            "原始地址 provider_ref 应被 CHECK 拒绝",
        )
        await _assert_deferred_integrity_rejected(
            engine,
            text(
                "INSERT INTO tool_calls "
                "(tenant_id,tool_call_id,tool_id,tool_version,risk_level,cost_class,"
                "request_fingerprint,fingerprint_version,status,duplicate_of,attempt_count,"
                "user_id,created_at,updated_at,completed_at) VALUES "
                "('tn_tool_db_other','tcl_00000000000000000000000013','email.send','v1',"
                "'high','low',repeat('a',64),'fp-v1','duplicate',:call,0,:actor,:now,"
                ":now,:now)"
            ),
            base,
            "fk_tool_calls_duplicate",
            "跨租户 duplicate self-FK 应被拒绝",
        )
        await _assert_deferred_integrity_rejected(
            engine,
            text(
                "INSERT INTO tool_call_events "
                "(tenant_id,event_id,tool_call_id,stage,outcome,actor_id,occurred_at,"
                "duration_ms) VALUES ('tn_tool_db_other',"
                "'tce_00000000000000000000000010',:call,'tenant','allowed',:actor,"
                ":now,0)"
            ),
            base,
            "fk_tool_call_events_call",
            "跨租户 event FK 应被拒绝",
        )
        await _assert_statement_integrity_rejected(
            engine,
            text(
                "INSERT INTO tool_call_events "
                "(tenant_id,event_id,tool_call_id,stage,outcome,actor_id,occurred_at,"
                "duration_ms,cost_note) VALUES (:tenant,"
                "'tce_00000000000000000000000011',:call,'tenant','allowed',:actor,"
                ":now,0,'secret_customer_text')"
            ),
            base,
            "自由客户文本不得进入 tool call event",
        )
        await _assert_statement_integrity_rejected(
            engine,
            text(
                "UPDATE tool_calls SET status='corrupt' "
                "WHERE tenant_id=:tenant AND tool_call_id=:call"
            ),
            base,
            "未知 persisted status 应被拒绝",
        )
    finally:
        await engine.dispose()


async def test_0011_outreach_send_claim_roundtrip_and_state_guard(db_url: str) -> None:
    """0011 的 claim 证据可往返，非法状态组合由真实 PostgreSQL 拒绝。"""
    import importlib

    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        async with engine.connect() as conn:
            before_columns = await conn.run_sync(
                lambda sync: {
                    str(item["name"])
                    for item in inspect(sync).get_columns("outreach_message_attempts")
                }
            )
            before_checks = await conn.run_sync(
                lambda sync: {
                    str(item["name"]): str(item["sqltext"])
                    for item in inspect(sync).get_check_constraints(
                        "outreach_message_attempts"
                    )
                }
            )
        assert "send_claimed_at" in before_columns
        state_check = before_checks["ck_outreach_attempt_state_fields"]
        for value in (
            "sending",
            "rate_limited",
            "provider_auth_required",
            "provider_permanent",
        ):
            assert value in state_check

        _run_alembic(db_url, "downgrade", "0010")
        async with engine.connect() as conn:
            downgraded = await conn.run_sync(
                lambda sync: {
                    str(item["name"])
                    for item in inspect(sync).get_columns("outreach_message_attempts")
                }
            )
        assert "send_claimed_at" not in downgraded
        _run_alembic(db_url, "upgrade", "0011")

        async with engine.connect() as conn:
            restored_0011 = await conn.run_sync(
                lambda sync: {
                    str(item["name"])
                    for item in inspect(sync).get_columns("outreach_message_attempts")
                }
            )
        assert "send_claimed_at" in restored_0011
        _run_alembic(db_url, "upgrade", "head")

        helper = importlib.import_module("tests.integration.test_outreach_send_claim")
        seeded_engine, _service, _actor, attempt, _audit = await helper._ready(
            db_url, suffix="migration-guard"
        )
        await seeded_engine.dispose()
        params = {
            "tenant": str(attempt.tenant_id),
            "attempt": str(attempt.attempt_id),
            "now": datetime(2026, 8, 11, 5, tzinfo=UTC),
        }
        await _assert_statement_integrity_rejected(
            engine,
            text(
                "UPDATE outreach_message_attempts SET state='sending' "
                "WHERE tenant_id=:tenant AND attempt_id=:attempt"
            ),
            params,
            "SENDING 缺 send_claimed_at 必须被拒绝",
        )
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE outreach_message_attempts SET state='sending', "
                    "send_claimed_at=:now WHERE tenant_id=:tenant "
                    "AND attempt_id=:attempt"
                ),
                params,
            )
        await _assert_statement_integrity_rejected(
            engine,
            text(
                "UPDATE outreach_message_attempts SET state='failed_transient', "
                "failure_category='provider_permanent' "
                "WHERE tenant_id=:tenant AND attempt_id=:attempt"
            ),
            params,
            "临时失败不得携带永久类别",
        )
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_0012_email_feedback_schema_and_roundtrip(db_url: str) -> None:
    """0012 四表和 Attempt correlation 必须 0012→0011→0012 精确恢复。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        _run_alembic(db_url, "downgrade", "0012")
        async with engine.connect() as conn:
            revision = await conn.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        assert revision == "0012"
        assert set(EMAIL_FEEDBACK_TABLES) <= await _table_names(engine)
        attempt_columns = await _columns(engine, "outreach_message_attempts")
        assert {"deterministic_message_id", "idempotency_header"} <= attempt_columns
        async with engine.connect() as conn:
            attempt_contract = await conn.run_sync(
                lambda sync: {
                    "checks": {
                        str(item["name"])
                        for item in inspect(sync).get_check_constraints(
                            "outreach_message_attempts"
                        )
                    },
                    "indexes": {
                        str(item["name"]): tuple(item["column_names"])
                        for item in inspect(sync).get_indexes(
                            "outreach_message_attempts"
                        )
                        if not item.get("duplicates_constraint")
                    },
                }
            )
        assert {
            "ck_outreach_attempt_correlation_pair",
            "ck_outreach_attempt_correlation_grammar",
        } <= cast(set[str], attempt_contract["checks"])
        assert attempt_contract["indexes"] == {
            "ix_outreach_attempts_tenant_enrollment_created": (
                "tenant_id",
                "enrollment_id",
                "created_at",
                "attempt_id",
            ),
            "uq_outreach_attempts_tenant_message_id": (
                "tenant_id",
                "deterministic_message_id",
            ),
            "uq_outreach_attempts_tenant_idempotency_header": (
                "tenant_id",
                "idempotency_header",
            ),
        }
        async with engine.connect() as conn:
            contract, triggers, functions = await conn.run_sync(
                _sync_email_feedback_contract
            )
        assert contract == {
            "email_feedback_cursors": {
                "columns": {
                    "tenant_id",
                    "mailbox_alias",
                    "provider_cursor",
                    "version",
                    "bootstrap_started_at",
                    "last_succeeded_at",
                },
                "constraints": {
                    "pk_email_feedback_cursors",
                    "ck_email_feedback_cursor_tenant",
                    "ck_email_feedback_cursor_mailbox",
                    "ck_email_feedback_cursor_version",
                    "ck_email_feedback_cursor_value",
                },
                "indexes": {},
            },
            "email_feedback_receipts": {
                "columns": {
                    "tenant_id",
                    "mailbox_alias",
                    "provider_event_id",
                    "ordinal",
                    "kind",
                    "occurred_at",
                    "result",
                    "attempt_id",
                    "enrollment_id",
                    "account_id",
                    "contact_point_id",
                    "sending_identity_id",
                    "created_at",
                },
                "constraints": {
                    "pk_email_feedback_receipts",
                    "fk_email_feedback_receipts_cursor",
                    "fk_email_feedback_receipts_attempt",
                    "ck_email_feedback_receipt_tenant",
                    "ck_email_feedback_receipt_mailbox",
                    "ck_email_feedback_receipt_event",
                    "ck_email_feedback_receipt_ordinal",
                    "ck_email_feedback_receipt_kind",
                    "ck_email_feedback_receipt_result",
                    "ck_email_feedback_receipt_target",
                },
                "indexes": {
                    "ix_email_feedback_receipts_tenant_mailbox_created": (
                        "tenant_id",
                        "mailbox_alias",
                        "created_at",
                        "provider_event_id",
                    )
                },
            },
            "email_feedback_quarantines": {
                "columns": {
                    "tenant_id",
                    "mailbox_alias",
                    "provider_event_id",
                    "reason",
                    "provider_ref_digest",
                    "created_at",
                },
                "constraints": {
                    "pk_email_feedback_quarantines",
                    "fk_email_feedback_quarantine_receipt",
                    "ck_email_feedback_quarantine_reason",
                    "ck_email_feedback_quarantine_digest",
                },
                "indexes": {
                    "ix_email_feedback_quarantines_tenant_created": (
                        "tenant_id",
                        "created_at",
                        "provider_event_id",
                    )
                },
            },
            "unsubscribe_tokens": {
                "columns": {
                    "tenant_id",
                    "nonce_sha256",
                    "contact_point_id",
                    "message_attempt_id",
                    "key_id",
                    "expires_at",
                    "consumed_at",
                    "created_at",
                },
                "constraints": {
                    "pk_unsubscribe_tokens",
                    "fk_unsubscribe_token_attempt",
                    "ck_unsubscribe_token_tenant",
                    "ck_unsubscribe_token_nonce",
                    "ck_unsubscribe_token_contact",
                    "ck_unsubscribe_token_attempt",
                    "ck_unsubscribe_token_key",
                    "ck_unsubscribe_token_expiry",
                    "ck_unsubscribe_token_consumed",
                },
                "indexes": {
                    "ix_unsubscribe_tokens_tenant_attempt": (
                        "tenant_id",
                        "message_attempt_id",
                        "created_at",
                    )
                },
            },
        }
        assert triggers == {
            "email_feedback_cursors": {"trg_email_feedback_cursors_guard"},
            "email_feedback_receipts": {"trg_email_feedback_receipts_append_only"},
            "email_feedback_quarantines": {
                "trg_email_feedback_quarantines_append_only"
            },
            "unsubscribe_tokens": {"trg_unsubscribe_tokens_guard"},
        }
        assert functions == {
            "guard_email_feedback_append_only",
            "guard_email_feedback_cursor_update",
            "guard_unsubscribe_token_update",
        }

        _run_alembic(db_url, "downgrade", "0011")
        assert set(EMAIL_FEEDBACK_TABLES).isdisjoint(await _table_names(engine))
        attempt_columns = await _columns(engine, "outreach_message_attempts")
        assert {"deterministic_message_id", "idempotency_header"}.isdisjoint(
            attempt_columns
        )

        _run_alembic(db_url, "upgrade", "head")
        assert set(EMAIL_FEEDBACK_TABLES) <= await _table_names(engine)
        assert {"deterministic_message_id", "idempotency_header"} <= await _columns(
            engine, "outreach_message_attempts"
        )
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_0013_receipt_fingerprint_schema_and_roundtrip(db_url: str) -> None:
    """已应用 0012 的数据库必须经 0013 显式获得安全 payload fingerprint。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        async with engine.connect() as conn:
            revision = await conn.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            columns = await conn.run_sync(
                lambda sync: {
                    str(item["name"]): item
                    for item in inspect(sync).get_columns(
                        "email_feedback_receipts"
                    )
                }
            )
            constraints = await conn.run_sync(
                lambda sync: {
                    str(item["name"])
                    for item in inspect(sync).get_check_constraints(
                        "email_feedback_receipts"
                    )
                }
            )
        assert revision == "0018"
        assert "item_fingerprint" in await _columns(engine, "email_feedback_receipts")
        assert columns["item_fingerprint"]["nullable"] is False
        assert columns["item_fingerprint"]["default"] is None
        assert "ck_email_feedback_receipt_fingerprint" in constraints

        _run_alembic(db_url, "downgrade", "0012")
        assert "item_fingerprint" not in await _columns(
            engine, "email_feedback_receipts"
        )
        legacy_tenant = "tn_01KZX4C1000000000000000013"
        legacy_mailbox = "feedback-legacy"
        legacy_event = "d" * 64
        legacy_now = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO email_feedback_cursors "
                    "(tenant_id,mailbox_alias,provider_cursor,version,"
                    "bootstrap_started_at,last_succeeded_at) "
                    "VALUES (:tenant,:mailbox,NULL,0,:now,NULL)"
                ),
                {
                    "tenant": legacy_tenant,
                    "mailbox": legacy_mailbox,
                    "now": legacy_now,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO email_feedback_receipts "
                    "(tenant_id,mailbox_alias,provider_event_id,ordinal,kind,"
                    "occurred_at,result,created_at) VALUES "
                    "(:tenant,:mailbox,:event,0,'unparseable',:now,"
                    "'quarantined',:now)"
                ),
                {
                    "tenant": legacy_tenant,
                    "mailbox": legacy_mailbox,
                    "event": legacy_event,
                    "now": legacy_now,
                },
            )

        _run_alembic(db_url, "upgrade", "head")
        assert "item_fingerprint" in await _columns(engine, "email_feedback_receipts")
        async with engine.connect() as conn:
            assert (
                await conn.scalar(
                    text(
                        "SELECT item_fingerprint FROM email_feedback_receipts "
                        "WHERE tenant_id=:tenant AND mailbox_alias=:mailbox "
                        "AND provider_event_id=:event"
                    ),
                    {
                        "tenant": legacy_tenant,
                        "mailbox": legacy_mailbox,
                        "event": legacy_event,
                    },
                )
                == "0" * 64
            )
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def _artifact_insert_rejected(
    engine: AsyncEngine, statement: TextClause, values: dict[str, object]
) -> None:
    with pytest.raises((DBAPIError, IntegrityError)):
        async with engine.begin() as conn:
            await conn.execute(statement, values)


async def test_artifact_store_0014_roundtrip_and_guards(db_url: str) -> None:
    """0014 只增加两张 tenant metadata 表，并由真实 PostgreSQL 拒绝非法记录。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    tenant = "tn_01KZX4C1000000000000000014"
    raw_artifact = "art_01KZX4C1000000000000000014"
    generated_artifact = "art_01KZX4C1000000000000000015"
    run_id = "run_01KZX4C1000000000000000014"
    enrollment_id = "enr_01KZX4C1000000000000000014"
    raw_values: dict[str, object] = {
        "tenant": tenant,
        "artifact": raw_artifact,
        "kind": "pdf",
        "hash": "a" * 64,
        "size": 32,
        "mime": "application/pdf",
        "object_key": f"raw/{tenant}/{raw_artifact}",
        "uploader": "usr_01KZX4C1000000000000000014",
        "occurred": _NOW,
    }
    generated_values: dict[str, object] = {
        "tenant": tenant,
        "artifact": generated_artifact,
        "kind": "email_draft",
        "hash": "b" * 64,
        "size": 64,
        "mime": "application/vnd.tradeos.email-draft+json",
        "object_key": f"generated/{tenant}/{generated_artifact}",
        "run_id": run_id,
        "subject_ref": enrollment_id,
        "sequence": 1,
        "key": f"{enrollment_id}:1:draft",
        "generated_by": "outreach_agent_v1",
        "occurred": _NOW,
    }
    insert_raw = text(
        "INSERT INTO raw_artifacts "
        "(tenant_id,artifact_id,kind,content_hash,size_bytes,mime_type,"
        "object_key,uploaded_by,uploaded_at) VALUES "
        "(:tenant,:artifact,:kind,:hash,:size,:mime,:object_key,:uploader,:occurred)"
    )
    insert_generated = text(
        "INSERT INTO artifacts "
        "(tenant_id,artifact_id,kind,content_hash,size_bytes,mime_type,"
        "object_key,workflow_run_id,subject_ref,sequence_number,idempotency_key,"
        "generated_by,generated_at) VALUES "
        "(:tenant,:artifact,:kind,:hash,:size,:mime,:object_key,:run_id,"
        ":subject_ref,:sequence,:key,:generated_by,:occurred)"
    )
    try:
        async with engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            contract = await conn.run_sync(
                lambda sync: {
                    table: {
                        "columns": {
                            str(item["name"])
                            for item in inspect(sync).get_columns(table)
                        },
                        "pk": str(inspect(sync).get_pk_constraint(table)["name"]),
                        "unique": {
                            str(item["name"])
                            for item in inspect(sync).get_unique_constraints(table)
                        },
                        "checks": {
                            str(item["name"])
                            for item in inspect(sync).get_check_constraints(table)
                        },
                        "indexes": {
                            str(item["name"])
                            for item in inspect(sync).get_indexes(table)
                            if not item.get("duplicates_constraint")
                        },
                    }
                    for table in ARTIFACT_TABLES
                }
            )
        assert revision == "0018"
        assert contract == {
            "raw_artifacts": {
                "columns": {
                    "tenant_id",
                    "artifact_id",
                    "kind",
                    "content_hash",
                    "size_bytes",
                    "mime_type",
                    "object_key",
                    "uploaded_by",
                    "uploaded_at",
                },
                "pk": "pk_raw_artifacts",
                "unique": {"uq_raw_artifacts_tenant_kind_hash"},
                "checks": {
                    "ck_raw_artifacts_tenant",
                    "ck_raw_artifacts_id",
                    "ck_raw_artifacts_hash",
                    "ck_raw_artifacts_size",
                    "ck_raw_artifacts_kind_mime",
                    "ck_raw_artifacts_object_key",
                    "ck_raw_artifacts_uploader",
                },
                "indexes": set(),
            },
            "artifacts": {
                "columns": {
                    "tenant_id",
                    "artifact_id",
                    "kind",
                    "content_hash",
                    "size_bytes",
                    "mime_type",
                    "object_key",
                    "workflow_run_id",
                    "subject_ref",
                    "sequence_number",
                    "idempotency_key",
                    "generated_by",
                    "generated_at",
                },
                "pk": "pk_artifacts",
                "unique": {"uq_artifacts_tenant_key"},
                "checks": {
                    "ck_artifacts_tenant",
                    "ck_artifacts_id",
                    "ck_artifacts_hash",
                    "ck_artifacts_size",
                    "ck_artifacts_kind_mime",
                    "ck_artifacts_object_key",
                    "ck_artifacts_run",
                    "ck_artifacts_subject",
                    "ck_artifacts_sequence",
                    "ck_artifacts_idempotency",
                    "ck_artifacts_generated_by",
                },
                "indexes": set(),
            },
        }

        async with engine.begin() as conn:
            await conn.execute(insert_raw, raw_values)
            await conn.execute(insert_generated, generated_values)

        for changed in (
            {"hash": "A" * 64, "artifact": "art_01KZX4C1000000000000000016"},
            {"size": 0, "artifact": "art_01KZX4C1000000000000000017"},
            {
                "kind": "pdf",
                "mime": "text/html",
                "artifact": "art_01KZX4C1000000000000000018",
            },
            {
                "object_key": "raw/other/art_01KZX4C1000000000000000019",
                "artifact": "art_01KZX4C1000000000000000019",
            },
        ):
            await _artifact_insert_rejected(
                engine, insert_raw, raw_values | changed
            )
        for changed in (
            {"hash": "G" * 64, "artifact": "art_01KZX4C1000000000000000020"},
            {"size": 0, "artifact": "art_01KZX4C1000000000000000021"},
            {
                "kind": "email_draft",
                "mime": "message/rfc822",
                "artifact": "art_01KZX4C1000000000000000022",
            },
            {
                "object_key": "generated/other/art_01KZX4C1000000000000000023",
                "artifact": "art_01KZX4C1000000000000000023",
            },
            {"run_id": "run_bad", "artifact": "art_01KZX4C1000000000000000024"},
            {
                "subject_ref": "enr_bad",
                "key": "enr_bad:1:draft",
                "artifact": "art_01KZX4C1000000000000000025",
            },
            {
                "key": f"{enrollment_id}:2:draft",
                "artifact": "art_01KZX4C1000000000000000026",
            },
            {"artifact": "art_01KZX4C1000000000000000027"},
        ):
            await _artifact_insert_rejected(
                engine, insert_generated, generated_values | changed
            )

        _run_alembic(db_url, "downgrade", "0013")
        assert set(ARTIFACT_TABLES).isdisjoint(await _table_names(engine))
        assert "email_feedback_receipts" in await _table_names(engine)
        _run_alembic(db_url, "upgrade", "head")
        assert set(ARTIFACT_TABLES) <= await _table_names(engine)
        await _artifact_insert_rejected(
            engine,
            insert_raw,
            raw_values | {"hash": "A" * 64},
        )
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_0015_notification_jobs_roundtrip(db_url: str) -> None:
    """0015 的表、列、索引、约束和 FK 可精确回退并恢复。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        async with engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            def inspect_contract(sync):
                inspector = inspect(sync)
                return set(inspector.get_table_names()), {
                    table: {
                        "columns": {str(item["name"]) for item in inspector.get_columns(table)},
                        "checks": {str(item["name"]) for item in inspector.get_check_constraints(table)},
                        "unique": {str(item["name"]) for item in inspector.get_unique_constraints(table)},
                        "fks": {str(item["name"]): item for item in inspector.get_foreign_keys(table)},
                        "indexes": {str(item["name"]) for item in inspector.get_indexes(table) if not item.get("duplicates_constraint")},
                    }
                    for table in ("notification_jobs", "in_app_notifications")
                }
            names, contract = await conn.run_sync(inspect_contract)
        assert revision == "0018"
        assert {"notification_jobs", "in_app_notifications"} <= names
        assert {"status", "available_at", "lease_token", "last_error"} <= contract["notification_jobs"]["columns"]
        assert {"ck_notification_jobs_status", "ck_notification_jobs_priority", "ck_notification_jobs_attempt_count"} <= contract["notification_jobs"]["checks"]
        assert "uq_notification_jobs_source_recipient_kind" in contract["notification_jobs"]["unique"]
        assert "ix_notification_jobs_tenant_due" in contract["notification_jobs"]["indexes"]
        fk = contract["in_app_notifications"]["fks"]["fk_in_app_notifications_job"]
        assert tuple(fk["constrained_columns"]) == ("tenant_id", "source_job_id")
        assert fk["referred_table"] == "notification_jobs"
        assert tuple(fk["referred_columns"]) == ("tenant_id", "notification_job_id")
        assert fk["options"]["ondelete"] == "RESTRICT"
        assert "uq_in_app_notifications_source_job" in contract["in_app_notifications"]["unique"]
        assert "ck_in_app_notifications_priority" in contract["in_app_notifications"]["checks"]
        assert "ix_in_app_notifications_recipient_created" in contract["in_app_notifications"]["indexes"]
        for status, priority, attempts in (("invalid", "urgent", 0), ("pending", "invalid", 0), ("pending", "urgent", -1)):
            with pytest.raises((DBAPIError, IntegrityError)):
                async with engine.begin() as conn:
                    await conn.execute(text("INSERT INTO notification_jobs (tenant_id,notification_job_id,source_event_fingerprint,source_event,recipient_employee_id,priority,context_kind,primary_id,dedup_key,status,available_at,attempt_count,created_at) VALUES ('tn_guard','njb_guard_' || :status,:fp,'Event','emp',:priority,'handoff_escalation','han','key',:status,now(),:attempts,now())"), {"status": status, "priority": priority, "attempts": attempts, "fp": status[0] * 64})
        with pytest.raises((DBAPIError, IntegrityError)):
            async with engine.begin() as conn:
                await conn.execute(text("INSERT INTO in_app_notifications (tenant_id,notification_id,recipient_employee_id,priority,title,context_kind,primary_id,source_job_id,created_at) VALUES ('tn_guard','not_orphan','emp','urgent','title','handoff_escalation','han','njb_missing',now())"))
        _run_alembic(db_url, "downgrade", "0014")
        assert {"notification_jobs", "in_app_notifications"}.isdisjoint(await _table_names(engine))
        _run_alembic(db_url, "upgrade", "0015")
        assert {"notification_jobs", "in_app_notifications"} <= await _table_names(engine)
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_0012_email_feedback_database_guards(db_url: str) -> None:
    """真实 PG 锁定 append-only、cursor 单步、token 单向和安全词表。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    tenant = "tn_01KZX4C1000000000000000001"
    mailbox = "feedback-primary"
    event = "a" * 64
    now = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO email_feedback_cursors "
                    "(tenant_id,mailbox_alias,provider_cursor,version,"
                    "bootstrap_started_at,last_succeeded_at) "
                    "VALUES (:tenant,:mailbox,NULL,0,:now,NULL)"
                ),
                {"tenant": tenant, "mailbox": mailbox, "now": now},
            )
            await conn.execute(
                text(
                    "INSERT INTO email_feedback_receipts "
                    "(tenant_id,mailbox_alias,provider_event_id,item_fingerprint,ordinal,kind,"
                    "occurred_at,result,created_at) VALUES "
                    "(:tenant,:mailbox,:event,:fingerprint,0,'unparseable',:now,'quarantined',:now)"
                ),
                {
                    "tenant": tenant,
                    "mailbox": mailbox,
                    "event": event,
                    "fingerprint": "f" * 64,
                    "now": now,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO email_feedback_quarantines "
                    "(tenant_id,mailbox_alias,provider_event_id,reason,"
                    "provider_ref_digest,created_at) VALUES "
                    "(:tenant,:mailbox,:event,'malformed',:digest,:now)"
                ),
                {
                    "tenant": tenant,
                    "mailbox": mailbox,
                    "event": event,
                    "digest": "b" * 64,
                    "now": now,
                },
            )
        for table in ("email_feedback_receipts", "email_feedback_quarantines"):
            await _assert_statement_integrity_rejected(
                engine,
                text(
                    f"UPDATE {table} SET created_at=created_at + interval '1 second' "
                    "WHERE tenant_id=:tenant"
                ),
                {"tenant": tenant},
                f"{table} UPDATE 必须被 append-only trigger 拒绝",
            )
            await _assert_statement_integrity_rejected(
                engine,
                text(f"DELETE FROM {table} WHERE tenant_id=:tenant"),
                {"tenant": tenant},
                f"{table} DELETE 必须被 append-only trigger 拒绝",
            )
        await _assert_statement_integrity_rejected(
            engine,
            text(
                "UPDATE email_feedback_cursors SET provider_cursor='jump', version=2 "
                "WHERE tenant_id=:tenant AND mailbox_alias=:mailbox"
            ),
            {"tenant": tenant, "mailbox": mailbox},
            "cursor version 只能精确 +1",
        )
        await _assert_statement_integrity_rejected(
            engine,
            text(
                "DELETE FROM email_feedback_cursors "
                "WHERE tenant_id=:tenant AND mailbox_alias=:mailbox"
            ),
            {"tenant": tenant, "mailbox": mailbox},
            "cursor 不可删除",
        )
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE email_feedback_cursors SET provider_cursor='next', "
                    "version=version+1,last_succeeded_at=:now "
                    "WHERE tenant_id=:tenant AND mailbox_alias=:mailbox"
                ),
                {"tenant": tenant, "mailbox": mailbox, "now": now},
            )
        for bad_mailbox in ("Customer@Example.com", "UPPER", "-leading"):
            await _assert_statement_integrity_rejected(
                engine,
                text(
                    "INSERT INTO email_feedback_cursors "
                    "(tenant_id,mailbox_alias,provider_cursor,version,bootstrap_started_at) "
                    "VALUES (:tenant,:mailbox,NULL,0,:now)"
                ),
                {"tenant": tenant, "mailbox": bad_mailbox, "now": now},
                "mailbox alias 只能是安全 operator label",
            )
        await _assert_statement_integrity_rejected(
            engine,
            text(
                "INSERT INTO email_feedback_receipts "
                "(tenant_id,mailbox_alias,provider_event_id,item_fingerprint,ordinal,kind,"
                "occurred_at,result,created_at) VALUES "
                "(:tenant,:mailbox,:event,:fingerprint,0,'unparseable',:now,"
                "'quarantined',:now)"
            ),
            {
                "tenant": tenant,
                "mailbox": mailbox,
                "event": "A" * 64,
                "fingerprint": "f" * 64,
                "now": now,
            },
            "provider event ID 必须 lower-hex",
        )
        await _assert_statement_integrity_rejected(
            engine,
            text(
                "INSERT INTO email_feedback_receipts "
                "(tenant_id,mailbox_alias,provider_event_id,item_fingerprint,ordinal,kind,"
                "occurred_at,result,created_at) VALUES "
                "(:tenant,:mailbox,:event,:fingerprint,0,'unparseable',:now,"
                "'quarantined',:now)"
            ),
            {
                "tenant": tenant,
                "mailbox": mailbox,
                "event": "c" * 64,
                "fingerprint": "short",
                "now": now,
            },
            "item fingerprint 必须 lower-hex 64 字符",
        )
    finally:
        await engine.dispose()


async def test_0016_authentication_check_requests_roundtrip_and_guards(
    db_url: str,
) -> None:
    """0016→0015→0016 保留唯一/FK/状态/不可变边界。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    tenant = "tn_01K2C5R6J7ABCDEFGHJKMNPQRS"
    identity = "sid_01K2C5R6J7ABCDEFGHJKMNPQRS"
    request = "acr_01K2C5R6J7ABCDEFGHJKMNPQRS"
    now = datetime(2026, 8, 14, 12, tzinfo=UTC)
    try:
        async with engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "0018", "RED：0017 投诉迁移尚未创建"

            def inspect_contract(sync):
                inspector = inspect(sync)
                return {
                    "tables": set(inspector.get_table_names()),
                    "columns": {
                        str(item["name"])
                        for item in inspector.get_columns(
                            "sending_auth_check_requests"
                        )
                    },
                    "checks": {
                        str(item["name"])
                        for item in inspector.get_check_constraints(
                            "sending_auth_check_requests"
                        )
                    },
                    "unique": {
                        str(item["name"])
                        for item in inspector.get_unique_constraints(
                            "sending_auth_check_requests"
                        )
                    },
                    "fks": {
                        str(item["name"]): item
                        for item in inspector.get_foreign_keys(
                            "sending_auth_check_requests"
                        )
                    },
                }

            contract = await conn.run_sync(inspect_contract)
        assert "sending_auth_check_requests" in contract["tables"]
        assert {
            "tenant_id",
            "request_id",
            "sending_identity_id",
            "request_key",
            "status",
            "requested_at",
            "completed_at",
        } == contract["columns"]
        assert {
            "ck_sending_auth_request_status",
            "ck_sending_auth_request_completion",
            "ck_sending_auth_request_ids",
            "ck_sending_auth_request_key",
        } <= contract["checks"]
        assert "uq_sending_auth_request_tenant_key" in contract["unique"]
        fk = contract["fks"]["fk_sending_auth_request_identity"]
        assert tuple(fk["constrained_columns"]) == (
            "tenant_id",
            "sending_identity_id",
        )
        assert tuple(fk["referred_columns"]) == ("tenant_id", "identity_id")

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO sending_domains(tenant_id,domain,role,created_at) "
                    "VALUES (:tenant,'cold.example.com','cold_outreach',:now) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"tenant": tenant, "now": now},
            )
            await conn.execute(
                text(
                    "INSERT INTO sending_identities(tenant_id,identity_id,domain,address,state,"
                    "throttle_hard_bounce_rate,suspend_hard_bounce_rate,"
                    "throttle_complaint_rate,suspend_complaint_rate,suspend_on_spam_trap,"
                    "suspend_on_blocklist,minimum_sample,created_at) VALUES "
                    "(:tenant,:identity,'cold.example.com','sales@cold.example.com','auth_pending',"
                    "0.03,0.05,0.001,0.003,true,true,50,:now) ON CONFLICT DO NOTHING"
                ),
                {"tenant": tenant, "identity": identity, "now": now},
            )
            await conn.execute(
                text(
                    "INSERT INTO sending_auth_check_requests "
                    "(tenant_id,request_id,sending_identity_id,request_key,status,requested_at) "
                    "VALUES (:tenant,:request,:identity,'request-key','requested',:now)"
                ),
                {
                    "tenant": tenant,
                    "request": request,
                    "identity": identity,
                    "now": now,
                },
            )
        for statement in (
            (
                "UPDATE sending_auth_check_requests SET sending_identity_id="
                "'sid_01K2C5R6J7ABCDEFGHJKMNPQRT' WHERE tenant_id=:tenant"
            ),
            (
                "UPDATE sending_auth_check_requests SET request_key='other-key' "
                "WHERE tenant_id=:tenant"
            ),
            (
                "UPDATE sending_auth_check_requests SET status='succeeded' "
                "WHERE tenant_id=:tenant"
            ),
            "DELETE FROM sending_auth_check_requests WHERE tenant_id=:tenant",
        ):
            with pytest.raises((DBAPIError, IntegrityError)):
                async with engine.begin() as conn:
                    await conn.execute(text(statement), {"tenant": tenant})

        _run_alembic(db_url, "downgrade", "0015")
        assert "sending_auth_check_requests" not in await _table_names(engine)
        _run_alembic(db_url, "upgrade", "0016")
        assert "sending_auth_check_requests" in await _table_names(engine)
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_0017_email_complaints_schema_and_roundtrip(db_url: str) -> None:
    """0017→0016→0017：complaint 进入 receipt kind/target 词表并可逆恢复。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    tenant = "tn_01KZX4C1000000000000000017"
    mailbox = "feedback-complaints"
    identity = "sid_01KZX4C1000000000000000017"
    campaign = "cmp_01KZX4C1000000000000000017"
    enrollment = "enr_01KZX4C1000000000000000017"
    attempt = "mat_01KZX4C1000000000000000017"
    account = "acc_01KZX4C1000000000000000017"
    contact = "cp_01KZX4C1000000000000000017"
    message = "msg_01KZX4C1000000000000000017"
    approval = "apr_01KZX4C1000000000000000017"
    employee = "emp_01KZX4C1000000000000000017"
    now = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)
    try:
        async with engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "0018", "RED：0017 投诉迁移尚未创建"
            for constraint_name in (
                "ck_email_feedback_receipt_kind",
                "ck_email_feedback_receipt_target",
            ):
                definition = await conn.scalar(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname=:name"
                    ),
                    {"name": constraint_name},
                )
                assert "complaint" in definition, f"{constraint_name} 缺少 complaint"

        async def seed_chain() -> None:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO sending_domains(tenant_id,domain,role,created_at) "
                        "VALUES (:tenant,'complaints.example.com','cold_outreach',:now) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"tenant": tenant, "now": now},
                )
                await conn.execute(
                    text(
                        "INSERT INTO sending_identities(tenant_id,identity_id,domain,"
                        "address,display_name,state,connector_ref,warmup_started_on,"
                        "target_daily_volume,activated_at,suspended_at,retired_at,"
                        "sendable_state_before_restriction,suspension_category,version,"
                        "throttle_hard_bounce_rate,suspend_hard_bounce_rate,"
                        "throttle_complaint_rate,suspend_complaint_rate,"
                        "suspend_on_spam_trap,suspend_on_blocklist,minimum_sample,"
                        "created_at) VALUES "
                        "(:tenant,:identity,'complaints.example.com',"
                        "'complaints@complaints.example.com',NULL,'active',"
                        "'complaints_connector_ref',:day,50,:now,NULL,NULL,NULL,NULL,1,"
                        "0.03,0.05,0.001,0.003,true,true,50,:now) ON CONFLICT DO NOTHING"
                    ),
                    {
                        "tenant": tenant,
                        "identity": identity,
                        "day": (now - timedelta(days=40)).date(),
                        "now": now,
                    },
                )
                await conn.execute(
                    text(
                        "INSERT INTO outreach_campaigns(tenant_id,campaign_id,state,"
                        "current_version,created_by,created_at,round_robin_cursor,"
                        "approval_id,approved_by,approved_at,paused_reason) VALUES "
                        "(:tenant,:campaign,'active',1,:employee,:now,0,:approval,"
                        ":employee,:now,NULL) ON CONFLICT DO NOTHING"
                    ),
                    {
                        "tenant": tenant,
                        "campaign": campaign,
                        "employee": employee,
                        "approval": approval,
                        "now": now,
                    },
                )
                await conn.execute(
                    text(
                        "INSERT INTO outreach_campaign_versions(tenant_id,campaign_id,"
                        "version,name,markets,target_entity_types,allowed_categories,"
                        "sender_identity_ids,daily_new_contact_limit,"
                        "daily_total_message_limit,handoff_triggers,stop_on_reply,"
                        "created_by,created_at) VALUES "
                        "(:tenant,:campaign,1,'complaints fixture',"
                        "CAST(:markets AS jsonb),CAST(:entities AS jsonb),"
                        "CAST(:categories AS jsonb),CAST(:senders AS jsonb),"
                        "10,20,CAST(:triggers AS jsonb),true,:employee,:now) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {
                        "tenant": tenant,
                        "campaign": campaign,
                        "markets": '["US"]',
                        "entities": '["business"]',
                        "categories": '["hinges"]',
                        "senders": f'["{identity}"]',
                        "triggers": '["reply"]',
                        "employee": employee,
                        "now": now,
                    },
                )
                await conn.execute(
                    text(
                        "INSERT INTO outreach_enrollments(tenant_id,enrollment_id,"
                        "campaign_id,campaign_version,account_id,contact_point_id,"
                        "sending_identity_id,state,current_step,next_send_at,"
                        "enrolled_at,stopped_at,stop_reason,idempotency_key) VALUES "
                        "(:tenant,:enrollment,:campaign,1,:account,:contact,:identity,"
                        "'in_sequence',1,:next,:now,NULL,NULL,"
                        "'complaints-migration-enrollment') ON CONFLICT DO NOTHING"
                    ),
                    {
                        "tenant": tenant,
                        "enrollment": enrollment,
                        "campaign": campaign,
                        "account": account,
                        "contact": contact,
                        "identity": identity,
                        "next": now + timedelta(days=1),
                        "now": now,
                    },
                )
                await conn.execute(
                    text(
                        "INSERT INTO outreach_message_attempts(tenant_id,attempt_id,"
                        "message_id,campaign_id,enrollment_id,campaign_version,"
                        "step_number,sending_identity_id,idempotency_key,state,"
                        "provider_ref,failure_category,created_at,updated_at,"
                        "send_claimed_at,deterministic_message_id,idempotency_header) "
                        "VALUES (:tenant,:attempt,:message,:campaign,:enrollment,1,1,"
                        ":identity,'complaints-migration-attempt','sent',"
                        "'provider_ref_complaints',NULL,:now,:now,:now,NULL,NULL) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {
                        "tenant": tenant,
                        "attempt": attempt,
                        "message": message,
                        "campaign": campaign,
                        "enrollment": enrollment,
                        "identity": identity,
                        "now": now,
                    },
                )
                await conn.execute(
                    text(
                        "INSERT INTO email_feedback_cursors(tenant_id,mailbox_alias,"
                        "provider_cursor,version,bootstrap_started_at,last_succeeded_at) "
                        "VALUES (:tenant,:mailbox,NULL,0,:now,NULL) ON CONFLICT DO NOTHING"
                    ),
                    {"tenant": tenant, "mailbox": mailbox, "now": now},
                )

        complaint_receipt = text(
            "INSERT INTO email_feedback_receipts "
            "(tenant_id,mailbox_alias,provider_event_id,item_fingerprint,ordinal,kind,"
            "occurred_at,result,attempt_id,enrollment_id,account_id,contact_point_id,"
            "sending_identity_id,created_at) VALUES "
            "(:tenant,:mailbox,:event,:fingerprint,0,'complaint',:now,'applied',"
            ":attempt,:enrollment,:account,:contact,:identity,:now)"
        )

        await seed_chain()

        # head(0017) 接受 complaint：在回滚事务内证明，不留下 append-only 行
        # （complaint receipt 在共享容器中会破坏其后 0017 以下版本的 downgrade）。
        async with engine.connect() as conn:
            transaction = await conn.begin()
            await conn.execute(
                complaint_receipt,
                {
                    "tenant": tenant,
                    "mailbox": mailbox,
                    "event": "c" * 64,
                    "fingerprint": "f" * 64,
                    "attempt": attempt,
                    "enrollment": enrollment,
                    "account": account,
                    "contact": contact,
                    "identity": identity,
                    "now": now,
                },
            )
            await transaction.rollback()

        _run_alembic(db_url, "downgrade", "0016")
        await _assert_statement_integrity_rejected(
            engine,
            complaint_receipt,
            {
                "tenant": tenant,
                "mailbox": mailbox,
                "event": "c" * 64,
                "fingerprint": "f" * 64,
                "attempt": attempt,
                "enrollment": enrollment,
                "account": account,
                "contact": contact,
                "identity": identity,
                "now": now,
            },
            "0016 词表不允许 complaint receipt",
        )

        _run_alembic(db_url, "upgrade", "head")
        # 0016→0017 恢复后再次证明 complaint 可写（同样在回滚事务内）。
        async with engine.connect() as conn:
            transaction = await conn.begin()
            await conn.execute(
                complaint_receipt,
                {
                    "tenant": tenant,
                    "mailbox": mailbox,
                    "event": "c" * 64,
                    "fingerprint": "f" * 64,
                    "attempt": attempt,
                    "enrollment": enrollment,
                    "account": account,
                    "contact": contact,
                    "identity": identity,
                    "now": now,
                },
            )
            await transaction.rollback()
        await _assert_statement_integrity_rejected(
            engine,
            text(
                "INSERT INTO email_feedback_receipts "
                "(tenant_id,mailbox_alias,provider_event_id,item_fingerprint,ordinal,"
                "kind,occurred_at,result,attempt_id,enrollment_id,account_id,"
                "contact_point_id,sending_identity_id,created_at) VALUES "
                "(:tenant,:mailbox,:event,:fingerprint,0,'bogus',:now,'applied',"
                ":attempt,:enrollment,:account,:contact,:identity,:now)"
            ),
            {
                "tenant": tenant,
                "mailbox": mailbox,
                "event": "d" * 64,
                "fingerprint": "f" * 64,
                "attempt": attempt,
                "enrollment": enrollment,
                "account": account,
                "contact": contact,
                "identity": identity,
                "now": now,
            },
            "未知 kind 必须被约束拒绝",
        )
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_0018_conversation_classifications_roundtrip_and_guards(
    db_url: str,
) -> None:
    """0018→0017→0018：分类留痕表可逆往返；表/列/PK/CHECK 与 ORM 一致。"""
    from sqlalchemy import CheckConstraint, inspect

    from infra.db.session import create_engine_from
    from infra.db.tables import ConversationClassificationRow

    engine = create_engine_from(db_url)
    try:
        async with engine.connect() as conn:
            revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "0018", "RED：0018 会话分类留痕迁移尚未创建"

            def inspect_contract(sync) -> dict[str, object]:
                inspector = inspect(sync)
                return {
                    "tables": set(inspector.get_table_names()),
                    "columns": {
                        item["name"]: str(item["type"])
                        for item in inspector.get_columns("conversation_classifications")
                    },
                    "pk": list(
                        inspector.get_pk_constraint(
                            "conversation_classifications"
                        )["constrained_columns"]
                    ),
                    "checks": {
                        item["name"]: item["sqltext"]
                        for item in inspector.get_check_constraints(
                            "conversation_classifications"
                        )
                    },
                }

            contract = await conn.run_sync(inspect_contract)
        # 与 ORM 一致：列名/类型（按 DB 方言编译；反射端与 ORM 端统一归一化
        # " WITH TIME ZONE" 后缀，避免 timestamptz 渲染差）/PK/CHECK
        def _type_key(value: str) -> str:
            return value.replace(" WITH TIME ZONE", "")

        orm_columns = {
            name: _type_key(str(column.type.compile(dialect=engine.dialect)))
            for name, column in ConversationClassificationRow.__table__.columns.items()
        }
        orm_pk = [
            column.name for column in ConversationClassificationRow.__table__.primary_key.columns
        ]
        orm_category_check = next(
            constraint.sqltext
            for constraint in ConversationClassificationRow.__table__.constraints
            if isinstance(constraint, CheckConstraint)
        )
        assert "conversation_classifications" in contract["tables"]
        assert {
            name: _type_key(str(type_value))
            for name, type_value in contract["columns"].items()
        } == orm_columns
        assert contract["pk"] == orm_pk == ["tenant_id", "message_id"]
        assert set(contract["checks"]) == {
            "ck_conversation_classifications_category"
        }
        # CHECK 语义 parity（不比较 raw SQL：Postgres 会把 IN 规范化为 ANY，
        # TextClause 不能直接等于字符串）：从 ReplyCategory 枚举取完整 14 类词表，
        # 安全解析 DB 约束定义与 ORM CheckConstraint 文本，各自包含全部且无多余类别。
        from domains.conversations.schemas import ReplyCategory

        expected_categories = {item.value for item in ReplyCategory}
        assert len(expected_categories) == 14
        db_check = contract["checks"]["ck_conversation_classifications_category"]
        orm_check = str(orm_category_check)
        db_values = set(re.findall(r"'([a-z_]+)'(?=::)", db_check))
        orm_values = set(re.findall(r"'([a-z_]+)'", orm_check))
        assert db_values == expected_categories
        assert orm_values == expected_categories

        # 往返：downgrade 0017 后表消失，upgrade head 后恢复且契约不变
        _run_alembic(db_url, "downgrade", "0017")
        assert "conversation_classifications" not in await _table_names(engine)
        _run_alembic(db_url, "upgrade", "head")
        async with engine.connect() as conn:
            restored = await conn.run_sync(inspect_contract)
        assert restored == contract
    finally:
        await engine.dispose()
