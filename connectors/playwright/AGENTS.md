# connectors/playwright/ —— 浏览器操作（Phase 1 受限使用）

## 优先级

**能用 API 就不开浏览器**：官方 API → 公开 HTTP 页面 → 确定性 Playwright Adapter → 受限 Browser Agent → 人工接管。

## 允许 / 禁止（docs/architecture/08-compliance.md）

允许：读公开企业页面、提取公开目录、下载公开文件、操作经授权的本地登录页、页面结构变化时辅助分析。
禁止：绕过验证码、绕过登录限制、读取未授权 Cookie、违反平台限制批量发私信。

## 隔离

每个租户/账号/Run 独立 BrowserContext，状态不共享。操作留 Trace（截图、快照、网络活动）进 artifact_store 作证据链。

## 运行位置

只在 `apps/browser_worker` 进程内运行（资源重、易崩溃，必须隔离）。
