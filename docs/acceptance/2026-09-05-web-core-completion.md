# Web 核心同版本受控验收

实际验收日期：2026-09-06。Task12，待独立审查；不是 Task13 总交付声明。
BASE：`6cdb40d8019d560d1490925df72a58d14f4881d6`。本批24个源/测试文件的冻结内容摘要：`fa21469003d1391487e0f093c33c1adf046bca1d44d4c3037ff0e3f7421b055d`，逐文件 SHA256 见 `output/acceptance/task12/source-snapshot.json`。

## 验收边界与实际入口

Mac 完整链只从原 `scripts/run_web_core_controlled.py` 启动 API、scheduler、notification、Web 四进程。新声明环境实际安装 `.[dev]` 并启动该入口，Python3.12.14、Node24.15.0；隔离探针确认没有旧 Catalog 工作树依赖。PostgreSQL、MinIO、核心域、Gateway、Workflow、Outbox、审批、权限及预算均真实。合成端口只提供具名 DNS、邮件、公开研究页面/搜索、联系人与模型外部响应，网络边界拒绝未知地址，无真实供应商联系、真实邮箱/模型/联系人 Provider 或付费来源调用。

本批发现并修正原受控入口遗漏的研究与联系人装配。研究使用原 ResearchRuntimePorts、真实持久 quota/Artifact/Gateway；联系人按 [ADR0065](../adr/0065-late-bound-contact-runtime-ports.md) 在本 runtime 的 canonical Outreach 形成后借用同一 core/session，注册原 manifest/check/handler。外部合成联系人的验证结果由原 VerifyContactsStep 经 Prospecting 落库；没有直接写已验证结果。真实 Hunter 就绪判断保持不变，合成账户不是现实供应商账户。

## A1–A10 证据

| 项 | 已执行入口与断言 | 安全证据 |
| --- | --- | --- |
| A1 | 新声明 Python 环境安装并启动原四进程；原缺 Node/端口占用/迁移故障/未持锁不 ready 回归 | `install.json`、`installation-isolation.json`；原 `test_web_core_launcher.py`、`test_web_core_runtime.py` |
| A2 | 浏览器创建 Playbook 与 KE 政策，各由另一老板真实审批；确认老板研究指令产生3 Signal/3 Hypothesis，研究阶段无 Campaign、无联系人调用、无发送 | 第九轮主链 owner `335a86cc07fc4d7092270b1d8767d809` 下 `research-confirmation.json`、`proof.json`、研究截图 |
| A3 | 另一个明确人工输入的企业/需求假设和独立 Campaign 审批；未验证联系人被拒；原 account_discovery v2 经单 Provider enrich→verify→持久验证→归属→精确版本入组→原 HTTP 发送，一次发送 | `test_web_core_controlled.py` 反向消费原入口，`web_core_contacts.py` 只调用公开域/Workflow；最终全量新 proof 记录 Run、联系点、来源及逐次调用 |
| A4 | 合成 MIME 经原 Gateway、Artifact、ingest、Outbox、回复识别形成真实 Need/Opportunity/Handoff；原字段来源/邮件下载可读；真人接受交接 | 主链 `proof.json`、`message.eml`、Need/Handoff 1440/390截图；`test_reply_completion.py` |
| A5 | 同消息重放、原四进程 HUP 后一份交接且一次发送；Provider 动作后 ack 前未知响应按每次真实调用计数；candidate commit→Run start故障及同actor/key/payload恢复；DB暂停后原端点恢复 | `test_reply_completion.py::test_unknown_provider_send_is_reconciled_without_a_second_send`；`test_web_core_settings_recovery.py`；原审批/Campaign workflow 回归 |
| A6 | Need、Handoff、Run、原件均按当前 actor 检查；真实停用 actor 后旧内容清除且入口拒绝。boss/manager/sales 的列表、附件及纠正矩阵由原真实PG测试覆盖 | 主链撤销断言；`test_inbox_access.py`、`test_email_inbound_access.py`、`test_web_core_observability.py` |
| A7 | 独立原 Linux 公开回复→来源/单位→Decimal成本→独立审批→PDF链；原 Need→Sourcing→estimated cost适用链分开验收；无quoted且无风险接受时拒绝正式报价 | `a7-final-source.log`：3 passed/131.60s；Linux browser owner `a79870b4f3714cd79d6211d061c5ab64`、integration owner `8ad147dd94c84955aa1ba9de772dff88` |
| A8 | Web全量411项验证等待/失败/未知/暂停/stale/queued；实际看过主链Need1440、Handoff390和Linux报价390/精确成本1440，关键控件可读，无横溢 | `web-test-gate.log`，主链与Linux截图；机会页截图仅证明看板展示，精确Need绑定来自真实API |
| A9 | 原owner PID+出生时间、容器label+ID核验；正常/HUP/子进程故障停止后精确删除mail与reply-model SQLite及侧文件；停止不确定保留文件 | 主链 `cleanup.json`；原launcher正常/故障回归与 `test_web_core_private_cleanup.py` |
| A10 | 未知查询/页面/模型输入/联系点拒绝；网络未知地址在连接前拒绝；实际Provider动作独立记数，业务核心不可替代 | 原allowlist测试、新research/contact端口反例、Gateway账本和调用证据；真实外部能力仍为 `not_run` |

本表路径省略的统一前缀为 `output/acceptance/task12/`；Linux产物位于 `output/playwright/t10-<owner>/`。第九轮是一次中间完整主链通过，最终全仓结果不与该数相加。截图名 `need-sales-denied-390` 实际页面是 Handoff；它只证明当前页面撤销后清除，Need拒绝另由真实HTTP断言证明。OpportunityList 不消费 `?opportunity_id`，没有将无效参数声称为精确深链。

## 完整门禁

静态边界与敏感扫描 exit0，mypy 562文件通过；全仓ruff首次两处本批格式提示已修，最终exit0。Web：411 passed，typecheck/lint/build/gen:api均exit0，生成API无diff。lint实际112 warnings/0errors，逐行git blame证实均早于本计划branch base：App.vue25、OutreachWorkbench.vue34、BillingUnavailable.vue10、ManualOperations.vue12、ProductSupplyCenter.vue31。归属见 `lint-attribution.json`，不沿用历史120或172数字，也不声称零告警。

冻结版本完整后端实际为 **98 failed、9217 passed、exit1**，pytest耗时2076.35s、wrapper耗时2083.9s；本检查点门禁未通过，不能作为最终验收通过。完整98项安全索引见 `backend-failure-index.json`，后续修复与作用组结果另行登记，不倒写本轮结论。最终独立新owner与告警分类待核。所有pytest均清除TEST_DATABASE_URL，设置PYTHON_DOTENV_DISABLED=1、TRADEOS_REQUIRE_E2E=1，数据库测试串行。命令/耗时以 `static-gates.json`、`web-gates.json`、`backend-full.json`、`diff-gates.json` 为准。

## 限制与失败历史

Mac 原入口的 quotation 与自动寻源准入配置仍未具备完整运行条件；Mac成本页的明确503不能替代Linux报价证明。Linux与Mac的owner、Need、Opportunity不同。Linux固定业务时钟不作为真实耗时证据。本批没有新增统一launcher、跨网络桥或绕过Linux parser资源probe。

DB独立stop/start会因原随机HostPort分配改变公开端口，原配置不自动更新。内部pg_isready成功而原端点持续ConnectionRefused的失败已保留，不能归因为API连接池问题；应用HUP只重启四应用，不承诺自动恢复改变的DB端点。该情况下必须回到原owner入口受控重建配置/依赖，不能把新空库冒充原业务恢复。A5的短暂不可用用同容器pause/unpause保持端点，并以独立限时恢复任务保证取消请求能完成。

首次pause试验的driver取消也等待暂停PG，需精确owner手动unpause才结束；不计作有界恢复通过。期间误启动的E2E已立即中断并清理，之后恢复数据库串行纪律。所有中间TDD、旧fixture错误、审批选错旧记录及上述故障轮保留在Task12报告与独立日志；没有倒推为同一失败原因。

受控输入是合成演练证据，不代表真实买家、法律政策、供应商报价、邮箱可达性或生产账户就绪。受控exclusive研究账户只描述当前owner的合成端口。本批不部署、push、merge或实际外发。

另在只复制声明锁文件、公开src与构建配置的新目录中完成Node依赖安装与构建：`node-isolated-final.json` 记录 `npm ci` exit0/28.71s、`npm run build` exit0/5.05s。用户与全局npm配置均指定各自独立空文件，未借用用户凭证配置。首次两配置都指向/dev/null被npm以double-loading拒绝，原日志保留；这不属于应用运行失败。

合成模型没有真实计费token数据，真人处理没有可核实的实际处理时长；这些值保持未知，不填0或编造效率改善。已观察的Provider调用次数与Handoff等待状态分别按持久账本和真实当前时钟解释，Linux固定业务时钟不用于耗时比较。

## 首轮98项失败逐项映射（第二全量前冻结）

首轮完整结果保持98 failed/9217 passed；以下是针对修复与作用组证据，不能相加为完整通过。迁移组合原因只在具名顺序复现，未把全部历史异常倒推为同因。完整安全索引为output/acceptance/task12/backend-resolution-index.json。

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
