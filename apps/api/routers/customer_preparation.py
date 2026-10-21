"""本人客户资料准备HTTP；不提供联系人验证或外部触达动作。"""

from typing import Annotated

from fastapi import APIRouter, Depends

from domains.prospecting.customer_preparation import (
    CustomerPreparationCommand,
    CustomerPreparationView,
)
from shared.errors import TransientError
from shared.schemas.identifiers import CandidateAllocationId

from ..composition.customer_preparation import CustomerPreparationApplication
from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
)
from ..identity import RequestIdentity

router = APIRouter(prefix="/customers", tags=["customers"])
Identity = Annotated[RequestIdentity, Depends(get_request_identity)]
Dependencies = Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)]


def _application(
    dependencies: ConfiguredApiDependencies,
) -> CustomerPreparationApplication:
    application = getattr(dependencies, "customer_preparation", None)
    if application is None:
        raise TransientError("客户资料准备服务尚未配置")
    return application


@router.get("/{allocation_id}/preparation", response_model=CustomerPreparationView)
async def read(
    allocation_id: str, identity: Identity, dependencies: Dependencies
) -> CustomerPreparationView:
    return await _application(dependencies).read(
        identity, CandidateAllocationId(allocation_id)
    )


@router.post("/{allocation_id}/preparation", response_model=CustomerPreparationView)
async def prepare(
    allocation_id: str,
    body: CustomerPreparationCommand,
    identity: Identity,
    dependencies: Dependencies,
) -> CustomerPreparationView:
    return await _application(dependencies).prepare(
        identity, CandidateAllocationId(allocation_id), body
    )
