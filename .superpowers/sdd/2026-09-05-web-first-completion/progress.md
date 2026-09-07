# SDD ledger — plan: docs/superpowers/plans/2026-09-05-web-first-completion.md
## 授权与基线
用户于本任务确认执行全部 Web 核心计划。范围为本机受控 Web；桌面实现、服务器部署、真实发送/供应商联系不执行。
worktree: .worktrees/web-core-completion；branch: codex/web-core-completion；功能基线 1b760b2；带入计划 ec801a8。

## 预检：逐任务一致性
| Task | 测试/实现/文件一致性 | 裁定 |
| --- | --- | --- |
| 0 | 文档与安全只读检查；无业务实现 | 独立能力清单 + 聚焦基线 |
| 1 | FileSkillRouter 为新实现，现有 Protocol 与 schema 有字段差异 | 保留既有 Protocol 兼容，新增严格解析与版本规则 |
| 2 | 通用 build 与 worker build 形状不同 | 窄 adapter，不新建第三种 context；必须受信身份映射 |
| 3 | 工厂缺注入需接线；composition_support 限定报价 | 不越界扩公共容器；各进程入口独立组合 |
| 4 | 本机受控启动要求真实核心、受控外部 | 测试数据入口不能直插结果态；loopback-only |
| 5 | 新入站工具与现有 feedback 是不同责任 | 独立插件+工作流，保持旧反馈能力和幂等 |
| 6 | 回复步骤已存在，主要缺实际装配 | 不重写已验业务规则，严格 Outbound 证据 |
| 7 | 角色开放依赖完整服务层 scope | 先过滤和证据权限，再开放 UI |
| 8 | 前端消费新 DTO，异步身份状态可漂移 | generated types + generation 失效 |
| 9 | 不同页面状态共同依赖 canonical 状态 | 不新增无真实命令的恢复按钮 |
| 10 | 既有寻源报价已有验收 | 仅补连续操作缺口，不扩自动承诺 |
| 11 | 金额/工时不完整 | 未知不能作零，不实现钱包 |
| 12 | 集中 E2E 必须同版本，核心不能 mock | 受控完整链与 research_only 分开；无 skip 假绿 |
| 13 | 文档和桌面契约不等于桌面实现 | 无空壳 Tauri/IPC；多人认证独立 gate |

## 预检：共享接口/文件
| Tasks | 生产→消费 | 发现/处置 |
| --- | --- | --- |
| 0→3,4,13 | 能力清单→工厂/启动/交付 | 名称与开启条件保持一份证据来源 |
| 0→7 | 身份模式→收件箱范围 | dev-only 不作多人认证 |
| 1→2 | SkillManifest/router→context | description/evals 等字段不可静默丢失 |
| 2→3 | ContextBuilder/worker adapter→装配 | 同端口形状；禁止从 app 导回 runtime |
| 3→4,6 | SchedulerRuntimeFactory/依赖→入口/回复 | 各进程资源独立，真实 singleton lock |
| 4→5,12 | 受控资源归属→入站/E2E | 不读取生产 .env，不清理别人资源 |
| 5→6 | Artifact+ingest→InboundMessageStored | 同事务/重放/未知关联保守处理 |
| 6→7,8 | Conversation/Need/接管→权限与页面 | 客户表达仍需可信来源 |
| 7→8 | inbox DTO/scope→Vue | 不并行改相同公开契约 |
| 8→9,12 | 身份generation/页面动作→恢复/E2E | stale响应不能污染当前对象 |
| 9→10,11 | Run与恢复状态→工作台/观测 | 不把未知映射成功；保持原幂等键 |
| 10→11,12 | Quote/Case/审批状态→统计/验收 | indicative/quoted 原规则不变 |
| 11→12 | 安全指标→验收 | 去重归因，未知费用不用假数据 |
| 12→13 | 同版本报告/截图→交付 | 历史与最新证据不混用 |

## 裁定
Ruling: 当前 user 确认已授权实施上述计划与必要的隔离工作树，无需重复确认。— 用户在规划后说“可以的”。— 若对单项取舍有新指示，保留其余任务推进。
Ruling: 使用子代理执行/审查独立任务，生产修改串行。— executing-plans 在子代理可用时要求使用 subagent-driven-development。— 成本是审查开销，不引入并行写冲突。
Ruling: Baseline 先跑当前改动相关的纯逻辑/装配 tests 和结构检查，不重跑 30 分钟全仓历史验收。— 计划要求聚焦+集成里程碑全量；新版本全量在 Task 12。— 其他未知回归由后续完整门禁发现。
Ruling: Python 现有依赖环境先只读复用；需要新增 YAML parser 时创建本工作树可写 venv，不修改原 Catalog 环境。— 保护其他任务依赖和基线。— 增加少量 setup 时间。

## 任务状态
Task 0: complete — base ec801a8; initial eb147df; fix1 0b58bd9; /root/review_task0 spec+quality Approved; reports task-0-report.md/task-0-review.md
Task 1: complete — base0b58bd9; initial143983f; fix1ca7827dd; fix2 5ab99feb; /root/review_task1 spec+quality Approved, no Important; final85unit/ruff/mypy/boundaries PASS; controller同步计划checkbox与正式skill-router契约
Task 2: complete — base d6dee3f; head6eb0e14a; implementer /root/web_task2；final101unit/ruff/mypy/boundaries PASS；reviewer /root/review_task2 spec+quality Approved，无Important；task-2-review.md。controller同步正式计划审查证据。
Task 3: complete — 3a 7c640494+89d74bd2已Approved；3b初始22e2fd3+f654fc6，fix1生产85a48f94+文档da41c45；review_task3b定向复审两Important均ADDRESSED，无新增/域外观察，质量Approved。75聚焦/2旧入口/2最终断言与静态PASS，原report末尾有确切证据。控制器68fe023同步整体Task3勾选及Task8冷启动入口，不改业务。
Task 3b: minor (deferred): Git AppleDouble stderr环境噪声保留并在验收中标明安全捕获，不把stderr捕获说成底层零告警；不修改共享.git。
Task 3b: ⚠️ resolved by controller — 前批owned/borrowed与driver关闭依3a独立复审89d74bd2及本批161组相关回归，非重新执行；Task4多进程/zeroexternal与Task5/6真消费者仍为后续硬门禁，brief已明确。控制器具名核bootstrap.py:307/325/348：外部业务适配器仅各enabled分支构造，ports由caller传入；整个进程/SDK实际构造计数由Task4/12验收，不能用本批topology身份比较替代。
Task 4: complete — base68fe023→初始b818263→fix1 ed1d81601582bb663e30f64ddc62c15c25034f30；review_task4复审I1 ADDRESSED，无新增Critical/Important，质量Approved。fix源码f1632658，22launcher总跑通过后追加强停1项单跑通过（不伪称一次23），ruff/mypy/7boundaries/增量扫描通过，含anchor的owned残留0。初始Web337/fresh install/截图证据仍按原版本记录。
Task 4: fix round1/5 (1 addressed, 0 open — 首次快照前leader死亡清理窗口；commits b818263..ed1d816)，review task-4-review.md Fix1追加结论优先。
Task 4: minor (deferred): 172lint warnings/Router R0004/4处旧unit fixture scanner命中，按报告精确位置与base证据，Task12必须裁定全量非零门；不将已知噪声称通过。
Task 5a: complete — base a353ce5→初始0da81d16→fix1 46df91c37034c7a236c8a7351c5d57c232bdc76c；review_task5a两Important ADDRESSED，无新增/域外观察，spec✅质量Approved。fix源码904a2947；最终97unit、6真实PG/Gateway针对组（年份最后收紧前，后97unit覆盖）、静态/增量scan/清理PASS。初始80/59/216仍分轮不累加；报告task-5a-report.md与review追加Fix1优先。
Task 5a: fix round1/5 (2 addressed, 0 open — 文本charset准入与Date完整语法；commits 0da81d1..46df91c)，无deferred minor。
Task 5: complete — 5a Approved，5b base a55e44a5→初始f432415→Fix1 a2b7a64c4c7022effb71a470c08a1df110e2e00a；review_task5b复审I1 ADDRESSED、无新增Critical/Important，Spec✅质量Approved。初始46passed，Fix1 12passed/17deselected分版本记录；M1二进制schema交Task8消费前修。自动入站暂因缺完整reply消费者disabled，6必须实际启用。报告task-5b-report.md/review.md Fix1追加结论优先。
Task 6: complete — base21b0b770→初始aab51d0→Fix1 b3b1c9887202b13eaf78e0fda9066008f8f20a3c，Fix源码2e0afa43db54b67d90613cf005d9b3822e477b4f；same /root/review_task6限定复审I1 ADDRESSED、无新增问题，Spec✅/Task quality Approved。初始364及119分组、Fix193作用组/静态均按确切版本记录，不累加；完整report/review末尾Fix1优先。controller已读复审并以97a534796944dd891f908c163d0a1cd2f88050e1同步正式plan勾选/浏览器7/8依赖。M1/M2和早期RED归档限制保留，非全Web最终验收。
Task 6: fix round1/5 complete (1 addressed, 0 open — Outlook历史头后缀隔离；commits aab51d0..b3b1c988)，未重跑364/119，无新增Critical/Important/Minor。
Task 6: minor (deferred): M1 HTML void集合缺source/track/area/embed等，blockquote内导致引用未闭合、合法邮件保守误拒；Task12按实际HTML语义小修并补当前片段/不拼接反例，最终全分支复核。M2已知Git AppleDouble stderr噪声归最终环境记录，不维修共享.git。
Task 6: ⚠️ resolved by controller — 真实Provider/模型效果/事务邮件与多渠道不在当前授权验收内，7/8消息原件/owner/UI和12全仓有明确brief门禁；不以6受控证据替代。controller已按最终report具名公共接口更新7/8，7未开始。
Task 7: complete — BASE97a534796944dd891f908c163d0a1cd2f88050e1，源码3090ee35763b6e6f84b302d4a91715e18cb99bfc，文档HEAD1aa1d99c21339e6a4197c18f655da607bf2fa2ea；review_task7 Spec✅/Approved，无Critical/Important，M1留Task13。179passed/63.59s无skip，Ruff22/Mypy21/七boundaries/增量scan/schema exporter/generator/TS exit0，owned清理已报。控制器已读完整命令/结果及独立review，未重跑；包review-97a5347..1aa1d99.diff 192186bytes/2commits，stderr386行既有噪声。正式plan已同步完成勾选与真实接口。
Task 7: minor (deferred): M1 domains/conversations/service_impl.py:555纠正docstring仍把跨租户拒绝写ValidationError，实际统一PermissionDenied；对应test_conversations_correction.py跨租户旧docstring也同步。Task13仅文档修正，最终全分支复核。
Task 7: ⚠️ resolved by controller — 当前179/静态/schema/owned清理采完整report证据不重复执行；原transfer写路径已具名核过，双向真实PG竞争覆盖；旧权限helper未改且原CRM/通知/technical-review回归在最终组，不宣称全域审计。浏览器8/12、全仓12、真实Provider/部署/附件not_run各有明确范围，不以本批Approved替代。
Task 8: complete — BASE31922c4d266260a9016a4641ce4ca8afe04f008e，初始源码96c0c01/报告47772bd/补证fd550a3，Fix1源码85b3def23f5b15422f274fc7feeeb7521ec9a610，最终报告HEAD3ae0f1eb9c63a38a25c3c9af20ab49e7d46bf94d。same review_task8限定复审I1/I2均ADDRESSED、无新增Critical/Important，Spec✅/Approved，原M1留12。controller已读原完整report与Fix1/复审，实际查看desktop390身份及填充handoff/inbox图。真实原件下载/accept204/owned清零，后端179+修复单项1、Web358后增量33、Fix1最终28各版本分开，不合成全量。正式plan已同步行为与验收。
Task 8: fix round1/5 complete (2 addressed, 0 open — SendingIdentity与Inbox拒权后跨通道旧响应复活；commits fd550a3..3ae0f1e)，复审Approved，无新增域外问题。
Task 8: ⚠️ resolved by controller — 原register耐久winner/预热原子状态机已在preflight具名核过，7当前后端权限已Approved，本批窄API与179后端证据/生成类型支持增量，无全域重审宣称。填充浏览器与cleanup采完整report及controller已视截图，Fix1纯请求生命周期用28项作用组/最终build验证；12仍最终全量/120warnings实际归属，13正式持久化。未跑的最终180/359不补造。
Task 5b: minor M1 addressed by Task8 — 原件octet-stream binary schema及生成类型已补，初始RED无content→API组通过，独立exporter/generator0且无漂移，真实binary下载有浏览器证据，8独立Spec✅。最终全分支核保留此解决记录。
Task 9: complete — BASE91c77754e295f5eb9754bd87e4e4f920985ae2fe，初始ebb8da6，Fix1源码1810acd、report HEAD27098ce3ed637fd85df722b1a9e997fe972ef4a9；same review_task9 Spec✅/Approved，I1/I2均ADDRESSED，无新增Important。最终Web88/API10有限组分别通过，完整证据task-9-report/review；M1留12。恢复action/稳定canonical header/安全请求422的裁定均持久化，角色演练与真实/受控浏览器界限保留。
Task 10: complete — BASEeec91eaf4c4356c3ff7a8b24f137056e3de5c28a；初始8cc0419，Fix2源码0890ae99dd1fe340247024991d200eecf72efb91，report HEADa5a59d9d44d208081a53c912b22612ad78d180f9；review_task10 Spec✅/Approved，I1经两轮修复ADDRESSED，无Minor。最终68组件及静态通过；原Mac/Linux分栏证据和平台限制保留给12/13。
Task 11: complete — BASE940db7c86249aaaa85c3b18df710a804ffd1c398；源码4be8753e5b46442244e9dfe110bd4c308a367c25，report a3390b5144c32fa72fd1e7f2a9c8ed882343ef43；review_task11 Spec✅/Approved，无Critical/Important。Minor sourcing绑定覆盖及旧launcher清理缺口交12；成本/资格unknown与受控限制保留。
Task 12: complete (commits 6cdb40d..5b63744, review clean) — 实现者web_task12，审查者review_task12；SOURCE48e4465全量9318通过，I1源码7e10383主链1passed；Spec/Quality Approved，0未关闭问题。
Task 13: complete (commits aa24508..febf7e0, review clean) — 实施web_task13、审查review_task13；I1/M1/M2在round1关闭，双Approved。

## W1 实施前裁定
Ruling: Task2复审Minor适配器内挂起点取消测试暂不新增。— 新链只捕获Exception，CancelledError按语言语义传播；原Worker取消与资源清理已有行为覆盖且审查无缺陷。— 内部await取消的直接回归覆盖有限，后续接生产provider/改变异常分类时必须补对应测试；最终审查可见。
Ruling: Task1复审Minor“64层/10000项缺直接边界测试”暂不扩大测试。— 显式迭代资源计数直观，已有危险DAG/非法层级行为覆盖；大量文件fixture对T7代价高。— 该两个精确上限的回归覆盖有限，保留最终审查可见，不把minor当未完成业务功能。
Ruling: Task1 nested prompt与拒绝更深manifest同时满足，采用64层/10000项有界目录遍历，目录symlink一律拒绝，空SemVer目录拒绝；exact规则同步brief。— 固定两层扫描会遗漏错误版本，任意无限递归又违反资源界限。— 超大/软链技能资产树需先整理后再加载；当前3份canonical不受影响。
Ruling: Task 1 精确解析/版本/重载契约以 task-1-brief.md 执行裁定为准；现有 eval_refs 是逻辑引用而非逐文件存在保证。— 保留现有技能资产与安全插件扩展。— 后续仍需独立绑定/执行评估样例。
Ruling: Task 2 保留原六参数 build，受信 descriptor 锁技能版本，每任务不可变策略装配；提供真实员工公开映射和机会读取 adapter，缺租户 policy 拒绝。— 不扩员工 Playbook 权限，不把模型 dict 当授权。— 通用 worker disabled 时只宣称受控组件验收，不伪称当前九个 Agent 消费 BuiltContext。
Ruling: scheduler outreach 目前是确定性模板；reply 窄模型流程独立保留；Task 5/6 必须处理完整候选先过输入护栏、正文邮箱安全投影和下一问排队语义。— context-design-report.md 源码核查揭示。— 不能靠未使用的通用上下文组件冒称修复业务输入。

## 工程证据
- ec801a8 基线：结构 7 PASS；相关 unit 60 passed in 4.98s。
- Python 3.12.14，Node v24.15.0，Docker 可用（仅布尔检查，不回显守护配置）。
- 本工作树 .venv 已从 symlink 改为私有 venv；只读 .pth 复用 Catalog 依赖，新增 PyYAML 6.0.3 与 types-PyYAML 6.0.12.20260815 仅安装在私有环境。Task 1 声明 pyproject 依赖。
- 现有 AppleDouble invalid distribution / non-monotonic Git index 噪声保留，不修共享环境或 .git。
- 私有 apps/web/node_modules 已通过 npm ci --ignore-scripts --no-audit --no-fund 安装，270 packages，未改lock。

## 后续准备（设计不算实施完成）
- context-design-report.md：通用ContextBuilder、UserId映射、disabled消费者边界及scheduler现有模型路径。
- runtime-design-report.md：Task3/4实际工厂/资源/健康缺口；task-3-brief.md已追加裁定。必要时拆3a/3b，完成全部才能勾选Task3。
- inbound-design-report.md：Task5 typed Gateway、Raw/PG两阶段、独立cursor/receipt/review；task-5-brief.md已追加预算/关联/事务裁定。必要时拆5a/5b，不能降验收口径。
- inbox-access-design-report.md：Task7现有OwnershipLock授权、同事务metadata公共port、证据读取竞态；task-7-brief.md已追加裁定。无需owner迁移，不能仅开放Vue角色。
- 以上三个只读报告代理均已完成，不需再次派同一审查。实现仍从Task1按门禁串行。
- 控制器文档提交d6dee3f：同步用户已授权执行/真实worktree基线/Task1勾选与正式skill-router契约；不改变业务实现。
- 控制器文档提交2fb1de8e：同步Task2 6eb0e14a与101tests/独立审查，通过后开始3a；不改变业务实现。
- Task3设计发现构造次序环（schema之后尚无canonical approvals/sending/outreach），已修正brief为3a生命周期/typed注入→3b拓扑域装配，分别实现审查、整体完成才勾Task3。完整细节见runtime-design-report.md末尾。
- 控制器补充Task3公开DTO预检：qualified_categories无现成Prospecting字段，发送前必须读account有效Demand假设窄投影，不能从Campaign或industry填充；Sender IdentityView无tenant需依tenant-bound service+ID复核；细节已写task-3-brief。
- Task8/9 briefs已生成并补充具体UI缺口（SmartInbox身份/选择旧响应，Handoff锁定写目标，证据入口，Campaign/Settings错误变空，country提案幂等键）；Task10/12/13 briefs已按plan生成，执行前按最终接口补充。不需重生成覆盖裁定。
- Task3b完整brief已在task-3b-brief.md准备，配套task-3b-preflight.md列public DTO/类别/未分类回复的保守读取边界；执行前只需按3a最终report更新接口，不重做整套设计。
- Task12/13 briefs已追加A1–A10证据、owned资源、干净声明依赖环境、真实浏览器、桌面与not_run交付口径。
Ruling: Task5拆5a typed Connector/Gateway/Raw与5b耐久整页ingest/review，各自实现审查后整体勾选。— 技术读取归档与事务业务入库有清晰公共DTO边界，避免一个不可审查大改动。— 多一个门禁；task-5a-brief.md已准备，5b按5a最终接口写brief，Task4先完成才开始5a。
Ruling: Task3a可补OpenAIJsonModelClient和惰性S3/deferred transport最小aclose及ConfiguredApiDependencies两个明确owned lifecycle端口。— 实际惰性打开SDK客户端目前缺统一清理，属于3a资源归属缺陷。— 不扩大模型业务/SDK替换，不关闭caller-owned注入对象，不建通用容器；spec记录唯一归属与quotation/model/object/engine退出次序，失败保留引用与主异常，详见3a brief与代理消息。
Ruling: Task3a覆盖原acquire query/首次commit取消窗口。— session advisory lock可能已在服务端获得，尚未进入旧unlock finally，回池可能遗留锁。— 仅外扩原生命周期/必要时丢弃原物理连接，不重连到别的backend假unlock；真实pg_locks+独立backend验证，细节已追加3a brief。
- 3a实施中证据（待最终report/独立审查）：真实PG commit取消与query结果未知RED均残留1把锁，修复为丢弃原物理连接；解锁取消覆盖主异常RED也复现并修复。最终相关回归仍在跑，不能当最终通过或开始3b。
- 3a独立审查中Important疑点已复现：detach再invalidate会使SQLAlchemy fairy失去record从而可能不关闭原DBAPI，既有PG green可能依赖asyncpg GC。reviewer以保留驱动引用的零网络检查确认；另核unlock False未discard。等待完整task-3a-review.md统一派fix1，不把7c64049作为通过基线。
Ruling: Task3a fix1必须确定关闭原driver，方法可用正确公开invalidate顺序或显式原handle close/terminate，不能机械照错误detach顺序。— 独立审查两个Important确认；强引用PG检查排除GC偶然性。— 普通unlockSQL失败但原物理连接已确认关闭可视已恢复清理，无需强制失败；关闭未知无主异常则固定非零，有主异常保留原异常。只修这两点/直接引发问题并聚焦复审。
- fix1实施中：acquire_result/unlock_cancel/unlock_false强引用RED三项driver.is_closed=False；改为锁查询前捕获原driver，close(timeout=2)失败/取消则同driver terminate，再invalidate wrapper并核原handle.closed。7项聚焦GREEN，最终锁/runtime回归和静态门仍待report；无detach/cleanup重连，不提前宣称通过。
- 3b实施中：ADR0025先写；采用account级保守reply读取、Demand inferred/contacting有evidence类别，200 distinct上限超限failclosed。公开投影首轮真实PG RED→GREEN4项，尚待整批测试/审查。
Ruling: Task3b真实bootstrap必须替换旧BossAccountDiscoveryActorResolver的UserId强转EmployeeId。— 实施发现实际旧路径的错误前提，与Task2可信映射要求一致。— 当前tenant唯一active user_id映射+所需角色检查，未知/重复/停用拒绝，无同串fallback；复用本批employee reader，核仍可达旧生产调用者，不仅放未使用adapter。
- 控制器具名上游核查：customer_discovery.py:187把employee_id写acting_user_id，command_center.py:349写confirmed.decided_by_id需核EmployeeId语义，campaigns.py:523把employee_id强转Gateway UserId。已通知3b沿实际producer→consumer修一致性，真实user_id缺失拒绝，历史审批EmployeeId不改、不重写历史Run；manualsend现有按EmployeeId判权需显式契约迁移/兼容，不能悄改值使权限失效。无新认证系统。
Ruling: Task3b若需要去重，可先ADR后限定扩展composition_support四个明确纯reader模块，名单/禁止项见task-3-brief。— API默认contact/sender/reply/materials事实上unavailable，不能把演示字典当current facts；避免整段复制也不能造全系统容器。— 需要少量公共窄读契约与现有API回归，不能跳过。
Ruling: Task3b DeliveryMaterialReader为可信Gateway内部材料映射，上游原EmailSendHandler持续取得和校验canonical当前preflight/attempt；reader核对精确tenant/contact/account/email与当前sender材料，不重复读取Outreach构造资格环。— 授权与材料读取职责分离且保留原发送检查。— 若将来将reader暴露为任意调用方端口，必须另加授权，不得沿用该信任前提；子规格与行为测试固定边界。
- 控制器核对command_center修正使用confirmed.decided_by_id对应EmployeeView.user_id，不是重放请求的当前老板，保留原确认人语义。
- 3b实施中最新进度（非最终证据）：15项新增PG/配置用例通过，包含真实API不可达/phone、入组、material错account、未知入站暂停后auto恢复、类别撤销；剩HTTP Run user_id→worker与当前通知受众、既有回归/静态/OpenAPI。后续Task4接口预告已放task-4-brief，必须按3b最终report复核。
- Task10预检已补到brief：OpportunityDetail缺到成本报价的对象链接，CostingQuotes只消费quoteId需核新增opportunity路由；保留现有quote request scope/确认/hash，沿用Approval与Run既有深链query。
Ruling: Task8补发件身份冷启动的窄人工配置API/UI与管理读取，Task4注入原DNS Resolver受控端口。— 具名源码确认现有API仅list/get/authentication-check，原list只显示可发送身份；若不补Web无法从干净环境准备发送，只能靠违规预插结果态。— 增加少量域公开管理读/生成DTO/Settings表单和回归；不扩真实邮箱配置、强制active或自定义预热，保持原register/authentication/start_warmup与逐次人工确认。
- 控制器已更新正式plan Task8对应段落，暂未提交，不属于3b实现提交；3b代理已获告知。
Ruling: Task3b同时修正可达的旧research acceptance受控入口真实UserId映射与其CLI EmployeeId边界。— 替换公共reader后原入口同串强转将失效，属于当前跨调用者兼容缺陷。— 不新增认证/重写历史Run，增加必要旧受控入口与phase1真实身份fixture回归；报告单列共享容器downgrade保护失败及独立通过证据，不将多轮计数相加。
- Task8冷启动入口进一步核实：复用SendingIdentityCenter.vue及真实/crm/sending-identities前缀，Settings只链接，不复制表单；Task9新增具名额度显示修复：target_daily_volume-remaining_today不是used（预热首日误报95），按真实剩余/目标分别展示，认证失败不得勾号成功。brief与正式plan已同步。
Ruling: Task4不能以恒定“待补充输入”模型作为唯一交付并将所有可执行场景推到Task12；至少有一条显式合成TradeManager提案场景可通过正常Web/API配置和独立审批。— 原brief要求真实提案/审批操作路径，Task5/6会消费同一启动入口，空壳永远不能配置会推迟真实接线缺陷。— 增加最小明确场景响应与操作验证；不自动批准/默认生产预算，未覆盖组如实disabled并指明后续归属，Task6所需发送回复场景不拖至Task12。
- Task4正式子规格路径docs/superpowers/specs/2026-09-05-web-core-controlled-launcher.md：owner0700/config0600，run CLI三个端口可0，TERM停止/HUP同owner重启apps保留PG/MinIO/外部SQLite邮件场景。早期端口冲突/缺Node真实CLI RED→GREEN。上述受控模型范围已要求代理修正，尚非最终验收。
- Task4实施进展（未验收）：独立API/scheduler/Vite+owned PG/MinIO已真实连通；健康API沿用本次真实身份header，不新增绕过。最小合成提案走原discovery-proposals、Playbook/国家政策原审批激活；seed两老板及经理/销售/寻源/产品基础员工。当前补HUP/TIME_WAIT、TERM/worker崩溃/阶段失败/实际cleanup核验与提案HTTP证据。控制器提醒用户可见示例采用中文业务输入，不要求手写底层DTO JSON，不造通用DSL。
- Task4最新实施证据（待最终报告/审查）：中文具名研究提案经TradeManager，研究执行未装配时确认拒绝；老板甲Playbook/国家政策自批原域400、老板乙批准→独立scheduler激活。SIGKILL本次scheduler后supervisor非零child_exited且本次容器清完。新venv从pyproject+[dev]安装、不含旧Catalog路径，真实同launcher ready→TERM退出0。仍在收尾失败阶段/前端/静态门，未标Task4 complete。
- Task4自审进展：Docker close失败曾阻断私有配置删除、leader先死遗留已登记子进程，两项RED→GREEN；各启动阶段失败实际子进程通过。RouterView全局重建曾影响旧authenticated延迟测试，现限定DEV+controlled待最终回归。外部SQLite加tenant_id/每查询过滤，ControlledGmailTransport(path, *, tenant_id)供Task5消费（以最终report为准）。
- Task4最新进度：list_calls()持久记录call_id/operation/recorded_at并tenant过滤，两次send相同引用也保留两次调用，重构后仍在；未知结果注入留Task6/12。Web最终337passed、typecheck/build0；lint172warnings需报告区分新增/旧来源。全仓scan_sensitive4处base未改测试fixture形态命中，已要求安全路径/行/规则与base证据，不回显值；本批新文件扫描收口，Task12必须处理该已知非零门禁，不能遗忘或称全仓干净。
- Task4控制器已实际查看desktop/390px两张截图，受控身份/健康条与未配置页面可见；只属本批界面证据，非全链成功。Task5a brief按最终报告更新persistent transport/list_calls接口。
- Task6通知预检已补brief：原notification runtime缺email配置会拒绝，原router精确双渠道；受控入口不能只传None/打印log宣称站内已投递。Task6子规格要复用原job→claim→template→router→in_app并明确受控通道能力，生产规则不悄改，同owner进程生命周期沿Task4。
- Task4 fix1方案：小型启动包装在exec原业务前握手登记同组anchor出生身份，保留业务PID/退出码，不靠首次public快照或裸PGID；已要求TERM升级期间归属证据、anchor自身清理、握手失败非零/回收、监听FD及EOF管道不泄漏到anchor，最终真实残留未知不可报成功。仍待实现测试与复审，不当已解决。
- Task11指标brief补明GLOSSARY.md:14的五项合格条件；Opportunity/接管实体数不能直接当合格机会数，供应/利润证据缺失时资格未知。沿既有根规则，不新增模型评分或为可算指标补造业务事实。
- Task4 fix1进度：无public立即退出真实RED“unrecorded owner child survived stop”→GREEN；握手失败阻止exec并清组、API监听FD和管道EOF不被anchor保留通过。直接伴生run_once成功需process.stop关闭新anchor已RED→GREEN；代理仍在最终生命周期/静态/清理，未复审通过。
Ruling: Task5a initial_inbound_cursor(route, bootstrap_started_at, after_epoch)由composition调用，5b首次持久化后读取profile锚点，workflow不导入Gmail codec；bootstrap最多30天仅在初态核定，翻页/重启沿耐久初值。— 技术协议与事务业务层分离，避免因时间流逝让未完成bootstrap失效或漂移漏消息。— 5b需传受信初始值并实现持久复用，不能每次重造cursor。
Ruling: 完整候选超过256KiB时，在MIME/解码预算内先guard，再以text_too_large归档隔离，不裁剪后分类。— 保留完整内容安全检查与不可变原件，超预算不当缺字段。— 超大合法邮件本版需要人工核对，可能降低自动处理比例；5a正式契约/5b说明记录。
- Task5a实施中子规格/ADR0026，公开fetch(tenant,alias,cursor,page_limit)→ArchivedInboundPage含受信route、repr=False starting/next cursor与items。尚未实现验收，不能据此开始5b。
- Task5a中间证据（非最终）：首组真实PG/MinIO/Connector/Gateway5项通过，扩展57passed/2failed（HTTPfixture参数重名，非产品RED，已修）；真实PG触发器拒绝EXECUTING/完成ledger、PG表锁取消、Raw commit未知后metadata有而bytes丢失、8MiB pending、child槽隔离通过。新增产品RED HTML仍含标签/损坏JSON误归网络已修，正跑60聚焦。Provider仅内存guard_body保留完整HTML给guard、body为确定性文本，Archived均不含；5b签名不变，fingerprint须显式覆盖敏感原值，不能hash脱敏model_dump。
Ruling: Task5a增加parse_inbound_content(raw_mime)->InboundContent纯内容解析口，并由parse_inbound_message复用；允许Gmail目录单独content模块分责。— Task6只有授权Raw，不能伪造Provider labels/internalDate来借用解析，且两阶段需相同预算/HTML/双视图语义。— 增加一个窄公共DTO/函数与对应兼容测试；纯解析不代表已guard，不引入IO/身份判断/新插件框架，5a正式契约和Task6消费说明记录。
- Task5a标签拆分marker真实PG/Gateway RED→GREEN，handler检查完整subject+原HTML和subject+实际文本后再大小判定；自审Message-ID dot-atom/Date尾部垃圾/附加multipart候选亦有RED修复。最终总验收与报告尚待，不先启动5b。
- 已建立task-5b-brief.md预备版，必须5a最终报告/复审后校准再派发。具名预检：ControlledConfig无sid，SendingIdentity.register在service_impl.py:843生成实际sid，controlled scheduler当前只装send transport；5b需先收敛实际sender→单mailbox人工绑定/未绑定disabled/耐久cursor不暗换的5b→8契约，不能预seed身份结果或选first sender。尚未选具体写接口，不属于5a修改范围。
Ruling: Task5b子规格必须明确永久入站失败的阻断状态及合法原位重试契约，暂态/429遵循原ToolGatewayError受限分类，不解析错误文字、不自动跳cursor。— 5a已将协议损坏/history过期分为永久错误；若scheduler每轮无差别重试，会违背隔离和恢复要求。— 增加最小技术状态/恢复消费约定，由Task8/9接UI，不能扩成任意cursor编辑、重关联或另一个worker。
- 5b公开契约预检只读取shared入站DTO/Protocol和wrapper失败类型：ToolGatewayError来自原tool_gateway.errors、InboundError来自shared；workflows/AGENTS允许tool_gateway依赖，无需新增重复错误层。未替代5a独立review；其两Important仍由原implementer修复。
Ruling: Task5b新增POST /email-inbound/binding（仅真实identity_id）、GET /email-inbound/status和POST /email-inbound/retry（安全expected_version/等效If-Match，不收cursor）。— 新环境无sid且服务自行生成，需要明确人工绑定与恢复才能形成Web冷启动闭环。— 增加三表内最小管理状态/API和Task8/9消费；绑定走SendingIdentity窄boss授权，review/route读/retry走Conversations当前active员工事实公开端口，trusted alias/route/version/secret不由请求覆盖；不构建通用配置系统。
Ruling: 首次绑定记录真实确认员工/时间，重复同身份幂等，任何换identity/route拒绝；无行disabled，下轮scheduler生效，重启保留初始cursor与时间。retry必须当前version CAS，并且next_retry_at未到不得人工提前清期限；永久错误可原位核对，history过期仍blocked，无reset/跳最新。— 防止静默换邮箱、旧页面干扰新状态与绕过Provider等待。— 过期history本版仍需后续独立迁移方案，合法恢复范围有限，UI/文档须明示。
- web_task5b已确认当前迁移head0058，真实Outreach公开关联检查SENT/tenant/identity、Conversations原RFC冲突规则；采用补充后的权限分工，先正式5b子规格/ADR及真实RED。没有批准数据库结果种子或自动替用户绑定。
- Task5b实施中：中文规格/ADR0026补充已写，首个真实PG RED缺cursor仓储→0059三表/真实SendingIdentity.register→首绑/同身份幂等/拒换身份1passed。T7新迁移AppleDouble导致首次launcher migration_failed，经原remove_appledouble_version_sidecars只清1个迁移sidecar后通过，未动共享.git；最终报告保留此环境失败，非业务RED。整页UoW/关联/回滚尚在实施。

Ruling: Task5b“审计故障整页回滚”限定于提交前缓冲/deny sink及PG业务审计事件写入；原StandardAuditLogger独立INFO运行日志沿既有TransactionAwareAudit在commit后flush，失败固定可观测但不重做页。— 原端口不是耐久审计表，已提交事务无法因外部日志失败回滚；Message/InboundMessageStored/outbox/receipt/review仍同事务。— 独立运行日志sink故障可能缺日志，不能冒称保证送达；测试分别核PG拒绝与postcommit sink失败，正式规格/report记录区别。
- Task6具名Web消费者预检已补brief：inbox.py仅docstring宣称draft-reply、无真实路由；suggest_next_questions只读接caller缺项，create_follow_up仅enqueue。6子规格必须收敛真实Demand缺项→最多两主题→受权可读建议，后接7权限/8页面，不误把旧docstring当现成能力。
- Task7/8 brief已追加6的下一问读取衔接：新建议路由纳入同一当前owner权限矩阵和页面generation，不让原四action清单遗漏第五路径；依据6最终契约执行。
- Task5b中间事务组7passed（10.54s，未最终审查）：真实SENT→第二receipt触发器拒绝使首Message/event/receipt/review/cursor全回滚、Raw保留；双session同页一committed一replayed与旧CAS拒绝；commit未知/close错经新session耐久核对；commit前错/取消零业务效果；postcommit运行日志失败不重做。剩冲突/跨租户/等待取消及应用/scheduler/最终门禁。
- Task5b新增冲突/等待取消组5passed（10.10s，独立中间版本）：本owner外部SQLite同provider异内容、同RFC异Raw/时间、PG表锁等待取消、两mailbox同RFC并发。未替代最终批次验收/审查。
Ruling: Task5b按明确职责拆为workflow inbound.py/inbound_contracts.py/inbound_management.py、infra email_inbound_uow.py/repositories/email_inbound.py、scheduler inbound_driver.py及独立review Raw handler；HTTP权限测试另test_email_inbound_access.py复用owned fixture。— 单一页事务文件已近400行，混入管理/HTTP/调度将失去边界；均属原授权职责。— 增加模块导航成本，不增加通用框架或第二业务消费者，报告列职责供独立审查。
- Task6读取预检补brief：旧ArtifactMessageContentReader调用unbounded get后len，现有BoundedRawArtifactStore已可复用，应与本批解码/护栏修正一起迁移，不能旧get回退。
- Task5b应用层初始RED缺apps.composition_support.email_inbound；恢复真实PG RED缺mark_failure→1passed expected_version/CAS与120秒Retry-After不越过。Raw/API尚未通过，非整体验收。
Ruling: 先补ADR0026和就近AGENTS，允许composition_support新增且仅email_inbound.py作本批fetch/review Raw机械Gateway/slot/Store/wrapper构造；registry若必要仅注册具名本批manifest，四reader原禁registry不放宽。— API/scheduler需要同一70行机械构造，直接重复会漂移；已有报价代码共享模式可沿用。— 增加一个明确例外维护成本；参数由各进程typed显式提供，每调用独立对象，无env/globalcache/engine或workflow构造/循环/业务规则/app进程import，资源归原进程且中途失败对称释放；不得演变全系统容器。
- Task5b应用/Raw组2passed（7.48s，独立中间版本）：真实PG/MinIO+当前Employee公开读取+具名Gateway，未绑定disabled、sales/未知sid拒绑、真实确认/同identity幂等、安全review/完整下载；跨tenant/sales/停用后Raw拒绝。共享机械例外已写ADR/AGENTS，HTTP route RED及API/scheduler接线继续，尚无final commit。
- Task5b代理在API/scheduler接线阶段因模型capacity错误中断，非测试失败；控制器已followup原代理恢复，保留全部未提交修改与已跑证据，未重派生产或回滚。若容量持续阻断再交接另一可用模型，需先保留当前报告。
- Task5b scheduler driver字段缺失RED→1passed，阶段前后及后置Outbox前同backend确认已接；独立进程/HTTP最终组继续。发现受控reply_factory=None，无原InboundMessageStored handler，不能默认放任Outbox死信。
Ruling: Task5b自动入站driver必须以本进程真实注册原InboundMessageStored消费者为前置；受控reply_factory=None时自动处理明确disabled，仍可人工绑定/读status/review，cursor不抓取不推进。— 无handler的原Outbox会将事件dead，提前启用会丢失后续可处理性；完整reply组合属Task6。— 5b独立受控启动暂不能自动收件；本批真实Outbox+原handler+engine集成证明到Run交接，并核无消费者零fetch/推进/dead；6装完整消费者后开启并重验，不能新增假ack/暂存队列/半截classifier。
- Task5b消费者门禁已落SchedulerRuntime及inbound_body required_ports_missing健康；真实独立进程曾观察无消费者仍产review的RED，修正后跑零fetch/零cursor及同owner restart。干净0059→0058→head迁移1passed；真实监听HTTP disabled/越权绑定403/额外cursor400/真实sid200通过。剩进程组/原Outbox→Run/缺Raw下载/最终静态schema合跑与清理提交，尚未完工。
- Task5b最新独立组（未最终合跑）：provider429/503/401及真实PG backend前/后终止5passed；HTTP完整Raw/无归档409/未知404/真实对象缺失503为1passed；真实SENT→入站→原Outbox→原reply Run重投幂等1passed；mypy23生产文件0错误。独立进程此前只读测试误用outbox表名，改现存outbox_events后继续2项绑定/restart；该测试错误非业务RED。新增原健康服务/health/capabilities固定DTO区分已绑定与缺reply消费者disabled。
- Task5b同owner独立API/scheduler restart组1passed（15.40s）：确认人/起点/binding耐久、cursor=1、无reply零fetch/receipt/入站event；非空0059 downgrade拒绝且head不变。OpenAPI exporter与openapi-typescript各自exit0。原位history过期、第二项审计缓冲失败、跨tenant真实SENT review及RFC同ID换另一真实SENT关联补充组通过（最后1项9.30s）。已进入最终4个5b文件+6旧节点聚焦合跑，修改文件ruff0错；其余静态/TS/清理/report待最终，未提前验收。

Task 5b: initial review in_progress — 源码34be43465992844b9e61080bffab82eaea989308，HEAD f432415da070d4de254c9930aa1469bc9e713260；最终46passed/45.47s、ruff28/mypy23/结构/增量scan/TS/schema分别exit0。暂存检查发现新迁移行尾空格已无语义清理，cached diffcheck及迁移ruff/scan通过。工作树报告clean；owned清理由report具体证据说明。review-package a55e44a..f432415 252236bytes/2commits，Git stderr仍有已知环境噪声仅捕获未修复。fresh /root/review_task5b (gpt-6-astra high)唯一规格+质量审查中，report task-5b-review.md。整体5未勾，6未开始。
- 2026-09-06用户再次明确“请继续”，原全部Web计划授权持续，未改变scope。5b独立review_task5b仍审查f432415；原implementer已暂停写/测试，仅给6公开组合及真实fixture只读交接，已补task-6-brief。未重复派5b实现或提前开始6。
- Task6真实消费者预检补brief：reply_factory仅(core,outreach)，Core无sessions/raw，InboundComposition.raw是review-scoped而非Message读取；需6子规格收敛typed资源参数/原生命周期，不挖service私有属性、不新增engine或借review权限。已具名只读核runtime.py:973/1491、bootstrap.py:248/423与composition公开字段，未代替5b独立review。

Task 5b: fix round1/5 in_progress — review_task5b Spec❌/Needs fixes，I1提交取消叠加close错误覆盖primary，原review内存具名核验确认；原web_task5b已获原文及unit/真实PG组合故障覆盖要求，fix base f432415。未开始6。
Task 5b: minor (deferred): M1原件下载实际bytes但OpenAPI 200 content?:never；Task8消费该路由前补application/octet-stream binary schema并原流程生成，最终全分支审查保留该项核对。不混入I1定向修复。
Task 5b: ⚠️ resolved by controller — 真实Provider/完整分类/UI/全仓门禁明确分别not_run或属6/8/9/12，不能当前宣称；5b同版本exporter/generator分别exit0及TS/46tests采用已读正式报告执行证据，未重跑。控制器持有后续brief与门禁，真实Provider不在本轮授权内。
- Task5b Fix1 RED为4failed/25deselected（14.78s，exit1）：内存commit或audit flush取消+close错，以及真实PG提交前/后取消+close错；提交后DID NOT RAISE CancelledError。当前仅__aexit__维护实际primary并捕获新增异常，12项相关组在跑；M1/API/schema未改，待GREEN/静态/提交/复审。
- Task5b Fix1提交源码f3e2a942a91730c3c339dd82ecb7598744c6337a、文档HEAD a2b7a64c4c7022effb71a470c08a1df110e2e00a；GREEN12passed/17deselected（11.87s）及作用集ruff/mypy/结构/增量scan/diffcheck通过，后仅测试换行格式整理。控制器已读完整Fix1命令/输出/owned清理证据；same review_task5b正在限定复审f432415..a2b7a64（15486bytes/2commits），M1仍由8处理，无重复全套或Web/schema。
Task 5b: fix round1/5 complete (1 addressed, 0 open — 提交/审计刷新取消叠加关闭失败；commits f432415..a2b7a64)，复审Approved；M1仍为已登记非阻断后续项。控制器同步正式plan勾选整体5，随后进入6。
Ruling: Task6整链先经真实PG/原事件工作流及现有受权boss读取验证，完整浏览器原文入口归7的message-scoped授权与8页面接线验收。— 原6 Exit gate先于其明确的7/8依赖，提前补快捷入口会绕过权限设计或重复实现。— 6单批不能宣称完整浏览器原文追溯已交付；7/8与12必须补齐，最终目标不减少。本批下一问真实产出和受权可读仍必做。
Ruling: Task6采用ReplyRuntimeResources显式typed keyword传本进程sessions与bounded raw store，InboundComposition公开借用端口但拥有者不变。— 旧reply_factory只有core/outreach，不能获取正文资源；另建engine或挖私有字段会破坏3b唯一服务与生命周期。— 需最小factory/caller签名调整及回归，缺资源仍拒绝启用，不扩大全系统容器。
Ruling: Task6下一问采用真实message/evidence到Need精确映射后的只读GET与最多两主题英文建议，保持queued，不称draft/sent。— 原selector只读且没有实际draft-reply路由，最小可审阅产出足够接员工操作。— 无精确Need关联时明确不可用，不能猜最新账户Need；实际发送仍另走原审批/Gateway，7/8需接该新读取权限和展示。
Ruling: Task6专用本机controlled_in_app模式仅选择真实站内通道，高优先级值/模板/受众保留，生产默认邮件必需与双渠道保持；先ADR及就近规则写明受控例外。— 恒失败假email适配器会制造永久配置的假暂态，原router None claim还含退避可误complete，不应为受控测试扩大修router。— 本机job完成仅证明已选站内通道送达，真实邮件/多渠道完成not_run；health/docs必须清楚标email disabled，不能普通env开关静默降级生产。
Ruling: Task6在唯一canonical Demand构造时注入公开Protocol的一次性延迟verifier委托，真实Outreach形成后绑定，ready/消费前完成。— 当前catalog_products.demand构造早于Outreach，缺真实customer_evidence；重建Demand或改private会产生两套事实服务。— 多一个严格初始化状态：未绑定拒绝、重复绑定拒绝、每runtime独立；需真实证据调用及隔离回归，不扩大Catalog业务。
- Task6正式spec/ADR0027及就近规则已写，reader首组8passed（bounded-only/完整HTML护栏/预算/地址和locator拒绝），尚非最终验收。真实5b prepare_sent仅关联前置，没有Enrollment.source_hypothesis_id或Account.field_provenance，因此直接高意向被原业务正确拒绝。
Ruling: 允许Task6给prepare_sent新增可选reply_source=True真实前置分支，默认保持5b；通过原EnrollmentCreateRequest传实际Demand生成hypothesis ID，原AccountResolveRequest带员工输入Provenance，Playbook/owner按原公开配置审批归属流程。— 5b只验证关联，本来不需要完整需求/机会门槛；6需要真实可追溯业务输入。— 增加测试helper分支及最小默认路径回归，不把employee_input说成客户表达，不直插业务结果、不绕过原拒绝门槛。
Ruling: Task6安全投影不能默认因普通邮箱/URL签名就拒绝整封合法回复；真实credential marker仍完整先guard拒绝。— 原要求是模型不见locator且原话必须可靠原文逐字核验，整封拒绝会不必要损失普通业务回复。— 需要保留仅确定性核验可见的原始可靠文本与模型投影分离，绝不能在投影上把占位/拼接文本验证成原话；agent先提出实际最小参数/消费者改法及正常签名、假quote、模型无locator的覆盖，不能扩大禁用国家/城市等需求字段。
Ruling: 采用ReplyMessageContent仅内存original_subject/original_body，reader提供完整可靠源文本与固定占位安全投影；ClassifyStep只给classifier原有subject/body投影，并在record_classification前增原文逐字/占位符拒绝门，handoff摘录来自original_body。— 实际QualificationAgent只在入参body校验，handoff也取body，必须将模型输入与原始证据分开而不改classifier公开API。— 新源字段不能进state/event/log/model serialization，repr=False不单独算防护；生产reader总提供原文，旧缺字段兼容只能代表未投影可信输入，已投影丢源必须拒绝，必要显式标记/构造校验；覆盖模型捕获与持久输出。

## Task6后续实施裁定
Ruling: Task6以有界连续evidence_segments排除明确邮件历史引用，最终quote须完整落在一个当前表达片段且在完整可靠原文逐字出现；subject只分类、不作本次采购字段证据。— 具名核inbound_mime.py的HTMLText只排script/style，原blockquote/gmail_quote及plain大于号引用仍入body，会误把旧出站数量验证成新需求；删除后拼接又会制造新句子。— 只覆盖明确blockquote/gmail_quote/yahoo_quoted及plain引用/历史分隔符，不宣称所有客户端识别；主题独有字段需补问/人工确认，异常或预算超限使字段不可验证。完整body/guard_body先guard，原文仅内存、Raw保留，handoff取真实当前片段；新增共存/旧数量/跨引用拼接测试与正式spec/ADR说明。
Ruling: Task6 StructuredReplyModelPort允许strip后非空的prompt含外层空白且原样转发，保留原其它类型与长度约束及固定prompt文本。— 原QualificationAgent固定prompt末尾换行与port要求prompt等于strip不兼容，真实组合在模型前失败。— 最小契约放宽需原Agent到真实port RED/GREEN及相关eval，不重写prompt语义或绕过校验。
Ruling: Task6 notification health关闭先有界等待serve正常退出、超时再取消并回收任务，保持primary和其它owned清理。— 真实PG重启暴露原close只设should_exit后立即cancel遗留监听socket、同端口Errno48。— 增加最多5秒正常退出等待及同端口restart覆盖，必须如实说明最终清理界限；不扩全局router或Supervisor框架。
- Task6中间进展：reader7、binding2、下一问越权403及完整入站退订/自动回复/拒绝/兴趣4项已见RED/GREEN；正在原公开流程配置高意向Playbook/owner、验证真实Need/站内job和独立进程，不将分批证据当最终验收。

Ruling: Task6模型正文仅来自当前连续表达片段的安全投影，以固定不可证据标记分隔；生产subject固定当前回复占位，完整原subject/body仍先guard且留Raw。— 代理核实只限制采购quote不能防无quote的unsubscribe/refusal使用旧引用；旧Re主题也可能独立引导stop。— 放弃主题独有分类信息，需人工核对并在spec说明；无当前表达不可分类，新增当前普通回复加历史退订的真入口无抑制测试，不宣称所有历史格式识别。
Ruling: Task6 request_handoff在精确真实Need存在、确定性missing_for_sourcing非空且无Opportunity时，转原CREATE_FOLLOW_UP pending；其余无机会仍拒绝。— 原provides_specification固定extract再handoff，真实部分Need已创建但原intake因缺项返回None，继而reply_actions.py无机会ValidationError；缺项来自真实Need而非模型。— 增加窄分支及待补资料语义，Run完成仅回复处理完成，不能说已接管；followup用消息稳定且对应自身动作的幂等键。补部分Need无handoff、后续补齐真正Opportunity/Handoff及重放测试，不吞任意ValidationError。
- Task6 notification真实同端口重启已GREEN 1passed/7.08s；完整最终集成与静态尚未完成。

- Task8具名幂等预检已补brief：原register_if_address_absent返回winner并核完整登记字段，同payload返回原sid，无需因候选new_id另造账本；start_warmup仅AUTH_PENDING，提交未知后须当前精确identity状态核对，不能因重试状态拒绝称肯定未执行/重设日期。只读service_impl.py:829–902/1147–1188，未修改生产或提前派8。

Ruling: Task8将现有NotificationBadge/NotificationCenter纳入本批identity/object generation与明确未知计数；Task7保留同名require_inbox_access的notification本人语义。— 具名源码确认App的identity key只覆盖RouterView，顶栏Badge无订阅会留旧身份计数，Center读/写也缺unmount和throw回收；6现在真实产通知，成为交付消费者。— 增加两个既有组件的窄修复/deferred和真实通知深链验证，不建新页/渠道/轮询框架；未知不报零，切身份清旧计数。两brief已同步，无提前生产修改。

Ruling: Task6新增Outreach公开只读resolve_reply_source和REPLY_SOURCE_READ当前boss动作，复用完整SENT/双key/Enrollment绑定读取private helper；原feedback SYSTEM精确identity权限不放宽。— 下一问真实链被原技术feedback解析口403，Inbox公开视图无identity不能凭空构造SYSTEM scope；需要独立受权读关联。— 新增一个公开窄契约/ADR和角色租户关联回归；无独立任意message-id HTTP口，先本消息Conversations授权并核target.account一致。新read资源授权显式真实account/enrollment，7须扩真实员工owner scope到此及后续Enrollment/Demand读取，不能假boss。
- Task6真实高意向已到canonical Demand/Opportunity/owner/Handoff，部分字段已Need且无Opportunity/Handoff；当前下一问读关联和高意向Outbox永久失败仍在修复，未把handoff存在或job入队当通知送达。

- Task6下一问精确映射的部分/完整需求2案GREEN（10.54s，中间版本），原真实Need引用及最多两题断言通过。代理报告一次操作偏差：新增测试heredoc漏workdir，短暂在original创建原本不存在的tests/integration/test_reply_completion.py；核自己内容后转入任务worktree并仅移除该新建文件，未改原有文件/shared.git。已要求最终report留依据、后续exec均显式workdir，不为此追加修改原目录。通知/独立进程/最终门禁继续，未验收。

Ruling: Task6 ReplyClassificationResult新增确定性parser推导的内部rejected_candidates布尔信号，ClassifyStep在分类写入/动作前拒绝；模型JSON不得自报该标志。— 真入口非法quote反例揭示原QualificationAgent静默continue丢候选后返回空字段类别，导致分类先落库、后提取才失败，绕过本批原文门。— 保留旧独立Agent过滤语义但新workflow更严格，可能拒绝原先忽略的无效候选；只传安全布尔无原quote，具名检查其它丢候选分支一致性，补真实Agent→step零分类写及相关eval，不改prompt词表/通用框架。

Ruling: Task6 Opportunity新增公开窄get_notification_audience_target及NOTIFICATION_AUDIENCE_READ，仅SYSTEM精确单一opportunity scope，DTO只含实际受众解析需要的account/current owner字段。— 原CurrentNotificationAudience以SYSTEM调用禁止SYSTEM的Opportunity.get，真实HandoffRequested Outbox死信PermissionDenied、零jobs；不能假boss/放宽完整get或仅信旧assigned_to。— 增加公开契约/ADR和精确scope回归，canonical后续按原Employee当前ownership/active manager解析，不回退旧负责人；真实event→job→InApp投递与转移/停用/原get继续拒SYSTEM验证，不能将入队算送达。

Ruling: Task6 Employee新增get_notification_owner与NOTIFICATION_OWNER_READ，仅SYSTEM且typed notification_account_id非空精确匹配当前tenant/account，只返回当前owner ID/None。— 同一真实通知路径的第二处授权错误：原get_ownership禁止SYSTEM，原Actor只有role/scope无资源维度；Opportunity窄口修复后仍无法投递。— 增加一个默认None的窄资源字段/公开契约和拒绝回归，不放宽原OWNERSHIP_READ/其它action、不假boss；原list_active核当前owner/manager，无旧assigned_to回退，真通知/转移/停用与原口仍拒SYSTEM验证。

- Task6收口中：真实HandoffRequested已生成原job并InApp送达；原SLA另产通知导致按全urgent计数5而非4的断言失败，改source_job_id精确join，不删/停原SLA，不算产品RED。独立4进程已实际ready，capabilities list被测试误作dict正修。7个引用/占位/伪quote/当前普通回复+旧退订反例GREEN；剩同Need补齐、cursor重建、cancel、发送未知、当前owner转移/停用与窄权限回归，最终整组/eval/静态/schema尚未跑。mypy中间Optional subject/UoW Protocol cast/scripts module问题仍收敛，未报最终通过。
Ruling: Task6已有Need的后续提取先读取确切Need，重复product_category严格等于原值则保留原Provenance并从update移除，不同则拒绝；类别唯一候选不空调update，仍走原intake/确定性缺项。— 真入口续补发现模型重复类别传原update而Demand明确不允许修改类别，导致合法续补失败。— 最小组合兼容，不改Demand词表/另造Need；所有候选仍先过quote门，补同Need续补和类别变更零覆盖/误接管，安全失败待核对不冒称已排人工任务。
- Task6关键中间GREEN：owned四独立进程API/scheduler/notification/Web ready→同端口restart→TERM清理1passed/12.90s，inbound_body enabled且controlled_in_app/email disabled；发送未知与cancel同组2passed，实际send持久调用恰1次，未确认不SENT、租约后仅search恢复/幂等，cancel分类前零写重建无重复；同Need部分→后续完整并重启1passed，Need始终1、Opportunity/Handoff各1，历史数量不入模型。不同类别拒绝和最终整组/静态仍在收口，不累加成最终测试总数。
- Task6最终组合正在跑：21个unit/eval文件加真实reply_completion/controlled_notification_delivery/bounded reader/四进程restart/5b默认SENT入口，约350项时已见6失败，须结束后定位，不算通过或挂起。此前ruff51文件、mypy生产38文件、boundaries均exit0；Supervisor原包/非包混用统一，explicit-package-bases两脚本mypy0。最终报告须保留完整失败与后续修复版本。
- Task6首轮组合exit1，357passed/6failed，62.84s：4旧Outreach枚举测试漏新REPLY_SOURCE_READ；1真实回归为未listening即取消也多等5秒；1测试错把5a更早credential_marker review期待成failed Run。正保留role/resource矩阵补枚举、仅真实监听后优雅等待并覆盖启动中socket回收/迅速cancel/同端口restart、改早期拒绝断言为真实review+零Message/分类。不得为测试绕过Gateway，不把此轮称通过。
Ruling: Task6 owned health uvicorn在异常/取消路径显式shutdown并保留primary，runtime仅真实ready后软等待；核shutdown连接/任务等待预算，必要仅本health Config设显式graceful timeout。— 具名真实PG/uvicorn窗口反例：bind已完成但wait_started挂起时取消，原serve未shutdown，重新reserve端口RED port_in_use（1failed/6.04s）。— 多一条清理路径和窗口回归，不扩服务器框架；原5秒软预算不能冒充最终硬界限，shutdown不可无界抵消，保留迅速取消/同端口restart/零残任务。增量63文件scan与diffcheck已0，Git环境噪声保留。
- Task6 health最终相关组23passed/19.69s，真实窗口新增asyncio残留任务为空；已核安装uvicorn shutdown使用Config.timeout_graceful_shutdown，本health设5秒。准确界限为ready后正常软等5秒+shutdown连接/task5秒，ASGI lifespan/DB dispose未另造硬超时，不宣称总cleanup硬上限。正在最终364项同版本组合，尚未最终交付。
- Task6实施者报告最终同版本364passed/60.25s exit0，另原Opportunity权限/服务+Employee服务119passed/0.34s exit0，分组不累加；ruff53、mypy38+2scripts、boundaries、63文件增量scan、diffcheck0。report/本地提交中，尚未交exactHEAD/独立审查；控制器提醒补独立exporter/generator/TS证据，若已跑仅记录不重复。
- Task6初始交付aab51d0完整report已读，exporter/generator/TS独立exit0及owned清理、原目录自己新建文件纠正均明确；root git HEAD匹配/statusstdout空。review_task6完整审查后仅I1阻断，原implementer进入Fix1；7/8 brief已按真实next-questions DTO、Conversations分开的source/technical-review/qualify action、Outreach精确account scope及Demand现有tenant/ID读衔接，不能靠泛化helper放宽其它路径。
Ruling: Task6早期若干RED只有诚实定位描述、未保留精确命令输出，不能宣称控制器看到过；接受现有最终同版本命令/输出和实际保留的取消窗口RED作为相应行为证据，不回溯编造或重复制造历史失败。— 审查⚠️指出归档缺口，最终报告已明确边界；功能可信度仍由独立diff和现行覆盖验证，I1另走真实RED/GREEN。— 早期TDD过程的可审计性有限，最终交付必须保留该限制，后续Fix及Task7起完整记录各覆盖命令与实际结果，不能抹去失败轮。
- Task6 Fix1原源码RED exit1，6failed/9passed/25deselected，20.30s：实际PG抑制应0变1（模型no_current_need、原Agent退订override执行）、旧数量候选产生分类、闭合/自闭合历史头后的兄弟正文进入投影。仅parser新增单向history_suffix，保留完整body/HTML guard，M1不改；GREEN及当前字段真实持久证据核验中。
- Task6 Fix1定向GREEN15passed/25deselected（20.14s），旧退订零抑制/旧数量候选零分类Need，真实当前hinges入Need且quantity None，历史后缀secret仍完整guard拒绝。生产仅inbound_mime.py10行，3测试/1spec；ruff4/mypy1/结构/增量scan通过。最终解析/bounded reader/reply真实链/Agent/eval作用组合在跑，未重跑364，待report/commit和限定复审。
- Task6 Fix1最终源码2e0afa43db54b67d90613cf005d9b3822e477b4f，文档HEAD b3b1c9887202b13eaf78e0fda9066008f8f20a3c；同源码193passed/38.23s（原解析/guard/Agent/eval/全部reply真实链/bounded reader），静态scope及owned清理通过，无API/DTO/prompt变更不重跑schema/TS。控制器已读完整Fix1命令输出，gitHEAD匹配/status空；fix包aab51d0..b3b1c98 27012bytes/2commits（stderr144行已知噪声）。same review_task6限定复审I1中，M1/M2仍后续，7未开始。

Ruling: Task7采用精确OwnershipLock FOR SHARE，然后按employee_id稳定顺序FOR SHARE锁actor/current owner，锁后重验并在同一UoW追加纠正。— 控制器具名核对employees/service_impl.py transfer及infra/db/repositories/employees.py replace：原转交条件UPDATE OwnershipLock，未反向锁Employee；FOR SHARE必须覆盖非键owner/active/manager更新，KEY SHARE不足。— 增加短事务读锁争用与稳定锁序要求，需多连接证明写先提交及撤权先提交两种顺序；同session最小权限事实投影保持既有裁定，不新增通用跨域查询框架。
- Task7 ADR0028方案已反馈原web_task7：新message-scoped Gateway上下文绑定actor，next_questions所有返回分支最终复核；正在RED/实现，未验收。Task6最终复审Approved及97a5347计划同步已完成，Task7 BASE为97a534796944dd891f908c163d0a1cd2f88050e1。
- Task9具名恢复预检补brief：Sourcing路由只验证后丢弃HTTP Idempotency-Key，实际canonical依赖command.reconciliation_id；旧表单与父级每次同时换两个键，未知结果重试会冲突。Settings收到任意HTTP即清key/body，包括503；待Task9依真实终态修复。仅只读和交接，无提前生产改动。
- Task7中间证据（未最终验收）：缺actor读列表RED 1failed，manager/sales HTTP旧403 RED 2failed；真实PG/MinIO inbox_access+reply_completion作用组42passed/41.66s。三连接pg_blocking_pids确认纠正持锁使真实归属/员工UPDATE阻塞；撤锁反例3failed/7.29s，恢复后3passed/7.60s。正在原件IO中转移、旧snapshot、反向提交顺序与原回归，完整命令留task-7-report，不累加中间组。
- Task7已补repository.get_inbox显式actor、原件Gateway/HTTP及读中真实transfer拒绝；真实Need下一问扩sales/manager并在Need读取期间transfer后最终拒绝。中间95passed/3failed，失败为2旧fake签名与1Outreach旧期望矩阵，已修待终组；append-only/幂等/并发原回归已覆盖。剩撤权先持锁的反向3项、两证据fixture纠正、最终静态/schema/TS，不算最终通过。
- Task7终组一次170passed/8failed，8项集中旧correction迁移helper误把两boss映同一employee_id，正按实际身份修复；原件错误kind/mime与HTTP缺tenant header前置也已纠正。Ruff/Mypy21源/七boundaries/扫描/exporter/generator/TS各exit0，未最终验收。
Ruling: Task7用现有get_inbox精确tenant/account与当前角色/归属SQL predicate做最后单语句资源核验，保留snapshot上界；纠正仍锁后重验，不用它替代写锁。— 实施者具名发现READ COMMITTED分次principal/owner读取可能拼出不同时间点的授权事实，当前权限矩阵要求同一时点。— 增加精确资源查询成本与竞态RED/GREEN，不扩通用框架；读出后仍存在已声明不可撤回界限。最终作用组在此修正后重验。

- Task8具体方案已回：原GET /crm/sending-identities保持Campaign，新增boss有界management及人工确认register/warmup窄API调用原域；自然登记幂等、预热未知精确sid GET，不自动重启。复用原身份订阅和object/route generation，原件用7、先补5b binary schema；binding/status不扩Task9retry。controller接受并提醒确认字段不是权限来源、目标相同不证明本次预热成功、binding不推出processing enabled；新management公开契约先ADR，正在RED。
- Task8首轮RED已报告：管理路径400/登记405/预热404/角色路由与binary schema共5项，公开域management缺方法第6项；中间后端10passed/1failed为ScopeLevel漏import已修待复验。前端6项RED对应旧身份通知/发送者/会话、badge残留、markRead广播、Need路由旧error/finally；通知/Need已修，Inbox/表单/Handoff继续。ADR0063正式落地，控制器已读；要求report子规格另存正式spec、限额须引用原域约束。未提前验收。
- Task8首批前端3文件8项GREEN：身份/路由失效、canonical原件及下一问、登记确认/未知payload保留；identity页管理/认证/预热/binding/reviews已接。Handoff通知精确路径未注册和Provenance误贴已验证事实2项RED在修，剩Handoff/指挥中心scope/deep links、组件/type/lint与实际desktop390 QA。控制器提醒只读next_questions不得叫草稿已存或已发，消息简称不应进入UI/report。未累计成最终组。
- Task8 Handoff2项GREEN，指挥中心旧identity面板RED后已scope；组件组合29passed/1旧fixture因详情403清队列策略，后恢复同身份已授权queue只清详情。控制器强调queue本身401/403/身份切换仍必须清队列，不能为fixture留越权缓存。全Web首轮345passed/3旧文案断言待更新，后端155passed/TS0，13局部lint问题在修。conda缺psutil首次启动dependency_missing，改原.venv后owned四进程ready、Web127.0.0.1:51797；开始实际QA，环境失败需留证，不算最终验收。
- Task8实际浏览器暴露冷启动阻塞：Web登记→认证真实Run已创建，但dns.auth.check在受控tradeos-controlled.test/selector controlled仍failed_permanent/validation，auth null，不能称预热通过。首次误用不支持域也留失败。原web_task8具名诊断DNS Gateway/controlled resolver，不伪造AuthResult/改核心；精确归因后由controller裁定本批必要窄修，其他QA继续。
Ruling: Task8将ControlledDnsResolver三条TXT的当前受控域统一改为tradeos-controlled.example.com，并同步原launcher fixture测试和web-core-local当前使用示例，不保留.test别名；生产Connector/PSL/Gateway不变。— 真实Web认证Run失败后具名诊断：validate_request要求本地PSL suffix，tradeos-controlled.test在调用resolver前ValidationError，新example.com子域本地校验通过；原Task4只直接resolver测未覆盖组合。— 需真实Connector+受控Resolver RED/GREEN与owned HUP后Web重新登记/认证/预热，增加一项原fixture兼容变更；历史失败Run/报告保持，不公网DNS、不伪造认证。控制器已核三键及仅launcher测试/本地guide当前引用。
- Task8真实DNS组合RED1failed（新域旧resolver拒），改三键GREEN2passed；owned HUP后网页新域登记→真实SPF/DKIM/DMARC全通过→人工目标15预热day1剩余5→绑定待核对。完整Playwright脚本exit0，desktop/390无水平溢出，最终确认/就绪截图实施者已view_image；旧两failed_validation Run保留。Mypy12files0，核心隔离+机会48passed中间组，最终回归/report/commit仍收口。控制器此前已实看旧.test390确认图，仅布局/历史证据。
- Task8 controller已实际查看最终identity-ready-desktop/390，真实认证三项通过、目标15/剩余5与绑定待核对可区分；两个旧失败身份保留。截图观察旧认证失败身份仍展示预热按钮，已给Task9按最终代码/真实状态核合法禁用，未替代独立review。前端全356passed，最终后端+launcher179passed/1 timeout尚未通过；原证据403包清理RED另窄修，需最终作用组覆盖。
Ruling: Task8把原launcher故障注入测试的顶层controlled_web_supervisor别名改为实际canonical scripts.controlled_web_supervisor，main也用一致路径；不改启动器、不提高超时或跳过测试。— 真实最终组90秒超时且stdout ready，controller核test_web_core_launcher.py:535和run_web_core_controlled.py:49为两个模块实例，原migration故障根本没注入；之前6已统一生产module路径。— 增加本批旧测试兼容修正与定向GREEN/cleanup断言，不能只推给12并称本批全绿；本轮owner精确残留先清零，历史失败留证，不泛kill/prune或读config凭证。
- Task8 canonical注入测试定向1passed；原超时owner按PID/born核0活进程、owner标签2容器清至0，秘密文件逐名删未读。带cleanup断言最终180组在跑。实施者说明目前浏览器仅sender填充，其余Inbox/notification/Handoff为空，Need负例。controller指出brief真实Task6通知深链/长证据浏览器门不能全部推12：原Task6公开helper与真实入站链形成一条填充场景，验证通知→接管/Need与原件/建议互动及desktop390；不seed、不重做12全A1–A10，资源桥接缺口先具名反馈。尚未验收。
Ruling: Task8临时QA harness可对本次精确owner目录调用原ControlledConfig.read，秘密仅留确定性loader/runtime内存，不输出/模型读取；借既有PG/MinIO和同mail.sqlite，用原build_phase1_dependencies在该进程内一套canonical服务。— 真实4进程Supervisor的fixture helper需要内存config/factory/deps/provider，无法直接从浏览器调用；另造业务API或seed结果会破坏证据。— 新增窄测试桥接，先核owner，关闭仅自己的engine/句柄、不删借用容器，不改生产组合；使用网页已真实认证预热的精确sid和sender_prepared=True并原公开读核tenant/state，跳过helper默认AuthenticationResult注入。其余真实前置/审批/Gateway与4进程原消费者保持；harness命令/安全输出和限制需归档。原禁止旧/他人凭证不等于禁止本owner运行时代码正常解析其配置，模型仍不接触值。
- Task8填充场景已完成原发送前置/Playbook独立审批且真实会话入站；首分类Run为QA model fingerprint错误（fixture主题Internal fixture，真实安全投影固定current reply），只修模型响应key，保留失败Run并同一实际SENT关联新合成入站，不再发出站。180组剩余失败是新cleanup断言把历史processes要求为空，按PID/born修后定向1passed/23deselected；179组合与1定向分版本，不伪称一次180通过。
- Task8填充原reply Run completed，真实Need/Opportunity/Handoff与站内notification已生成；当前选中通知实际kind=handoff_escalation（原SLA），精确handoff路径，不冒称是初始通知类型。同实际SENT仅新增入站，无额外出站；只读收集harness exit0，model_calls2包含旧失败1次。正在实际通知→接管→Need/canonical原件与填充截图，未提前算浏览器完成。
- Task8填充浏览器链首次exit0/pageerrors空，但view_image揭示390 Handoff操作卡覆盖长证据（RED packetBottom2075/statusTop670），单页堆叠修后GREEN2460.36/2471.36；Inbox纠正栏同类覆盖窄修后timelineBottom1623/correctionTop1639。最终精确通知→handoff→Need+原件下载exit0，实施者已实看desktop/390填充证据，定向29组件/最终build/5组件lint通过。控制器也view_image实看最终filled-handoff-evidence-390与filled-inbox-evidence-390。正原accept按钮204、owned清理/report/commit，尚未独立审查。
- Task8补证report-only HEAD fd550a3c64866e34a3d303beb104e672afbe1fe0，源码仍96c0c01；34变更文件原敏感scan exit0零命中，独立exporter/generator各exit0，临时类型与仓库逐字节一致。原npm gen已有pipefail，但旧report无独立status故只补此项，不重跑pytest/Websuite。controller已读追加节并通知review_task8；首次report git add忽略规则exit1后明确路径-f文档提交，未动共享Git/source。
Ruling: Task8文件映射按行为实现验收，不为满足Modify清单机械修改client.ts/api-client-identity.test.ts/smart-inbox.test.ts；复用既有identitySnapshot/subscribeIdentity，新具名测试可承接要求，旧回归仍需实际证据。— Global Constraints末条已声明路径/接口为拟议，brief也要求复用而非重建身份store；无必要源修改或重复同义测试没有收益。— reviewer须继续核实际行为与覆盖，不能以文件名裁定通过；具体403跨通道回填风险不因此豁免，待最终findings进入修复。
- Task8 reviewer已读完整4570行diff，初步发现SendingIdentityCenter protectedFailure不失效gate，403后旧预热/复核成功可回填；SmartInbox list403不失效detailVersion使旧详情回填。未开始修源，待正式issue/行号；9仍未开始。
Task 8: fix round1/5 in_progress — review_task8 Spec❌/Needs fixes，I1 SendingIdentity受拒后旧reviews/exact/command回填；I2 Inbox列表403后旧detail/原件按钮回填。独立checkout外真实deferred反例2failed，前三次runner环境错误不算产品RED。原web_task8已收到原文与精确复现/测试范围，FIX_BASE fd550a3c64866e34a3d303beb104e672afbe1fe0；只修两组件及必要回归，不后端/全stack/全Web重跑，源码改完限定复审，9未开始。
Task 8: minor (deferred): M1当前全仓lint120warnings归属仍需Task12实际核验，不能只凭摘要称每条既有；Git stderr噪声仍仅捕获不修共享.git。最终全分支复核。
- Task8 review报告额外进行了数处具名定向读取（scope helper、changed repository的joined、changed CommandCenter按钮及临时runner配置），超过控制器要求的最小一次域外核对预算；结果未扩大为更多缺陷，I1/I2实际复现有效。后续复审严格只fixdiff/原findings，禁止再扩全页/全域检查；不以此过程偏差丢弃真实问题。
- Task8 Fix1仓库core-access-revocation两反例先2failed；最终精确9项GREEN覆盖旧reviews/exact/预热success/error、旧finally对新loading、Inbox旧详情/纠正success/error。测试动态loading定位修正后，以FIX_BASE两页临时复核9项全AssertionError RED，随后恢复窄修，未扩大源码。限定作用组/build/lint/scan/边界及report/提交待完成。
- Task8 Fix1源码85b3def23f5b15422f274fc7feeeb7521ec9a610，文档HEAD3ae0f1eb9c63a38a25c3c9af20ab49e7d46bf94d；最终5文件28passed/2.88s，build(含TS)/修改3文件lint/增量scan/7boundaries/diffcheck0，未后端/全Web/stack重跑、无CSS变化。controller已读Fix1完整命令输出，git匹配/status空；包fd550a3..3ae0f1e 26871bytes/2commits（stderr112行噪声）。same review_task8正限定复审I1/I2，M1仍12，9未开始。
- Task9已具名核最终8：Run精确深链保留/401与跨通道失效，Campaign分离列表/身份/Enrollment读失败及未知变更canonical刷新；Settings POST候选先持久再start Run，503不证明未提交，同key/payload冻结，历史不按内容猜成功；Sourcing精确uncertain-reconciliations GET核execution唯一canonical并保留reconciliation_id+header，目标消失不首选。controller接受原接口方案，暂无新API/ADR必要，正式spec/RED进行中。
- Task9首轮8/8产品RED（先修1不完整fixture后重跑，无unhandled）：Campaign入组错误空集/403旧响应、Run错误带空数据、Settings研究失败隐去/503不冻结Playbook、无认证预热可点/入站retry缺失、Sourcing新reconciliation_id。Run/Campaign/Settings初版已写，Sourcing父子canonical/入站version与期限/deferred继续；尚未GREEN或ownedQA，不提前验收。
Ruling: Task9补最小后端安全恢复action投影，复用原reconcile-uncertain-request及同一不可变canonical命令；不新增通用账本/搜索/核对事实。— controller具名核application.py:791–824只在reconciliation为空时can=true，而1229–1306先存canonical再ack quota/deliver_event，原命令支持uncertain/consumed幂等重放；只显示待核对会留下Web无法完成的既有合法恢复路径。— 先spec/ADR，增加当前权限、精确tenant/case/run/execution/canonical/原reconciled_by、active public_search与quota/必要精确has_delivered_event的安全投影成本；读取不确定不开放，POST仍最终重验，已送达不等于业务完成。补保存后ack失败/ack后event失败/重放/旧run/错actor与UI同键恢复，不能前端翻旧false。实际字段依公开契约，缺口先具名反馈。

Ruling: Task9新操作HTTP header固定由reconciliation_id派生为sourcing-reconcile-{reconciliation_id}，初次、同页与刷新恢复保持一致；legacy原随机header未持久化且路由只校验后丢弃，允许仅在后端明确resume、当前同actor且canonical命令字段完整时，从既有canonical派生稳定header续交付。— 实际耐久保证是原reconciliation_id/完整payload/reconciled_by，无法凭空恢复旧随机header，新增浏览器账本没有权威性；这是对原header字面要求的兼容裁定，不声称HTTP header持久幂等。— 保留现有header校验，不新建业务命令或核对事实；代价是legacy网络header与最初值不同，须以新操作刷新同key、legacy canonical恢复不重复事实/额度/事件的定向证明和正式spec/ADR说明约束。
- Task9最终九文件聚焦组150passed/4.93s（包含Settings并行读一支403共享失效），实际API390角色拒绝复看pageerrors空。controller实际view_image查看settings-unknown-390与sourcing-canonical-resume-390：冻结文案/原请求恢复按钮和canonical恢复/完整select在390可见。前者真实202被浏览器替换503，后者受控API响应，保持证据限制；独立review尚未开始。

- Task9中间后端作用组232passed/8.27s（sourcing plan confirmation/router/v2 contracts/service），新增组件14passed；exporter/generator各0、mypy2源0。实际Playwright20个desktop/390状态截图：Run缺失/Settings真实202已保存后仅浏览器丢响应503及同请求恢复/实际sales403用真实API，其余认证与入站409/限流/Sourcing canonical部分用受控响应，须与PG证据分栏，不冒称完整真实API。390 Sourcing select局部裁切正在最小CSS修后复看；最终组/静态/清理/report尚未完成，未验收。
- Task9源码ebb8da662c3be3e87e5451320a44238d0f0345c7，report HEADffdbb0ec980531ee2745608952e06a519b67cb5d；controller读完整report及核HEAD/status空。最终232后端/4入站各组通过；Web150在最后纯类型改动前、其后相关64通过；生成/类型/lint/build/7边界/显式18路径scan各0；owner127d051d清理0。差分包91c7775..ffdbb0e为179114bytes/2commits，Git stderr278行噪声捕获不维修。独立review_task9（Astra high）开始，10尚未开始；Settings服务内部保存后启动失败未注入的限制明确交12核查，不能以浏览器202丢响应冒充。

Task 9: fix round1/5 in_progress — 独立review_task9 Spec❌/Needs fixes；I1入站retry/status/绑定跨channel旧响应覆盖新binding/version，I2 Sourcing首次明确422仍冻结父子命令无法修正。原web_task9已收到原文与精确deferred/422→修正、503→422仍冻结回归要求，FIX_BASE ffdbb0ec980531ee2745608952e06a519b67cb5d；限定两页/必要子表单测试，完成后same reviewer只fixdiff复审。10尚未开始。
Task 9: minor (deferred): M1 web-core-state-recovery.test.ts:40标题声称拒绝后迟到研究响应，实际仅503失败文案；Task12收窄标题或补精确实际场景，不混Fix1。
- Task9 Cannot verify处置：全A1–A10/Settings内部故障已属12；真实发送、provider、多人认证明确不在本轮。既有research/Catalog预算queued/stale用本批原45组件补证，非新增实现；原POST未改变，controller前序具名核1229–1306精确Run/额度/域canonical/事件最终核验，原PG232作用组证明同命令恢复，不能升级为所有并发锁证明；12统一故障验证与最终全分支复核仍保留。临时浏览器逐条console证据不足的限制明确，13须持久化可核验实际证据。

Ruling: Task9 Fix1 I2允许最小安全422契约，只为reconcile请求模型验证失败提供脱敏ApiErrorResponse 422，真实application调用为0；业务ValidationError/运行时PydanticError仍原400，已有未知结果不得解冻。可机械提取Settings现有显式422 APIRoute至同API公共位置复用，Settings行为不变，不改全局映射/域/workflow。— 实施者发现review首次FastAPI422前提不符合实际；controller具名核middleware250/275–281合流400与settings70–93局部422、sourcing445原路由及命令模型，不能凭400判断业务未提交，也不能只用假422测试宣称修复。— 代价是Fix1新增少量API契约/共享机械处理器文件与生成类型检查，须真实HTTP坏字段422且application0、业务400仍400及原Settings422聚焦回归；避免整块复制和扩为全局错误重构。reviewer已收到更正，限定复审仍只I1/I2及fixdiff新破坏。

Ruling: 后续独立审查把controller此前“域外总共只一次”收紧改为技能原文要求的“每个具名具体风险一次定向核对”，仍禁止泛查/重复全文件读取/重复同版本测试；复审限原findings与fixdiff新破坏。— Task9 I2把默认FastAPI422当成本项目事实，实际全局400契约不同，过严总预算使必要跨层前提只能留给controller二次核实；风险逐项定向核对更符合技能且减少误报修复。— 代价是少量额外读上下文，但每次须报告风险、具体文件及结果，不允许以泛称安全/全调用点扩成全仓审查。之前已完成审查不重跑。

- Task9 Fix1中间证据：I1四个真实deferred反例RED4failed/15skipped，共享inbound generation与写期间读门禁后相关5passed；I2真实HTTP原400 RED→请求坏字段422/application0 GREEN，业务ValidationError及运行时Pydantic仍400；前端首次422可修改新命令、先503后422仍原命令冻结2passed。最终六文件Web/原Settings422与新HTTP有限组/静态仍进行中，无CSS/stack变更，未验收。

- Task9 Fix1源码1810acd2072888f8378de3dfd3ed306c60d393fd，report HEAD27098ce3ed637fd85df722b1a9e997fe972ef4a9；controller已读完整Fix1命令输出，核HEAD匹配/status空。最终六文件88passed/4.40s，真实ASGI有限10passed/52deselected/3.10s；独立schema生成、mypy3、lint/build、7boundaries及显式11路径scan通过；无CSS/stack重跑。Fix包ffdbb0e..27098ce为53639bytes/2commits（Git stderr206行噪音）。same review_task9正在限定复审I1/I2；M1留12，10尚未开始。

Ruling: Task10只依据现有具名强类型ID补Quote→Run→Approval及Opportunity→Need/精确成本单的真实链；不从ApprovalView的affected_entities字符串或proposed_change_display字典猜反向报价路由。— 实施者核quote submit已有quote_id/run_id，RunApprovalView已有approval_id，但审批投影无具名quote_id/resource route；现有契约足够补前向断链。— 代价是审批页暂不新增无可靠ID的反向捷径，正式spec保留限制；精确成本query仍须API授权和对象一致性，若后续确有必要再定义有证据的安全投影，不为拟议文件映射机械改后端。

Ruling: Task10以两套明确分栏、同源码的owned环境验证：当前Mac owner真实回复→Need/Opportunity→新链接及成本503状态；原隔离internal-network Linux owner原样完成其公开回复→Need/Opportunity→来源确认/单位/成本/独立审批/PDF。不得声称同一Need或当前Mac统一入口报价可用，不追加launcher模式/跨网络桥接/绕过Linux parser probe。— 原linux_stack固定新owner独立网络与公开前置，不支持借loopback PG/MinIO；主ControlledConfig缺报价技术配置，LinuxEvidenceTextParser在Mac固定不可用。规格允许受控核心与既有报价工作台回归，独立owner不改变原完整链真实性；仅503页面不能算A7，必须实际跑Linux整链。— 代价是统一Mac入口尚不能完整报价，正式report DONE_WITH_CONCERNS，Task12同版本验收与13能力矩阵/可运行说明必须突出此环境限制；若后续需要统一Mac运营入口，另行做有界运行环境装配，不能靠临时QA配置伪称已交付。
Ruling: Task10窄修OpportunityList全局body min-width:1080px和页面固定双列，Detail长Need链接换行。— 实际填充浏览器390在CRM出现document overflow且全局样式污染后续成本页，属于本批真实联动路径断点。— 增加两处局部布局变更及实际RED/GREEN截图/受影响组件与build，保持全站框架、已验业务/权限语义，不扩全站重构。

Ruling: Task10原Linux browser失败后的诊断只可在原测试server安全异常边界输出本仓测试相对文件名/行号或固定阶段标签；不输出异常消息、locals、配置/请求体/凭证。— 首轮1failed/53.34s无manifest，现有只有t10_fixture_error=AssertionError，不足以判断parser/probe还是其他公开前置；不能凭候选行猜原因。— 增加有限测试诊断代码及精确owner清理核验，原预算与parser资源/平台检查不变；owner d027f849c3ab434aabe309c1cee2be9b初报cleanup unknown，先核事实，失败历史保留，不提升为已清理。

Ruling: Task10修OpportunityDetail两处“provenance存在即已验证事实”及“客户确认信息”总标题，改为关键字段/来源记录，保留实际ProvenancePopover等级与证据。— 实施者具名核BASE源220/291，Task8 SOURCE RECORD实际在HandoffPacketView，controller旧brief误写为OpportunityDetail已修；不得以“不要重做Task8”阻止修本批真实暴露的断言错误。— 增加该页窄文案/RED与视觉验证，不能改变域验证事实或把所有来源降成同一证据等级；原brief前提明确更正。

Ruling: Task10仅在原quote_evidence_linux_support的quotation=True源码白名单增加email_inbound、campaign_approval_reader、delivery_material_reader、employee_readers、outreach_fact_readers五个composition_support文件，补原tar-build RED/GREEN。— 第四轮安全frames明确ModuleNotFoundError在costing_quote_case→apps.api.runtime；controller核_source_paths171–202仅含__init__/quotations，与当前api runtime16/composition21–41和scheduler的五个真实导入不符，是本计划装配变更对旧验收包的兼容缺口。— 增加有限测试打包维护成本；不泛目录、不改A-only包、真实runtime/parser/网络/预算。前三轮早期AssertionError根因仍未独立证明，不能全部倒推同因；旧失败owner核0不改写历史cleanup_unknown。

Ruling: Task10旧HistoricalEligibility.get_reply_status测试adapter改委托现有CurrentReplyStatusReader，复用canonical服务与固定NOW；不补假InboxActor、不保留list_inbox的200条扫描。— 第五轮已通过原Linux parser probe，公开前置明确TypeError；controller核costing_quote_case136缺当前actor契约，现reader136–181精确tenant/contact/account并将unknown判暂态失败，符合当前生产语义。— 增加最小fixture兼容与必要调用/错对象/unknown RED-GREEN，不复制原reader完整测试矩阵，不改权限。后续同类旧fixture仅适配已批准公共契约可继续具名报告，不逐处等待确认；若涉及生产语义、预算/probe/平台或结果seed仍先裁定。

- Task10第7轮Linux31.31s已到真实两版成本/报价/submit及新Quote→Run点击，员工按原boss-only403；测试strict locator两同文案导致失败，未全链通过。四文件组件121passed/1failed暴露初始undefined route重置currency默认值，正在修不跳过。主Mac390原1080横溢RED/GREEN及真实Need/成本链接完成；controller实看actual-opportunity-390/actual-cost-390：来源记录及精确链接完整，成本当前对象准确且如实dependency_unavailable。截图属于Mac主owner，不冒称Linux整链。

- Task10 Linux第8轮ownere157fae06cbd40a787bf5ebedeb2ebcd完整1passed/62.99s：原来源/单位/两版报价/独立审批/PDF及新Quote→精确成本ID/刷新、boss Run→Approval真实点击；员工Run403保留。worker23/forbidden0，cleanup verified。四组件122passed；Python三文件实际80passed/35.71s（原-k未匹配排除名，完整Linux integration实际跑了，待report精确范围）。controller实看最终Linux quote-390与run-approval-390；批准V2/长ID和精确审批链接可见。固定fixture结束2026/8/29与DB开始2026/9/6混合，不作真实耗时证据，11/12须留口径；尚未源冻结/独立review。

- Task10源码8cc0419c04334681ca6918a2fa9e662872d3dd6b，report44049d8后更正“15日预热”为目标15封/日（非15天），最终report-only HEAD023db808303ba24f7ef6649ae076fc97fb2e96d9。controller完整读report/更正句，4个未提交controller文档与9owner未跟踪测试产物保留，无生产源码待改。122/77Web（77在最后成本watch前，122覆盖后改）/80Python含原Linux完整integration/1Linux browser各组及静态通过，按版本分栏；主Mac owner4867dcc1d5ab4866bea2285bf1ff5f7e已清零。review-eec91ea..023db80.diff101332bytes/3commits（stderr271行），fresh review_task10 Astra high开始；11尚未开始。

Task 10: fix round1/5 in_progress — review_task10 Spec❌/Needs fixes，仅I1 CostingQuotes.loadVersions每次用cost_sheet_id query覆写新建/显式选择的版本，创建B或保存后回A；原SFC函数定向转译反例证明，未重跑既有套件。原web_task10已接原文/完整Vue创建B→保存B→仍B与精确失踪不回退回归，FIX_BASE023db808303ba24f7ef6649ae076fc97fb2e96d9；仅CostingQuotes/对应测试，不重启stack/Linux。无Minor，11未开始。
- Task10 reviewer具名核必要外部契约、实际安全cleanup/PDF hash与三张图，符合每风险一次规则。Cannot verify的全仓/完整寻源矩阵仍属12；Mac报价不可用与独立Linux公开服务前置（非Mac Gmail/UI同链）为已披露平台裁定；早1/2/6失败根因未知保留。不把122/77/80/1累加或用同源码成功倒推未知故障。

- Task10 Fix1 I1完整Vue反例初2failed/62skipped，加选择消失确认边界后3failed/62skipped，窄修同scope选择优先后3passed。覆盖A深链→POST201 B→B items204→刷新仍B、明确B重读/换missing query拒绝、B消失旧scope清除同hash不复用；最终quotation-flow/costing-quotes与静态在跑，只有1Vue/1测试，无CSS/stack/Linux重跑。controller提醒B消失不能静默回URL旧A冒充当前选择，待最终报告精确说明。

- Task10 Fix1中间提交2e8c31c后，追加B消失再次刷新明确RED（原实现回旧A），最终54a01265029015e51771af05bf2b5d26492102bb仅保留缺失B目标意图/清数据确认，报告HEAD40f751c4951fad160839a61f082af5d8f99d5a7e。controller读完整追加节，最终66组件/1.85s、build/类型/lint/7边界/2显式路径scan/diff通过；无CSS/stack/Linux重跑。URL仍A，工作台同scope明确选择B保留，完整重进则依URL，报告明确。包023db80..40f751c 20031bytes/3commits（Git stderr109行），same review_task10限定复审I1，11未开始。

Task 10: fix round1/5 (0完全addressed, 1open — I1成功/200缺失已修，但503或网络异常仍清selectedSheetId，下一次成功回URL A；commits023db80..40f751c)。same reviewer原函数503→200定向复现，无其他新增Critical/Important。
Task 10: fix round2/5 in_progress — 原web_task10收到复审原文，FIX_BASE40f751c4951fad160839a61f082af5d8f99d5a7e；仅非授权成本读取失败保留B目标意图但清数据/确认，权限/身份/机会/route撤销不放宽；完整Vue503/网络参数化RED/GREEN、两文件作用组/静态，不stack/Linux。11未开始。

- Task10 Fix2源码0890ae99dd1fe340247024991d200eecf72efb91，report HEADa5a59d9d44d208081a53c912b22612ad78d180f9；controller读完整新增报告。原完整Vue missing/503/network RED2failed/1passed，最终两文件68passed/1.90s，build/类型/lint/7边界/2路径scan/diff通过；仅非200/catch保留目标意图并清数据确认，401/403及scope重置不变。包40f751c..a5a59d9为14528bytes/2commits（stderr106行），same review_task10限定复审，无stack/Linux重跑，11未开始。

Ruling: Task11无可信模型usage/费率/人工工时来源时只给typed unknown与缺项，不新增人工成本录入/计量账本；可靠canonical实体计数可含真实零。— brief明确来源不足不得补造成本，当前StructuredJsonModelClient不提供usage，等待不等于工时；新增账本不是现阶段观测所必需。— 代价是单位合格机会成本等指标仍不可算，必须明确限制；Need记录不自动等于已验证需求，Opportunity五门槛缺证据不称合格，Run归因只现有可靠ID，不猜文本关系。

- Task11正式spec docs/design/2026-09-06-web-core-observability.md：boss-only GET /runs/observability，成对aware起止/默认服务端7天/上限31天；单SQL tenant/window快照，各阶段独立时间字段非cohort，当前requested接管队列另scope，成本unknown。Need仅当前validated/sourcing_ready/handed_to_sourcing状态，须明确非累计曾验证数量；机会不等于合格。真实PG首3 RED/GREEN，后续精确Handoff绑定/反序时钟/新API RED/GREEN，中间14passed；生成DTO/前端/owned实际浏览器待进行，未验收。

Ruling: Task11只显示已核验Opportunity ID，导航使用已存在的精确Handoff/Need路由；不生成OpportunityList尚未消费的opportunity_id伪深链，不扩改机会看板。— 实施者核当前路由无精确机会消费，已有handoff详情含真实机会组合，需求入口可定位已绑定对象。— 代价是没有单独机会深链，spec/docs明确；只有该Run持久绑定经tenant/当前对象核验时才能导航，没有可靠绑定则暂无可定位对象，不能借tenant队列任一项猜关联，目标API仍重新授权。

- Task11 controller具名核workflows/human_handoff/flow.py:434–452原RequestedHandler写入Run.subject_ref=handoff_id，145–155读取context并核一致，与正式spec持久绑定一致，无缺陷。提醒ToolCall按created_at入窗但attempt_count当前累计、quota当前状态须明确为窗口内创建记录的当前值，不能冒称历史窗口实际发生次数/消费；Need/Quote/Outcome也标observed_at当前状态快照。
Ruling: Task11允许对本轮真实Handoff通过原WorkflowEngine.start提交明确故障注入缺字段context，由原scheduler/handler生成失败审计Run，验证失败分类与精确链接。— 正常受控回复链已生成真实Need/Opportunity/Handoff各1，正常运行未自然产生所需失败场景；走真实引擎失败路径避免直接写failed状态。— 代价是该失败率/Run只是人工故障注入验收，不能作为真实业务失败统计或运营效率证据；报告和截图说明明确区分，不改时钟或业务结果。

- Task11实施者报真实受控Browser10页/交互1440×1000与390×844，pageerror/console/overlay/横溢均0；缺context Run经原engine实际failed重试0，精确Handoff/Need导航成功，boss200/sales403/错tenant403。阶段1/1/1/1、供应unknown、报价/接受接管/成交0仅合成验收数据。最终作用组后端16/Web46及build/type/mypy5/ruff/eslint/七边界过，余schema漂移/scan/diff/提交与report。owner停止exit0，9PID/2容器/4端口0；原launcher未管reply-model.sqlite在owner已停后补删，须报告初次缺项及生命周期界限，12具名评估。root尚未实看截图/最终report，尚未验收。
Ruling: Task12补原owned launcher的reply-model.sqlite及其SQLite侧文件清理，停止确认后仅按精确文件名处理，并覆盖正常/故障清理。— Task11实际补删暴露旧生命周期缺口，controller具名核scheduler_worker/controlled.py:52在配置同目录创建文件，而supervisor.py:442–447只清mail.sqlite系列，属当前真实残留而非假设风险。— 增加最小原launcher修复与有意义生命周期回归；不并入Task11观测源码、不泛删目录或读配置/SQLite，不覆盖Task11初次清理缺项历史。

- Task11源码4be8753e5b46442244e9dfe110bd4c308a367c25，报告HEAD a3390b5144c32fa72fd1e7f2a9c8ed882343ef43；controller读完整report，HEAD匹配，仅4份controller文档与9个Task10产物待变，新的12/13review-context仍ignored待controller提交。后端16/5.76s（含6PG）、Web46/1.97s、build160/413ms、静态/schema无漂移/15显式源scan通过。root实看run-cost-inputs-390及failed-run-binding-390，缺项/精确链接/长ID可读。owner c820695ce9b44dbaad2cd6099738ab2b本轮手动补清后0，初始遗漏保留。review包940db7c..a3390b5 113711bytes/2commits（stderr258行），fresh review_task11 Astra high正在独立审查；Task12未派发。

Task 11: minor (deferred): review M1 tests/integration/test_web_core_observability.py:153缺sourcing成功/版本不匹配/Opportunity.need_id不匹配三种新增分支覆盖；未发现实现错误，交Task12聚焦补证据，最终全分支review核结论。
- Task11最终review Spec✅/Approved，无Critical/Important，controller读完整报告。Cannot verify逐项：目标Need/Handoff权限已在7/8有当前作用组与竞态证据，12再次覆盖最终身份/拒绝；同版本全仓门禁原属12；launcher已具名定位并裁定12修，当前owner补清0不冒称自动清理通过；真实费用/获客/多人认证/独立Linux链不由本轮合成指标推导，无新增事实缺口。继续Task12，无生产Fix round。

- Task11正式controller验收提交6cdb40d8019d560d1490925df72a58d14f4881d6（8份doc，git commit stderr142行），Task12 BASE即此。fresh /root/web_task12 Astra high已派发，为唯一生产实施者；完整brief包括A1–A10、新依赖/最终门禁、已知最小缺口、两个平台分栏、当前权限与真实未知send调用计数；先详细证据规格，不提前13。

- Task12子规格docs/superpowers/specs/2026-09-06-web-core-final-acceptance.md已写，controller完整读并与正式design A1–A10逐项核，相符；提醒A5显式DB短暂/旧重复审批、A7实际寻源与成本分栏，不把10空Sourcing页面当整链。HTML/生命周期8failed预期RED→35passed中间GREEN；scanner4处安全核3护栏输入+1异常脱敏，显式placeholder保留marker语义后全scan0；sourcing3新PG例首轮fixture漏state_changed_at非生产缺陷待补。A1新环境/Settings内部commit故障/总E2E/全量未跑，未验收。

- Task12执行代理曾因账户usage limit中断，用户再次明确“请继续”，controller于2026-09-06 08:49 UTC恢复同一web_task12（非新任务/不赎回额度）。核HEAD仍6cdb40d8019d560d1490925df72a58d14f4881d6，Task12生产/测试与子规格待提交；新增tests/e2e/test_web_core_controlled.py、tests/integration/test_web_core_settings_recovery.py、tests/unit/test_web_core_private_cleanup.py及output/acceptance/task12/install.json已存在，但未读取或假定结果。尚无task-12-report.md、无最终验收结论，继续原剩余步骤。

Ruling: Task12补原controlled API/scheduler的research_only真实装配，复用ResearchRuntimePorts和CanonicalSchedulerBootstrap，仅替受控search/page/model外部端口、使用真实owned对象store与显式合成技术配置。— 新环境两次ready，实际Playbook/KE政策审批后研究提案仍不能确认；controller具名核infra/controlled/config.py:85–156缺Tavily组、scheduler/controlled.py:35–57未传research，bootstrap.py:175–188/363–399已有完整typed组合，原Task4只证提案/拒绝未证运行，不能满足设计A2。— 代价是本次最终验收新增此前漏接的研究集成工作与最终门禁成本；不是新launcher或真实provider启用。API可用性需与真实scheduler端口一致，合成exclusive只代表owned fixture，不能翻布尔冒称可用；原singleton/额度/Guard/tenant/公开政策审批不变，Signal/Hypothesis只经原域，research_only零Campaign/联系人/send，未知外部输入仍拒绝。补详细spec与真实RED/GREEN，新增资源沿owned关闭/清理，不noop消费事件或扩预算，13更新实际能力。

Ruling: Task12 A3必须在同一owned受控触达链通过原Gateway单Provider验证→原handler/域持久可达性→已批Campaign入组/发送，不接受独立Provider integration与prepare_sent直接record_verification(VERIFIED)拼接为整链通过。— 实施者明确旧helper只是经公开域直接记录合成VERIFIED，并未调用验证Provider；设计A3及Task12要求可达性验证/单Provider验证接缝，正是待证缺口。controller核bootstrap.py:203–210/335–359已有ContactRuntimePorts/真实AccountDiscovery组合。— 代价是本批可能新增最小controlled联系人端口装配及同链证据；优先替换直接验证前置，不无故扩整套聊天意图，不真实Hunter/多源/账号就绪伪造。API/公开获批独立触达入口可复用，若契约必需额外装配先具名方案；原Gateway/权限/额度/当前政策/suppression不变，未验证负例拒绝并记录逐次Provider调用，A2启用相关端口后仍research_only零联系人发现/入组/send。
- Task12第三轮已实际reply→Need/Opportunity/Handoff、重放与HUP四应用后send_calls1；权限测试把当前归属sales误当越权预期403但真实200，改用EmployeeService.transfer真实撤权再断言，不能称生产权限缺陷。研究接线初始unit缺模块RED→1passed，第四轮E2E进行中，未验收。

Ruling: Task12允许apps CanonicalSchedulerBootstrap新增最小typed contacts late-binding factory，沿既有reply_factory方式，在canonical core/本runtime资源创建后返回原ContactRuntimePorts；先ADR/子规格，现contacts与factory互斥，默认路径兼容。— 原ContactRuntimePorts预构造，而真实验证Gateway需core.prospecting/outreach与本runtime sessions；controller核runtime.py:1545–1609原自定义联系人路径只消费这两个端口，生产Hunter专用builder绑定真实readiness不应伪造。属于apps装配依赖接缝，非Gateway核心业务管线改动，manifest/check/handler插件保持原样。— 代价是本批扩大一次集成接口和原bootstrap/runtime聚焦回归；不得建controlled环境if后门、第二core/engine、挖private、infra导apps/tests或复制业务，必须验证同实例/互斥/未绑定failclosed。受控Gateway只替外部reader，真实tenant/permission/suppression/rate_limit/PG ledger，不用测试_Stage占位或假Hunter snapshot。ContactVerificationHandler只返回typed结果，持久record_verification归原VerifyContactsStep（修正controller前条粗略“handler/域落库”），同链验证到发送不变。

- Task12第四轮E2E原launcher真实研究产生3 Signal/3 Hypothesis，research_only Campaign/send均0（联系人零尚待新增装配后完整断言）；随后reply→Need/Opportunity/Handoff及HUP幂等。失败为Account转移后仍assigned_to当前员工的Handoff应200，测试错误预期403，改真实停用actor；不判生产权限缺陷。DB停启后原池首读500待bounded幂等只读恢复证据，不能抹掉首故障或重试未知写。ADR0065-late-bound-contact-runtime-ports.md已读，typed contacts_factory互斥/同core+Campaign/outreach/资源/唯一engine与原裁定一致；首RED缺参数TypeError，GREEN未报。

- Task12 DB恢复探测20次GET（250ms间隔）仍500，明确未通过，不能归因单次旧池抖动；实施者只捕获固定异常类别/SQLSTATE/阶段定位，不读DSN/原异常正文。controller要求先核PG ready/原端口，再看原API池，不能盲增sleep或重试未知写。contacts factory2项GREEN、受控Provider逐次验证计数测试GREEN；计数复用同owner mail.sqlite独立表，无新增生命周期文件，整链未验收。

Ruling: Task12 DB短暂不可用测试改exact-owner container.pause/unpause，保持容器/数据库/公开端点，finally恢复并核原端点ready，再测原API实例/原key/payload；不改Docker端口分配或生产连接语义。— 实际stop/start两轮内部pg_isready通过但原公开端口SELECT1和API均ConnectionRefused；controller核infra/controlled/resources.py:255随机HostPort且只create初次读取，依赖端点实际上没有恢复，不能归因API连接池。— 代价是本轮证明固定端点暂态恢复，不证明DB容器单独stop/start透明恢复；当前随机端口/配置不自动更新限制必须留report/13运维，不能只叫测试错误而抹去事实。暂停期间实际bounded取消，无遗留后台查询，finally仅owner+ID核验的原容器；原HUP只重启应用，不扩成数据库重启契约。

- Task12 pause首轮asyncio.wait_for取消仍等待暂停PG，实施者核精确owner/容器后手动unpause才结束；两测试最终exit0不算有界故障GREEN。期间误以为前session结束而另启E2E，发现后SIGINT该轮并finally清理；两个pytest已结束，恢复串行，实际失误/中断轮须保留report。controller接受独立固定2秒unpause计时器先建立、最终await收敛、finally再次核owner/未paused/原端点；1秒查询timeout不等于整体1秒，须记录实际2秒恢复后的取消总耗时/无后台任务。属于注入工具恢复保障，不修改生产取消语义或宣称新生产修复。

- Task12第9轮主受控E2E实际1passed/32.36s，owner335a86cc07fc4d7092270b1d8767d809，清理0错误/SQLite侧文件断言过；实施者实看1440 Need、390 Handoff，安全proof待最终生成核contact Run/point、verification provider/status、enrich/verify逐次计数与未验证拒绝。第7轮类别空格公开契约拒绝，第8轮未验证真实ContactNotEligibleError但测试误接ValidationError，均fixture前提已纠正，不是生产缺陷。Settings pause中间2passed/10.68s，计时新断言待最终作用组。原Linux报价Browser/已批PDF不发+Need寻源3目标串行在跑；全Web独立可并行。boundaries/scan/mypy562文件通过，Ruff本批两格式修后0，未冻结或全量通过。

- controller已读第9轮335a86cc... proof.json/cleanup.json，真实3研究信号/3假设、send1、reply model1、handoff accepted及stopped/cleanup_errors[]，proof尚无后补contact细项，不倒写；实看need-1440/handoff-390，客户字段来源与原件入口、当前队列/长ID可读。提醒OpportunityList不消费opportunity_id query，proof含该query仅到看板，不算精确机会深链；需原列表精确ID打开详情/核Need或降为看板展示，勿扩新query功能。need-sales-denied截图名实际URL为handoff，最终名称/断言按实际页纠正。A1新Python3.12.14隔离且无Catalog依赖探针通过；Web五门禁exit0（精确计数待report），A7仍未终态。

- Task12 A7原Linux报价Browser+已批PDF不发送/不成交+原Need寻源三目标3passed/131.60s；Linux Browser ownera79870b4f3714cd79d6211d061c5ab64与integration owner8ad147dd94c84955aa1ba9de772dff88 lifecycle code0/cleanup_verified=true，实施者实看quote390/exact-cost1440。正式acceptance草稿已在docs/acceptance/2026-09-05-web-core-completion.md，仍明确待全量；机会页仅看板/撤销实际Handoff已修正口径。后端全量约2%已有多F，继续整轮汇总，未判同因/未通过。后续只按实际新改动影响复验，不将历史局部数字累加成全仓绿。

- Task12 fresh Node npm ci28.71s/build5.05s通过；首轮npm拒绝user/global同指/dev/null，改两个独立空config后通过，历史失败保留。后端全量先6%约684s后10%约787s仍推进，只有单一DB pytest；短暂无输出不能由最近fixture推断阻塞，未新增PG诊断并发任务。尚无最终汇总/验收。

- Task12完整后端已终态exit1：98failed/9217passed，pytest2076.35s、wrapper2083.9s，session14628结束，24源/测试全程冻结。output/acceptance/task12/backend-failure-index.json初提75项漏23含括号/长标题，正在补完整98安全索引；尚未分类为同因。controller要求修前保留精确源码checkpoint或可复原清单/patch，不把本轮改写为最终源码全过；迁移需区分旧head门禁与特定历史往返，不全局替换revision。新主E2E无失败名仅初步信息，最终proof/cleanup尚待核。开始按实际失败类别修复，无并发DB命令。

- Task12全量失败源码checkpoint 1cbcdcf3d1bbd5ed088b87fff5329c263f6b731d已保存（24源/测试+ADR0065+子规格+明确98失败验收草稿，未混controller/report/log）；24文件SHA256与冻结快照一致，commit stderr518行。完整安全索引98项已补齐。已核2旧head预期0058而实际0059、current_outreach纠正缺actor、15quotation生命周期SimpleNamespace缺email_inbound、4身份权限enum缺INBOUND_BIND；controller允许最小旧fixture兼容，不在生产加getattr默认或optional actor迁就。特定历史迁移往返仍需准确错误/路径诊断，不能批改revision或预期。

- controller核1cbcdcf全量中的主E2E owner39ea7630eb12472eb7b0686ccf3cd52a proof/cleanup：研究3/3，contact.enrich/verify各1，Run run_01M1V0YMQ9PMFM3REMRF9S60JS、point cp_01M1V0YNQGKDF1WQKGT0MXX0G8、controlled-single-provider持久verified、未验证入组拒绝true、send1、Handoff接受true/pageerrors[]，stopped/errors[]；不是第9轮倒写。root实看handoff-accepted-390原件入口/操作区，视口未露上方接受状态，不能靠图名判断，接受由真实断言/proof。提醒model_calls1仅ControlledReplyModelClient计数，不是研究/联系人/TradeManager整条总模型调用，最终口径要注明或保持总量未知。
- 定向employees0002→head往返已passed，不能由全量失败倒推生产迁移缺陷；Catalog同组poll_due2期待3尚未归因，不机械改预期。quotation fixture补email_inbound后又暴露缺model_lifecycle，按真实完整契约补，生产不加宽默认。旧无政策研究confirm200需真实执行阶段政策拒绝和零副作用证据，而非只改旧disabled预期。

Ruling: Task12 Catalog policy workflow测试每个独立场景在_start后从原get_run持久created_at建立时钟基线，不改engine/审批七日期限或精确匹配，后续advance不能被回拨抹掉。— controller核workflow_engine.py:304–320由PG默认created_at，policy_steps.py:85–88/317–319用created_at+7d，mapping.py要求fact.expires_at与expires_at_limit精确一致；原fixture NOW固定2026-09-05而PG已09-06，Approval min(fixture now+7d,limit)造成真实ValidationError。— 代价是测试基线依赖本轮持久创建时刻，需保留后续超时/重放语义及原processed==3/唯一审批断言，不能机械改2或降低审批边界；这次定向失败不是迁移损坏。

- Task12迁移受影响完整52项52passed/70.96s（session26678结束），仅当前head常量/两断言修为0059，全部特定旧revision往返原样；全量downgrade失败共享前置根因仍未充分归因，不倒推全由head。两全量失败Linux owner5576a5f6fb57458896f3756f093d7424和2ce63f77eb394cf59cba2b39d03fdda8安全核验matching PID+birth0、各owner容器0/网络0/四端口关闭，未删除，linux-failed-owner-audit.json留证；历史cleanup_verified=false不改。Catalog6passed/10.84s、回复无ORDER BY状态列表按精确Run核，slice4两503待具名依赖分析。

Ruling: Task12将98项失败逐项映射到修复/作用组后，在最终提交且冻结版本再跑一次完整后端门禁。— 首轮全量迁移downgrade共享前置仍未充分归因，两Linux浏览器全量失败而独立组通过，实际组合/顺序依赖风险未解除；设计要求同版本完整验收，单独作用组不能证明这类风险消失。— 代价是再投入一轮约此前35分钟量级的验证时间（不作耗时保证），不是无新疑点机械重复；先收完当前已确认缺口、清自有资源，源码冻结/单DB进程/原预算不变，保留98failed/9217passed历史，绝不累加局部通过伪装全量绿色。下一全量仍失败须按具体事实处理，不能因成本已花就标完成。

- Task12旧回复作用组47passed/45.10s：3旧scheduler/闭环fixture缺新Raw bounded_transport，生产reader正确failclosed；补真实S3Bounded或内存外部blob同预算协议，未改reader。unit兼容130passed/9.58s。原launcher无有效政策研究真实failed，search/page/research-model增量0，旧测试不再只沿未装配假设。
Ruling: Task12 ControlledGmailTransport._read_calls精确检查sqlite_master中provider_calls表是否存在；同owner研究账本先创建mail.sqlite而Gmail尚未初始化/调用时返回空tuple，其他DB错误原样失败。— 新无政策/有政策主链计数E2E真实OperationalError，旧逻辑误将文件存在等同Gmail表存在；controller已核_read_calls当前窄改，没有宽catch。— 代价是新增共享受控文件中独立账本的初始化语义，须unit证明只有研究表→Gmail0、实际Gmail调用后精确1、坏列/损坏DB仍报错，保留tenant查询/非法operation拒绝；不能把计数异常泛吞为零。最终冻结版本及全量门禁覆盖此变更，无真实Provider/业务规则改变。

- Task12最新主E2E与clean migration两项passed/43.20s，旧driver一fixture状态名误推unbound而实际disabled待纠正；新主E2E先无active政策实际failed且research search/page/model0，再独立审批后3/3及联系人/回复链成功。Linux旧failure-boss图为成本只读连接失败，不是机会query精确定位问题；8槽relay饱和仅候选，controller允许本owner固定阶段/槽占用/拒绝计数安全诊断，不记录请求原文/headers/敏感URL参数，不扩槽/超时或盲重试。只有证实机械reload发生在本页请求未收敛时才等真实完成条件；真实就绪后仍失败继续根因，不能用等待掩盖。


### Task12 裁定：旧 slice4 精确失败契约与 Linux 首循环就绪

Ruling：root 已读取 slice4-http-status.json，原 sending fixture 未装配 inbound_mailbox；允许仅匹配 /email-inbound/status 503 两次与原 attempts/prepare 409 一次的精确 route/status 多重集，并要求页面真实展示入站状态读取失败，额外错误仍失败。理由：新页面读取未配置能力时的 503 是诚实响应；成本：此旧场景仍不证明入站可用，由新完整受控链单独验收，不扩大通用错误白名单。

Ruling：root 核 tests/integration/costing_quote_case.py runtime_case 及 apps/scheduler_worker/main.py 的循环顺序；公开 wait 回调在真实 _run_cycle 完成且 cycles 增加后调用。允许 fixture 通过首回调事件，在原启动 deadline 内等首完整循环完成再公开 case；保留原 interval/stop、singleton、parser、退出 cycles>0，并在 worker 提前结束或异常时及时失败。理由：短 probe 可能在激活完成前结束，STARTED/0 不能证明 worker 已执行；成本：fixture 就绪语义更严格，不代表生产 readiness 新承诺。未证实 relay 饱和，不加入容量放宽、固定 sleep 或盲重试。


### Task12 裁定：迁移组合失败的真实前置数据污染

worker 具名组合 test_current_outreach_facts 全文件后 employees downgrade0002 得到 1 failed/17 passed，0052 明确拒绝删除 sourcing admission base evidence；安全证据 migration-predecessor-safe.json。Ruling：允许最小修复该测试 fixture 对其明确拥有的租户数据与共享引擎生命周期的收口，禁止清空共享库/全租户或放松生产 0052 保护。若需新隔离库须先明确边界、复用原 owner 生命周期。理由：这是此前独立迁移全过但组合失败的具体前置污染证据，不可归因于 head0059 预期；成本：再跑该执行次序组合及最终完整 suite，保留首次 full 失败与组合诊断历史。


Ruling（迁移隔离具体方案）：已定位 test_http_account_run_keeps_user_id_for_actual_worker_mapping 的公开 submit_discovery_proposal 持久化 base_directive_version，directive_proposals 不可变触发器禁止删除，不能靠事后清表收口。root 核 tests/integration/test_need_units.py:80 的现有 unit_engine：同 owned 测试容器内 UUID 独立库，迁移 head，finally dispose 后 DROP 精确库名。允许仅此具名例复用该 fixture，所有配置/session/engine 从该库派生，不残留共享 db_url；保留原公开业务入口、不可变触发器与 0052 护栏。理由：已有生命周期专为不可变证据与旧迁移隔离；成本：一例增加独立建库/head 迁移，并需原执行次序组合验收，不新造 fixture 或清共享数据。


### Task12 冻结版本第二次完整后端与只读审查调度

第二次完整后端冻结源码 48e4465fc307212e794d6ed87501cb418f74d245，UTC 2026-09-06 10:46:11 启动，原命令/预算不变，单 DB 测试进程。98项具名映射已落正式验收附录；最后指定顺序组合23 passed/63.10s、slice4单项通过，static四门exit0。1630源码文件哈希冻结；报告和安全证据可更新，源码不改。

Ruling: 在完整 suite 运行期间启动同一 Task12 独立只读审查席位，先读冻结 BASE→48e4465 的完整 diff 一次，最终报告/全量结果到齐后再给完整结论；不启动另一个测试进程、不改源码、不提前验收或开始 Task13。理由：代码已提交冻结，审查独立于 DB 运行，可减少串行空等；成本：若全量发现需改动，同一审查者只检查后续修复 diff 与证据更新，不能将预读称最终 Approved，也不增加重复全量审查席位。review package 217324 bytes，Git stderr518行计数保留，不修共享元数据。


### Task12 独立预审发现：A3 独立审批证据无效

review_task12 具名确认 Important/P2：web_core_contacts.py:108–142 用 boss_identity 创建/提交 Campaign，却将审批包 proposed_by_employee 设 identities[2]，再由原 boss decide。Campaign/版本持久 created_by 实为原 boss；legacy approvals.submit 不反查 Campaign，仅信任传入提案人，因此此测试绕开自批门槛，不能证明独立审批。Ruling: 接受审查发现；当前第二 full 继续冻结，结束后按同一审查完整清单统一修复，使用真实提案人、先证自批拒绝、再另一老板经原 HTTP 审批并继续原链。理由：审批证据必须来自真实 actor，公开 API/service 调用也可能被错误夹具误组装；成本：此前 A3 独立审批声明无效，历史保留并撤销该通过口径，修后需新 owner 全主链验证。当前 full 仍用于迁移/组合风险证据，是否再 full 依据实际失败和修复范围，不自动无限重跑。

额度疑点已具名消除：原 hunter_contacts 装配同样采用固定 tool_user 与 InMemoryHunterQuotaGuard，contact_provider 明确秒/分钟单 worker 限流，不声称跨进程一致。新装配沿原机制，不新增该缺陷；最终报告必须区分联系人单 worker 短期限流与研究持久 quota，不能总称所有额度跨重启持久。


Task 12: fix round 1/5 已授权排队（I1：A3 真实提案人与审批 proposed_by 不一致；当前第二 full 结束后执行，不在冻结运行中改码）。root 已完整阅读 task-12-review.md，Spec/Quality 均 Requires changes，唯一阻断 Important/P2。修复限测试编排及相应安全证据/报告，保留生产审批规则；自批拒绝后另一老板经 HTTP 批准并完整主链新 owner 验证。最终报告区分 48e4465 全量与 fix 后聚焦版本；同 reviewer 仅核 fixdiff 与最终证据。尚无修复提交，Task12 未完成。


Task12 第二 full 期间只读视觉核验：原 Linux 完整 browser owner 2ea6b4a4e32f429192c2d1926a2ebfc1，实施者报告 lifecycle code0/cleanup_verified=true/52.535s。root 实际 view output/playwright/t10-2ea6b4a4e32f429192c2d1926a2ebfc1/quote-390.png 与 exact-cost-1440.png：390可见指定报价已批准和对象绑定，1440可见精确Cost ID、quoted成本来源与只读就绪区，无截图内横向溢出。截图不是PDF内容/下载证明，后者沿完整测试断言。该场景不受A3 Campaign夹具提案人缺陷影响，也不是Mac同一实体；最终证据与全量结果待收齐。


### Task12 第二次完整门禁通过与 I1 验证范围

冻结 SOURCE 48e4465fc307212e794d6ed87501cb418f74d245 第二完整后端 exit0，9318 passed，pytest2132.64s/wrapper2139.76s，UTC2026-09-06 11:21:50结束，session5079完成。此数为单轮实际结果，不与首次98failed/9217passed或局部组合累计；warnings/skips/source hash/本轮故障owner最终核验待报告。

Ruling: 若 I1 fix 确实只改测试审批 actor 编排和绑定断言，未改生产实现、共享 fixture 或迁移，则新 owner 完整主链聚焦验收加受影响静态足以覆盖，不作无依据第三次 full。理由：第二 full 已消除原迁移/组合疑点，I1 是独占主链 fixture 证据错误；成本：最终分栏报告 48e4465 全量与 fix 后主链版本，不能声称最终 fix SHA 完整全仓同轮运行。若修复扩大或出现新失败，再按具体风险决定验证。Task12 仍待 I1 修复及同 reviewer 限定复审，尚未完成。


Task 12: fix round 1/5 complete — I1 addressed，0 open；7e10383 source /5b63744 report；同review_task12限定复审双Approved。root已完整读最终报告/新增review，实读安全proof/audit并看最终Mac Need1440/Handoff390及Linuxquote390/exactcost1440。Task6 void/Task9标题/Task11 sourcing覆盖/私有文件清理等本批闭合；Task7纯文档Minor交13。早期warning未知、平台/恢复/真实外部未运行、内存限流边界仍保留。


Task 13: in_progress — fresh实施者 /root/web_task13（Astra high、fork none、无子代理），BASE aa24508dc77fac1e6cc2e21225cb3dcd21dd240b。此controller验收提交5文件，stderr142行保留；原6张tracked Catalog截图与历史output未混入。要求最终交付/桌面契约/owned备份恢复/文档Minor/正式裁定证据，之后独立审查和唯一全分支review。


### Task13 备份恢复演练范围裁定

Ruling: 允许新增仅测试演练 test_web_core_backup_restore.py，复用原Supervisor.start_infrastructure/migrate和OwnedContainers，source PG+MinIO无四应用写入，经原RawArtifact公开接口保存合成原件/metadata；源owned库dump恢复至另一全新owned空PG，原件复制至另一owned bucket，精确tenant元数据摘要与原件SHA256一致。连接/客户端先收口，finally精确owner+ID清理，配置/dump/DSN只在脚本受信内存或私有临时文件，不到模型/日志。理由：原入口无备份脚手架，Task13要求隔离数据实际恢复验证，不需要新增业务launcher。成本：这仅证明静止owned数据/原件恢复，不是运行中跨PG/对象/外部邮箱状态的一致性备份产品，不证明PITR、任意生产恢复或自动恢复launcher；文档必须给准确演练命令并保留这些后续门禁，新空owner不能称旧业务恢复。禁止关闭不可变约束、目标覆盖已有业务库或读取用户生产dump。


Task13实现交付：SOURCE0bfb4d11a8e3e8eb7678d48be091736bd399870b，report fe6f7dc2622391c9359866dd90fafbf3573ebdb5。root已完整读task-13-report及safe backup JSON，静止owned恢复1passed/9.31s，hash一致/source不变/不同owner/清理无错误；边界/ruff/scan/189当前链接和64报告链接通过。唯一新验收测试、两处纯docstring，未变生产行为。已派fresh review_task13 Astra high/fork none；包aa24508..fe6f7dc 774532bytes，stderr500行保留。Task13仍待独立审查。

Ruling: Task13中32份历史报告/审查/spec/ledger字节复制用manifest source/archive SHA逐项核一致，无须再次审原实现或把原文读两遍；所有新增文档、恢复测试、docstring差异完整审一次。理由：这是精确归档复制，不是重做已Approved任务；成本：既有历史结论本身的限制保留，新的导航/截止说明/当前状态和最终全分支语义仍须独立审查，hash不能替代这些检查。


Task 13: fix round 1/5 — 初审Spec/Quality Changes requested，1 Important/P2、2 Minor/P3，0Critical。I1：新恢复测试transport.close异常跳过engine.dispose、第二client构造/first.close异常跳过其它client关闭；M1：证据写失败跳过logging全局状态恢复；M2：正式索引空行拆出8行表格。root完整读report后正式followup同web_task13，BASE fe6f7dc，待fix提交。

Ruling: 接受I1，独立登记/释放每项资源，保留固定安全主失败并补受控异常清理覆盖；M1同一资源收口文件且影响后续测试诊断，M2纯一处格式，均在既有I1同次修复完成，不开启额外Minor循环。理由：正常1passed不能证明关闭异常路径，外层容器删除不能替代engine/client释放；成本：新增聚焦失败覆盖及正常owned恢复新版本复验，旧正常成功保留，不重跑未变Task12全量，不改生产connector或新增备份产品。


Task13 fix round1/5 complete：SOURCE041bc741e0cd57dc9ed942d3a53f116b4fdffcbd、REPORTfebf7e0eeb20282b511b9e5bc48c760a0a325ad6；同review_task13限定复审双Approved，0 open，I1/M1/M2全关闭。root完整读新增报告/review并核新safe backup：4项无资源异常unit GREEN、正常owned恢复1passed9.67s、source/target hash一致且源不变/清理无错。保证关闭尝试及失败记账，不宣称真实SDK永久阻塞也必可释放。Task0–13均完成，下一步唯一全分支review，尚未通过。


Final review: in_progress — 唯一审查者 /root/final_review，Astra high/fork none/无子代理。BASE ec801a87270503b92d3c70389cbcd4b92807dbb9 → HEAD b2c64dbf42f2a4cdbade1df16eece8500749d4d1；72本地提交，包399路径/69008行/3904484bytes，Git stderr3262行计数；final-review-map.json逐文件行号便于一次顺序检查。Task0–13均已独立Approved，最终review不以分批结论替代，尚未通过。仅一统一fix波+一限定复审，root不写生产修复。

用户询问还有多少没完成，已明确本轮Web核心受控开发与分批验收完成，剩全分支审查、实际需要的修复与最终记录/启动说明收口；未猜百分比或完成时长。用户未取消原目标，继续执行。


最终交付收口预检：当前主能力矩阵及A1–A10报告顶部仍是Task12截止声明；最终review通过后应以正式交付索引补当前总状态，保留48e4465 full/7e10383 main-chain/041bc741 restore的版本区别。计划末5项通用checklist待最终核对后勾选。归档采用新的final-ledger全文及final-review原文和hash manifest，不覆写原87条Ruling所在行的历史快照；旧archive链接作为历史定位保留。用户已补充获知本机受控完成不等于正式运营，真实Provider/共享登录/部署及Mac完整quotation未配置仍是后续范围。


## 用户关机暂停 — 2026-09-06

Ruling: 用户明确要求先收尾、马上关机，后续另行通知继续；立即中断唯一final_review，审查者只保存断点后终止，不继续审查、测试或修复。理由：这是用户对执行时机的明确变更，优先于此前持续完成计划；成本：最终全分支review未完成、尚无Approved，剩余源码/测试范围与两个未确认疑点随正式暂停交接保存，不把未发现已确认缺陷冒称全过。原Task0–13与逐版本证据不变。

Ruling: 暂停保留本计划scratch、worktree和分支，将暂停完整ledger、审查progress/paused报告、diff定位map与SHA清单归档到docs/acceptance/web-core-delivery；原历史快照不覆写。理由：最终门禁未结束，提前清理会破坏断点；成本：保留381个output历史未跟踪文件，后续恢复需先读pause-handoff，不重做完整任务。没有合并/推送/部署或新增后台自动任务。

关机只读核验：ps命令指向本工作树的已知pytest/Web/worker运行进程0；Docker running filter tradeos.controlled.owner结果0，两命令exit0。只核本轮范围，不声称其他项目停机，无清理别人进程/容器。本次仅metadata暂停提交，敏感扫描与diff-check后本地保存；不运行新的业务验收。


## 2026-09-07 恢复最终收尾

用户明确继续web收尾。已核HEAD068eed58e0b12e38e3d87e5c89f593b07c617ace，分支codex/web-core-completion，tracked无改动，381个untracked全在output；暂停未遗留新的业务改动。原final_review代理不在当前live tree，按pause-handoff由/root/final_review_resumed（Astra high/fork none/无子代理）接续同一审查职责与已读断点；固定ec801a8→b2c64db范围不变，068eed58仅暂停metadata。不是重开Task0–13或重复全分支审查。
Ruling: 恢复先完成未读范围与具名疑点，历史相同副本按hash去重；仍只一统一fix波+一次限定复审，最后归档全部裁定并清本计划scratch。理由：用户恢复执行且前轮审查未完成；成本：最终结论仍待实际审查，既有9318/411及局部修复结果按版本保留，不自动重跑全仓，不据暂停metadata将其冒称最终HEAD全量。


最终审查恢复中新增已确认Important（审查者初报，完整报告待）：CurrentEmployeeReplyFactory将BoundedRawArtifactStore直接交ArtifactMessageContentReader，ClassifyStep先load/S3/模型，当前active boss检查在后续CurrentEmployeeReplyActions._run才做；已排队分类在员工停用/降权后可能仍读原件/交模型并保存分类，读取也无Gateway ledger。旧ArtifactStore设计仍要求调用前actor授权，不构成豁免。已要求最终完整清单附精确位置及最小修复面，尚未提前派fix，仍保持唯一统一波次。


Final review complete（审查本身完成，交付未通过）：root已全文读final-review.md，固定ec801a8→b2c64db，Spec/Quality Needs fixes；0Critical、1Important R1、3Minor M1/M2/M3。原手写diff/测试/契约均完成，历史32副本、1630源码快照及安全证据已核，详见正式报告，不重跑原验收。
Ruling: 接受R1，按现有当前active boss qualify资格在Raw读取、模型调用和分类写入前执行实际当前检查；Raw读取经真实Gateway授权/执行账本/有界一次交付，不凭旧受信Artifact设计豁免；同波处理M1四处SQLite显式close。理由：撤权员工仍读内容/调用模型是实际授权缺口，M1是独立确定资源释放缺口且可窄改；成本：需新的真实领域/PG/Gateway撤权与失败顺序回归和完整受控主链，原9318/411不得改称新源码full，不修改Gateway核心或扩大角色权利。
Ruling: M2技能64/10000精确阈值、M3adapter内部await取消直接测试继续parked，保留最终review独立意见。理由：未发现实现错误，现有危险结构/取消传播与外层清理已有覆盖，通用Agent生产消费者仍disabled，二者不阻断当前本机受控范围；成本：阈值off-by-one与未来异常包装的直接回归保护有限，技能加载规则变更时补M2，生产provider/任务来源启用或异常分类变更前补M3，不静默删除。
仅派一个fresh最终fixer，完整清单/精确BASE/brief/report随派发；之后同final_review_resumed仅限定一次复审。root不改生产实现。
