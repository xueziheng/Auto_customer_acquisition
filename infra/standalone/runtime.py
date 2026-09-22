"""进程装配事实；心跳不是模型探测，也不解析凭证。"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import TenantId, new_id


class ModelRuntimeLifecycle:
    def __init__(
        self,
        repository: SqlModelConfigurationRepository,
        settings: StandaloneModelSettings,
        tenant_id: TenantId,
        process: Literal["api", "scheduler"],
        recovery: Callable[[str], Awaitable[int]] | None = None,
    ) -> None:
        self._repository, self._settings, self._tenant_id, self._process = (
            repository,
            settings,
            tenant_id,
            process,
        )
        self._recovery = recovery
        self._instance_id = new_id("mrt").lower()
        self._task: asyncio.Task[None] | None = None
        self._started = False

    @property
    def instance_id(self) -> str:
        return self._instance_id

    async def startup(self) -> None:
        config = self._settings
        await self._repository.initialize(
            self._tenant_id,
            config.configuration_version,
            config.model,
            config.limits,
            config.model_data_export_enabled,
        )
        await self._repository.register_process(
            self._tenant_id,
            self._process,
            config.configuration_version,
            self._instance_id,
        )
        self._started = True
        if self._process == "api":
            self._task = asyncio.create_task(self._heartbeat_loop())

    async def heartbeat(self) -> None:
        if self._recovery is not None:
            await self._recovery(self._instance_id)
        await self._repository.heartbeat(
            self._tenant_id, self._process, self._instance_id
        )

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            try:
                await self.heartbeat()
            except Exception:  # noqa: BLE001 - 过期即拒绝模型动作，禁止记录连接详情
                return

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._started:
            await self._repository.unregister_process(
                self._tenant_id, self._process, self._instance_id
            )
            self._started = False
