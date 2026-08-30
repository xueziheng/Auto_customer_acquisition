# Task 9 实施报告：安全网页候选抽取与确定性价格解析

## 结果

Task 9 已按 BASE `af511e257e2940739b00e83acb8d206f12ad134d`、Ruling P31/P32 完成。实现只接受已经过 Gateway 安全读取的结构化页面快照和可信 `SourcingNeedSnapshot`，调用一次严格模型端口，然后用确定性代码完成 JSON 白名单、逐字段精确原文锚定、同 Artifact 绑定、规格覆盖、数量档校验和 `Decimal` 价格解析。没有搜索、页面抓取、数据库写入、候选提交、联系人、邮件、报价或成本动作，也没有导入 `connectors.*`。

Fix Round 1 已按 P33/P34 完成：价格单位改为受控 alias 到 canonical unit 的确定性映射；多价格档检查不再被单个完整档掩盖；模型金额文本护栏覆盖 ISO 4217、货币符号、价格关键词邻近数字和 `per unit` 表达；快照 URL 拒绝 legacy IPv4 数字写法及编码主机；`source_quote` 默认不序列化；选中字面值/摘录拒绝邮箱、URL、电话数据、页面指令及 Unicode `Cc/Cf/Cs`；孤立 surrogate 被转换为无异常上下文的固定 `ValidationError`。

Fix Round 2 已按 P35 完成：混合 hex/octal-like/decimal 组件的 legacy 数字主机全部拒绝，标准公网 IPv4 与普通域名保持可用；`urlsplit` 的畸形 IPv6 URL 异常被固定脱敏；联系 CTA、裸电话、带标签电话和无 scheme 域名路径不能成为 supplier/product/spec/unit 观察值；金额关键词从宽泛字符窗口收紧为有限连接词/标点语法，避免把成本影响、价格敏感型号和钢材牌号误判为金额。

Fix Round 3 已按后续审查完成：联系 CTA 扩展到 sales/now/reach out 等目标与动词，电话形状允许点分隔并继续按总十进制数字数判断，裸无 scheme 域名与域名路径均失败关闭；金额关键词语法进一步限制为紧邻标点、`is/was/starts at/begins at` 和可选 `approximately/about/around`，去除 `of` 与裸 `number + cost` 误报。

Fix Round 4 已按 P36 完成：联系、电话和点分域名改为确定性上下文语法，明确的 Model/Series/Part/SKU/Code/Version 标识可保护型号，但不能跨句保护后续联系数据；模型金额护栏拆为货币符号、ISO 金额、price/cost 谓词、反向谓词、per-unit、可信规格 Decimal span 六个小规则，不再依赖单一巨型正则。

## TDD 证据

### RED

1. 先新增页面抽取测试与两张受控合成页面 fixture；首跑在收集阶段因 `agent_runtime.sourcing_agent.extraction` 不存在而失败。
2. 新增既有 `SourcingAgent` 的只读候选交接测试；首跑 3 个用例因 `build_page_candidate_review` 不存在而失败。
3. 增加 P32 硬化用例后，旧实现出现 4 个预期失败：非法单位仍生成金额、重复币种区间未归类为 `vague_range`、公共类型接受 `quoted`、明显私网快照进入模型。
4. 增加同一数量价格档必须共享一条 source quote 的用例；首跑因不同 quote 仍可组合而失败。
5. 增加 CHF 与小写 USD 金额文本用例；既有 Agent 原金额正则未覆盖 CHF，随后扩展时又用单独 RED 证明小写常见币种行为没有回归。
6. Fix Round 1 先新增六组审查回归，定向首跑为 `35 failed, 89 passed`：任意字母单位、canonical alias、多档数量聚合、非硬编码币种/无币种价格表达、legacy IPv4/编码主机、原文默认序列化、联系/指令/Unicode 夹带和 raw surrogate 均分别暴露旧边界。多档测试的受控 quote 锚点修正后，得到预期的数量聚合 `1 failed, 2 passed`；单位与币种原本已采用 all-tier 聚合，不误报为本轮回归。
7. Fix Round 2 增加 P35 手工/表驱动 hostile 边界；修正 unit 价格档共同 quote 后得到 `16` 个真实 RED，随后用独立 RED 证明 `unit_literal=email us` 也必须按 P35 失败关闭：合计 5 个混合 legacy 数字主机绕过、2 个 `urlsplit` 原始 `ValueError`、8 个联系 CTA/电话/域名路径夹带和 2 个金额误报。实现后全部转绿，正常公网 IP、产品语言、尺寸、型号与成本影响说明继续通过。
8. Fix Round 3 先增加确定性表驱动边界变体，首跑得到 `13 failed, 151 passed`：7 个新联系 CTA、点分电话及裸域名绕过，1 个长数字产品型号误杀，3 个带近似词或起始谓词的金额漏检，以及 2 个规格/成本说明金额误报。既有 `/contact` 与 `Unit price 2.50 per piece` 用例保持通过，证明它们是防回归覆盖而非本轮新增缺口。
9. Fix Round 4 首轮组合/上下文矩阵得到 `21 failed, 169 passed`：7 个扩展 imperative CTA/Unicode 中点电话漏报、3 个明确型号误杀、6 个 price/cost 组合谓词漏报、2 个未受信 Decimal 漏报，以及 amount 整数说明和 2 个 ISO 型号误报；既有安全项继续通过。最小实现转绿后再加跨句上下文 fuzz，独立得到 3 个 RED，证明 Model/Series cue 不能跨句保护域名、电话或 ISO 金额。

### GREEN

```text
pytest tests/unit/test_sourcing_page_extraction.py \
       tests/unit/test_sourcing_agent_boundary.py -q
=> 193 passed

pytest tests/unit -q
=> 5885 passed
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
- 价格单位只接受受控贸易单位：例如 `pcs -> piece`、`grams -> g`、`cubic meters -> cubic meter`；通过白名单的 canonical unit 与原始 `unit_literal.literal` 分开保存。`widgets` 等普通非白名单词只形成 `unit_unclear`、金额保持 `None` 且不进入默认序列化；P35 起 `contact/email/call us`、电话及域名路径属于安全夹带，直接失败关闭。
- `source_quote` 仍可在可信内存对象中用于 Provenance，但字段设置为 `exclude=True` 且不进入 repr，默认 `model_dump`、`model_dump_json` 和既有 Agent 安全投影均不携原文。邮箱、URL、显式电话数据、提示注入及 Unicode 控制/格式/surrogate 字符不能成为选中字面值或 quote。
- `parse_observed_price_literal` 直接从正则捕获串构造 `Decimal`，不经过 `float`、不舍入、不选择区间端点；公共价格类型的 `price_basis` 只有 `Literal["indicative"]`。
- `SourcingAgent.build_page_candidate_review` 只做窄映射；网页原价不进入模型解释输入，既有输出护栏继续拒绝改写规格和生成金额。
- `SourcingAgent` 金额护栏使用静态 ISO 4217 集合且大小写无关，同时拒绝货币符号、紧语法的 `price/cost/amount/unit-price` 数字及数字 `per` 受控单位表达；不再使用“关键词后任意 24 字符”的宽窗口，正常尺寸、数量、型号、`cost impact ... 304 steel` 和 `price-sensitive ... AED-250` 文本可通过。
- 联系文本护栏使用小型确定性 CTA、域名和电话形状规则：电话号码只在候选片段累计至少 10 个十进制数字后拒绝，并用字母/连字符边界避免把 `ZX-13800138000-A` 等明确型号误判为电话。
- 金额关键词后只允许紧邻数字，或经有限谓词与可选近似词连接；数字在关键词前只接受 `number is/was the price/cost/amount`，因此 `amount of 304 steel`、`304 cost impact` 等规格说明不会被当作金额。
- P36 后联系 CTA 由动词、目标和 imperative 组合语法判定；电话 token 支持点、中点、括号、横线等常见分隔符并累计十进制数字。带 `+`、电话标签或没有明确 identifier cue 的长数字拒绝，Model/Series/Part/SKU/Code 上下文中的数字型号保留。
- 无 scheme 域名按 match 的 path 与上下文判定：path 永远拒绝，裸域名和非型号上下文拒绝，Model/Series/Version 只保护同一句中的点分型号；句号、换行及其他句界会终止保护。
- 模型金额文本先无条件拒绝货币符号、price/cost/unit-price 谓词、反向 price/cost 谓词和 per-unit 表达；ISO code + number 只在同句明确 identity context 中允许。其余带小数的 token 只有被 exact required/offered spec literal span 完整覆盖时才允许，显式价格谓词不会被可信字面量豁免；`amount` 本身不再作为价格谓词。

## 最终门禁

- Ruff（全库及全部 touched Python 文件）：通过。
- Mypy（两个 touched 实现文件，`--follow-imports=skip`）：通过。
- `python3 scripts/check_boundaries.py`：7 项全部通过。
- `git diff --check`：通过。
- `scripts/scan_sensitive.py`（全部 touched 文件与 fixture）：通过。
- 全量 unit：`5885 passed`；`tests/evals/sourcing` 目前只有既有 `.gitkeep`，本任务的 hostile/valid 样本明确放在 controlled fixture 层，不冒充真实模型或联网 eval。

## 残余风险

- Task 9 只定义结构边界，不能自行证明 DNS/重定向/robots/登录墙安全；Task 10 必须从现有 Tool Gateway 安全页面读取器传入快照，不能直接构造本 Protocol 绕过 Gateway。
- 解析器故意拒绝货币符号、千分位、区间和非 ASCII/不清晰单位；真实页面变体可能产生保守误拒，后续只能用新增受控 fixture 扩展确定性语法，不能让模型猜测。
- 本任务没有真实模型或真实网络调用；两张 fixture 都标为 `controlled_synthetic_fixture`，真实 Tavily/页面/模型状态仍为 `not_run`。
- 仓库标准全量 Mypy 命令当前仍有 Task 9 之外的既有 package-name/Decimal typing/reportlab stub 等错误；本任务未修改这些文件，touched 实现的隔离类型检查为零错误。
- Git 持续输出共享对象库 `._pack-*.idx` 的 `non-monotonic index` 警告；所有相关命令退出码正常，本任务未修改、删除或重打包共享 Git 对象库。
