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
研究成功读取的来源容量还受max_signals限制；失败读取仅消耗pages_used尝试预算。
同URL+hash的合法原文信号由确定性代码展开到每份查询/线路，不依赖模型重复输出。
每来源优先一条信号，额外观察超预算明确停止，假设只能引用实际记录；Run区分
planned_discovery_lanes与已持久化证据的discovery_lanes。LF/TAB逐字证据不得压平。

多来源执行只重排已确认查询，来源与国家/品类共同轮转。每条查询按剩余页面预算与
剩余可执行查询数向上均分尝试份额；仅可跳过的页面拒绝允许在份额内继续，首个成功
读取（含去重页面）即换查询，之后再轮流加深。失败同样消耗页面预算，页面和信号
总上限不变；预算足够时为后续查询保留至少一次页面机会。计划、成功搜索、持久留证
的来源方向分别计数，不能混用。
免费额度失败后只处理已读取原件，不再访问已被adapter作废的批次；finally统一清槽。
禁止在workflow按URL缓存而跳过页面Gateway；网页复用只在全部工具检查后执行，
页面尝试预算仍逐次计数。登录墙、禁止访问和危险重定向仅跳过该页，权限或政策
拒绝不属于可跳过页面错误。
