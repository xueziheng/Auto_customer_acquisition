### Spec Compliance

- ❌ Issues found：恢复中断或诊断配置写入失败时，不能保证落入已停止、固定失败状态；见 Important I1。其余能从本任务 diff 核验的 profile/存储/冷备份/新目标恢复主体满足要求。
- ⚠️ 跨任务待验：`scripts/run_web_pilot.py:34` 明确以 `application_launch_not_configured` 拒绝完整 start，符合 Task 3 留接口的范围；Task 4 必须验证三应用 supervisor、端口继承及占用拒绝、启动后最新 DB/object 配置、LOCAL_IN_APP、真实账号装配与 loopback 限制，Task 5 验证浏览器会话与重启链路。本审查不宣称集成完成。
- 文件核对：brief 所列 8 个新文件均有对应完整 diff，无缺失文件，无本任务范围外功能扩张。

### Strengths

- `infra/pilot/config.py:98`、`:120`、`:142`：私有文件实际 fd 校验 owner/0600/普通文件/单硬链接，原子写入与不删除锁 inode 的操作锁机制清楚；SecretStr/repr 隐藏及固定错误码符合凭证边界。
- `infra/pilot/resources.py:185`：资源核验同时覆盖精确容器 ID、镜像、owner/role、卷名称/CreatedAt、挂载目的地/RW 和独占附着集合，停止路径不删除持久卷。
- `infra/pilot/backup.py:161`、`:225`：备份前后检查全部应用及存储静止，采用停止卷流式 tar、SHA256 manifest、原子不覆盖发布；恢复后回读实际卷，比较每个成员字节摘要及 UID/GID/mode，而非把上传成功当恢复成功。
- `infra/pilot/backup.py:253`、`:307`：创建目标前验证整包及本地精确镜像；新 owner/卷保留 tenant 与 bucket/key，检查 schema 并调用租户绑定公开接口撤销会话。
- `tests/integration/test_pilot_persistence.py:119`：真实持久卷覆盖停启后原容器身份、Employee/tenant、对象 SHA 和新目标恢复会话拒绝，源 profile 全文件 hash 保持。测试报告明确区分完整 27 项运行与后续目标回归，没有拼成虚构全量结果。

### Issues

#### Critical (Must Fix)

- 无。

#### Important (Should Fix)

- **I1 — 恢复清理没有覆盖中断，也会被失败状态保存异常阻断。** `infra/pilot/backup.py:307` 启动恢复存储，`:316` 只捕获 `Exception`；Ctrl-C 的 `KeyboardInterrupt` 直接跳过该块。另在 `:321` 先 `profile.save()`，再进入 `:322` 的停止清理 try；若原失败是磁盘满/配置写入失败，第二次保存再次失败也会阻断停止。因此操作者中断恢复，或恢复后保存遇到磁盘错误时，可留下运行中的存储和 `pending` 配置，旧会话撤销可能尚未完成；CLI 也无法承诺固定失败码与安全失败状态。修复应把已创建 profile 的精确停止与 client.close 放入可靠的 finally/独立清理路径，诊断写入失败不能阻止停止；明确处理用户中断，并添加启动存储后的中断及诊断保存失败回归。不可清理其他 owner，也不能把未完成恢复标 complete。
  - 聚焦证据：只用临时目录和内存替身，在 `check_schema_locked()` 注入 `KeyboardInterrupt`，输出 `interrupt_propagated=true`、`storage_start_calls=1`、`storage_stop_calls=0`、`profile_client_close_calls=0`、`failed_state_save_calls=0`。没有创建 Docker 资源或处理凭证。

#### Minor (Nice to Have)

- **M1 — 非法 tar 成员测试存在共同的无关失败原因。** `tests/unit/test_pilot_profile.py:139` 的 `test_archive_rejects_unsafe_members`只写被测恶意成员，没有正常 `data` 根目录。`infra/pilot/backup.py:103` 最终要求根目录存在，所以即使未来删除对应路径/链接/设备拒绝条件，这七个用例仍可能因缺根而通过。先加入合法根及普通文件，再附加一个恶意成员，并加合法归档基线，使各拒绝原因可独立检验。当前实现本身包含这些拒绝条件；这是回归测试辨别力问题。

### Checks and scope

- 已完整阅读一次 `review-b31cffd..4bc696a.diff`，按 4 段顺序读取；没有重读改变的源文件内容，没有执行 git 查询或全库搜索；末尾只通过已导入函数的 code object 元数据核准函数起始行号。
- 已读任务 brief/report、根/infra/tests/新增 pilot AGENTS 和绑定设计；scripts 及测试子目录没有额外 AGENTS。未读私有配置、Docker Env、凭证、工具正文或业务原件。
- 唯一 diff 外聚焦检查：命名风险“进程发布快照与既有 OwnedProcess 精确停止协议是否相容”；只读取 `infra/controlled/resources.py:24-228` 的 OwnedProcess 类。其 `public()` 刷新子进程快照，`stop()` 通过锚点、出生时间与进程组完成清理；本任务通过保存 public 身份保留接口。完整 supervisor 使用协议仍归 Task 4。
- 未重跑实现报告已运行的测试套件。唯一新增执行是上述恢复中断隔离探针；第一次使用系统临时目录别名触发既有 symlink 拒绝，随后解析真实临时路径后验证目标疑点。未启动 Docker、未污染用户 profile。
- 采信为“实现者报告的运行证据”：27 passed 无警告、随后两组目标回归各 2 passed、ruff/mypy/结构自检通过；本审查静态核验测试与代码，不把报告等同于重新运行。

### Assessment

**Task quality:** Needs fixes

**Finding counts:** Critical 0 / Important 1 / Minor 1。

**Reasoning:** 正常持久化与冷恢复路径边界清晰，并有真实卷和会话撤销证据；恢复中断/诊断失败可绕过停止，是本任务安全失败状态的实质缺口。修复 I1 并做聚焦回归后可重新判定 Task 3；完整应用与浏览器验收继续由 Task 4/5 承担。
