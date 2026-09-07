# Task 5 浏览器、生命周期与运维交付报告

## 结论

Task 5 已完成。新增持久内测真实浏览器 E2E 在生产源
`ecfb4d2d959ade7ffa143b7b9ad1b8e29cde4242`、测试修订
`c1cdad875bea547e20fdaefbf006ac31f8ec9c3b` 上通过：1 passed，28.52 秒。未发现生产缺陷，未修改
生产代码，未运行旧 9318/Web 411 全量门禁。

## 实际命令与结果

```sh
command -v npx
# 通过；Browser插件在当前会话不可用，任务明确要求pytest测试文件，使用Python async Playwright。

.venv/bin/python3.12 -m ruff check tests/e2e/test_web_pilot.py
.venv/bin/python3.12 -m py_compile tests/e2e/test_web_pilot.py
.venv/bin/python3.12 -m pytest tests/e2e/test_web_pilot.py --collect-only -q
# ruff/py_compile通过；1 test collected。

PATH="$PWD/.venv/bin:/Users/xueziheng/.nvm/versions/node/v24.15.0/bin:$PATH" npm --prefix apps/web run gen:api
# openapi-typescript 7.13.0通过；apps/web/src/api/api.d.ts无diff。

PATH="/Users/xueziheng/.nvm/versions/node/v24.15.0/bin:$PATH" npm --prefix apps/web run build
# vue-tsc与Vite 8.2.1生产构建通过，164 modules transformed。

env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 TRADEOS_REQUIRE_E2E=1 \
  .venv/bin/python3.12 -m pytest tests/e2e/test_web_pilot.py -q --tb=short
# c1cdad875bea547e20fdaefbf006ac31f8ec9c3b：1 passed in 28.52s。

.venv/bin/python3.12 scripts/check_boundaries.py
# 七类结构检查全部通过。

.venv/bin/python3.12 -m alembic heads
# 0060 (head)
```

版本探针：Python 3.12.14、Node 24.15.0、Docker Server 29.5.3、Playwright Chromium
151.0.7922.34。

## 测试修订记录

- `982978c`：新增测试。首次运行因 SQLAlchemy Row 未归一化而失败，未到浏览器生产断言。
- `264a867`：Row 转 tuple；运行完成主体后因测试重复关闭已由 Playwright 关闭的 context 失败。
- `e6a4245`：移除重复关闭；运行在页面重复姓名 locator 的 strict mode 失败。
- `398000a`：限定首个团队业务 marker；运行因测试 `await` 表达式优先级失败。
- `831902d`：修正 await；完整链路完成，最终 console 门禁把刻意断言的 401/403/网络中断误归为未知错误。
- `c1cdad8`：只过滤 Chromium 对已断言负路径的固定资源失败记录；其余 warning/error 与全部 pageerror
  仍失败。最终 E2E 通过。

上述均为新测试缺陷；没有据此修改生产实现，也没有虚构 RED。

## 覆盖证据

`tests/e2e/test_web_pilot.py` 使用生产构建、同源真实 Cookie 与独立随机 owned profile，覆盖：

- boss/sales登录、当前数据库员工角色、boss 200与sales 403服务端授权、刷新保持；
- 完整stop/start及应用/存储状态，团队业务fixture数据库canonical hash与对象SHA-256保持；
- logout网络失败不假称撤销、直接重试、最终204/服务器401；
- 两标签共享Cookie jar、延迟logout响应、Web Locks保证`logout_headers`先于`login_request`，旧业务视图
  在账号切换时卸载；
- disable/enable/reset-password、旧会话与旧密码拒绝、新密码登录、最终logout；
- 冷备份到新目录、恢复到新owner/新卷/新端口、源profile独立不变；
- 把源旧Cookie值仅在测试进程内改挂到目标正确Cookie名后仍401，随后重新登录；
- 恢复目标390×844无文档级横向溢出、无Vite overlay或未解释运行时错误。

证据路径：

- `tests/e2e/test_web_pilot.py`
- `docs/operations/web-internal-pilot.md`
- `docs/acceptance/2026-09-07-web-internal-pilot.md`
- `docs/operations/web-core-capability-matrix.md`
- `HANDBOOK.md`

未生成 screenshot、trace、HAR 或 HTML 报告，避免密码、Cookie、CSRF、请求或私有配置进入持久产物。

## 文件变化

- 新增 `tests/e2e/test_web_pilot.py`：完整浏览器、账号、stop/start、备份恢复与精确owned清理。
- 新增 `docs/operations/web-internal-pilot.md`：环境、非运行政策占位形状、CLI、getpass、权限、停启、
  冷备份恢复、磁盘与同故障域风险。
- 新增 `docs/acceptance/2026-09-07-web-internal-pilot.md`：精确版本、通过事实、限制与安全证据。
- 更新 `docs/operations/web-core-capability-matrix.md`：把受控、持久内测和独立Linux证据分栏，区分
  历史0059与当前0060。
- 更新 `HANDBOOK.md`：增加本机持久Web内测入口与边界索引。

## 范围限制与关注点

未执行物理机器重启、真实用户profile、真实Provider/模型/供应商/邮件、共享TLS/反向代理、其他浏览器、
备份加密/PITR/自动保留/异地灾备或桌面端。Browser worker生产任务源仍disabled，桌面扩展契约保留。
合成账号、Territory Assignment和对象不证明需求发现、客户验证、成交或北极星指标改善。

Git命令持续报告共享仓库既有AppleDouble pack sidecar的`non-monotonic index`警告；提交均成功，未按任务
边界修复或清理共享Git对象。历史未跟踪output保持未动。

## Owned资源清理

每次运行都只登记本测试创建的source/restore profile。teardown先重读并核验exact owner、完整资源集合、
容器ID/镜像/挂载、卷名/创建时间与owner标签，再停止和删除登记资源；各次cleanup error为0。生产
`stop`路径仅停止应用与存储并保留profile/容器/卷，测试删除行为只存在于合成fixture。
