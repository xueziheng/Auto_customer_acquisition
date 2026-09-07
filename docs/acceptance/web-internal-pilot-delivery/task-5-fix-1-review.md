- **I1 — 直接换号与两个已挂载标签共同失效：ADDRESSED。** `tests/e2e/test_web_pilot.py:568` 先让 B 保持登录表单、A 登录 boss 并显示团队资料；`:576` 直接从 B 登录 sales，期间没有先退出 A；`:577` 检查 A 返回登录表单、团队资料和业务壳均移除，`:582` 与 `:592` 分别检查 B 的服务端当前身份和团队 API 403。`:599` 随后让 A 重新挂载共享 sales 会话，确认两个标签都有退出按钮；只在 B 退出，`:607` 检查两页回到登录表单及 A 业务壳清空。原来“本来已经退出的 A”造成的假阳性前提已消除。
- **I2 — 真正响应交付与 Web Lock 排队顺序：ADDRESSED。** `tests/e2e/test_web_pilot.py:635` 用 `route.fetch()` 实际请求服务端，核验 204 后才设置响应就绪事件；`:639` 暂缓 `route.fulfill(response=response)`，延迟点已从发请求前移到向页面交付真实响应前。`:392` 检查浏览器同名锁同时存在 held/pending；`:672` 等排队成立后断言尚无 logout response 或 login request。`:644` 通过页面 response 事件记录实际退出响应，`:680` 校验该事件先于 B 的 login request；`:687` 刷新后再次核验 sales 当前服务端身份，并保留团队 API 403。此处证明的是 response 事件顺序，没有冒充额外的网络 requestfinished 观测。`docs/acceptance/2026-09-07-web-internal-pilot.md:32` 和 `.superpowers/sdd/2026-09-07-web-internal-pilot/task-5-report.md:131` 已按实际测量方式修订，撤回旧 logout_headers 证据。
- **I3 — 退出后的隔离 context 旧 token 重放：ADDRESSED。** `tests/e2e/test_web_pilot.py:335` 在退出前按端口 Cookie 名和 `/api` 路径保存会话材料，仅以 SecretStr 留在测试进程；`:357` 独立检查正常浏览器 jar 已清除该 Cookie；`:363` 新建 API request context，以原名称、原路径和同一源域装入退出前材料，`:381` 请求 session 并检查 401，随后释放响应和 context。重试退出、共享会话普通退出、最终普通退出分别在 `:538`/`:560`、`:605`/`:612`、`:739`/`:742` 应用该检查。401 不再仅来自清空后的原 jar；文档 `docs/acceptance/2026-09-07-web-internal-pilot.md:27` 与 `:36` 已准确对应。
- **M1 — 文档撤回要求：ADDRESSED；collector 收紧：NOT ADDRESSED，按控制者裁定延期，非本轮阻塞项。** 现有宽过滤仍位于 `tests/e2e/test_web_pilot.py:472`；本轮没有宣称已修复。`docs/acceptance/2026-09-07-web-internal-pilot.md:43` 将结果限定为保留事件计数，`:46` 明确任意 failed-resource 401/403 及模拟故障窗口全部 console warning/error 的盲区；`.superpowers/sdd/2026-09-07-web-internal-pilot/task-5-report.md:145` 明确撤回过强描述。`docs/operations/web-core-capability-matrix.md:37` 与 `HANDBOOK.md:791` 同步限制结论。

## New Breakage in the Fix Diff

- 无新增 Critical、Important 或 Minor 发现。该 diff 仅调整测试和证据文档，没有生产代码变化。

## Out-of-Scope Observations

- 已有 Minor M1 继续交最终全分支审查处理，位置 `tests/e2e/test_web_pilot.py:472`；不重复计为本轮新发现。除此之外无新增范围外观察。

## Checks 与证据边界

- 已按 scoped re-review prompt 阅读根 `AGENTS.md`、`tests/AGENTS.md`、Task 5 brief、原 review、report 的 Fix round 1，以及 `review-541bf2d..6bb5f51.diff`。`docs/AGENTS.md` 不存在；tests/e2e、docs/operations、docs/acceptance 和报告目录的逐级目录均无额外 AGENTS。只对本轮修复、相关断言和文档作定位核对，没有重新全仓审查。
- Fix base 为 `541bf2d289b1b79e01bc46766a96628d790dcf2b`，head 为 `6bb5f51d81df5e7f6d83f514bed5dac175efcca0`。报告 `.superpowers/sdd/2026-09-07-web-internal-pilot/task-5-report.md:110` 列出 ruff、py_compile、collect-only 和完整单项 E2E 命令；`:118` 记录测试源码 `63150da376b07414088f5c0f90bcb7ea5f22e075` 的 `1 passed in 34.15s`。该记录由实现者提供，本复审已核对其覆盖断言和版本表述，没有把它表述为审查者重新运行的结果。
- 没有发现必须追加实测、且现有运行不能回答的具体新风险，因此没有重复 E2E 或门禁。没有运行 Git 命令，没有读取历史 output 或私有配置，没有获取或输出任何凭证值、Cookie 值或 CSRF 值；除本审查报告外未修改文件，未提交或派生子代理。

## Verdict

- **Spec compliance：Approved（本轮限定范围）。** I1、I2、I3 全部 ADDRESSED；M1 文档撤回准确，collector 实现继续按既有裁定延期。
- **Task quality：Approved（本轮限定范围）。** 三个原 Important 验收缺口已关闭，修复 diff 未引入新破坏；不替代控制者后续全分支审查。
- **Fix round：All findings addressed, no new Critical/Important breakage。** 此结论中的 findings 指本轮要求修复的 I1、I2、I3。
- **计数：I1–I3 已关闭 3 / 未关闭 0；新增 Critical 0 / Important 0 / Minor 0；已有延期 Minor 1（M1）。**
