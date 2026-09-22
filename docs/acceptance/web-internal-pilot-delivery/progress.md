# SDD ledger — plan: docs/superpowers/plans/2026-09-07-web-internal-pilot.md

Base: 37e76497f23b016296d4c563312663d96a8582d4
Branch: codex/web-internal-pilot
Worktree: /Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion
Spec: docs/superpowers/specs/2026-09-07-web-internal-pilot-design.md

## Preflight

|Tasks|Shared surface / producer → consumer|Finding|
|1|DTO/password/session/migration → own PG behavioral tests|一致；管理不创建Employee，避免infra业务seed|
|2|Cookie/identity/admin → API and CLI integration tests|一致；真实模式显式依赖，未配置保持拒绝|
|3|Profile/storage/backup → real owned persistence tests|一致；start应用完成在Task4，不虚报当前可完整启动|
|4|Runtime/frontend → mode/state/runtime tests|一致；精确签名以已审Task报告消费；不改旧dev语义|
|5|Built browser and cold restore → delivery evidence|一致；验收缺陷由控制者派回负责者，不越权自修|
|1,2|AuthenticationService/management signatures → cookie/CLI|绑定Spec签名；管理内部精确参数记录Task1报告|
|1,3|revoke_all/auth session table → restore revocation|恢复后逐tenant撤销，不恢复登录能力|
|1,4|real sessions → runtime binding|API注入service，worker不接浏览器会话|
|1,5|expiry/revoke → E2E|库层并发测试与浏览器效果分层、不重复全库基线|
|2,3|CLI account config → PilotConfig|Task2先显式factory函数，Task4接profile，避免未定义导入|
|2,4|API args/DTO/generated types → login/build|Task2报告精确cookie/CSRF/DTO，Task4只消费生成类型|
|2,5|current employee gating → browser role proof|后端最终裁决，UI不授权|
|3,4|profile/runtime env/owned process → launcher|Task3公开技术接口，Task4装配唯一canonical工厂|
|3,5|stop/restart/backup/restore → owned E2E|保持原profile和数据，只清理测试owner|
|4,5|built SPA/same-origin CLI → browser evidence|无Vite dev身份头或mock替代真实cookie|

Ruling: 用户已批准这一步，直接设计并按SDD执行，不重复征求技能流程选择或登录实现许可 — 开发者明确既有授权优先且要求持续完成 — 若理解偏差会产生可撤销的本地实现返工。
Ruling: 在既有linked worktree新建codex/web-internal-pilot分支，基于已归档37e7649；不新建另一目录 — 复用已验证依赖且保留已完成分支指针 — 工作目录名称仍旧，文档须使用真实绝对路径避免混淆。
Ruling: 单租户loopback内测、同源cookie、scrypt、8小时会话等采用Spec技术默认；共享TLS与真实Provider后续独立验收 — 适合当前先Web本机验证，不要求用户透露凭证 — 后续共享部署有额外安全与运维工作，不可直接公网开放。
Ruling: 新profile首用要求操作者显式业务政策文件，测试使用独立合成政策且不初始化真实用户profile — HANDBOOK禁止AI默认评分/SLA/业务阈值 — 首用多一步配置，不能宣传零配置业务运行。
Ruling: 冷备份只允许停写后恢复到新owner，不提供覆盖恢复或自动数据清理 — 保证原数据可保留并验证对象/数据库一致 — 备份有停机时间且需要额外磁盘空间，本轮无备份加密/自动保留策略。
Ruling: 历史基线9318和Web411属于旧代码，不重复跑无关全库，按新改动覆盖测试并最终独立审查 — 成本与测试相关性要求 — 全库新版本通过不能声称；发现跨面影响再补测。

## Progress

Task 1: pending
Task 2: pending
Task 3: pending
Task 4: pending
Task 5: pending

Task 1 BASE: b31b8d2a6e5d5dbc4ab3b938d6cf00eab80f25c1

Task 1 agent: /root/pilot_auth (gpt-6-astra high; credential/concurrency architecture)
Ruling: CSRF改为以高熵session token作key的域分离HMAC推导，新增get_session返回当前expiry与CSRF；仍仅保存摘要 — 独立随机CSRF且只存摘要无法在刷新后恢复，GET轮换又破坏多标签且产生写操作 — 格式成为稳定安全契约，未来调整需版本化/撤销旧会话。已在Task1实现前/过程中通知实现者更新Spec。

Ruling: Task1管理接口采用已有账号username绑定，revoke_all(username=None)仅撤销bound tenant，发行/管理按tenant→account统一锁序；ADR使用首个空编号0067 — 0028已存在且全租户撤销必须与并发发行串行 — 登录管理吞吐受单租户锁限制，适合本机内测，扩大部署须重新评估。
Ruling: Task4新增明确LOCAL_IN_APP通知模式并留ADR0068，不借用受控专用mode — 就近AGENTS只允许controlled入口使用旧例外，内测需要准确表达站内实际送达 — 本轮不提供邮件通知，重要消息仍需用户打开Web；默认生产邮件要求保留。
Ruling: Web/API端口首建后固定，存储端口变化显式更新 — 用户书签和cookie来源需稳定，Docker存储重启端口可能变化 — 固定Web端口被占用时需操作者解决，不能悄悄换地址。
Preflight amendment Task4: added notification runtime/AGENTS/ADR0068/test surface; independent from Tasks1–3 and consumed by Task5 worker readiness evidence.

Preflight file-map correction: apps/web has no local env.d.ts; Task4 edits existing client/App and may use Vite built-in env typing, removed nonexistent Modify target.

Preflight Task2/4 health interface: exact GET live/ready is safe no-identity probe with Host validation; capabilities authenticated; no monitoring account and no wildcard anonymous matcher. Existing probe emits status only.

Task 1 checkpoint (implementer, not review): initial RED12 missing module with real PG; GREEN20 authentication behaviors;0060→0059→0060 and ORM comparison passed. Static/migration checks and report still pending; no completion claimed.

Plan inline selfcheck: five tasks/five interfaces/balanced code fences and sequential Create→Modify map passed. First naive existing-only check rejected Task4 script created by Task3; corrected checker to honor prior planned creations, no plan gap.

Task 1 implementation: 6c8a2de5f443eba8c705e8044757fdb636b50acf;25passed21.07s, mypy3, ruff, boundaries, migrations; report task-1-report.md.
Task 1 review: /root/pilot_auth_review (gpt-6-astra high) reviewing review-b31b8d2..6c8a2de.diff; pending.
Task1 known costs: tenant hash verification holds lock (serial), historical session retention unbounded; review must independently triage.

Task 1 review result: Spec issues / Quality Needs fixes; I1 Important (test failure assertion rewriting can expose raw synthetic authentication materials);0Critical/0Minor.
Task 1 fix round1/5 dispatched original /root/pilot_auth; FIX_BASE6c8a2de5f443eba8c705e8044757fdb636b50acf; scope new auth test assertions and bounded captured fault-injection diagnostics proof.
Task 1 cross-task warnings resolved by scope mapping: private session response, Host/Origin/CSRF, dev rejection and atomic CLI→Task2; request limits/logging→Task2/4; owned loopback/provider refusal/restore→Task3/4/5; browser cleanup→Task4/5. Not marked integrated complete now; carry exact review pointers in briefs.

Task 1 fix round1 implementation: cef101e0b80c84fba672ccf496529745a2078283; tests-only,26passed24.51s, three real pytest fault-injection failure-output cases; ruff/boundaries passed.
Task 1 scoped review: /root/pilot_auth_recheck (gpt-5.6-sol high) on review-6c8a2de..cef101e.diff;pending.

Ruling: 认证层只为login增加JSON/4KiB要求，业务写请求保持原router的MIME/空命令协议，统一强制Origin+自定义头+会话CSRF — 现有Blob上传采用真实MIME，且存在合法空POST，统一JSON门槛会破坏已有协议并复制业务校验 — 其他请求体保护继续依赖既有router；认证检查必须覆盖所有写路径，Task2/4测试验证。

Task 1: fix round1/5 (1 addressed,0open; I1 safe test failure diagnostics;commits6c8a2de..cef101e). Scoped review clean, no new breakage.
Task 1: complete (commits b31b8d2..cef101e, review clean). Cross-task integration requirements explicitly mapped to subsequent tasks above.
Task 2 BASE: cef101e0b80c84fba672ccf496529745a2078283

Task 2 agent: /root/pilot_api (gpt-6-astra high; auth middleware/current employee/API security integration), implementing.

Ruling: Task3采用存储容器全部停止后的物理卷冷归档，恢复要求相同镜像ID — 直接满足零写入一致性边界，避免运行PG目录复制或跨对象/DB非原子快照 — 备份体积和停机时间较高，跨版本逻辑迁移不在本轮。Docker官方cp文档确认stopped与tar流支持；所有权/具名卷真实性仍须实测。

Ruling: Task2补员工域公开纯创建校验与infra认证provisioning_scope事务端口 — 现有EmployeeService没有创建校验，CLI直接持有业务规则或复制私有限流锁会违反边界 — 增加小型公共接口与对应测试，后续HR功能需复用/扩展，不能把本机可信CLI当Web授权入口。更新Task2文件表、Spec和brief；ADR0067补充不占0068。
Preflight amendment Task1/2: auth service/tests now intentionally extended by Task2 public transaction scope; previously approved core remains source basis, Task2 diff review covers scope locking and API use. Task2 self consistency: pure validator no read/write grant, all actual writes remain trusted local scoped transaction.

Ruling: 更正上一条provisioning_scope要求，只保留员工域纯校验；复用create_account(session=...)之后仍持有的tenant锁，再锁读经理并校验至同事务提交 — 实现者澄清原顺序未访问私有锁，控制者前一判断信息不足 — 已付文档返工，避免不必要公共API；Task2必须测试无效经理整笔回滚。撤回前一preflight的auth service扩展文件，Task1认证核心无需更改。

Ruling: cookie名改为tradeos_session_加已验证Origin端口 — 原环境与恢复环境可在同一127.0.0.1不同端口，cookie没有端口隔离，固定名会互相覆盖 — 技术cookie名称成为内部契约；端口变更需重新登录，稳定Web端口设计保持。增加同cookie jar双来源测试，不增加公共配置选项。

Task 2 checkpoint (implementer, pending review): 31 initial RED→31 API GREEN; CLI6 RED→6 GREEN; expanded144 passed after exact OpenAPI route assertions updated. Pure domain validator, safe parser and port-cookie regression being finalized; no task completion claimed.

Ruling: logout成功契约采用204无正文，修正计划示例中的200 — 规格要求POST撤销且清cookie，未限定成功状态；实现已使用更准确的无内容响应并导出schema — 客户端应按成功状态处理而非强制解析JSON，Task4严格消费生成契约；仅计划文档调整，无生产修改。

Task 2 implementation: af4d9b64ba9df078adcfab81bd12bbd64341b28a;147主要回归通过，后续测试安全断言修改后API42通过，runtime26通过；mypy8/ruff/boundaries/gen:api/Web typecheck通过，不累计重叠测试。报告task-2-report.md。

Task 2 review: /root/pilot_api_review (gpt-6-astra high), review-cef101e..b31cffd.diff; HEADb31cffdb02be82df9895bab6929dbc7f7445b38b (source af4d9b6 plus plan-only204 alignment); pending.

Task 2 review result: Spec compliant / Quality Approved; 0 Critical, 0 Important, 1 Minor M1 (manager FOR UPDATE stronger than shared lock).
Task 2: minor (deferred): M1 stronger manager row lock can add contention; final whole-branch review must triage.
Task 2 cross-task warnings resolved: actual profile CLI/engine/getpass wiring and loopback/static/provider refusal belong to Task4; full browser/multi-tab/restart/restore acceptance belongs to Task5, with storage restore session revocation supplied by Task3. Briefs explicitly carry these obligations; no end-to-end completion claimed.
Task 2: complete (commits cef101e..b31cffd, review clean; 1 deferred Minor).
Task 3 BASE: b31cffdb02be82df9895bab6929dbc7f7445b38b

Task 3 agent: /root/pilot_storage (gpt-6-astra high; owned persistence/restore architecture), implementing.

Task3 interface preflight: explicit policy JSON wraps existing handoff_policy/scoring_policy fields; PilotConfig.read(profile/config.json), sync PilotProfile methods and locked internal variants accepted. Operation locks remain bounded; Task4 owns supervisor lifetime/stop signaling and must avoid lifetime-lock preventing stop. No business defaults accepted.

Task3 checkpoint: unit RED PILOT_PROFILE_MISSING and integration missing resources/backup modules recorded; private config in progress. Existing HandoffPolicy/ScoringPolicy reused. Owned stopped-container copyUIDGID archive approach planned, root/ownership restore still must be empirically verified. No blocking ambiguity.

Task4 named risk preflight: same-browser login rotation revokes prior shared-cookie session; other tabs must invalidate old employee state through allowed logout event with no identity/secret payload. Carry unit behavior and Task5 browser scenario; prevents current cookie B data landing in stale employee A page.

Task3 checkpoint: implementer reports first real owned storage stop/restart/backup/fresh restore integration passed; archive member identity/UID/GID/mode/file hashes, tenant/employee/bucket/object SHA preserved, restored old session denied. Unit private config/permission/owner/lock/stable-port gates green; boundaries passed. Edge tests/static cleanup and independent review remain.

Task3 implementation: 4bc696a2167b0e7fcb5dbd8d91068ac55df1ff55. Report task-3-report.md: 27 focused passed24.74s; subsequent changed-code regressions2passed4.16s and2passed0.45s; 28 tests now total, not an aggregate fresh run. ruff/mypy5/boundaries passed; owned containers/volumes0. DONE_WITH_CONCERNS are explicit Task4 wiring, not unresolved Task3 correctness concerns.
Task3 review: /root/pilot_storage_review gpt-6-astra high on review-b31cffd..4bc696a.diff, pending.

Task3 review: Spec issues / Quality Needs fixes; I1 Important restore KeyboardInterrupt and diagnostic save errors bypass storage stop/client close;0Critical/1Minor M1 unsafe tar tests can pass due missing valid data root.
Task3: minor (deferred): M1 malicious tar member tests need valid archive baseline to isolate rejection reasons; final review must triage.
Task3 fix round1/5: original /root/pilot_storage, FIX_BASE4bc696a2167b0e7fcb5dbd8d91068ac55df1ff55; I1 only, independent cleanup/finally and focused interruption/failed-save regressions.
Task3 cross-task warnings mapped: complete three-app supervisor/latest config/fd/LOCAL_IN_APP/account/loopback Task4; browser and full lifecycle Task5. No integrated completion claimed.

Task3 I1 fix implementation: c669eec841fa3fb9a5ccf6fc2bf7183130bca592; actual owned interruption/diagnostic I/O RED2; GREEN3 including normal cold restore20.86s; strengthened persistent diagnostics + CLI interruption3passed12.53s; ruff/mypy2/boundaries passed; owned containers/volumes0.
Task3 scoped fix review: /root/pilot_storage_recheck gpt-5.6-sol high, review-4bc696a..c669eec.diff; pending.

Ruling: Task4新增scripts/pilot_web_supervisor.py承载三应用生命周期，run_web_pilot.py保持CLI解析/派发 — 完整健康/信号/OwnedProcess收口需要独立职责，已有controlled采用同形拆分，避免把所有流程塞进CLI — 新增一个内部模块需要纳入审查/测试；不改变用户命令或进程边界。

Task3 fix round1/5: I1 ADDRESSED,0open/new C/I; scoped quality/spec pass, commits4bc696a..c669eec.
Task3: complete (commits b31cffd..c669eec, review clean;1 deferred Minor M1). Cross-task supervisor/browser warnings mapped to Tasks4/5; corresponding exact interfaces appended Task4 brief.
Controller plan-only commit d0be8f9fdb6ed0199ca5e1a375db3962e19f6f30 adds supervisor file responsibility.
Task4 BASE: d0be8f9fdb6ed0199ca5e1a375db3962e19f6f30

Task4 agent: /root/pilot_web_runtime (gpt-6-astra high; full auth/browser/runtime architecture), implementing.

Ruling: Task4扩展notification health及scheduler config/runtime窄接线 — 旧health只识别controlled，旧scheduler无条件要求Gmail/DKIM而pilot禁止虚构外部配置 — 新增显式pilot-only typed/parser/factory端口，原生产/受控必填校验保留；缺DNS/发送配置必须真实失败，不得fake成功。代价是扩大兼容性回归面，若错误可能影响旧入口，因此Task4补旧默认与新缺项拒绝测试。

Task4 checkpoint: Web auth missing-module RED→5GREEN and LoginPanel RED→1GREEN; covers logout204/failure, upload MIME+CSRF,403retain,401generation,cross-tab rotation. Runtime5 initial missing-entry/mode RED; API canonical engine/lifespan/static in progress, scheduler/notification approved seams next; real supervisor tests outstanding, no blocker.

Ruling: Task4为S3配置新增显式from_pilot_environ，返回dev_mode=False且只接受canonical http://127.0.0.1:port — 原parser只有开发模式允许HTTP，借dev=True会模糊真实认证模式 — 增加connector config/AGENTS与严格地址回归，默认parser保持；代价是维护独立受限解析入口，未来HTTPS共享部署不能复用本机例外。

Task4 checkpoint: actual three-process boot/health success and duplicate-start refusal; rapid stop/reserve found TCP TIME_WAIT fixed-port reuse issue; first complete Web run exposed obsolete authenticated-header and dev fixture expectations.
Ruling: Task4允许reserve_port设置SO_REUSEADDR以支持稳定端口快速重启，禁止SO_REUSEPORT并保留活监听者拒绝 — 实际停机后TIME_WAIT挡住原地址重启，属集成发现的Task3窄缺陷 — 需同端口真实重启及占用回归，若错误会影响唯一监听者保证。
Task4 file-map amendment: old authenticated X-Employee/X-Tenant tests now must assert absent headers; isolated DEV header and rendering contracts preserved, production remains login gated.

Task4 test-contract correction: catalog product mock previously selected synthetic tenant via authenticated identity headers. Approved capture provider.current snapshot at request receipt and assert headers absent, preserving authenticated/late-A/current-B scenarios; no mode change to dodge real-mode contract. Added exact existing test file to plan/brief.

Task4 checkpoint: actual built-Web Playwright login/refresh/two-tab logout at1440/390 pass, no page errors/browser persisted auth, owned cleanup complete. Web418passed; runtime10passed; broader Python207passed/1obsolete Task3 start-stub assertion pending correction; lint0errors/87existing warnings after formatting changed files; boundaries pass.
Task4 file-map correction: test_pilot_profile.py former application_launch_not_configured expectation must now assert fixed launch-failure/no false ready using real supervisor seam; actual boot/health/restart evidence remains separate.

Task4 implementation: 0d370b1b8b545ad66207977ed0410d5eec3fcd93;26 files, tracked clean, historical output400. Full report read; Web418 plus post-responsive46, final runtime/notification14passed20.08s, affected sets preserved individually (207+obsolete stub corrected focused, notifications27, canonical34, profile/S3/pilot137). lint87 existing warnings in four untouched files; no false pristine claim.
Task4 review: /root/pilot_web_review gpt-6-astra high on review-d0be8f9..0d370b1.diff, pending.
Controller visual check: viewed Task4 desktop blank-login and390 business screenshots via view_image; login readable, navigation fits within header, no apparent horizontal overflow. This is static visual inspection only, not independent browser execution or business-data acceptance.

Task4 review result: Spec issues / quality Needs fixes;0Critical/3Important I1 logout retry loses required CSRF, I2 stale logout clears/broadcasts new identity, I3 unexpected app exit recorded requested_stop.
Task4: minor (deferred): M1 lint87 existing warnings in untouched files, retain accurate reporting; final review triage.
Task4 fix round1/5: original /root/pilot_web_runtime, FIX_BASE0d370b1b8b545ad66207977ed0410d5eec3fcd93, all I1-I3; focused auth/App tests and real owned runtime crash test, no unrelated baseline rerun.
Task4 cross-task warnings resolved by accepted Tasks1-3 reports and explicit Task5 full role/disable/reset/restore browser scope; no completion claimed before fix gate.

Ruling: Task4 I2采用同源Web Locks序列化login/logout，另保留busy与generation fence，缺API固定拒绝 — 单页代次只能保护JS状态，无法排序其他标签迟到Set-Cookie；W3C规范确认合作同源异步排他锁 — 新增浏览器能力要求，当前只实测Chromium，旧浏览器可能不能登录；不把Web Locks当服务端权限或恶意同源隔离。Spec已同步，Task5操作说明需列支持检查。

Task4 fix1 checkpoint: auth I1/I2 RED4→GREEN13 including retryCSRF/busy/noWebLocks/generation/cross-modulelock/Apphidden; I3 actualowned schedulerSIGKILL REDstopped/requested_stop→GREEN2 crashfailedcleanup+normal3processstop/restart/schema. Typecheck/scopedlint pass. Actualtwo-tab delayedlogout/newlogin + directretry/serverrevocation browserproof pending beforecommit/review.

Task4 fix1 implementation: ecfb4d2d959ade7ffa143b7b9ad1b8e29cde4242;6files including controller design amendment. FocusedWeb14passed572ms (anonymous pendingrestore RED also fixed); actualowned crash+normal3process2passed18.98s; final rebuiltChromium directretry→204→actual oldcookie revoked and delayedlogout headers→queuedlogin→refresh survival passed. typecheck/build/scopedlint/ruff/mypy/boundaries pass; cleanupcomplete/trackedclean/output400.
Task4 fix1 scoped review: /root/pilot_web_recheck gpt-6-astra high on review-0d370b1..ecfb4d2.diff, pending.

Task4 fix round1/5: I1/I2/I3 ADDRESSED,0open/new C/I, scoped spec/quality pass, commits0d370b1..ecfb4d2.
Task4: complete (commits d0be8f9..ecfb4d2, review clean;1 deferred Minor existing lint87). Complete runtime/current-Web integration accepted; full role/disable/reset/backup browser lifecycle remainsTask5.
Task5 BASE: ecfb4d2d959ade7ffa143b7b9ad1b8e29cde4242

Task5 agent: /root/pilot_acceptance gpt-5.6-sol high (existing-interface acceptance/tests/docs), implementing.
Finalization preflight: read requesting-code-review and finishing-a-development-branch skills/templates. Full branch review uses initial37e7649 baseline, all task gates/evidence and deferred minors; no unprompted merge/push or sharedgit cleanup. Higher developer affected-test/no redundant rerun requirements retain existing verification strategy; no integration into another branch is planned.

Final review baseline exact: 37e76497f23b016296d4c563312663d96a8582d4; verified ancestor of active branch.

Task5 checkpoint: new E2E written, not yet committed/run; plannedcoverage actual builtWebcookie/boss-sales/currentauth/refresh/fullstopstart/DBobjectSHA/logoutretry/WebLockorder/disableenable reset/coldfreshrestore transplantedoldsession/sourceunchanged/390px. Staticself-review beforetestcommit; no productiondefect reported yet.

Task5 checkpoint: actual new complete E2E passed at test commit c1cdad875bea547e20fdaefbf006ac31f8ec9c3b,1passed28.52s. Earlier test-only corrections: RowJSONnormalization,duplicatePlaywrightclose,duplicatetextlocator,awaitprecedence,expected401/403/abortednetwork consoleclassification. No productiondefect reported; specifieddocs now inprogress. Exactfullreport/review stillpending.

Task5 implementation complete: tested sourceecfb4d2/testc1cdad875bea547e20fdaefbf006ac31f8ec9c3b,1E2Epassed28.52s; docsHEAD541bf2d289b1b79e01bc46766a96628d790dcf2b. Report fullyread. Python3.12.14/Node24.15.0/Docker29.5.3/Chromium151.0.7922.34; genapiunchanged/build/ruff/compile/boundaries/head0060checked. Earlierfailures solelytestharness documented, no productionedit/defect.
Task5 review: /root/pilot_acceptance_review gpt-6-astra high on review-ecfb4d2..541bf2d.diff, pending.

Task5 review result: Spec issues / quality Needs fixes;0Critical/3Important I1 actualdirectaccountswitch/twoauthtabs invalidation missing, I2 logout_headers event is request interception not response proof, I3 logout401 from clearedjar notoldtokenreplay.
Task5: minor (deferred): M1 consolecollector ignores allwarning/error duringfaultwindow and401/403 globally; finalreview musttriage, meanwhiledocs mustnotclaim narrowerfiltering.
Task5 cross-task warnings resolved by prior acceptedreports: Task1/2 authCLI/currentpermissions/migration; Task3 ownedstorage/backup; Task4WebLocks/affectedchecks/lint87. Named UI doccheck: apps/web/src/views/team/TeamCenter.vue:166 renders 员工ID with item.employee_id; manager ID operator instruction supported by existingUI, no extra browserrun.
Task5 fix round1/5: original /root/pilot_acceptance, FIX_BASE541bf2d289b1b79e01bc46766a96628d790dcf2b; tests/docs only I1-I3, exact oldcookie replay/responseevent/directswitch/twoauthviews required. If strengthenedtest discoversproductiongap, report tocontroller for priorimplementer.

Task5 fix1 checkpoint: strengthenedtest63150da376b07414088f5c0f90bcb7ea5f22e075 passedfullE2E1/34.15s. I1 directBsaleslogin whileAbossviewmounted clearsA, thenbothsalesauthviews oneBlogout clearsboth; I2 actualserver204 heldbeforeresponsedelivery with WebLocks held+pending, responseeventbeforeloginrequest andrefreshstable; I3 separateoldtokenreplays401 afterretry/ordinary/final logout. No productiondefect, docs/M1truthfulscope correctionpending.

Task5 fix1 implementation final: test63150da/fullE2E1passed34.15s, docs6bb5f51d81df5e7f6d83f514bed5dac175efcca0. Fix report has exact covering commands/results; ruff/py_compile/collect/diff passed, ownedcleanup0errors. No production change. M1 broadcollector explicitly disclosed and strong claims withdrawn. Scoped review /root/pilot_acceptance_recheck gpt-6-astra high on review-541bf2d..6bb5f51.diff pending.

Task5 fix round1/5: I1/I2/I3 ADDRESSED,0open/new C/I, scoped spec/quality Approved, commits541bf2d..6bb5f51. M1 docsaccurate/collector deferred.
Task5: complete (commits ecfb4d2..6bb5f51, review clean;1 deferred Minor consolecollector). All five task gates complete.

Final broad review: /root/pilot_final_review gpt-6-astra high; baseline37e76497..HEAD6bb5f51;19commits466109bytes; all taskgates complete, four deferredminors supplied; pending.

Final broad review complete: final-review.md Approved for localpilot; new Critical0/Important0/Minor0; four deferred items triaged, no required fixes. Full report read; existing task gates remain valid, no duplicate tests performed.
Ruling: 最终接受经理排他行锁与历史87条lint告警作为本轮已知代价；保留tar合法根基线不足和console宽过滤两项非阻断Minor，不开启无必修项的最终修复波 — 独立全分支审查未发现生产绕过，直接功能断言成立，文档已明确撤回过强观测结论 — 后续可能漏掉归档拒绝条件或并发console告警的回归；扩大告警门禁前须补强这两项，真实争用出现后再评估经理锁。
Ruling: 采用finishing-a-development-branch的保留分支方式，不再询问合并菜单，也不重复同代码全库测试 — 已授权范围是本机Web实现与收尾，用户此前选择保留继续，开发者要求完成已有授权且只补受影响检查 — 不提供合并后或全库新版本通过的证据；分支仍需用户将来决定如何集成。
Ruling: 将本计划全部Markdown任务记录与完整Ruling原文归档到docs/acceptance/web-internal-pilot-delivery，核对哈希与Git提交后仅删除本计划scratch — 保留可审查决策/反转/成本并遵循SDD收尾，不让决策随临时目录丢失 — 原报告中的scratch绝对路径成为历史位置，README与manifest提供归档映射；diff包以提交范围和哈希重建，不复制冗余补丁。
Final delivery state: all five tasks complete; localpilot final review Approved with two disclosed nonblocking testing minors; source ecfb4d2, enhancedE2E63150da (1passed34.15s), reviewedHEAD6bb5f51. Branch codex/web-internal-pilot and existing worktree retained; no push/merge, no realuserprofile or defaultpolicy/password. Historical output400 preserved.
