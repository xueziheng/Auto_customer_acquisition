# 持久化本机 Web 内测：最终全分支审查

**结论：Approved，具备本轮限定的本机 pilot 交付条件。** 未发现需要本轮修复的 Critical 或 Important。四项延期 Minor 已逐项独立裁定：两项作为已接受代价/历史告警结案，两项保留为非阻断测试改进，不要求为本次交付开启最终修复波。

- BASE：`37e76497f23b016296d4c563312663d96a8582d4`
- HEAD：`6bb5f51d81df5e7f6d83f514bed5dac175efcca0`
- 工作目录：`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion`
- 审查对象：提供的全分支 package（19 commits、60 files、466109 bytes），以及其绑定设计、计划、五任务原审/限定复审与 `progress.md` 中的裁定。
- 计数：新增 Critical 0 / Important 0 / Minor 0；既有延期项裁定后保留 Minor 2，均非阻断；本轮必须修复 0。

## Spec / plan 合规

正式依据为 `docs/superpowers/specs/2026-09-07-web-internal-pilot-design.md` 与 `docs/superpowers/plans/2026-09-07-web-internal-pilot.md`，并已读取根及涉及路径适用的 AGENTS。未将早期派发中的旧接口要求凌驾于后续正式裁定。

| 交付要求 | 全分支判断与代码依据 |
| --- | --- |
| 密码、持久会话、限流与撤销 | 符合。固定 scrypt 参数和格式、受限线程见 `infra/authentication/passwords.py:20`、`:62`；租户锁与账号锁统一排序、失败计数提交后抛异常见 `infra/authentication/service.py:139`；每次认证联查当前账号/员工/user 映射、版本、到期和撤销见 `:237`。三个新表与复合外键均带 tenant，迁移 `0060_web_authentication.py` 与 ORM 同步。 |
| 同源 API 与当前权限 | 符合。`apps/api/authentication.py:130` 起精确 Host、Origin、安全头、cookie/CSRF 校验；真实模式拒绝开发身份头，不退回 dev。`apps/api/identity.py:134` 通过当前 Employee 公共服务重新推导角色及经理下属范围。`apps/api/routers/authentication.py:64` 限制登录 body，私有响应不返回 session token；`:126` 在服务器撤销成功后才清 cookie。 |
| 可信账号维护 | 符合。`apps/api/pilot_accounts.py:49` 的新 Employee 与账号共用事务，调用公开 `create_account(session=...)` 后锁读当前经理，再调用员工域纯校验，拒绝完整回滚。实际 profile 接线在 `scripts/run_web_pilot.py:63`，使用当前配置/tenant、getpass 与自有 engine；没有 Web 账号管理或密码 argv 接口。旧模块自身的未接线 main 不作为交付入口，操作文档给出统一 CLI。 |
| 独立持久化与 owned 生命周期 | 符合。`infra/pilot/config.py:98`、`:120`、`:142` 实际 fd 权限/owner/链接校验、原子私有写入和操作锁；`infra/pilot/resources.py:186` 核对精确容器、镜像、owner、卷创建身份、挂载及独占附着；停止不删除数据。`scripts/pilot_web_supervisor.py:87` 先检查 build/端口/schema，再等待三个真实 ready；`:146` 精确回收且保留故障终态。 |
| 冷备份与新目标恢复 | 符合。`infra/pilot/backup.py:161` 在全部进程/存储静止后归档，SHA 与原子不覆盖发布；`:225` 恢复后回读实际卷逐成员比对字节/UID/GID/mode；`:290` 只创建新 owner/卷，核验 schema、撤销恢复会话并停止后才交付。`:254` 与 `:354` 的独立清理覆盖中断及持续诊断写入失败，pending/failed 不可普通启动。 |
| Web 与跨标签认证 | 符合当前 Chromium/合作页面范围。`apps/web/src/api/authentication.ts:25` 的 Web Locks 覆盖认证变更及响应处理，`:68`、`:84`、`:100` 保留身份代次检查与直接重试退出的当前 CSRF 获取；`apps/web/src/App.vue:91`、`:92` 在恢复/退出期间隐藏业务并卸载旧状态。`apps/web/src/api/client.ts:163` 仅 dev 发身份头，真实请求采用同源 cookie，401 与旧响应隔离。 |
| 生产 / controlled / pilot 隔离 | 符合。API 使用原 canonical 工厂和 engine，挂载显式运行 business lifespan（`apps/api/pilot.py:90`、`:114`）；三个独立入口安装 loopback 网络限制。scheduler 的显式 pilot parser/组合关闭研究、联系人和 Campaign（`apps/scheduler_worker/pilot.py:38`）；S3 pilot parser 保持非 dev 且只允许精确 loopback HTTP（`connectors/object_store/config.py:136`）；LOCAL_IN_APP 仅承诺持久站内投递，默认生产邮件约束保留（`apps/notification_worker/runtime.py:283`）。 |
| 验收与操作说明 | 符合限定交付。真实 PG/MinIO/built Web/Chromium E2E 覆盖直接换号、两个已挂载标签失效、真实退出响应顺序、隔离旧 token 重放、停启、冷恢复、源环境不变和 390px。操作说明提供显式政策、统一 CLI、TTY 账号、迁移和新目标恢复；准确披露故障域、容量及未运行项。 |

接口变化均有具体理由：CSRF 改为可只读重建的域分离派生；cookie 名按端口避免合作 profile 覆盖；复用现有外部事务接口而撤回多余 provisioning_scope；新增独立 supervisor；LOCAL_IN_APP、scheduler/S3 的显式 pilot 入口；稳定端口的 SO_REUSEADDR；Web Locks 的浏览器前提。它们与最终 spec/ADR/ledger 相符，没有据此扩张为共享部署或真实外部能力。计划中的旧编号和阶段占位不视作当前实现遗漏。

## Strengths

- 登录只是身份入口，授权仍取当前员工事实；没有把浏览器中的 role、tenant 或 owner 当作后端授权。这一点同时由实际数据库驱动 API 回归和 boss/sales 浏览器角色证明支持。
- 恢复的完成条件包含真实卷回读、会话撤销、停止和最终配置保存，既有中断缺陷已经沿可靠清理路径关闭；原 profile 的数据/会话独立性有直接断言。
- Task 5 修复后的退出验收具有辨别力：实际服务端 204 后暂缓响应、浏览器 held/pending Web Lock、response 事件先于 login request；退出后另建请求 context 携带旧 token，排除空 cookie 的假阳性。
- 证据保留了初始失败、修复范围、重叠测试和版本边界，没有将旧 9318/Web 411 或测试合成业务结果包装为当前完整门禁或真实贸易成果。

## Issues

### Critical / Important

无。原各任务 Important 在最终代码或补强测试中均已关闭；未发现新的跨层阻断问题。

### Minor：保留但不阻断本轮

**M1（承接 Task 3）— 恶意 tar 用例没有合法根基线，拒绝原因可能被掩盖。**

- 位置：`tests/unit/test_pilot_profile.py:151`，尤其创建 tar 的 `:156` 起；相应生产末端检查在 `infra/pilot/backup.py:102`。
- 当前每例只添加恶意成员，没有合法 `data` 根。即便某个成员拒绝条件将来回归，仍可能因为最终缺根而通过。这降低各个路径/链接/设备拒绝回归的辨别力。
- 后续改进：先创建合法根和普通文件并证明合法归档通过，再逐例添加一种非法成员，确认精确变异后被拒绝。
- **本轮不必修复。** 已读生产验证器在 `infra/pilot/backup.py:77` 起明确拒绝绝对/越界路径、错误根、非规范路径、重复名、链接/设备及危险权限；真实冷恢复正向测试和坏包整体拒绝测试另外存在。当前证据不足以证明这些七项测试分别命中预期拒绝条件，但没有据此发现生产验证绕过；不能把该 Minor 写成“已修复”或独立负路径均获强测试证明。

**M2（承接 Task 5）— Console collector 的宽过滤可能漏掉并发告警。**

- 位置：`tests/e2e/test_web_pilot.py:477`、`:480`；故障窗口在 `:544` 到 `:552`；最终门禁在 `:841`。
- 任意 failed-resource 401/403 均被忽略，网络故障窗口内全部 console warning/error 被忽略。因此保留事件为零不能排除这些范围内的其他应用告警。
- 后续改进：把预期拒绝关联到精确 URL、方法、阶段与已断言响应；网络中断只豁免本次已识别请求的固定类别。仍只保存安全分类/计数，不保存原始 console、请求或认证材料。
- **本轮不必修复。** 正式文档已撤回“只有已断言负路径被过滤”“无未解释运行错误”等强声明（`docs/acceptance/2026-09-07-web-internal-pilot.md:46`）；pageerror 始终保留，核心会话/权限/持久化的直接断言不依赖 collector。交付标准要求的实际行为证据已建立，不能仅因这项观测盲区推断存在已发生的生产错误。后续若把 console 清洁作为强门禁，必须先收紧收集器。

## 四项延期 Minor 的最终裁定

| 来源 | 独立裁定 | 本轮修复要求 |
| --- | --- | --- |
| Task 2 M1：经理 FOR UPDATE 较强 | **接受为低并发可信 CLI 的代价，结案。** `apps/api/pilot_accounts.py:103` 的确使用排他锁；正式 spec/ADR 要求锁读当前事实而未限制共享模式，当前锁序与事务提交不变。额外争用是事实，但账号创建属于低频本机管理，且租户认证本已串行；没有引出安全/原子性缺陷。不宣称共享部署吞吐达标。 | 否。以后若出现实际管理/认证等待，再在保留锁序与当前事实约束的前提下评估共享锁。 |
| Task 3 M1：tar fixture | **保留非阻断 Minor M1。** 生产防护存在，测试拒绝原因辨别力不足；见上文。 | 否。明确保留技术债。 |
| Task 4 M1：lint 87 warnings | **历史告警接受并保留记录，结案于本轮范围。** `task-4-report.md:110` 指明四个未改文件及 34/10/12/31 项；提供的全分支文件集合不包含这些页面。`:177` 的限定零输出 lint 只涉及修复文件，不能覆盖历史告警。本审查未重跑 lint 或逐条重新诊断它们，因此不将其宣称为已消除，也没有证据将它们升级成本次回归。 | 否。交付措辞必须保持“0 errors / 87 existing warnings”的历史运行归属，禁止“全库零告警”。 |
| Task 5 M1：console collector | **文档纠正已完成；实现保留非阻断 Minor M2。** 直接功能断言仍成立，完整运行无其他告警的强结论不成立；见上文。 | 否。以后强化运行时告警门禁时收紧。 |

## 测试证据与审查边界

以下均是实现者报告及已完成任务 gate 提供的实际运行记录，本审查静态核对测试断言、代码和版本关系；没有重跑相同版本的测试，没有新增无具体疑点的 probe。

| 范围 | 采信的已报告证据 |
| --- | --- |
| Task 1 | 初始 25 项含迁移；安全断言修复后认证两文件 26 passed / 24.51s，包含三项真实失败诊断的进程内泄漏检测；迁移往返与 ORM 比较。 |
| Task 2 | 主要受影响集 147 passed；后续 API 专项 42 passed；旧 runtime 26 passed；类型生成、mypy/ruff、结构自检、Web typecheck。 |
| Task 3 | 完整两文件 27 passed；后续状态/跨 boot 两组各 2 passed；恢复修复后正常冷恢复与故障组 3 passed，持续诊断失败加强后另 3 passed。它们不是一次累计全量。 |
| Task 4 | Web 418 passed 后相关 46 passed；runtime/notification 14、通知旧回归 27、canonical 34；207 passed + 一个过时 start 占位断言的历史失败，批准修正后 profile/pilot 37 项通过。最终修复 Web 14 passed、真实异常退出及正常生命周期 2 passed / 18.98s；typecheck/build/限定 lint/ruff/mypy/结构自检及 rebuilt Chromium 证据。 |
| Task 5 | 生产源 `ecfb4d2d959ade7ffa143b7b9ad1b8e29cde4242`，增强测试 `63150da376b07414088f5c0f90bcb7ea5f22e075`：真实 PG + MinIO + built Web + Chromium 完整单项 E2E 1 passed / 34.15s。最终 HEAD 只追加相关文档，不冒称 HEAD 又运行了一次。 |

已逐段阅读全分支变更；大段工具输出截断部分补读，重复的最终 plan/spec 以已完整读取的绑定文档核对。查阅了五任务原审与限定复审、证据摘录和所有 ledger ruling。没有读取私有 profile/config、Docker Env、cookie 值、原始失败产物或历史 output；没有生成凭证、调用真实 Provider、修改源码/index/HEAD、提交、合并、推送、清理共享 Git 或派生其他审查者。唯一写入是本报告。

## Assessment / Recommendations

**Ready to merge? Yes，限已授权的本机 pilot 范围；本报告不执行合并。** 功能实现、失败关闭边界与实际验收能够支持单租户 loopback 真实登录、当前权限、持久停启及新目标冷恢复。现存两个测试 Minor 不破坏该结论，也不应从记录中删除。

交付仍要求操作者显式提供业务政策并在真实 TTY 设置首个账号；未创建真实用户 profile。验收只到完整应用/存储 stop→start，没有物理断电/整机重启证据。只实测指定 Chromium；HTTP cookie、Web Locks、loopback 限制不能扩张为共享 TLS/反向代理、公网、恶意同源隔离或 OS 沙箱认证。真实模型/搜索/邮件/供应商、桌面、PITR/加密/异地备份均未验收；会话历史、对象和备份容量以及冷备份停机成本仍由操作者承担。当前合成结果不能说明真实需求验证、成交或单位合格贸易机会成本改善。
