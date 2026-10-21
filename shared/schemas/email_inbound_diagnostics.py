"""入站阶段的固定脱敏诊断；不保存异常对象、协议原文或业务内容。"""

from __future__ import annotations

import imaplib
import ssl
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from functools import wraps
from time import monotonic_ns
from typing import Any, ParamSpec, TypeVar

from shared.schemas.email_inbound import InboundError


class InboundStage(StrEnum):
    TARGET = "target"
    CREDENTIAL = "credential"
    CONNECT = "connect"
    AUTHENTICATE = "authenticate"
    SELECT = "select_readonly"
    UID_STATE = "uid_state"
    SEARCH = "search"
    HEADERS = "headers"
    BODY = "body"
    AUTHORIZE = "authorize"
    PARSE = "parse"
    PAGE = "page"
    ARCHIVE = "archive"


class InboundFailureKind(StrEnum):
    TIMEOUT = "timeout"
    TLS = "tls"
    NETWORK = "network"
    PROTOCOL = "protocol"
    INTEGRITY = "integrity"
    UNEXPECTED = "unexpected"


class InboundProtocolStatus(StrEnum):
    OK = "ok"
    NO = "no"
    BAD = "bad"
    BYE = "bye"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class InboundDiagnostic:
    stage: InboundStage
    kind: InboundFailureKind
    protocol_status: InboundProtocolStatus
    duration_ms: int

    def __post_init__(self) -> None:
        if (
            type(self.stage) is not InboundStage
            or type(self.kind) is not InboundFailureKind
            or type(self.protocol_status) is not InboundProtocolStatus
            or type(self.duration_ms) is not int
            or not 0 <= self.duration_ms <= 86_400_000
        ):
            raise ValueError("入站诊断字段无效")


class InboundProtocolFailure(InboundError):
    def __init__(self, status: object) -> None:
        super().__init__()
        allowed = {
            "OK": InboundProtocolStatus.OK,
            "NO": InboundProtocolStatus.NO,
            "BAD": InboundProtocolStatus.BAD,
            "BYE": InboundProtocolStatus.BYE,
        }
        self.protocol_status = (
            allowed.get(status, InboundProtocolStatus.UNKNOWN)
            if type(status) is str
            else InboundProtocolStatus.UNKNOWN
        )


def require_protocol_ok(status: object) -> None:
    if type(status) is not str or status != "OK":
        raise InboundProtocolFailure(status)


class DiagnosedInboundError(InboundError):
    """仍是原不可自动重试错误；只带重新校验的白名单诊断。"""

    def __init__(self, diagnostic: InboundDiagnostic) -> None:
        super().__init__()
        if type(diagnostic) is not InboundDiagnostic:
            raise ValueError("入站诊断字段无效")
        self.diagnostic = InboundDiagnostic(
            diagnostic.stage,
            diagnostic.kind,
            diagnostic.protocol_status,
            diagnostic.duration_ms,
        )


def safe_failure(
    stage: InboundStage,
    error: Exception,
    duration_ms: int,
    status: InboundProtocolStatus = InboundProtocolStatus.UNKNOWN,
) -> DiagnosedInboundError:
    """不读取 str/repr/args/cause/context；已有typed诊断也只复制四个安全字段。"""
    if type(error) is DiagnosedInboundError:
        return DiagnosedInboundError(error.diagnostic)
    if type(error) is InboundProtocolFailure:
        status = error.protocol_status
        kind = InboundFailureKind.PROTOCOL
    elif isinstance(error, TimeoutError):
        kind = InboundFailureKind.TIMEOUT
    elif isinstance(error, ssl.SSLError):
        kind = InboundFailureKind.TLS
    elif isinstance(error, OSError):
        kind = InboundFailureKind.NETWORK
    elif isinstance(error, imaplib.IMAP4.error) or status in {
        InboundProtocolStatus.NO,
        InboundProtocolStatus.BAD,
        InboundProtocolStatus.BYE,
    }:
        kind = InboundFailureKind.PROTOCOL
    elif isinstance(error, InboundError):
        kind = InboundFailureKind.INTEGRITY
    else:
        kind = InboundFailureKind.UNEXPECTED
    return DiagnosedInboundError(
        InboundDiagnostic(
            stage,
            kind,
            status,
            min(86_400_000, max(0, duration_ms)),
        )
    )


class _StageObservation:
    def __init__(self) -> None:
        self.status = InboundProtocolStatus.UNKNOWN

    def protocol(self, status: object) -> None:
        # 不调用外来对象的比较、repr、str 或 lower。
        allowed = {
            "OK": InboundProtocolStatus.OK,
            "NO": InboundProtocolStatus.NO,
            "BAD": InboundProtocolStatus.BAD,
            "BYE": InboundProtocolStatus.BYE,
        }
        self.status = (
            allowed.get(status, InboundProtocolStatus.UNKNOWN)
            if type(status) is str
            else InboundProtocolStatus.UNKNOWN
        )


@contextmanager
def inbound_stage(stage: InboundStage) -> Iterator[_StageObservation]:
    """异常离开except后再抛；切断原始异常链，取消仍按原语义传播。"""
    if type(stage) is not InboundStage:
        raise ValueError("入站诊断阶段无效")
    started = monotonic_ns()
    observation = _StageObservation()
    failure = None
    try:
        yield observation
    except Exception as error:  # noqa: BLE001 只复制固定字段，禁止原始异常进入诊断
        failure = safe_failure(
            stage, error, (monotonic_ns() - started) // 1_000_000, observation.status
        )
    if failure is not None:
        try:
            raise failure from None
        except DiagnosedInboundError:
            # contextlib.throw 会重新挂接正在处理的异常；在离开边界前显式断链。
            failure.__context__ = None
            raise


P = ParamSpec("P")
T = TypeVar("T")


def diagnosed(
    stage: InboundStage,
) -> Callable[[Callable[P, Coroutine[Any, Any, T]]], Callable[P, Coroutine[Any, Any, T]]]:
    """仅包裹已有异步操作；不新增IO、重试或日志。"""

    def decorate(call: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Coroutine[Any, Any, T]]:
        @wraps(call)
        async def invoke(*args: P.args, **kwargs: P.kwargs) -> T:
            with inbound_stage(stage):
                return await call(*args, **kwargs)

        return invoke

    return decorate
