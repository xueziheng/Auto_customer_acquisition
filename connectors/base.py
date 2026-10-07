"""Connector Protocol 与注册表 —— 插件点 1 的核心。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ConnectorManifest:
    """连接器注册元数据。

    字段：
        connector_id:    如 "gmail"、"email_verification"
        capabilities:    提供的能力标识（tool handler 按此发现）
        secret_refs:     需要的密钥**名称**（不是值）。部署时据此
                         校验密钥服务里配齐了——启动时发现比
                         运行时发现好
        rate_limit_note: 上游限流说明
        compliance_note: 合规约束摘要（详见各自 AGENTS.md）
    """

    connector_id: str
    capabilities: tuple[str, ...]
    secret_refs: tuple[str, ...]
    rate_limit_note: str = ""
    compliance_note: str = ""


@runtime_checkable
class Connector(Protocol):
    """连接器 Protocol。

    实现约定（三条铁律，详见 AGENTS.md）：
    - 凭证只在内部：``configure`` 收到的是密钥引用名，运行期自取
    - 不写业务表：返回数据由域服务落库
    - 错误分类：可重试抛 TransientError/RateLimited（带 retry_after），
      认证失败等不可重试的抛 ConnectorError 子类
    """

    manifest: ConnectorManifest

    async def configure(self, secret_resolver: Any) -> None:
        """启动时配置。``secret_resolver`` 由部署环境注入
        （Vault / 环境变量 / Keychain），connector 用引用名换取值，
        值只存在于实例内部。"""
        ...

    async def health_check(self) -> bool:
        """连通性检查。worker 启动时与周期巡检调用。"""
        ...


class ConnectorRegistry:
    """连接器注册表。应用启动时装配。"""

    def register(self, connector: Connector) -> None:
        """注册。

        实现要求：
        - connector_id 重复直接抛错
        - 校验 ``secret_refs`` 在密钥服务中全部可解析，缺失则
          启动失败并列出缺哪些——不要等第一次调用才发现没配密钥
        """
        raise NotImplementedError

    def get(self, connector_id: str) -> Connector:
        raise NotImplementedError

    def find_by_capability(self, capability: str) -> list[Connector]:
        raise NotImplementedError
