# sourcing_agent/ —— 寻源分析（Phase 2 主用）

## 职责

Phase 1：辅助人工寻源——对人工录入的候选做规格比对分析、生成 MatchExplanation 草稿、识别诱导价特征。
Phase 2：自动执行公开寻源（web 搜索 → 候选提取 → 核验建议）。

## 关键纪律

- 所有价格标 INDICATIVE（硬边界 7 的入口在寻源）
- 规格比对逐项给出 SpecMatchLevel，UNKNOWN 不得乐观合并为 EXACT
- 诱导价识别是「建议拒绝 + 理由」，最终拒绝由人（Phase 1）或核验规则（Phase 2）决定
- 图片处理遵守 sourcing 域 AGENTS.md 的权属规则

## 可用工具

web.search、web.read_page、artifact.store
