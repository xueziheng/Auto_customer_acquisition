"""受控API进程入口；唯一生产工厂与本次基础身份初始化。"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
from collections.abc import Mapping
from pathlib import Path

import uvicorn
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from connectors.object_store.config import S3ObjectStoreSettings
from infra.controlled.config import ControlledConfig, ControlledError
from infra.controlled.network import install_network_boundary
from infra.controlled.providers import ControlledGmailTransport
from infra.db.schema import assert_database_schema_current
from infra.db.session import create_engine_from
from infra.db.tables import EmployeeRow

from .runtime import create_runtime_app_from_settings
from .runtime_config import Phase1RuntimeSettings

CONTROLLED_RESEARCH_MESSAGE = "受控演练：只研究肯尼亚家具五金需求，覆盖进口商、分销商、电商三线路，各查1次，最多读3页、记录3条信号和3条假设，最低证据档位low_mid，不触达不发送。"


class ControlledModelClient:
    """只响应具名中文合成场景；原Guardrails与领域schema仍最终校验。"""

    async def complete_json(
        self,
        *,
        model: str,
        system_prompt: str,
        payload: Mapping[str, object],
        max_output_tokens: int,
    ) -> str:
        del system_prompt, max_output_tokens
        if model != "controlled-json-v1" or dict(payload) != {
            "message": CONTROLLED_RESEARCH_MESSAGE
        }:
            raise ControlledError("controlled_model_input_required")
        return json.dumps(
            {
                "interpretation_summary": "本次仅演练肯尼亚家具五金需求研究；不触达、不发送。",
                "expected_behavior_changes": [
                    "确认后仍须检查研究依赖、业务政策和预算。"
                ],
                "no_auto_send": True,
                "plan": {
                    "objective": "受控肯尼亚家具五金需求研究",
                    "queries": [
                        {
                            "query": "Kenya furniture hardware " + lane,
                            "country": "KE",
                            "category": "furniture hardware",
                            "limit": 1,
                            "discovery_lane": lane,
                        }
                        for lane in ("importer", "distributor", "ecommerce")
                    ],
                    "target_countries": ["KE"],
                    "target_categories": ["furniture hardware"],
                    "excluded_countries": [],
                    "excluded_categories": [],
                    "max_search_queries": 3,
                    "max_pages_read": 3,
                    "max_signals": 3,
                    "max_hypotheses": 3,
                    "minimum_confidence_tier": "low_mid",
                    "strategy_group": "controlled-research",
                    "execution_mode": "research_only",
                },
            }
        )


async def initialize_identities(config: ControlledConfig) -> None:
    """仅初始化受控角色；业务政策、审批、邮件和需求均不写入。"""
    engine = create_engine_from(config.database_url.get_secret_value())
    try:
        await assert_database_schema_current(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory.begin() as session:
            current = (
                await session.scalars(
                    select(EmployeeRow).where(EmployeeRow.tenant_id == config.tenant_id)
                )
            ).all()
            if current:
                expected = {
                    (i.employee_id, i.user_id, i.role) for i in config.identities
                }
                if {(e.employee_id, e.user_id, e.role) for e in current} != expected:
                    raise ControlledError("identity_state_invalid")
                return
            manager = next(
                i.employee_id for i in config.identities if i.role == "manager"
            )
            for identity in config.identities:
                session.add(
                    EmployeeRow(
                        employee_id=identity.employee_id,
                        tenant_id=config.tenant_id,
                        user_id=identity.user_id,
                        name=identity.label,
                        role=identity.role,
                        manager_id=manager if identity.role == "sales" else None,
                        team_id=None,
                        languages=["en"],
                        timezone="UTC",
                        is_active=True,
                    )
                )
    finally:
        await engine.dispose()


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        config = ControlledConfig.read(Path(sys.argv[1]))
        install_network_boundary(
            destinations=frozenset({config.database_port, config.object_port}),
            listeners=frozenset({config.api_port}),
        )
        if len(sys.argv) == 3 and sys.argv[2] == "initialize-identities":
            asyncio.run(initialize_identities(config))
            return 0
        environment = config.runtime_environment()
        settings = Phase1RuntimeSettings.from_environ(environment)
        app = create_runtime_app_from_settings(
            settings,
            secret_resolver=config,
            object_store_settings=S3ObjectStoreSettings.from_environ(environment),
            model_client=ControlledModelClient(),
            gmail_transport=ControlledGmailTransport(
                Path(sys.argv[1]).parent / "mail.sqlite", tenant_id=config.tenant_id
            ),
        )
        listener = socket.socket(fileno=int(sys.argv[2]))
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=config.api_port,
                access_log=False,
                log_config=None,
                timeout_graceful_shutdown=15,
            )
        )
        asyncio.run(server.serve(sockets=[listener]))
        return 0 if server.started else 2
    except BaseException:  # noqa: BLE001 进程边界固定失败，不能回显底层异常
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
