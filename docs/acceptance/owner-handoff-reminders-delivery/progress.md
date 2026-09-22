# SDD ledger — plan: docs/superpowers/plans/2026-09-08-owner-handoff-reminders.md

Spec: docs/superpowers/specs/2026-09-08-owner-handoff-reminders-design.md
Working directory: /Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion
Branch: codex/web-internal-pilot
Feature baseline: e6b2446909f435ac6dd0250d992555f2c764a741
Task1 BASE / plan commit: e6e08aac170670eff93d4f73188cb16672103568
Historical output400 and shared Git sidecar warnings preserved. Prior plan archive is immutable; do not recreate/delete its scratch.

## Preflight

| Pair/task | Producer/consumer check | Outcome |
| --- | --- | --- |
| Task1 internal workflow/config | pilot persists explicit interval, API and scheduler consume same choice | mode must not exist only in unit tests; integration covers actual composition |
| Task1 internal accept/notification | domain acceptance commits, scan/job/delivery recheck current fact | accepted event alone is insufficient; narrow transactional serialization is required |
| Task1 internal legacy/new mode | engine supports version key and latest start; old running work must remain interpretable | implementer must record final version/type and mode-change constraints |
| Task1 tests/files | real PG + notification storage; synthetic config remains in process; fixtures must use existing owned resources pattern | no live provider, no realprofile and no imaginary fullsuite claim |

Ruling: 将当前工作聚焦为待接管每两小时提醒、接受即停止的完整子项目 — 用户已明确停止条件，提醒可独立交付；主动退回与关闭金额分档是不同功能 — 首次真实profile仍不能伪造缺失业务政策初始化，其他已确认需求继续保留待实现。
Ruling: 用户确认的业务意图已足以授权实现，不重复询问brainstorming或writing-plans流程选择 — 开发者要求执行已授权工作，连续对话已明确行为 — 工程解释若偏差会产生本地可撤销返工；技术方案/限制留文档。
Ruling: 只提醒当前员工、按连续UTC经过时间实现，不抄送上级、不增加工作日历 — 上轮已明示最小受众假设，用户未给工作时间安排，现有scheduler以UTC运行 — 可能在非工作时段出现站内提醒；后续若需要工作日历应另定，不能伪称用户明确选择。
Ruling: 保留旧升级模式，显式配置新模式，不用超大T1/T2或隐藏7200全局默认绕过政策 — 本次应落实真实行为而不是配置假象 — 增加兼容面，必须验证两个模式与持久旧运行。

Task1: pending implementation.

Task1 agent: /root/owner_reminders (gpt-6-astra high, multi-layer concurrency and persistence judgment). Implementing from e6e08aac; no other implementation agent.

Task1 checkpoint: implementer identified real race: acceptance UPDATE and in_app append independent commits. Proposed narrow opportunities public current-state lock context through actual notification append; scan shares same fact boundary. API old notifier is structured_log, therefore needs shared mechanical persistent-job adapter.
Ruling: 新增apps/composition_support/handoff_notifications.py共享机械notifier，并保留scheduler兼容导出 — API与scheduler不能互导，而新模式必须真正进入同一持久通知队列 — 增加组合回归面，必须遵守composition_support边界、无共享运行对象，旧日志模式兼容。
Ruling: 使用opportunities最窄公共持锁事实接口串行接受与真实站内提交，infra承载跨表锁、域不导入其他域内部 — 单靠HandoffAccepted异步消费或投递前普通SELECT仍会竞态 — 长通知事务可能延迟接受；应只包本机站内确定性提交，验证多连接锁序和无反向等待，不覆盖外部网络投递。

Task1 checkpoint: definition RED4/19 due missing owner_reminder_interval. ActualPG fixture initially blocked by stoppedDocker; Docker.app exists, controller executed open -a Docker, fixedsocket unchanged. Fixture creation exception was initially outside safe capture and emitted FileNotFound traceback without credentials; implementer corrected diagnostic capture immediately and must disclose exact evidence.

Task1 checkpoint: definition GREEN19; realPG2 RED missing handoff_guard. Implementer proposed period seconds+1 as persistent workflowversion, controller requests bounds/future collision/oldrun handling justification before accepting contract.
Ruling: 扩展精确文件apps/api/pilot.py的runtime_settings(config)映射 — 真实pilot绕过通用from_environ，缺此映射会使运行忽略新策略 — 增加pilot装配验证，不扩大到其他API业务入口。

Ruling: 接受当前human_handoff类型以周期秒数+1编码owner模式持久版本，v1仍为旧升级模式；所有入口限定1..2147483646整数秒，ADR永久保留版本空间，未来结构变更使用新type — 复用engine版本校验拒绝静默重新解释旧Run，不修改通用engine — 版本不再是普通递增结构号，增加长期维护约束；API/scheduler真实startup必须拒绝活跃不同模式/周期（含v1对新模式），不能仅文档限制或静默悬挂，需恢复原配置完成旧Run后切换。

Task1 checkpoint: actualPG acceptance suppression/race GREEN2; workflow→job→LOCAL_IN_APP→actualstore timing/restart/lateOutbox queued suppression newtest GREEN3 total. FullOutbox+register_complete_scheduler refinement pending. Newmode skips duplicate HandoffRequested notification projection and keeps workflow initialnotice; exactfinalcoverage awaitsreport.
Ruling: 扩展apps/api/runtime.py真实异步startup的一致性检查 — composition构造必须无IO，只有真实lifespan才可检查已持久Run与所选周期 — 需沿原失败/取消清理，拒绝时零业务推进；不在构造器加数据库访问。

Ruling: 当前事实锁包含账户OwnershipLock，并要求与机会/接管负责人一致；缺事实或不一致时抑制旧受众，不自动更改归属 — 员工域transfer更新账户归属但不保证同步Opportunity.owner，只查机会会错提醒旧员工 — 不一致事项需人工修正既有事实，当前不重写归属转移流程；验证真实transfer后抑制及锁序。

Review scheduling: after Task1 spec/quality gate, perform the one whole-branch review using the same independent reviewer via follow-up (most-capable model), with full feature baseline package and prior accepted report. Task1 is the only implementation task; reuse its already-read source evidence, inspect additional plan/spec/cross-cutting scope, do not duplicate tests or blindly re-read unchanged patch. This does not skip either gate and no implementer reviews itself.

Task1 checkpoint: latest newPG group12passed4.70s; actualpilot→APIlifespan→canonical scheduler root/completeOutbox→LOCAL_IN_APP store, three incompatible activeversion startup refusals/zero advancement, ownership transfer/inactive/wrongtenant/duplicateaccept covered. Mypy17source pass. Affected unit and legacyv1 PG regression/finalruff/boundaries pending; ADR0069+operations docs written. Prior interim fixture errors involved synthetic Decimal/mandatoryobjectsettings/UnconfiguredModelClient; no hungPG or productionsecret. Finalcommands/commits awaitreport.

Task1 checkpoint: final targetedPG newowner+legacyv1 behavior20passed; two unchanged0007migration tests explicitly deselected (no schema change). Unit groups274passed and107passed reported separately pending exactfile/command report; ruff, mypy17source, boundaries pass. Fullroot test covers accept before firstscan, initialjob queued, periodicjob queued. Newowner reason routed onlyin_app even normalproduction, preventing suppressedsite+selectedemail divergence; existingreasons retainchannels. Report/commit pending, no blockers.

Task1 implementation DONE: acd89d23ca289f92bdbe5a280cbdf70fb05384f0;28files; fullreport read. FinalPG20passed5.94s=new14+legacy6, two unchanged0007migration tests deselected/no skip; unit274passed2.83s and107passed1.85s distinctreportedgroups, ruff21files/mypy17source/boundaries/diff pass. Exactownedcleanup reported complete. Additional static existing test-example DSN exposure disclosed by implementer; no actualrandom/realcredential output reported. Review package masks credential-bearing URI segments before reviewer reads; literal secret values are unnecessary for this feature review.

Task1 independent review: /root/owner_reminders_review gpt-6-astra high, safe review-e6e08aa..acd89d2.safe.diff; spec+quality pending. All report concerns read before dispatch; no production blocker asserted by implementer.

Task1 review checkpoint: independent realPG barrier probe reproduced deadlock SQLSTATE40P01, probe1passed1.59s means defect reproduction NOT productpass. Employee FOR UPDATE at infra/db/repositories/opportunities.py:584 conflicts with OwnershipHistory FK KEY SHARE after transfer ownership UPDATE (employees.py:364/:382; tables.py:2801/:2811). Important pending fullreview. Earlier report assertion of no reverse locking omitted implicitFK path; fix must address actual interleave and preserve active/state guard. No new source edits by reviewer.

Task 1: fix round 1/5 dispatched to original implementer /root/owner_reminders; FIX_BASE acd89d23ca289f92bdbe5a280cbdf70fb05384f0. Independent Task review spec ❌ / quality Needs fixes: I1 Important real FK deadlock. Requested durable barrier regression RED→GREEN, compatible employee fact lock, explicit ADR correction and narrow affected validation. Also requested actual API+scheduler matching-active-version startup positive test to resolve review evidence caution. No task gate passed yet.

Fix1 checkpoint: durable real in_app/ownership-history concurrency regression RED on original code; same-version active Run actual API+scheduler startup positive GREEN (combined1failed/1passed). Implementer verified installed SQLAlchemy key_share=True/read=False compiles FOR NO KEY UPDATE; adjusting only Employee lock, retaining remaining fact checks/locks.

Fix1 checkpoint: focused realPG GREEN3/2.79s (concurrent actual transfer/historyFK with no40P01; matching active API/scheduler startup; deactivate waits for in_app commit then suppresses). Original-code RED explicitly failed outcomes.count(40P01)==0. Final affected file regression and validation/commit pending.

Fix1 implementation DONE b52f833244560e7e22d18b562519a2120d2f277a; full fix report read. Final affected PG17passed6.33s (14 existing+3 new), flow21passed0.14s; ruff2/mypy1/boundaries/diff pass. Independent scoped review dispatched to original reviewer with acd89d2..b52f833 package15485bytes. Matching active startup test now supplies actual two-root positive evidence, reviewer confirmation pending.

Task 1: fix round 1/5 (1 Important addressed, 0 open; I1 FK deadlock corrected with compatible Employee lock; commits acd89d2..b52f833). Scoped independent review spec ✅ / quality Approved, new findings0. Actual matching-active two-root startup evidence caution ADDRESSED with direct persistent test. Full report read; no parked or deferred current-task findings.
Task 1: complete (commits e6e08aa..b52f833, review clean).
Final whole-branch review dispatched separately against feature baseline e6b2446909f435ac6dd0250d992555f2c764a741..b52f833244560e7e22d18b562519a2120d2f277a, three commits safe package147355bytes, original independent most-capable reviewer reuses already-checked source evidence.

Final whole-branch review: Approved; spec/plan ✅; Critical0/Important0/Minor0; no deferred/parked current-subproject findings. Reviewed e6b2446..b52f833, report read completely. No second fix wave needed.
Ruling: 收尾保留现有分支/worktree、不重复请求合并或推送选项，并复用最终变更对应的针对性测试 — 用户授权继续Web开发，开发者要求完成已授权可逆工作并避免无新增疑点的重复测试 — 当前不提供合并后或全仓测试保证，分支尚未发布。
Final archive: this plan records copied verbatim with SHA-256 manifest; raw diff packages represented by reconstructable Git ranges. Historical reviewer probe expects old-code deadlock and is not current functionality evidence. Only exact current plan scratch is scheduled for removal after verified archive commit; prior plan and output400 preserved.
