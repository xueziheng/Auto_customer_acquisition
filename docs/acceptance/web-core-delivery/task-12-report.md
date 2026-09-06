# Task12 实施报告

状态：DONE_WITH_CONCERNS，等待同一独立审查者限定复审；不宣称Task13最终交付。

## 最终事实

- BASE `6cdb40d8019d560d1490925df72a58d14f4881d6`。
- 首轮检查点 `1cbcdcf3d1bbd5ed088b87fff5329c263f6b731d`：98failed/9217passed，历史保留。
- 第二完整门禁源码 `48e4465fc307212e794d6ed87501cb418f74d245`：9318passed、0failed/0skipped/0warning entries，pytest2132.64s/wrapper2139.76s；1630源码文件整轮前后hash差异0。
- I1最终fix源码 `7e10383c253df4a98cd224fb7ee526d721476f9a`：仅2测试文件修正Campaign真实提议人/独立决定人及安全proof；完整主链1passed34.97s。依controller裁定，不因纯该测试编排修复重复第三次full。没有把两轮或局部数字相加。
- 生产版本与第二全量相同；boundary/scan/ruff/mypy通过，Web411passed，typecheck/build/gen:api通过，lint112warnings/0errors且逐行归属。I1后boundary/scan/受影响ruff均通过。
- 98条逐项映射在正式验收文档附录及backend-resolution-index.json，final_full均对应48e4465实际通过，pending0。

正式A1–A10、双环境边界、版本分栏与限制见 `docs/acceptance/2026-09-05-web-core-completion.md`。安全证据前缀 `output/acceptance/task12/`。本报告与最终正式文档单独提交；controller的progress/review-context/Task13 brief未stage。

## I1修复与最终owner

原新增helper错把Approval提议人写成经理，而Campaign实际由boss创建/提交，原boss自批能返回200。因此旧“独立Campaign审批”声明撤回，不能用48e4465测试通过掩盖审查问题。

RED：i1-approval-red.log为1failed20.33s；补固定状态诊断的i1-approval-red-status.log为1failed20.57s，明确campaign_self_decision=200。两个owner631e187e57144938a1c9d4baf30ea341、fe99b028cb53465cb6d6780460eb84ee均stopped/cleanup_errors[]。

GREEN：owner `8d234c0956fb498e9f171342c3b72f51`，Campaign cmp_01M1V7H2KYG3YR28747K9S2DVE/v1，Approval apr_01M1V7H2PMCX7DXRRCA503FK91。实际created_by=submitter=proposed_by；同人HTTP400/request_rejected后仍pending且无decider；第二老板原HTTP批准，decided_by=Campaign.approved_by并与提交人不同。联系人Run run_01M1V7H2TMMDM5ATD2W1HJ1SJ8，enrich/verify/send各1，持久controlled-single-provider verified，未验证入组拒绝。真实回复Need/Opportunity/Handoff、重放/HUP、接受和撤销链通过，accepted=true/pageerrors[]，cleanup stopped/errors[]。

已实际view最终owner need-1440.png、handoff-390.png。前者显示真实客户表达/Provenance/原件入口；后者显示390待接管队列，无横溢。它不是接受后的视觉证明，接受由原HTTP状态和proof断言。Linux48e4465 owner2ea6b4a4e32f429192c2d1926a2ebfc1的quote-390.png/exact-cost-1440.png也实际view，批准/精确成本/quoted来源可读；PDF由原完整测试证明。

第二全量五Linuxowner核验记录backend-final-linux-owner-audit.json：全部精确PID/容器/网络/端口0。两个主动故障初code2/cleanupfalse仍保留，后续核验不倒写。I1后无运行中的owned资源。

## 保留限制

Mac完整quotation/自动寻源准入未配置，Linux与Mac拥有不同owner/Need；不合称单环境完整报价。DB stop/start可改变HostPort，不属四应用HUP透明恢复；pause/unpause只证同端点短断。联系人原内存限流与研究PG quota/持久调用账本分开。reply model计数不是全部模型计数；token/真实人工耗时未知。早期3/2warnings类别正文未保存；最终无warning不抹去历史。原9个Task10产物和6个被完整测试重生成的既有Catalog截图保留，后者未stage源码。所有真实外部Provider/邮箱/供应商为not_run，未部署/push/merge。

## 历史过程记录（以下保留各当时检查点，不覆盖上述最终事实）

实际日期：2026-09-06。BASE 6cdb40d8019d560d1490925df72a58d14f4881d6；首轮源码检查点1cbcdcf3d1bbd5ed088b87fff5329c263f6b731d；第二次完整门禁待最终冻结。
工作目录 /Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion。
子规格 docs/superpowers/specs/2026-09-06-web-core-final-acceptance.md；最终验收文档尚待完成。
所有测试命令前缀为 `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 TRADEOS_REQUIRE_E2E=1 .venv/bin/python3 -m pytest`。
以下是中间结果，不能累计为最终全量通过。

## 最初实施阶段的历史证据

- HTML void 集合按 WHATWG 13项完整补齐，保持原引用分段和保守拒绝。26个普通/自闭标签参数覆盖当前字段保留及跨引用不拼接。
- Supervisor.close补reply-model.sqlite四文件精确清理，先stop_apps；停止不确定时保留私有文件及原cleanup错误，不跨owner清理。
- Task9 Settings测试标题收窄至实际503读取失败语义。
- scanner四处已安全核对：test_context_builder.py三处为CredentialMarkerGuard合成输入，test_agent_worker.py一处为异常脱敏；均改显式placeholder且保留完整password marker，异常测试同步不回显断言。不改scanner。全仓scan exit0。
- 新sourcing绑定真实PG三例（成功、workflow_version不一致、Opportunity.need_id不一致）及原观测9项通过。此组是仓储读模型合成夹具，V2 admission/快照满足真实PG约束，绝不当成完整业务链或人类审批证据。
- 新Settings内部恢复测试：真实PG组织域candidate commit后在原workflow.start端口抛TransientError；HTTP503后版本1、approval为空、Run0；同actor/key/payload新API实例恢复202、重复回执完全相同、版本1/Run1。最终单项1passed。
- 新声明依赖环境 `pip install -e '.[dev]'` exit0。路径与结果见 output/acceptance/task12/install.json，公开pip配置且不读用户pip凭证配置。该环境的原launcher浏览器启动尚在首次验证。

## TDD及失败历史

1. `tests/unit/test_reply_current_evidence.py tests/unit/test_web_core_private_cleanup.py -q --tb=short`：生产修改前8failed/27passed。area/base/col/embed/source/track普通start标签造成evidence_available=False；正常停止漏reply-model文件，停止失败仍删mail/config。均为预期RED。
2. 同命令最小修改后35passed，0.35s。既有未闭合/Outlook后缀仍拒绝；旧分段回归保持。
3. `tests/unit/test_agent_worker.py tests/unit/test_context_builder.py tests/integration/test_web_core_observability.py -q --tb=short`：3failed/76passed/3warnings。新增合成SourcingCase缺state_changed_at，非生产RED。已补。
4. `tests/integration/test_web_core_settings_recovery.py tests/integration/test_web_core_observability.py -q --tb=short`：3failed/7passed/2warnings。Settings身份复用全局主键冲突；新V2 Run缺准入被真实guard拒绝。分别补新身份及合法read-model admission/snapshot夹具，未放宽guard。
5. 同命令：1failed/9passed，12.53s；Settings公开DTO实际version嵌套字段，测试KeyError。修正为version.playbook_version_id。
6. Settings单项同前缀最终1passed，8.33s。纯故障注入测试证明已有恢复能力，未改生产Settings。
7. 新E2E `tests/e2e/test_web_core_controlled.py -q --tb=short -rs` 首次正在执行；日志 output/acceptance/task12/e2e-first.log，未宣称通过。

## 最初检查点与资源边界（历史，已由后续记录更新）

A1新环境实际启动、A2研究+独立A3/A4触达回复浏览器、A5完整故障矩阵、A6当前权限/证据下载、A7最终源码原Linux链及寻源适用链、A8视觉、A9实际清理、A10边界证据与全量门禁尚待完成。
当时源码只改两个生产边界与具名测试；当前范围还含原受控研究/联系人装配和ADR0065。Task10九个既有output/playwright/t10-*保持。所有本批launcher只创建自身owner，pytest finally停止；未执行真实发送、部署、push/merge或读取真实凭证。中途曾因账户usage limit暂停，用户明确继续后恢复同任务；未兑换reset。

## 恢复后的实际进度（替代上文首次启动/仅两生产修改的早期状态）

新声明环境的原launcher已启动四进程及浏览器。新增生产范围包括原controlled研究接线和ADR0065联系人late-binding/Gateway插件装配；首轮完整门禁及其修复见后文，第二轮待最终冻结。

E2E第一轮18.68s因两处同名label的strict locator失败，改精确#boss-command。第二轮29.39s确认disabled，识别A2真实装配缺口。第三轮30.67s独立回复链推进后，错误期待当前owner销售员403。第四轮31.81s：原研究实际3 Signal/3 Hypothesis、研究阶段零Campaign/send；原回复链生成Need/Opportunity/Handoff且重启幂等，Account转移后仍assigned_to销售员的Handoff合法200，测试前提错误。改停用当前actor验证撤销。第五轮18.35s新增第二份独立触达政策审批时helper误取旧已应用审批；改精确待审批记录，未改审批行为。

联系人factory首个RED为缺contacts_factory参数TypeError；generic typed接缝实现后身份/互斥/未绑定/非法返回两项通过。联系人外部端口RED为模块缺失，GREEN证明每次verify调用计数而不按邮箱去重。

Settings数据库stop/start后原端点20次GET均500，固定诊断ConnectionRefusedError（原连接SELECT1同样失败），内部pg_isready成功。resources.py:255随机HostPort随独立容器stop/start可能变更，原配置不自动更新；不是API池问题。保留settings-db-recovery[-2].log与safe-diagnostic[-2].log。改exact-owner pause/unpause保持同端点，原start bounded取消后恢复，尚待GREEN。独立DB容器stop/start不属于应用HUP恢复契约；恢复需由原owner launcher受控重新启动依赖并重新生成配置，不能只重启容器沿旧端点继续。

## 本轮已完成门禁与当前全量

- 第七轮E2E：独立Campaign类别含空格，被原公开DTO拒绝；改为hinges。第八轮：实际未验证资格拒绝正确，fixture误捕ValidationError；改精确ContactNotEligibleError。第九轮1passed/32.36s，owner335a86cc07fc4d7092270b1d8767d809；真实enrich/verify各一次，原account_discovery落库并入组，发送一次，回复Need/Handoff、HUP重启/重放、原件下载、接受交接与当前actor停用均通过。cleanup.json零错误，私有SQLite quartet均消失。第九轮proof尚无联系人细项；这些新增安全字段将在最终全量新owner中生成，不倒写旧proof。
- 第六轮因发现另一个暂停DB测试未结束而主动SIGINT，20.43s，无完成测试；其finally完成自身清理。暂停DB测试的精确owner eb06532cd0be4c4a9ef1e0dda2fff6c5 与容器 b8cedf16541ccaf36280ba2753221ad385cd8a6b0471b0081703ad29c22c8f40 经标签核验后unpause，原pytest退出；不把这一非有界轮当作恢复通过。后续独立2秒解暂停、1秒触发取消组2passed/10.68s；整体取消可能等待2秒恢复，未声称整体1秒。新增计时/原端点探针断言纳入完整后端。
- 同源码A7原Linux浏览器、原已批PDF不发送集成、原Need寻源适用链3passed/131.60s。browser owner a79870b4f3714cd79d6211d061c5ab64 code0/cleanup_verified true/91.04s；integration owner8ad147dd94c84955aa1ba9de772dff88 code0/cleanup_verified true/26.24s。Mac与Linux不是同一Need/机会。
- 已实际view_image：第九轮need-1440.png和handoff-390.png；Linux a798…的quote-390.png和exact-cost-1440.png。页面可读、控件无横溢。Opportunity看板URL不消费opportunity_id参数，只记看板显示；精确Need绑定取实际API。need-sales-denied-390截图实际是Handoff撤销页，报告按真实页面解释。
- 新声明Python环境隔离probe Python3.12.14/isolated=true/catalog_dependency=false。Web全量411passed（32files），typecheck/lint/build/gen:api全部exit0；API生成无diff。实际lint112warnings/0errors，按每条blame全部早于计划branch base；App25/Outreach34/Billing10/Manual12/Product31，见lint-attribution.json。
- boundaries/scan exit0；mypy562files通过；ruff首次2个本批格式提示修复后全仓exit0。diff-check通过，AppleDouble仅计stderr（status4、API diff2、diff-check44行），未修共享git。
- 当前冻结24个源/测试文件摘要fa21469003d1391487e0f093c33c1adf046bca1d44d4c3037ff0e3f7421b055d。完整后端正在串行执行，已有早期失败但尚未最终汇总，不累计局部通过为最终全过。

## 完整后端检查点与修复进展（2026-09-06）

源码检查点 `1cbcdcf3d1bbd5ed088b87fff5329c263f6b731d` 精确保存此前24源/测试的冻结哈希；本轮完整后端 **98 failed/9217 passed**，pytest2076.35s，wrapper2083.9s，exit1。完整98项安全索引为 `output/acceptance/task12/backend-failure-index.json`。该轮不通过，后续局部数不与9217累加。controller要求由于组合/顺序疑点，在修复后最终冻结版本再次完整后端。

全量主链owner `39ea7630eb12472eb7b0686ccf3cd52a`：3Signal/3Hypothesis，contact.enrich/verify各1；Run/point/verification_provider/status、未验证拒绝、send1在其proof明确，cleanup stopped/errors[]。`model_calls=1`仅为ControlledReplyModelClient计数，不是整条研究/联系人/TradeManager总数。已实看该owner Need1440；原截图标题不代替实际状态断言。

全量两Linux owner5576a5f6fb57458896f3756f093d7424、2ce63f77eb394cf59cba2b39d03fdda8最初cleanup_unknown/code2保留。后续只读精确owner审计为PID+birth0、容器0、网络0、loopback端口全部关闭；无删除动作，见linux-failed-owner-audit.json。第一owner failure-boss实看为成本只读连接失败；正在核原8槽relay是否有未收敛请求，不扩大并发或预算。

已确认最小测试兼容修正：当前head0059（旧版本downgrade目标不改），迁移52项70.96s通过；全量往返失败的共享前置仍不能全归因于head。Catalog固定NOW早于PG Run.created_at导致7日期限精确绑定正确拒绝；仅首次_start读Run持久时间作为测试clock基线，6passed/10.84s，保留processed3/状态/审批断言。quotation fixture补email_inbound/model_lifecycle/object_store_lifecycle及原cleanup失败抛错契约；权限矩阵补INBOUND_BIND boss-only；API枚举补11个既有路径与reconcile安全422；研究内存fixture补真实当前employee端口形状与user映射；Sourcing恢复fixture处于真实public_search步骤。最终该unit组130passed/9.58s。

旧回复：RawArtifactStore未绑定新bounded_transport，reader.py95正确failclosed；旧真实MinIO fixture补S3Bounded读取，内存外部blob实现有界协议，不改生产reader。旧纠正调用补当前boss身份与真实员工基础行；状态列表按created_at/run_id排序。47passed/45.10s。旧driver沿当前enabled组合测试真实未匹配邮件进入待核对/重启保留；未绑定API状态仍disabled，未虚构unbound。后续浏览器兼容组5passed/1failed38.89s，其中唯一失败为新增诊断误用ROOT而该模块为_REPO_ROOT，已修并重跑；原HTTP问题仍待诊断。

补未授权研究验收：confirm200只为受理，未active Playbook/国家政策下原Workflow failed，search/page/research-model0、Signal/Hypothesis/Campaign/send0；独立审批后再走真实全链。研究usage/search/page/model操作名逐次记原owned mail.sqlite独立tenant表。调用表创建先于Gmail初始化导致原list_calls查询不存在provider_calls，E2E与unit均RED；精确sqlite_master表存在检查后空账本返回0，坏列仍抛错，不catch数据库错误。该新增受控证据装配与负例将在最终版本重跑主链。

## 最后聚焦兼容与组合隔离

首轮全量后兼容修复保持当前actor、显式API契约、INBOUND_BIND矩阵与bounded artifact读取；没有放宽生产授权。Catalog测试时钟仅在每个独立场景首次start后以持久Run.created_at为基线，保留7日、processed==3和唯一审批。

Linux短unit probe公开GET/确认成功，结束时STARTED但cycles0；原fixture未等待首轮循环就发布manifest。现通过原公开wait首调用建立真实ready事件，保留退出cycles>0、interval/stop和外层总启动deadline，worker提前返回或异常立即暴露。未证实relay饱和，不改变8槽容量或预算。fixture编辑曾残留重复finally导致SyntaxError：browser-focused-green-2为1failed/1passed47.03s，后者是slice4通过；linux-ready-diagnostic为1failed5.61s。owner be4d30da7698406eaa4d2ef32563ae50、ae990fdd3a2545d4adb58d01f146ba65初cleanup_unknown，后按PID出生/label/端口核0（linux-ready-owner-audit.json、linux-ready-syntax-owner-audit.json）。另一次不存在test名collection exit4，无owner。修正后ruff语法检查通过才启动聚焦。

迁移组合RED按整个test_current_outreach_facts.py后employees downgrade0002：1failed/17passed7.46s。固定RaiseError/0052 refuses to drop sourcing admission base evidence，见migration-predecessor-safe.json。具体前置是公开submit_discovery_proposal写base_directive_version，不可变提案不得DELETE。仅该HTTP actor映射例复用已有unit_engine UUID独立库，全部config/sessions取该engine，沿原finally dispose→精确DROP；不清共享数据、不关闭触发器或改0052。旧revision往返保留。

最终聚焦按同顺序current_outreach_facts整文件→employees downgrade0002→Linux initial-unit probe→账本坏列/损坏DB反例，23passed/63.10s，final-focused-green.log。slice4精确/email-inbound/status 503×2与prepare409×1，并验证读取失败可见、无假绑定成功；任何额外失败拒绝。

proof.model_calls仅ControlledReplyModelClient调用，不代表研究/联系人/TradeManager全部模型调用。研究逐次调用记在同owner mail.sqlite独立表；Gmail仅在provider_calls表不存在时返回空，坏列/损坏DB仍报错。总模型调用与计费token未知，不编造0。

研究 quota 使用原 PostgreSQL 持久额度；联系人秒/分钟限流沿用原 Hunter 组合的 InMemoryHunterQuotaGuard，它不是跨重启持久额度。联系人 ToolCall ledger 与外部逐次调用表持久化不能被混称为该限流器持久化。早期两组3/2 warnings仅保存数量，类别正文未保存；最终一轮有无告警按实际输出登记，不抹去早期数量。
