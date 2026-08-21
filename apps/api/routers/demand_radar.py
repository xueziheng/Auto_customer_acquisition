"""Demand Radar —— 需求信号、假设、需求簇。

GET /demand/signals              信号列表（按类型/状态过滤）
GET /demand/hypotheses           假设列表（置信度档位现算，含解释）
GET /demand/hypotheses/{id}      假设详情 + 证据链（可点击到原始来源）
GET /demand/needs                已验证需求（含完整度与缺失字段）
GET /demand/needs/{id}           详情：每个字段带来源与客户原话摘录
GET /demand/clusters             需求簇
GET /demand/clusters/{id}        需求簇详情

界面要求：假设与已验证需求必须视觉区分（is_inference 标记）——
推断长得和事实一样，老板就会把推断当事实。
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from domains.demand.schemas import (
    DemandSignalView,
    HypothesisView,
    NeedClusterView,
    ValidatedNeedView,
)
from domains.employees.permissions import EmployeeAction
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    NeedClusterId,
    NeedHypothesisId,
    ValidatedNeedId,
)

from ..dependencies import (
    ConfiguredApiDependencies,
    DemandRadarService,
    get_api_dependencies,
    require_employee_action,
)
from ..identity import RequestIdentity

router = APIRouter()

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_HYPOTHESIS = re.compile(rf"hyp_{_ULID}")
_NEED = re.compile(rf"need_{_ULID}")
_CLUSTER = re.compile(rf"ncl_{_ULID}")
_BOSS_ONLY = frozenset({"boss"})


def _radar(dependencies: ConfiguredApiDependencies) -> DemandRadarService:
    if dependencies.demand_radar is None:
        raise TransientError("Demand Radar 服务尚未配置")
    return dependencies.demand_radar


_read_gate = Depends(
    require_employee_action(
        EmployeeAction.OWNERSHIP_READ,
        allowed_roles=_BOSS_ONLY,
    )
)


@router.get(
    "/signals",
    response_model=list[DemandSignalView],
    dependencies=[_read_gate],
)
async def list_signals(
    identity: Annotated[RequestIdentity, _read_gate],
    dependencies: Annotated[
        ConfiguredApiDependencies,
        Depends(get_api_dependencies),
    ],
    signal_type: str | None = None,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DemandSignalView]:
    return await _radar(dependencies).list_signals(
        identity.tenant_id,
        identity.employee_actor,
        signal_type=signal_type,
        status=status,
        limit=limit,
    )


@router.get(
    "/hypotheses",
    response_model=list[HypothesisView],
    dependencies=[_read_gate],
)
async def list_hypotheses(
    identity: Annotated[RequestIdentity, _read_gate],
    dependencies: Annotated[
        ConfiguredApiDependencies,
        Depends(get_api_dependencies),
    ],
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[HypothesisView]:
    return await _radar(dependencies).list_hypotheses(
        identity.tenant_id,
        identity.employee_actor,
        status=status,
        limit=limit,
    )


@router.get(
    "/hypotheses/{hypothesis_id}",
    response_model=HypothesisView,
    dependencies=[_read_gate],
)
async def get_hypothesis(
    hypothesis_id: str,
    identity: Annotated[RequestIdentity, _read_gate],
    dependencies: Annotated[
        ConfiguredApiDependencies,
        Depends(get_api_dependencies),
    ],
) -> HypothesisView:
    if _HYPOTHESIS.fullmatch(hypothesis_id) is None:
        raise ValidationError("需求假设标识无效")
    return await _radar(dependencies).get_hypothesis(
        identity.tenant_id,
        identity.employee_actor,
        NeedHypothesisId(hypothesis_id),
    )


@router.get(
    "/needs",
    response_model=list[ValidatedNeedView],
    dependencies=[_read_gate],
)
async def list_needs(
    identity: Annotated[RequestIdentity, _read_gate],
    dependencies: Annotated[
        ConfiguredApiDependencies,
        Depends(get_api_dependencies),
    ],
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ValidatedNeedView]:
    return await _radar(dependencies).list_needs(
        identity.tenant_id,
        identity.employee_actor,
        status=status,
        limit=limit,
    )


@router.get(
    "/needs/{need_id}",
    response_model=ValidatedNeedView,
    dependencies=[_read_gate],
)
async def get_need(
    need_id: str,
    identity: Annotated[RequestIdentity, _read_gate],
    dependencies: Annotated[
        ConfiguredApiDependencies,
        Depends(get_api_dependencies),
    ],
) -> ValidatedNeedView:
    if _NEED.fullmatch(need_id) is None:
        raise ValidationError("已验证需求标识无效")
    return await _radar(dependencies).get_need(
        identity.tenant_id,
        identity.employee_actor,
        ValidatedNeedId(need_id),
    )


@router.get(
    "/clusters",
    response_model=list[NeedClusterView],
    dependencies=[_read_gate],
)
async def list_clusters(
    identity: Annotated[RequestIdentity, _read_gate],
    dependencies: Annotated[
        ConfiguredApiDependencies,
        Depends(get_api_dependencies),
    ],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[NeedClusterView]:
    return await _radar(dependencies).list_clusters(
        identity.tenant_id,
        identity.employee_actor,
        limit=limit,
    )


@router.get(
    "/clusters/{cluster_id}",
    response_model=NeedClusterView,
    dependencies=[_read_gate],
)
async def get_cluster(
    cluster_id: str,
    identity: Annotated[RequestIdentity, _read_gate],
    dependencies: Annotated[
        ConfiguredApiDependencies,
        Depends(get_api_dependencies),
    ],
) -> NeedClusterView:
    if _CLUSTER.fullmatch(cluster_id) is None:
        raise ValidationError("需求簇标识无效")
    return await _radar(dependencies).get_cluster(
        identity.tenant_id,
        identity.employee_actor,
        NeedClusterId(cluster_id),
    )
