# Task 8 实现报告

状态：实现完成，源码冻结待独立审查。基点 31922c4d266260a9016a4641ce4ca8afe04f008e。

## 具体子规格

- 保留 Campaign 的 GET `/crm/sending-identities`；增加 boss/TENANT 的 GET `/crm/sending-identities/management`，1–200 条按 ID 稳定排序，SQL 有界、tenant 过滤，覆盖 created/auth_pending/受限身份。
- POST `/crm/sending-identities` 接收登记字段与 `confirmed: true`，POST `/{identity_id}/warmup` 只接受目标整数 5–100 与 `confirmed: true`。人工确认只是本次意图，身份来自 RequestIdentity，域继续重读原权限，不接收 actor/tenant/auth/state/start date。
- 登记冻结 payload；未知时人工核对仍重放完全相同 payload，原 register 返回耐久 winner。预热成功后读取精确 sid；响应丢失只显示当前状态和目标及“原请求结果待核对”，不据相同 target 宣称本次成功。
- 既有身份中心接认证工作流、人工 binding；binding active 文案“已绑定，处理状态待核对”，无合法 scheduler 能力读取时不声称同步。原 retry 留给 Task9。
- UI 复用 client identity snapshot/subscription 与既有请求 scope；身份、路由、对象变化失效 success/error/finally，并清空受限数据与确认意图；卸载 unsubscribe。
- Smart Inbox 使用 canonical message evidence 下载和 next_questions DTO，展示最多两个真实建议；当前只读建议未保存为正式草稿、未发送，不从 queued 推断已完成；未关联邮件只读 reviews/raw，不重绑定。
- Need/机会字段依据实际 provenance 区分客户表达、人工确认、推断、来源暂不可用。Handoff/Need/Inbox/通知按精确对象深链，不猜第一项。

## 验证记录

本批测试已执行，逐轮记录见下。Browser plugin not available；已使用仓库原 Playwright + owned Supervisor，desktop 与 390px 实际截图并查看，最终填充链结果见后文。

### 实际执行记录（截至核心实现）

所有命令 workdir 为本 worktree，Python 命令均 `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1`，Python 使用 `/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python`。

| 轮次 | 命令 | exit / 实际结果 |
|---|---|---|
| RED API 1 | `python -m pytest tests/unit/test_web_identity_management.py -q` | 第一次同命令工具未保留 session ID，不作为可核验结果；随即同命令重跑 exit 1，5 failed：管理400/register405/warmup404/sales路由400/binary无content |
| RED API 2 | 同上（增加域读测试） | 1，6 failed，新增方法不存在 |
| GREEN尝试 API | `python -m pytest tests/unit/test_web_identity_management.py tests/unit/test_sending_identity_api.py -q` | 1，10 passed/1 failed：ScopeLevel漏导入，随后修复 |
| 生成 | `npm --prefix apps/web run gen:api`（PATH显式Python运行时） | 0，原exporter与openapi-typescript成功 |
| RED scopes | `npm --prefix apps/web test -- tests/core-request-generation.test.ts` | 1，6 failed：身份泄漏、旧广播、旧路由error/finally |
| 局部 GREEN | 同上 | 1，4 passed/2 failed，剩余Inbox/发件页尚未改 |
| RED evidence | `npm --prefix apps/web test -- tests/message-evidence.test.ts` | 1，缺下一问与下载动作 |
| 部分 GREEN | `npm --prefix apps/web test -- tests/message-evidence.test.ts tests/core-request-generation.test.ts` | 1，6 passed/1 failed，仅发件页未改 |
| RED register | `npm --prefix apps/web test -- tests/identity-registration.test.ts` | 1，缺登记表单 |
| GREEN core | `npm --prefix apps/web test -- tests/identity-registration.test.ts tests/core-request-generation.test.ts tests/message-evidence.test.ts` | 0，3 files / 8 passed |
| RED handoff | `npm --prefix apps/web test -- tests/handoff-queue.test.ts -t '通知精确\|来源有Provenance'` | 1，2 failed/15 skipped：深链无路由、provenance被贴已验证事实 |
| GREEN handoff | 同上 | 0，2 passed/15 skipped |
| 指挥测试校正 | `npm --prefix apps/web test -- tests/command-center.test.ts -t '身份切换后旧提案'` | 首次0但断言了未渲染的ID，无效；改为可见确认面板后再次执行1，旧提案确认面板实际残留。仅后次作为RED |

完整子规格：[Web核心操作子规格](../../../docs/superpowers/specs/2026-09-06-web-core-8.md)。

### 后续实际执行记录

以下不累加到历史总数；Python 自 owned launcher 开始改用仓库 `.venv/bin/python`（3.12），不安装依赖。

| 轮次 | 命令 / 输入 | exit / 实际结果 |
|---|---|---|
| API完整尝试 | `python -m pytest tests/unit/test_web_identity_management.py tests/unit/test_sending_identity_api.py tests/unit/test_sending_identity_service.py -q` | 首次仍有1项测试fixture manager scope不合法，修正为真实 narrowed scope 后0，155 passed |
| Core组合 | `npm --prefix apps/web test -- tests/handoff-queue.test.ts tests/command-center.test.ts tests/core-request-generation.test.ts` | 1，29 passed/1 failed：旧单详情403 fixture希望仍可选择另一已授权项；恢复只清详情、队列401/403才清队列的语义 |
| 类型 | `npm --prefix apps/web run typecheck` | 0 |
| 前端全量1 | `npm --prefix apps/web test` | 1，345 passed/3 failed：来源标题与未知徽标旧断言 |
| Lint首次 | `npm --prefix apps/web run lint` | 1，13 errors/417 warnings；globalThis和finally语法及局部格式修正 |
| 前端全量2 | `npm --prefix apps/web test` | 1，351 passed/4 failed：第三处来源标题及3项卸载harness过早取请求计数 |
| 隔离组合 | `npm --prefix apps/web test -- tests/handoff-queue.test.ts tests/core-request-generation.test.ts` | 1，27 passed/3 failed，同卸载harness问题；先flush路由挂载/卸载，再计数后结算旧Promise，无生产变更 |
| 隔离与来源 | `npm --prefix apps/web test -- tests/core-request-generation.test.ts tests/opportunity-list.test.ts` | 0，48 passed |
| 人工确认严格性RED | `.venv/bin/python -m pytest tests/unit/test_web_identity_management.py -q` | 1，6 passed/1 failed；JSON numeric 1误被Literal true转换并到达fake service；增加before validator要求`is True` |
| 权限拒绝审计RED | 同上，补manager拒绝audit断言 | 1，6 passed/1 failed；最后audit仍allow-full；补原deny audit |
| 徽标401 RED | `npm --prefix apps/web test -- tests/core-request-generation.test.ts -t '身份失效401'` | 1，1 failed/12 skipped；已有计数仍1，修复失效后未知 |
| 生成两轮 | `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 PATH="$PWD/.venv/bin:$PATH" npm --prefix apps/web run gen:api` | 两次均0；第二次前后SHA256相同，api_no_drift True |
| 全量前端3 | `npm --prefix apps/web test` | 0，30 files/356 passed |
| 预热未知回归 | `npm --prefix apps/web test -- tests/identity-registration.test.ts` | 0，2 passed；POST丢失后仅GET精确sid，目标相同仍待核对、无自动第二次POST；此项为补充回归，不称先验RED |
| 原件403 RED | `npm --prefix apps/web test -- tests/handoff-queue.test.ts -t '接管原件403'` | 1，1 failed/18 skipped；原件被拒绝后packet仍在；组件透传denied状态，清当前组合并使在途动作失效，单403保留仍获授权队列，401清队列 |
| 原件/隔离/登记组合 | `npm --prefix apps/web test -- tests/handoff-queue.test.ts tests/message-evidence.test.ts tests/identity-registration.test.ts tests/core-request-generation.test.ts` | 0，4 files/35 passed |
| 全量前端4（最终） | `npm --prefix apps/web test` | 0，30 files/358 passed |
| 构建两轮 | `npm --prefix apps/web run build` | 两轮均0，vue-tsc和Vite157 modules成功；第二轮包含最后原件denied修复 |
| Lint修复调用误路径 | `npm --prefix apps/web exec -- eslint src/components/MessageEvidenceDownload.vue src/views/crm/HandoffPacketView.vue src/views/crm/HandoffQueue.vue --fix` | 2，npm prefix不改变exec匹配cwd，无文件匹配；随后原workdir下调用`apps/web/node_modules/.bin/eslint apps/web/src/components/MessageEvidenceDownload.vue apps/web/src/views/crm/HandoffPacketView.vue apps/web/src/views/crm/HandoffQueue.vue --fix`，0 |
| 全量lint | `npm --prefix apps/web run lint` | 0，0 errors/120 warnings，保留其他既有页面格式warning |
| Ruff | `.venv/bin/python -m ruff check domains/sending_identity/service_impl.py domains/sending_identity/service.py domains/sending_identity/repository.py infra/db/repositories/sending_identities.py infra/controlled/providers.py apps/api/routers/sending_identities.py apps/api/routers/email_inbound.py tests/unit/test_web_identity_management.py tests/integration/test_web_core_launcher.py` | 最终0，All checks passed；此前一次--fix修5处import排序后0 |
| mypy | `.venv/bin/python -m mypy domains/sending_identity infra/db/repositories/sending_identities.py apps/api/routers/sending_identities.py apps/api/routers/email_inbound.py` | 两轮均0，12 source files |
| 结构首次环境失败 | `python3 scripts/check_boundaries.py` | 1，系统Python3.9无法解析既有PEP695语法，25parse错误并SyntaxError；不是伪造边界通过 |
| 结构恢复 | `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 PATH="$PWD/.venv/bin:$PATH" python3 scripts/check_boundaries.py` | 0，7组结构检查通过 |

### 受控冷启动及具名组合缺口

Browser plugin not available。使用仓库原 Playwright 和原 `scripts/run_web_core_controlled.py`；未用jsdom替代视觉检查。首次conda runtime启动 exit2、固定`dependency_missing`（缺psutil）；切到原仓库`.venv`后ready，未安装依赖。

owned owner `e95b397e57d0440f9a2393f128976267`，目录 `/var/folders/t2/6_w0ct0s04l68z693_h2fzkw0000gn/T/tradeos-controlled-e95b397e57d0440f9a2393f128976267`。原四进程、owned PG/MinIO，Web 51797/API 51796。未读取配置秘密，未用既有DSN/TEST_DATABASE_URL，未写库seed结果。

两轮真实失败保留：网页登记 `cold.example.test` 与原guide的 `tradeos-controlled.test` 后，原认证Run均failed_validation。安全详情为ToolGatewayError/failed_permanent/validation，auth仍null。旧run `run_01M1TG8WY2K10NN21FT7BKPKCK`、`run_01M1TGBY30MNBWQNQY98F85WBR` 未改写为成功。

精确原因：原DNS Connector `validate_request` 使用本地PSL，`.test`无合法suffix，在进入ControlledDnsResolver前被拒绝。Controller裁定只改受控三TXT键、当前guide示例与launcher fixture为 `tradeos-controlled.example.com`，生产Connector/本地PSL/Gateway完全未改，也未调用公网DNS。新增真实Connector+ControlledResolver测试：

- `.venv/bin/python -m pytest tests/integration/test_web_core_launcher.py -q -k controlled_dns_passes_real_connector`：RED exit1，原resolver无新域记录，TransientError。
- `.venv/bin/python -m pytest tests/integration/test_web_core_launcher.py -q -k dns`：GREEN exit0，2 passed/22 deselected。
- owned supervisor PID/born核对后HUP，重新加载新fixture；不抹旧Run。
- `.venv/bin/python /tmp/task8-browser.py`：最终exit0，Web真实登记新域地址，sid `sid_01M1TGPFFB55GX4VHQD9E6KCNZ`；认证SPF/DKIM/DMARC均通过；明确确认预热15后原状态warming、day1、剩余5；明确binding后仅显示“已绑定，处理状态待核对”。未发信、未伪造28天、未注入AuthResult。

真实浏览器脚本首两次分别在认证通过断言超时exit1，最后成功exit0；不是成功截图覆盖失败记录后声称首轮通过。`/tmp/task8-browser.py`与截图供Task13正式持久化。

### 原launcher最终作用组失败与恢复

命令 `.venv/bin/python -m pytest tests/unit/test_web_identity_management.py tests/unit/test_sending_identity_api.py tests/unit/test_sending_identity_service.py tests/integration/test_web_core_launcher.py -q`：首次 exit1，179 passed/1 failed，172.91s。失败是原 `test_migration_failure_keeps_primary_and_cleans_files_when_docker_close_fails` 90s TimeoutExpired：旧测试patch顶层`controlled_web_supervisor`，Task6 launcher实际import `scripts.controlled_web_supervisor`，不同实例使故障未注入并真实ready；不是DNS或业务链失败。

Controller批准窄修测试canonical `scripts` import，移除顶层路径alias，不改启动器生产行为、不加超时或skip。定向命令 `.venv/bin/python -m pytest tests/integration/test_web_core_launcher.py -q -k migration_failure_keeps_primary`：exit0，1 passed/23 deselected，3.20s；随后增加明确零owner容器/零processes断言并纳入最终整组。

超时产生的本轮owner `f9dffbb9478545d5a4f3ea0373196b89`：按status记录PID/born核对0活进程，通过原OwnedContainers按owner标签清两个容器，错误数组[]，再查0容器；逐名删除原Supervisor的5个临时私有文件而不读取内容。未泛kill/prune，未清他人资源。

### 视觉证据与准确覆盖

实际截图在 `/tmp/task8-browser-evidence`，desktop1440×1000、mobile390×844。脚本10个路由/视口结果均无Vite overlay、无页面水平溢出、pageerror=[]；console两条400为刻意使用无效Need ID的负例，不能声称console零错误。

已用`view_image`实际查看：新域`identity-confirm-390.png`、`warmup-confirm-390.png`、`identity-ready-desktop.png`、`identity-ready-390.png`；Inbox390、Handoff1440和390、Need390、notifications390、commands1440和390。长域名与sid换行、确认/取消和主要动作按钮未水平裁切。页面可纵向滚动，Handoff空状态不可接受。

限制：本owner Inbox/通知/Handoff为空，Need为不可见负例；真实填充的冷启动身份卡/认证/预热已验。填充业务长证据及真实Task6通知→接管的统一浏览器链尚由Task12场景验收，本批不seed结果冒充覆盖。精确深链/历史404不fallback、accept身份切换、canonical原件403清包已组件测试通过。单详情403可以保留同身份仍获授权队列；身份切换/失效以及队列401/403必须清队列和packet。

业务限制：binding.active仅证明耐久绑定；页面无合法scheduler能力入口，故文案为“已绑定，处理状态待核对”。next_questions是可审阅建议，未保存为正式草稿、未发送。Task9 retry、Task10成本、Task11指标、Task12全仓/统一场景、Task13证据持久化未扩大到本批。

### 最后增量与填充场景（后续裁定覆盖前述待验限制）

- 原180项终组第二轮：exit1，179 passed/1 failed，117.62s。canonical故障注入已生效；新增`assert not state.processes`错误地把含exit9的历史process记录当成存活。改为每个记录exit非null、PID/born对应进程已不存在或为zombie，再定向同`-k migration_failure_keeps_primary`：exit0，1 passed/23 deselected，4.53s；容器按owner标签零、配置文件不存在的断言均通过。生产代码未因此改变，不抹历史记录。
- 最终只读浏览器脚本`/tmp/task8-browser-final.py`首次exit1：选择器猜测label不匹配而超时；改用实际`.controlled-mode select`和已有选项“演练销售”，第二次exit0，精确sid仍warming/day1/target15，切身份后受限发件卡清除，pageerrors=[]。新截图`final-identity-desktop.png`、`final-identity-390.png`。
- Controller要求本批补填充Task6通知/接管/Need浏览器门，故之前“由Task12验填充”的延期不再作为完成依据。最小QA桥接获明确裁定：仅原ControlledConfig.read在harness内读取本owner，由确定性原runtime持有秘密，模型不读取内容也不输出/序列化配置、环境、原异常；不得删除借用PG/MinIO，只关自己资源。当前已认证预热精确sid公开核对后`sender_prepared=True`复用原prepare_sent后续公开前置，跳过其认证注入分支；不伪造AuthResult或开始日期。仍由原四进程推进真实消费者。

填充harness首轮 `.venv/bin/python /tmp/task8-filled-setup.py` exit1：原prepare_sent通过、公开playbook提案与独立审批完成；原入站已归档形成真实会话，但分类Run `run_01M1THYETKED9H0YXGH27GGTKN` 在classify以ControlledError失败，model_calls=1、无Opportunity/Handoff。精确原因是QA模型fixture配置了原Subject `Internal fixture`，实际原ArtifactMessageContentReader的安全模型投影subject固定`(current reply)`，fingerprint不匹配。此失败保留；只修受控外部模型响应契约，不修改原投影/Agent，使用新合成入站消息重新形成独立输入，不重试或篡改失败Run。

纠正外部模型投影key后的 `/tmp/task8-filled-resume.py` 使用同一已SENT关联，仅新增一封受控入站；真实Run `run_01M1TJ3DSCZBZZ3BX7HRXBVD2P` completed，已形成Need/Opportunity/Handoff/InApp。harness自身仍exit1，因为等待错误假设的通知kind`handoff_requested`；实际DTO是`handoff_escalation`，原relative_link已精确指向接管，不是业务链失败。随后只读 `/tmp/task8-filled-read.py` exit0，按真实通知link收集结果，无额外发送/入站：Need `need_01M1TJ3DZ9GAMNY1VAQZ9GHWM2`，Handoff `hand_01M1TJ3E612YV34GY5B0BWGC8C`，Message `msg_01M1TJ3CS71RPRSX9EHVM00RAA`，通知 `not_01M1TJ3F68KYSWTR4W92PQQBR4`，relative_link `/crm/handoffs/hand_01M1TJ3E612YV34GY5B0BWGC8C`。model_calls=2包括旧失败1，不当成重复成功次数。

### 填充浏览器门最终验收与视觉修复

`/tmp/task8-filled-browser.py`第一次exit1是断言完整body，原Handoff仅返回合法bounded客户摘录`We need hinges`；第二次exit1是Need blockquote含中文引号，exact locator无匹配；修正为真实DTO摘录与blockquote locator，第三次exit0：通知“前往处理”实际导航精确hand ID→精确Need ID，Inbox canonical message下载产生真实非空`.eml`，两视口pageerrors=[]、无水平溢出。未因断言误差修改原业务摘要。其后视觉修复两轮同脚本均exit0。

`view_image`的真实填充390截图发现Handoff包被操作卡遮住：原mobile grid缩行且overflow visible。补 `/tmp/task8-handoff-layout.py` 实际矩形RED exit1，packetBottom2075.08/statusTop669.80。只改Handoff shell剩余高度和mobile flex纵向排版，GREEN exit0，packetBottom2460.36/statusTop2471.36。继续真实查看Inbox发现mobile sticky纠正条遮挡证据，增加同脚本RED exit1 timelineBottom1623/correctionTop637.5；mobile详情按内容排版、纠正条static后GREEN exit0 timelineBottom1623/correctionTop1639。通知mobile detail覆盖其后flex规则的顺序一并修正，操作链接可滚动访问。均沿原视觉，不改全站布局框架。

填充卡还暴露“无负责人姓名”不能推断未分配：显示“负责人姓名暂不可用”；字段区改为“关键字段 / SOURCE RECORD”，不把有Provenance视为已验证事实。此为真实QA发现的精确文案修正。

增量验证：`npm --prefix apps/web test -- tests/handoff-queue.test.ts` exit0/19 passed；mobile修复后`npm --prefix apps/web test -- tests/smart-inbox.test.ts tests/notification-center.test.ts tests/handoff-queue.test.ts` exit0/29 passed。最终5个变更组件的ESLint exit0；Handoff修复后和最终Inbox/通知修复后各一次`npm --prefix apps/web run build`均exit0（vue-tsc+Vite157 modules）。此前全量358项结果对应逻辑实现；最后只改上述文案/CSS，由这些定向组件和真实浏览器覆盖，未伪称重新全量358。

`/tmp/task8-filled-sections.py` exit0，5张关键位置390截图。已逐张`view_image`检查最终 `filled-handoff-1440.png`、`filled-handoff-390.png`、`filled-handoff-ids-390.png`、`filled-handoff-evidence-390.png`、`filled-need-1440.png`、`filled-need-390.png`、`filled-need-source-390.png`、`filled-inbox-1440.png`、`filled-inbox-evidence-390.png`、`filled-notification-390.png`、`filled-notification-action-390.png`：长ID换行，原话/来源/未人工确认分离，原件按钮与通知处理链接可达，操作卡不覆盖证据。JSON证据`filled-qa.json`、`filled-ids.json`位于同目录；截图/合成MIME由Task13正式持久化，当前临时路径不当长期证据库。

最后实际接受接管：`/tmp/task8-accept-browser.py`已收到原POST 204、精确历史packet accepted、按钮disabled；脚本exit1发现深链刷新把“已接受接管”提示覆盖成通用加载完成。新增`npm --prefix apps/web test -- tests/handoff-queue.test.ts -t '精确接管深链接受后'` RED exit1/1 failed/19 skipped。窄修深链loadQueue分支在有效generation和详情成功后保留本次成功提示。`npm --prefix apps/web test -- tests/handoff-queue.test.ts tests/core-request-generation.test.ts`最终exit0，33 passed；随后`npm --prefix apps/web run build`最终exit0。未再POST已接受记录；`/tmp/task8-accepted-read.py`只读exit0，当前精确历史accepted与按钮disabled，已view_image检查`filled-accepted-390.png`，不把本轮只读显示成新操作成功。最后结构自检exit0（7组），git diff --check exit0。

## 最终版本与交付

源码提交 **96c0c01ddf42cfbea79fb10c9fa349b79be4f325**（34 files）；基点31922c4d266260a9016a4641ce4ca8afe04f008e。源码已冻结，交Controller独立review，不自行派review或推进后续任务。正式规格与ADR已在源码提交内，不依赖scratch报告。

测试版本范围：后端作用组实际179 passed/1 failed（最后失败仅新cleanup历史记录断言），修正后该项定向1 passed；未合成“180项整组全过”。前端全量曾358 passed，后续实际填充QA追加文案/CSS/深链成功提示修复分别由19项、29项和最终33项定向组件验证、真实Playwright矩形/操作与最终构建覆盖；未合成未执行的全量359。mypy12文件、Ruff、API无漂移、结构检查均真实通过。Lint保留全仓120 warning，最后触及5组件lint无警告错误。没有仓库可选Browser插件；原Playwright完成desktop/390实际验收。

本次成功填充链中的合成客户业务数据只经原前置/审批/Gateway/入站/Outbox/分类/Need/Opportunity/Handoff/InApp形成；没有真实外发、认证注入、数据库seed结果或跨租户接口。旧两条DNS失败Run与第一条分类fixture失败Run均作为失败事实保留到owner正常关闭，未篡改。

owned Supervisor按status的owner+PID/born核对发送TERM，原会话exit0，最终status=stopped/reason=requested_stop/cleanup_errors=[]。后续只读核验见`/tmp/task8-browser-evidence/cleanup-qa.json`：owned活进程0、容器0、原5个临时私有文件已清。未用泛kill/prune或清其他owner。QA harness只关自己engine/lifecycle句柄，不删除借用资源；最后由原Supervisor统一关闭。

Git命令stderr均捕获计数不打印：最终源码diff-check5148 bytes、add4862、commit86086、rev-parse0；命令exit均0。未修共享.git或改全局Git身份。报告另做文档提交，不变更源码；证据临时目录由Task13持久化。
