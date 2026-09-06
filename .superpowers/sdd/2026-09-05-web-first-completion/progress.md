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
Task 10: pending
Task 11: pending
Task 12: pending
Task 13: pending

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
