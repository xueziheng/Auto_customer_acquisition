# Task 9A 实施报告：Unicode 金额词法器与确定性分类器

## 结果

Task 9A 已按 BASE `e542a65ba023680c0d9ab7d3980611b195c8f75b` 和 Ruling P38 完成。
旧 `SourcingAgent` 中五轮累积的金额邻近正则已全部删除，改由独立纯函数
`contains_untrusted_money` 对模型产生的摘要、替代影响和价格拒绝说明统一执行
有界、确定性的 Unicode 词法和句级分类。联系人、URL、单位、默认序列化、网页抽取、
工作流、域、Connector、Gateway、数据库和 Task 10 行为均未改动。

## TDD 证据

### RED

1. 先新增独立 parser contract；首次收集因 `money_guard` 模块不存在而失败。加入只返回
   `False` 的最小桩后，reviewer corpus 得到 `88 failed, 14 passed`，证明 joined / Unicode
   分隔 ISO、全部 `Sc`、Unicode 小数、位置化可信 span、price/cost 谓词和输入上限均由
   新实现负责，而不是复用旧正则。
2. 在 Agent 边界新增集成 RED：`AED250`、`AED—250`、`250/AED`、`.50`、全角小数、
   Arabic 小数和 raw surrogate 暴露旧实现的漏检或未包装 `UnicodeEncodeError`；同一
   corpus 分别放入 summary、substitution impact 和 rejection explanation，证明三个模型
   文本出口都必须调用同一 parser。
3. 完成首轮 GREEN 后，继续用独立 RED 覆盖 `RMB250`、全角 `ＡＥＤ２５０`、整数
   `250 per piece` / `250/piece`、反向严格标识 `Code 250/AED`，旧行为产生 5 个预期失败。
4. 对既有逗号小数边界补充 `2,50` 和 `１２，５０`，得到 2 个预期失败；同句重复
   `2.50` 改用逗号而非句界，独立证明可信 occurrence 只覆盖自己的原始坐标。
5. 新增 `Model AED 250 uses grade steel`，得到 1 个预期失败，证明 identity 组合不能靠
   宽前缀吞掉不完整的后续 prose。

### GREEN

```text
pytest tests/unit/test_sourcing_page_extraction.py \
       tests/unit/test_sourcing_agent_boundary.py \
       tests/unit/test_sourcing_money_guard.py -q
=> 415 passed

pytest tests/unit -q
=> 6107 passed
```

## 实现摘要

- parser 先按 `.?!;。！？；`、CR/LF、U+2028/U+2029 切分原始字符 span；小数点不会
  错切数字，可信 literal 和 identity 均不能跨句保护另一句。
- 数字扫描逐字符调用 `unicodedata.decimal`，识别任意 Unicode 十进制数字、leading
  decimal、ASCII/全角点号与逗号、Arabic decimal separator；token 同时保留原 span、
  canonical 数字串和 decimal 标记，不进行浮点或金额计算。
- 每个 `Sc` 字符单独记录并失败关闭。alpha token 用 NFKC/casefold 分类，因此小写、
  全角及与数字直接相连的 ISO-4217 code 都能识别；保留既有 `RMB` 安全别名。
- ISO/数字同句默认拒绝；只允许受控 `model|series|grade|type|part|sku|code` identity span。
  `price-sensitive Model AED-250` 和 brief 指定的双 identity 组合可通过，但额外 prose、
  不完整 grade 或第二段金额表达不能借前置 cue 获得豁免。
- price/cost token 只采用封闭词类。非豁免句只要同时含数字即拒绝；`cost impact`、
  `cost implication`、`price-sensitive` 只有在每个数字都被同句 exact trusted occurrence
  或严格 identity span 覆盖，且不存在 `:`、`=` 或指向数字的后置构造时才能通过。
- 任意未被 exact trusted occurrence 覆盖的小数独立拒绝。同一数字出现在别处、另一句
  或仅有相同 digit 文本都不能获得覆盖。
- 输入、可信 literal 数量、单项长度和总长度均有确定上限；Cc（句界 CR/LF 除外）、Cf、
  surrogate 和越界输入只返回 fail-closed boolean，不返回或记录原始文本。
- Agent 对 raw model output 的 UTF-8 计数显式包装 `UnicodeEncodeError` 为固定
  `ValidationError`，无原异常 cause/context 或原文泄漏。
- 为保持 Task 9 已审查行为，整数 `per`/斜杠 + 受控贸易单位也由 token 序列拒绝；没有
  重新引入金额 regex、模型、locale、网络或 `float`。

## 最终门禁

- Task 9 完整定向套件：`415 passed`。
- 全量单元测试：`6107 passed`。
- Ruff 全库及 touched format check：通过。
- touched Mypy：
  `mypy --follow-imports=skip agent_runtime/sourcing_agent/money_guard.py agent_runtime/sourcing_agent/agent.py`
  通过。
- 标准全库 Mypy 命令仍在既有
  `apps/composition_support/quotations.py` duplicate-module 映射处停止；该文件和错误均不在
  Task 9A diff 中。
- `python3 scripts/check_boundaries.py`：7 项全部通过。
- touched sensitive scan、staged sensitive scan、`git diff --check`：通过。

## 残余风险

- 分类器故意保守：未列入封闭 identity/非金额豁免的真实产品文案可能被拒绝；后续只能
  以受控 fixture 和 RED 测试扩展语法，不能回退到距离正则或模型猜测。
- ISO-4217 code 表是代码内静态确定性集合；新增或撤销货币代码需要显式更新并补 fixture。
- Task 9A 只处理模型输出金额护栏，不证明网页读取、重定向、额度或真实网络安全；这些仍
  属于 Task 10，且本任务没有执行真实模型或真实联网验收。
- Git 仍输出共享对象库 `._pack-*.idx` 的既有 `non-monotonic index` 警告；所有相关命令
  正常完成，本任务未删除、修改或重打包共享 Git 对象。
