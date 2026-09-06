### Spec Compliance

- ❌ Issues found：Task 9 的主要状态区分、原命令恢复与安全投影已实现，但入站跨通道旧响应仍能覆盖新绑定／版本；寻源首次明确校验拒绝被当成未知提交永久冻结。具体见 I1、I2。
- ✅ 需求指定四个页面均有对应修改：`apps/web/src/views/runs/RunCenter.vue:97`、`apps/web/src/views/campaigns/CampaignCenter.vue:152`、`apps/web/src/views/settings/SettingsCenter.vue:535`、`apps/web/src/views/sourcing/SourcingRecoveryForm.vue:48`；新增 `apps/web/tests/web-core-state-recovery.test.ts:1`。ADR 与生成 DTO 对应新增 `recovery_action`，未引入通用账本或发送重试接口：`docs/adr/0064-sourcing-reconciliation-recovery-action.md:9`、`domains/sourcing/schemas.py:1151`。
- ⚠️ Cannot verify from diff：未验证 Task 12 全仓 A1–A10、其他任务未修改页面的完整预算／queued／stale 行为、实际多人登录、真实发送／Provider／认证。报告的研究与 Catalog 两文件 45 项属于补充既有行为证据，非本 diff 新实现。
- ⚠️ Cannot verify from diff：Settings 服务内部候选 commit 后、Run start 前的进程中断恢复未在本批真实注入。实施报告明确区分服务器已返回 202 后浏览器丢响应与服务内部中断；本审查不将前者升级为完整端到端重启证明。

### Strengths

- Settings 对 Playbook 与版本历史的并行读取逐响应检查保护性拒绝，不等待另一悬挂请求才使共享 gate 失效；未知请求保留原 body/key：`apps/web/src/views/settings/SettingsCenter.vue:603`、`:668`。对应并行 403 测试位于 `apps/web/tests/web-core-state-recovery.test.ts:105`。
- Sourcing 恢复动作由后端计算，核 tenant、execution、quota、actor 与原事件指纹；已交付事件不等同业务完成：`workflows/sourcing_case/application.py:801`。真实 PG 作用组增加 canonical 保存后 ack 失败、ack 后 delivery 失败及同命令恢复断言：`tests/integration/test_sourcing_plan_confirmation.py:504`、`:564`。
- Campaign 仍调用原 pause/activate；未知暂停后，只有精确 Campaign 达到目标状态才解除未知锁，并继续显示已有额度：`apps/web/src/views/campaigns/CampaignCenter.vue:165`、`:268`；对应测试 `apps/web/tests/web-core-state-recovery.test.ts:97`。
- 实施报告最终冻结节明确九文件 150 项在最后仅类型标注修正之前；最终类型版本只再运行两文件 64 项，没有累加或冒充最终九文件重跑。浏览器真实 owned API 与受控响应的界限也明确记录于该报告“实际浏览器 QA”节。

### Issues

#### Critical (Must Fix)

- 无。

#### Important (Should Fix)

- **I1 — 入站 retry 与绑定／读取没有共享对象及版本失效边界。** `apps/web/src/views/SendingIdentityCenter.vue:151` 的 retry 使用独立 `inbound-retry` channel，并在 `:159` 只验证页面 op 后直接覆盖 `binding`；绑定写入在 `:115`，状态读取在 `:63`，同样直接赋值。触发顺序：A 绑定 v7 的 retry POST 已在途 → 用户仍可通过身份卡绑定 B（绑定按钮仅受 `busy` 控制；retry 只设置 `retryBusy`）→ B 的绑定响应先写入 → A 的旧 retry 响应最后抵达，将页面倒退为 A。另一条路径是顶部身份刷新完成后启动旧 `/status` 读取，与 retry 成功交错，旧 GET 又覆盖新 version 并清 `retryUnknown`。共享页面 gate 的身份 generation 没变，三个独立 channel 的 op 仍有效。这违反“旧版本请求／响应不得干扰当前状态”，会展示错误 canonical 绑定并重新开放基于旧状态的操作；后端 expected_version 仍能拒绝实际陈旧请求，因此不认定为后端 CAS 绕过。应在绑定变更、retry 与 status refresh 间建立绑定／版本 epoch，或串行化相关操作并使既有请求失效；success/error/finally 均只作用于原对象 generation。补 deferred 测试：B 新响应先到、A retry 后到；以及旧 status GET 后到。断言新绑定、version、unknown 与 busy 均不被旧响应改写。

- **I2 — 寻源首次明确校验拒绝后，输入及命令仍永久冻结。** `apps/web/src/views/sourcing/SourcingCaseDetail.vue:483` 在首次 POST 前保存 `recoveryAttempt`，`:502`–`:504` 对任何非 200 只设错误；只有保护性拒绝经 reset 或 200 才清原尝试。`apps/web/src/views/sourcing/SourcingRecoveryForm.vue:48`–`:57` 同时冻结本地 attempt，后续提交总是重放同一命令，输入／选择保持 disabled。首次请求若收到明确的 FastAPI/Pydantic 422，例如不合法的 Artifact 标识／请求字段，可以确定该次命令未进入业务处理，用户却无法纠正输入；“核对原请求并恢复”只会重交同一非法 body。该变更把确定失败混成未知状态。应像 Settings 的首次严格校验拒绝分支一样，仅对能够证明尚未提交的首次校验拒绝清除父子 pending attempt 并允许修正；此前已有未知结果时，后续拒绝仍不能解除原命令冻结。补首次 422 → 修改错误字段 → 新命令成功，以及先 503 后 422 → 原命令继续冻结的定向测试。

#### Minor (Nice to Have)

- **M1 — 测试名称超出实际断言。** `apps/web/tests/web-core-state-recovery.test.ts:40` 的“Settings 读取失败不渲染未配置，拒绝后旧研究响应不能复活”仅返回 503 并断言失败文案，没有 deferred 旧研究成功响应或保护性拒绝。现有身份变更测试覆盖另一失效路径，不能替该标题提供证据。名称应收窄；若保留后半句，应增加同身份保护性拒绝之后迟到研究响应的精确断言。

### Checks and review boundaries

- 只读审查 BASE `91c77754e295f5eb9754bd87e4e4f920985ae2fe` 到 HEAD `ffdbb0ec980531ee2745608952e06a519b67cb5d`，依 task-reviewer-prompt 执行。完整 diff 按 1–490、491–1040、1041–1660、1661–2250、2251–2700、2701–3054 顺序读取一次；首块输出仅重复文档段被截断，正式 brief/spec/report 已另行完整读取。没有再次生成 git diff。
- 截断函数补读：SourcingCaseDetail 缺失的类型／状态定义及 `canReconcile` 周围上下文（18–43、74–105），SendingIdentityCenter.confirmCommand 截断的绑定成功处理（110–123）。没有单独重读完整实现文件。
- 唯一具名域外风险核对：“恢复投影 early continue 是否保留原域 true boolean，从而在 quota／execution 不匹配时错误开放首次核对”。仅查 `domains/sourcing/service_impl.py:2021` 的 get_uncertain_search_execution、`:2438` 的列表及其直接投影 helper `:997`；确认 `:1012` 默认 false，early continue 不开放动作，故不报缺陷。未扩查未修改 engine、quota、router 或其他任务；原 POST 全部并发最终重验／锁契约交 controller／Task 12，不能由本 diff 独立证明。
- 未重跑任何既有作用组，也未写源码、索引、HEAD 或额外测试文件。I1/I2 为 diff 可确定的控制流缺陷，最小验证建议随 findings 给出。未读取 .env、既有 DSN、config 或密钥。唯一写入为本报告；首次通过 shell 写报告因 stdin 编码错误 exit 1，未生成文件，随后使用 apply_patch 成功写入。
- 最终成功作用组报告未留未解决测试 warning；早期 fixture／路径／VueRouter／lint 失败与修复分开记录。Git AppleDouble stderr 只记录计数且未修共享 git，按本任务约束不提升为源码 finding。浏览器 console error 只保留总数 8，缺逐条 URL／文本，不能独立确认全部属于预期资源错误；报告已明确该证据缺口。Task 13 须持久化临时浏览器证据，Task 12 负责全仓统一门禁。

### Assessment

**Task quality:** Needs fixes

**Reasoning:** 原命令／canonical 恢复设计和失败状态呈现大体正确，证据没有把受控响应或历史数字冒充更强证明；入站对象／版本竞争与寻源确定失败无法修正这两处行为缺陷应在本 task gate 修复后再验收。


## Fix round 1 限定复审

### Spec Compliance

- ✅ Spec compliant（本轮 I1/I2 修复范围）。FIX_BASE `ffdbb0ec980531ee2745608952e06a519b67cb5d` → HEAD `27098ce3ed637fd85df722b1a9e997fe972ef4a9`。I1、I2 均 ADDRESSED；没有发现 fixdiff 新引入的 Critical／Important 问题。
- **原 I2 事实前提更正：** 初审把 Sourcing 原 HTTP 请求校验错误直接称为 FastAPI 422，这个前提不成立。原全局中间件把 RequestValidationError、运行时 PydanticError 与业务 ValidationError 统一返回 400，不能把该 400 当作未提交证明。Fix1 按 controller 已授权的 spec／ADR 裁定，只为 reconcile 增加显式安全 422。原报告历史 finding 保留供审计，本节更正其错误前提，最终结论以此节为准。

### Findings disposition

- **I1 — ADDRESSED。** `apps/web/src/views/SendingIdentityCenter.vue:62`、`:107`、`:156` 将 status read、binding write、retry write 归入同一个 `inbound` channel。绑定新意图会立即替换旧 retry／read 的 controller；写入在途时 `loadBinding` 的 guard 不允许读请求抢占。bind 开始同时清旧 retry busy、保留 unknown，当前 bind 成功才清 unknown；所有响应及 finally 仍先判断原 op 是否有效。`:163` 的 409 分支先设 unknown，再启动 canonical read，旧 version 不会在读取期间重新可操作。新增 `apps/web/tests/web-core-state-recovery.test.ts:110`、`:130` 的 deferred 测试覆盖旧 A retry 200／503、旧 status 在 B 的 busy／unknown 后到；`:170` 覆盖 409 读取未完成时禁用、随后使用新 version。符合原 finding 要求，未把浏览器中止等待解释为服务端取消。

- **I2 — ADDRESSED。** `apps/api/validation_route.py:14`–`:34` 机械提取原 Settings route 处理：只有 responses 显式以 ApiErrorResponse 声明 422 的路由才启用包装，并且只捕获 RequestValidationError。`apps/api/routers/settings.py:64` 保留原 `_SettingsRoute` alias 与行为；`apps/api/routers/sourcing.py:447` 只给 reconcile 显式声明此 422，原业务／运行时 PydanticError 没有被此捕获器改写。`apps/web/src/views/sourcing/SourcingCaseDetail.vue:503` 仅在无既有 attempt、无 canonical、且响应为 422/http_error 时清 pending；已有子组件 watcher 接续清本地 attempt，输入可改而不是强行重放。503 后 422、业务 400、canonical 恢复 422 仍冻结。`tests/unit/test_sourcing_router.py:564` 的真实 ASGI 定向测试明确断言坏字段 422/application 调用 0，以及合法请求进入 application 后业务／运行时错误仍 400；前端 `apps/web/tests/web-core-state-recovery.test.ts:149`、`:162` 验证修正后的新命令及此前未知／canonical 不解冻。生成 `apps/web/src/api/api.d.ts:14985` 新增的 422 响应引用 ApiErrorResponse，与服务契约对应。

### New issues

- Critical：无。
- Important：无。
- 原 Minor M1 按 controller 明确留 Task 12，本轮未重新检查或要求扩修。

### Checks and evidence boundaries

- Fix diff 674 行按 1–370、371–674 顺序完整读取一次；只读 Fix1 报告新增节、spec／review-context 裁定与 diff 中 ADR 增补。没有重读原完整源码、没有重新生成 diff、没有重复运行既有测试。
- 唯一具名域外定向核对：共享 channel 是否只是调用 abort，还是也能拒绝忽略 AbortSignal 的旧响应。读取 `apps/web/src/views/costing-quotes/quote-request-scope.ts:34` 的 begin／valid 实现，确认 `requests.get(channel) === controller` 与全局 generation／身份一起参与 valid；因此 I1 即使旧 fetch 实际完成，也不能回填或清当前 busy。未扩查其他任务或未修改业务层。
- 接受报告中最终六文件 88 项与有限 API 10 项分别通过的证据范围；不与原 150／64 或 Fix1 较早 87 项累加。报告明确 API 10 项之后仅 Web 409 锁变化、最终 88 项包含该锁；结构自检在该 Web 单行锁之前，依赖与后端未再改。controller 已独立读过相关命令输出，本 reviewer 未为重复确认重新跑测试。
- ⚠️ Cannot verify：Fix1 没有新增浏览器、owned stack、真实 PG 全链或全仓测试证据；本轮为 deferred 真实组件与真实 ASGI HTTP 契约证据，不扩成外部 Provider／数据库端到端结论。Task 12 全仓门禁、Task 13 浏览器证据持久化及原报告其余跨任务限制继续有效。
- 本轮没有源码／索引／HEAD／提交修改，没有秘密读取，没有子代理；仅追加本审查报告。

### Assessment

**Task quality:** Approved（Task 9 Fix1 task gate；原 M1 留 Task 12）

**Reasoning:** 两处原 Important 的失败路径都有相应状态／契约修复和具名回归证据。新增 API 提取保持 opt-in，并将请求校验拒绝与可能已经进入业务的 400 分开，未以放宽未知命令约束来换取可编辑体验。
