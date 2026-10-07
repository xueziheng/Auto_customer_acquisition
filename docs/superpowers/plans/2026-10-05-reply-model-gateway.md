# 回复模型网关接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 测通“回复识别 → 退订拦截 → 需求建档 → 人工接管”，并在远端 Windows 的 TradeOS 网页中核对真实持久记录。

**Architecture:** 在原 singleton scheduler 中，通过既有 `classifier=` 接口注入绑定 canonical Run 的 QualificationAgent。模型调用走原 `model.generate`；领域服务继续决定抑制、Validated Need、Trade Opportunity 和 Handoff。调用历史跨配置版本约束身份，结果不确定时停止推进。

**Tech Stack:** Python 3.12+、FastAPI、Pydantic v2、SQLAlchemy 2.x、Postgres、现有 DeepSeek Connector；Vue 3、TypeScript、Vite、Ant Design Vue；pytest、Playwright。

**Spec:** [回复模型网关接入设计](../specs/2026-10-05-reply-model-gateway-design.md)。用户已确认接入设计，并于本轮要求继续。

## Global Constraints

- 权威源码为 `/home/joyu/projects/TradeOS`，分支 `codex/wsl2-acceptance-fixes`；计划基线 `b5e4e78cfa04f14ba151e6419a9bc8deb3f218f8`。工作树已有改动，禁止整树覆盖、重置或全量暂存。
- 开始实现前读根 `AGENTS.md`、`HANDBOOK.md`、`GLOSSARY.md` 及各目标目录规则；中文内部文档和提示，英文客户内容。
- 模型永不接触凭证；所有模型和 Gmail 动作走现有 Gateway；不改 `tool_gateway/pipeline.py` 或阶段顺序。
- 专用 capability 为 `reply_qualification`，`turn_id=None`、`sequence=0`；canonical 幂等键为 `reply:{message_id}`。
- 金额保持 Decimal，关键字段保留客户消息原话和 Provenance；模型不输出置信度，不执行业务动作，不扩展字段词表。
- 每项业务查询显式限制 tenant；infra 实现只依赖共享类型，不反向导入 apps 中定义的 Protocol。
- 回复开关默认关闭。开启时 `max_output_tokens` 必须处于 128–8192；全部付费限额、模型、版本、外发许可、凭证引用来自操作者明确配置，不编造生产默认值。
- 同一 Run 的调用历史存在时，员工、用户、模型和配置版本必须一致。unknown、丢失结果、重启或换版均不自动创建第二次付费调用。
- 保留旧 pilot 网络边界和明确的规则模式。装配 Outreach 服务不替代 Campaign 审批，不修改发件身份、不启动新 Campaign、不发送额外客户邮件。
- 当前受控演示继续运行，测试使用独立 owner 和资源；演示数据不复制进日常租户。“我的邮箱”不自动转成业务入站。
- 每次提交前运行 `python3 scripts/check_boundaries.py`，只暂存本任务文件或本任务的精确 diff；不提交运行配置、凭证、原始客户正文或无关已有改动。

## Review Focus

1. engine 持有 Run 行锁时，独立读取连接能否完成校验；任务 1 用真实两连接和超时检测，不用 savepoint 掩盖等待。
2. 外发已发生但业务未落库，随后更换模型/员工/配置版本；任务 2 与 4 证明没有第二次 Provider 调用。
3. profile 已有 Gmail 而回复开关关闭，或只配置了部分端口；任务 3 明确拒绝不完整组合，不静默修改 profile 或启用抓取。
4. 消息带旧邮件引用、主题标签、Unicode 原话或伪造身份指令；任务 4 与 5 验证生产投影、逐字证据和输入身份隔离。
5. 页面接受请求仍在途中或接受后员工撤权；任务 6 验证最终队列、数据库接管人及敏感内容清除，不依靠瞬时文案。

---

### Task 1: 持久业务关联和历史调用读取

**Files:** 修改 `shared/schemas/model_invocation.py`；新增 `apps/scheduler_worker/reply_model_binding.py`（本步仅窄协议）、`infra/db/reply_model_binding.py`、`tests/integration/test_reply_model_binding.py`；扩展 `tests/unit/test_model_invocation.py`。

**Interfaces:**
- Produces: `ReplyRunBindingReader` Protocol，`async resolve(tenant_id: TenantId, message_id: MessageId) -> RunId`、`async validate(tenant_id: TenantId, run_id: RunId) -> MessageId`、`async prior(tenant_id: TenantId, run_id: RunId) -> tuple[tuple[InvocationIdentity, str], ...]`；元组第二项为持久 model 标识。
- Produces: `SqlReplyRunBindingReader(factory: async_sessionmaker[AsyncSession])` 结构化实现上述端口，使用已有共享 ID 和 InvocationIdentity，不新增表或迁移。

- [ ] 写失败测试 `test_resolves_only_canonical_reply_run`、`test_rejects_cross_tenant_or_changed_reply_association`、`test_reads_binding_while_engine_holds_run_lock`。参数覆盖错误 workflow/step/status/key、subject/context 不一致、缺失 inbound/outbound/Attempt/Enrollment、账户与联系人拼接、同消息重复 Run。断言：
  ```python
  assert await reader.resolve(tenant, message_id) == run_id
  assert await reader.validate(tenant, run_id) == message_id
  with pytest.raises(ModelGenerationError):
      await reader.resolve(other_tenant, message_id)
  assert await asyncio.wait_for(reader.validate(tenant, run_id), timeout=2) == message_id
  ```
  第四项在另一真实连接持有该 Run 锁时执行。历史测试种入两个配置版本和另一租户，断言返回本租户所有版本，且没有正文或凭证字段。
- [ ] 运行 `.venv/bin/python -m pytest tests/unit/test_model_invocation.py tests/integration/test_reply_model_binding.py -q`，确认新能力和读取器尚不存在导致失败。
- [ ] 增加 capability；实现三个只读方法。查询 Run、Message、OutreachMessageAttempt、OutreachEnrollment；逐个比对五个 context ID、subject 和 canonical key。拒绝不唯一结果。`prior` 只读本 tenant/Run/capability/sequence=0 的安全元数据，保留全部状态和配置版本；不加独立 Run 行锁。
- [ ] 重跑本步测试，全部通过；Ruff 检查本步 Python 文件、结构自检通过。
- [ ] 仅提交本步变更：`feat: 校验回复模型的持久运行绑定`。

### Task 2: 当前授权、稳定身份和 Gateway 分类器

**Files:** 扩展 `apps/scheduler_worker/reply_model_binding.py`；新增 `tests/unit/test_reply_model_authority.py`、`tests/integration/test_reply_model_gateway.py`。

**Interfaces:**
- Consumes: 任务 1 的 `ReplyRunBindingReader`；既有 `ModelConfigurationService.authorize`、员工公开服务、`require_reply_internal_access`、`ModelGenerationPort.generate`。
- Produces: `ReplyModelAuthority(runs: ReplyRunBindingReader, employees: EmployeeScopeFactory, lookup: Actor, configuration: ModelConfigurationService, *, tenant_id: TenantId, employee_id: EmployeeId, model: str)`，方法 `async identity_for_message(message_id: MessageId, configuration_version: str) -> InvocationIdentity` 与 `async check(identity: InvocationIdentity) -> None`。
- Produces: `BoundReplyClassifier(authority: ReplyModelAuthority, generator: ModelGenerationPort, *, model: str, configuration_version: str, max_output_tokens: int)`，只读 `model: str` 及 `async classify(*, message: dict[str, str]) -> ReplyClassificationResult`，符合既有 ReplyClassifier。

- [ ] 写失败测试 `test_reply_identity_comes_from_current_employee_and_run`、`test_prior_invocation_pins_actor_model_and_version`、`test_revocation_during_generation_discards_result`、`test_unknown_or_lost_result_is_not_resent`。使用 task 1 真实历史和现有 Gateway/usage，不以 fake authority 代替被测授权；Provider spy 位于 Connector 接口边界。核心断言：
  ```python
  assert identity.capability == "reply_qualification"
  assert identity.run_id == run_id and identity.turn_id is None and identity.sequence == 0
  assert provider.calls == 0  # 任一权限、外发、probe、heartbeat 或配额条件不满足
  assert provider.calls == 1  # 已派发后重复、换版或结果丢失，再次尝试仍为一次
  assert persisted_classifications == []  # 调用途中撤权
  ```
  分别覆盖同版本重复、不同版本、员工 user_id 改变、多个冲突历史、模型改变、错 capability/turn/sequence，以及已失败/unknown/succeeded 历史。
- [ ] 运行 `.venv/bin/python -m pytest tests/unit/test_reply_model_authority.py tests/integration/test_reply_model_gateway.py -q`，记录预期失败。
- [ ] 实现当前员工和业务绑定校验；历史任何一项身份/模型不匹配即关闭。授权前后都重读事实，当前资格与模型配置许可取交集。分类器每次创建独立 GatewayJsonModelClient、StructuredReplyModelPort 和 QualificationAgent，显式传配置 token 上限；只把 subject/body 交给模型，不共享可变 identity 或响应槽。
- [ ] 重跑本步测试和既有 `tests/unit/test_model_gateway.py`、`tests/integration/test_model_gateway.py`；核对 ledger/usage 状态、未知用量未补零，无额外 Provider 请求。Ruff 和结构自检通过。
- [ ] 仅提交本步变更：`feat: 通过模型网关识别已授权业务回复`。

### Task 3: standalone 回复接线和配置边界

**Files:** 新增 `apps/scheduler_worker/standalone_reply.py`、`tests/integration/test_standalone_reply_runtime.py`、`tests/unit/test_standalone_capability_binding.py`；修改 `apps/scheduler_worker/standalone.py`、`apps/scheduler_worker/runtime.py`（仅构造器的显式能力校验）、`infra/standalone/settings.py`、`infra/standalone/model-settings.example.json`、`tests/unit/test_standalone_model_settings.py`、`docs/operations/builtin-deepseek-agent.md`。

**Interfaces:**
- Produces: `StandaloneModelSettings.reply_enabled: bool = Field(default=False, strict=True)`。
- Produces: `bind_reply(core: SchedulerCoreServices, outreach: OutreachService, resources: ReplyRuntimeResources, *, tenant_id: TenantId, employee_id: EmployeeId, settings: StandaloneModelSettings, resolver: SecretResolver, fingerprints: HmacFingerprintProvider, owner: str, provider_factory: Callable[[], ModelProvider] | None = None) -> ReplyQualificationComposition`。
- Extends: `create_standalone_factory` 和其内部 `_create_standalone_factory` 增加 keyword-only `reply_inbound: InboundRuntimePorts | None = None`，供受控 Provider 验收；正常入口未传时按明确 profile 组装 GmailOAuthSecretResolver、GmailInboundApiTransport 和已有 InboundRuntimePorts。

- [ ] 写失败测试 `test_reply_switch_requires_explicit_valid_configuration` 与 `test_standalone_composes_reply_under_existing_singleton`。覆盖默认关闭、字符串 bool 拒绝、127/128/8192/8193 token 边界；enabled 缺 Gmail/员工拒绝；disabled 传入端口拒绝；已有 Gmail 且 disabled 延续当前 fail-closed 行为，不删 profile 中的配置以启动。
  ```python
  assert StandaloneModelSettings.model_validate(base_settings).reply_enabled is False
  assert provider.calls == 0 and gmail.calls == []  # 仅构造或未获 singleton
  assert pending_unapproved_campaign.state == "draft"
  assert gmail.sent == []
  ```
  同时覆盖无 Gmail 的现有独立入口、只有回复、回复加现有研究端口三种组合；失败启动和取消均关闭自有资源。
- [ ] 运行 `.venv/bin/python -m pytest tests/unit/test_standalone_model_settings.py tests/integration/test_standalone_reply_runtime.py -q`，确认配置/接线失败。
- [ ] 在 `tests/unit/test_standalone_capability_binding.py` 写 `test_explicit_standalone_research_does_not_hit_legacy_pilot_rejection` 和 `test_legacy_pilot_or_partial_research_still_rejected`。显式 `standalone_research=True` 且原 assistant/research 工厂完整时允许构造且零外部调用；关闭该标志、缺任一工厂、开启 contacts 或 Gmail 端口不完整时仍抛 ValidationError。运行该文件确认前者复现下述既有失败，再仅修正 runtime 构造器对显式研究组合的重复拒绝条件；不改变 singleton、driver、网络边界和其他能力校验。
- [ ] 实现 bind_reply：只借用 resources.sessions、原件和域服务，按研究组合的现有方式创建配置服务、usage、ledger、Gateway，沿用同一 instance_id 和现有 ModelRuntimeLifecycle；经 `CurrentEmployeeReplyFactory(classifier=...)` 返回组合，不另建 engine/循环/heartbeat。
- [ ] 在显式 enabled 且 profile Gmail 完整时，同时装配 CampaignMessaging 的原公开服务、inbound 和 reply；邮箱 alias/route/config_version 沿用已部署绑定，避免重置入站游标。受控 reply_inbound 必须与本 profile tenant/邮箱绑定一致，并满足现有 Gmail 端口协议。保留全部批准、可达性、身份和抑制检查；构造阶段不解析 Gmail 或模型凭证、不联网。
- [ ] 重跑本步与 `tests/integration/test_standalone_model_runtime.py`、`tests/integration/test_builtin_research.py`，包含下述两个既有失败用例。必须证明完整显式组合可以启动、旧 pilot 和不完整组合仍拒绝；不能移除整段 guard 使测试通过。
- [ ] 补操作文档：开启/换员工/换模型/换限额用新配置版本，两进程一致、显式 probe，unknown 不重发；已有手动 standalone 进程必须显式停机，旧 supervisor status 不代表它们。示例无真实密钥和可误用预算。Ruff、结构自检通过后仅提交本步：`feat: 在独立调度入口装配模型回复`。

### Task 4: 真实持久业务链和失败恢复集成

**Files:** 修改 `tests/integration/test_pilot_reply_chain.py`，按需扩展其既有 helper `tests/integration/test_reply_completion.py`；新增 `tests/integration/test_standalone_reply_chain.py`。本步只修本任务实现中的缺陷，不替换业务服务。

**Interfaces:** 消费任务 3 的实际 standalone factory、任务 2 的实际绑定分类器、现有 MIME → inbound → Outbox → Workflow → API 接管链。controlled Provider 仅替换最外侧网络接口；不得注入固定 ReplyClassifier。

- [ ] 将原失败的规格回复正向断言保留到新 standalone 链路；规则模式另留 `test_rule_mode_cannot_extract_specification`，明确断言失败和零 Need。新增完整模型链测试 `test_standalone_reply_reaches_need_and_accepted_handoff`，消息仍为 `We need hinges for cabinet doors. We need 5000 units at USD 2 per unit.`；断言：
  ```python
  assert run.status.value == "completed"
  assert need.product_category.value == "hinges" and str(need.quantity.value) == "5000"
  assert need.quantity.provenance.source_id == inbound_message_id
  assert handoff.customer_verbatim in original_body
  assert accepted_by == current_employee_id
  assert reply_provider_calls == 1
  ```
  分别按原 API/ORM 实际模型读取上述字段，不为断言增加产品 DTO。
- [ ] 新增 `test_model_unsubscribe_persists_scope_once`、`test_invalid_evidence_creates_no_need`、`test_changed_association_or_crash_cannot_reissue_paid_reply`。覆盖联系人/账户退订、中英文、多次扫描；分类/抑制唯一、序列停止、零 Need/Handoff；伪造 quote、旧引用需求、邮件中伪造身份、非法动作/概率输出；取消后重新创建 runtime；额度和未配置拒绝。
- [ ] 运行 `.venv/bin/python -m pytest tests/integration/test_standalone_reply_chain.py tests/integration/test_pilot_reply_chain.py tests/integration/test_reply_completion.py -q --tb=short`，先验证新测试会暴露缺陷，再在对应实现文件最小修复；禁止 xfail、弱化断言、绕过 Gateway 或修改冻结语料。
- [ ] 同命令全部通过后，运行相关 reply qualification、scheduler trigger、suppression、handoff API 回归。证据明确写“真实 Postgres/工作流，受控 Gmail/模型 Provider”；Ruff、结构自检通过。
- [ ] 仅提交本步和必要修复：`test: 验证模型回复到人工接管的持久闭环`。

### Task 5: 同一网关的 160 条真实模型评估

**Files:** 新增 `tests/evals/reply_gateway_evals.py`、`tests/evals/test_reply_gateway_evals.py`、`tests/integration/test_live_reply_acceptance.py`、`docs/acceptance/2026-10-05-reply-model-gateway.md`；复用 `tests/evals/reply_evals_runner.py`，不修改冻结 `replies/**/input.json` 或 `expected.json`。

**Interfaces:**
- Produces: `GatewayReplyEvalClassifier(classifier: ReplyClassifier, messages: Mapping[str, dict[str, str]])`，实现原 ReplyClassifier；`messages` 为语料 ID → 已通过原生产内容读取/护栏的独立 canonical 入站消息（含真实 message_id），不接受 expected 标签；`classify(*, message: dict[str, str]) -> ReplyClassificationResult` 查映射后调用任务 2 分类器。
- Consumes: `run_reply_evals(classifier: ReplyClassifier) -> ReplyEvalReport`；真实验收 pytest fixture 从显式 `TRADEOS_REPLY_MODEL_SETTINGS_PATH` 读取 settings，通过可信 resolver 在进程内取模型凭证。
- Live entry: `tests/integration/test_live_reply_acceptance.py::test_live_reply_corpus_and_business_chain`。默认未设置 `TRADEOS_REPLY_LIVE=1` 时明确 skip；显式 live 但缺路径/配置时 fail，不降级成 fake Provider。

- [ ] 写适配器失败测试 `test_eval_projection_does_not_leak_expected_labels`：传给模型的 subject 固定 `(current reply)`，正文经过原生产 MIME 投影/输入护栏，原 JSON 文件 hash 不变；ID 缺失或映射冲突拒绝；模型 payload 仍只有 subject/body。拒绝将 expected 或分类目录名发给模型。
- [ ] 运行 `.venv/bin/python -m pytest tests/evals/test_reply_gateway_evals.py -q`，确认失败后实现适配器。live fixture 建独立 owned 测试环境，用实际领域服务和入站事件建立 160 个独立 canonical Run，不借用正式客户身份；每个分类器调用读取其真实 Run，模型调用走同一 Gateway，Gmail 保持受控。
- [ ] 加 live 前置检查：有效配置、在职员工、两进程同版本心跳、显式 probe、外发许可和限额；在现有调用窗口/并发限制内顺序执行。预估 probe + 160 样本 + 业务正向/退订验收所需调用数，预算不足列出未运行项，不修改限额、反复 probe 或绕开额度。
- [ ] 受控适配器测试通过后，只有得到操作者配置路径、可用 resolver 和明确限额才执行 live 命令；报告实际模型和配置版本、projection、160 分母、逐条错误/未识别、动作、范围、提取及实际 ledger 用量。没有真实结果时保留“未运行”，不能填通过。
- [ ] 按 spec 的关键安全门槛裁定：退订/投诉漏拦、自动回复误停、错误范围抑制、无证据建档任一出现，阻止完成；其他语义偏差逐条列为未完成项。修 prompt 后原样重跑语料，新增变体只增不改；复跑仍受操作者限额约束。
- [ ] 保存脱敏验收报告；Ruff、结构自检和原 eval runner/integrity 回归通过后仅提交本步。真实评估未完成则不得把此任务勾为完成或提交“验收通过”结论。

### Task 6: 远端网页最终状态及启用验收

**Files:** 扩展 `tests/e2e/test_web_core_controlled.py`；新增 `tests/e2e/test_standalone_reply_browser.py`；补 `docs/acceptance/2026-10-05-reply-model-gateway.md`、`docs/operations/builtin-deepseek-agent.md`。

**Interfaces:** 使用任务 3 的同一 API/scheduler 组合和任务 4 的持久证据；真人通过既有 `/crm/handoffs/{id}/accept` 接口接管，不新增产品入口。实时模型证据引用任务 5，受控 browser 结果独立标注。

- [ ] 编写 `test_standalone_reply_browser_accepts_and_clears_revoked_content`，在 1440 和 390 宽度分别核对同一 Need/Opportunity/Handoff 链；预先保存准确 ID，记录 pageerrors。断言：
  ```python
  assert accept_response.status == 204
  await expect(final_status).to_have_text("已接受接管；已按后端等待顺序刷新队列。")
  assert queue_json == [] and accepted_by == employee_id
  assert pageerrors == []
  ```
  `final_status` 为 `get_by_role("status").filter(has_text="已接受接管")`；等待最终响应后查询 DB。撤权后用既有认证失效流程刷新页面，断言客户原话与字段从 DOM 清除。
- [ ] 运行 `.venv/bin/python -m pytest tests/e2e/test_standalone_reply_browser.py -q --tb=short`，发现缺陷先保留证据，再修复任务内的接线或断言；不强点禁用按钮或绕过登录/审批。
- [ ] 重跑相关 E2E、Ruff、`python3 scripts/check_boundaries.py` 和 scoped diff 检查。汇总不同层级通过/失败/未运行，不累加重叠测试数；当前保留的演示尚未执行最终 cleanup，报告必须说明。
- [ ] 只有受控和真实模型验收达标且部署配置齐备，才按现有运维流程停旧日常应用、启动同配置版本的 standalone 两进程及原通知进程，验证只有一个持锁 scheduler；不停止现有演示。切换失败沿原入口恢复，未知调用不可重试。
- [ ] 在远端 Windows 的交互式 Edge 打开这次验收对应的网页，明确区分 owned 验收记录和日常真实记录。仅查询真实 Gmail 新入站；没有合法关联的实际业务回复时不能伪造日常 Need，部署的真实邮件业务效果应标为待真实入站验证。
- [ ] 仅提交本步：`test: 验证回复接管网页和部署边界`。完成整分支独立审查后，按实际证据列出剩余缺陷；未通过的 live 或 UI 项目不勾完成。

## 追加基线证据：standalone 组合故障

在计划提交 `dbf3bdb` 后、尚未修改实现代码时，远端执行：

```sh
.venv/bin/python -m pytest tests/unit/test_standalone_model_settings.py tests/integration/test_builtin_research.py -q --tb=short
```

结果为 **6 passed、2 failed，156.98 秒**；安全摘要原日志 `/tmp/tradeos-standalone-baseline-20261005.log`。两个参数化研究测试 `[False]`、`[True]` 均在 `SchedulerRuntimeFactory.__init__` 抛出固定错误 `本机 scheduler 外部能力必须未配置`，未到达研究模型执行。该结果使用受控 Provider，不包含真实付费调用。

静态根因：构造器先验证显式 `standalone_research` 的 assistant/research 工厂，随后 pilot guard 又无条件拒绝 `bootstrap.research_enabled=True`。任务 3 新增了这一基线修复和反例测试；修改对象仍是装配校验，不是 Gateway 核心管线。工作树的 runtime.py 本来就有未提交改动，实施必须保留这些内容，仅提交本任务差异。

## 自查与执行交接

设计第 4 节由任务 1–2 落实，第 5/7 节由任务 3 落实，第 6 节由任务 4 验证，第 8 节由任务 4–6 验证；第 9 节范围限制贯穿全程。Review Focus 五项分别有对应测试。接口只使用现有公共类型，没有 infra→apps 反向导入，也不要求更改 Gateway 核心或数据库迁移。

建议 Native：由当前会话顺序实现，末尾独立审查。六步共享身份、生命周期和验收数据契约，连续实现便于保持一致；分步重新派发会增加上下文和审查成本。待操作者审阅本计划并选择执行方式后进入实现；现有接入设计不再重复审批。
