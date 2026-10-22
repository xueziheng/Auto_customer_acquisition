# 有界采集规划修复报告

BASE：00e5ab7。仅实现父授权的搜索片段、预算反馈与选页指导；不访问生产、凭证、浏览器或真实 Provider，不新增模型/API 调用。

## 事实与结论边界

第 5 次生产任务三个来源的诊断均为合法 candidates 空数组，raw_count/projected/rejected 均为 0。父通过受信 RawArtifactStore 校验取得的两份时间相关公开网页摘要分别为第三方基金会项目介绍和法国企业联系页；时间窗相关性不能证明 exact job/artifact 关联，也不能重建原搜索 query、模型理由或所有已见材料。上述材料不足以支持目标非洲企业官网候选，不能断言模型返回空数组错误。

已复现的代码问题是搜索 description 被丢弃、初始上下文缺少 2 次搜索/7 次总工具的明确上限和真实剩余反馈。第三次搜索原本会耗尽整个会话。本补丁改善规划信息，不声称已恢复真实候选产出，也不证明这些问题是生产空结果的唯一原因。

## 改动

- collector 保留每条搜索结果最多 400 字符 description；整个搜索输出标注 content_is_untrusted。片段仅用于选页，不写入证据快照或候选证据。
- discovery 提供共用搜索/工具常量 2/7，collector 初始上下文和 enforcement、model prompt 使用同一值。
- 仅库存组合显式启用 remaining_searches 回调。DiscoverySession 给成功及可纠正错误的结果附加受信 meta：remaining_tools 扣除已预留、排队、失败操作；remaining_searches 来自实际搜索计数。executor 返回的 meta 不能伪造余量。到期 remaining_tools 为 0；remaining_searches 仍表示未消耗的搜索配额，不表示过期会话可再派发。
- prompt 明确搜索用尽后读已返回页或交付，非官网来源预留官网检索与读取；经营地址符合目标地区不同于向该地出口；允许 africa 按单一国家细化但遵守显式 country。官网搜索路径及 discovery_only=false 不是官网身份的证明，第三方介绍仍只是线索。
- 三个旧测试文件的合成搜索结果补齐既有 WebSearchResult.description 契约；更新两个旧结果精确断言以同时核验新 meta。

九条硬边界不变。证据接受谓词、同页证据、来源顺序/访问范围、未知错误关闭、权限/租约/账本、失败不退款、工具/搜索/模型额度均不改。第三次搜索仍使会话耗尽，未以“优化”修改原失败关闭语义。无新企业/domain seed、无自动重试或跨页拼证。

## 测试证据

TDD：首批预算/片段用例先因缺少 tool_budget/meta 失败，再实现通过。截止时间用例先显示 budget_exhausted 却 remaining_tools=7（1 failed, 4 passed），修正为 0 后通过。

定向回归：137 passed，6 skipped（macOS 无 Linux bwrap 的受控沙箱测试）。包括：

```bash
python -m pytest tests/unit/test_inventory_search_guidance.py tests/unit/test_inventory_collector.py tests/unit/test_inventory_page_failures.py tests/unit/test_projection_diagnostic.py tests/unit/test_codex_discovery_session.py tests/unit/test_codex_agent_bridge.py tests/unit/test_codex_provider.py tests/unit/test_inventory_model.py tests/unit/test_inventory_public_evidence.py tests/evals/test_inventory_evidence.py tests/integration/test_inventory_model.py -q --basetemp=/tmp/tos-guidance-regress-final
```

最后将第三次搜索测试加强为真实 collector 路径后，新 guidance 文件再次 6 passed。覆盖：错误扣额、队列预留、搜索用完仍可读取、第三次搜索关闭且零额外派发、过期零派发、恶意片段不得单独成为证据、提示词预算与地区/官网约束。集成 model 测试使用真实本地 PostgreSQL/Gateway 及受控模型端口，未调用真实模型。

改动文件 Ruff 通过；git diff --check 通过；项目虚拟环境 Python 的 scripts/check_boundaries.py 通过。系统 python3 为 3.9，不支持现有库的 Python 3.12 类型语法，首次结构命令因此失败；换项目 Python 后检查全通过，未为旧解释器改代码。

## 取舍与后续验收

每次搜索最多 20 项，片段最多额外 8000 字符（最多两次搜索），加上固定 prompt 指导会增加输入。原工具结果 100000 bytes 和模型输入/输出/轮数限制仍生效，没有提高阈值。片段可能误导选页或含提示注入：只作为不可信线索，确定性证据门继续依赖实际读取页。提示词只能改善规划，不能保证选中有效页面或产生候选。

父应在 Linux 重跑上述定向组、真实 CLI 的既有 3 项 synthetic 验收及既有 bridge/session/native sandbox 项，确认附加 meta/description 沿实际工具链传回而不影响最终 JSON 校验。无需迁移、全库 pytest 或新增 Provider 调用。独立审查通过后是否执行唯一一轮真实 UI 由父控制；本报告未声称部署或候选恢复。
