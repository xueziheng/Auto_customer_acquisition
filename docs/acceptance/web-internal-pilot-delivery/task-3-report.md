# Task 3：持久化 profile 与冷备份恢复交接

提交：`4bc696a2167b0e7fcb5dbd8d91068ac55df1ff55`（精确 8 个新文件；本交接报告为本地 SDD 工件）。

状态：DONE_WITH_CONCERNS。Task 3 存储/CLI 基础接口完成；完整 `start` 三应用接线明确属于 Task 4，当前固定拒绝 `application_launch_not_configured`，不能把存储成功当作完整 Web 成功。

## 实现与文件

本任务只在 `codex/web-internal-pilot`、基线 `b31cffdb02be82df9895bab6929dbc7f7445b38b` 的指定 worktree 新增：

- `infra/pilot/AGENTS.md`、`infra/pilot/__init__.py`
- `infra/pilot/config.py`：私有配置、显式政策、原子写入、操作级锁、技术配置与 SecretResolver。
- `infra/pilot/resources.py`：固定本机 Docker、独立持久卷、精确容器/卷/挂载身份、存储停启、显式迁移、只读 schema head 检查、进程身份状态、精确 supervisor 停止请求。
- `infra/pilot/backup.py`：冷物理卷归档、整包校验、新目录原子发布、新 owner 恢复、逐文件 UID/GID/mode/SHA 回读、原 tenant 会话撤销。
- `scripts/run_web_pilot.py`：统一 CLI，直接脚本执行也能定位本 checkout。
- `tests/unit/test_pilot_profile.py`、`tests/integration/test_pilot_persistence.py`。

未修改 controlled 资源语义、未导入 apps、未种 Employee/业务数据到 infra；测试独立创建合成记录。未更改迁移、认证服务或 API。

## Task 4 精确接口

`--profile PATH` 是持久目录，私有配置固定 `PATH/config.json`，不是第二种环境文件格式。

```python
PilotConfig.create(path: Path, policy_file: Path) -> PilotConfig  # 仅新目录/配置，不创建 Docker
PilotConfig.read(path: Path) -> PilotConfig                    # 参数是 config.json 文件
config.write(path: Path) -> None                              # 调用者持 profile 锁
config.database_url: SecretStr                                # 按当前 database_port 推导
config.resolve(reference: str) -> str                         # SecretResolver 结构接口
config.runtime_environment() -> dict[str, str]
```

配置字段：`version=1`、`restore_state`（none/pending/failed/complete）、`owner`、`tenant_id`、`api_port`、`scheduler_port`、`notification_port`、`database_port`、`object_port`、`bucket`、`secrets`、`policy`、`storage`。`web_port` 属性等于 `api_port`，同源单一稳定入口。初建 DB/object 端口是 0，存储启动后根据实际 Docker loopback 绑定原子重写；Task 4 必须使用启动后的 `profile.config` 或重新 read。

`storage` 键严格为 database/objects，每项 `StorageIdentity` 含 `image_id`、`volume_name`、`volume_created_at`、`container_id`。Docker 具名卷没有独立的不可变 ID；这里核对完整名称、CreatedAt、owner/role 标签、精确容器 ID、镜像 ID、唯一挂载及所有附着该卷的容器集合。未知/旧 ID、错卷、额外容器挂载均拒绝。

政策 JSON 是完整的：

```text
{
  "handoff_policy": {
    "sla_seconds": <显式正整数>, "backlog_threshold": <显式正整数>,
    "t1_seconds": <显式正整数>, "t2_seconds": <显式正整数>
  },
  "scoring_policy": {
    "version": <显式非空版本>, "currency": <三字母币种>,
    "value_band_boundaries": [<Decimal 字符串>, ...],
    "bucket_map": {"1": <high/mid/low>, ..., "7": <high/mid/low>}
  }
}
```

无业务默认；金额走 WireDecimal/Money，语义校验复用 `domains.opportunities.models.HandoffPolicy` 与 `domains.opportunities.scoring.ScoringPolicy`。不允许环境变量或 Provider 配置混入政策文件。真实操作者没有提供政策，本轮没有创建真实 profile。

`runtime_environment` 给出 DATABASE_URL、tenant、显式政策、对象存储当前 endpoint/bucket/ref、技术 lease/batch/poll/retry、固定端口、fingerprint/unsubscribe 引用、`TRADEOS_DEV_MODE=false`、`TRADEOS_NOTIFICATION_DELIVERY_MODE=LOCAL_IN_APP`、dotenv/metadata 禁用值。**它不含真实 Provider 引用或任何假的 Gmail/model/search 凭证，也不复制父环境。** Task 4 需按 LOCAL_IN_APP 契约解析通知模式、装配拒绝型 Provider 并安装 loopback 网络限制；不能把 canonical dev runtime 强行解释为已支持本模式。技术密钥通过 config.resolve 交给可信进程，不要求把 secrets dict 枚举到环境。

```python
profile = PilotProfile(path: Path)              # 固定 unix:///var/run/docker.sock
profile.reload() -> PilotConfig
profile.provision_storage(image_ids: Mapping[str, str] | None = None) -> None
profile.start_storage() -> None                # 仅存储，绝不自动迁移/装配应用
profile.migrate() -> None                      # 显式启动存储 + OwnedProcess alembic upgrade head
profile.check_schema() -> None                 # 只读、合法单 head，不自动升级
profile.stop() -> None                         # 精确 supervisor TERM/等待，然后停存储
profile.status() -> dict[str, object]           # storage/applications/web_url，无凭证
profile.client.close()                         # 释放客户端，不影响容器/卷
```

所有上述变更入口使用 `exclusive_profile_lock(path)` 操作级非阻塞锁，忙时固定 `profile_busy`。锁不会跨整个应用寿命持有，也不会被删除。Task 4 在已经持锁时使用：

```python
profile.provision_storage_locked(image_ids=None)
profile.start_storage_locked()                 # restoring=True 仅恢复内部允许
profile.check_schema_locked()
profile.stop_storage_locked()
profile.save()
profile.verify_all()
profile.require_stopped()
profile.require_no_processes()
```

这些方法是同步接口；内部 schema/revoke 用 `asyncio.run`，不要直接从已运行事件循环调用。`migrate` 一次最多等待 120 秒，只迁移，成功后保持存储运行；CLI init 之后显式 stop，交付停止状态。

启动协议建议顺序（Task 4 实现）：持锁 → reload → require_no_processes → `reserve_port(config.api_port)`（socket 监听 127.0.0.1，供 API 继承 fd）及两 worker 端口预检 → start_storage_locked → check_schema_locked → 用更新 config 启动三个 `OwnedProcess` → 发布 supervisor/子进程实际状态 → 释放操作锁。API 端口已占用时固定失败，不能重新分配。初次创建不做默认账户；可信账号入口重新读取同一 profile 后使用该 URL/tenant，等待真实操作者 getpass。

进程协议：

```python
ProcessIdentity.current() -> ProcessIdentity     # pid + born
ProcessIdentity.live() -> bool                  # 同 boot PID 出生时间不符拒绝；前次 boot 身份视为已终止
profile.publish_processes_locked(
    supervisor=ProcessIdentity.current() | None,
    processes=Sequence[OwnedProcess],
    status="stopped" | "starting" | "running" | "failed",
    reason="requested_stop" | "storage_ready" | "applications_ready" | "operation_failed" | "restore_failed",
)
profile.runtime_state() -> RuntimeState
profile.request_application_stop(timeout: int = 40) -> None
```

只接受当前进程作为被发布 supervisor；子进程从 `OwnedProcess.public()` 记录真实 pid/born/anchor/children，名称限 api/scheduler/notification/migration 且不能重复。`runtime.json` 同样 0600/当前 owner。记录的所有进程已死时 status 不再回报旧 running；部分仍活而部分已死时回报 failed。Task 4 的 readiness 仍需真实三个进程健康检查，状态文件不是 HTTP 健康替代物。

停止 CLI 在锁内核验存储和 supervisor pid/born，发送 TERM 后释放锁并有界等待其退出，再重新加锁检查所有记录的 app/anchor/children 已停止，最后停两个容器。**Task 4 supervisor 必须捕获 TERM，先依次调用实际 `OwnedProcess.stop()` 完成出生时间/锚点协议；在同一操作锁中发布 `supervisor=None, processes=(), status=stopped`，然后 stop_storage_locked。** 不得先清记录再停止进程；不得让 supervisor 等待这个 CLI 持有寿命锁。找不到活 supervisor 但仍有活子进程时失败关闭，不尝试接管或广域 kill。

## CLI 与备份恢复

直接 `.venv/bin/python scripts/run_web_pilot.py`：

```text
init    --profile PATH --policy-file PATH
migrate --profile PATH
start   --profile PATH                         # 当前固定 application_launch_not_configured，Task 4 接线点 start_profile(Path)
stop    --profile PATH
status  --profile PATH
backup  --profile PATH --destination NEW_PATH
restore --backup PATH --profile NEW_PATH
```

CLI 成功返回 0/安全 JSON；操作错误返回 2/固定 reason。没有 reset、destroy、清库、命令行密码参数或凭证输出。

`backup_profile(path, destination) -> None` 只接受应用与两个存储都停止，文件 config.json/database.tar/objects.tar + manifest.json；目录 0700、文件 0600。流式归档和 SHA，私有同父暂存目录后 Darwin renamex_np(RENAME_EXCL)/Linux renameat2(RENAME_NOREPLACE) 原子发布，已有空目录也不覆盖。失败暂存目录可以保留用于人工核对，不称备份成功。

`restore_profile(backup, path) -> PilotProfile` 在创建目标之前校验完整白名单文件、SHA、版本、路径/符号链接/硬链接/设备/重复成员、镜像关系与现有本机镜像。只接受 manifest 中精确 image ID 同时匹配本地支持镜像标签；标签发生更新也会固定拒绝，不会下载或替换镜像。保留 source tenant、账户/员工/业务 ID 和 bucket/key；仅重建 owner、卷/容器身份和三个应用端口。恢复先标 pending，失败标 failed，普通 start_storage/migrate 不能启动不完整恢复。

通过 Docker 官方 archive API 的 `copyUIDGID=true`（SDK `_put` 私有适配集中在 `_restore_volume`）恢复，随后从停止的**实际目标具名卷**重新 get_archive，对每个文件的路径/字节 SHA/UID/GID/mode 比较；不是只验证上传成功。启动恢复存储后检查当前 schema 并用 `PostgresAuthentication(factory, TenantId(config.tenant_id)).revoke_all()` 撤销会话，最后停止存储并标 complete。

官方依据：[Docker Engine archive API](https://docs.docker.com/reference/api/engine/version/v1.46/) 的 get/put archive 与 copyUIDGID 参数。没有运行中的 PG datadir tar，也没有对停止容器调用 pg_dump。

## RED/GREEN 与实际证据

解释器均为指定 `.venv/bin/python`（项目 Python 3.12），固定本机 socket，仅已有 PG/MinIO 镜像，未 pull。

1. 首次 `-m pytest tests/unit/test_pilot_profile.py::test_profile_contract_exists -q --tb=short`：1 failed，明确 PILOT_PROFILE_MISSING。
2. 完整初始单测：13 failed/2 passed，缺失 resources/backup 及首轮政策适配问题；完成 config 后 8 passed/7 failed（缺 backup）。初次持久化集成收集缺 resources（3 errors），随后真实集成进入行为验证。
3. 首次两文件运行：17 passed/1 failed，恢复本地镜像探测误把 DockerClient 当 context manager；改为 closing 后真实主链 `test_real_stop_restart_and_cold_restore` 1 passed。
4. 扩展边界集首次 25 passed/1 failed：初始 PostgreSQL 临时 Unix socket 就绪窗口导致一次 migration_failed；改为 `pg_isready -h 127.0.0.1` 等待最终 TCP 服务，目标集成 1 passed。
5. `test_private_read_missing_file_uses_fixed_error` RED：原始 FileNotFoundError；改为安全固定 configuration_invalid，随后包含在完整 GREEN。
6. `test_direct_cli_works_outside_checkout_without_pythonpath` RED：DIRECT_CLI_IMPORT_FAILED；补直接脚本定位 checkout，随后包含在完整 GREEN。
7. 最近完整两文件：`-m pytest tests/unit/test_pilot_profile.py tests/integration/test_pilot_persistence.py -q --tb=short`：**27 passed in 24.74s**，无警告。包含真实停启恢复、坏包/旧ID/错卷、活进程阻断、schema 只读与失败恢复拒绝。
8. 自审发现状态文件可陈旧：给原有活进程集成增加停止后 status 断言，RED running!=stopped；修复后该集成 + 直接 CLI 回归 **2 passed in 4.16s**。没有重跑不受影响 27 项或 9318 全库基线。
9. 增加前次 boot 的 PID 复用安全边界：RED process_identity_invalid；修复后与同 boot 错 birth 拒绝一起 **2 passed in 0.45s**。整轮目前测试文件共 28 项；最后两处改动采用对应目标回归，不把不同次运行累计成一轮完整通过。
10. `-m ruff check infra/pilot scripts/run_web_pilot.py tests/unit/test_pilot_profile.py tests/integration/test_pilot_persistence.py` 通过；相同范围 ruff format 已规范。
11. `-m mypy --follow-imports=silent infra/pilot scripts/run_web_pilot.py`：Success，5 source files。
12. `.venv/bin/python scripts/check_boundaries.py`：通过；最后代码变动后再次运行。

实测数据结论只报告安全比对：原 Employee 标记与 tenant 关联保留；完整 stop/start 后相同精确容器/卷仍在；当前端口持久配置匹配；对象原件 SHA 相同；新 owner 恢复桶名和 key 相同；目标数据/uid/gid/mode 回读相同；恢复旧会话拒绝；源配置全文件 SHA 未变化；坏包不创建目标；失败恢复停止且不能启动。没有把合成记录报告为真实商机或真实用户。

## 资源清理与限制

测试清理只操作 fixture 记录的新 owner，删除之前核验精确容器和卷 owner；生产代码没有销毁 API。最终只读 inventory：`pilot_containers_remaining=0`、`pilot_volumes_remaining=0`。私有合成 config/备份位于 pytest 独占临时目录，受 0700/0600 保护；没有读取或清理真实 profile。所有凭证随机生成并仅在测试/运行进程中比较安全布尔，不打印配置、Docker Env、DSN、原始身体或认证值。

保留原有输出目录和未跟踪文件，未做广域清理、共享 .git 修复、push/merge 或 Provider/桌面调用。Git AppleDouble stderr 仅计数，未回显 pack 警告正文。

剩余 Task 4：三个应用进程的独立入口、会话模式装配、同源生产构建、LOCAL_IN_APP、拒绝型 Provider/loopback 限制、健康检查、可信账号 CLI profile 绑定与 `start_profile` 实现。后续需实际验证应用 supervisor 的启动、重复启动、健康失败和 Ctrl-C/TERM；本 Task 3 已验证 OwnedProcess 精确清理阻止备份/停存储，但没有把未接线的整套 supervisor 宣称通过。整机重启通过出生时间相对 boot_time 的单测证明拒绝/失效规则，没有实际重启用户电脑。未实现备份加密/保留策略；恢复本机精确镜像标签更新后会拒绝，需运维保留匹配镜像；Docker SDK 升级应复核集中 `_put` 适配。

## 审查修复第 1 轮：仅 I1（FIX_BASE 4bc696a）

独立审查 I1 已核实并修复；M1 按控制者要求留待最终审查，本轮未改 tar 成员测试。

- 恢复主体现在捕获 BaseException，将用户 KeyboardInterrupt 映射为固定 `restore_interrupted`，其余失败保留 `restore_failed`，不回显底层异常。
- 已创建 profile 的失败收口在 finally 调用独立 `_cleanup_failed_restore(profile)`：**先尝试精确停止，再分别尝试失败配置、运行状态与固定诊断写入**；各步失败互不阻断，client.close 位于独立 finally。磁盘持续不可写时不声称状态文件已写成功；已有 pending 配置仍阻止普通启动。
- 未完成恢复不进入 completed 成功分支。停止仍复用原 owner/ID/mount 校验，不删除卷、不搜索或清理其他 owner。
- CLI 对尚未创建资源时的 KeyboardInterrupt 返回 `2` 和固定 `pilot_interrupted`；进入恢复资源主体后返回 `2` 和 `restore_interrupted`。成功返回 PilotProfile 的接口与 Task 4 锁/启动约定不变。
- 当诊断确实可写但存在清理/写入错误时，`restore-failure.json` 现在为 `{"errors": [<固定错误码>, ...]}`。可包含 restore_cleanup_unknown、restore_state_write_failed、restore_status_write_failed、restore_client_close_failed；不包含原始异常或凭证。

### 回归与验证

解释器均为指定 `.venv/bin/python`；仅本轮新 owner 的本机 PG/MinIO，没有 pull/Provider 调用。

1. 新真实集成 `-m pytest tests/integration/test_pilot_persistence.py::test_restore_failure_after_storage_start_always_stops_and_closes -q --tb=short`：修复前 **2 failed in 15.21s**，interrupt 与 diagnostic_io 都明确失败于 `RESTORE_FAILURE_LEFT_STORAGE_RUNNING`。测试实际见到两个目标存储运行后才注入故障，随后通过新客户端核对 Docker 实际状态。
2. 修复后上述两项 + 既有 `test_real_stop_restart_and_cold_restore`：**3 passed in 20.86s**。正常冷恢复的实际卷/对象/tenant/会话撤销链继续通过。
3. 将 diagnostic_io 加强为同时拒绝 profile.save、runtime 私有写入及最终诊断文件写入，并增加创建资源前 CLI 中断测试。移除 CLI 中断处理的 RED 检查：**1 failed in 0.55s**，`CLI_INTERRUPTION_ESCAPED`；恢复处理后，两个真实失败场景 + CLI 中断测试：**3 passed in 12.53s**。
4. 失败回归确认：原恢复 client.close 被实际调用，目标两个存储实际 stopped，CLI 固定失败，目标配置 pending/failed 且不能普通启动，源 profile 全文件 hash 和停止状态保持不变。测试没有通过假停止替身判定容器状态。
5. `-m ruff check infra/pilot/backup.py scripts/run_web_pilot.py tests/integration/test_pilot_persistence.py tests/unit/test_pilot_profile.py`：通过；相同范围 `ruff format --check`：4 files already formatted。
6. `-m mypy --follow-imports=silent infra/pilot/backup.py scripts/run_web_pilot.py`：Success，2 source files。
7. `.venv/bin/python scripts/check_boundaries.py`：通过。

本轮只修改 backup.py、CLI 和两份测试文件，未重跑无关全量基线。新资源仍由测试 fixture 按精确 owner 核验后清理；没有删除用户 profile 或共享输出。最后只读检查资源剩余计数写入下方提交记录。

本轮结束资源计数：`{'pilot_containers_remaining': 0, 'pilot_volumes_remaining': 0}`。

I1 修复提交：`c669eec841fa3fb9a5ccf6fc2bf7183130bca592`。
