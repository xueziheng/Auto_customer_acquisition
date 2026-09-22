"""老板指令域服务 —— **本域的公共 API**。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.directives.models import DirectiveContent
from domains.directives.schemas import (
    DemandDiscoveryPlanInput,
    DirectiveView,
    ProposalView,
    SourcingAdmissionConfigInput,
)
from shared.schemas.identifiers import AgentTurnId, DirectiveId, EmployeeId, TenantId


@runtime_checkable
class DirectiveService(Protocol):
    """指令服务。

    注意本服务**不做自然语言解析**——那在 ``agent_runtime``。
    这里接收解析结果、管理提案生命周期和版本生效。
    """

    async def submit_proposal(
        self,
        tenant_id: TenantId,
        raw_text: str,
        parsed: DirectiveContent,
        interpretation_summary: str,
        expected_behavior_changes: list[str],
        parsed_by: str,
    ) -> str:
        """提交指令提案。

        实现要求：
        - ``expected_behavior_changes`` 不得为空——没有预计行为变化
          的提案老板无法审。解析器算不出影响时，这里要拒收并让
          解析器补算。
        - ``raw_text`` 原样保存：解析错误的争议要回到原话
        - 探索配比两项之和必须为 100
        """
        ...

    async def submit_discovery_proposal(
        self,
        tenant_id: TenantId,
        raw_text: str,
        plan: DemandDiscoveryPlanInput,
        interpretation_summary: str,
        expected_behavior_changes: list[str],
        parsed_by: str,
    ) -> str:
        """提交需求探索提案；域内转换为不可变指令内容并二次校验。"""
        ...

    async def submit_discovery_proposal_once(
        self,
        tenant_id: TenantId,
        source_turn_id: AgentTurnId,
        source_version: int,
        request_hmac: str,
        raw_text: str,
        plan: DemandDiscoveryPlanInput,
        interpretation_summary: str,
        expected_behavior_changes: list[str],
        parsed_by: str,
        *,
        submitted_by: EmployeeId,
    ) -> str:
        """当前老板授权后，在同一事务保存提案和不可变来源；不做确认。"""
        ...

    async def submit_sourcing_admission_proposal(
        self,
        tenant_id: TenantId,
        raw_text: str,
        config: SourcingAdmissionConfigInput,
        interpretation_summary: str,
        expected_behavior_changes: list[str],
        parsed_by: str,
        *,
        submitted_by: EmployeeId,
    ) -> str:
        """由在职老板基于当前完整指令提交准入配置并记录乐观基线。"""
        ...

    async def confirm_proposal(
        self, tenant_id: TenantId, proposal_id: str, confirmed_by: EmployeeId
    ) -> DirectiveId:
        """老板确认提案，生成新的生效版本。

        **唯一能让指令生效的路径。** 实现要求：
        - 提案必须处于 PENDING_CONFIRMATION（过期的不能确认——
          一周前的理解可能已不适用当前系统状态）
        - 生成新版本，前一版本标记 superseded
        - 发布 ``DirectiveActivated``，各域自行应用相关部分
        - 确认人必须有 boss 角色（权限在服务层查，不只靠 API 层）
        """
        ...

    async def reject_proposal(
        self, tenant_id: TenantId, proposal_id: str, rejected_by: EmployeeId
    ) -> None:
        """老板否决提案（理解错了）。

        被否决的提案连同原话保留——**解析失败的样本是改进解析
        prompt 的素材**，也是评估集的来源。
        """
        ...

    async def rollback_to_version(
        self, tenant_id: TenantId, version: int, requested_by: EmployeeId
    ) -> DirectiveId:
        """回滚到历史版本。

        实现：把旧版本内容作为**新版本**重新生效（``rollback_of``
        指向被恢复的版本），不删除中间版本。历史只增——
        「上周系统按什么规则跑」必须永远可查。
        """
        ...

    async def get_active(self, tenant_id: TenantId) -> DirectiveView | None:
        """当前生效的指令。全系统读探索配比、市场分配、预算上限
        都从这里读。None 表示尚无指令（用 Playbook 默认值）。"""
        ...

    async def get_proposal(
        self, tenant_id: TenantId, proposal_id: str
    ) -> ProposalView: ...

    async def list_versions(
        self, tenant_id: TenantId, limit: int = 20
    ) -> list[DirectiveView]: ...

    async def get_confirmed_discovery_plan(
        self,
        tenant_id: TenantId,
        proposal_id: str,
        confirmed_by: EmployeeId,
    ) -> DemandDiscoveryPlanInput:
        """读取由指定老板确认的探索计划，供 workflow 在每步重新核对。"""
        ...


@runtime_checkable
class DirectiveEmployeeReader(Protocol):
    """指令域所需的最窄员工能力；实现位于应用装配层。"""

    async def is_active_boss(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> bool: ...

    async def names_for(
        self, tenant_id: TenantId, employee_ids: tuple[EmployeeId, ...]
    ) -> dict[EmployeeId, str]: ...
