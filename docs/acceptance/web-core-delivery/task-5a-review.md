### Spec Compliance

- ❌ Issues found：严格 Date 校验与读取过程中执行的解码预算尚未满足；见下列两项 Important。其余可见实现符合 Task5a 技术候选页范围，没有把 5b 事务、关联、review API、driver 或 Task6 分类提前算作交付。
- ⚠️ Cannot verify from diff：5b 对 initial cursor 的首次耐久保存、完整敏感事实 canonical fingerprint、整页事务及后续 Raw 授权重读属于后续任务；本批仅定义消费约束，见 `docs/superpowers/specs/2026-09-05-email-inbound-5a.md:27`、`:33`。Task4 原生命周期/anchor、四处旧 fixture scanner 基线未扩审。
- ⚠️ 验证证据按实现报告的实际分轮处理：80 项本批、最终 59 项 HTTP、较早 216 项兼容不是一次最终总跑。没有重跑这些组，也未把受控 Provider 结果当成真实 Gmail 来源验收；报告明确真实 Gmail/模型/客户动作 not_run，见 `.superpowers/sdd/2026-09-05-web-first-completion/task-5a-report.md:48`。

### Strengths

- `connectors/gmail/inbound.py:78` 的初态只取 profile 并返回空锚定页；pending 引用在页 byte 预算不足时留给下一页，耗尽前不越过旧 history_start。独立 `gic1` route 绑定及永久 history 404 保持失败关闭；未接管旧 feedback 正文职责。
- `infra/email_inbound_artifacts.py:36` 在 Raw.put 后先核安全 metadata，再实际 bounded get 并比较完整 metadata、bytes、长度和 hash；metadata 去重、未知提交或丢失对象不能被误报为完整归档。
- `tool_gateway/handlers/email_inbound.py:42` 使用独立 LOW/FREE/NONE manifest；prepare 只构造 HMAC 与安全 metadata，execute 内才构造 reader。归档先于两个完整文本视图 guard，随后才判候选大小，没有裁剪后放行。
- `tool_gateway/handlers/email_inbound_slots.py:25` 用实际 asyncio task 身份核 ownership，child 无法 take/discard 父槽；当前 task 的错误领取会消耗槽。wrapper 的 finally 清槽覆盖失败/取消，成功领取仍核完整 route 与 starting cursor。
- 真实集成测试使用原 Gateway、独占 PG metadata 与 MinIO，故障注入包括 EXECUTING/完成 ledger、表锁取消、未知提交以及对象损坏；领取边界伪返回值测试与这些真实集成证据有明确区分，见 `tests/integration/test_email_inbound_gateway.py:1` 和 `tests/unit/test_email_inbound.py:1`。

### Issues

#### Critical (Must Fix)

- 无。

#### Important (Should Fix)

- **解码预算之前允许任意 Python codec 执行解压** — `connectors/gmail/inbound_mime.py:98`（至 `:104`）。邮件自带 charset 被直接交给 `codecs.getincrementaldecoder`，并未限制为文本字符集；`zlib_codec`、`bz2_codec` 等二进制变换也可被选中。预算只在 decoder 返回后计算，不能限制该调用内部的分配。一次小规模纯解析复现中，4087 bytes 的 zlib 内容就在一次 decode 内产生了 4,195,328 bytes，越过 4 MiB 候选解码上限；之后仅因 bytes 没有 encode 方法而返回 malformed。攻击输入可进一步放大内存占用，在固定 disposition 生效前耗尽 worker。应先拒绝非文本/压缩类 codec，采用明确受支持字符集集合或等效严格字符集校验，再保持增量 UTF-8 预算；补针对压缩 charset 在进入解码器前即拒绝的回归测试。
- **Date 的末尾时区检查不能保证完整消费字段** — `connectors/gmail/inbound_mime.py:267`（至 `:277`）。`_ZONE.search(date)` 只要求字符串末尾看起来像时区，`parsedate_to_datetime` 会忽略其余尾部 token。聚焦复现的 `Sat, 05 Sep 2026 09:00:00 +0000 garbage GMT` 和 `Sat, 05 Sep 2026 09:00:00 +0000 -1200` 都被接收为 candidate，sent_at 均取第一个 `+0000` 得到 09:00 UTC。这样歧义/损坏时间仍进入 archived 候选事实，违反本批“严格 Date、无尾部垃圾”的明确要求。应先完整匹配允许的 Date 语法并确保恰好一个时区，再做语义日期转换；保留现有无时区、重复头、-0000 拒绝，并补上述尾部仍以合法时区结尾的案例。

#### Minor (Nice to Have)

- 无。

### Checks Performed

- 只读审查 base `a353ce51251d235319494e1bca5f4cf1c266c015` → HEAD `0da81d16ba31daa43add8a2e6aa51373c15f077e` 的指定 review package，完整 diff 按连续块读取一次；未运行 git，未修改源码/index/HEAD，未派子代理。
- 已读根和涉及目录现有 AGENTS，以及 Task5a brief、Task5 技术读取部分、正式子规格、ADR0026、实现报告。`docs/AGENTS.md` 不存在；未把不存在当成缺陷。
- diff 外一次具名聚焦读取：风险是新 reader 复用旧 feedback profile/list/history 方法时可能仍带报告过滤或保留错误协议语义；只读 `connectors/gmail/transport.py:216–301` 周边方法，确认 bootstrap 仅 after、history 仅 messageAdded，没有报告专属过滤，所有 Provider 参数仍 URL 编码。该方法体未在 diff hunk 内，未重读任何已完整展示的 changed 函数。
- 一次具名聚焦复现：通过 `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python` 执行纯解析小脚本，核实上述 Date 与 charset 风险；exit 0。codec observer 只计数真实标准库 decoder 输入/输出长度，未替换解码结果；最大展开约 4 MiB，不使用外部 IO、数据库或真实秘密。输出见两项 Important；没有扩跑测试 suite。

### Assessment

**Task quality:** Needs fixes

**Reasoning:** 插件、归档和结果交付边界总体清晰，受控真实基础设施测试能支持主要失败关闭行为。但攻击者控制的 charset 能绕过读取中预算，歧义 Date 又可成为候选时间事实，这两处属于本批明确承诺的解析边界，修复后才能批准。

## Fix1 定向复审

### Finding Verdicts

- **I1：任意 charset 在预算前触发解压** — **ADDRESSED**。`connectors/gmail/inbound_mime.py:140` 在正文解码前先通过有限 `_TEXT_CHARSETS` 映射选择受信文本 codec，未知/压缩/变换类名称直接返回固定 malformed；`connectors/gmail/inbound_mime.py:167` 只收到映射后的 codec 名，保留逐块解码及累计 UTF-8 预算。`tests/unit/test_email_inbound.py:358` 使用真实小型 zlib/bz2 内容并断言零 decoder 查询；新增非文本名称矩阵也断言零查询，常用文本编码矩阵核对实际内容兼容。正式子规格明确 UTF-7/未列编码隔离，未加入任意 fallback。
- **I2：Date 尾部额外 token/第二时区仍被接受** — **ADDRESSED**。`connectors/gmail/inbound_mime.py:32` 定义完整允许语法；`connectors/gmail/inbound_mime.py:341` 改为 fullmatch 后再调用日期语义转换，因此先前两个复现输入及 `GMT GMT` 均不能通过。四位年份限定 1900–9999，同时阻止标准库重解释 0000/0001/0099；原无时区、-0000、重复头拒绝未放宽。新增测试覆盖三个尾部歧义、三个零填充年份及七种合法时间表示。

### New Breakage in the Fix Diff

- None。未发现本次修复新引入的 Critical/Important/Minor。显式字符集集合与 Date 兼容限制已写入正式子规格；文本解码预算、原 HTML/实际文本两个 guard 视图和归档流程保持原语义。

### Out-of-Scope Observations

- None。未扩审旧 parser 其他行为、旧 transport、5b/Task6 或其他任务。

### Checks Performed

- 只读 Fix base `0da81d16ba31daa43add8a2e6aa51373c15f077e` → HEAD `46df91c37034c7a236c8a7351c5d57c232bdc76c` 指定 fix diff 一次；源码修复提交 `904a29475b5359680a9eb0fdfb911779044e4994`。按 scoped re-review 模板仅核 I1/I2 与 fix 本身；未运行 git、未派子代理、未修改源码/index/HEAD。
- 对照 Fix1 报告核实精确测试名、RED/GREEN 及分轮范围。最终单元 97 passed；真实 PG/MinIO/Gateway 6 passed 在最后年份收紧之前，其新增 `tests/integration/test_email_inbound_gateway.py:711` 逐项验证隔离 disposition、Raw 实际读回相等、sent_at=None、cursor 与空槽。年份最后变更由最终完整 97 项单元覆盖，未把两轮冒称 103 项一次总跑。
- 报告的静态检查、敏感增量检查、owner 清理均有具体命令/结果且没有新 warning。首次合并输出在报告/文档重复部分被截断，随后仅补读报告末尾，未重读 fix 源码或 diff。没有发现需要额外复现的新疑问，因此未重跑任何测试组。

### Verdict

**Fix round:** All findings addressed, no new Critical/Important breakage。

**Spec compliance:** ✅ 本批原有两项规格缺口已修复；后续任务及真实外部验收限制仍沿初审的 ⚠️ 范围。

**Task quality:** Approved。
