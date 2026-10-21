"""业务回复模型的受信绑定；正文和模型输出不得授予调用身份。"""

from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import ValidationError as SchemaValidationError

from agent_runtime.assistant.reads import EmployeeScopeFactory
from agent_runtime.gateway_model import GatewayJsonModelClient
from agent_runtime.qualification_agent.agent import (
    QualificationAgent,
    ReplyClassificationResult,
)
from agent_runtime.qualification_agent.openai_port import StructuredReplyModelPort
from domains.assistant.schemas import AssistantActor
from domains.assistant.service import ModelConfigurationService
from domains.conversations.service import require_reply_internal_access
from domains.employees.errors import EmployeeNotFoundError
from domains.employees.permissions import Actor
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import EmployeeId, MessageId, RunId, TenantId
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationError,
    ModelGenerationPort,
)
from shared.schemas.qq_trial import QQTrialGate
from shared.schemas.quote_facts import QuoteEmployeeFact
from tool_gateway.qq_trial_model import TrialMessageGenerators, TrialModelGenerator


class ReplyRunBindingReader(Protocol):
    """读取 canonical 回复关联及全部配置版本历史；不读取正文或凭证。"""

    async def resolve(self, tenant_id: TenantId, message_id: MessageId) -> RunId:
        """只返回该入站消息唯一且仍可分类的规范 Run。"""
        ...

    async def validate(self, tenant_id: TenantId, run_id: RunId) -> MessageId:
        """重核持久关联，拒绝换租户、换消息或换业务对象。"""
        ...

    async def prior(
        self, tenant_id: TenantId, run_id: RunId
    ) -> tuple[tuple[InvocationIdentity, str], ...]:
        """返回所有版本和状态的回复调用身份及模型，不能只查当前版本。"""
        ...


class ReplyRetryAuthority(Protocol):
    async def retry_identity(
        self, tenant: TenantId, message: MessageId, version: str
    ) -> InvocationIdentity | None: ...
    async def authorize_identity(
        self, identity: InvocationIdentity
    ) -> InvocationIdentity: ...


class ReplyModelAuthority:
    """受托员工的当前资格、规范回复关联与模型许可必须同时成立。"""

    def __init__(
        self,
        runs: ReplyRunBindingReader,
        employees: EmployeeScopeFactory,
        lookup: Actor,
        configuration: ModelConfigurationService,
        *,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        model: str,
        retries: ReplyRetryAuthority | None = None,
        recovery_only: bool = False,
    ) -> None:
        self._recovery_only = recovery_only
        self._retries = retries
        self._runs = runs
        self._employees = employees
        self._lookup = lookup
        self._configuration = configuration
        self._tenant_id = tenant_id
        self._employee_id = employee_id
        self._model = model

    async def _actor(self) -> AssistantActor:
        """只读当前员工公开服务，不把配置中的员工编号当成授权。"""
        try:
            async with self._employees(self._tenant_id) as service:
                employee = await service.get_employee(
                    self._tenant_id, self._employee_id, actor=self._lookup
                )
            if employee.employee_id != self._employee_id or not employee.user_id:
                raise ModelGenerationError("permission")
            fact = QuoteEmployeeFact.model_validate(
                {
                    "tenant_id": employee.tenant_id,
                    "employee_id": employee.employee_id,
                    "role": employee.role,
                    "is_active": employee.is_active,
                    "manager_id": employee.manager_id,
                    "team_id": employee.team_id,
                }
            )
            require_reply_internal_access(self._tenant_id, fact, action="qualify")
            return AssistantActor(
                tenant_id=self._tenant_id,
                employee_id=employee.employee_id,
                user_id=employee.user_id,
            )
        except (EmployeeNotFoundError, PermissionDenied, SchemaValidationError):
            raise ModelGenerationError("permission") from None

    async def identity_for_message(
        self, message_id: MessageId, configuration_version: str
    ) -> InvocationIdentity:
        """从持久关联和当前员工产生固定序号，拒绝历史身份漂移。"""
        run_id = await self._runs.resolve(self._tenant_id, message_id)
        actor = await self._actor()
        if self._retries is not None:
            retry = await self._retries.retry_identity(
                self._tenant_id, message_id, configuration_version
            )
            if retry is not None:
                if retry.run_id != run_id:
                    raise ModelGenerationError("permission")
                await self.check(retry)
                return retry
        try:
            identity = InvocationIdentity(
                **actor.model_dump(),
                run_id=run_id,
                capability="reply_qualification",
                configuration_version=configuration_version,
                sequence=0,
            )
        except SchemaValidationError:
            raise ModelGenerationError("permission") from None
        await self.check(identity)
        return identity

    async def check(self, identity: InvocationIdentity) -> None:
        """派发前和返回后均重读；历史任何版本不匹配即拒绝新调用。"""
        if (
            (self._recovery_only and identity.sequence < 1)
            or identity.tenant_id != self._tenant_id
            or identity.employee_id != self._employee_id
            or identity.capability != "reply_qualification"
            or identity.turn_id is not None
            or (
                identity.sequence != 0
                and not (identity.sequence >= 1 and self._retries is not None)
            )
        ):
            raise ModelGenerationError("permission")
        await self._runs.validate(self._tenant_id, identity.run_id)
        actor = await self._actor()
        if actor.user_id != identity.user_id:
            raise ModelGenerationError("permission")
        allowed = [identity]
        if identity.sequence >= 1:
            if self._retries is None:
                raise ModelGenerationError("permission")
            original = await self._retries.authorize_identity(identity)
            allowed.append(original)
        for prior, model in await self._runs.prior(self._tenant_id, identity.run_id):
            authorized_retry = (
                identity.sequence >= 1
                and 0 <= prior.sequence < identity.sequence
                and prior.model_copy(update={"sequence": identity.sequence}) == identity
            )
            if (prior not in allowed and not authorized_retry) or model != self._model:
                raise ModelGenerationError("permission")
        try:
            await self._configuration.authorize(
                actor, identity.configuration_version, probe=False
            )
        except (EmployeeNotFoundError, PermissionDenied, SchemaValidationError):
            raise ModelGenerationError("permission") from None


class BoundReplyClassifier:
    """每次分类独立绑定 Gateway；模型只接收 subject/body 和提取约束。"""

    def __init__(
        self,
        authority: ReplyModelAuthority,
        generator: ModelGenerationPort | TrialMessageGenerators,
        *,
        model: str,
        configuration_version: str,
        max_output_tokens: int,
        retry_generator: Callable[[InvocationIdentity, MessageId], ModelGenerationPort]
        | None = None,
        replay: Callable[[MessageId], Awaitable[ReplyClassificationResult | None]]
        | None = None,
    ) -> None:
        self._replay = replay
        self._retry_generator = retry_generator
        self._authority = authority
        self._generator = generator
        self._model = model
        self._version = configuration_version
        if type(max_output_tokens) is not int or max_output_tokens < 128:
            raise ValidationError("回复输出上限无效")
        # 能力上限不能迫使部署改写既有不可变全局配置版本。
        self._max_output_tokens = min(max_output_tokens, 8192)

    @property
    def model(self) -> str:
        """供分类持久记录标识实际模型。"""
        return self._model

    def uses_trial_gate(self, gate: QQTrialGate) -> bool:
        return (
            isinstance(self._generator, (TrialModelGenerator, TrialMessageGenerators))
            and self._generator.gate is gate
        )

    async def classify(self, *, message: dict[str, str]) -> ReplyClassificationResult:
        """调用身份只来自持久化消息关联，不能由正文或模型结果改变。"""
        if (
            not isinstance(message, dict)
            or not isinstance(message.get("message_id"), str)
            or not message["message_id"].strip()
        ):
            raise ValidationError("回复消息输入无效")
        if self._replay is not None:
            committed = await self._replay(MessageId(message["message_id"]))
            if committed is not None:
                return committed
        identity = await self._authority.identity_for_message(
            MessageId(message["message_id"]), self._version
        )
        generator = (
            self._generator.bind_message(message["message_id"])
            if isinstance(self._generator, TrialMessageGenerators)
            else self._generator
        )
        if identity.sequence >= 1:
            if self._retry_generator is None:
                raise ModelGenerationError("permission")
            generator = self._retry_generator(
                identity, MessageId(message["message_id"])
            )
        client = GatewayJsonModelClient(identity, generator)
        port = StructuredReplyModelPort(
            client, self._model, max_output_tokens=self._max_output_tokens
        )
        return await QualificationAgent(self._model, port, None, None).classify(
            message=message
        )
