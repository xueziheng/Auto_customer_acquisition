"""独立研究浏览器测试配置：不使用人工预览的固定端口，也不代理同源。"""

import os

from fastapi.middleware.cors import CORSMiddleware

from tests.research_ui_preview import app

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ["RESEARCH_TEST_ORIGIN"]],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Tenant-Id", "X-Employee-Id"],
)
