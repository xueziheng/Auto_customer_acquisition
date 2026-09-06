# Task 9 实施报告：错误、暂停与恢复

## 范围与结论

基点 `91c77754e295f5eb9754bd87e4e4f920985ae2fe`。唯一实现者在 `/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion` 实施；所有命令显式workdir。未重做Task0–8，未扩Task10成本跨页、11指标、12全仓、13文档。源码已冻结；源码SHA在末节，报告另行提交。没有派子代理或自行安排独立review，交controller独立审查。

正式子规格：同目录`task-9-spec.md`。应用TDD、verification-before-completion、frontend-testing-debugging和implementer-prompt指令。Browser plugin/skill未提供，故使用原Python Playwright。

实现：

- Run 401身份失效、403权限、404精确缺失、503不可用区分；列表失败不显示无记录，详情缺失不显示选择提示、不回退其他Run。读取失败计数为未知，保护性拒绝让旧通道失效。
- Campaign身份/入组读取分别保留loading/error，失败清旧选项并禁止草稿提交；暂停仅调用原pause，激活仅原activate。未知操作先核对精确Campaign当前状态，旧active不能解除未知pause锁；在途尝试和已有quota不抹除。未具备精确恢复命令的创建/修订未知结果只显示待核对，不提供换键重做。补窄屏动作、表单与表格布局。
- Settings研究配置读取失败不隐去/伪装未配置；Playbook与国家政策未知命令冻结body与原key，保留候选已持久但Run尚未确认的可能性。恢复先读取原历史，再以原命令返回精确accepted，不按内容或最新行猜成功。首次明确400/422校验拒绝允许修正；此前未知不能因后续普通错误认定原命令未落库。Playbook/versions并行任一保护性拒绝立即撤销全页，不等待另一请求。
- Sourcing保留原reconciliation_id/payload/actor，不用随机HTTP键冒充耐久幂等。ADR0064新增后端计算的`recovery_action`：unavailable / record_reconciliation / resume_reconciliation / event_delivered，原boolean仍仅首次核对。后端核精确tenant/case/run/execution/canonical、quota uncertain/consumed、当前actor、active public_search与持久事件指纹；错误读取不开放动作。Web按canonical和action恢复同一记录，事件送达不宣称业务完成。
- 新HTTP header由reconciliation_id稳定派生；controller明确接受legacy随机header不可找回时以同canonical派生稳定header续交付既有事实。原域/额度/engine仍是耐久权威，不新增表/迁移/通用命令账本。刷新恢复也不创建新reconciliation_id。
- Sourcing草稿切换目标清输入，目标消失不选首条；原未知命令冻结，身份/Case切换使旧success/error/finally失效。Case/写拒绝全页失效；已授权Case内独立投影403只关闭对应投影，保留quota不可读时合法的草拟/确认能力。
- 发件身份预热前置要求auth_pending且SPF/DKIM/DMARC当前全部通过；重用Task8容量和反馈，不重建配置。入站只使用原GET status及POST retry(expected_version)，不传cursor；未来next_retry_at不提前解除，409重读canonical，未知结果只读核对，永久原因仍显示，原位请求接受不宣称已恢复。

## 环境

- Python `.venv/bin/python` 3.12.14；系统python3为3.9.6（只用于简单文件工具，不用于项目结构检查）。
- Node v24.15.0；npm 11.12.1；Vitest 4.1.10；Vite 8.2.1；openapi-typescript 7.13.0；Playwright 1.62.0 / Chromium。
- 后端测试全部使用 `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python ...`；隔离Testcontainers数据库，不加载dotenv/既有DSN。
- 浏览器单独新建owner `127d051dedf44257bb6ddb9adb290fca`；原Supervisor、PG、MinIO及API/scheduler/notification/Web四进程。Web `http://127.0.0.1:52037`；API `http://127.0.0.1:52036`。
- owned目录 `/var/folders/t2/6_w0ct0s04l68z693_h2fzkw0000gn/T/tradeos-controlled-127d051dedf44257bb6ddb9adb290fca`。未读取config.json、.env或任何密钥/DSN，未seed业务结果，未运行真实发送、部署、桌面或认证系统。

## RED → GREEN 与失败实录

所有npm命令基准workdir为worktree，`npm --prefix apps/web ...`；直接eslint/build workdir为worktree/apps/web。

1. 新增8项组件回归：`npm --prefix apps/web test -- --run tests/web-core-state-recovery.test.ts`。
   - 初次8失败且2unhandled，由新增fixture缺countryOverview.coverage导致；不是业务RED，修fixture后重跑。
   - 合法RED：exit1，8 failed/8。分别观察入组失败被空集替代、403旧数据复活、Run错误显示空集、Settings研究失败隐去、503可编辑、未认证预热可点击、入站retry缺失、Sourcing两次reconciliation_id不同。
   - 首轮GREEN：exit0，8 passed，919ms。
2. 后端公开恢复动作：`env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/integration/test_sourcing_plan_confirmation.py -q -k reconciliation_recover_after_restart`。
   - RED exit1，1 failed/1 deselected，7.05s；真实canonical保存后读取无recovery_action属性。
   - GREEN exit0，1 passed/1 deselected，6.19s；同一真实PG作用组增加ack失败、ack后event失败、当前同actor、错actor、已投递与同命令重放断言。
   - 最终该作用组还验证无active Run/额度读取失败均unavailable；真实canonical行和quota reservation仍各1，旧域方法重复调用不新增业务事实。
3. 前端消费resume action：同新组件命令，RED exit1（1 failed/8 passed，期望第二POST实际仅1）；实现后纳入GREEN。
4. 首次旧组件聚焦7文件：exit1，9 failed/85 passed。4项预热测试旧fixture未给auth事实，补真实auth DTO前置；旧Settings503测试允许未知payload变化，改为三次同命令到202后才新草稿/新key；其余是权限提示/历史刷新与202结束时表单可用性，已修正。
5. Sourcing旧作用组41测试首次7失败：6项揭示局部403不应清整个已授权Case，恢复原独立投影语义；1项旧测试依赖自动首选执行，改为用户明确选择且先选择后输入。错误修正期间出现原测试清理路径`/`无匹配的VueRouter警告；最终成功组没有该警告。
6. 新country503测试首次错误selector找不到按钮，改为真实可见恢复按钮；不是业务失败。
7. Campaign精确状态核对回归：先保留旧“任何刷新即解锁”行为，纠正测试中不存在window.prompt及选择了标题含暂停的列表行两处harness问题后，RED exit1明确expected disabled=true实际false；恢复实现后14 passed。
8. Settings并行权限拒绝：RED exit1（1 failed/14 deselected），一个403、另一个versions挂起时页面还在加载且未显示权限；改为每个响应到达立即保护性失效。针对新组件+Settings最后64 passed，exit0。

## 最终验证命令

结果只对应各自命令，不把分批相加当全仓结果。

| 作用组 | 命令 | 实际结果 |
| --- | --- | --- |
| 后端寻源原作用组 | `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/integration/test_sourcing_plan_confirmation.py tests/unit/test_sourcing_router.py tests/unit/test_sourcing_v2_contracts.py tests/unit/test_sourcing_service.py -q` | exit0，232 passed，8.27s；原输出`/tmp/task9-backend.log` |
| 原入站重试契约 | `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/integration/test_email_inbound_access.py tests/integration/test_email_inbound_page.py -q -k retry` | exit0，4 passed/29 deselected，13.87s；覆盖CAS、旧version、429期限/固定阻断 |
| 研究预算与Catalog排队/陈旧状态 | `npm --prefix apps/web test -- --run tests/research.test.ts tests/catalog-product-proposal.test.ts` | exit0，2文件45 passed，2.09s；独立补证已有budget/queued/stale解释，不改这两页 |
| 新组件及Settings最终改动 | `npm --prefix apps/web test -- --run tests/web-core-state-recovery.test.ts tests/settings.test.ts` | exit0，2文件64 passed，2.08s；含15个本批新回归 |
| 完整本批前端作用组 | `npm --prefix apps/web test -- --run tests/web-core-state-recovery.test.ts tests/campaign-center.test.ts tests/settings.test.ts tests/run-center.test.ts tests/sourcing-case-detail.test.ts tests/sourcing-center.test.ts tests/sending-identity-center.test.ts tests/identity-registration.test.ts tests/core-access-revocation.test.ts` | exit0，9文件150 passed，4.93s；在最终结构类型lint修正之前，精确版本范围见末节 |
| 类型 | `npm --prefix apps/web run typecheck` | exit0；初次新DTO生成后fixture漏recovery_action曾exit2，补完整字段后通过 |
| 后端类型 | `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m mypy domains/sourcing/schemas.py workflows/sourcing_case/application.py` | exit0，Success: no issues found in 2 source files |
| Ruff | `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m ruff check domains/sourcing/schemas.py workflows/sourcing_case/application.py tests/integration/test_sourcing_plan_confirmation.py` | exit0，All checks passed |
| 结构 | `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python scripts/check_boundaries.py` | exit0，依赖、金额、置信度、事件、租户、AGENTS覆盖、域结构7项通过 |
| Web构建 | apps/web内`npm run build` | vue-tsc通过；Vite157 modules，exit0；最后一次结果在末节 |
| ESLint | apps/web内`./node_modules/.bin/eslint`，显式下列11个本批Vue/test路径 | 首次npm exec因cwd路径误用exit2；正确cwd修复格式后exit0；后增一段p有2格式warning，已修复，最终结果在末节 |

ESLint路径：src/views/runs/RunCenter.vue、src/views/campaigns/CampaignCenter.vue、src/views/settings/SettingsCenter.vue、src/views/sourcing/SourcingRecoveryForm.vue、src/views/sourcing/SourcingCaseDetail.vue、src/views/SendingIdentityCenter.vue、tests/web-core-state-recovery.test.ts、tests/settings.test.ts、tests/core-access-revocation.test.ts、tests/identity-registration.test.ts、tests/sourcing-center.test.ts。

API生成是两个独立过程，无管线掩盖exit：

- `.venv/bin/python apps/web/scripts/export_openapi.py`通过subprocess文件句柄写`/tmp/task9-openapi.json`：exit0，stderr0 bytes。
- `apps/web/node_modules/.bin/openapi-typescript /tmp/task9-openapi.json -o apps/web/src/api/api.d.ts`：exit0，150.1ms。生成唯一新增recovery_action枚举字段，不手写DTO。

敏感扫描仅显式17个本批路径：7个Web源码/API、5个Web测试、sourcing schemas、application、PG集成测试、ADR0064、task-9-spec。命令`env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python scripts/scan_sensitive.py <上述显式路径>`：exit0，无输出、零命中；末节另记最终报告扫描。未调用默认全仓扫描，未读.env。

## 实际浏览器QA（与组件/服务证据分开）

目标：Run缺失/Settings请求结果未知与角色拒绝/发件认证前置/入站等待与版本冲突/Sourcing同canonical恢复/Campaign读取失败与暂停 → 用户只看到真实可用操作。

命令：`.venv/bin/python -m playwright --version` exit0；`env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python /tmp/task9-browser.py`。

- 第一轮exit1：脚本使用不存在的“演练老板”精确标签而超时；已完成的Settings202丢响应恢复真实发生，未回滚或伪造。按原Supervisor实际“演练老板甲（提案）”标签修harness后继续新逻辑演练。
- 完整第二轮exit0：20个desktop1440×1000 / 390×844状态截图；页面标题TradeOS、目标URL、非空、无Vite overlay、无document水平溢出，pageerror0。
- 其中**真实owned API**：Run精确404（2图）、Settings原服务器202成功后浏览器丢失该响应并向UI呈503，原key/body再次POST确实仍202（未知/恢复4图）、实际销售身份403（2图）。这只证明响应丢失后的原请求恢复，不等于服务内部commit后start失败注入；服务内中断证据仅寻源PG作用组。
- **受控API响应，仅UI契约证据**：认证失败禁用（2图）；入站未来429期限、expected_version=7遇409并刷新blocked（4图）；Sourcing先503、刷新返回相同canonical+resume、同command/header恢复并显示event_delivered（6图）。这不是实际认证失败/入站provider阻断/浏览器触发真实寻源恢复的端到端业务证据。
- console仅记录error类型计数8，对应实际403/404与显式注入503/409的resource failure；无pageerror/框架overlay。未捕获原始响应或底层异常。该采集没有逐条保留URL/文本，因此不把计数当成逐条console审计证明。

截图实看发现：`sourcing-canonical-resume-390.png`下拉框超过卡片右边，document指标未发现局部裁切。加min-width/width约束后，`/tmp/task9-browser-focus.py`重新实测exit0，12个状态检查：Campaign读取失败、发件选项失败、pause后quota仍3/10共6图；Sourcing三状态6图替换同名旧图。额外断言select.right<=viewport；实际复看修正后箭头和边框完整，按钮/依据/理由可读。Campaign该轮只截取受控API响应并断言调用原pause路径，不宣称原服务真实暂停。

已实看：Settings未知390、权限desktop、身份前置390、入站等待390、Sourcing恢复390修前/修后、Campaign暂停390、Campaign发件错误390。错误/待核对说明、禁用按钮与当前额度可见，无动作卡覆盖。最后Settings并行保护修正后的实际角色拒绝另以`/tmp/task9-final-browser.py`复看，末节记结果。

所有证据位于`/tmp/task9-browser-evidence`：`qa.json`（20 checks）、`focus-qa.json`（12 checks，包含6个覆盖旧图）、PNG。不是32张不同截图，focus有覆盖。最终Task13持久化前这些为临时证据。

## 边界与尚未验证

- 没有真实客户发信、真实Provider/模型/外网认证、部署、多人登录或桌面端；角色演练仍是原本机开发身份。
- 未跑全仓测试或Task12 A1–A10。后端232、入站4、前端各具名作用组分别报告，不能相加作最终全仓通过。
- Settings已接受但响应丢失的历史仅呈现事实，不以同内容认定winner；真实服务内部故障的国家政策/Playbook重启恢复未在本批注入。
- Sourcing新action在读取期间仍可能变化，POST依原域/额度/Run最终重验。缺失当前合法action或canonical冲突只待核对，不能自动重试；已投递只表示恢复事件送达。
- 新增read projection每个execution读取Run/quota/event，受原limit50约束，未做性能基准。未改金额、证据、供应或报价边界。
- docs/AGENTS.md实际不存在；遵循root规则，未借此改全仓结构。

## 最终冻结、清理与提交

- 最终九文件命令实际输出 `Test Files 9 passed (9); Tests 150 passed (150)`，exit0，4.93s。其运行版本包含所有功能、样式及第15项并行403测试，随后仅将Settings局部泛型的 `response: Response` 改为 `response: { status: number }`，解决ESLint no-undef；没有运行行为变化。这个最后结构类型版本重跑新组件+Settings二文件，实际64 passed，exit0，2.14s。没有把64加进150，没有声称九文件组在类型修正后再整组重跑。
- 最后ESLint一度误写SendingIdentityCenter的settings子目录，exit2“no files matching”；修为实际 `src/views/SendingIdentityCenter.vue` 后，显式11路径ESLint exit0、无输出。随后同一进程 `npm run build` exit0，vue-tsc通过，Vite157 modules，458ms。最终结构自检再次exit0，7项全部通过；`git diff --check` exit0无输出。
- 最后实际API390角色拒绝浏览器命令exit0，输出 `status=passed, actual_api_role_refusal=true, viewport=390, pageerrors=[]`。已实看 `settings-final-forbidden-390.png`：研究权限说明、候选提交暂不可用和原卡片完整，无水平溢出、无overlay。controller亦独立实看Settings未知和Sourcing恢复390代表图。
- 清理前重读安全status并以psutil核对owner及Supervisor PID12565、born1788674566.124237，只向该PID发送SIGTERM。原启动进程exit0，输出 `status=stopped, reason=requested_stop, cleanup_errors=[]`。不读取private配置，仅检查存在性；独立再次按原PID/born、Docker owner label、5个private文件及4个监听端口核对，exit0，实际 `owned_processes=0, owned_containers=0, private_files=0, listeners=0`。未终止或清理其他owner资源。
- 最终敏感扫描使用上列17条显式路径并追加本报告，共18条；exit0，无输出、零命中。没有默认全仓扫描。
- 源码冻结提交 `ebb8da662c3be3e87e5451320a44238d0f0345c7`，命令 `git commit -m 'fix(web): clarify blocked states and recover original commands'` exit0，17 files changed，828 insertions / 209 deletions。包含正式子规格、ADR0064、生成API类型、实现和测试，不包含本报告。提交后源码冻结，等controller独立review。
- 本报告以单独提交 `docs: record task 9 verification and cleanup evidence` 交付；最终SHA由agent返回controller，以免自引用报告SHA。

Git stderr仅存临时文件并按字节计数，未读取噪音正文或修shared.git：task9-git-add-spec-stderr=143 bytes；task9-git-add-stderr=2288 bytes；task9-git-diff-check-stderr=3432 bytes；task9-git-diff-stderr=5148 bytes；task9-git-final-status-stderr=572 bytes；task9-git-source-commit-stderr=57629 bytes；task9-git-staged-stderr=6721 bytes；task9-git-status-stderr=572 bytes。
