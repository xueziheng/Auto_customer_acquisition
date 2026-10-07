# apps/agent_worker/ —— Agent 任务执行进程

## 职责

消费 Agent 任务队列（workflow 步骤里的能力调用、Command Center 的即时请求），执行 CapabilityAgent.run，产出 ChangeSet 交回。

## 约束

- 模型调用经统一封装：超时、重试上限、token 计量（成本记进 Run）
- 并发受控（模型 API 限流 + 成本控制）
- ChangeSet 应用前过 guardrails；高风险变更提交 approvals 后挂起
- 崩溃恢复：任务幂等，重跑安全

## 入口

`main.py`：装配能力实例（注入 model、gateway、guardrails）→ 消费循环。
