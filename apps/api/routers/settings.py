"""Playbook & Connections。

GET  /settings/playbook
PUT  /settings/playbook          走审批
GET  /settings/connectors        连接状态（健康检查结果，不含凭证）
GET  /settings/directives        指令版本历史
POST /settings/directives/{version}/rollback   仅 boss
"""

from __future__ import annotations

from .module_status import build_status_router

router = build_status_router(
    module="settings",
    mode="configuration",
    reason_code="SETTINGS_QUERY_API_NOT_CONFIGURED",
)
