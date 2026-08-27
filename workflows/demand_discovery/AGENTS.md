# workflows/demand_discovery/ —— 需求探索流程（Phase 1 浅）

## 触发

探索排程（scheduler 按 Directive 的探索配比周期性启动）或老板显式指令。

## 主要状态

```text
plan_search（demand_intelligence 设计搜索策略，按配比分配需求优先/能力辅助）
→ execute_search（web.search / web.read_page，产出信号）
→ generate_hypotheses（demand_intelligence，经 guardrails）
→ score_and_queue（打分门槛 → 通过者进触达队列）→ complete
```

## 涉及域

demand（信号与假设）、organization（Playbook 排除）、opportunities（门槛）、directives（配比）。

## 关键约束

每轮探索有信号数与页面读取数上限（防失控消耗）；按策略分组记录产出指标（Phase 2 自适应分配器的训练输入）。

## Phase 2 v2

保留v1定义及handler，v2按已确认提案模式分流，不能把旧提案默认改成研究。研究专属
编排在research.py，必须显式免费Tavily组合，未配置返回unsupported而不调用Brave。
研究最终handler不持有联系人队列、验证、发送或报价端口。来源归属由确认查询与原页
计算，缺官网/所在地证据只存待核验Signal。所有页面引用与线路在模型返回后重新绑定。
