# Task 9 实施报告：安全网页候选抽取与确定性价格解析

## 结果

Task 9 已按 BASE `af511e257e2940739b00e83acb8d206f12ad134d`、Ruling P31/P32 完成。实现只接受已经过 Gateway 安全读取的结构化页面快照和可信 `SourcingNeedSnapshot`，调用一次严格模型端口，然后用确定性代码完成 JSON 白名单、逐字段精确原文锚定、同 Artifact 绑定、规格覆盖、数量档校验和 `Decimal` 价格解析。没有搜索、页面抓取、数据库写入、候选提交、联系人、邮件、报价或成本动作，也没有导入 `connectors.*`。

## TDD 证据

### RED

1. 先新增页面抽取测试与两张受控合成页面 fixture；首跑在收集阶段因 `agent_runtime.sourcing_agent.extraction` 不存在而失败。
2. 新增既有 `SourcingAgent` 的只读候选交接测试；首跑 3 个用例因 `build_page_candidate_review` 不存在而失败。
3. 增加 P32 硬化用例后，旧实现出现 4 个预期失败：非法单位仍生成金额、重复币种区间未归类为 `vague_range`、公共类型接受 `quoted`、明显私网快照进入模型。
4. 增加同一数量价格档必须共享一条 source quote 的用例；首跑因不同 quote 仍可组合而失败。
5. 增加 CHF 与小写 USD 金额文本用例；既有 Agent 原金额正则未覆盖 CHF，随后扩展时又用单独 RED 证明小写常见币种行为没有回归。

### GREEN

```text
pytest tests/unit/test_sourcing_page_extraction.py \
       tests/unit/test_sourcing_agent_boundary.py -q
=> 73 passed

pytest tests/unit -q
=> 5765 passed
```

受控测试覆盖：

- 有效前缀/后缀币种价格、精确 Decimal exponent/scale、16 位整数与 12 位小数边界；
- 区间、符号币种、正负号、指数、NaN/Infinity、零、逗号/混合分隔符、多金额/多币种、币种不一致、尾随文本和不可持久精度全部拒绝；
- 缺失/非法数量档、单位、币种和模糊区间只形成固定结构化拒绝原因，金额保持 `None`；
- supplier/product/spec/MOQ/quantity/unit/currency/price 分别要求 literal 位于自己的 exact quote，quote 又是页面正文的逐字子串；空白、Unicode/大小写改变均不做模糊归一；
- 未知规格不形成观察事实；规范化重复规格和价格档失败关闭；
- 提示注入、页面内 JSON、action/contact/email/phone/address、confidence/probability/score、需求/OEM/进口/运输推断、quoted/formal price、计算、notes、URL override 和 search summary 字段均不能进入草稿；
- 所有观察字段和价格档只能绑定同一不可变 Artifact；草稿序列化没有 action/contact/confidence/quoted 路径，repr 不含页面正文和 source quote；
- 模型端口异常被丢弃并在 `except` 外转换成固定 `ValidationError`，无 cause/context 或原异常文本；
- 既有 `SourcingAgent` 只能接收确定性映射的 required/offered spec、价格检查与快照元数据，不能改写观察值或生成任何三字母币种金额。

## 实现摘要

- `SafeSourcingPageSnapshot` 是 `agent_runtime` 本地结构 Protocol，只含正文、canonical public URL、UTC 观察时间、64 位哈希和 `ArtifactId`；Task 10 可直接传入 Gateway 返回的 connector 快照，不建立反向依赖。
- 快照边界拒绝无效元数据、控制字符、用户信息 URL、fragment、localhost、`.local` 和明显私网/保留 IP；模型只收到需求规格/数量/单位白名单与页面正文，不收到 Provenance 内部身份、凭证、headers、cookies 或搜索摘要。
- 模型 JSON 顶层与每个嵌套对象均使用 exact key set；重复 JSON key、非法常量、超长/畸形输出、控制字符及 normalized duplicate 均失败关闭。
- `SourcingObservedLiteral`、`SourcingObservedSpec`、`SourcingObservedPriceTier`、`SourcingPageCandidateDraft` 结构性分离可信 Need 要求、网页观察和确定性拒绝原因。每个价格档的 quantity/price/unit/currency 必须共享同一 exact quote 与 Artifact。
- `parse_observed_price_literal` 直接从正则捕获串构造 `Decimal`，不经过 `float`、不舍入、不选择区间端点；公共价格类型的 `price_basis` 只有 `Literal["indicative"]`。
- `SourcingAgent.build_page_candidate_review` 只做窄映射；网页原价不进入模型解释输入，既有输出护栏继续拒绝改写规格和生成金额。

## 最终门禁

- Ruff（全库及全部 touched Python 文件）：通过。
- Mypy（两个 touched 实现文件，`--follow-imports=skip`）：通过。
- `python3 scripts/check_boundaries.py`：7 项全部通过。
- `git diff --check`：通过。
- `scripts/scan_sensitive.py`（全部 touched 文件与 fixture）：通过。
- 全量 unit：通过；`tests/evals/sourcing` 目前只有既有 `.gitkeep`，本任务的 hostile/valid 样本明确放在 controlled fixture 层，不冒充真实模型或联网 eval。

## 残余风险

- Task 9 只定义结构边界，不能自行证明 DNS/重定向/robots/登录墙安全；Task 10 必须从现有 Tool Gateway 安全页面读取器传入快照，不能直接构造本 Protocol 绕过 Gateway。
- 解析器故意拒绝货币符号、千分位、区间和非 ASCII/不清晰单位；真实页面变体可能产生保守误拒，后续只能用新增受控 fixture 扩展确定性语法，不能让模型猜测。
- 本任务没有真实模型或真实网络调用；两张 fixture 都标为 `controlled_synthetic_fixture`，真实 Tavily/页面/模型状态仍为 `not_run`。
- 仓库标准全量 Mypy 命令当前仍有 Task 9 之外的既有 package-name/Decimal typing/reportlab stub 等错误；本任务未修改这些文件，touched 实现的隔离类型检查为零错误。
- Git 持续输出共享对象库 `._pack-*.idx` 的 `non-monotonic index` 警告；所有相关命令退出码正常，本任务未修改、删除或重打包共享 Git 对象库。
