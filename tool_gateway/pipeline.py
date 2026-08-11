"""检查管线。

顺序固定，理由见 AGENTS.md：便宜且否决率高的在前；
幂等必须在执行前——顺序错了会重复发送。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from shared.errors import (
    PermissionDenied,
    PolicyViolation,
    TradeOSError,
    TransientError,
    ValidationError,
)
from shared.schemas.identifiers import IdempotencyKey, RunId, TenantId, UserId

from .errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from .repository import (
    ToolCallEventRecord,
    ToolCallId,
    ToolCallRecord,
    ToolGatewayUnitOfWorkFactory,
)

type SafeScalar = str | int | bool | None
_LABEL_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,99}")
_AUDIT_VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_CANONICAL_ID_RE = re.compile(r"[a-z]{2,8}_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_SECRET_TOKENS = frozenset(
    {
        "recipient",
        "sender",
        "subject",
        "body",
        "header",
        "headers",
        "token",
        "secret",
        "password",
        "authorization",
        "bearer",
        "cookie",
        "dsn",
        "address",
        "url",
    }
)
_SAFE_OUTPUT_KEYS = frozenset(
    {"provider_ref", "already_existed", "duplicate", "retry_after_seconds"}
)
_SECRET_AUDIT_KEYS = frozenset(
    {
        "api_key",
        "access_key",
        "private_key",
        "oauth_credential",
        "oauth_token",
        "client_secret",
    }
)
_TRANSIENT_RESULT_CATEGORIES = frozenset(
    {
        ToolErrorCategory.IN_PROGRESS,
        ToolErrorCategory.RATE_LIMITED,
        ToolErrorCategory.PROVIDER_TRANSIENT,
        ToolErrorCategory.RECONCILIATION_REQUIRED,
    }
)
_PERMANENT_RESULT_CATEGORIES = frozenset(ToolErrorCategory) - (
    _TRANSIENT_RESULT_CATEGORIES
)


@dataclass(frozen=True)
class ToolCallContext:
    """一次工具调用的上下文。

    字段：
        tenant_id, user_id
        run_id:           发起的 Agent Run（人工操作为 None）
        tool_id
        params
        idempotency_key:  manifest 要求 REQUIRED 时必填
        approval_ref:     已批准的审批引用（approval stage 校验）
        campaign_ref:     所属 Campaign（边界内发送的依据）
    """

    tenant_id: TenantId
    user_id: UserId
    tool_id: str
    params: Mapping[str, Any] = field(repr=False)
    run_id: RunId | None = None
    idempotency_key: IdempotencyKey | None = None
    approval_ref: str | None = None
    campaign_ref: str | None = None

    def __post_init__(self) -> None:
        _require_label(self.tool_id, "tool_id")
        if not isinstance(self.tenant_id, str) or not self.tenant_id:
            raise ValidationError("tenant_id 无效")
        if not isinstance(self.user_id, str) or not self.user_id:
            raise ValidationError("user_id 无效")
        if not isinstance(self.params, Mapping):
            raise ValidationError("tool params 无效")
        copied: dict[str, Any] = {}
        for key, value in self.params.items():
            if not isinstance(key, str) or _LABEL_RE.fullmatch(key) is None:
                raise ValidationError("tool param key 无效")
            copied[key] = value
        object.__setattr__(self, "params", MappingProxyType(copied))


@dataclass(frozen=True)
class PreparedToolCall:
    """只读准备结果；payload 仅在当前进程存活且永不 repr。"""

    request_fingerprint: str
    fingerprint_version: str
    audit_projection: Mapping[str, SafeScalar]
    payload: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request_fingerprint, str)
            or _FINGERPRINT_RE.fullmatch(self.request_fingerprint) is None
        ):
            raise ValidationError("request fingerprint 无效")
        _require_label(self.fingerprint_version, "fingerprint_version")
        if not isinstance(self.audit_projection, Mapping):
            raise ValidationError("audit projection 无效")
        object.__setattr__(
            self,
            "audit_projection",
            _freeze_safe_mapping(self.audit_projection, audit=True),
        )


@dataclass(frozen=True)
class CheckRejection:
    """结构化拒绝 —— **不是异常**。

    Agent 要靠它决定下一步：``remediation`` 说明能否补救以及怎么补
    （"联系方式未验证 → 先调 contact.verify"）。裸异常会被当成
    临时故障重试，那正是最不该发生的。

    字段：
        stage:        哪个 stage 拒的
        rule:         哪条规则
        reason:       人类可读原因
        remediation:  怎么补救（None = 不可补救，别再试）
    """

    stage: str
    rule: str
    reason: str
    remediation: str | None = None

    def __post_init__(self) -> None:
        _require_label(self.stage, "rejection stage")
        _require_label(self.rule, "rejection rule")
        _require_safe_text(self.reason, "rejection reason", max_length=300)
        if self.remediation is not None:
            _require_safe_text(
                self.remediation,
                "rejection remediation",
                max_length=300,
            )


@dataclass(frozen=True)
class ToolCallResult:
    """调用结果。

    ``rejected`` 与 ``output`` 互斥。``duplicate_of`` 非 None 表示
    幂等命中，``output`` 是上次的结果——这是正常路径，不是错误。
    """

    tool_id: str
    status: ToolCallStatus
    output: Mapping[str, SafeScalar] | None = None
    rejected: CheckRejection | None = None
    duplicate_of: str | None = None
    tool_call_id: str | None = None
    cost_note: str | None = None
    error_category: ToolErrorCategory | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        _require_label(self.tool_id, "tool_id")
        if not isinstance(self.status, ToolCallStatus):
            raise ValidationError("tool call status 无效")
        if self.rejected is not None and not isinstance(self.rejected, CheckRejection):
            raise ValidationError("tool rejection 无效")
        if self.error_category is not None and not isinstance(
            self.error_category, ToolErrorCategory
        ):
            raise ValidationError("tool error category 无效")
        if self.tool_call_id is not None:
            _require_canonical_id(self.tool_call_id, "tool_call_id", prefix="tcl")
        if self.duplicate_of is not None:
            _require_canonical_id(self.duplicate_of, "duplicate_of", prefix="tcl")
        if self.cost_note is not None:
            _require_label(self.cost_note, "cost_note")
        if self.retry_after_seconds is not None and (
            not isinstance(self.retry_after_seconds, int)
            or isinstance(self.retry_after_seconds, bool)
            or not 1 <= self.retry_after_seconds <= 86_400
        ):
            raise ValidationError("retry_after_seconds 无效")
        if self.status is ToolCallStatus.FAILED_TRANSIENT:
            if self.error_category not in _TRANSIENT_RESULT_CATEGORIES:
                raise ValidationError("transient result 缺少匹配错误分类")
        elif self.status is ToolCallStatus.FAILED_PERMANENT:
            if self.error_category not in _PERMANENT_RESULT_CATEGORIES:
                raise ValidationError("permanent result 缺少匹配错误分类")
        elif self.status is not ToolCallStatus.REJECTED and self.error_category is not None:
            raise ValidationError("非失败结果不能携带错误分类")
        if self.retry_after_seconds is not None and (
            self.status is not ToolCallStatus.FAILED_TRANSIENT
            or self.error_category not in _TRANSIENT_RESULT_CATEGORIES
        ):
            raise ValidationError("非临时失败不能携带重试时间")
        if self.status is ToolCallStatus.REJECTED:
            if self.rejected is None or self.duplicate_of is not None:
                raise ValidationError("rejected result 字段不匹配")
        elif self.status is ToolCallStatus.DUPLICATE:
            if self.duplicate_of is None or self.rejected is not None:
                raise ValidationError("duplicate result 字段不匹配")
        elif self.rejected is not None or self.duplicate_of is not None:
            raise ValidationError("tool result 状态字段不匹配")
        if self.output is not None:
            if self.status not in {ToolCallStatus.SUCCEEDED, ToolCallStatus.DUPLICATE}:
                raise ValidationError("非成功结果不能携带 output")
            object.__setattr__(
                self,
                "output",
                _freeze_safe_mapping(self.output, audit=False),
            )


@runtime_checkable
class CheckStage(Protocol):
    """检查 stage 接口。每个 stage 一个实现，见 ``checks/``。

    返回 None = 通过；返回 ``CheckRejection`` = 拒绝并终止管线。
    stage 内部**只读**——检查不产生副作用（幂等 stage 的占位写入
    是唯一例外，它必须原子）。
    """

    name: str

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None: ...


class _ManifestValue(Protocol):
    value: str


class _GatewayManifest(Protocol):
    tool_id: str
    version: str
    risk_level: _ManifestValue
    cost_class: _ManifestValue
    checks: tuple[str, ...]


class _GatewayHandler(Protocol):
    async def prepare(self, ctx: ToolCallContext) -> PreparedToolCall: ...

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> Mapping[str, SafeScalar]: ...


class _GatewayRegistry(Protocol):
    def get(self, tool_id: str) -> tuple[_GatewayManifest, _GatewayHandler]: ...


@dataclass
class ToolInvocationState:
    """单次进程内调用状态；payload/preflight 永不持久化。"""

    manifest: _GatewayManifest
    tool_call_id: str
    prepared: PreparedToolCall | None = None
    preflight: object | None = None


STAGE_ORDER: tuple[str, ...] = (
    "tenant",
    "permission",
    "playbook",
    "country_policy",
    "suppression",
    "approval",
    "idempotency",
    "rate_limit",
)
"""Stage 顺序。**不可随意调换**：

- tenant/permission 是内存判断，最便宜，放最前
- suppression/idempotency 要查库，居中
- approval 可能等人工，放执行前最后一段
- idempotency 在 rate_limit 之前：幂等命中直接返回旧结果，
  不该消耗限额
"""


class ToolGateway:
    """网关入口。"""

    def __init__(
        self,
        registry: _GatewayRegistry,
        checks: Mapping[str, CheckStage],
        ledger_uow_factory: ToolGatewayUnitOfWorkFactory,
        *,
        lease_duration: timedelta,
        lease_owner: str,
        now: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str],
    ) -> None:
        if not isinstance(checks, Mapping):
            raise ValidationError("Gateway checks 无效")
        ordered = tuple(checks)
        if tuple(stage for stage in STAGE_ORDER if stage in checks) != ordered:
            raise ValidationError("Gateway checks 顺序无效")
        if (
            not isinstance(lease_duration, timedelta)
            or lease_duration <= timedelta(0)
            or lease_duration > timedelta(days=1)
        ):
            raise ValidationError("Gateway lease duration 无效")
        _require_label(lease_owner, "lease owner")
        self._registry = registry
        self._checks = MappingProxyType(dict(checks))
        self._uow_factory = ledger_uow_factory
        self._lease_duration = lease_duration
        self._lease_owner = lease_owner
        self._now = now or (lambda: datetime.now(UTC))
        self._id_factory = id_factory

    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult:
        """执行一次工具调用。

        实现要求：
        1. 从注册表取 manifest + handler（未注册 → 拒绝）
        2. 按 ``manifest.checks`` 的交集依 ``STAGE_ORDER`` 逐个跑，
           任一拒绝立即返回（仍要写审计）
        3. 幂等命中 → 返回旧结果，不执行、不重复计费
        4. 执行 handler，捕获错误分类（可重试 / 不可重试）
        5. **写 tool_call 审计（含被拒的调用）**；审计写入失败必须
           让整个调用失败——审计不完整时继续发客户邮件是合规裸奔
        6. 记录成本（Phase 1 只记录，Phase 3 接结算）
        """
        from tool_gateway.repository import ClaimStatus

        manifest, handler = self._registry.get(ctx.tool_id)
        now = self._require_now(self._now())
        call_id = ToolCallId(self._id_factory("tcl"))
        attempt_id = ctx.params.get("attempt_id")
        safe_attempt_id = attempt_id if isinstance(attempt_id, str) else None
        state = ToolInvocationState(manifest=manifest, tool_call_id=call_id)
        canonical_claimed = False
        received = ToolCallRecord(
            tenant_id=ctx.tenant_id,
            tool_call_id=call_id,
            tool_id=manifest.tool_id,
            tool_version=manifest.version,
            risk_level=manifest.risk_level.value,
            cost_class=manifest.cost_class.value,
            idempotency_key=None,
            request_fingerprint=None,
            fingerprint_version=None,
            status=ToolCallStatus.RECEIVED,
            duplicate_of=None,
            lease_owner=None,
            lease_expires_at=None,
            attempt_count=0,
            run_id=ctx.run_id,
            user_id=ctx.user_id,
            campaign_id=ctx.campaign_ref,
            message_attempt_id=safe_attempt_id,
            provider_ref=None,
            error_category=None,
            retry_after_at=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
        )
        async with self._uow_factory(ctx.tenant_id) as uow:
            await uow.calls.create_received(received)
            await uow.calls.append_event(
                self._event(ctx, call_id, "ledger", "received", None, None)
            )

        for stage_name in manifest.checks:
            if stage_name == "idempotency" and state.prepared is None:
                try:
                    state.prepared = await handler.prepare(ctx)
                except TradeOSError as error:
                    return await self._fail_typed(
                        ctx,
                        call_id,
                        self._translate_error(error),
                        canonical=False,
                        stage="handler.prepare",
                    )
            stage = self._checks.get(stage_name)
            if stage is None:
                return await self._reject(
                    ctx,
                    call_id,
                    CheckRejection(
                        stage_name,
                        "stage:unconfigured",
                        "工具检查阶段未配置",
                    ),
                )
            try:
                rejection = await stage.check(ctx, state)
            except ToolGatewayError as error:
                return await self._fail_typed(
                    ctx,
                    call_id,
                    error,
                    canonical=canonical_claimed,
                    stage=stage.name,
                )
            if rejection is not None:
                return await self._reject(ctx, call_id, rejection)
            if stage_name != "idempotency":
                await self._append_event(
                    ctx, call_id, stage.name, "allowed", None, None
                )
            if stage_name == "idempotency":
                if ctx.idempotency_key is None or state.prepared is None:
                    return await self._reject(
                        ctx,
                        call_id,
                        CheckRejection(
                            "idempotency",
                            "idempotency:required",
                            "工具调用缺少幂等键",
                        ),
                    )
                async with self._uow_factory(ctx.tenant_id) as uow:
                    claimed = await uow.calls.claim(
                        ctx.tenant_id,
                        call_id,
                        tool_id=manifest.tool_id,
                        idempotency_key=ctx.idempotency_key,
                        request_fingerprint=state.prepared.request_fingerprint,
                        fingerprint_version=state.prepared.fingerprint_version,
                        lease_owner=self._lease_owner,
                        lease_expires_at=now + self._lease_duration,
                    )
                if claimed.status is ClaimStatus.DUPLICATE:
                    await self._append_event(
                        ctx,
                        call_id,
                        "idempotency",
                        "duplicate",
                        "idempotency:canonical",
                        None,
                    )
                    if claimed.canonical.status is ToolCallStatus.FAILED_PERMANENT:
                        return ToolCallResult(
                            tool_id=ctx.tool_id,
                            status=ToolCallStatus.FAILED_PERMANENT,
                            tool_call_id=str(claimed.canonical.tool_call_id),
                            error_category=claimed.canonical.error_category,
                        )
                    output = {
                        "provider_ref": claimed.canonical.provider_ref,
                        "duplicate": True,
                    }
                    return ToolCallResult(
                        tool_id=ctx.tool_id,
                        status=ToolCallStatus.DUPLICATE,
                        output=output,
                        duplicate_of=str(claimed.canonical.tool_call_id),
                        tool_call_id=str(claimed.canonical.tool_call_id),
                    )
                if claimed.status is ClaimStatus.CONFLICT:
                    await self._append_event(
                        ctx,
                        call_id,
                        "idempotency",
                        "rejected",
                        "idempotency:conflict",
                        ToolErrorCategory.IDEMPOTENCY_CONFLICT,
                    )
                    return ToolCallResult(
                        tool_id=ctx.tool_id,
                        status=ToolCallStatus.REJECTED,
                        rejected=CheckRejection(
                            "idempotency",
                            "idempotency:conflict",
                            "幂等键已绑定不同请求",
                        ),
                        tool_call_id=str(call_id),
                        error_category=ToolErrorCategory.IDEMPOTENCY_CONFLICT,
                    )
                if claimed.status is ClaimStatus.IN_PROGRESS:
                    category = (
                        ToolErrorCategory.RECONCILIATION_REQUIRED
                        if claimed.canonical.status is ToolCallStatus.EXECUTING
                        else ToolErrorCategory.IN_PROGRESS
                    )
                    await self._append_event(
                        ctx,
                        call_id,
                        "idempotency",
                        "in_progress",
                        "idempotency:leased",
                        category,
                    )
                    return ToolCallResult(
                        tool_id=ctx.tool_id,
                        status=ToolCallStatus.FAILED_TRANSIENT,
                        tool_call_id=str(claimed.canonical.tool_call_id),
                        error_category=category,
                    )
                call_id = claimed.canonical.tool_call_id
                state.tool_call_id = str(call_id)
                canonical_claimed = True
                await self._append_event(
                    ctx,
                    call_id,
                    "idempotency",
                    "claimed",
                    "idempotency:canonical",
                    None,
                )

        if state.prepared is None:
            try:
                state.prepared = await handler.prepare(ctx)
            except TradeOSError as error:
                return await self._fail_typed(
                    ctx,
                    call_id,
                    self._translate_error(error),
                    canonical=canonical_claimed,
                    stage="handler.prepare",
                )
        async with self._uow_factory(ctx.tenant_id) as uow:
            await uow.calls.mark_executing(ctx.tenant_id, call_id)
            await uow.calls.append_event(
                self._event(ctx, call_id, "ledger", "executing", None, None)
            )
        try:
            handler_output = await handler.execute(ctx.tenant_id, state.prepared)
        except ToolGatewayError as error:
            return await self._fail_typed(
                ctx, call_id, error, canonical=True, stage="connector"
            )
        except TradeOSError as error:
            return await self._fail_typed(
                ctx,
                call_id,
                self._translate_error(error),
                canonical=True,
                stage="connector",
            )
        safe_result = ToolCallResult(
            tool_id=ctx.tool_id,
            status=ToolCallStatus.SUCCEEDED,
            output=handler_output,
            tool_call_id=str(call_id),
            cost_note=manifest.cost_class.value,
        )
        provider_ref = None if safe_result.output is None else safe_result.output.get(
            "provider_ref"
        )
        if not isinstance(provider_ref, str):
            raise ValidationError("成功工具结果缺少 provider_ref")
        rate_stage = self._checks.get("rate_limit")
        record_sent = getattr(rate_stage, "record_sent", None)
        if record_sent is not None:
            try:
                await record_sent(ctx, state, provider_ref)
            except Exception:  # noqa: BLE001 -- provider 已成功，任何本地失败都须转人工对账
                return await self._fail_typed(
                    ctx,
                    call_id,
                    ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED),
                    canonical=True,
                    stage="outreach.complete",
                )
        try:
            async with self._uow_factory(ctx.tenant_id) as uow:
                await uow.calls.complete(
                    ctx.tenant_id,
                    call_id,
                    status=ToolCallStatus.SUCCEEDED,
                    provider_ref=provider_ref,
                    error_category=None,
                    retry_after_at=None,
                )
                await uow.calls.append_event(
                    self._event(ctx, call_id, "ledger", "succeeded", None, None)
                )
        except Exception:  # noqa: BLE001 -- connector 已成功，账本完结失败必须进入对账
            return await self._fail_typed(
                ctx,
                call_id,
                ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED),
                canonical=True,
                stage="ledger.complete",
            )
        return safe_result

    async def _fail_typed(
        self,
        ctx: ToolCallContext,
        call_id: ToolCallId,
        error: ToolGatewayError,
        *,
        canonical: bool,
        stage: str = "runtime",
    ) -> ToolCallResult:
        if not canonical:
            return await self._reject(
                ctx,
                call_id,
                CheckRejection(
                    "runtime",
                    f"runtime:{error.category.value}",
                    "工具调用当前不可继续",
                ),
                category_override=error.category,
            )
        status = (
            ToolCallStatus.FAILED_TRANSIENT
            if error.is_retryable
            else ToolCallStatus.FAILED_PERMANENT
        )
        retry_at = (
            self._require_now(self._now())
            + timedelta(seconds=error.retry_after_seconds)
            if error.retry_after_seconds is not None
            else None
        )
        async with self._uow_factory(ctx.tenant_id) as uow:
            await uow.calls.complete(
                ctx.tenant_id,
                call_id,
                status=status,
                provider_ref=None,
                error_category=error.category,
                retry_after_at=retry_at,
            )
            await uow.calls.append_event(
                self._event(
                    ctx,
                    call_id,
                    stage,
                    "failed",
                    None,
                    error.category,
                )
            )
        return ToolCallResult(
            tool_id=ctx.tool_id,
            status=status,
            tool_call_id=str(call_id),
            error_category=error.category,
            retry_after_seconds=error.retry_after_seconds,
        )

    async def _reject(
        self,
        ctx: ToolCallContext,
        call_id: ToolCallId,
        rejection: CheckRejection,
        *,
        category_override: ToolErrorCategory | None = None,
    ) -> ToolCallResult:
        category = category_override or {
            "tenant": ToolErrorCategory.PERMISSION_DENIED,
            "permission": ToolErrorCategory.PERMISSION_DENIED,
            "suppression": ToolErrorCategory.SUPPRESSED,
            "suppression.preflight": ToolErrorCategory.SUPPRESSED,
            "approval": ToolErrorCategory.APPROVAL_REQUIRED,
            "idempotency": ToolErrorCategory.IDEMPOTENCY_CONFLICT,
            "rate_limit": ToolErrorCategory.RATE_LIMITED,
        }.get(rejection.stage, ToolErrorCategory.VALIDATION)
        async with self._uow_factory(ctx.tenant_id) as uow:
            await uow.calls.complete(
                ctx.tenant_id,
                call_id,
                status=ToolCallStatus.REJECTED,
                provider_ref=None,
                error_category=category,
                retry_after_at=None,
            )
            await uow.calls.append_event(
                self._event(
                    ctx,
                    call_id,
                    rejection.stage,
                    "rejected",
                    rejection.rule,
                    category,
                )
            )
        return ToolCallResult(
            tool_id=ctx.tool_id,
            status=ToolCallStatus.REJECTED,
            rejected=rejection,
            tool_call_id=str(call_id),
            error_category=category,
        )

    @staticmethod
    def _translate_error(error: TradeOSError) -> ToolGatewayError:
        if isinstance(error, ToolGatewayError):
            return error
        if isinstance(error, PermissionDenied):
            category = ToolErrorCategory.PERMISSION_DENIED
        elif isinstance(error, ValidationError):
            category = ToolErrorCategory.VALIDATION
        elif isinstance(error, TransientError):
            category = ToolErrorCategory.PROVIDER_TRANSIENT
        elif isinstance(error, PolicyViolation):
            category = ToolErrorCategory.PROVIDER_PERMANENT
        else:
            category = ToolErrorCategory.UNEXPECTED
        return ToolGatewayError(category)

    async def _append_event(
        self,
        ctx: ToolCallContext,
        call_id: ToolCallId,
        stage: str,
        outcome: str,
        rule: str | None,
        category: ToolErrorCategory | None,
    ) -> None:
        async with self._uow_factory(ctx.tenant_id) as uow:
            await uow.calls.append_event(
                self._event(ctx, call_id, stage, outcome, rule, category)
            )

    def _event(
        self,
        ctx: ToolCallContext,
        call_id: ToolCallId,
        stage: str,
        outcome: str,
        rule: str | None,
        category: ToolErrorCategory | None,
    ) -> ToolCallEventRecord:
        attempt_id = ctx.params.get("attempt_id")
        return ToolCallEventRecord(
            tenant_id=ctx.tenant_id,
            event_id=self._id_factory("tce"),
            tool_call_id=ToolCallId(call_id),
            stage=stage,
            outcome=outcome,
            rule=rule,
            category=category,
            actor_id=str(ctx.user_id),
            run_id=str(ctx.run_id) if ctx.run_id else None,
            campaign_id=ctx.campaign_ref,
            message_attempt_id=attempt_id if isinstance(attempt_id, str) else None,
            occurred_at=self._require_now(self._now()),
            duration_ms=0,
            cost_note=None,
        )

    @staticmethod
    def _require_now(value: object) -> datetime:
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != UTC.utcoffset(value)
        ):
            raise ValidationError("Gateway clock 必须返回 UTC aware datetime")
        return value


def _require_safe_text(value: object, field_name: str, *, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= max_length
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(f"{field_name} 无效")
    return value


def _require_label(value: object, field_name: str) -> str:
    label = _require_safe_text(value, field_name, max_length=100)
    if _LABEL_RE.fullmatch(label) is None:
        raise ValidationError(f"{field_name} 无效")
    tokens = frozenset(re.split(r"[_.:-]", label.casefold()))
    if tokens & _SECRET_TOKENS:
        raise ValidationError(f"{field_name} 无效")
    return label


def _safe_key(key: object, *, audit: bool) -> str:
    if not isinstance(key, str) or _LABEL_RE.fullmatch(key) is None:
        raise ValidationError("safe mapping key 无效")
    tokens = frozenset(re.split(r"[_.:-]", key.casefold()))
    if audit:
        sensitive = tokens & _SECRET_TOKENS
        if key.casefold() in _SECRET_AUDIT_KEYS or (
            sensitive
            and not (
            key.endswith("_bytes")
            and len(sensitive) == 1
            and next(iter(sensitive)) in {"recipient", "sender", "subject", "body"}
            )
        ):
            raise ValidationError("audit projection key 无效")
    elif key not in _SAFE_OUTPUT_KEYS and not key.endswith("_id"):
        raise ValidationError("tool output key 无效")
    return key


def _freeze_safe_mapping(
    values: Mapping[str, SafeScalar],
    *,
    audit: bool,
) -> Mapping[str, SafeScalar]:
    copied: dict[str, SafeScalar] = {}
    for raw_key, value in values.items():
        key = _safe_key(raw_key, audit=audit)
        if value is not None and not isinstance(value, (str, int, bool)):
            raise ValidationError("safe mapping value 无效")
        if isinstance(value, str):
            if audit:
                _require_audit_string(value, key)
            else:
                _require_safe_text(value, key, max_length=200)
        if isinstance(value, int) and not isinstance(value, bool) and value < 0:
            raise ValidationError("safe mapping integer 无效")
        if not audit:
            if key in {"already_existed", "duplicate"} and not isinstance(value, bool):
                raise ValidationError("tool output boolean 无效")
            if key == "retry_after_seconds" and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 1 <= value <= 86_400
            ):
                raise ValidationError("tool output retry 无效")
            if key == "provider_ref" and not isinstance(value, str):
                raise ValidationError("tool output provider ref 无效")
            if key == "provider_ref" and isinstance(value, str):
                _require_provider_ref(value)
            if key.endswith("_id"):
                _require_canonical_id(value, key)
        copied[key] = value
    return MappingProxyType(copied)


def _require_audit_string(value: str, field_name: str) -> str:
    _require_safe_text(value, field_name, max_length=200)
    if _AUDIT_VALUE_RE.fullmatch(value) is None:
        raise ValidationError(f"{field_name} 无效")
    tokens = frozenset(re.split(r"[_.:-]", value.casefold()))
    if tokens & _SECRET_TOKENS:
        raise ValidationError(f"{field_name} 无效")
    return value


def _require_canonical_id(
    value: object,
    field_name: str,
    *,
    prefix: str | None = None,
) -> str:
    if (
        not isinstance(value, str)
        or _CANONICAL_ID_RE.fullmatch(value) is None
        or (prefix is not None and not value.startswith(f"{prefix}_"))
    ):
        raise ValidationError(f"{field_name} 无效")
    return value


def _require_provider_ref(value: str) -> str:
    lowered = value.casefold()
    if (
        not 1 <= len(value) <= 200
        or value != value.strip()
        or any(character.isspace() for character in value)
        or "://" in value
        or "@" in value
        or any(
            marker in lowered
            for marker in ("bearer", "token", "secret", "password", "authorization")
        )
    ):
        raise ValidationError("provider_ref 无效")
    return value
