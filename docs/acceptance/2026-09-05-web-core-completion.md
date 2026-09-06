# Web 核心受控验收

实际日期：2026-09-06。Task12实施及独立限定复审双Approved；不代表Task13或最终全分支审查已完成。
安全报告、裁定及代表图的持久交付见[正式索引](web-core-delivery/README.md)。
BASE：`6cdb40d8019d560d1490925df72a58d14f4881d6`。

## 版本与实际门禁

| 版本 | 本轮结果 | 边界 |
| --- | --- | --- |
| `1cbcdcf3d1bbd5ed088b87fff5329c263f6b731d` | 首轮完整后端98 failed / 9217 passed；pytest2076.35s、wrapper2083.9s | 历史失败检查点，未通过；98条完整索引保留 |
| `48e4465fc307212e794d6ed87501cb418f74d245` | 第二完整后端9318 passed / 0 failed / 0 skipped / 0 warning entries；pytest2132.64s、wrapper2139.76s | 1630源码文件在整轮前后SHA256差异0；包含迁移组合与原Linux全链 |
| `7e10383c253df4a98cd224fb7ee526d721476f9a` | 独立审查I1修复后完整Mac主链1 passed / 34.97s；边界、scan、受影响ruff通过 | 仅2测试文件修正真实审批actor及绑定断言；生产实现、共享fixture、迁移未变。按controller裁定不重复第三次full |

没有把局部通过相加成完整结果。第二全量命令是 `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 TRADEOS_REQUIRE_E2E=1 .venv/bin/python3 -m pytest -q -rs --tb=short`，数据库独占串行。结果见 `output/acceptance/task12/backend-final.json`、`backend-final-safe-summary.json`、`final-source-snapshot.json`；修复证据见 `i1-source-commit.json`、`i1-static-gates.json`。

第二全量前boundary/scan/ruff/mypy全部exit0，mypy562文件。Web源码没有后续变动：完整Web411 passed（32文件），typecheck/build/gen:api通过，API生成无diff。ESLint112 warnings/0errors；逐行git blame归属早于本计划branch base：App25、OutreachWorkbench34、BillingUnavailable10、ManualOperations12、ProductSupplyCenter31，见lint-attribution.json。Web本轮无VueRouter R0004或Vue warnings日志；不回填历史提示。早期两组3/2 warnings只保留数量，类别未保存，不能据最终无warning抹去历史。

## 实际入口与实现范围

Mac完整链反向消费唯一原 `scripts/run_web_core_controlled.py`，启动API、scheduler、notification、Web四进程。新声明Python环境实际安装 `.[dev]`、Python3.12.14；隔离探针确认未借用旧Catalog工作树。Node24.15.0在独立目录/独立空npm配置实际npm ci与build，见installation-isolation.json及node-isolated-final.json。初次两个npm配置都指向/dev/null被double-loading拒绝，保留为工具装配历史，不算应用失败。

本批最小生产修改包括完整13项HTML void保守引用处理、owner停止后精确SQLite生命周期、原受控研究接线、[ADR0065联系人late binding](../adr/0065-late-bound-contact-runtime-ports.md)及受控外部逐次调用账本。核心域、Gateway检查、Workflow、Outbox、审批、tenant/当前actor和Decimal计算保持真实。模型和Agent不接触真实凭证；合成外部端口只接受具名fixture，未知输入与公网拒绝。没有seed Message/Need/Opportunity或批准结果。

研究quota使用原PG持久额度；联系人秒/分钟限流沿原InMemoryHunterQuotaGuard，不是跨重启持久额度。持久ToolCall和Provider调用账本不等同限流器持久化。真实Hunter就绪语义未改，controlled single provider与synthetic exclusive研究账户均不是现实供应商账户。

## A1–A10

最终Mac主链owner为 `8d234c0956fb498e9f171342c3b72f51`（修复SHA7e10383）；下表省略路径前缀为 `output/acceptance/task12/`。Linux独立链在48e4465完整门禁内验证，owner及业务对象与Mac不同。

| 项 | 实际证据与断言 | 来源 |
| --- | --- | --- |
| A1 | 新声明依赖环境启动原四进程，缺Node/端口占用/迁移故障/未持锁不ready回归 | installation-isolation.json、node-isolated-final.json；原launcher/runtime测试在9318项中 |
| A2 | 无活跃政策时confirm200只排队，真实Run failed，外部calls[]与Signal/Hypothesis/Campaign/send全0；Playbook和KE政策各由另一老板批准后研究产生3 Signal/3 Hypothesis，阶段无联系人发现/入组/发送 | 最终owner/research-unconfigured.json、research-confirmation.json、proof.json |
| A3 | 独立人工输入触达任务；实际Campaign提交人自批HTTP400且仍pending，另一当前老板经原HTTP批准精确包；原account_discovery v2经单Provider enrich→verify→持久verified→入组→HTTP send；未验证负例拒绝 | 最终proof.campaign_approval、contact_run_id、contact_point_id、verification_provider；enrich/verify/send各1 |
| A4 | 合成MIME经原Gateway/Artifact/ingest/Outbox/回复识别生成Need/Opportunity/Handoff；来源与邮件原件可读；真人接受交接 | 最终proof与message.eml，原reply_completion作用组与完整门禁 |
| A5 | 同消息重放/HUP后一份业务结果且一次send；未知send在真实Provider动作后ack前，按每次Provider调用计数；Settings candidate commit→Run start故障后同actor/key/payload恢复；同端点PG短断恢复 | 最终proof；test_reply_completion未知发送；test_web_core_settings_recovery两故障分支在完整门禁中 |
| A6 | Need/Handoff/Run/原件均按当前actor；停用员工后旧内容清除、真实HTTP拒绝；原boss/manager/sales权限矩阵 | 最终撤销断言；inbox_access/email_inbound_access/web_core_observability真实PG门禁 |
| A7 | 原独立Linux公开回复→来源/单位→Decimal成本→独立审批→PDF；Need→既有Sourcing→estimated cost适用链另列。Mac成本503不是此证明 | Linux owner2ea6b4a4e32f429192c2d1926a2ebfc1完整浏览器；f9aa1aec503442a08e00d81fc8ea5801完整integration。原3目标在第二完整门禁通过 |
| A8 | Web全量状态恢复；实际view最终Need1440/Handoff390、Linux quote390/exact-cost1440，关键控件/来源可读，无横向溢出 | 最终owner截图及Linux目录；accepted状态由真实断言/proof证明，待接管截图不冒称接受后 |
| A9 | 正常/HUP/故障停止先核owner并停进程，精确删除mail和reply-model四文件；停止不确定保留私有文件。最终Macstopped/errors[]；第二全量五Linuxowner最终PID/容器/网络/端口均0 | 最终cleanup.json、backend-final-linux-owner-audit.json；两个主动故障初code2/cleanupfalse与后续0分开记录 |
| A10 | 具名search/page/model/contact允许清单及未知反例；Gmail表尚未初始化返回0，已有坏列/坏DB仍报错，不能吞错误伪造调用数 | controlled端口unit、原Gateway/allowlist，均包含于完整门禁；真实外部能力not_run |

最终I1审批对象：Campaign `cmp_01M1V7H2KYG3YR28747K9S2DVE` / v1，Approval `apr_01M1V7H2PMCX7DXRRCA503FK91`。proof中created_by=submitter=proposed_by，decided_by=campaign_approved_by且与提交人不同；self_decision_status=400、state_after_self_denial=pending，随后approved。修复前两轮RED分别20.33s、20.57s，后者固定记录自批实际返回200；owner631e187e57144938a1c9d4baf30ea341、fe99b028cb53465cb6d6780460eb84ee均正常清理。此前包括48e4465主链在内的“独立Campaign审批”声明撤回，由本次真实修复证据替代，不改生产legacy审批合同。

最终proof的model_calls=1只计ControlledReplyModelClient；研究调用另为usage3/search3/page3/model1。TradeManager/联系人等全链总模型调用、真实计费token和人工处理工时未知，不填0或编造效率改进。

## 平台与证据限制

Mac原入口未配置完整quotation/自动寻源准入，保留诚实503/未配置状态；Linux遵守原parser资源probe和固定网络/生命周期，不增加统一launcher或跨网络桥。Linux固定业务时钟不作为实际耗时。图中PDF入口/批准状态不是PDF内容证明，下载与解析由原完整测试断言。

OpportunityList不消费opportunity_id查询参数，相关截图只算看板展示，精确Need绑定来自真实API；名为need-sales-denied的截图实际仍是Handoff页，不据标题虚称Need页面。所有输入是合成演练，不代表真实买家意愿、供应商报价、真实邮箱可达性或生产账户就绪。

DB独立stop/start可改变随机HostPort，原配置不自动更新；原端点ConnectionRefused失败不应归因API池。HUP只覆盖原四应用，不能承诺DB换端点透明恢复。A5用同owner pause/unpause与独立2秒恢复计时保持端点，finally收敛恢复任务/就绪检查。历史首次取消等待暂停PG、一次误重叠启动和精确主动清理保留，不能倒写成功。

首轮98失败、所有中间fixture错误、初cleanup_unknown及后续精确核验保留在Task12报告与安全索引；不回显原始敏感日志。共享.git AppleDouble只记录stderr行数不维修。Task10原9个未跟踪产物保留；全量重生成的6个既有Catalog截图未混入Task12源码提交；Task13逐张查看并安全归档后，
仅此六原路径恢复至Task13 BASE，hash与复制关系见正式索引，其他历史output未动。没有真实外发、供应商接触、部署、push或merge。

## 首轮98项失败逐项映射（第二全量已通过）

首轮完整结果保持98 failed/9217 passed；以下修复均由48e4465的第二完整门禁实际覆盖通过；作用组数不相加。迁移组合原因只在具名顺序复现，未把全部历史异常倒推为同因。完整安全索引为output/acceptance/task12/backend-resolution-index.json。

| 组 | 最小处理 | 聚焦证据 |
| --- | --- | --- |
| linux-browser | Linux成本刷新曾连接失败；定向完整链后通过，未证实relay饱和，保留组合风险由第二全量验证 | browser-remaining-diagnostic.log：真实完整链通过 |
| linux-ready | 原fixture尚未完成首循环即yield；公开wait首次调用事件等待真实完成，保留cycles>0 | final-focused-green.log：23 passed |
| legacy-browser | 旧fixture补当前公开契约的bounded artifact/当前actor；真实浏览器 | browser-compat.log（最终实际日志见逐项字段） |
| slice4 | 旧fixture无入站端口，精确503×2/prepare409×1与可见错误，不屏蔽其它失败 | browser-focused-green-2.log：slice4 passed |
| catalog-clock | 持久Run.created_at与旧固定测试时钟不一致；仅每个独立场景建立持久时钟基线 | catalog-compat-green.log：6 passed |
| head | 当前head断言0058→0059；旧定向revision往返/降级拒绝语义保持 | migration-compat.log：52 passed |
| migration | 组合前置不可变提案导致0052正确拒绝；仅具名HTTP前置例使用已有unit_engine。各旧迁移单独作用组通过，首轮各错误不倒推为同因 | migration-compat.log：52 passed；final-focused-green.log：同顺序组合23 passed |
| actor | 纠正入口补真实当前员工actor，保留实际权限 | reply-compat-green.log：47 passed |
| inbound | 旧disabled scheduler预期过期；现真实启用收取并review，未绑定状态仍disabled | browser-compat.log（最终实际日志见逐项字段） |
| reply | 旧RawArtifactStore fixture补实际bounded_transport，保留生产读取护栏 | reply-compat-green.log：47 passed |
| reply-order | SQL结果按created_at/run_id稳定排序，保留精确Run终态 | reply-compat-green.log：47 passed |
| research-negative | 受控研究现在接线；confirm200只排队，无活跃政策实际Run failed且search/page/model0、业务副作用0，审批后才研究 | research-unconfigured-chain.log：旧launcher负例passed；controlled-negative-driver.log：主链passed |
| api-schema | 契约快照补现有API路径和公开422，不移除旧契约 | compat-unit-4.log：130 passed |
| lifecycle | 旧SimpleNamespace补显式runtime资源字段并适配已批准cleanup异常契约 | compat-unit-4.log：130 passed |
| research-actor | 预览和API旧fixture补当前员工映射/员工服务，未放宽生产权限 | compat-unit-4.log：130 passed |
| permissions | 显式权限矩阵补INBOUND_BIND，只有boss允许 | compat-unit-4.log：130 passed |
| sourcing | uncertain恢复例使用实际active public_search Run步骤，保持公开恢复语义 | compat-unit-4.log：130 passed |

| # | 原失败节点 | 组 |
| --- | --- | --- |
| 1 | `tests/e2e/test_costing_quote_browser.py::test_real_costing_quote_browser` | linux-browser |
| 2 | `tests/e2e/test_costing_quote_browser.py::test_real_initial_unit_read_probe` | linux-ready |
| 3 | `tests/e2e/test_phase1_browser.py::test_phase1_browser_visible_reply_to_handoff_chain` | legacy-browser |
| 4 | `tests/e2e/test_research_browser.py::test_research_confirm_refresh_and_radar_tabs_are_operable_across_origins[1280-900]` | legacy-browser |
| 5 | `tests/e2e/test_research_browser.py::test_research_confirm_refresh_and_radar_tabs_are_operable_across_origins[390-844]` | legacy-browser |
| 6 | `tests/e2e/test_slice4_manual_send.py::test_slice4_manual_send_fixed_journey` | slice4 |
| 7 | `tests/integration/test_catalog_policy_workflow.py::test_real_workflow_is_visible_in_central_queue_then_applies_and_projects_redacted_notification` | catalog-clock |
| 8 | `tests/integration/test_catalog_policy_workflow.py::test_response_loss_after_each_committed_boundary_converges_without_duplicates[catalog_product_policy.submit]` | catalog-clock |
| 9 | `tests/integration/test_catalog_policy_workflow.py::test_response_loss_after_each_committed_boundary_converges_without_duplicates[catalog_product_policy.apply]` | catalog-clock |
| 10 | `tests/integration/test_catalog_policy_workflow.py::test_response_loss_after_each_committed_boundary_converges_without_duplicates[catalog_product_policy.mark_applied]` | catalog-clock |
| 11 | `tests/integration/test_catalog_policy_workflow.py::test_rejection_and_timeout_are_durable_terminal_without_activation` | catalog-clock |
| 12 | `tests/integration/test_catalog_product_migration.py::test_0057_is_the_only_script_head` | head |
| 13 | `tests/integration/test_catalog_product_migration.py::test_0057_downgrade_refuses_catalog_approval_before_any_ddl` | head |
| 14 | `tests/integration/test_current_outreach_facts.py::test_reply_unknown_auto_history_and_current_correction` | actor |
| 15 | `tests/integration/test_email_inbound_driver.py::test_owned_api_scheduler_disabled_binding_restart` | inbound |
| 16 | `tests/integration/test_employees_repositories.py::test_roundtrip_downgrade_0002_then_upgrade_head` | migration |
| 17 | `tests/integration/test_human_handoff_workflow.py::test_0007_roundtrip_exact_schema_and_append_only` | migration |
| 18 | `tests/integration/test_migrations.py::test_0052_sourcing_admission_base_version_roundtrip_and_guard` | migration |
| 19 | `tests/integration/test_migrations.py::test_costing_quote_evidence_0041_roundtrip_only_adds_four_tables` | migration |
| 20 | `tests/integration/test_migrations.py::test_roundtrip_downgrade_base_then_upgrade_head` | migration |
| 21 | `tests/integration/test_migrations.py::test_sending_identity_roundtrip_0008_0007_0008` | migration |
| 22 | `tests/integration/test_migrations.py::test_0009_outreach_schema_and_roundtrip` | migration |
| 23 | `tests/integration/test_migrations.py::test_0010_tool_call_schema_is_safe_tenant_scoped_and_roundtrips` | migration |
| 24 | `tests/integration/test_migrations.py::test_0011_outreach_send_claim_roundtrip_and_state_guard` | migration |
| 25 | `tests/integration/test_migrations.py::test_0012_email_feedback_schema_and_roundtrip` | migration |
| 26 | `tests/integration/test_migrations.py::test_0013_receipt_fingerprint_schema_and_roundtrip` | migration |
| 27 | `tests/integration/test_migrations.py::test_artifact_store_0014_roundtrip_and_guards` | migration |
| 28 | `tests/integration/test_migrations.py::test_0015_notification_jobs_roundtrip` | migration |
| 29 | `tests/integration/test_migrations.py::test_0016_authentication_check_requests_roundtrip_and_guards` | migration |
| 30 | `tests/integration/test_migrations.py::test_0017_email_complaints_schema_and_roundtrip` | migration |
| 31 | `tests/integration/test_migrations.py::test_0018_conversation_classifications_roundtrip_and_guards` | migration |
| 32 | `tests/integration/test_migrations.py::test_0024_contact_verification_observation_revision_present` | migration |
| 33 | `tests/integration/test_migrations.py::test_0020_classification_corrections_contract_matches_orm` | migration |
| 34 | `tests/integration/test_migrations.py::test_0020_classification_corrections_downgrade_roundtrip` | migration |
| 35 | `tests/integration/test_migrations.py::test_0019_conversations_messages_roundtrip_and_guards` | migration |
| 36 | `tests/integration/test_migrations.py::test_0021_demand_signals_contract_matches_orm` | migration |
| 37 | `tests/integration/test_migrations.py::test_0021_demand_signals_downgrade_roundtrip` | migration |
| 38 | `tests/integration/test_migrations.py::test_0022_need_hypotheses_contract_matches_orm` | migration |
| 39 | `tests/integration/test_migrations.py::test_0022_downgrade_roundtrip` | migration |
| 40 | `tests/integration/test_migrations.py::test_0024_prospecting_contract_matches_orm` | migration |
| 41 | `tests/integration/test_migrations.py::test_0024_verification_observation_downgrade_roundtrip` | migration |
| 42 | `tests/integration/test_migrations.py::test_0023_downgrade_roundtrip` | migration |
| 43 | `tests/integration/test_migrations.py::test_0031_company_playbook_contract_matches_orm` | migration |
| 44 | `tests/integration/test_migrations.py::test_0031_company_playbook_downgrade_roundtrip` | migration |
| 45 | `tests/integration/test_migrations.py::test_0032_country_policy_contract_matches_orm` | migration |
| 46 | `tests/integration/test_migrations.py::test_0032_country_policy_downgrade_roundtrip` | migration |
| 47 | `tests/integration/test_migrations.py::test_0034_snapshot_artifact_backfill_downgrade_upgrade_roundtrip` | migration |
| 48 | `tests/integration/test_migrations.py::test_0035_reply_field_evidence_roundtrip_matches_orm` | migration |
| 49 | `tests/integration/test_migrations.py::test_0036_reply_scope_and_owner_work_roundtrip_match_orm` | migration |
| 50 | `tests/integration/test_migrations.py::test_0037_enrollment_source_hypothesis_roundtrip_matches_orm` | migration |
| 51 | `tests/integration/test_migrations.py::test_0038_prospect_account_field_provenance_roundtrip_matches_orm` | migration |
| 52 | `tests/integration/test_migrations.py::test_0034_snapshot_artifact_backfill_missing_match_fails_closed` | migration |
| 53 | `tests/integration/test_migrations.py::test_0034_snapshot_artifact_backfill_ambiguous_match_fails_closed` | migration |
| 54 | `tests/integration/test_notification_dedup.py::test_0006_upgrade_creates_notification_deliveries` | migration |
| 55 | `tests/integration/test_notification_dedup.py::test_0006_unique_tenant_dedup_channel` | migration |
| 56 | `tests/integration/test_notification_dedup.py::test_0006_downgrade_removes_table_roundtrip` | migration |
| 57 | `tests/integration/test_outbox_delivery.py::test_0005_upgrade_adds_outbox_delivery_columns` | migration |
| 58 | `tests/integration/test_outbox_delivery.py::test_0005_upgrade_creates_outbox_deliveries_table` | migration |
| 59 | `tests/integration/test_outbox_delivery.py::test_0005_upgrade_status_check_allows_dead` | migration |
| 60 | `tests/integration/test_outbox_delivery.py::test_0005_upgrade_guard_allows_delivery_fields` | migration |
| 61 | `tests/integration/test_outbox_delivery.py::test_0005_upgrade_adds_unique_tenant_event` | migration |
| 62 | `tests/integration/test_outbox_delivery.py::test_outbox_deliveries_unique_tenant_event_handler` | migration |
| 63 | `tests/integration/test_outbox_delivery.py::test_outbox_deliveries_composite_fk_tenant_isolation` | migration |
| 64 | `tests/integration/test_outbox_delivery.py::test_0005_downgrade_restores_0002_semantics` | migration |
| 65 | `tests/integration/test_outbox_delivery.py::test_0005_downgrade_maps_dead_rows_to_pending` | migration |
| 66 | `tests/integration/test_phase1_closed_loop.py::test_phase1_postgres_closed_loop_is_durable_tenant_bound_and_replay_safe` | reply |
| 67 | `tests/integration/test_reply_completion.py::test_missing_need_is_completed_by_later_reply_across_runtime_restart[True]` | reply-order |
| 68 | `tests/integration/test_scheduler_reply_trigger.py::test_inbound_stored_starts_reply_run_and_applies_actions` | reply |
| 69 | `tests/integration/test_scheduler_reply_trigger.py::test_reply_trigger_consumer_redelivery_is_idempotent` | reply |
| 70 | `tests/integration/test_scheduler_reply_trigger.py::test_reply_trigger_does_not_retrigger_on_reply_received` | reply |
| 71 | `tests/integration/test_web_core_launcher.py::test_real_http_configuration_approval_and_worker_crash` | research-negative |
| 72 | `tests/unit/test_api_app.py::test_import_and_zero_arg_factory_do_not_create_database_resources` | api-schema |
| 73 | `tests/unit/test_api_app.py::test_factory_openapi_matches_s3_15_crm_runtime_contracts` | api-schema |
| 74 | `tests/unit/test_quotation_lifecycle.py::test_actual_api_lifespan_closes_quotation_before_database_even_before_yield[None-None]` | lifecycle |
| 75 | `tests/unit/test_quotation_lifecycle.py::test_actual_api_lifespan_closes_quotation_before_database_even_before_yield[None-schema]` | lifecycle |
| 76 | `tests/unit/test_quotation_lifecycle.py::test_actual_api_lifespan_closes_quotation_before_database_even_before_yield[None-probe]` | lifecycle |
| 77 | `tests/unit/test_quotation_lifecycle.py::test_actual_api_lifespan_closes_quotation_before_database_even_before_yield[None-cancel]` | lifecycle |
| 78 | `tests/unit/test_quotation_lifecycle.py::test_actual_api_lifespan_closes_quotation_before_database_even_before_yield[None-body]` | lifecycle |
| 79 | `tests/unit/test_quotation_lifecycle.py::test_actual_api_lifespan_closes_quotation_before_database_even_before_yield[ordinary-None]` | lifecycle |
| 80 | `tests/unit/test_quotation_lifecycle.py::test_actual_api_lifespan_closes_quotation_before_database_even_before_yield[ordinary-schema]` | lifecycle |
| 81 | `tests/unit/test_quotation_lifecycle.py::test_actual_api_lifespan_closes_quotation_before_database_even_before_yield[ordinary-probe]` | lifecycle |
| 82 | `tests/unit/test_quotation_lifecycle.py::test_actual_api_lifespan_closes_quotation_before_database_even_before_yield[ordinary-cancel]` | lifecycle |
| 83 | `tests/unit/test_quotation_lifecycle.py::test_actual_api_lifespan_closes_quotation_before_database_even_before_yield[ordinary-body]` | lifecycle |
| 84 | `tests/unit/test_quotation_lifecycle.py::test_actual_api_lifespan_closes_quotation_before_database_even_before_yield[cancel-None]` | lifecycle |
| 85 | `tests/unit/test_quotation_lifecycle.py::test_actual_api_lifespan_closes_quotation_before_database_even_before_yield[cancel-schema]` | lifecycle |
| 86 | `tests/unit/test_quotation_lifecycle.py::test_actual_api_lifespan_closes_quotation_before_database_even_before_yield[cancel-probe]` | lifecycle |
| 87 | `tests/unit/test_quotation_lifecycle.py::test_actual_api_lifespan_closes_quotation_before_database_even_before_yield[cancel-cancel]` | lifecycle |
| 88 | `tests/unit/test_quotation_lifecycle.py::test_actual_api_lifespan_closes_quotation_before_database_even_before_yield[cancel-body]` | lifecycle |
| 89 | `tests/unit/test_research_api.py::test_api_confirmation_gate_is_enforced_before_start[None-True-200]` | research-actor |
| 90 | `tests/unit/test_research_api.py::test_api_confirmation_gate_is_enforced_before_start[record1-True-200]` | research-actor |
| 91 | `tests/unit/test_research_api.py::test_api_confirmation_gate_is_enforced_before_start[record2-True-200]` | research-actor |
| 92 | `tests/unit/test_research_ui_preview.py::test_confirmed_without_run_recovers_by_read_then_explicit_same_key` | research-actor |
| 93 | `tests/unit/test_research_ui_preview.py::test_controlled_preview_confirm_result_and_run_flow` | research-actor |
| 94 | `tests/unit/test_sending_identity_permissions.py::test_phase1_matrix_allows_only_the_explicit_actions[boss-TENANT]` | permissions |
| 95 | `tests/unit/test_sending_identity_permissions.py::test_phase1_matrix_allows_only_the_explicit_actions[manager-MANAGER]` | permissions |
| 96 | `tests/unit/test_sending_identity_permissions.py::test_phase1_matrix_allows_only_the_explicit_actions[system-SYSTEM]` | permissions |
| 97 | `tests/unit/test_sending_identity_permissions.py::test_phase1_matrix_allows_only_the_explicit_actions[sales-SELF]` | permissions |
| 98 | `tests/unit/workflows/test_sourcing_plan.py::test_current_quota_and_uncertain_recovery_are_safe_application_projections` | sourcing |

研究 quota 使用原 PostgreSQL 持久额度；联系人秒/分钟限流沿用原 Hunter 组合的 InMemoryHunterQuotaGuard，它不是跨重启持久额度。联系人 ToolCall ledger 与外部逐次调用表持久化不能被混称为该限流器持久化。早期两组3/2 warnings仅保存数量，类别正文未保存；最终一轮有无告警按实际输出登记，不抹去早期数量。
