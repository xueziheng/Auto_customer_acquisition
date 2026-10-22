# 页面失败修复报告

基线：4868aff。唯一实施代理 inventory_live_fix；未访问生产、凭证或实际模型上下文，未派子代理。范围来自 live-page-failure-brief.md 与父代理后续授权。九条全局硬边界均未改变。公开页面合成正文可作为测试证据；原始 SDK/浏览器错误、请求头、凭证及实际模型上下文不进入诊断。

## 已实现

- page_id 从本次会话 ULID 改为 page_1 等稳定短编号；只查询本次 pages 映射。未知编号固定返回 page_id_unknown，仍扣原 7 次工具预算。工具/schema 错误、跨来源搜索门、候选需成功官方页证据均不放宽。
- 正文/robots 已收到 HTTP >=500 分别产生 page_http/page_robots + http_error；网络 OSError/TimeoutError/HTTPException 为 network_unknown。只有明确页面失败允许选择别的站点，不将所有 PROVIDER_TRANSIENT 当成已确定失败。
- collector 对失败 host 保守隔离（含 scheme/port 变化），跨本 job 的来源、新 page_id、重新搜索及 browser 均不再派发该 host。不同 host 可继续并形成候选。按 tenant/job/lease 绑定；回收 lease 的任务原有 attempts 门禁止再次外部执行。未引入持久 URL 记录或自动重试。
- 浏览器保存主文档首个已知拒绝，正常阻止子资源不会伪装为主文档失败。URL/初始化/导航/HTML/截图/trace/关闭/IPC/产物阶段只返回固定枚举。初始化失败只是失败阶段，不能断言环境故障。子进程非零错误帧严格检查字段及枚举；未知帧关闭。清理竞态不再覆盖首个错误。
- 新私有 WebPageFailureSlot 将固定错误穿过实际 Gateway，绑定 task/tenant/run/user/URL，单次消费且 finally 清理；父子 task 不共用。HTTP/browser 分开槽。只有账本返回匹配的失败 category/status 才恢复投影；权限、限流、reconciliation 不能被降格。Gateway 核心管线未改。
- 未确定失败写入既有 source attempt 有界 outcome：如 failed:page_robots:unknown:3、failed:browser_init:res:4。已知可恢复页面失败仍作为工具 page_unavailable 反馈，Gateway 保留原固定分类；不新增自由文本日志。

## 事实与限制

第三生产 job 的 provider_transient 只能证明 connector 阶段失败，不能事后确定 HTTP5xx 还是网络未知；browser unexpected 也不能证明环境故障。本补丁修复可复现的分类丢失/会话过早结束，不宣称真实采集已经恢复。

robots 已知5xx/网络未知有单独阶段；传统 robots 拒绝、解析器严格拒绝及页面访问拒绝继续使用原 page_access_forbidden。未修改 robots 解析器、Crawl-Delay 支持、HTTP→HTTPS/www 规范跳转门。host 隔离比单URL更保守，代价是某页失败后同站其他有效页也不再尝试。

## 验证

先 RED 后实现：缺少 typed 页面分类、未知页编号杀会话、浏览器主文档原因丢失、缺失浏览器二进制/启动失败分类、畸形错误帧以及清理竞态覆盖原错误均已复现。补充回归验证不同 host 继续、同 host 新ID/browser/后续来源不重派、7次预算、无证据候选拒绝、task槽隔离、Gateway 权限/限流/reconciliation 保持关闭。

最终定向回归：226 passed，1 条原有 Alembic path_separator DeprecationWarning，13.30秒。包含本机真实 Chromium（合成公开页 transport、无生产网络）、真实 PostgreSQL Gateway/租户/job/source/fence 及迁移回归。

运行命令（本机 Python 为项目 .venv/bin/python）：

```bash
python -m pytest tests/unit/test_web_search_discovery.py tests/unit/test_inventory_collector.py tests/unit/test_codex_agent_bridge.py tests/unit/test_codex_discovery_session.py tests/unit/test_inventory_acquisition_driver.py tests/unit/test_inventory_page_failures.py tests/unit/test_inventory_browser.py tests/unit/test_web_page_reuse.py tests/integration/test_inventory_page_gateway.py tests/integration/test_inventory_model.py tests/integration/test_prospect_inventory.py --basetemp=/tmp/tos-page-verified -q
python scripts/check_boundaries.py
```

项目 Python 3.12 结构自检通过；首次系统 Python 3.9 运行无法解析既有3.12语法，随后使用项目运行时通过。改动文件 Ruff 和 git diff --check 均通过。未执行全库 pytest/无关 E2E，未宣称全库静态类型检查通过。

## 交父代理 Linux 验收

在已准备的隔离验收目录/测试数据库及配置好的原生 Codex binary 下：

```bash
python -m pytest tests/unit/test_inventory_browser.py tests/unit/test_inventory_page_failures.py tests/unit/test_web_page_reuse.py tests/integration/test_inventory_page_gateway.py tests/unit/test_inventory_codex_cli.py --basetemp=/tmp/tos-page-linux -q
python scripts/check_boundaries.py
```

须设好 TRADEOS_TEST_CODEX_BINARY 指向已验证二进制，否则3个 native CLI 用例会 skip，不能记为通过。Linux 真实浏览器/native CLI 及部署后的新 UI job 尚待父代理验收。仅取新job固定 source outcome、Gateway分类、预算/计量，不重放原失败URL；不把 Provider 合成测试当生产恢复证据。
