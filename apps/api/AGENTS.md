# apps/api/ —— HTTP API（Phase 1 深）

## 职责

服务 Web 前端的 FastAPI 进程。router 按 16 个产品页面分组（设计稿第四十二节），每个 router 只做四件事：解析请求 → 判权装饰 → 调域服务 → 返回域 DTO。

**不在 router 里写业务规则、不在 router 里定义业务结构**（请求/响应模型引用域的 `schemas.py`，只在需要时裁剪字段）。

## Router 与页面对应

| router 文件 | 页面 | Phase |
|---|---|---|
| `command_center.py` | Agent Command Center（老板/员工自然语言入口） | 1 |
| `demand_radar.py` | Demand Radar（信号/假设/需求簇） | 1 |
| `customer_discovery.py` | Customer Discovery（潜在企业与评分） | 1 |
| `campaigns.py` | Campaign Center | 1 |
| `inbox.py` | Smart Inbox | 1 |
| `crm.py` | CRM & Opportunities | 1 |
| `products.py` | Product & Supply Center | 1（只读为主） |
| `sourcing.py` | Sourcing Center（Phase 1 人工操作界面） | 1 |
| `costing_quotes.py` | Deal Cost & Quote | 1（人工录入） |
| `team.py` | Team & Territory | 1 |
| `work_uploads.py` | Work Uploads | 1 |
| `commitments.py` | Commitment Center | 1 |
| `approvals.py` | Approval Center | 1 |
| `runs.py` | Run Center | 1 |
| `settings.py` | Playbook & Connections | 1 |
| `billing.py` | Credits & Billing | 3（不建） |

## 三条纪律

1. **判权装饰器 + 服务层判权双保险。** router 上的装饰器是第一道，域服务内是第二道——worker 不走 router，只有服务层判权才能覆盖它。
2. **产品客户视图只经 `get_customer_view`。** 对外暴露的产品接口不允许调内部视图方法（成本泄漏是永久损伤）。
3. **响应里的每个关键字段带 provenance 摘要**——前端「为什么判断高意向」的展开数据从这里来。

## 入口

`main.py`：FastAPI 实例、依赖注入装配、中间件（租户上下文、审计、错误转换——把域错误映射为结构化 HTTP 响应，`is_retryable` 转 Retry-After）。
