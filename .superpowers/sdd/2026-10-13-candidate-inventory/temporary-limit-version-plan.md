# 临时模型额度：版本切换与可恢复维护

用户已明确授权 20→26、最多两批六次采集后恢复 20，不需重复征求相同额度授权。父已收紧实际执行计划为一次新政策探测加一批三来源采集；不提高到 27，不清理调用账本。本文件记录代码核实和一次性维护工具，尚未操作生产。

## 已核实版本关系

- `infra/db/model_configuration.py:57` initialize 对同 tenant/version 的 model、limits、export_enabled 做不可变校验；已有 head 不会因 initialize 自动切换。只修改部署文件 limits 会触发 startup 拒绝，问题不在 typed JSON 是否能解析。
- `domains/assistant/service_impl.py:261` 的 save_nonsecret 先校验当前管理员，再由 Repository.save 以 expected_version 锁定旧 head、插入新 mcv 并移动 head。`POST /api/settings/model` 是已有合法创建路径，不读取模型密钥。
- 新 head 与 api/scheduler 运行时版本、心跳、外发许可及验证状态共同授权模型动作。新政策没有旧政策的真实 probe，必须 unverified，等当前管理员通过 `POST /api/settings/model/probe` 显式测试。不得把旧 api_verified_at 随新政策继承，`deployment_verification()` 会把该时间绑定到当前政策版，继承相当于伪造新版本已验证。
- `infra/tenant_model.py` 和 `infra/model_catalog.py` 区分政策 configuration_version 与连接 api_connection_version。保持目录连接、公司 binding、模型、协议和密钥引用不变时，无需重建连接或重复平台 API/Agent 连接测试。目录中的真实已有测试不变。新 task/run 会绑定新政策和同一连接；旧 run pin 不允许改政策后继续，因此须使用全新任务并在恢复前确认相关工作结束。
- 非目录 fallback 的 resolver 使用 api_connection_version 或 policy version 校验 key 文件；此次工具只接受现有目录模式部署，以防政策改名导致非目录密钥版本失配。
- `workflows/assistant/steps.py:144` model_probe 真实调用原 Gateway。`infra/db/model_usage.py:105` 按 tenant/employee 和窗口累计全部非 rejected 调用，不按政策版本重置；probe 不豁免。若原窗口已有 20 次，26 只容纳一次 probe 加最多五次采集。运行一次 probe + 一批三来源，预计累计 24，不能保证还完整容纳第二批。
- 全库现有 head 写路径只有 Repository.save（创建新版）和 `scripts/apply_model_api.py`（平台连接发布，保留 limits 且绑定连接版）。后者不是额度工具，不应强行拿来启用额度版。没有公开 activate-existing 端口；恢复旧 20 不能新建第三个政策，否则需要再次 probe 且额度可能已耗尽。

## 一次性维护工具及边界

工具：同目录 `temporary-limit-version.py`。不修改业务核心或已部署应用代码。测试：`tests/integration/test_temporary_limit_maintenance.py`。

工具 prepare 在线运行，仅锁读数据库并写私有备份，不修改 DB。校验当前旧 head/immutable row/部署文件 20/20 一致、原政策验证通过、两进程同版且心跳新鲜。私有 journal 绑定 tenant、当前 UI 管理员 user_id、settings 绝对路径、旧版本、旧文件 SHA256 与准备时间；原文件逐字节备份，权限 0700 目录/0600 文件，拒绝 symlink、异主、硬链接与并发。

apply 必须在父明确停止完整应用并只启动 owned storage 后执行。它只接受 UI 已创建、创建者为绑定管理员、创建时间不早于 prepare 的新 mcv，且 model/export/全部其他 limits 与原文件一致，唯一额度差异为 tenant_calls/employee_calls 20→26。锁定 head 必须已等于这个新 mcv。仅同步新政策号、26/26 并清空 api_verified_at；保留全部原连接引用、目录、协议、密钥路径与其他配置。原 JSON 逐字段保留后整体 typed 验证，不用会排除秘密引用字段的 model_dump 重建部署文件。

restore 仅接受本次 old/new head 与 old/new 文件字节组合，严格 CAS 切回已存在的旧 head、原文件逐字节恢复。旧/new immutable 版本行、probe、目录 bindings/run pins 和 model_invocations 均不修改或删除。新的政策行保留审计。原 head + 原文件恢复后重启，复用原真实旧政策验证，不创建新 probe。

apply/restore 同时要求显式 `--service-stopped-sole-writer`、无新鲜 api/scheduler runtime 心跳、无未释放 ModelInvocation 槽、无 queued/processing acquisition job、无 queued/running AgentTurn（包括 probe）。这些 DB 条件不是 OS 停机证明，父仍必须真正停服务并禁止其他管理员/运维写入。工具不停止服务、不启动数据库、不取消任何用户任务、不解除未知调用槽。若 shutdown 留下旧 runtime 行，只有心跳超过原 TTL 后才允许继续；不删行伪造停机。

## 两资源故障恢复

数据库 head 与文件不能原子提交。工具以持久 journal 先记录 applying/restoring 意图，再修改资源；每次运行均持 settings-parent 与 operation-dir 的非阻塞锁。apply 不改 head（UI 已切换），文件原子写后完成 journal；restore 先提交 head→old，再原子恢复文件，最后 journal 完成。

任何阶段中断保持应用停止。重跑同 action 和相同新版本，或在 apply 未完成时运行 restore；工具核验实际 old/new 组合再继续。head 已旧/文件仍新、head 新/文件已旧均只用于恢复本次已绑定操作。遇第三个 head、任意文件字节变化、actor/tenant/path/backup 变化、尚未结束工作或不可验证的状态，固定拒绝，禁止猜测覆盖。不要删除 journal 或备份重新 prepare；不要在中间状态启动完整服务。恢复失败需保留 storage 与私有状态供父排查，不自动退款、取消或清理账本。

## 父执行顺序（独立审查后）

变量均由父使用已核验的受信路径/当前 UI 身份设置，不输出配置或凭证。复制到 `/tmp` 运行时必须给 PYTHONPATH 指向实际应用根目录；脚本不会猜根目录。

```bash
PYTHONPATH="$TASK_APP_ROOT" "$TASK_PYTHON" "$TASK_TOOL" prepare \
  --profile "$TASK_PROFILE" --settings "$TASK_MODEL_SETTINGS" \
  --state-dir "$TASK_PRIVATE_OPERATION" --actor-user-id "$TASK_ADMIN_USER_ID"
```

1. 当前完整服务健康、旧设置 verified 时执行 prepare。UI 只读确认旧政策及 20/20/86400；确认没有进行中的 Agent/采集任务。
2. 当前管理员在既有 `/api/settings/model` 保存：expected_version 为原政策，model/export/其他 limits 完全保留，仅 20→26。取得返回新 mcv。不要触发模型任务；此时 pending_restart 正常。
3. 停止完整服务；只启动现有 owned storage（数据库）供维护连接。不要通过完整服务 start 来启动 storage，避免在新 head/旧 settings 的中间态运行 scheduler。
4. 执行 apply：

```bash
PYTHONPATH="$TASK_APP_ROOT" "$TASK_PYTHON" "$TASK_TOOL" apply \
  --profile "$TASK_PROFILE" --settings "$TASK_MODEL_SETTINGS" \
  --state-dir "$TASK_PRIVATE_OPERATION" --actor-user-id "$TASK_ADMIN_USER_ID" \
  --new-version "$TASK_TEMP_VERSION" --service-stopped-sole-writer
```

5. 只有 maintenance_completed 后才正常启动完整服务。核验 health、head/api/scheduler 都为新 mcv、26/26/86400、状态 unverified/can_probe=true。真实 UI 使用本次稳定 idempotency_key 请求一次 probe；等待该 turn terminal 并 status=verified，不因轮询超时换 key 再探。
6. 在预算内执行一批新 UI 三来源任务。若使用 NG，仅代表 query 规划和交付地区条件，不声称 Tavily 使用了国家搜索过滤；目前 transport 丢弃 country 参数，本工具不改该行为。不得复用旧 job/run。
7. 等新 probe、job 与所有正在运行工作结束，检查安全摘要及 quota；停止完整服务，只启动 owned storage，执行 restore：

```bash
PYTHONPATH="$TASK_APP_ROOT" "$TASK_PYTHON" "$TASK_TOOL" restore \
  --profile "$TASK_PROFILE" --settings "$TASK_MODEL_SETTINGS" \
  --state-dir "$TASK_PRIVATE_OPERATION" --actor-user-id "$TASK_ADMIN_USER_ID" \
  --new-version "$TASK_TEMP_VERSION" --service-stopped-sole-writer
```

8. 恢复完成后正常启动，核验 head/runtime 回到原版、limits20/20、原验证状态和 health。若当窗已用 24，恢复20意味着剩余额度为0，直到原窗口自然结束；这是正确行为，不调账。

如果 UI save 前撤销，可停机后 restore 不传 new-version，将 prepared 标记为 restored。UI save 已发生但 apply 尚未完成，也传该次 new-version 执行 restore；工具会绑定并核验这一次新版本。若实际保存了其他值/第三个版本，工具拒绝，父不能删状态强行重试。

## 验证证据

先 RED：5 项因工具不存在失败。基础 GREEN：5 passed，覆盖正常及 apply 文件写后、restore head提交后、restore文件写后的故障重入。扩展安全组 8 passed，含真实隔离 PG 的旧20调用跨新版本累计到26（probe算第21次）、恢复20仍limited；错误 head/未停机/live runtime、文件篡改、并发锁、active acquisition、queued probe 拒绝。最终组合测试结果见本文件后续记录；所有 Provider 都未真实调用。

关键局限：工具不提供 OS 服务停止证明，不处理其他运行期未列入的工作流类型；父必须保持独占维护窗口，准备与恢复前核验当前业务无其他后台模型工作。测试使用合成部署验证事实，不将其写入生产。生产原验证的复用只来自原文件/原行，工具不造新验证。

最终测试记录：扩展组合 25 passed（维护工具 10 项 + 原 runtime/model-usage/tenant-model 15 项）；最后增加非目录/越权额度与 CLI 脱敏检查、收紧 prepare 仅接受目录部署后，维护工具独立组 12 passed。Ruff、结构检查通过。推荐独立审查/Linux 复跑：

```bash
python -m pytest tests/integration/test_temporary_limit_maintenance.py tests/integration/test_standalone_model_runtime.py tests/integration/test_model_usage.py tests/unit/test_tenant_model_runtime.py -q --basetemp=/tmp/tos-temp-policy-review
python scripts/check_boundaries.py
```

维护测试依赖同一 Git tree 中本计划目录的 temporary-limit-version.py（测试按仓库根绝对定位）、既有 tests/integration/conftest.py 的受控本地 PostgreSQL，以及 test_need_units 的每例隔离数据库/migration fixture；不依赖 /tmp 私有脚本或生产文件。工具、计划及测试一并显式提交，git archive 可复现。CLI 拒绝只输出固定 maintenance_refused_or_incomplete，不输出原异常或参数。
