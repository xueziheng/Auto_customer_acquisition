### Task 4: 真实登录Web与完整内测运行入口

**Files:**
- Create: `apps/api/pilot.py`, `apps/scheduler_worker/pilot.py`, `apps/notification_worker/pilot.py`
- Modify: `apps/notification_worker/runtime.py`, `apps/notification_worker/health.py`, `apps/notification_worker/AGENTS.md`
- Modify: `apps/scheduler_worker/config.py`, `apps/scheduler_worker/runtime.py`（显式pilot配置接线；保留默认生产必填检查）
- Modify: `connectors/object_store/config.py`, `connectors/object_store/AGENTS.md`（显式loopback pilot解析，dev_mode保持False，默认解析不变）
- Create: `docs/adr/0068-local-in-app-notifications.md`, `tests/integration/test_pilot_notifications.py`
- Modify: `scripts/run_web_pilot.py`, `apps/api/runtime_config.py`（仅明确pilot配置接线需要时）
- Create: `scripts/pilot_web_supervisor.py`（三应用生命周期与健康检查；CLI只解析/派发）
- Create: `apps/web/src/api/authentication.ts`, `apps/web/src/components/LoginPanel.vue`
- Modify: `apps/web/src/api/client.ts`, `apps/web/src/App.vue`
- Modify: `apps/web/tests/api-client-identity.test.ts`（真实模式去掉旧身份头断言，保留dev断言）
- Modify: `apps/web/tests/catalog-product-proposal.test.ts`（mock请求时捕获身份快照，保留authenticated迟到响应隔离）
- Modify: `infra/pilot/resources.py`（仅稳定端口SO_REUSEADDR及实际快速重启验证）
- Modify: `tests/unit/test_pilot_profile.py`（旧start占位断言换为实际supervisor失败不得假成功）
- Create: `apps/web/src/api/authentication.spec.ts`, `apps/web/src/components/LoginPanel.spec.ts`, `tests/integration/test_pilot_runtime.py`
- Modify: `apps/web/src/api/api.d.ts`（仅重新生成）

**Interfaces:**
- ConsumesTask2明确认证参数/DTO/headers，Task3 PilotConfig与owned资源接口；以各任务报告精确签名接线。
- Produces同源生产构建Web与`/api`业务API；统一CLI能启动/停止完整API/scheduler/notification，账号CLI只接受本profile，真实模式没有开发身份选择器。
- 原有API客户端保留isolated dev模式，新增cookie请求credentials same-origin与CSRF内存读取，登录后identity只用于状态隔离不作后端授权断言。

- [ ] **Step 1: 写失败测试。** 初始无会话仅登录面板、会话恢复后才挂业务路由、401撤销与旧请求隔离、403正常展示、退出同步、不保存secret、raw upload同样CSRF。runtime测试核对唯一canonical工厂、非dev、拒绝外部model/邮箱/搜索、同源资源路径/未知API404、worker单实例及schema落后拒绝。
```typescript
await restoreSession()
expect(currentIdentity()).toBeNull()
await login(username, password)
expect(currentIdentity()?.mode).toBe('authenticated')
await logout()
expect(currentIdentity()).toBeNull()
```
- [ ] **Step 2: 运行RED。** 按Task2实际导出的命名实现测试；不手写重复API响应类型。
- [ ] **Step 3: 实现运行组合与Web。** API同一进程挂静态SPA，`/api`挂原业务app并正确运行lifespan；API/worker不互相import。各入口消费profile显式政策与独立技术配置，认证API最终dev_mode=False。没有真实外部客户端，也没有ControlledModelClient伪成功；拒绝适配器与网络限制同时生效，公开能力矩阵准确标未配置。worker保持原canonical消费与关闭。通知新增显式`NotificationRuntimeMode.LOCAL_IN_APP`，仅pilot入口使用；默认PRODUCTION无邮件仍拒绝，旧CONTROLLED_IN_APP保留。复用同一站内持久投递策略，健康披露email disabled，完成仅代表站内；不创建邮件client、不把fake adapter称作真实投递。以ADR0068记录并更新就近AGENTS，新增该mode回归。CLI start检查build/schema并更新当前端口/安全status，stop保留数据，账号CLIgetpass接profile。Web中文登录、加载/失败/退出状态，应用根监听所有identity generation，卸载通知、隔离迟到结果；多标签只广播注销事件。生产构建不能开启受控选择器。
- [ ] **Step 4: 运行GREEN。** Web测试/typecheck/lint/build、受影响API/runtime/client测试、结构自检。实际三进程启动并验证健康和安全能力；不要接外网或实际用户密码。
- [ ] **Step 5: 自审并提交。** 报告运行命令与后续Task5合成fixture的窄接口。


## Controller runtime handoff

Existing API runtime composition is apps/api/composition/runtime.py, not runtime_composition.py. It creates OpenAIJsonModelClient when model_client=None: pilot must inject rejecting StructuredJsonModelClient, no synthetic response. No manual_send/gmail/inbound/research ports means disabled capabilities. Scheduler CanonicalSchedulerBootstrap supports research_enabled=False, contacts_enabled=False, campaign_enabled=False; use explicit operator policies. Default scheduler resolver would perform DNS: inject rejecting AsyncTxtResolver and keep loopback network boundary, never return synthetic valid DNS.
Original notification default PRODUCTION requires email; LOCAL_IN_APP must be an explicit new mode with ADR0068 and local AGENTS updated, keeping both existing modes unchanged. Its delivery is real persistent in_app, not fake success, health declares email disabled.
API static mount must explicitly enter business app lifespan (mounted FastAPI lifespan is not automatically startup/shutdown). Test schema rejection and symmetric cleanup. Stable API port is set at profile init; storage ports may change and are refreshed. A same-origin build uses /api relative base, never embeds secrets or employee headers.
Task2 accepted cookie/header/DTO names were tradeos_session_<origin port>, X-TradeOS-Request:1, X-CSRF-Token and SessionResponse; consume final Task2 report/schema for exact committed contract, not provisional notes. Raw uploads use actual Blob MIME; auth guard preserves route MIME/bodyless protocols while enforcing Origin/custom header/CSRF.
Existing .venv Python3.12 and Node24 path available; no global environment or HOME changes. Production Web build and all three process bootstraps need actual owned integration before handoff.

Named cross-tab identity risk: a successful login rotates/revokes the prior cookie session shared with other tabs. Ensure those tabs also clear old identity/data rather than continuing to render employee A while cookie now authenticates B. Reuse the allowed logout/invalidation event (no secret or identity payload) for revoked prior-session tabs when login rotates; explicit logout still broadcasts. Add a focused browser-unit behavior test and carry scenario to Task5. No role/tenant assertion from a broadcast.

## Committed Task3 exact handoff (source 4bc696a; pending independent review)


`--profile PATH` 是持久目录，私有配置固定 `PATH/config.json`，不是第二种环境文件格式。

```python
PilotConfig.create(path: Path, policy_file: Path) -> PilotConfig  # 仅新目录/配置，不创建 Docker
PilotConfig.read(path: Path) -> PilotConfig                    # 参数是 config.json 文件
config.write(path: Path) -> None                              # 调用者持 profile 锁
config.database_url: SecretStr                                # 按当前 database_port 推导
config.resolve(reference: str) -> str                         # SecretResolver 结构接口
config.runtime_environment() -> dict[str, str]
```

配置字段：`version=1`、`restore_state`（none/pending/failed/complete）、`owner`、`tenant_id`、`api_port`、`scheduler_port`、`notification_port`、`database_port`、`object_port`、`bucket`、`secrets`、`policy`、`storage`。`web_port` 属性等于 `api_port`，同源单一稳定入口。初建 DB/object 端口是 0，存储启动后根据实际 Docker loopback 绑定原子重写；Task 4 必须使用启动后的 `profile.config` 或重新 read。

`storage` 键严格为 database/objects，每项 `StorageIdentity` 含 `image_id`、`volume_name`、`volume_created_at`、`container_id`。Docker 具名卷没有独立的不可变 ID；这里核对完整名称、CreatedAt、owner/role 标签、精确容器 ID、镜像 ID、唯一挂载及所有附着该卷的容器集合。未知/旧 ID、错卷、额外容器挂载均拒绝。

政策 JSON 是完整的：

```text
{
  "handoff_policy": {
    "sla_seconds": <显式正整数>, "backlog_threshold": <显式正整数>,
    "t1_seconds": <显式正整数>, "t2_seconds": <显式正整数>
  },
  "scoring_policy": {
    "version": <显式非空版本>, "currency": <三字母币种>,
    "value_band_boundaries": [<Decimal 字符串>, ...],
    "bucket_map": {"1": <high/mid/low>, ..., "7": <high/mid/low>}
  }
}
```

无业务默认；金额走 WireDecimal/Money，语义校验复用 `domains.opportunities.models.HandoffPolicy` 与 `domains.opportunities.scoring.ScoringPolicy`。不允许环境变量或 Provider 配置混入政策文件。真实操作者没有提供政策，本轮没有创建真实 profile。

`runtime_environment` 给出 DATABASE_URL、tenant、显式政策、对象存储当前 endpoint/bucket/ref、技术 lease/batch/poll/retry、固定端口、fingerprint/unsubscribe 引用、`TRADEOS_DEV_MODE=false`、`TRADEOS_NOTIFICATION_DELIVERY_MODE=LOCAL_IN_APP`、dotenv/metadata 禁用值。**它不含真实 Provider 引用或任何假的 Gmail/model/search 凭证，也不复制父环境。** Task 4 需按 LOCAL_IN_APP 契约解析通知模式、装配拒绝型 Provider 并安装 loopback 网络限制；不能把 canonical dev runtime 强行解释为已支持本模式。技术密钥通过 config.resolve 交给可信进程，不要求把 secrets dict 枚举到环境。

```python
profile = PilotProfile(path: Path)              # 固定 unix:///var/run/docker.sock
profile.reload() -> PilotConfig
profile.provision_storage(image_ids: Mapping[str, str] | None = None) -> None
profile.start_storage() -> None                # 仅存储，绝不自动迁移/装配应用
profile.migrate() -> None                      # 显式启动存储 + OwnedProcess alembic upgrade head
profile.check_schema() -> None                 # 只读、合法单 head，不自动升级
profile.stop() -> None                         # 精确 supervisor TERM/等待，然后停存储
profile.status() -> dict[str, object]           # storage/applications/web_url，无凭证
profile.client.close()                         # 释放客户端，不影响容器/卷
```

所有上述变更入口使用 `exclusive_profile_lock(path)` 操作级非阻塞锁，忙时固定 `profile_busy`。锁不会跨整个应用寿命持有，也不会被删除。Task 4 在已经持锁时使用：

```python
profile.provision_storage_locked(image_ids=None)
profile.start_storage_locked()                 # restoring=True 仅恢复内部允许
profile.check_schema_locked()
profile.stop_storage_locked()
profile.save()
profile.verify_all()
profile.require_stopped()
profile.require_no_processes()
```

这些方法是同步接口；内部 schema/revoke 用 `asyncio.run`，不要直接从已运行事件循环调用。`migrate` 一次最多等待 120 秒，只迁移，成功后保持存储运行；CLI init 之后显式 stop，交付停止状态。

启动协议建议顺序（Task 4 实现）：持锁 → reload → require_no_processes → `reserve_port(config.api_port)`（socket 监听 127.0.0.1，供 API 继承 fd）及两 worker 端口预检 → start_storage_locked → check_schema_locked → 用更新 config 启动三个 `OwnedProcess` → 发布 supervisor/子进程实际状态 → 释放操作锁。API 端口已占用时固定失败，不能重新分配。初次创建不做默认账户；可信账号入口重新读取同一 profile 后使用该 URL/tenant，等待真实操作者 getpass。

进程协议：

```python
ProcessIdentity.current() -> ProcessIdentity     # pid + born
ProcessIdentity.live() -> bool                  # 同 boot PID 出生时间不符拒绝；前次 boot 身份视为已终止
profile.publish_processes_locked(
    supervisor=ProcessIdentity.current() | None,
    processes=Sequence[OwnedProcess],
    status="stopped" | "starting" | "running" | "failed",
    reason="requested_stop" | "storage_ready" | "applications_ready" | "operation_failed" | "restore_failed",
)
profile.runtime_state() -> RuntimeState
profile.request_application_stop(timeout: int = 40) -> None
```

只接受当前进程作为被发布 supervisor；子进程从 `OwnedProcess.public()` 记录真实 pid/born/anchor/children，名称限 api/scheduler/notification/migration 且不能重复。`runtime.json` 同样 0600/当前 owner。记录的所有进程已死时 status 不再回报旧 running；部分仍活而部分已死时回报 failed。Task 4 的 readiness 仍需真实三个进程健康检查，状态文件不是 HTTP 健康替代物。

停止 CLI 在锁内核验存储和 supervisor pid/born，发送 TERM 后释放锁并有界等待其退出，再重新加锁检查所有记录的 app/anchor/children 已停止，最后停两个容器。**Task 4 supervisor 必须捕获 TERM，先依次调用实际 `OwnedProcess.stop()` 完成出生时间/锚点协议；在同一操作锁中发布 `supervisor=None, processes=(), status=stopped`，然后 stop_storage_locked。** 不得先清记录再停止进程；不得让 supervisor 等待这个 CLI 持有寿命锁。找不到活 supervisor 但仍有活子进程时失败关闭，不尝试接管或广域 kill。


CLI entry seam is scripts/run_web_pilot.py:start_profile(Path); account subcommand binding remains Task4. Read final review amendments before implementation.

Task3 I1 fix c669eec leaves callable interfaces unchanged. Restore cleanup handles KeyboardInterrupt and persistent diagnostic write failure independently; CLI fixed pilot_interrupted/restore_interrupted. Do not weaken this finally cleanup when wiring start/accounts. Original minor tar-fixture weakness remains deferred in ledger for final review.

Task3 final gate: I1 scoped review Approved at c669eec, all blocking findings addressed. Current HEAD d0be8f9 is only plan supervisor-file amendment after that source fix. Lifecycle/CLI interfaces above are accepted.

Controller approved seam expansion: notification health must identify LOCAL_IN_APP email disabled; scheduler config/runtime may have explicit pilot typed parser/factory seam for absent Gmail/DKIM. Never weaken default production/controlled requirements or invent references/selectors. Keep canonical factory/definitions/singleton; attempted unavailable DNS/send fails before adapter/credential use. A narrow rejecting StepHandler injection for pilot is acceptable if needed; report before changing workflow/domain files. Cover old missing-config rejection and new unavailable-provider behavior.

Approved S3 seam: from_pilot_environ returns dev_mode=False; only canonical http://127.0.0.1:<port>, reject localhost/::1/credentials/path/query/fragment/devtrue or arbitrary other endpoint. Reuse field validation; preserve default from_environ behavior. config.py and AGENTS.md are in amended file map; add brief documented explicit local exception and focused parser regression.

Approved rapid restart correction: only reserve_port SO_REUSEADDR in infra/pilot/resources.py, no SO_REUSEPORT, retain exact loopback/live-listener refusal. Cover actual same-port rapid stop/start and occupied port. Existing api-client-identity.test.ts authenticated-header assertions are obsolete and must change to session/no identity headers, retain all DEV-specific headers and isolated rendering assertions.

Fix1 I2 controller decision: serialize same-origin login/logout using navigator.locks plus local mutation busy state and generation fencing. Missing Web Locks fails safely; no weaker fallback or claim of all-browser support. Do not recursively acquire same lock in logout retry session-CSRF helper. Official technical source https://www.w3.org/TR/web-locks/. Design amended; include exact tracked design change in fix commit and carry Chromium compatibility limitation.
