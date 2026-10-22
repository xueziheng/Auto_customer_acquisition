"""独立库验证核对分类迁移往返、ORM CHECK 一致和有数据回退保护。"""

import importlib
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateTable


def migrate(url, action, target):
    script = (
        'from alembic.config import Config; from alembic import command; '
        'c=Config(); c.set_main_option("script_location", "migrations"); '
        f'command.{action}(c, {target!r})'
    )
    p = subprocess.run(
        [sys.executable, '-B', '-c', script],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, 'DATABASE_URL': url.render_as_string(False), 'PYTHON_DOTENV_DISABLED': '1'},
        capture_output=True, timeout=120, check=False,
    )
    return p.returncode


async def test_review_migration_roundtrip_orm_and_refuses_loss(integration_engine):
    from infra.db.tables import Base

    name = 'test_reply_review_' + uuid4().hex
    async with integration_engine.connect() as conn:
        admin = await conn.execution_options(isolation_level='AUTOCOMMIT')
        await admin.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(integration_engine.url.set(database=name))
    try:
        for action, target in [('upgrade', 'head'), ('downgrade', '0077'), ('upgrade', 'head')]:
            assert migrate(engine.url, action, target) == 0

        async with engine.connect() as conn:
            transaction = await conn.begin()
            await conn.execute(text('CREATE SCHEMA review_orm'))
            names = ('conversation_classifications', 'conversation_classification_corrections', 'conversation_reply_work')
            for table in names:
                ddl = str(CreateTable(Base.metadata.tables[table]).compile(dialect=engine.dialect))
                ddl = ddl.replace(f'CREATE TABLE {table}', f'CREATE TABLE review_orm.{table}', 1)
                await conn.execute(text(ddl))
                actual = await conn.run_sync(lambda c, t=table: inspect(c).get_check_constraints(t))
                expected = await conn.run_sync(lambda c, t=table: inspect(c).get_check_constraints(t, schema='review_orm'))
                assert {r['name']: r['sqltext'] for r in actual} == {r['name']: r['sqltext'] for r in expected}
                actual_columns = await conn.run_sync(lambda c, t=table: inspect(c).get_columns(t))
                expected_columns = await conn.run_sync(lambda c, t=table: inspect(c).get_columns(t, schema='review_orm'))
                signature = lambda rows: {(r['name'], str(r['type']), r['nullable']) for r in rows}
                assert signature(actual_columns) == signature(expected_columns)
            await transaction.rollback()

        # 分别证明分类、纠正和待办任一新值都阻止回退；每例回滚不抹除外部数据。
        module = importlib.import_module('migrations.versions.0078_reply_needs_review')
        statements = [
            "INSERT INTO conversation_classifications (tenant_id,message_id,category,classified_by,classified_at,candidate_fields) VALUES ('tn_review','msg_review','needs_review','fixture',now(),'[]'::jsonb)",
            "INSERT INTO conversation_classification_corrections (tenant_id,correction_id,message_id,corrected_category,corrected_by,corrected_at) VALUES ('tn_review','ccr_review','msg_review','needs_review','fixture',now())",
        ]
        for statement in statements:
            async with engine.connect() as conn:
                transaction = await conn.begin()
                await conn.execute(text(statement))
                def refuse(c):
                    with (
                        Operations.context(MigrationContext.configure(c)),
                        pytest.raises(RuntimeError, match='已存在回复核对记录'),
                    ):
                        module.downgrade()
                await conn.run_sync(refuse)
                await transaction.rollback()

        # 工作项须经过真实会话服务创建，保留 FK 与幂等约束。
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from domains.conversations.schemas import (
            ReplyWorkAction,
            ReplyWorkActionRequest,
        )
        from shared.schemas.identifiers import ProspectAccountId, TenantId
        from tests.integration.test_conversation_reply_work_actions import (
            _message,
            _service,
        )
        tenant, account = TenantId('tn_review'), ProspectAccountId('acc_review')
        service = _service(async_sessionmaker(engine, expire_on_commit=False), tenant)
        message = await _message(service, tenant, account)
        await service.enqueue_reply_work_action(tenant, ReplyWorkActionRequest(
            message_id=message, outbound_message_id='out_review', enrollment_id='enr_review',
            account_id=account, contact_point_id='cp_review', action=ReplyWorkAction.REVIEW_REPLY,
            idempotency_key=f'reply:review_reply:{message}',
        ))
        assert migrate(engine.url, 'downgrade', '0077') != 0
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM conversation_reply_work WHERE tenant_id='tn_review' AND action='review_reply'")) == 1
            assert await conn.scalar(text('SELECT version_num FROM alembic_version')) == '0078'
    finally:
        await engine.dispose()
        async with integration_engine.connect() as conn:
            admin = await conn.execution_options(isolation_level='AUTOCOMMIT')
            await admin.execute(text(f'DROP DATABASE "{name}"'))
