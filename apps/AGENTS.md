# apps/ —— 可运行进程

## 职责

七个进程（见 `docs/architecture/00-overview.md`）。**这一层只做编排与呈现**：装配依赖、暴露 HTTP、驱动 worker 循环。业务规则一律在 `domains/`，出现业务 if 就是放错了。

## 进程清单

| 目录 | 进程 | Phase |
|---|---|---|
| `api/` | FastAPI 服务 | 1（深） |
| `web/` | Vue 3 前端 | 1（浅骨架） |
| `agent_worker/` | Agent 任务执行 | 1 |
| `scheduler_worker/` | 状态机扫描与定时任务 | 1（**单副本**） |
| `browser_worker/` | Playwright 隔离进程 | 1（受限） |
| `notification_worker/` | 通知投递 | 1 |
| desktop-tauri | macOS 桌面端 | 3（不建目录，边界见 ROADMAP） |

## 装配规则

依赖注入在各进程入口（`main.py`）完成：Repository 实现、EventBus、ToolRegistry、ConnectorRegistry、订阅关系全部在这里接线。**域内部不知道谁实现了它的 Protocol。**

## 依赖白名单

```text
允许   一切下层
禁止   apps 之间互相 import（进程间协作靠数据库与事件，不靠 HTTP 互调）
```
