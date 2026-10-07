# Task 11 实施报告：Run、接管与成本来源观测

状态：DONE_WITH_CONCERNS，等待controller安排独立审查；实施者未自行做独立review、未派子代理。

- 工作目录：`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion`
- 分支：`codex/web-core-completion`
- BASE：`940db7c86249aaaa85c3b18df710a804ffd1c398`
- 冻结源码：`4be8753e5b46442244e9dfe110bd4c308a367c25`
- 正式规格：`docs/design/2026-09-06-web-core-observability.md`
- 运维口径：`docs/operations/web-core-metrics.md`
- 本报告单独提交；精确报告提交ID由最终短回给出。

## 结果与边界

新增boss-only `GET /runs/observability`。服务端默认过去七天；显式start/end须成对、有时区、起点含终点不含、不超过31天且不晚于观测时刻。当前员工/tenant仍来自原身份解析与服务二次授权。单条SQL读取同租户canonical阶段实体、工具调用及当前接管快照，重复读取不写历史账本。

阶段按各自observed/created/accepted/closed时间字段入窗，状态取本次observed_at的当前值。当前有效已验证需求仅含validated / sourcing_ready / handed_to_sourcing，不把已履行/撤回/丢失记录算作当前有效，也不称为累计曾验证数量。各阶段不是同cohort漏斗，未算转化率。Opportunity记录数与接管数均未当合格机会数；五项联合资格缺证据时显示未知和缺项。

工具调用非duplicate记录数、当前累计尝试数、duplicate重放回执分列。窗口内创建的记录可能在窗口之外重试；Tavily同样是窗口内创建预留的当前consumed/reserved/uncertain状态，不是期间实际结算，也不是账号总量。没有模型供应商usage、人工计时、可信费率，token、工时、总额与单位合格机会成本均null。没有新增成本录入端口、钱包或回填；Money/Decimal契约保持。

Run详情按真实持久化绑定显示负责人/Need/Opportunity/Handoff安全引用。human_handoff通过同租户Handoff→Opportunity→Need，sourcing_case通过Case→Need及已绑定Opportunity。没有使用任意subject_ref前缀、context全文或租户任意接管猜链。OpportunityList无精确机会deep-link，controller已接受仅显示核验后的Opportunity ID；实际导航使用原精确接管（含机会组合）与Need页面，目标API重新授权。

Run记录时间跨度与等待都不当实际人工工作耗时。反序步骤/工具时钟输出未知；requested_at晚于观测时刻，接管最久等待未知并列异常数。当前requested全队列另标tenant_current范围；员工接管页只显示当前授权列表数量，明确最多50项。

新增观测读取失败清空该区；401/403清空Run列表/详情及观测，并失效在途响应。原Run深链、审批精确链接与scope generation保持。没有增加包含正文、地址、locator、hash或凭证的日志/通知。

## 环境与最终聚焦命令

Python3.12.14、Node v24.15.0、pytest9.1.1、Playwright1.62.0、ruff0.16.6、mypy2.3.1、Vitest4.1.10、Vite8.2.1、openapi-typescript7.13.0。Browser plugin/skill不存在，按brief使用原Python Playwright。下列cwd除ESLint为`apps/web`外均为上述工作目录。所有Python测试使用`env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1`，不读取既有DSN或.env。

| 精确命令 | 实际最终结果 |
| --- | --- |
| `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest tests/integration/test_web_core_observability.py tests/unit/test_run_audit_repository.py tests/unit/test_run_audit_service.py tests/unit/test_runs_router.py -q --tb=short` | exit0，16 passed，5.76s；含6个新PG集成测试 |
| `npm --prefix apps/web test -- tests/run-center.test.ts tests/handoff-queue.test.ts` | exit0，2文件46 passed，1.97s |
| `npm --prefix apps/web run build` | exit0；vue-tsc --noEmit与Vite160模块通过，build413ms |
| `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m ruff check workflows/engine/audit.py workflows/engine/observability.py infra/db/run_audit.py infra/db/web_core_observability.py apps/api/routers/runs.py tests/integration/test_web_core_observability.py tests/unit/test_runs_router.py tests/unit/test_run_audit_repository.py` | exit0，All checks passed |
| `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m mypy workflows/engine/observability.py workflows/engine/audit.py infra/db/web_core_observability.py infra/db/run_audit.py apps/api/routers/runs.py --follow-imports=silent` | exit0，5 source files无问题 |
| `./node_modules/.bin/eslint src/components/WebCoreObservationPanel.vue src/views/runs/RunCenter.vue src/views/crm/HandoffQueue.vue tests/run-center.test.ts` | exit0，无输出 |
| `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python scripts/check_boundaries.py` | exit0，分层、金额float、置信度、事件、tenant、AGENTS覆盖、域结构七组通过 |
| `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python apps/web/scripts/export_openapi.py > /tmp/task11-final-openapi.json` | exit0，独立export |
| `apps/web/node_modules/.bin/openapi-typescript /tmp/task11-final-openapi.json -o /tmp/task11-final-api.d.ts`；`cmp apps/web/src/api/api.d.ts /tmp/task11-final-api.d.ts` | 各exit0，生成类型无漂移 |
| `.venv/bin/python scripts/scan_sensitive.py` + `/tmp/task11-source-files.json`列出的15个显式源路径 | exit0，stdout/stderr均0行；调用由Python subprocess argv传入，无shell拼接路径 |
| `git diff --check --` + 同15路径；`git diff --cached --check` | 各exit0；仅自有15文件进入源码提交 |

API生成类型242行属于自动产物，前端没有手写重复业务DTO。没有修改迁移，因此本任务未重跑迁移roundtrip。没有扩大为全仓门禁；Task12接手集中验收。源码冻结后没有改变生产文件，也没有重复上述同版本已通过组。

## RED → GREEN及纠正记录

1. 首轮真实PG测试（同新集成文件，3项）先运行得到3 failed：仓储缺get_observability、服务构造缺now。实现窗口/去重/未知/权限后3 passed，4.13s。
2. 增加真实Run持久绑定/反序时间与 `/runs/observability` 路由优先级：先2 failed，分别为缺observation字段和路径被当Run ID返回400；实现后包含旧Run服务/仓储/router的14项作用组全部通过。
3. 新UI窗口/未知费用/精确绑定两例先2 failed（无观测区/无精确链接），再实现。扩展旧测试时4项失败来自旧mock把所有`/runs/*`都当详情或共享一个Response；测试helper改为独立完整观测响应，使旧测试仍只控制其目标详情请求，没有削弱原迟到响应断言。
4. 新403测试先失败：新增观测请求拒绝后原email_draft仍显示。加入统一revokeRead清理并失效各通道后通过。最后覆盖旧详情迟到、观测200/503迟到、失败刷新、权限拒绝与接管，共46项。
5. Gateway重放与Tavily窗口/当前状态作为额外集成覆盖。额度测试最初尝试在同一DB第二tenant创建Tavily账户，被现有单owner契约PermissionDenied拒绝（15 passed/1 failed）；修正测试为断言这个真实拒绝，再验证第二tenant读到0个本地额度记录。没有削弱生产隔离、伪造账户或修改账本。最终16 passed。
6. 静态检查曾发现Stage类型用str而非Literal，改为共享ObservationStage别名后mypy通过；Vue首次35条格式warning经ESLint修正后0；类型检查曾发现有默认值DTO字段inputs可选，改为明确可选读取后通过。无金额转float或补零。

## 真实PG、Gateway与合成边界

新集成测试使用新testcontainers PostgreSQL和真实Alembic head：canonical/窗口/时钟测试直接建立低层ORM合成行，仅验证仓储投影，不声称它们是通过域验证的市场事实。Gateway重放测试用真实持久化Gateway与原pipeline测试替身检查/handler，验证canonical调用与duplicate回执；Tavily使用真实PostgresSearchQuotaRepository和合成free usage，验证预留状态与单owner保护。这两者不替代以下完整runtime链。

完整Web证据使用新owner `c820695ce9b44dbaad2cd6099738ab2b`，原launcher命令：

`env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python scripts/run_web_core_controlled.py --directory /tmp/tradeos-task11-20260906-1612`

实际目录 `/tmp/tradeos-task11-20260906-1612/tradeos-controlled-c820695ce9b44dbaad2cd6099738ab2b`。首次传入不存在父目录导致startup_failed，尚未建立owner资源；创建0700父目录后重启成功。Web `http://127.0.0.1:49498`，API49497，scheduler49499，notification49503。

`/tmp/task11-sender.py`经实际Web登记/认证/人工确认预热目标15/绑定；`/tmp/task11-filled-setup.py`使用原公开playbook提案→独立boss审批和原受控发送历史→Gmail入站→分类与四消费者。仅确定性原ControlledConfig loader在脚本内存读取本owner配置，秘密不出内存/日志。真实PG、领域服务、Workflow、Gateway、Outbox与API；外部Provider/模型为合成。没有seed Need/Opportunity/Handoff结果。

- sender：`sid_01M1TWFJ52EKW0BVMNQRJZB8JJ`
- message：`msg_01M1TWHRJ3TH7PFSM117P58QTH`
- conversation：`con_01M1TWHRHY5QZNKA512JRP485V`
- Need：`need_01M1TWHSQR1NJCX8727VG1V8D3`
- Opportunity：`opp_01M1TWHSRRCXJKBFMPCP6G2606`
- Handoff：`hand_01M1TWHSVA7EMD5NVAZ4XGBC47`
- notification：`not_01M1TWHT9KN7SDHCBVMSTN7JMS`
- 合成回复模型调用1次仅为测试仪器计数，未映射成token或费用。

`/tmp/task11-failure-run.py`通过原WorkflowEngine.start对该已存在Handoff提交故意缺字段context，原scheduler与handler产生failed Run `run_01M1TWNZ6D7M8N08PCR4W6F7BD`，停在notify_owner、ValidationError、retry_count0。这是明确的synthetic failure，不是业务失败率样本；没有直写failed状态、伪造业务对象或修改时钟。此类多Run不改变实体计数。

`/tmp/task11-probe.py`实际HTTP获得boss200、sales403、错误tenant403；重复观测阶段不变：Signal1 / Hypothesis1 / 当前有效Need1 / Opportunity记录1 / Supply Match未知 / Quote0 / 已接受接管机会0 / Deal Outcome0；当前requested深度1。总成本与合格机会仍未知。证据 `/tmp/task11-browser-evidence/api-proof.json` 含安全typed投影；不包含原始context或秘密。这些数字只适用于本owner的合成场景。

## Browser验证

目标流程：Run精确深链→窗口与未知成本→失败Run核验绑定→原精确接管与机会组合→原Need客户证据。命令：

`env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python /tmp/task11-browser.py`

实际exit0；新Chromium context，1440×1000与390×844。十次观测均为TradeOS、目标URL、真实非空内容，overlay0、横溢false、pageerror[]、console[]。点击展开成本、精确对象链接后均检查URL与目标内容；接管页显示当前可见1项、机会ID匹配，Need页显示本次合成客户原话。没有mock HTTP响应作为这组浏览器证据。

| 场景 | 1440截图绝对路径 | 390截图绝对路径 |
| --- | --- | --- |
| 独立窗口阶段与未知资格 | `/tmp/task11-browser-evidence/run-window-1440.png` | `/tmp/task11-browser-evidence/run-window-390.png` |
| 成本缺项/当前接管展开 | `/tmp/task11-browser-evidence/run-cost-inputs-1440.png` | `/tmp/task11-browser-evidence/run-cost-inputs-390.png` |
| synthetic失败Run停留步骤/负责人/精确链接 | `/tmp/task11-browser-evidence/failed-run-binding-1440.png` | `/tmp/task11-browser-evidence/failed-run-binding-390.png` |
| 原精确接管与机会组合 | `/tmp/task11-browser-evidence/handoff-1440.png` | `/tmp/task11-browser-evidence/handoff-390.png` |
| 原精确Need客户证据 | `/tmp/task11-browser-evidence/need-link-1440.png` | `/tmp/task11-browser-evidence/need-link-390.png` |

结构化结果 `/tmp/task11-browser-evidence/browser-qa.json`。已通过view_image实看run-window-1440、run-cost-inputs-390、failed-run-binding-390、handoff-390，数字、缺项、长ID换行和主要导航可读。其他浏览器/宽度未验证。

## owned资源清理与生命周期缺口

先核验owner、supervisor PID32669及出生时间1788682324.946055，再用`/tmp/task11-stop-main.py`发送SIGTERM。原launcher最终exit0：stopped / requested_stop / cleanup_errors[]。`main-pre-cleanup.json`保留本owner原资源安全清单。

首次扩展核验发现原launcher生命周期未覆盖原scheduler入口创建、辅助脚本写入合成响应的 `reply-model.sqlite`（不是config或凭证，但含合成回复夹具）。初次结果保留 `/tmp/task11-browser-evidence/main-cleanup-initial.json`，不能声称全自动清理完成。确认owner已stopped、全部进程/容器/端口关闭且剩余文件精确为该文件后，实施者单独unlink本owner的reply-model.sqlite；未读取内容。**controller已具名核验scheduler_worker/controlled.py:52创建该文件、supervisor.py:442–447仅清mail系列，裁定Task12最小补该文件/侧文件清理与生命周期测试；本Task未修改launcher。**

最终`/tmp/task11-check-cleanup.py` exit0，`main-cleanup.json`记录9个原PID均不存在（32669、32699–32706）、2个原owner容器无残留、49497/49498/49499/49503均关闭、config与mail/reply-model SQLite及侧文件均不存在。测试截图和安全JSON保留在`/tmp/task11-browser-evidence/`供审查。

没有清理他人owner或已有9组Task10 output/playwright产物；controller拥有的progress/brief/review-context/task12 brief改动未纳入源码提交。Git AppleDouble噪声仅捕获stderr行数、不输出/修共享.git：源码commit捕获506行，stage15行、staged diff-check30行；这是局部调用计数，不宣称全程零噪声或全程累计数。

## Concerns与未验证项

1. 模型usage、人工计时、费率、合格机会五项资格与统一已确认Supply Match来源仍不足，因此核心成本北极星指标未知；这是保留事实边界的预期交付，不是0成本。
2. 阶段窗口与当前状态/当前累计尝试的交集不能解释为同cohort转换或历史期间发生量。本Task没有新增归因系统。
3. Run仍boss-only；链接仅覆盖已核验human_handoff/sourcing_case与原审批绑定，其他Run没有可信业务对象则不可定位。Opportunity工作台没有精确机会深链。
4. 本机角色演练、外部Provider/模型合成、synthetic失败；没有真实搜索、客户发送、供应商联系、真实成本、多人认证、推送、合并或部署。本owner没有完整quotation runtime，未重跑另一Linux owner的报价链，也未把其数据混入本观测。
5. 原launcher对scheduler创建的reply-model.sqlite自动清理覆盖不足，已精确手工补清；controller已裁定Task12补最小清理与生命周期测试，不要将其隐去。
6. 全仓门禁及独立审查尚待controller安排；本报告仅给出上述版本和作用组的实际证据。

## 主要截图

![窗口观测1440](/tmp/task11-browser-evidence/run-window-1440.png)
![费用缺项390](/tmp/task11-browser-evidence/run-cost-inputs-390.png)
![失败Run精确绑定390](/tmp/task11-browser-evidence/failed-run-binding-390.png)
![当前接管390](/tmp/task11-browser-evidence/handoff-390.png)
