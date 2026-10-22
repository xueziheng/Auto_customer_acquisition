# TradeOS Agent 升级验收记录

日期：2026-10-22。状态：Task6 fix round1已通过独立复审；全分支评审随后提出I1/I2/M2，本次集中修复待一次定向独立复审。本文件不声明生产发布或真实邮件发送。

## 范围与代码来源

工作分支 `codex/tradeos-agent-upgrade`；实际fork基点 `codex/jslt-enterprise-access@22ece5a86d374795437407a67154ceba2973e1b9`。Task6起点 `0df70cc9c8e486598a018b26d573141a62e05a61`，此前Task1–5/7已通过各自门禁。Task6最终提交与完整BASE..HEAD记录在计划报告，由最终独立评审确认。

原工作区 `/Volumes/T7/Company/Auto_customer_acquisition` 保持只读。其 `codex/gmail-mailbox-sync@aebaa9b6` 与升级分支在非浅克隆仓库无共同祖先，不能把交付描述成可直接merge回原分支。原独有改动按源码审计语义迁入，没有覆盖原目录或配置。

`c058a59` 初次导入704个代码路径：701项逐字一致、3项仅空白差异、0缺失。**704是文件数，不是获客来源数。** 当前保留670条路径；另外34条均为 `apps/web/dist-before-login-hint-20261016/` 废弃生成备份，已在7715b40有意清理，不恢复这些产物。控制者最终静态检查对冻结验收副本2947文件重新哈希，0改动、0缺失。

54个来源ID及 `shared/schemas/discovery_sources.py` 原registry哈希保持，来源的scope/notes/limits、模型Catalog/平台绑定、DeepSeek等key引用、员工本人邮箱和Obsidian路径未迁移或重建。保留证明不读取真实凭证，也不说明54个来源全部可运行。现有来源的planned/partial/available状态仍有效。

## 最终实现与兼容

- 平台模型：企业域与旧HTTP保存入口均拒写；企业界面显示当前安全平台摘要。GET不pin运行/不解析密钥。平台正式修改路径、旧模型使用记录与旧运行绑定保留。
- 企业资料：原件与版本不可变，确认资料只作本企业私有探索上下文；原Obsidian授权快照与清理机制保留，不自动公开为产品。资料变化/撤权时恢复任务重新检查。
- 统一库存任务：私有目标、地区和自然语言条件在企业任务内保存；公共候选只保存公开事实。来源恢复遵守原24h截止，剩余缺口和unknown均耐久，不空转新模型/来源请求。
- 原服务计划：`domains/prospecting/service_tiers.py`、`service_plan_contracts.py`、`infra/db/prospecting_service_*`、运营CLI、原研究tier wrappers/筛选应用均保留；周期改由 `ScheduledInventoryDriver` 提交 canonical InventoryRequest。新迁移0102不覆盖原0070编号。旧持续研究驱动不装配；旧proposal排程要求重新确认产品目标。历史筛选等待由 `screening_wake` 与原workflow engine精确代次唤醒。
- 档位上限：30天候选100/300/600，搜索200/600/1200，联系人20/120/240；周期168/56/24小时，每轮候选与假设10/20/30、搜索10/20/30、页面10/25/40，单企业页面2/4/6、联系人1/1/2。切档保留计数，显式续期才换30天；unknown不返还。即时库存命中也有真实请求/周期收据，不伪造WorkflowRunId。新旧联系人处理同Campaign+客户共享收据，历史run收据兼容，不因升档扩大旧预留。
- 自动首封：自动客户准备/验证/入组后，原Campaign scheduler经原email.send和正式QQ连接器发送。发件人从真实active本人邮箱owner、当前客户归属及已批SendingIdentity交集产生；不是技术tool用户或分类boss。绑定版本进入指纹并在解析凭证前重核；换绑不能悄悄换发件人。旧Gmail及private QQ路径保持。
- 普通接续：两步活动序列真实回复后REPLIED、停止后续冷邮件；单步/末步保持COMPLETED，凭已完成真实分类流程/可继续待办独立准入普通scope，不复活旧Enrollment。running阶段只排待办，零binding、零普通模型、零普通发送。同一Need继续补证/验证；商业问题转审批/owner review，sales在真正Opportunity intake节点交给有权限人员，不假称机会已建。
- UI保持原路由与页面布局。模型页只读，找客户页自然加入服务周期摘要/设置；客户详情公开来源记录与实际联系人验证状态分开表述，不再与“已验证并入组”矛盾。租户/员工切换清除服务周期选中目标。

完整接口决定见[ADR0118](../adr/0118-agent-upgrade-final-integration.md)，普通对话运行约束见[运行说明](ordinary-dialogue.md)和[ADR0117](../adr/0117-ordinary-dialogue.md)。

## 原独有服务计划的合并位置

|原能力/文件|最终处理|
|---|---|
|`domains/prospecting/service_tiers.py`、`service_plan_contracts.py`|同路径保留精确档位、30天、证据定义，增加统一目标与收据契约|
|`infra/db/prospecting_service_plans.py`、`prospecting_service_tables.py`、`contact_screening_waits.py`|同路径迁入，适配当前tenant/RLS；新`inventory_service_plan.py`绑定统一任务累计预算|
|`continuous_discovery.py`旧研究循环|不装配；改用`apps/scheduler_worker/scheduled_inventory.py`窄提交器，唯一库存driver执行外部工作|
|account_discovery `service_tiers.py`、`screening_wake.py`及原flow/steps|同路径保留共享联系人收据、限额切片和历史等待唤醒；旧v1/v2定义不重写|
|demand_discovery `service_tiers.py`、`screening_application.py`|保留原研究wrapper/筛选证据；统一排程现查已确认资料，不授予发信|
|`apps/api/routers/prospecting_service_plans.py`、两个Vue panel、运营CLI|保留原HTTP与页面能力；新目标输入兼容旧proposal；CLI仍显式preview/apply，企业无自助赋档|
|原0070迁移与原0078/79 ADR|不覆盖冲突编号；新增0102与ADR0118。未读取或导入原运行数据库数据|
|API dependencies/runtime/main、scheduler bootstrap/runtime/standalone、prospecting service导出|逐处语义接线，保持原认证、库存、资料、邮件流程和单一singleton|

## 验证证据与实际边界

下表为首次交付 `43a1148084b0b304e3cd85b1e611bf34716db8bb` 的全量验证证据；不是fix round1重新运行的结果。退出码均明确记录，早期通过数不相加充当最终结果。

|检查|最终结果|证据|
|---|---|---|
|全unit/evals|10002通过、76跳过；exit0|`/tmp/tau-task6-final-unit4.log`，144.03秒|
|相关真实PG/浏览器/两企业|33模块302通过、5条既有M1告警；exit0，406.23秒|`/tmp/tau-task6-final-pg4.log`；含Task5 I1/I2/I3、knowledge隔离、服务计划RLS/迁移与完整QQ链|
|Web类型/构建/交互/lint|64文件691用例、类型/构建通过；lint0错误1191警告；均exit0|`final-web4`、`final-build4`、`final-web-lint5`日志；Node24.15.0|
|Linux真实CLI与协议|83通过、1条环境warning，exit0；解析器29通过、exit0|`/tmp/tau-task6-final-linux4.log`、`/tmp/tau-task6-final-parser3.log`；只读源码/deps/真实CLI，无home/auth/docker.sock，network none|
|结构/ruff/完整mypy/差异|通过，exit0；mypy955文件|`final-boundaries8`、`final-ruff8`、`final-mypy8`、`final-diff8`日志|
|敏感扫描|tracked与新增共3130路径、默认tracked与index均exit0|`final-sensitive-all`、`final-tracked-scan`、`final-staged-scan`日志；无scanner豁免变更|

### fix round1：服务计划预算与周期恢复

首次审查I1确认高级档每轮30次直接传给平台0..24构造校验会失败；现DeepSeek/Tavily正式装配先取服务档与平台policy的较小值，原持久cap和当前降档仍只收紧。高级档30和平台24分别保留，升级不扩张已绑定cap。

I2确认独立额度预留已提交、InventoryRequest事务回滚后，原周期被自身预留阻塞。现只用当前有效服务期的原receipt恢复同key/数量；原额度不重复扣除、不因切档改变请求指纹。当前排程、员工授权和确认资料仍在新请求前重核；实际候选/页面/搜索执行仍受当前档位收紧。暂停/到期不新建请求，旧服务期receipt不能用于续期后的消费；已有请求回执仍可按原规则补绑定。

fix round1相关验证：201通过，20.52秒，exit0（`/tmp/tau-task6-fix1-final.log`）。涵盖五个真实PG/持久仓储模块及五个相关单元模块；其中两后端装配的模型身份端口是合成替身，没有解析凭证或调用供应商。真实PG故障点位于InventoryRequest已flush后，断言事务回滚且独立预留留下，再用新service/store恢复；另覆盖升降档、原数量冲突、暂停、到期、续期、撤销员工/产品权限。完整mypy955文件、ruff、结构、敏感扫描及差异检查均exit0，日志前缀`/tmp/tau-task6-fix1-final-`。未重复全unit/evals、302 PG、Web或Linux套件；这些既有证据保留上表的版本边界。M1既有告警交控制者分流，没有顺带修改。

```bash
.venv/bin/python /tmp/tradeos-agent-upgrade-20261021/fresh_test_database.py \
  tests/integration/test_inventory_service_plan.py \
  tests/integration/test_prospecting_service_plans.py \
  tests/integration/test_inventory_search_budget.py \
  tests/integration/test_inventory_search_readiness.py \
  tests/integration/test_prospect_inventory.py \
  tests/unit/test_account_discovery_service_tiers.py \
  tests/unit/test_prospecting_service_tiers.py \
  tests/unit/test_prospecting_service_plan_contracts.py \
  tests/unit/test_prospecting_service_plan_cli.py \
  tests/unit/test_inventory_search_policy.py -q --basetemp=/tmp/t6-fix1-final
```

首次交付具体命令从仓库根运行（Web命令在`apps/web`，PATH前置Node24）：

```bash
.venv/bin/python -m pytest tests/unit tests/evals -q --basetemp=/tmp/t6-final-unit4
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy domains shared tool_gateway connectors apps workflows notification_gateway infra
.venv/bin/python scripts/check_boundaries.py
.venv/bin/python scripts/scan_sensitive.py
.venv/bin/python scripts/scan_sensitive.py --staged
git diff --check
npm test
npm run build
npm run lint
```

PG使用任务`fresh_test_database.py`每批创建并迁移全新数据库对，凭证不输出。最终33模块清单与Linux完整容器命令见下，任务报告另保留机器记录；没有并行运行两套PG。自动QQ演练包含全局off、两步首封REPLIED、单步COMPLETED和两个独立企业角色的完整同Provider链。准入前/每轮共用当前业务事实检查；直接admit及running预检在联系人失去verified、公开产品hash改变/撤回、联系人或客户抑制时均不新增binding/普通待办/模型/发送，已有待办和抑制历史保留。原私有回复13场景保留。客户详情真实浏览器截图为`/tmp/t6-final-pg4/test_actual_preparation_browse0/preparation-1280.png`与`.../test_actual_preparation_browse1/preparation-390.png`，已人工检查。

警告与失败修正不隐去：Web build保留PlatformConsole同时静态/动态导入的分块warning；lint1191条为风格类告警，没有本次批量清理。Linux离线依赖缺charset检测组件产生1条RequestsDependencyWarning，网络禁用且相关协议替身测试通过。敏感扫描初始17处与Task6BASE相同但CI仍要求绿色；4处产品运行期表达式、4处测试运行期/DOM表达式、9处合成credential/marker已按原占位及运行期写法规整，未改scanner或移除credential反例。完整unit/evals覆盖原件版本、Obsidian快照/隔离和credential-marker，相关PG另验证知识库实际角色隔离。

证据级别明确分开：真实Postgres测试证明事务、角色RLS、状态恢复和API/scheduler/Gateway装配；浏览器使用本地合成后端；Linux使用真实Codex CLI 0.147.0与受控模型服务，不调用真实模型供应商。模型、Hunter、网页、SMTP/IMAP在完整业务演练中均为协议/端口替身，未发送真实客户邮件。

Task7唯一真实来源诊断：原Gateway固定SYSTEM来源诊断供应商HTTP **1 / cap6**，TED真实notice **674225-2026** 成功。Tavily未运行，原因usage_unknown；DeepSeek未运行，原因official_current_version_missing，未进入balance/search。page/model/business/email均0；有技术账本/配额写入，不能说完全无写入。未重复诊断，也未修改来源配置来制造成功。原生CLI、合成DeepSeek搜索链与这个TED样本是三类不同证据。

未执行生产迁移、部署、真实模型/联系人验证/邮箱操作。真实合格贸易机会数量、单位成本与运营效果未测量，不能以候选、调用或测试数量替代北极星指标。

## 24项裁定、依据与成本

以下来自实施过程的明确裁定；成本不等于已经发生的模型或供应商费用。

|序号|决定与依据|代价或限制|
|---|---|---|
|1|冻结receipt-fix/src累积基线并修复未完整普通回复，保留用户现有功能，不反转实验改动|基线更广，必须扩大回归，不能假设导入代码已正确|
|2|原工作区保留完整、验收副本只读；存在分叉与未提交用户改动|原独有文件要语义合并，不能直接覆盖|
|3|遵守用户根九条边界，不导入快照根的私有邮箱例外|私有测试路径不扩大为生产授权|
|4|确认企业资料只授予私有搜索上下文，不自动发布产品或承诺客户|回复事实必须另查当前公开可见性|
|5|总体升级/测试已有用户授权，不重复索取计划批准|可逆实现决策要留痕，不能视为生产外发授权|
|6|普通持续对话必须有独立绑定授权/attempt与email.reply插件；客户回复后旧冷邮件Enrollment保持终止|新增授权账本、尝试、精确关联及迁移；判断有误需重做接口/集成，不能为省工程量复活旧序列|
|7|24h恢复期限仅停止新外部工作，当前授权且新鲜的已存结果仍可交付；剩余缺口原子终结|交付可能晚于原采集窗，仍须检查freshness/refs/权限，不能继续fit或来源IO|
|8|用户新授权仅放宽有界免费/赠额来源读取，取代此前全部合成限制|可能消耗少量免费额度；不授权付费回退、邮件或部署|
|9|自动跟进scope精确绑定当前确认资料document/revision，品类字符串不足以证明产品事实|无refs旧自动政策需修订再审批；确认不授予对外发布权限，缓存也要私有fit|
|10|独立DeepSeek Search经现source_search Gateway；当时官方原生web_search不遵从max_tool_calls且仅v4-pro支持|独立Search付费，与原生工具免费促销不同；仅另证免费赠额时才可免费实测，平台显式付费许可，无Tavily前置/密钥复制|
|11|保持composition_support无副作用白名单；机械事务桥去infra/db，授权编排去workflows公共端口|需要兼容重导出与接线测试，不放宽依赖边界|
|12|保留ADR0093老板搜索/请求/分配双层权限，manager/sales仅受托处理本人客户|员工不能自行发起自动搜索，需另行明确权限变更|
|13|全局automatic_send_enabled=false只挡实际发送，合规准备/验证/入组仍可执行|更多scheduler事实接线；必须证明email.send=0，不能伪造sender readiness|
|14|scheduler使用与API相同显式Hunter配置及惰性受信resolver，经原Gateway/quota/readiness|新增启动兼容与singleton测试；缺配置阻断、unknown只核对，不自动验证配置/复制密钥/扩大探测|
|15|没有Opportunity的商业问题暂停普通对话，进入当前owner的REVIEW_REPLY|早期商业问题多一人工步骤；具体承诺仍逐次审批，不能造Opportunity或通用handoff后继续自动追问|
|16|允许窄固定来源SYSTEM诊断插件及受审任务脚本；旧系统无现成诊断入口，不能伪造人工proposal|多一小诊断测试面；总限额10内，旧远端不兼容保持NOT RUN，不部署产品模块/配置、不写业务记录|
|17|真正首次询盘回复可用独立ordinary_dialogue_policy明确批准的PRIMARY_BUSINESS身份；冷Campaign仍仅COLD_OUTREACH|增加精确版本回复发件范围/选择与角色隔离测试，政策改变重批，不默认扩旧政策|
|18|普通对话资格/规划只窄授当前受托邮箱owner且有当前客户访问权，每步验证精确批准政策|独立权限路径与撤销/归属测试；失去任一权限即暂停等待合法批准/分配，不扩大冷邮件、Inbox或搜索权|
|19|保留老板专有Opportunity intake与Playbook权限；无该权限的普通对话owner仍完成有客户证据的同一Need更新/验证，到实际intake/handoff节点才暂停进入owner REVIEW_REPLY并给明确原因|合格sales对话多一步有权限真人接管创建机会；不冒充老板或新增SYSTEM创建权限，不假称已建Opportunity，也不提前阻断尚未完整Need的正常沟通|
|20|精确当前获批普通对话scope内，对已许可普通产品资料的询问可到有界product-section planner；旧无scope冷邮件行为、商业与不支持内容的handoff保留|多一scoped action分支及首个冷回复准入时序/测试；如判断有误只回退此窄批准行为，不扩大邮箱或商业承诺权限|
|21|复用正式QQ EmployeeMailConnectors经原email.send证明同Provider首封与普通回复；Task5保留严格SENT冷attempt/BusinessMail准入和显式active-boss classifier，Task6接同connector到原Campaign scheduler完成自动首封|新增显式部署委派字段/冲突检查、scheduler连接器装配和同Provider自动全链测试；不扩private grant、不造boss、不混Gmail/QQ夹具，API首封不能代替自动证明；通过前自动QQ冷启动仍未完成|
|22|保留冷Enrollment全部终态；正常耗尽COMPLETED后的真实分类回信，可凭精确SENT、MIME/canonical、已完成原流程和可继续待办进入独立获批普通scope|增加窄准入、双阶段时序及拒绝测试；running只排无binding待办，完成后重核，不能复活冷序列、清除人工review或扩大商业权限|
|23|最终评审M1的SQLAlchemy DISTINCT ON兼容、离线charset依赖包、Web分块与风格告警转独立维护；未观察到本次新业务故障，避免扩大升级回归面|保留PG5告警、Linux1环境告警、Web0错误1191警告和分块提示，不声称零告警或生产HTTP已验收|
|24|最终评审六项无法判断内容作为明确证据限制接受；合成测试、来源哈希不证明真实运行、法律事实或已控制邮箱者的攻击抵抗|以下六项未验收；不扩大权限或生产就绪结论，必要时另做验证/设计|

## 最终评审的六项证据限制

1. 真实DeepSeek/Tavily当前API可用性、免费资格及真实模型质量：禁止本轮重新live；合成响应不证明线上可用。
2. 真实联系人验证、送达率、域名信誉、真实客户多轮沟通与单位合格机会成本：没有真实运营实验。
3. 生产配置、API key引用解析、生产迁移/回退与生产多企业隔离：没有部署/配置访问授权；本地两企业PG不替代生产验收。
4. 各国商业触达合法性与处理依据真实性：代码只验证引用、政策及审批，未核查真实法律事实。
5. 未触及的全部历史报价、供应、私人测试、外围UI导入代码的逐行正确性：风险导向评审及保存回归不等于穷尽人工审查。
6. 完整邮箱控制权者恶意修改Sent正文而保留摘要头：当前只验证精确头绑定，未证明可抵抗该攻击，不扩张连接器保证。

## 迁移与回退

迁移0102仅用于正常受控部署流程，本任务只迁移任务临时数据库。四个服务计划表存在数据时拒绝降级，必须先评估记录保留与恢复，不能静默删除额度或历史证据。升级运行时保留旧HTTP和旧数据读路径；企业模型保存返回403是已确认权限收口。

上线需由实际运营者选择企业、当前获批产品/资料、明确Campaign/普通scope、真实本人邮箱和显式分类boss，并核对原全局发送开关。测试通过不会自动开关、批准历史Campaign或扩大private grant。撤批、失去owner权限、邮箱换绑、产品公开hash变化和未知外部结果仍按原停止/人工核对机制处理。

Task6独立评审与全分支独立评审待控制者登记；未完成时不称最终审查通过。旧SQLAlchemy DISTINCT ON告警M1保留如实记录；O1伪造SENT关联暂停问题已窄修并有合成攻击验证。最终验证发现及限制以本节和任务报告为准。

## 完整PG与Linux复核命令

```bash
.venv/bin/python /tmp/tradeos-agent-upgrade-20261021/fresh_test_database.py \
  tests/integration/test_prospect_inventory.py \
  tests/integration/test_inventory_model.py \
  tests/integration/test_inventory_source_recovery.py \
  tests/integration/test_inventory_page_gateway.py \
  tests/integration/test_inventory_search_budget.py \
  tests/integration/test_inventory_search_readiness.py \
  tests/integration/test_inventory_search_migration.py \
  tests/integration/test_inventory_service_plan.py \
  tests/integration/test_inventory_followup_queue.py \
  tests/integration/test_prospecting_service_plans.py \
  tests/integration/test_customer_preparation.py \
  tests/integration/test_customer_preparation_execution.py \
  tests/integration/test_customer_preparation_consumer.py \
  tests/integration/test_customer_preparation_runtime.py \
  tests/integration/test_customer_preparation_migration.py \
  tests/integration/test_dialogue_execution.py \
  tests/integration/test_dialogue_gateway_driver.py \
  tests/integration/test_dialogue_source.py \
  tests/integration/test_dialogue_owner_access.py \
  tests/integration/test_dialogue_model_budget.py \
  tests/integration/test_ordinary_dialogue_factory.py \
  tests/integration/test_inquiry_admission.py \
  tests/integration/test_reply_completion.py \
  tests/integration/test_scheduler_campaign_driver.py \
  tests/integration/test_outreach_campaign_lifecycle.py \
  tests/integration/test_enterprise_knowledge_isolation.py \
  tests/integration/test_enterprise_api_isolation.py \
  tests/integration/test_standalone_model_runtime.py \
  tests/integration/test_tenant_row_security.py \
  tests/integration/test_email_inbound_page.py \
  tests/integration/test_aliyun_web.py \
  tests/integration/test_business_mail_tenant_registration.py \
  tests/integration/test_private_reply_send.py \
  -q \
  --basetemp=/tmp/t6-final-pg4
```

Linux真实CLI（只挂只读任务源/现有纯Python依赖/CLI，network none）：

```bash
docker run --rm --init --network none --cap-add SYS_ADMIN \
  --security-opt seccomp=unconfined --security-opt systempaths=unconfined \
  --entrypoint python3 -v "$PWD:/src:ro" \
  -v /tmp/tradeos-agent-upgrade-20261021/linux-deps:/deps:ro \
  -v /tmp/tradeos-agent-upgrade-20261021/codex-linux-package:/codex-package:ro \
  -e PYTHONPATH=/deps:/src \
  -e TRADEOS_TEST_CODEX_BINARY=/codex-package/package/vendor/aarch64-unknown-linux-musl/bin/codex \
  -w /src tradeos-agent-linux-test-runtime:20261021 -m pytest \
  tests/unit/test_codex_provider.py tests/unit/test_inventory_codex_cli.py \
  tests/unit/test_inventory_collector.py tests/unit/test_inventory_query_binding.py \
  tests/unit/test_dialogue_codex_cli.py tests/unit/test_qq_p1_model_wiring.py \
  tests/unit/test_qq_preparation_deadline.py tests/unit/test_qq_sent_collect.py \
  -q -p no:cacheprovider --basetemp=/tmp/t6-final-linux4

docker run --rm --init --network none --cap-add SYS_ADMIN \
  --security-opt seccomp=unconfined --security-opt systempaths=unconfined \
  --entrypoint python3 -v "$PWD:/src:ro" \
  -v /tmp/tradeos-agent-upgrade-20261021/linux-deps:/deps:ro \
  -e PYTHONPATH=/deps:/src -w /src \
  tradeos-agent-knowledge-test-runtime:20261022 -m pytest \
  tests/unit/test_knowledge_file_parser.py -q -p no:cacheprovider \
  --basetemp=/tmp/t6-final-parser3
```

## 最终全分支评审集中修复（2026-10-22）

FIX_BASE `4f83fa2265b2642361e57cb78880b8e3330d979e`；本节仅记录最终修复，不覆盖上述历史全套基线。最终提交SHA及BASE..HEAD由计划区 `final-fix-report.md` 记录。此刻仅READY_FOR_SCOPED_REVIEW，不能视为独立复审通过或生产许可。

- I1：原来源候选预留独立提交保留。恢复先锁当前服务计划和任务，核实原服务期、当前租约、固定来源计划、24h期限以及不存在该来源started/终态尝试；仅复用同digest原预留，原amount不改、不退款、不换身份。返回可执行量由当前上限减全部已用量再加本来源原预留推导，并受driver原来源分配上限约束。已started的unknown仍跳过，无第二次调用。降档原15预留仍记15，实际恢复最多5；后续零额度来源持久标记quota_exhausted而不外调。正常/升档恢复继续剩余未调用来源，累计原10不扩大。
- I2：实际InventoryDiscoveryModel.generate在构造模型组合前，对最终model_payload完整JSON执行既有CredentialMarkerGuard；覆盖query/intent/标签、候选证据和确认资料实际字段。失败仅固定ModelGenerationError(permission)，不回显内容。evaluate经同一边界。真实旧请求持久化、过期领取新lease后仍拒绝；实际生成器零调用。正常输入及原公共搜索隔离验证保持。
- M2：根总纲入口与当前阶段按已接受ADR0113同步，确认资料可私有搜索，确认不等于正式Product发布、Campaign批准或对外承诺。九条硬边界及商业审批未改。
- M1：按裁定23转维护；上文原SQLAlchemy 5告警、离线依赖1告警、Web分块提示与1191风格告警仍存在，未批量清理。本轮未重跑Web/Linux/10002全套或真实来源诊断，相关数量不与本轮相加。

本轮最终命令：

```bash
.venv/bin/python -m pytest tests/unit/test_inventory_model.py tests/unit/test_inventory_source_recovery.py tests/unit/test_inventory_acquisition_driver.py tests/unit/test_inventory_enterprise_context.py tests/unit/test_inventory_search_policy.py tests/unit/test_inventory_query_binding.py tests/unit/test_inventory_search_guidance.py tests/unit/test_inventory_standalone_composition.py tests/evals/test_inventory_evidence.py -q
.venv/bin/python /tmp/tradeos-agent-upgrade-20261021/fresh_test_database.py tests/integration/test_inventory_source_recovery.py tests/integration/test_inventory_service_plan.py tests/integration/test_inventory_model.py tests/integration/test_inventory_search_budget.py tests/integration/test_inventory_search_readiness.py --basetemp=/tmp/tau-ff-pg4 -q
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy domains shared tool_gateway connectors apps workflows notification_gateway infra
.venv/bin/python scripts/check_boundaries.py
.venv/bin/python scripts/scan_sensitive.py
git diff --check
```

|检查|精确结果与退出码|日志|
|---|---|---|
|相关unit/evals|99 passed in 1.74s，exit0|`/tmp/tau-final-fix-unit2.log`|
|真实PG相关5模块|56 passed in 36.61s，exit0|`/tmp/tau-final-fix-pg4.log`|
|全ruff|All checks passed!，exit0|`/tmp/tau-final-fix-final-ruff.log`|
|全mypy|Success: no issues found in 955 source files，exit0|`/tmp/tau-final-fix-final-mypy.log`|
|结构|七类检查通过，结构自检通过，exit0|`/tmp/tau-final-fix-final-boundaries.log`|
|敏感扫描|无命中，exit0|`/tmp/tau-final-fix-final-sensitive.log`|
|差异空白|无输出，exit0|`/tmp/tau-final-fix-final-diff.log`|

RED及中间结果分别保留：

- 将三个实现文件临时还原为FIX_BASE后，新增模型测试命令 `.venv/bin/python -m pytest tests/unit/test_inventory_model.py -k actual_model_dispatch -q`：20 failed, 5 passed, 16 deselected in 0.67s，exit1；`/tmp/tau-final-fix-red-model.log`。正常5条通过，四种凭证×五个输入位置全部未被拦截。
- 同FIX_BASE，fresh_test_database.py仅运行test_inventory_source_recovery.py的`-k service_budget_checkpoint --basetemp=/tmp/tau-ff-red -q`：4 failed, 2 passed, 16 deselected in 1.47s，exit1；`/tmp/tau-final-fix-red-pg.log`。三项未started恢复失败证明原缺陷；降档unknown的额外失败来自过强终态断言，后续按保守未知消耗修正，不算另一缺陷。
- 首次两模块PG：2 failed, 34 passed in 8.33s，exit1，`/tmp/tau-final-fix-pg1.log`。零额度分支不能直接record未started来源，修复其状态顺序；unknown且已耗尽不要求成功终态。
- `/tmp/tau-final-fix-pg2.log`：2 failed, 54 passed in 38.53s，exit1；进程加载旧零额度分支后才完成修改，不能当作修改后失败或通过。一次系统python3编辑脚本因其旧解释器拒绝UTF-8而未执行，已改用.venv解释器执行，未修改仓库解释器配置。
- `/tmp/tau-final-fix-pg3.log`：56 passed in 37.56s，exit0；随后进一步将恢复的当前服务期/政策核对置于计划锁内，最终以pg4为准。
- 初次敏感扫描exit1，2条新合成password赋值命中（`/tmp/tau-final-fix-sensitive.log`）。改为既有识别的placeholder值，保留相同凭证标记负例；未改扫描器、白名单或删负例。最终扫描exit0。首次局部ruff有2个import顺序问题，由ruff --fix修正；最终全ruff通过。

本轮无真实provider、模型、页面、邮箱、配置或生产访问；只用合成输入、协议端口和任务PG。六项证据限制与24项裁定逐条保留于本文件。独立复审仍由控制者执行。

最终交付补充：默认与index敏感扫描均exit0；显式`git ls-files --cached --others --exclude-standard -z`全部3130路径（仅排除原无关`.playwright-cli/`与`output/`）扫描exit0，日志`/tmp/tau-final-fix-sensitive-all.log`，没有扫描豁免变更。提交前结构重查通过；任务PG活动client连接0，日志`/tmp/tau-final-fix-release.log`。
