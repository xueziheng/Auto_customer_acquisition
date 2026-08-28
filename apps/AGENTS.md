# apps/ —— 可运行进程

## 职责

七个已实现进程和一个未来桌面进程（见 `docs/architecture/00-overview.md`）。**这一层只做编排与呈现**：装配依赖、暴露 HTTP、驱动 worker 循环。业务规则一律在 `domains/`，出现业务 if 就是放错了。

## 进程清单

| 目录 | 进程 | Phase |
|---|---|---|
| `api/` | FastAPI 服务 | 1（深） |
| `web/` | Vue 3 前端 | 1（浅骨架） |
| `agent_worker/` | Agent 任务执行 | 1 |
| `scheduler_worker/` | 状态机扫描与定时任务 | 1（**单副本**） |
| `browser_worker/` | Playwright 隔离进程 | 1（受限） |
| `notification_worker/` | 通知投递 | 1 |
| `email_feedback_worker/` | Gmail 投递反馈拉取与整页提交 | 1（**按租户＋邮箱别名单副本**） |
| desktop-tauri | macOS 桌面端 | 3（不建目录，边界见 ROADMAP） |

## 装配规则

依赖注入在各进程入口（`main.py`）完成：Repository 实现、EventBus、ToolRegistry、ConnectorRegistry、订阅关系全部在这里接线。**域内部不知道谁实现了它的 Protocol。**

`email_feedback_worker` 的零参数入口只接受显式环境配置。生产 Gmail base URL 固定为
`https://gmail.googleapis.com`；只有 `TRADEOS_DEV_MODE=true` 才允许带显式端口的
loopback HTTP，供本地验收使用。SIGTERM 只设置停止标志，必须完成正在处理的整页事务后
再退出；provider 429 维持 live/ready，但 ready payload 标记 `provider=degraded`。

## 依赖白名单

```text
允许   一切下层
禁止   apps 进程之间互相 import（进程间协作靠数据库与事件，不靠 HTTP 互调）
```

`composition_support/`是非进程机械装配库，仅供API与scheduler引用，不是新增进程。
该库只能向下或依赖本库，绝对/相对导入任何进程模块都禁止；进程仍不得互导。
共享的是代码，不是parser、Gateway、slot、域、engine或其他全局运行实例。
