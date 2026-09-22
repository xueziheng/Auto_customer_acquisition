**I1 — 新增测试的断言重写会在失败时输出认证材料。** — ADDRESSED。`tests/unit/test_authentication_passwords.py:20-27` 与 `tests/unit/test_authentication_passwords.py:124-130` 先在断言外完成密码 repr、CSRF 与摘要比较，断言只接收布尔量和固定 `AUTH_*` 错误码；`tests/integration/test_web_authentication.py:56-77` 对恢复后的 CSRF、数据库摘要及会话 repr 采用同一形状，不再把原始会话或 CSRF 交给 pytest 断言重写。

### New Breakage in the Fix Diff

None. `tests/integration/test_web_authentication.py:471-583` 的三种故障注入分别命中既有 CSRF、密码 repr 和会话 repr 测试节点；子进程 stdout/stderr 全量捕获且不拼入异常，父进程仅断言固定退出/guard 布尔值和材料缺席，并在断言前删除捕获对象。fix diff 的其余修改保持原测试行为，只把可能携带认证对象、异常文本或子进程状态的表达式预先归约为安全布尔量。

### Out-of-Scope Observations

None.

### Checks

- Implementer report names the focused RED/GREEN regression and the final two-file run, reporting `3 failed`, then `3 passed`, then `26 passed`; it also reports ruff, boundary, and diff checks passing with no skips or warnings. Per scope, these same-version tests were not re-run.
- Read the complete supplied fix package `review-6c8a2de..cef101e.diff`; no git commands or source/branch mutations were performed.

### Verdict

**Fix round:** All findings addressed, no new Critical/Important breakage.

Counts: Critical 0; Important 0; Minor 0.
