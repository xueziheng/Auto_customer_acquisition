### Spec Compliance

- ❌ Issues found：身份变化后的迟到 `logout` 仍可清除新身份（I2）；退出失败后的直接重试不能撤销仍有效的服务器会话（I1）；supervisor 将运行故障覆盖成 `requested_stop`（I3）。Task 4 尚不能通过。
- 范围：仅审查 BASE `d0be8f9fdb6ed0199ca5e1a375db3962e19f6f30` → HEAD `0d370b1b8b545ad66207977ed0410d5eec3fcd93` 的 Task 4，包含 brief 已批准的 scheduler/S3/SO_REUSEADDR/test amendments，不是全分支最终评审。
- 文件清单匹配：所列新入口、Web 认证、通知模式、CLI/supervisor、S3/scheduler 窄接线与测试均有对应 diff；`apps/api/runtime_config.py` 是按需项，`apps/web/src/api/api.d.ts` 是仅重新生成项，报告第67行说明生成内容未变化，不据此报漏项。
- ⚠️ 本 diff 不独立证明 Task 1–3 的密码/会话并发、当前员工授权、租户 FK、备份恢复全链；沿用 controller 接受的前置接口。Task 5 的完整角色/停用/重置/恢复浏览器链仍未交付，本次不作为 Task 4 漏项。

### Strengths

- `apps/api/pilot.py:114` 显式注入拒绝模型和真实认证，认证仓储绑定 canonical API engine；`apps/api/pilot.py:90` 显式进入被挂载业务 lifespan，避免静态挂载后跳过 startup/cleanup。未知 API 与缺失资源有实际路由断言，见 `tests/integration/test_pilot_runtime.py:24`。
- `apps/web/src/api/client.ts:163` 清除浏览器员工/租户断言，仅 fixed-dev 分支发送身份头；`apps/web/src/api/client.ts:179` 拒绝旧 generation 响应并仅在401清会话。`apps/web/src/App.vue:171` 附近对通知和业务页应用 generation key，根节点行为由 `apps/web/src/api/authentication.spec.ts:90` 覆盖。
- `apps/scheduler_worker/pilot.py:38` 复用 canonical bootstrap 并明确关闭研究/联系人/Campaign；DNS拒绝步骤返回失败而非认证事实。`apps/scheduler_worker/config.py:295` 保留显式 pilot parser 与原生产 parser 的分离。
- `apps/notification_worker/runtime.py:291` 沿用真实持久站内路由，生产邮件组合分支保留；`tests/integration/test_pilot_notifications.py:30` 验证缺邮件的生产入口拒绝与真实站内记录，health 的 `email=disabled` 有对应断言。
- `scripts/pilot_web_supervisor.py:87` 在启动时检查固定端口/schema，三个真实 ready 后才发布 running；`scripts/pilot_web_supervisor.py:146` 在实际 owned process 停止后才清记录。`tests/integration/test_pilot_runtime.py:130` 包含真实三进程、重复启动拒绝、停止后同端口再启、落后 schema 拒绝。
- `connectors/object_store/config.py:136` 保留 `dev_mode=False` 的显式入口并限制精确 IPv4 loopback endpoint；相关拒绝回归见 `tests/integration/test_pilot_runtime.py:198`。网络限制在三个新进程 main 中安装，没有扩成 OS 沙箱承诺。

### Issues

#### Critical (Must Fix)

- 无。

#### Important (Should Fix)

1. **I1 — 退出失败后，“重试退出”永久缺少 CSRF。** `apps/web/src/api/authentication.ts:70–75` 无论网络失败或503都执行 `clearAuthenticatedIdentity()`，该函数在 `apps/web/src/api/client.ts:90` 同时清除 CSRF；`apps/web/src/App.vue:60–68` 的“重试退出”仍直接调用同一 `exit()`。如果第一次请求未到服务器，浏览器保留有效 cookie，但第二次 POST 没有 CSRF，现有 `apps/api/authentication.py:192–195` 必定以403拒绝。因此“恢复服务后重试退出”的指引不能完成服务器撤销，用户必须碰巧先选恢复会话。应让退出重试在业务仍隐藏的情况下重新取得会话绑定 CSRF，然后撤销，或明确实现等价的可靠撤销恢复流程。补“首次网络失败→服务恢复→直接重试退出→204且服务器会话无效”的测试；当前 `authentication.spec.ts:82` 只验首次报错和本地清空。

2. **I2 — 旧退出请求可以撤销新身份的前端状态。** `apps/web/src/api/authentication.ts:70–74` 与 restore/login 不同，没有 generation 检查，finally 无条件清除身份并广播。`apps/web/src/App.vue:71` 不受 `exiting` 限制：A 的退出还在等待时，若另一请求401或跨标签事件先清身份，登录表单就会挂载；B可以登录成功；随后A的退出收到迟到失败响应，finally会清除B并向其他标签广播。这个序列违反“身份变化隔离旧请求”，现有 `authentication.spec.ts:49` 只覆盖业务请求迟到401，未覆盖认证变更请求。应协调认证变更的进行状态，避免旧退出未收尾就重新登录，并对迟到退出的状态更新/广播做 generation 隔离；同时考虑成功退出响应的 cookie 清除顺序。补 A退出挂起→身份失效→新登录尝试→旧退出完成的定向用例。

3. **I3 — 意外应用退出被记录为正常请求停止，丢失故障原因。** `scripts/pilot_web_supervisor.py:252–259` 检测到任一子进程退出时仅设本进程 `result=2` 后退出循环；finally调用 `close()`，后者在 `scripts/pilot_web_supervisor.py:162–167` 无条件发布 `status="stopped", reason="requested_stop"` 并清空进程记录。`OwnedProcess.stop()` 对已经退出的进程不检查其非零退出码（`infra/controlled/resources.py:99–136`），故一个运行中崩溃的 scheduler 通常会经过此正常清理分支。CLI start 早已返回、supervisor stdout/stderr 为 DEVNULL，操作者最后只看到“请求停止”，无法区分故障与人工停机；scheduler 的停止推进告警要求也失去可靠事实来源。应把结束原因带入清理：仍精确停止所有 owned 进程及存储，但持久状态保留 `failed/operation_failed`（无需保留已死进程为活实例），人工 TERM 才记 `requested_stop`。补 running 后单个 owned 子进程意外退出的测试，断言资源静止且故障诊断保留。

#### Minor (Nice to Have)

- **M1 — 已知 lint 输出不纯净。** `.superpowers/sdd/2026-09-07-web-internal-pilot/task-4-report.md:110` 记录0 errors、87 warnings，归于四个未改页面。本评审未核实其逐条来源，也不要求在 Task 4 扩散清理，但应保留为既有告警/最终验收台账，不能表述为零告警验证。

### Checks / Evidence Boundaries

- 阅读 root 与所有实际涉及目录的就近 AGENTS、完整 brief/report、绑定 design。review package 的完整 diff 仅顺序读取一次，按1–420、421–1000、1001–1580、1581–2150、2151–2580、2581–2836分段，无缺失 hunk；之后只用脚本计算指定锚点行号，没有重新读取 changed source。无 git 查询、无实现修改、无子代理。
- 命名风险检查1：退出失败后的重试是否可由后端接受；只定位并读取现有 `apps/api/authentication.py:130–210`，确认精确来源、开发头拒绝及带 cookie 的不安全方法必须携带 CSRF。
- 命名风险检查2：意外退出是否会被 OwnedProcess 清理自动保留为失败；只定位并读取 `infra/controlled/resources.py:24–140,200–238`，确认出生时间/锚点协议和已经退出时不拒绝非零退出码的行为。未审查或改动此前资源实现的其他问题。
- 命名风险检查3：传入拒绝模型是否仍构造真实模型/邮件，以及被挂载业务的资源生命周期是否独立；只定位并读取 `apps/api/composition/runtime.py:1192–1232` 和 `apps/api/runtime.py:140–208`，确认显式模型避免 OpenAI 构造、无 manual_send 时使用 unavailable gateway，以及 canonical lifespan 的关闭路径。
- 未重跑已报告测试。报告第107–116行的418 Web tests、14 pilot integration tests、27 notification tests、34 canonical runtime tests及各工具通过，是实现者执行记录；本评审核对了 diff 中测试断言与覆盖范围，没有据此声称自己执行过。旧占位测试失败后被替换的范围已在报告第113行披露，不把不同运行计数相加。
- 实际浏览器记录位于报告第124–137行，脚本在仓外；本次未重新读取脚本、截图或重跑浏览器。其登录/恢复/多标签退出证明不覆盖 I1–I3，不能用既有 GREEN 排除这些失败序列。
- 已延期的 Task 2 manager 更强行锁与 Task 3 tar fixture minor 未展开，本 diff 未显示它们的新回归。

### Assessment

**Task quality: Needs fixes**

**计数：Critical 0；Important 3；Minor 1（已知、未改文件 lint 告警）。**

**Reasoning：**正常登录、三进程启动与本机能力关闭的组合清晰且有实跑记录；退出恢复/迟到退出和 supervisor 故障诊断的控制流仍有可直接从代码推出的问题，应定向修复后通过本任务 gate。
