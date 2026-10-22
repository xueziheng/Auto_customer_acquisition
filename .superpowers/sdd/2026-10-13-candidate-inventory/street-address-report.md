# 正文块与国家地址证据修复

BASE：31eb307。仅本地合成/受控测试；未 SSH/UI/调用真实 provider，未读取凭证或模型原始输出。未修改临时额度维护工具或父验收文档。

## 实现

- 共用 `_VisibleTextParser` 按固定 DOM 块/br 分行，同块内先连接原始 inline 字符再归一空白。`Ni<b>ger</b>ia` 不拆词，`We export to <span>Nigeria</span>` 不再产生独立国家行。HTTP/browser 共用此实现。保留脚本等原有隐藏标签排除，不新增 CSS/可见性引擎。
- 200,000 字符上限计入分隔换行；round1 修正为任一完整块超过剩余空间时整份派生正文失败关闭，固定 ValidationError，不再交付早前完整块。HTTP/browser 原件仍保存，均不返回部分 PageSnapshot。详见下方审查修复记录。
- 国家门移除纯国家行接受。只接受锚定的自身地址/所在地标签（带冒号或连字符，可独立标签紧接下一块），或完整单块门牌/街道类型/locality/末尾完整国家形式。块最长 240 字符；完整国家实体最长匹配不变。
- 两种国家证据统一检查前后各两块、最多 1,000 字符上下文。超限、明确假设/计划/否定/问句/市场覆盖/第三方地址/新闻/其他国家时拒绝，不截断反证再接受。街道名字中的 Market 不作为市场覆盖词；企业自身“tricycle dealer”描述不等同第三方地址。
- 其他 name/domain/product/email/single-page/discovery-only/provenance 门保持；provenance extractor 标记从 public-evidence-v1 改为 public-evidence-v2，原件 URL/hash/artifact 绑定不变。不增加网络动作、迁移、搜索或工具预算。
- 旧合成正例 `Acme\nKenya` 补为 `Acme\nAddress: Kenya`；评估中的未出现邮箱样本也补地址，保证仍在测邮箱门而非被国家门提前拦截。

## RED → GREEN 实际证据

首次新增真实 parser→project 测试运行：17 failed、20 passed。失败准确覆盖完整街道漏拒、inline 拆句误接受、纯国家/菜单误接受以及文本截断块。随后补测自身 dealer 描述出现 1 failed、49 passed，收窄第三方词组；补测疑问/possibly/maybe 标签出现 3 failed、50 passed，加入固定反证。

最终同一次定向命令：**588 passed，7.55 秒，无 skip**。其中新增街道/解析测试 53 项，真实 PostgreSQL research 测试 6 项。本机 `test_inventory_browser.py` 中受控 Playwright/browser 渲染测试实际通过，但仍需父代理在部署 Linux 环境复跑。所有网络传输为受控 fixture；没有外部 provider/API 调用。

HTTP 与 browser connector 一致性测试给两者相同合成 HTML，断言同样 text、原始字节、SHA256、tenant/uploader、WEB_SNAPSHOT kind/MIME、artifact 引用；browser 子进程 IPC 在此特定测试由固定帧模拟。受控实际 browser 测试另在既有 inventory_browser 文件中。

Ruff 对本轮全部 Python 文件通过；结构自检通过；git diff --check 通过。不重跑全库。

## 可复现命令与父 Linux 验证范围

使用项目 Python 执行（本机路径 `/Volumes/T7/Company/Auto_customer_acquisition/.venv/bin/python`）：

```bash
python -m pytest \
  tests/unit/test_inventory_street_address.py \
  tests/unit/test_inventory_public_evidence.py \
  tests/evals/test_inventory_evidence.py \
  tests/unit/test_inventory_collector.py \
  tests/unit/test_projection_diagnostic.py \
  tests/unit/test_web_search_discovery.py \
  tests/unit/test_web_page_reuse.py \
  tests/unit/test_inventory_page_failures.py \
  tests/unit/test_sourcing_page_extraction.py \
  tests/unit/test_deterministic_research.py \
  tests/unit/test_controlled_research_ports.py \
  tests/unit/test_inventory_search_guidance.py \
  tests/unit/test_inventory_query_binding.py \
  tests/unit/workflows/test_research_discovery.py \
  tests/unit/workflows/test_research_source_coverage.py \
  tests/unit/workflows/test_sourcing_public_search.py \
  tests/unit/workflows/test_demand_discovery.py \
  tests/unit/test_inventory_browser.py \
  tests/integration/test_research_discovery.py \
  -q --basetemp=/tmp/tos-street-complete
python scripts/check_boundaries.py
```

父按原验收再跑 Linux 原生 CLI/session/cap/lease；本轮未修改这些实现，也未把其历史通过计入 588。部署无需迁移、重新 probe 或额度变更。

## 局限与兼容代价

这是有限地址形式和固定反证检查，不是通用自然语言归属识别。窗口外第三方指代、未列词组、多语言改写仍可能无法识别；相邻无关新闻、另一国家、营销文字可能导致真实地址被拒。多块分散地址、P.O. Box、无门牌、非英语街道类型不新增接受。inline 字符合并和块定义会改变所有 PageSnapshot.text 消费者的派生排版，但不修改原始 artifact。

生产第 7 批的 raw/projected/rejection 固定计数与时间相关 static HTML 不能精确绑定具体模型候选。因此只证明通用代码问题已复现并由合成回归修复，不声称 Simba 对应的具体拒绝已被归因、不承诺候选入库或生产效果已恢复。company_name_unproven、discovery_only 的其他拒绝仍保留。


## Round1：超限不得丢掉地址后的反证（BASE 4ed2099）

独立审查 P2 成立：仅删除超限块会使已有地址失去相邻反证，国家门无法区分缺失内容与真实页尾。没有新 DTO 或字符串哨兵协议；共用 parser 的 `text()` 在 `_exhausted` 时抛出固定 `ValidationError("公开页面正文超限")`。任一块导致累计可见文本加分隔符超过 200,000 都拒绝整份派生正文；恰好 200,000 仍可返回。此修改没有删除末尾几块后继续返回的路径。

HTTP 沿已有验证错误通道关闭；browser 经已有包装返回固定 `PageReadFailure(browser_art, failed)`，没有页面或异常原文。错误通道不同，但都不交付 PageSnapshot。超限前已经保存的原始 HTML/artifact/SHA256 保持，不更改存储、网络、预算或国家/其他投影门。

先 RED：实际 parser→project 的“地址后 200001 字符+否定”及“累计恰到 200000 后普通否定块”均失败（2 failed，53 deselected，旧实现错误地接受候选）；HTTP/browser 超限交付测试另有 1 failed，55 deselected。最小实现后定向 **256 passed，2.95 秒，无 skip**。包括新的两个投影负例、HTTP/browser 拒绝与原件保留、旧边界测试按整页关闭更新。最后 Ruff、结构自检、diffcheck 均通过。

本轮测试命令：

```bash
python -m pytest \
  tests/unit/test_inventory_street_address.py \
  tests/unit/test_inventory_public_evidence.py \
  tests/evals/test_inventory_evidence.py \
  tests/unit/test_inventory_collector.py \
  tests/unit/test_projection_diagnostic.py \
  tests/unit/test_web_search_discovery.py \
  tests/unit/test_web_page_reuse.py \
  tests/unit/test_inventory_page_failures.py \
  tests/unit/test_inventory_browser.py \
  tests/unit/test_inventory_search_guidance.py \
  tests/unit/test_inventory_query_binding.py \
  -q --basetemp=/tmp/tos-street-r1-green
```

不重复原 588 全组，原 research/sourcing/PG 记录是前版基线，不冒充本轮新验证。父已报告 4ed2099 的 Linux 基线 614 passed；本轮提交仍待独立复核/Linux 验证，未部署。

兼容代价：超过可见文本上限的页面，即使早前某地址看起来可靠，也整体没有可用派生快照。这降低大页面召回，但避免无法证明反证窗口完整时接受候选。不增加 retry 或再次读取，不改现有失败分类、额度或工具池。未再次访问生产或调用 provider，不宣称真实候选已恢复。
