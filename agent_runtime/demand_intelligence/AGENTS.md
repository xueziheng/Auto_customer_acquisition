# demand_intelligence/ —— 需求情报

## 职责

解读需求信号、生成需求假设。设计稿第六节的六类发现路径（直接需求 / 企业变化 / 产业链推断 / 相邻品类 / 供应不满 / 卖家反向）在这里执行。

## 输入 / 输出

输入：未关联的 Demand Signal 批次、探索章程（Directive 的 discovery 段）、Playbook 排除清单。
输出：ChangeSet（create_hypothesis 条目，含 reasoning 与 evidence 引用）。

## 关键纪律

- 假设的措辞必须是「出现了 X 变化，可能存在 Y 需求，值得验证」，不是「一定需要 Y」
- 每条假设必须引用信号 ID（无证据假设过不了 guardrails）
- **不输出置信度数字**——只标证据等级，档位由代码推导
- 产业链推断（家具厂→五金件）标 `AGENT_INDUSTRY_INFERENCE` 等级，它单独不够格进触达（打分门槛会拦）

## 可用工具

web.search、web.read_page（经 tool_gateway）

## 禁用工具

email.*、任何写入类工具
