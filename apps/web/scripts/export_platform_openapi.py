"""独立平台 API schema 导出；不连接数据库、不读取任何运行凭证。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sqlalchemy.ext.asyncio import create_async_engine

from apps.api.platform_access import create_platform_app
from shared.schemas.identifiers import TenantId

if __name__ == "__main__":
    app = create_platform_app(
        control_tenant=TenantId("tn_00000000000000000000000000"),
        engine=create_async_engine("postgresql+asyncpg://localhost/schema_export"),
        origin="http://127.0.0.1:1", readers=(),
    )
    json.dump(app.openapi(), sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
