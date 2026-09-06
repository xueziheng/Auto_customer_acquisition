# Task6 独立规格与质量审查

## Spec Compliance

- ❌ Issues found：明确列入本批支持范围的 `divRplyFwdMsg` 历史分隔没有正确隔离其后的历史正文。旧退订可以进入当前分类，旧采购原话也能通过当前片段证据门。见 I1。
- 审查范围：BASE `21b0b770fee078575c437d57a5f1b01bc8f5c689` → HEAD `aab51d0decd568b8755cbb2b6526ba4396a68ca6`；源码提交 `6d06a7b34db3f76ee3e6ee65c856004e44fba371`。按任务包分块通读一次全部 diff；未做全分支审查、未读取整体 plan/ledger、未运行 git diff。
- ⚠️ 真实 Provider/真实模型/事务邮件与多渠道送达、Task7/8 的 Message 原件授权及 owner 矩阵和页面消费、Task12 同版本全仓验收仍须由控制器收口。本批受控后端证据不替代这些交付。依据 `task-6-report.md:174` 起的限制及正式 spec 第 49–58 行。
- ⚠️ 部分早期 RED 只有报告中的缺口描述，没有保留精确历史命令输出；报告在 `task-6-report.md:162` 已明确承认。现有最终命令/版本/通过数可核对，不将未归档历史当成已看到的失败证据，也不因此重跑套件。

## Strengths

- `apps/scheduler_worker/runtime.py:1498`、`:1513` 与 `reply_binding.py:23` 把 canonical Demand verifier 的一次性绑定和本进程 borrowed Raw/session 资源分开，完整组合自然打开原入站消费者门禁。没有第二套 Demand 或 engine。
- `apps/scheduler_worker/adapters/message_content_reader.py:89`、`:113` 同时落实有界读取、精确 Raw metadata、完整 HTML/文本两次护栏及拒绝超限；`workflows/reply_qualification/steps.py:156` 在分类写入之前拒绝 parser 已丢弃的非法候选，并核可靠原文及单一片段。
- `apps/scheduler_worker/reply_actions.py:443` 保留同值不可变类别的原 Provenance；`:507` 仅将真实缺项 Need 转为稳定幂等的原 follow-up pending。集成测试覆盖同 Need 续补、重启、类别冲突与零误接管，没有把队列当作已发送。
- `workflows/reply_qualification/questions.py:72` 先核当前真实员工，`:133` 要求选中 Message 的现存字段证据，再以真实 Demand 缺项调用原 selector。新增 Outreach read action 与原 SYSTEM feedback action 分离。
- `apps/scheduler_worker/bootstrap.py:127`、`:145` 使用两处窄 SYSTEM 元数据读取，继续依据当前员工与 ownership 确定受众；`apps/notification_worker/runtime.py:291` 明确受控站内路由，生产邮件校验保留。真实通知测试把 HandoffRequested job 与 InApp 记录精确关联。
- `apps/notification_worker/health.py:118`、`:122` 和 `runtime.py:344` 处理已 bind 未 ready 的取消窗口，并区分正常退出软等待与总清理界限；报告的最终聚焦测试包含真实端口重绑。

## Issues

### Critical（Must Fix）

无。

### Important（Should Fix）

**I1 — `divRplyFwdMsg` 只排除历史头容器，旧正文重新成为当前表达。**

- 位置：`connectors/gmail/inbound_mime.py:229`、`:247`、`:249`；消费路径 `apps/scheduler_worker/adapters/message_content_reader.py:123`、`workflows/reply_qualification/steps.py:158`。明确要求见 `docs/superpowers/specs/2026-09-06-reply-completion-6.md:35`。
- 当前代码把 `divRplyFwdMsg` 与包裹全部引用正文的 blockquote/gmail_quote 同等处理。该历史头 `</div>` 一关闭，quote_stack 就清空，随后兄弟节点中的旧正文进入 evidence_segments；`evidence_available` 仍为 True。这不是“未支持其它客户端格式”的泛化要求，而是已具名支持的分隔符本身没有表达“后续属于历史”的语义。
- 最小输入：`<p>Thanks. We have no current need.</p><div id="divRplyFwdMsg"><b>From:</b> Supplier<br><b>Subject:</b> Previous message</div><p>Please unsubscribe our entire company.</p>`。纯解析结果同时包含当前回复和旧退订句。把这两个片段按生产固定分隔符交给原 QualificationAgent，受控模型返回 `no_current_need`，实际结果却确定性成为 `unsubscribe` / `account`。原分类器的明确退订 override 不依赖模型猜测。旧原话也同时满足完整 body 子串与单一当前片段条件。
- 风险：普通回复可以错误抑制整个客户企业；历史数量/规格也可能被标为本次客户证据并推进 Need/接管，直接破坏当前表达隔离。
- 修复方向：区别“引用容器”与“历史后缀分隔符”。对 `divRplyFwdMsg` 明确排除后续历史，或无法可靠界定时将该候选设为不可验证，不恢复为当前表达。保留完整 Raw/body 护栏；补真实入站当前回复＋该历史头＋旧退订/旧数量反例，证明零误抑制、零历史字段落库，同时保留真正当前字段。
- 归因：实现缺陷；不是 plan-mandated 例外。

### Minor（Nice to Have）

**M1 — 引用栈遗漏标准 HTML void 元素，可拒绝本来可用的整封回复。**

- 位置：`connectors/gmail/inbound_mime.py:233`。void 白名单缺少 `source`、`track`、`area`、`embed` 等无闭合标签元素。它们出现在 blockquote 内会被压栈；随后的 `</blockquote>` 不能弹出栈顶，最终被误判为引用未闭合。
- 最小输入 `<p>We need hinges.</p><blockquote><hr><source src="x">old reply</blockquote><p>5000 units.</p>` 返回 `evidence_available=False`、空片段，尽管引用容器已闭合。风险是合法当前采购表达整封失败；这是保守拒绝，不是事实污染。
- 修复方向：使用完整的 HTML void 元素集合，补一个真实结构的引用内媒体/图片元素反例，保证引用结束后恢复当前片段且不误拼接。

**M2 — 已知 Git 环境 stderr 仍有噪声，不能称验证输出全净。**

- 位置：`.superpowers/sdd/2026-09-05-web-first-completion/task-6-report.md:70`。`git diff --check` exit 0，但 stderr 有 48 行 AppleDouble 噪声；报告另记源码提交时环境噪声。这里没有证据表明它是本批源码缺陷，报告也没有隐瞒。
- 处理方向：由控制器在 Task12/最终环境验收统一记录与判断；本审查不维修共享 Git、不以重复测试消除噪声。此项不独立阻塞本批。

## 定向核验与测试记录

- 未重跑已报告的同源码测试。最终 `364 passed` 主组合、`119 passed` 原权限/服务回归，以及 Ruff/Mypy/结构检查/增量扫描/OpenAPI 生成/Web typecheck 均按报告的明确版本与命令核对；没有把中间版本数字相加，也没有将真实外部结果标作已运行。
- 新验证仅为两个只读内存脚本：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - <<'PY'`，工作目录始终为本 worktree。第一段调用 `parse_inbound_content` 检查上述历史头、引用内 void 元素和自闭合引用容器三种输入，exit 0；前两种输出支持 I1/M1，自闭合容器正常恢复片段。第二段调用原 `QualificationAgent`，只替换模型返回值为 `no_current_need`，exit 0，实际输出 `actual_category=unsubscribe; actual_scope=account`，且 `old_quote_passes_original_and_single_current_segment=True`。未启动 PG、MinIO、浏览器、Provider 或其它外部资源。
- 补读被 diff 截断、必须判断的同文件函数：`reply_actions.py:80–110` 的逐字摘录选择、`:303–329` 的 `_facts` 关联检查；`steps.py:70–150` 的 classify 前置/输入路径；`agent.py:187–239` 的分类 parser 与明确退订 override。用途分别是判断多片段接管是否误取原话、缺项 fallback 是否以真实关联为前提，以及 I1 是否会产生确定性动作影响。没有把它们作为第二轮完整审查。
- 具名域外核验①：新增 missing_information fallback 是否真取精确 Need 的确定性缺项、canonical verifier 是否只靠假身份。定向读原 `apps/scheduler_worker/adapters/reply_business_facts.py:89–210`、`reply_customer_evidence.py:45–199`，确认公开 Enrollment/发送/账户/Hypothesis/Need 链与原证据等级门保持。
- 具名域外核验②：新英文 renderer 词表是否覆盖真实 selector 可输出的缺项。定向读 `domains/conversations/service_impl.py:479–533`、`domains/demand/models.py:343–363`；真实缺项为 product_category、application/size_spec、quantity，当前 renderer 覆盖。一次命名路径探测发现不存在 `next_question_selector.py`，命令 exit 2；随后只读已定位的实际实现，没有爬库。
- 具名域外核验③：`build_reply` 新 keyword 对实际调用者的兼容风险。仅对 `apps`/`tests` 定向搜索 `def build_reply|reply_factory=`，得到 canonical factory、Protocol、受控入口与本批集成调用；未发现另一实际 bootstrap 实现。此搜索不替代 Task12 全仓检查。
- 只写本报告；源码、索引、HEAD、原目录和共享 Git 均未修改，未派子代理。

## Assessment

**Task quality：Needs fixes**

核心组合、权限收窄与受控持久验收做得扎实，但具名历史分隔符的解析缺口会直接把旧内容变成本次动作或证据，必须先修复 I1 再开放下一批。M1 是保守误拒的质量问题；跨批依赖和已有环境噪声由控制器继续收口。


## Fix1 限定复审（2026-09-06；以本节更新最终结论）

### Finding Verdicts

- **I1：divRplyFwdMsg 后历史正文被当作当前表达 — ADDRESSED。** `connectors/gmail/inbound_mime.py:224–227` 在历史头开始时先封存此前连续片段，再单向设置 `history_suffix=True`；该状态没有闭合标签恢复路径。当前文本与换行写入都增加后缀状态条件，完整 `values`/body 仍照旧收集。因此闭合或自闭合历史头之后的兄弟正文不再进入模型或证据片段，真正当前表达被保留。
- `tests/unit/test_reply_current_evidence.py:64` 起覆盖闭合及自闭合历史头、多个后续兄弟节点，并同时断言当前片段精确值及完整 body/guard_body 保留历史；`tests/unit/test_reply_content_boundary.py:140` 起经过实际 bounded reader，断言历史不进投影、仍留在可靠原文，而且历史 HTML 内的 credential marker 仍被完整 guard 拒绝。
- `tests/integration/test_reply_completion.py:252` 起扩真实入站合法采购案例，核当前 product_category 仍落入原 Need、历史 quantity 不落库并能读取真实缺项建议；`:400` 起的非法 quote 案例加入历史数量，仍复用分类/Need/Opportunity/Handoff 零写断言；`:484` 起的普通回复案例加入历史公司退订，模型仍返回 no_current_need，真实 PG 断言零 suppression、分类仍 no_current_need。修复没有靠调整模型回答回避原明确退订 override。

### New Breakage in the Fix Diff

无新 Critical/Important/Minor。生产变更只增加当前片段收集的单向历史后缀状态；原引用容器栈、完整内容 guard、Agent/action、权限与外部发送路径均未扩大或重写。正式 spec 第 35 行同步该分隔符的明确语义。

### Out-of-Scope Observations

没有新增域外观察。原 M1（HTML void 集合）与 M2（Git AppleDouble 噪声）本轮未修，按控制器裁定 deferred 至 Task12/最终环境说明，不延长本轮。原 Task7/8/12 与真实 Provider not_run 依赖保持；早期 RED 未归档的限制如实保留，不补造历史证据。

### 核验范围与证据

- FIX_BASE `aab51d0decd568b8755cbb2b6526ba4396a68ca6` → HEAD `b3b1c9887202b13eaf78e0fda9066008f8f20a3c`，源码 `2e0afa43db54b67d90613cf005d9b3822e477b4f`。一次分两块通读 fix diff 的全部 6 个文件和报告追加段；没有重新审查原整批或扩展至域外代码。
- `task-6-report.md:199–218` 给出同一反例命令的 RED（exit 1，6 failed / 9 passed / 25 deselected）、GREEN（exit 0，15 passed / 25 deselected）及最终同源码作用组（exit 0，193 passed in 38.23s）。测试内容与修复因果相符，既证明旧内容被拒，也证明当前字段仍可推进。
- `task-6-report.md:220–235` 记录修复作用集 Ruff/Mypy/边界检查/增量敏感扫描通过，owned fixture 清理完成；Git stderr 噪声独立披露，不冒称全净。没有 API/prompt 变化，未把前批 OpenAPI/Web 验证或真实 Provider 结果提升为本次新验证。
- 未重跑 364/193 或其他测试，没有新增最小验证的必要；未读取凭证、启动外部资源或子代理、操作 Git/索引/HEAD。所有命令显式使用本 worktree，唯一写入是本报告追加段。

### Verdict

**Fix round：All findings addressed, no new Critical/Important breakage。** 唯一 open Important I1 已关闭。

**Spec Compliance：✅（本批约定范围；原跨批依赖保持）。**

**Task quality：Approved。** 本节覆盖初审的 Needs fixes 结论；保留已登记的非阻塞 Minor 与最终集成依赖。
