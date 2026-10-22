# 实际检索文本绑定修复

BASE：b1bf209。按 search-query-binding-brief.md 独立实施；未改临时额度维护工具或父 acceptance 文档，未访问生产、凭证或外部 Provider。

## 已证问题与改变

合成模型仅给出 Acme products 时，旧 collector 实际派发 Acme products official company website contact，缺少任务三轮车与 Nigeria 等锚点。17 项 RED 明确复现实际 query 缺失、组装函数未实现及矛盾地域未在派发前拒绝。该代码问题可以离线证明；历史六批 raw0 没有精确 query 记录，不能据此断言历史全部命中或这是唯一根因。

新增 apps/scheduler_worker/inventory_query.py 纯函数，collector 只替换传给既有 searcher 的 query 文本：

- 固定保留已规范排序的首个 product_tag、最具体地域和原来源 strategy。产品精确复用 TAG_ALIASES 的第一个既有别名（“三轮车”→tricycle，“电动三轮车”→electric tricycle），未知中文/英文保留原词，不串全部别名、不改词表或 job.query。
- 有 country 使用既有 COUNTRY_NAMES 英文名，未知码单独使用时保留代码；无 country 且 africa 才用 Africa，无地域不自行添加。africa + 不在 AFRICA_COUNTRIES 的 country 本地固定拒绝，包括 FR 与未知码组合。
- 固定段先留空间；补充词规范 Unicode 空白，拒绝控制/格式/代理等 Unicode C 类字符，只保留 Unicode 字母数字组成的普通词，剔除 AND/OR/NOT 与已出现词，按完整词填入剩余 400 字符/50 词。单词装不下便跳过，后续能装下的词仍可保留。受信 LinkedIn site: 策略独立保留，模型补充中的 site:、减号、引号、括号等无法成为检索操作符。
- 固定段自身超限或范围矛盾沿固定 failed:source_scope:req:0 关闭，零 searcher 外部派发，不输出输入原文。原 guard、来源顺序、搜索次数计数、2search/7tool、page_id/failed-host/Gateway 身份均保持。

## 取舍与限制

首产品只是沿已有 category 的检索召回选择；InventoryQuery 已对标签排序，首项不是用户优先级，且最终证据门仍逐个要求全部产品标签。锚点使任务范围确定性出现在文本中，不保证召回/排序改善，也不等同搜索引擎严格国家过滤。仍可能保留模型的其他国家词或自然语言矛盾（例如 excluding Nigeria）；本补丁只移除操作符语法，不宣称理解这些句子。

通用 Tavily transport 未改，其 country 参数仍被忽略、supports_country_boost 仍 false。未知国家码不新增国家地址证据能力。产品/国家/官网事实仍仅由实际读取页面证据确认；query 与搜索摘要不能充当证据。未新增公司、国家种子列表、人工 URL、跨页拼证或额外调用。

UI/API 的 InventoryQuery 目前仍允许 country=FR + region=africa 以及某些含控制字符标签创建任务；本次在本地 composer 拒绝，尚未补入口验证，可能表现为任务创建后采集失败。这是独立输入验证缺口，不声称已修复。固定输入拒绝仍消耗已预留工具槽，不退款、不改额度关闭语义。

## 验证

RED：新 tests/unit/test_inventory_query_binding.py 17 failed，实际受控 search 端口捕获旧派发文本，非仅 prompt 断言。首轮 GREEN：31 passed（新文件及 collector/guidance）。增加空白与别名边界、France 正文不能被 Nigeria 锚点/摘要变成 NG 证据后，最终相关 134 passed：

```bash
python -m pytest tests/unit/test_inventory_query_binding.py tests/unit/test_inventory_collector.py tests/unit/test_inventory_search_guidance.py tests/unit/test_inventory_page_failures.py tests/unit/test_discovery_tool_usage.py tests/unit/test_codex_discovery_session.py tests/unit/test_codex_agent_bridge.py tests/unit/test_inventory_public_evidence.py tests/unit/test_projection_diagnostic.py tests/evals/test_inventory_evidence.py -q --basetemp=/tmp/tos-query-binding-regression
```

覆盖 NG、ZA、africa、无地域、未知码、未知中文；已有两个产品别名、排序多标签不改事实；400/401字符、50/51词、长单词、Unicode空白/控制、-Nigeria、site、引号/OR/NOT、受信LinkedIn策略、企业名跟进；目录先读/官网后读、第三次search拒绝但保留read槽、失败host禁重派、工具使用计数与证据eval。所有结果由受控本地端口产生，没有真实 Provider 调用。

改动文件 Ruff、git diff --check、项目 Python scripts/check_boundaries.py 通过。无迁移、无全库测试。

父独立审查后可在 Linux 重跑上述组并按既有模型/原生CLI验收组合检查；部署与新的 NG UI 场景由父执行。该场景的国家约束来自确定性检索文本和最终交付谓词，不来自 Tavily country boost；不应与前六批宽泛 Africa 条件直接当作同条件 A/B。当前不宣称库存候选恢复。
