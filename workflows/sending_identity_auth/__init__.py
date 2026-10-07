"""发件身份 DNS 认证工作流。"""

from .flow import (
    AuthenticationCheckRequestedHandler,
    DnsAuthenticationStep,
    build_sending_identity_auth_definition,
    register_sending_identity_auth,
)

__all__ = [
    "AuthenticationCheckRequestedHandler",
    "DnsAuthenticationStep",
    "build_sending_identity_auth_definition",
    "register_sending_identity_auth",
]
