# 内置 DeepSeek Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 TradeOS 指挥中心完成持久员工对话、DeepSeek 网关调用、权限内查询、多轮研究提案及 research_only 执行，使这些能力由产品自己的后台运行。

**Architecture:** 沿用现有 FastAPI、领域服务、Tool Gateway 和 Postgres scheduler。新增 assistant 域管理内部会话，DeepSeek Connector 只由模型工具调用，所有模型调用持久计量并绑定当前员工与 Run。业务查询、解释及提案由受限编排完成，不建设任意工具 Agent 循环。

**Tech Stack:** Python 3.12+、FastAPI、Pydantic v2、SQLAlchemy 2.x、PostgreSQL、现有 OpenAI SDK 的 DeepSeek Responses 兼容传输；Vue 3、TypeScript、Vite、Ant Design Vue、Node 24.x。

**Spec:** [已批准设计](../specs/2026-09-22-standalone-deepseek-agent-design.md)，2026-09-22 用户批准。本文只覆盖设计第 4–9 节与 A1–A8、相关 A11–A12；公司共享部署与后续业务集成分别制定计划，不算本批交付。

## Global Constraints

- 一个公司、一个租户、多个员工；不建设 SaaS、模型路由、订阅、积分钱包或桌面端。
- 根九条硬边界不修改；所有外部运行期调用经过 Tool Gateway，不改 `tool_gateway/pipeline.py` 的核心阶段。
- 凭证仅在 Connector 解析；密钥、完整提示词、响应和推理过程不进入通用账本、日志、前端存储。
- 金额只用 Decimal；置信度由代码推导；研究产出最多是 Demand Signal / Need Hypothesis。
- 内部 UI、文档、日志使用中文；代码标识符英文；Web 类型从 OpenAPI 生成。
- 明确配置模型 ID、凭证逻辑引用、输入/输出上限、超时、UTC 调用次数窗口、租户与员工并发上限。测试值不成为公司默认值。
- DeepSeek 固定 `https://api.deepseek.com`，SDK 重试为 0，显式 `reasoning.effort=none`；不回退服务商、模型或推理模式。
- 先保存轮次及执行意图，模型请求交给 scheduler；一般 Agent Worker/Browser Worker 保持 disabled。
- 每个新实体、唯一约束、外键、查询及仓储入口包含 tenant_id。会话仅发起员工可读，标题/列表预览同样裁剪，不用模型自动生成可能泄露的标题。
- 输入、上下文、模型调用、输出应用、历史查看分别核验当前权限；员工停用及对象归属变化立即生效。
- “好”“继续”等聊天文本不代替精确提案确认，也不能批准价格、Campaign 或对外承诺。
- 发送后结果未知不自动再次请求，保留费用预留；显式重新生成另建 attempt，不能重放下游业务动作。
- 首批只新增本机独立运行 profile，保留原 pilot；共享 HTTPS、域名、服务器部署和备份恢复属于第二批。
- 真实调用需要管理员的受信配置及明确额度；凭证缺失不妨碍本地受控实现，live 验收记“未运行”。

## Review Focus

1. 模型调用期间员工被停用或配置已换版本：禁止旧结果继续应用，保留已发生用量；任务 4、8、10 测试。
2. 响应成功而绑定轮次/提案前崩溃：不能自动重付一次模型费，也不能生成两个提案；任务 3、7、8 测试。
3. HTTP 200 但 response 不完整、usage 缺失或模型不匹配：业务失败与费用未知分别表示；任务 2、3 测试。
4. 历史摘要依赖的对象已无权查看：正文与派生总结同时隐藏，不能从聊天历史泄露；任务 6、9、11 测试。
5. UTC 跨日、最后一次配额竞争、202 回执丢失：额度按预留窗口归属，同请求只执行一次；任务 3、5、9 测试。

---

## 执行基线与文件地图

设计提交为 `eee51ef`，位于 `codex/web-internal-pilot`。执行时先使用 using-git-worktrees 技能，从含本计划的已提交版本建立独立 `codex/builtin-deepseek-agent` 分支。不要从旧根目录 main 开始。保留原工作树用户修改的尼日利亚研究文档及所有未跟踪验收资料，不复制、不清理、不提交它们。

进入目录先读上级和就近 AGENTS.md；实现前读 HANDBOOK.md。下文路径相对**新的执行工作树**，不是要求修改旧工作树。任务所列文件之外若需扩大跨层契约，先补同一 ADR，不绕过规则。

| 任务 | 新增文件职责 | 修改的既有接点 |
| --- | --- | --- |
| 1 | `shared/schemas/model_invocation.py`：受信调用/usage 契约；ADR 0070 | identifiers、架构边界、composition_support 规则 |
| 2 | `connectors/deepseek/{AGENTS.md,client.py,manifest.py}`：协议和秘密解析 | connectors 目录登记 |
| 3 | `tool_gateway/model_usage.py`、`infra/db/model_usage.py`、迁移 0061 | tables；持久预留/结算 |
| 4 | `tool_gateway/handlers/model_generate.py`、`tool_gateway/checks/model.py`、`agent_runtime/gateway_model.py` | 显式装配，不改 pipeline |
| 5 | `domains/assistant/` 七个规则/契约文件及 service_impl；`infra/db/assistant.py`、迁移 0062 | tables、域登记 |
| 6 | `agent_runtime/assistant/{AGENTS.md,context.py,reads.py}` | 现有领域公开读服务，不扩大权限 |
| 7 | `agent_runtime/assistant/{decision.py,proposal.py}`、迁移 0063 | directives 公开幂等提案接口与仓储 |
| 8 | `workflows/assistant/{AGENTS.md,flow.py,steps.py,ports.py}`、`apps/scheduler_worker/assistant.py` | scheduler 的规范装配和注册 |
| 9 | `apps/api/routers/assistant.py` | runtime API、依赖及 OpenAPI |
| 10 | `apps/composition_support/model.py`、`apps/api/standalone.py`、`apps/scheduler_worker/standalone.py`、`apps/api/routers/model_settings.py`、`infra/standalone/{AGENTS.md,settings.py,model-settings.example.json}` | 能力状态、新 profile，保留 pilot |
| 11 | Web AgentSessionList/AgentConversation/AgentTurnCard/ModelSettingsPanel 与 useAgentSession | CommandCenter、SettingsCenter、生成类型 |
| 12 | `apps/scheduler_worker/research_model_binding.py` | research_factory 绑定规范 Run 与网关模型 |
| 13 | `scripts/accept_builtin_agent.py`、业务 evals 与运行说明 | 能力矩阵及本批验收记录 |

迁移顺序：0060 → 0061_model_usage → 0062_assistant_sessions → 0063_assistant_proposal_source；执行前核对当前 Alembic head。若存在他人新增版本，只调整本批编号及 down_revision 并同步此表，不修改别人的迁移。ADR 0070 同理检查编号冲突。

## 统一测试与提交约定

- Python 命令在本批 Python 3.12+ venv 中执行；下文 `python` 指已激活的该解释器。
- 每项先写测试，运行应红，再实现，重复同一命令应绿；语法/环境错误不能当成有价值的红灯。每个大步骤按其子项逐个执行，避免一次生成全部代码。
- 数据库并发测试使用 `tests/integration/conftest.py` 的 `integration_engine`，每个竞争者独立 `async_sessionmaker` 事务。禁止用 SQLite 或同连接 savepoint 证明锁/恢复正确。
- 每项提交前执行 `python scripts/check_boundaries.py`，只显式暂存该项文件；不使用 `git add .`。新生产文件完成时移除其 stub 豁免，不为本批添加空壳豁免。
- 代码块给出决定接口和行为的实现部分；构造器按 Interfaces 的字段注入，不隐式读取环境/全局对象。测试只使用代码块定义的值或已明确的现有 fixture。

### Task 1：确定受信模型调用契约和新边界

**Files:** Create `shared/schemas/model_invocation.py`、`docs/adr/0070-builtin-deepseek-agent.md`；Modify `shared/schemas/identifiers.py`、`docs/architecture/02-boundaries.md`、`apps/composition_support/AGENTS.md`；Test `tests/unit/test_model_invocation.py`。

**Interfaces:** 新增 NewType：AgentSessionId、AgentTurnId、ModelInvocationId。`InvocationIdentity(tenant_id: TenantId,user_id: UserId,employee_id: EmployeeId,run_id: RunId,turn_id: AgentTurnId|None,capability: str,configuration_version: str,sequence: int)`；`ModelRequest(model: str,system_prompt: str,payload: dict[str,object],max_output_tokens: int)`；`ModelUsage(input_tokens: int|None,cached_input_tokens: int|None,output_tokens: int|None)`；`ModelResponse(text: str,model: str,usage: ModelUsage)`。全部 Pydantic frozen/extra=forbid；严格整数拒绝 bool、负数，sequence 从 0 起。正文 `repr=False`，identity 不含 secret_ref。此处同时定义 `ModelLimits(window_seconds:int,tenant_calls:int,employee_calls:int,tenant_concurrency:int,employee_concurrency:int,max_input_bytes:int,max_output_tokens:int,timeout_seconds:int)`：所有值严格正整数、无默认值，供技术配额和域 DTO 共用。

- [ ] 写输入/输出契约测试，覆盖任意额外身份字段、usage 缺失、缓存大于输入及凭证不出现在 repr。

```python
import pytest
from pydantic import ValidationError
from shared.schemas.model_invocation import ModelUsage

def test_usage_is_measured_not_coerced():
    assert ModelUsage(input_tokens=None, cached_input_tokens=None,
                      output_tokens=None).input_tokens is None
    with pytest.raises(ValidationError):
        ModelUsage(input_tokens=True, cached_input_tokens=0, output_tokens=1)
    with pytest.raises(ValidationError):
        ModelUsage(input_tokens=2, cached_input_tokens=3, output_tokens=1)
```

- [ ] 执行 `python -m pytest tests/unit/test_model_invocation.py -q`，新模块缺失时红；随后实现契约和缓存一致性检查。

```python
from pydantic import BaseModel, ConfigDict, Field, model_validator

class ModelUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    input_tokens: int | None = Field(strict=True, ge=0)
    cached_input_tokens: int | None = Field(strict=True, ge=0)
    output_tokens: int | None = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def check_cache(self) -> "ModelUsage":
        if (self.input_tokens is not None and
            self.cached_input_tokens is not None and
            self.cached_input_tokens > self.input_tokens):
            raise ValueError("缓存用量超过输入用量")
        return self
```

- [ ] ADR 记录 assistant 域、模型凭证归属、网关一次性结果、账本独立事务/未知执行、提案来源唯一键、延后共享部署。规则只允许 composition_support/model.py 做无 IO 的机械装配，各进程独立实例，不能 import 进程或决定权限。
- [ ] 同命令通过，结构自检通过；显式暂存本项五个生产/文档文件和测试，提交 `feat: define trusted model invocation contracts`。

### Task 2：DeepSeek Connector 的受限协议

**Files:** Create `connectors/deepseek/AGENTS.md`、`client.py`、`manifest.py`；Modify `connectors/AGENTS.md`；Test `tests/unit/test_deepseek_client.py`。

**Interfaces:** 消费 ModelRequest/ModelResponse/ModelUsage。`DeepSeekClient(secret_ref: str,resolver: ModelSecretResolver,timeout_seconds: int,client_factory: Callable[[str,int],AsyncOpenAI]|None=None)`；`async generate(request: ModelRequest)->ModelResponse`、`async aclose()->None`。在本 Connector 定义结构兼容的 ModelSecretResolver Protocol（resolve(str)->str），不 import OpenAI Connector。`decode_response(body: Mapping[str,object],expected_model: str)->ModelResponse` 为纯解析入口。`DeepSeekFailure(code: Literal['authentication','invalid_request','rate_limit','provider_error','invalid_response','unknown'],dispatched: bool,retry_after_seconds: int|None)` 不保存异常原文；未知发送确定性按 dispatched=True 保守处理。

- [ ] 用纯解析测试先锁定 HTTP 成功不等于业务成功。

```python
import pytest
from connectors.deepseek.client import DeepSeekFailure, decode_response

@pytest.mark.parametrize("status", ["incomplete", "failed", "in_progress"])
def test_noncompleted_response_cannot_be_used(status):
    with pytest.raises(DeepSeekFailure) as error:
        decode_response({"status": status, "model": "configured-model",
                         "output": []}, "configured-model")
    assert error.value.code == "invalid_response"
```

- [ ] `python -m pytest tests/unit/test_deepseek_client.py -q` 应红。实现固定客户端和请求，惰性解析 secret，关闭时幂等；不记录 SDK exception/message/request。

```python
import httpx
from openai import AsyncOpenAI

def make_sdk(key: str, timeout_seconds: int) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=key, base_url="https://api.deepseek.com",
        max_retries=0, timeout=timeout_seconds,
        http_client=httpx.AsyncClient(follow_redirects=False,
                                     timeout=timeout_seconds),
    )
```

请求仅包含 model、instructions、JSON 序列化 input、max_output_tokens、store=False、text.format.type=json_object、reasoning.effort=none。输出仅收集 completed assistant message 的 output_text；不收集 reasoning/refusal。解析 JSON 必须对象、有限大小，后续业务 schema 再严格检查；拒绝空正文、不匹配的 model、非 completed。usage 缺失返回 None，不能补 0。

- [ ] 用 httpx.MockTransport 注入 SDK，验证 URL/请求体、307 不跟随、401/429/5xx/断连分类；计数每案恰好一次请求。测试以哨兵密钥和正文核对 repr、caplog、异常均无泄露。未知 alias 只接受明确配置的完全匹配 model，连接测试不匹配则不启用。
- [ ] 同命令与 `python -m pytest tests/unit/test_api_owned_clients.py -q` 通过后自检、提交 `feat: add bounded DeepSeek responses connector`。

### Task 3：持久模型配额与实际用量

**Files:** Create `tool_gateway/model_usage.py`、`infra/db/model_usage.py`、`migrations/versions/0061_model_usage.py`；Modify `infra/db/tables.py`；Test `tests/unit/test_model_usage.py`、`tests/integration/test_model_usage.py`。

**Interfaces:** 消费 Task 1 的 ModelLimits。`ModelUsageRepository.reserve(identity:InvocationIdentity,request_hmac:str,limits:ModelLimits,now:datetime)->Reservation`；`mark_dispatched(tenant_id,invocation_id)->None`；`finish(tenant_id,invocation_id,usage:ModelUsage,state:InvocationState)->None`；`get(tenant_id,invocation_id)->InvocationView`；每个操作 async。Reservation 含 invocation_id、outcome（reserved/duplicate/conflict/limited）、state。InvocationState 为 reserved/dispatched/succeeded/rejected/invalid/unknown。`window_start(now:datetime,seconds:int)->datetime`；`model_cost(usage:ModelUsage,input_rate:Decimal|None,cached_rate:Decimal|None,output_rate:Decimal|None)->Decimal|None`，费率为每百万 tokens，附可信配置的费率版本/币种/来源。

- [ ] 先写跨窗口归属与 Decimal 的测试。

```python
from datetime import datetime, timezone
from tool_gateway.model_usage import window_start

def test_daily_window_is_utc():
    now = datetime.fromisoformat("2026-09-23T00:00:01+08:00")
    assert window_start(now, 86400) == datetime(2026, 9, 22, tzinfo=timezone.utc)
```

- [ ] `python -m pytest tests/unit/test_model_usage.py -q` 应红；实现纯计算。

```python
from datetime import datetime, timedelta, timezone

def window_start(now: datetime, seconds: int) -> datetime:
    if now.tzinfo is None or type(seconds) is not int or seconds <= 0:
        raise ValueError("配额窗口无效")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = (now.astimezone(timezone.utc) - epoch) // timedelta(seconds=1)
    return epoch + timedelta(seconds=(elapsed // seconds) * seconds)
```

- [ ] 迁移建 tenant-bound model_configuration_versions、model_invocations、model_quota_buckets：请求唯一键 `(tenant_id,run_id,capability,configuration_version,sequence)`，employee 必须与可信身份一致；存 request_hmac 不存正文。配置版本不可覆盖；更换模型/配置/密钥不重置已用配额；配额桶按 tenant、employee、window_start，租户桶与员工桶固定顺序锁。并发槽跨窗口计数；未知占用不因跨日/超时静默消失，运营显式解除槽位留痕但不退调用次数。
- [ ] 在真实 PG 测试两连接争最后配额、一员工重放、不同 payload 冲突、另租户独立、午夜结算仍计预留窗口、进程恢复 unknown 不退额度、二次 finish 幂等。测试中持有 workflow Run 行锁时另连接 reserve 必须及时完成；不锁 Run。所有调用边界先 commit dispatched，再进 Connector；reserved 崩溃也需证明未 dispatch 才能回收。
- [ ] 实现事务：锁桶 → 查幂等 → 检查当前配置/两级上限 → 同事务 insert invocation/增计数 → commit；finish 只单向 CAS。provider 请求已经发生但 invalid/unknown 都保留次数。cost 任一必要 usage/费率缺失返回 None，包含缓存且缓存费率缺失也不得假设全按普通输入收费。
- [ ] `python -m pytest tests/unit/test_model_usage.py tests/integration/test_model_usage.py -q` 应绿；在一次性 PG 上升级/降级/升级本迁移，自检、提交 `feat: persist atomic model budgets and usage`。

### Task 4：网关模型工具与受信绑定客户端

**Files:** Create `tool_gateway/handlers/model_generate.py`、`tool_gateway/checks/model.py`、`agent_runtime/gateway_model.py`；Test `tests/unit/test_model_gateway.py`、`tests/integration/test_model_gateway.py`。

**Interfaces:** `ModelGenerationPort.generate(identity:InvocationIdentity,request:ModelRequest)->ModelResponse`（async Protocol 放 shared/schemas/model_invocation.py，不暴露 Connector）。`GatewayJsonModelClient(identity:InvocationIdentity,generator:ModelGenerationPort)` 实现现有 StructuredJsonModelClient.complete_json 同签名。每个客户端只代表一个既定调用序号，不能隐式递增；下一调用由持久编排显式分配新 identity。`ModelResponseSlot.put(response)->str`、`take(handle)->ModelResponse`，单次使用、请求私有。`CurrentModelAuthority.check(identity)->None`（async，模型工具检查端口），实现不缓存授权。

- [ ] 写 wrapper 的受信绑定测试；FakeGenerator 在测试直接定义，无 SDK。

```python
from agent_runtime.gateway_model import GatewayJsonModelClient
from shared.schemas.model_invocation import (
    InvocationIdentity, ModelResponse, ModelUsage,
)

async def test_wrapper_keeps_trusted_identity():
    seen = []
    class FakeGenerator:
        async def generate(self, identity, request):
            seen.append((identity, request))
            return ModelResponse(text='{"kind":"clarify"}', model=request.model,
                usage=ModelUsage(input_tokens=1,cached_input_tokens=0,output_tokens=1))
    identity = InvocationIdentity(tenant_id="tenant_test",user_id="usr_test",
        employee_id="emp_test",run_id="run_test",turn_id=None,
        capability="product_help",configuration_version="test-v1",sequence=0)
    client = GatewayJsonModelClient(identity, FakeGenerator())
    assert await client.complete_json(model="test-model",system_prompt="规则",
        payload={"tenant_id":"attacker"},max_output_tokens=64) == '{"kind":"clarify"}'
    assert seen[0][0] == identity
```

- [ ] `python -m pytest tests/unit/test_model_gateway.py -q` 应红；实现 wrapper，其内部仅构造请求并调用 generator，不 import SDK。

```python
async def complete_json(self, *, model, system_prompt, payload, max_output_tokens):
    request = ModelRequest(model=model, system_prompt=system_prompt,
                           payload=dict(payload), max_output_tokens=max_output_tokens)
    result = await self._generator.generate(self._identity, request)
    return result.text
```

- [ ] 注册 `model.generate` manifest：MEDIUM、付费类别、幂等 REQUIRED、tenant/permission/playbook/idempotency/rate_limit 顺序，安全 input 仅 invocation 引用；实际正文通过 PreparedToolCall 私有 payload。playbook 检查公司模型外发许可，产品说明不要求研究市场政策。handler 授权及额度预留后才创建 Connector。HMAC 由受信 fingerprint provider 构造，不能用密钥值当配置 fingerprint。
- [ ] Gateway 通用 result 仅 provider_ref/configuration_version/status；正文经一次性 slot 交回当前可信 workflow，finally 清理。duplicate 找不到已保存的业务结果时标 unknown，不尝试复建 slot 或调用 Provider。账本成功不能单独证明轮次结果已落库。
- [ ] PG 测试：权限拒绝和额度拒绝 secret resolver 调用数为 0；同请求重放 SDK 为 1；发送后断连 SDK 为 1 且 unknown；旧配置或员工停用调用被拒绝；日志/outbox/ledger 含哨兵正文数量为 0。明确把 reconciliation_required 映射 unknown，不照通用 retryable 标记自动重试。
- [ ] `python -m pytest tests/unit/test_model_gateway.py tests/integration/test_model_gateway.py tests/unit/test_tool_gateway_pipeline.py -q` 通过，自检、提交 `feat: route model calls through trusted gateway`。

### Task 5：会话、轮次和持久执行意图

**Files:** Create `domains/assistant/{AGENTS.md,models.py,schemas.py,service.py,repository.py,events.py,errors.py,service_impl.py}`、`infra/db/assistant.py`、`migrations/versions/0062_assistant_sessions.py`；Modify `infra/db/tables.py`、`docs/architecture/02-boundaries.md`；Test `tests/unit/test_assistant.py`、`tests/integration/test_assistant.py`。

**Interfaces:** schemas 定义 `AssistantActor(tenant_id:TenantId,user_id:UserId,employee_id:EmployeeId)`（仅后端构造）；`ObjectRef(kind:Literal['need','opportunity','handoff','run','product_doc'],object_id:str,version:str|None)`；`TurnInput(text:str,object_refs:tuple[ObjectRef,...],idempotency_key:str)`；`SessionView(session_id,created_at,version)`；`TurnView(turn_id,session_id,run_id,state,created_at,result,attempt_of)`，result 为 Task 7 的 AssistantDecision 在本任务先定义的受限 union。不能使用任意 dict 作为 API result。

`AssistantService.create_session(actor)->SessionView`、`list_sessions(actor)->list[SessionView]`、`accept_turn(actor,session_id,input)->TurnView`、`get_turn(actor,session_id,turn_id)->TurnView`、`cancel_turn(actor,session_id,turn_id)->TurnView`、`regenerate(actor,session_id,turn_id,idempotency_key)->TurnView`，全部 async。服务调用 repository 协议，SQL 仅 infra；外部可见结果另经 Task 6 裁剪。`is_active_turn(state:str)->bool`。`AssistantRepository.accept(actor,session_id,input,request_hmac,run_id)->TurnView` 原子执行，不在 API 双写意图。

- [ ] 写状态槽测试与幂等集成测试，正文凭证 guard 在落库前执行。

```python
from domains.assistant.models import is_active_turn

def test_delivered_turn_releases_session_slot():
    assert is_active_turn("queued")
    assert is_active_turn("running")
    assert not is_active_turn("awaiting_input")
    assert not is_active_turn("proposal_ready")
    assert not is_active_turn("unknown")
```

- [ ] `python -m pytest tests/unit/test_assistant.py -q` 应红；实现状态表及模型。

```python
ACTIVE_TURN_STATES = frozenset({"queued", "running"})

def is_active_turn(state: str) -> bool:
    return state in ACTIVE_TURN_STATES
```

- [ ] 同事务创建 turn 和 dispatch intent，并预分配 canonical RunId。建部分唯一索引 `(tenant_id,session_id) WHERE state IN ('queued','running')`；幂等键 `(tenant_id,employee_id,session_id,idempotency_key)` 搭 request_hmac，先返回已有相同请求再判断 active 槽。turn.run_id 唯一；复合 FK 防止跨租户。输入、模型解释、受信业务引用分别存储。允许的状态转移显式列出，终态不回 running。
- [ ] PG 断言原始 accept 两次返回同一 turn/run；相同键不同正文冲突；另一员工（含 boss）读会话失败；另一租户伪造 ID 失败；不同输入争同一会话一胜一 409；awaiting_input 后可新轮；含秘密输入 turn/intent 计数均未变。关闭会话权限不依赖角色猜测。
- [ ] cancel 仅阻止本 turn 后续动作，保留模型计量和独立 research Run；regenerate 仅允许明确的失败/未知轮，生成新 id 与 attempt_of，原结果不覆盖、原配额不退。取消已派发请求不声称撤回。
- [ ] `python -m pytest tests/unit/test_assistant.py tests/integration/test_assistant.py -q` 通过，迁移前后旧登录/Run 数据可读，自检、提交 `feat: persist private assistant sessions and turns`。

### Task 6：当前权限上下文与可追溯的只读查询

**Files:** Create `agent_runtime/assistant/AGENTS.md`、`context.py`、`reads.py`；Test `tests/unit/test_assistant_context.py`、`tests/integration/test_assistant_reads.py`。

**Interfaces:** 消费 AssistantActor、ObjectRef 和各域公开服务。业务列表另定义 `AssistantReadQuery(kind:Literal['need','opportunity','handoff','run'],limit:int,cursor:str|None)`，limit 为严格整数 1–50，cursor 为租户和 actor 绑定的签名游标；`AssistantReadPort.list(actor:AssistantActor,query:AssistantReadQuery)->tuple[AuthorizedFragment,...]`（async），底层只查当前范围，不能先全量读取再筛选。`AuthorizedFragment(text:str,dependencies:tuple[ObjectRef,...],source_turn_ids:tuple[AgentTurnId,...])` 定义于 assistant/schemas.py；`filter_fragments(fragments:Sequence[AuthorizedFragment],visible_refs:frozenset[ObjectRef])->tuple[AuthorizedFragment,...]`；`AssistantReadPort.read(actor:AssistantActor,ref:ObjectRef)->AuthorizedFragment`（async Protocol，放 reads.py）；`AssistantContextBuilder.build(actor,session_id,turn_id)->AssistantContext`（async）。AssistantContext 包含 actor、当前角色 capabilities、可见 fragments、明确原话字段、已裁剪计数、当前配置版本，禁止 secret。`CurrentAssistantIdentity.resolve(actor)->tuple[str,frozenset[str]]`（async Protocol）从当前员工服务解析角色与能力。

- [ ] 写撤权后整段摘要隐藏测试。

```python
from agent_runtime.assistant.context import filter_fragments
from domains.assistant.schemas import AuthorizedFragment, ObjectRef

def test_hidden_dependency_removes_whole_summary():
    a = ObjectRef(kind="opportunity", object_id="opp_a", version=None)
    b = ObjectRef(kind="need", object_id="need_b", version=None)
    summary = AuthorizedFragment(text="两个对象的合并摘要",
        dependencies=(a,b), source_turn_ids=())
    assert filter_fragments([summary], frozenset({a})) == ()
```

- [ ] `python -m pytest tests/unit/test_assistant_context.py -q` 应红；实现整段筛除，并确保派生摘要依赖闭包是所有输入依赖的并集。

```python
def filter_fragments(fragments, visible_refs):
    return tuple(fragment for fragment in fragments
                 if set(fragment.dependencies).issubset(visible_refs))
```

- [ ] 建明确业务读 adapter：Need、Opportunity、Handoff、Run 分别调用其公开服务/授权审计端口。用户可见 ID 必须本次读取过，不能让模型编一个 ID 生成链接。读 Run 不沿用系统 actor；老板租户范围、经理团队范围、销售自身范围按原领域权限映射；未适配角色只给 product_help。明确 UserId→EmployeeId 解析，不能 cast 或自动把 finance/viewer 当 sales。
- [ ] 上下文先身份重核、再读取、最后限长；必备系统规则/排除项/预算不能裁剪，放不下返回固定超限错误；只裁剪背景并记计数。业务引文标记 untrusted，不作为系统消息。产品说明按版本化本地受信文档提取固定内容，不接受员工任意路径。
- [ ] PG 测试停用、归属转移、跨租户、来源被删除、经理调组和历史摘要复用；被隐藏的业务原文及其派生总结均不出现在模型输入或 API。模型输出事实引用集合必须为本次 authorized read 的子集，缺来源返回缺项。
- [ ] `python -m pytest tests/unit/test_assistant_context.py tests/integration/test_assistant_reads.py tests/unit/test_context_builder.py -q` 通过，自检、提交 `feat: authorize assistant context and history on every read`。

### Task 7：多轮澄清与幂等研究提案

**Files:** Create `agent_runtime/assistant/decision.py`、`proposal.py`、`migrations/versions/0063_assistant_proposal_source.py`；Modify `domains/assistant/schemas.py`、`domains/directives/service.py`、`service_impl.py`、`repository.py`、`infra/db/repositories/directives.py`、`infra/db/tables.py`；Test `tests/unit/test_assistant_decision.py`、`tests/integration/test_assistant_proposal.py`。

**Interfaces:** Task 5 的 result union 在 schemas.py 一次定义，Task 7 实现校验：`Clarification(kind='clarify',questions:tuple[str,...],missing_fields:tuple[str,...])`、`ReadRequest(kind='read',refs:tuple[ObjectRef,...],query:AssistantReadQuery|None)`（refs 与 query 恰好一种，refs 最多 10 条）、`Explanation(kind='explain',fragments:tuple[AuthorizedFragment,...])`、`ResearchDraft(kind='research',fields:tuple[SourcedField,...])`，均 extra=forbid、frozen。`SourcedField(name:str,value:str,source_turn_id:AgentTurnId|None,policy_version:str|None)` 必须二选一出处。`AssistantDecision` 是按 kind 判别的以上 union。模型不能写 policy_version/turn_id 的可信性，代码必须逐条验证来源及值。

`parse_decision(text:str)->AssistantDecision`；`unsupported_fields(fields:Sequence[SourcedField],allowed:Mapping[str,frozenset[str]])->tuple[str,...]` 检查值与受信候选集；`ResearchProposalBuilder.build(context:AssistantContext,draft:ResearchDraft)->DemandDiscoveryPlanInput|Clarification`（async）。复用现有 DemandDiscoveryPlanInput，不重新定义预算字段。

DirectiveService 增加 `submit_discovery_proposal_once(tenant_id:TenantId,source_turn_id:AgentTurnId,source_version:int,request_hmac:str,raw_text:str,plan:DemandDiscoveryPlanInput,interpretation_summary:str,expected_behavior_changes:list[str],parsed_by:str)->str`（async）。内部原提案构建规则不变；新增来源唯一记录和 proposal 在同一 UoW，精确重复返回旧 ID，不同 HMAC 冲突；source_version 是确定性候选版本，模型不提供。确认仍沿原 confirm_proposal 公开接口。

- [ ] 测试模型不能用默认值补预算/市场，不能把聊天肯定词变成确认动作。

```python
import pytest
from pydantic import ValidationError
from agent_runtime.assistant.decision import parse_decision

def test_freeform_confirmation_is_not_a_model_action():
    with pytest.raises(ValidationError):
        parse_decision('{"kind":"confirm","proposal_id":"dpr_other"}')
    result = parse_decision('{"kind":"clarify","questions":["研究哪些国家？"],'
                            '"missing_fields":["target_countries"]}')
    assert result.kind == "clarify"
```

- [ ] `python -m pytest tests/unit/test_assistant_decision.py -q` 应红；解析使用 Pydantic 判别 union，不用 eval/字符串路由。

```python
from pydantic import TypeAdapter
from domains.assistant.schemas import AssistantDecision

def parse_decision(text: str) -> AssistantDecision:
    return TypeAdapter(AssistantDecision).validate_json(text)
```

- [ ] 分别实现字段收集、来源核验、缺项澄清、最终计划转换。用户原话→字段提取保留具体轮次；当前已确认政策可提供值但必须展示版本。本轮明确修正覆盖旧值并留下新出处；冲突未消解先问。模型推测/测试预算不能转成用户确认。三条线路、预算、禁止外发范围由既有 schema 与业务代码约束；提示词不构成唯一防线。
- [ ] PG 测试 submit_once 两连接/重启重放返回同一提案、相同 source 不同请求冲突；在提案事务提交后、绑定会话前抛异常，恢复绑定原 ID；普通销售不得生成可确认提案；降权/旧版本无法确认；文本“继续”只产生下一 turn，审批数不增加。
- [ ] `python -m pytest tests/unit/test_assistant_decision.py tests/integration/test_assistant_proposal.py tests/unit/agent_runtime/test_research_proposal.py -q` 通过，自检、提交 `feat: build sourced research proposals from assistant turns`。

### Task 8：scheduler 持久交互流程和恢复

**Files:** Create `workflows/assistant/{AGENTS.md,flow.py,ports.py,steps.py}`、`apps/scheduler_worker/assistant.py`；Modify `apps/scheduler_worker/bootstrap.py`、`runtime.py`；Test `tests/unit/test_assistant_workflow.py`、`tests/integration/test_assistant_recovery.py`。

**Interfaces:** 消费 Task 4–7。`build_assistant_definition()->WorkflowDefinition`；`AssistantStepHandler.execute(run:WorkflowRun)->tuple[str,str|None,dict[str,object]]` 实现现有 StepHandler 语义。`AssistantDispatcher.dispatch(tenant_id:TenantId,limit:int)->int`（async）认领持久 intent 并按预分配 RunId 创建/核对/绑定。`AssistantRuntimePorts` frozen dataclass：assistant_service、context_builder、read_port、model_generator、proposal_builder、directive_service、current_identity；类型均来自前项 Protocol/公开服务，无全局服务定位器。bootstrap 新增可选 assistant_factory，工厂接当前 SchedulerCoreServices，并显式获得同一 engine；不新建第二 engine/registry。

- [ ] 写无自动模型重试的结构测试与恢复场景。

```python
from workflows.assistant.flow import build_assistant_definition

def test_model_step_has_no_implicit_retry():
    definition = build_assistant_definition()
    model_step = next(s for s in definition.steps if s.step_name == "generate")
    assert model_step.max_retries == 0
```

- [ ] `python -m pytest tests/unit/test_assistant_workflow.py -q` 应红；定义 load_context → generate → apply_result → complete。read decision 只允许一个受限读取阶段再生成解释，设明确最大模型次数且每步固定 sequence。计划不能含任意循环或动态 handler_ref。

```python
from workflows.engine.runner import StepDefinition

def generate_step() -> StepDefinition:
    return StepDefinition(step_name="generate", handler_ref="assistant.generate",
                          max_retries=0)
```

- [ ] dispatcher 的唯一 Run 创建要求数据库唯一 identity。若现有 engine.start 不能接受预分配 ID，新增窄 `start_once` 公共端口并保持原 start 不变，文件为 `workflows/engine/runner.py` 与 `infra/db/workflow_engine.py`，写入同一 Run 唯一约束。禁止“先查没有再 start 随机 ID”。崩溃恢复重查 canonical Run 后绑定，不创建第二个。
- [ ] generate 前重核角色/配置/cancel；响应只在当前权限和 schema/来源均通过后写会话。模型返回后无法持久结果，恢复时依据调用账本置 unknown；绝不根据“turn 无结果”自动重调。apply_result 幂等使用 Task 7 source_turn/version；run context 仅安全引用，不保存整段聊天。权限失效结果置 blocked，同时已发生 usage 保留。
- [ ] PG 测试窗口：接纳后未 dispatch、start 后未绑定、dispatch 后 provider 超时、成功后未写结果、结果已写未 advance、提案已存未绑定、停止后恢复。逐案计数 provider≤1、proposal≤1、run=1；模型未知终止该 turn 的自动推进。对已 cancel/blocked 的迟到结果不展示；关闭浏览器不影响已接纳后台流程。
- [ ] `python -m pytest tests/unit/test_assistant_workflow.py tests/integration/test_assistant_recovery.py -q` 通过，自检、提交 `feat: run durable assistant turns in scheduler`。

### Task 9：员工会话 API 和明确的重生成语义

**Files:** Create `apps/api/routers/assistant.py`；Modify `apps/api/composition/runtime.py`、`apps/api/runtime.py`、`apps/web/scripts/export_openapi.py`；Test `tests/unit/test_assistant_api.py`、`tests/integration/test_assistant_api.py`。

**Interfaces:** `create_assistant_router(dependencies:AssistantApiDependencies)->APIRouter`，dependencies 定义于新 router 同文件，包含 actor resolver、AssistantService、授权读裁剪端口。`AssistantActorResolver.resolve(request:Request)->AssistantActor`（async Protocol）接现有认证；`AssistantTurnProjector.project(actor:AssistantActor,turn:TurnView)->TurnView`（async Protocol）调用 Task 6 当前授权裁剪。路由 DTO 引用 domains/assistant/schemas.py，不接受 tenant/role/employee/tool 参数。

- `POST /agent/sessions` → 201 SessionView；`GET /agent/sessions` → 当前员工列表。
- `GET /agent/sessions/{session_id}/turns` → 当前可见轮次列表；`POST` 同路径 → 202 TurnView，含持久 turn/run。
- `GET /agent/sessions/{session_id}/turns/{turn_id}` → 当前权限的 TurnView。
- `POST .../{turn_id}/cancel` → TurnView；`POST .../{turn_id}/regenerate`（幂等键必填）→ 202 新 TurnView。
- 不可见会话/轮次为 404；已停用登录为 401/403（沿原认证规则）；活动轮冲突 409；未配置模型 503 固定缺项；限流 429 带安全等待值。

- [ ] 写输入身份不可由浏览器指定的单元测试。

```python
import pytest
from pydantic import ValidationError
from domains.assistant.schemas import TurnInput

def test_browser_cannot_supply_authority():
    with pytest.raises(ValidationError):
        TurnInput.model_validate({"text":"查我的机会", "object_refs":[],
            "idempotency_key":"test-request", "role":"boss"})
```

- [ ] `python -m pytest tests/unit/test_assistant_api.py -q` 应红；router 每次从现有认证取当前身份，先校验 input，再调用领域服务；HTTP 202 必须在 accept 事务成功之后。

```python
from fastapi import Response

def accepted_response(response: Response) -> None:
    response.status_code = 202
    response.headers["Cache-Control"] = "no-store"
```

- [ ] GET 每次裁剪历史并 no-store；错误不回显输入、密钥引用或别人的 active turn。对隐藏文本返回固定“当前无权查看相关内容”，不能返回已存原 result 给前端自筛。恢复 POST 丢失响应靠原幂等键读取，不自动换键。
- [ ] 真实 HTTP/PG 测试 accept commit 后主动断开客户端，重新提交同键读到相同 turn/run；CSRF 缺失拒绝；换用户同 session ID 隐藏；权限撤销后已完成摘要不再可读；cancel 与迟到响应竞争不会显示结果；regenerate 不新建 research Run 或重新确认提案。
- [ ] 公司 profile 对旧即时 `/commands/discovery-proposals` 若无受信 Run 绑定，返回明确 unavailable；保留纯查询与既有精确确认路径。新路径不 HTTP 回调自己。OpenAPI 导出必须包含新 DTO 且不导出凭证配置类型。
- [ ] `python -m pytest tests/unit/test_assistant_api.py tests/integration/test_assistant_api.py -q` 通过，自检、提交 `feat: expose authenticated assistant sessions API`。

### Task 10：独立本机配置、模型状态和显式连接测试

**Files:** Create `infra/standalone/{AGENTS.md,settings.py,model-settings.example.json}`、`apps/composition_support/model.py`、`apps/api/standalone.py`、`apps/scheduler_worker/standalone.py`、`apps/api/routers/model_settings.py`；Modify `shared/schemas/runtime_capabilities.py`、`apps/api/composition/runtime.py`；Test `tests/unit/test_standalone_model_settings.py`、`tests/integration/test_standalone_model_runtime.py`。

**Interfaces:** `StandaloneModelSettings(provider:Literal['deepseek'],model:str,secret_ref:str,configuration_version:str,limits:ModelLimits,model_data_export_enabled:bool)` 仅服务器端类型。`load_model_settings(path:Path)->StandaloneModelSettings`；`public_model_settings(settings:StandaloneModelSettings,runtime:ModelRuntimeStatus)->ModelSettingsView` 排除 secret_ref。ModelRuntimeStatus 包含 API/scheduler 实际版本、最近心跳、对应配置验证时间和失败分类，由数据库当前记录构造。ModelSettingsView 放 domains/assistant/schemas.py：provider/model/configuration_version/status/verified_at/failure_code/limits、可执行 probe 标志；status 为 missing/pending_restart/unverified/verified/failed，不能当成 runtime capability enabled 的替代。

`ModelConfigurationService.get_public(actor)->ModelSettingsView`、`save_nonsecret(actor,input:ModelSettingsUpdate)->ModelSettingsView`、`request_probe(actor,idempotency_key:str)->TurnView`（async，协议放 assistant/service.py）。ModelSettingsUpdate 仅 model 和 limits、expected_version，不含 secret 或任意 endpoint。配置持久化归 Task 3 配置版本表；改变后 pending_restart。部署 loader 提供启动版本及秘密引用，API/scheduler 启动注册各自版本，未达一致不给 verified。`build_model_composition(...)` 接受受信 settings、resolver、authority、usage repo、gateway ledger 与 fingerprint provider，返回独立 generator/aclose，不读取环境。

- [ ] 写所有限额必须显式提供、不允许额外 base URL 的测试。

```python
import pytest
from pydantic import ValidationError
from infra.standalone.settings import StandaloneModelSettings

def test_no_implicit_paid_defaults():
    with pytest.raises(ValidationError):
        StandaloneModelSettings.model_validate({"provider":"deepseek", "model":"test"})
```

- [ ] `python -m pytest tests/unit/test_standalone_model_settings.py -q` 应红；配置数据保持严格白名单。

```python
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from shared.schemas.model_invocation import ModelLimits

class StandaloneModelSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    provider: Literal["deepseek"]
    model: str = Field(min_length=1)
    secret_ref: str = Field(min_length=1, repr=False)
    configuration_version: str = Field(min_length=1)
    limits: ModelLimits
    model_data_export_enabled: bool
```

- [ ] 例子文件只展示字段说明/拒绝启用的未配置状态，不放可付费运行默认阈值。secret 只经原受限 resolver 引用。后台按显式部署配置启动，不能读用户 shell 的任意文件，也不在日志打印完整 settings。两进程分别创建/关闭自己的 SDK、slot、Gateway、DB 资源。
- [ ] 模型设置路由只允许当前老板/现有管理权限，保存非秘密配置不自动 probe；probe 是固定短 JSON 请求，通过 Task 8 持久轮次 type/capability=model_probe、统一 Gateway/租户+员工预算。探测结果只对对应配置版本有效，配置改变/跨进程版本不同显示待重启。普通员工只看安全能力状态，不能触发探测或看 secret_ref。配置轮次不混入员工自然语言历史。
- [ ] 扩展 capability 具名 builtin_assistant/model，UI 分开显示装配、配置验证、最近失败；不把现有 agent/browser 标 enabled。缺 Tavily 仍可 chat/help/draft，执行 research 显示缺项。原 pilot 继续拒绝真实外网模型，不移除 loopback origin 限制。
- [ ] PG 集成通过新 profile 启动 API/scheduler，使用受控 HTTP Provider，验证启动零付费请求、点击 probe 恰好一次、重复请求仍一次、两个进程总配额统一、v1 结果不能验证 v2、取消/停用不让迟到 probe 启用。未配置或不匹配不得直连旧 OpenAI wrapper。
- [ ] `python -m pytest tests/unit/test_standalone_model_settings.py tests/integration/test_standalone_model_runtime.py tests/integration/test_pilot_runtime.py -q` 通过，自检、提交 `feat: compose standalone DeepSeek runtime and admin probe`。

### Task 11：指挥中心的会话与状态界面

**Files:** Create `apps/web/src/views/command-center/AgentSessionList.vue`、`AgentConversation.vue`、`AgentTurnCard.vue`、`apps/web/src/composables/useAgentSession.ts`、`apps/web/src/views/settings/ModelSettingsPanel.vue`；Modify `apps/web/src/views/command-center/CommandCenter.vue`、`apps/web/src/views/settings/SettingsCenter.vue`、`apps/web/src/api/api.d.ts`；Test `apps/web/tests/assistant.test.ts`、`apps/web/tests/model-settings.test.ts`、`tests/e2e/test_assistant_browser.py`。

**Interfaces:** useAgentSession 接现有 API client，返回 sessionId/turns/loading/error/startSession/send/cancel/regenerate/dispose；DTO 全从 components/schemas 引用。send 生成一次幂等键直到请求确定接纳/拒绝；网络失败保留原键，禁止后台自动新建 attempt。AgentTurnCard props 为生成类型 TurnView；仅发出 cancel/regenerate/openProposal/openRun，不判断业务批准条件。

- [ ] 写状态卡测试；使用既有 createApp/DOM test 方式，不增加组件测试框架。

```typescript
import { createApp, nextTick } from "vue";
import { expect, it } from "vitest";
import AgentTurnCard from "../src/views/command-center/AgentTurnCard.vue";

it("未知结果不显示成功或自动重试", async () => {
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp(AgentTurnCard, { turn: {
    turn_id: "atr_test", session_id: "ase_test", run_id: "run_test",
    state: "unknown", created_at: "2026-09-22T00:00:00Z",
    result: null, attempt_of: null,
  }});
  app.mount(host);
  await nextTick();
  expect(host.textContent).toContain("结果未知");
  expect(host.textContent).toContain("重新生成将再次消耗额度");
  expect(host.textContent).not.toContain("执行成功");
  app.unmount();
  host.remove();
});
```

- [ ] `npm --prefix apps/web test -- assistant.test.ts` 应红；生成 OpenAPI 后实现状态卡。

```vue
<template>
  <a-alert v-if="turn.state === 'unknown'" type="warning"
    message="结果未知"
    description="请求可能已执行。重新生成将再次消耗额度。" />
</template>
```

- [ ] 完成列表/交流/来源/提案/Run 卡组合。说明“研究已发现信号”不能显示“客户需求已验证”。精确确认按钮继续现有接口/当前 can_confirm；聊天 send 不触发确认。配置页点击探测前显示调用计费说明，密钥输入框不进入聊天或浏览器持久存储；该页只展示部署端配置提示和非秘密设置。
- [ ] 轮询固定间隔，只更新当前用户/session；切会话、登出、卸载中止旧请求并校验响应身份世代。隐藏页面降低轮询频率；恢复读取当前状态，不重复 POST。unknown/blocked/failed 分开展示；停止生成与取消 research Run 是不同控件和权限。
- [ ] 测试旧用户迟到响应不污染新用户、撤权后历史文本移除、409 定位已有轮次、202 丢失保留幂等键、probe 必须用户点击、缺搜索可写提案但无法确认执行。真实浏览器在 1440px 和 390px 完成新会话→澄清→卡片→证据跳转→刷新恢复，检查焦点和无横向溢出。
- [ ] `npm --prefix apps/web run gen:api`（解释器指向本批 venv）、typecheck、lint、test、build；`python -m pytest tests/e2e/test_assistant_browser.py -q`。自检、提交 `feat: add persistent assistant conversations to command center`。

### Task 12：研究工作流的模型绑定与真实端口接线

**Files:** Create `apps/scheduler_worker/research_model_binding.py`；Modify `apps/scheduler_worker/bootstrap.py`、`apps/scheduler_worker/standalone.py`、`workflows/demand_discovery/research.py`；Test `tests/unit/test_research_model_binding.py`、`tests/integration/test_builtin_research.py`。

**Interfaces:** `BoundResearchModelFactory.for_step(tenant_id:TenantId,run_id:RunId,step_name:str,sequence:int)->StructuredJsonModelClient`（async），从当前 Run 的正式确认来源重读员工与权限；不从模型返回/任意 context 字典取 actor。bootstrap 新增 research_factory 与既有 ResearchRuntimePorts 互斥，等规范服务创建完再装配；原直接传 ports 的受控测试不变。模型调用序号持久绑定 step 与 attempt，重试同一调用不会产生新序号。

- [ ] 写不接受缺 actor/Run 的模型绑定测试；工厂内部提供纯前置 `validate_binding(run_id:str,employee_id:str)->None` 用于固定错误且不能进行 SDK IO。

```python
import pytest
from shared.errors import ValidationError
from apps.scheduler_worker.research_model_binding import validate_binding

def test_research_model_cannot_run_without_actor():
    with pytest.raises(ValidationError):
        validate_binding("run_test", "")
```

- [ ] `python -m pytest tests/unit/test_research_model_binding.py -q` 应红；实现前置验证并接当前确认事实读端口。

```python
from shared.errors import ValidationError

def validate_binding(run_id: str, employee_id: str) -> None:
    if not run_id or not employee_id:
        raise ValidationError("研究模型缺少受信运行身份")
```

- [ ] 串联 DeepSeek→模型网关，Tavily→既有搜索网关/免费额度，PublicPage→页面网关/Artifact，保留国家政策/页面许可；三个出口分别配额。未知模型执行必须使研究步骤进入人工可见未知/阻断，不被既有 max_retries 默认值再次调用；使用已存在可表达状态，必要时版本化研究 flow，不覆盖正在执行旧版本。
- [ ] PG 测试通过已认证产品 API 多轮输入、精确确认、规范 scheduler 推进，到存储 Signal/Hypothesis 与证据来源。Provider/Search/Page 用受控 transport，但 gateway/PG/Artifact 真实；验证所有调用带同一租户和正确业务 Run，普通销售无法借复制老板会话执行。零联系人补全、零发送、零报价、零 ValidatedNeed 晋升。
- [ ] 无搜索配置时 chat 和草稿通过，确认 research 返回准确缺项；无模型配置全部模型动作拒绝；取消 chat turn 不取消已确认的独立 research Run。source proposal 重放只启动一个 research Run。
- [ ] `python -m pytest tests/unit/test_research_model_binding.py tests/integration/test_builtin_research.py tests/integration/test_research_acceptance.py -q` 通过，自检、提交 `feat: bind research execution to metered DeepSeek calls`。

### Task 13：同版本验收、业务评估和使用说明

**Files:** Create `scripts/accept_builtin_agent.py`、`tests/unit/test_builtin_agent_acceptance.py`、`tests/evals/test_builtin_agent_evals.py`、`tests/evals/assistant/clarification.jsonl`、`docs/operations/builtin-deepseek-agent.md`、`docs/operations/2026-09-22-builtin-deepseek-acceptance.md`；Modify `docs/operations/web-core-capability-matrix.md`、`HANDBOOK.md`。

**Interfaces:** 验收脚本 `main(argv:list[str]|None=None)->int`，默认只受控。`--live` 必须同时有 `--settings-file` 和 `--approved-research-input`，文件内明确管理员限制与研究输入，不能 CLI 传密钥；真实模式经已运行产品认证 API 操作，不绕过角色、提案确认或 gateway。报告字段 source_commit、configuration_version、model_id、prompt_version、controlled、live_model、live_sources、shared_deployment；各结果 passed/failed/not_run，禁止 bool 把未运行当失败/成功。

- [ ] 写未给真实配置就拒绝 live 的测试。

```python
import pytest
from scripts.accept_builtin_agent import parse_args

def test_live_requires_explicit_input_and_settings():
    with pytest.raises(SystemExit):
        parse_args(["--live"])
```

- [ ] `python -m pytest tests/unit/test_builtin_agent_acceptance.py -q` 应红；实现参数门槛。

```python
import argparse

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="内置 Agent 分层验收")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--settings-file")
    parser.add_argument("--approved-research-input")
    args = parser.parse_args(argv)
    if args.live and not (args.settings_file and args.approved_research_input):
        parser.error("真实验收需要部署配置及明确的研究输入")
    return args
```

- [ ] 追加业务 eval 样本：含糊国家、缺预算、连续两次修正预算、排除项冲突、假来源 ID、提示注入、销售冒充老板、客户原话被员工转述、网页参考价、要求概率、中文聊天要求正式报价、模型返回多余动作。每案固定输入/期望拒绝或澄清/合法来源，不改已有样本降低标准。受控模型输出与 live 模型评估结果分栏。
- [ ] 完整静态门：`python scripts/check_boundaries.py`、`python -m ruff check .`、`python -m mypy domains shared tool_gateway connectors`；然后后端 unit→integration→evals，受影响 e2e；Web gen:api/typecheck/lint/test/build。记录实际命令、源码 commit、结果及 skips，数据库/浏览器依赖缺失必须明确记未运行。生成类型后 diff 应只有预期 API 改动，第二次生成不再变化。
- [ ] 在一次性数据库完成迁移升级、旧数据兼容、停 worker→接纳输入→启 worker→完成、worker 在未知窗口重启。检查 loopback pilot 回归，不启动共享入口，不读取真实 .env 来“试试看”。原有不可用能力显示准确。
- [ ] 若管理员已配置真实模型与明确额度，先显式短 probe，再从产品入口执行一项人工明确范围的 research_only 任务；记录真实模型 usage、搜索/页面/Artifact 证据及零发送。无配置则记 A7 not_run，受控交付不能称“真实研究已可用”。真实 eval 也消耗调用额度，不额外绕过限额。
- [ ] 运行说明提供一次配置→显式迁移→启动 API/scheduler/既有依赖→登录→probe→聊天/确认→停止/恢复的实际命令，区分模型余额、搜索额度、维护和服务器持续运行成本。写清首批本机独立后台的能力；公司网址共享与备份恢复的第二批验收仍未完成。
- [ ] 更新能力矩阵/验收记录，审阅全部 diff，执行边界自检，显式提交 `test: verify builtin agent runtime and document readiness`。不自动 merge、部署或发送客户消息。

## 契约补充与执行检查

以下是接口必须一致的约定，避免执行者靠猜测衔接任务：

- Task 5 的 schemas.py 在首次创建时就定义 Task 7 所列 union 与 SourcedField；Task 7 只实现解析和业务核验。ResearchDraft 只有来源字段，不能携带一个未验证的完整计划绕过来源核验。
- ObjectRef 为 frozen Pydantic 模型，可作集合键；visible_refs 中版本必须与读取版本一致。派生摘要来源闭包在保存前确定，不依赖将来从自然语言倒推。
- Task 5 的 AssistantService 还定义 `list_turns(actor:AssistantActor,session_id:AgentSessionId)->list[TurnView]` 供 Task 9；实际返回前始终执行 Task 6 裁剪。领域服务不直接 import agent_runtime，通过公开 fragment-access Protocol 注入。
- Task 10 探测以服务器专用 `turn_kind=model_probe` 记录，普通 TurnInput 不含 turn_kind。Task 5 的持久模型增加 `turn_kind: Literal['conversation','model_probe']`，缺省 conversation；Task 8 专用 probe 路径只校验固定 JSON 与实际模型，不进入业务 decision/提案生成。用户会话列表不列出 probe，管理界面以配置版本查询其安全结果。
- `AssistantRepository.accept` 接受已有 `RunId` 但只在确认尚无该幂等请求时采用；重放忽略调用方新分配 ID，返回数据库 canonical ID。
- Task 8 窄 engine 端口的确定签名为 `start_once(tenant_id:TenantId,run_id:RunId,workflow_type:str,subject_ref:str,context:dict[str,object])->RunId`；相同 ID/相同绑定返回原 ID，冲突拒绝。已有 workflow version 由 registry 固定，恢复不改版本。只有可信 dispatcher 可调用。
- ModelConfigurationService 实现在 assistant/service_impl.py，以 repository 保存配置引用/非秘密元数据，技术配置通过 assistant/repository.py 中的 `AssistantConfigurationRepository` Protocol 保存，SQL 实现复用 Task 3 配置表；域不导入 tool_gateway，运行配额继续由网关负责。凭证逻辑引用只在 infra 启动 settings 存在，不经过 assistant DTO。
- Task 10 public_model_settings 消费显式 ModelRuntimeStatus；它是安全投影，不仅凭配置对象推导 verified。两进程活跃版本与配置一致且 probe 成功才 verified；心跳过期显示 worker unavailable，不凭旧记录允许执行。
- ModelUsageRepository 的所有身份参数使用 Task 1 强类型；finish 只允许 dispatched→succeeded/invalid/unknown 或 reserved→rejected，重复同终态 no-op，不同终态冲突。后台标 unknown 只针对超过明确 lease 且 owner 不再活动的请求，不能抢正在执行的调用。
- Unknown 并发槽释放需增加 `release_unknown_slot(tenant_id:TenantId,invocation_id:ModelInvocationId,operator:UserId,reason:str)->None`（async，Task 3），仅管理端操作、审计，调用计数不退；本批 Web 无静默“修复”按钮。
- ModelResponseSlot 仅一次性内存传递，不是持久恢复源；validated 结果在领域仓储成功落库前任何丢失均 unknown。产品明确允许“已付费但结果丢失”的保守恢复，不承诺服务商 exactly-once。

## 覆盖表与自审结果

| 设计/验收 | 任务 | 要区分的证据 |
| --- | --- | --- |
| §4 架构/依赖 | 1、4、5、8、10 | 结构自检与具体装配 |
| §5 接口/配置，A1 | 1、2、10 | MockTransport 与真实 probe 分栏 |
| §6 网关/费用，A2–A3 | 3、4、8 | PG 多连接、秘密不泄漏、未知不重试 |
| §7 会话/权限，A4 | 5、6、9 | 当前身份/历史派生来源撤权 |
| §7 多轮提案，A5–A6 | 7、9、12 | 来源、缺项、精确确认、幂等提案 |
| §8 Web，A11 | 9、10、11 | DTO 生成、1440/390px 实际浏览器 |
| §9 研究，A7 | 12、13 | 产品入口真实模型+来源；受控不代替 live |
| 故障恢复，A8 | 3、5、7、8、13 | API/worker 崩溃窗口与独立事务 |
| 质量门，A12 | 每任务及 13 | 同一最终源码版本、失败/跳过单列 |
| §10、A9–A10 | 第二批独立规格/计划 | 本批不宣称共享部署完成 |
| 后续业务 §11 | 第三批逐组计划 | 不由聊天可用推导发信/成交可用 |

自审：已将五个 Review Focus 分别分配到行为测试；确认 schema、idempotency、Run 与配置版本是跨任务固定接口。没有在计划中放真实密钥或公司默认付费限额。文档阶段不执行上述测试，也不把待执行步骤记为通过。

## 执行交接

计划需审阅后进入实现。建议由当前 Agent 顺序执行（Native），原因是模型调用身份、账本、会话和 scheduler 强耦合，保留同一实现上下文更容易控制接口漂移；完成后安排独立整分支审阅。若选择分任务子 Agent，实现与审阅均按任务边界交接，避免同时修改 schemas/tables/bootstrap。

执行者完成本批后应说明：哪些是受控验证、哪些是真实 DeepSeek/搜索验证、哪些仍缺管理员配置。下一批才把这些后台能力交付为公司网址与可靠持续运行服务；不得在本批结束时宣布原始整体目标已经全部完成。
