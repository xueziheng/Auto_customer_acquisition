# apps/browser_worker/ —— 浏览器进程（Phase 1 受限）

## 职责

Playwright 操作的唯一运行位置。资源重、易崩溃，必须与其他进程隔离。

## 约束

- 每租户/账号/Run 独立 BrowserContext
- 操作 Trace（截图、快照、网络活动）进 artifact_store
- 合规边界见 connectors/playwright/AGENTS.md 与 docs/architecture/08
- 内存回收：定期重启浏览器实例（Playwright 长跑必涨）

## 入口

`main.py`：消费浏览器任务队列。
