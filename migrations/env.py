"""Alembic 迁移环境（异步模板）。

职责与边界：
- 只从仓库根 `.env` 加载环境变量（python-dotenv），不读取其它位置。
- 数据库 URL 在运行时从 `DATABASE_URL` 读取并注入 alembic；绝不写死在
  任何受跟踪文件里，也绝不打印。
- `DATABASE_URL` 缺失时给出明确的中文报错，指引从 `infra/.env.example`
  复制 `.env` 到仓库根填写。
- 同时支持 online（异步引擎）与 offline（仅生成 SQL）两种模式。
- Phase 0 的 `target_metadata` 为 `None`：业务 schema 尚未引入，迁移按
  版本文件手写演进，不用 autogenerate。
"""
from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# 只加载仓库根目录的 .env；已存在的环境变量优先，不被覆盖。
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "缺少 DATABASE_URL：无法运行迁移。请从 infra/.env.example 复制 .env "
        "到仓库根目录，并填写 DATABASE_URL（本地 Docker 的连接串见该文件注释）。"
    )

# 运行时注入连接 URL，不写死在 alembic.ini。
# ConfigParser 会做 % 插值：URL 里合法的百分号编码（如密码中的 %40）必须转义
# 成 %% 再注入，否则读取时抛 InterpolationSyntaxError。此处只转义、绝不打印 URL。
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))

# Phase 0：业务 schema 未引入，元数据留空；迁移按版本文件手写演进。
target_metadata = None


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 不连库，适用于 `alembic upgrade --sql`。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在已建立的连接上执行迁移（online 模式共用）。"""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """用 SQLAlchemy 异步引擎执行迁移。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """online 模式入口：以异步事件循环执行。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
