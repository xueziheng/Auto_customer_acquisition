"""显式安全422路由，仅转换HTTP请求模型验证失败。"""

from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.responses import Response

from .middleware import ApiErrorResponse


class ExplicitValidationRoute(APIRoute):
    """只让显式声明安全 422 契约的 HTTP route 使用运行时 422。"""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        response_422 = self.responses.get(422)
        if not (
            isinstance(response_422, Mapping)
            and response_422.get("model") is ApiErrorResponse
        ):
            return handler

        async def explicit_422_validation_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(status_code=422) from None

        return explicit_422_validation_handler
