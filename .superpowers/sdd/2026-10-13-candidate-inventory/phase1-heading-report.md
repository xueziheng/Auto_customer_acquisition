# 客户发现标题断言修复报告

## 变更

将 `tests/e2e/test_phase1_browser.py` 中 `/prospects/accounts` 的页面标题断言从过期文案“自动找客户”更新为页面当前标题“客户发现”。同文件无其他相同过期标题断言。未修改产品行为或其他业务断言。

## 验证

- `ruff check tests/e2e/test_phase1_browser.py`：通过。
- `git diff --check`：通过。
- `python scripts/check_boundaries.py`：通过，分层/依赖方向、金额 float、置信度数值、事件注册、租户过滤、AGENTS 覆盖和域结构检查均通过。
- `/Volumes/T7/Company/Auto_customer_acquisition/.venv/bin/python -m pytest -q --basetemp=/tmp/tos-heading-e2e tests/e2e/test_phase1_browser.py::test_phase1_browser_visible_reply_to_handoff_chain`：1 failed。浏览器已通过“客户发现”标题断言；随后同页的 console/HTTP 错误检查失败，客户发现库存 sources 和 allocations 接口返回 503。完整链路未通过，后续步骤未验收。原始输出留在本机 `/tmp/tos-heading-e2e.log`，未复制到报告。

## 限制

此次只同步测试标题契约。E2E 后续失败涉及库存接口服务不可用；本任务没有改动该行为，也未独立确认其根因或基线状态。未访问生产或凭证。

## Commit

`fbfd975cd316e494edad227b1b704f1b6406c366`
