# 持久化 Web 内测验收（2026-09-07）

## 结论与版本

本机持久化 Web 内测在生产源
`ecfb4d2d959ade7ffa143b7b9ad1b8e29cde4242` 与测试修订
`63150da376b07414088f5c0f90bcb7ea5f22e075` 上通过增强后的真实浏览器验收：1 passed，34.15 秒。
测试使用 Python 3.12.14、Node 24.15.0、Docker Server 29.5.3、Playwright Chromium
151.0.7922.34，schema 为单一 head `0060`。

这是新增验收证据，不替代 2026-09-06 受控 Web 的历史 9318/Web 411 结果，也不把独立 Linux 报价
证据并入本机持久内测。文档提交发生在上述测试 commit 之后；没有把文档 commit 冒充一次新的完整
E2E 运行。

## 实际运行范围

测试只创建随机 owned profile、三个合成账号、一条合成 Territory Assignment 与一个随机对象，使用
本地已有 PostgreSQL/MinIO 镜像和 `apps/web/dist` 生产构建。未读取真实用户 profile、默认密码、
业务政策或外部凭证，未调用真实 Provider、邮件、桌面或公网服务。

通过项：

1. boss 与 sales 分别经页面登录；`/api/auth/session` 返回当前数据库员工角色，boss 对团队 API 为
   200，sales 为 403，证明由服务端当前授权裁决，而非 UI 标签。
2. 页面刷新保持真实 HttpOnly Cookie 会话；完整 `stop` 后存储和应用均停止，再次 `start` 后团队数据、
   canonical 数据库 marker 与对象 SHA-256 保持一致。
3. 首次退出 POST 被浏览器模拟网络中断后，页面明确显示未完成，旧会话仍为 200；直接“重试退出”
   后正常 Cookie jar 已清除，隔离请求 context 以原 Cookie 名和 `/api` 路径重放退出前 token 得到 401。
   普通跨标签退出和最终退出也执行同样的旧 token 重放检查。
4. A 保持已登录 boss 业务页，B 保持登录表单；B 不先退出 A 而直接登录 sales 后，A 的老板业务壳被
   无 payload 广播卸载。B 的当前服务端身份为 sales，团队 API 为 403。
5. A、B 均以同一个有效 sales 会话挂载业务壳，仅从 B 退出后，两页都清除旧状态。另一个竞争场景中，
   退出请求已到真实服务端并取得 204 响应，但测试暂缓把响应交付给 A；此时浏览器报告同名 Web Lock
   同时存在 held 和 pending，B 尚未发出登录请求。释放响应后，A 的实际 response 事件先于 B 的 login
   request，B 登录完成并刷新后仍是有效 sales 会话。
6. `disable` 和 `reset-password` 均撤销当前 sales 会话；停用时登录拒绝，重新启用后可登录；重置后旧
   密码拒绝、新随机密码成功；最终 logout 的正常 Cookie 清除和旧 token 401 均已验证。
7. 冷备份只在应用和存储停止后执行；恢复到新 profile 得到新 owner、新卷和新 API 端口，同时保留
   tenant、bucket、数据库 marker 与对象 SHA-256。源 profile 重新启动后会话、数据库和对象不受恢复
   影响。
8. 将备份前源 Cookie 值仅在测试进程内改挂到恢复目标的正确端口 Cookie 名后，目标仍返回 401；随后
   用保留的合成账号重新登录成功。这排除了“端口不同所以没带 Cookie”的弱证据。
9. 恢复目标在 390×844 viewport 显示团队业务数据，无文档级横向溢出、无 Vite 错误 overlay；全部
   page error 和 collector 保留的 console warning/error 均为零。

console collector 当前会宽泛忽略任意 failed-resource 401/403，并在模拟网络故障窗口忽略全部 console
warning/error；因此不能据此排除这些过滤范围内同时发生的其他告警。M1 的精确 URL、方法和阶段关联由
最终 review 延期处理，本记录不再声称“仅过滤已断言负路径”或“其余 warning/error 均失败”。

## 清理与证据安全

测试退出时逐个重读自己登记的 profile，先核验 exact owner、完整资源集合、容器 ID、镜像、挂载、卷名、
卷创建时间和 owner 标签，再停止并删除仅属于该测试的两个合成 owner 资源。生产 `stop` 本身没有删除
任何持久资源。密码、会话 token、Cookie 值、CSRF、数据库/对象凭证、原始请求和容器环境均未进入
pytest 输出、trace、HAR、截图或报告；断言只使用固定错误码和安全状态。

## 未覆盖与不能推导的结论

- 未执行物理机器断电或重启；浏览器证据是完整应用/存储 stop→start。跨 boot 进程身份另有窄单元证据。
- 未验收 Safari、Firefox 或缺少 Web Locks 的浏览器；当前只确认 Chromium 151.0.7922.34。
- 未验收共享主机、HTTPS/TLS、Secure Cookie、反向代理、多机 scheduler、备份加密/PITR/自动保留或
  异地灾备。
- 未运行真实 Tavily、Hunter、Gmail、模型、供应商、邮件发送或桌面端；站内通知以外的能力不能写成
  已完成。
- 合成 Territory Assignment 与对象只证明持久化和租户内授权，不证明真实 Demand Signal、Validated
  Need、Trade Opportunity、客户意愿、成交或单位成本改善。

复现入口、政策字段与恢复流程见[持久化 Web 内测操作说明](../operations/web-internal-pilot.md)，能力分栏见
[Web 核心能力矩阵](../operations/web-core-capability-matrix.md)。完整命令与测试修订记录见本任务报告
[Task 5 浏览器、生命周期与运维交付报告](../../.superpowers/sdd/2026-09-07-web-internal-pilot/task-5-report.md)。
