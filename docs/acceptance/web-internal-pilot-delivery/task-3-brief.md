### Task 3: 独立持久化profile与冷备份恢复

**Files:**
- Create: `infra/pilot/AGENTS.md`, `infra/pilot/__init__.py`, `infra/pilot/config.py`, `infra/pilot/resources.py`, `infra/pilot/backup.py`
- Create: `scripts/run_web_pilot.py`
- Create: `tests/unit/test_pilot_profile.py`, `tests/integration/test_pilot_persistence.py`

**Interfaces:**
- Produces `PilotConfig.read(path: Path)`验证0700/0600/owner并隐藏凭证；`database_url: SecretStr`、`tenant_id: str`、当前端口、精确资源ID、`runtime_environment() -> dict[str,str]`、SecretResolver.resolve接口。
- Produces CLI `init --profile PATH --policy-file PATH`、`migrate --profile PATH`、`start --profile PATH`、`stop --profile PATH`、`status --profile PATH`、`backup --profile PATH --destination NEW_PATH`、`restore --backup PATH --profile NEW_PATH`。Task4完成start应用接线；本任务infra生命周期可独立由测试调用。
- Consumes现有OwnedProcess精确PID/birth停止协议；不复用OwnedContainers.close，不改变controlled资源语义。不导入apps、不种Employee或业务数据。

- [ ] **Step 1: 写失败测试。** profile权限/软链接/错owner/并发锁、端口更新、旧ID和错volume拒绝；实际owned PG+对象存储具名卷写合成数据并停止重启。
```python
await profile.stop()
await profile.start_storage()
assert await read_synthetic_marker(profile) == expected_marker
assert await read_object_hash(profile) == expected_hash
```
backup仅stopped，restore仅fresh；坏SHA/缺文件/穿越/软链接/错版本拒绝；验证原目标不变，恢复数据和对象相同，恢复会话撤销。
- [ ] **Step 2: 运行RED。** 容器只使用现有本地镜像，测试清理仅本测试新owner、删除动作不得触碰用户profile。
- [ ] **Step 3: 实现profile、锁、持久卷和冷备份。** 长期profile不使用tempdir、不在stop删卷；资源身份以实际label+ID+mount核对。独立政策输入必须完整显式，不复制controlled业务默认值；技术密钥随机且只入私有配置。迁移仅显式命令，start检查head。镜像ID写入manifest；备份流式写入新私有暂存目录再原子完成，所有文件有SHA；restore验证整包后创建独立owner卷，禁止解包出界，失败以固定错误码标记。恢复DB后按tenant撤销会话。runtime应用接线留明确私有launch协议，不能以空start假成功。
```python
with exclusive_profile_lock(profile_path):
    verify_owned_resources(config)
    stop_exact_owned_processes(config)
    stop_exact_owned_containers(config)  # 不remove，不删除数据卷
```
- [ ] **Step 4: 运行GREEN、实际持久化/恢复测试、结构自检。** 保存安全计数和hash比对结论，不输出实际私有config或业务原件。
- [ ] **Step 5: 自审并提交。** 报告精确config/lifecycle/launch接口、剩余Task4接线点，不能宣称CLI start整体验收已完成。


## Controller downstream preflight

Both local images verified available without pull: pgvector/pgvector:pg16 and minio/minio:RELEASE.2025-04-22T22-12-26Z. Existing backup reference is tests/integration/test_web_core_backup_restore.py (patterns only; never import test code into infra).
Task1 reviewed management API: PostgresAuthentication(factory, TenantId(...)).revoke_all(username=None) revokes only bound-tenant sessions, serializes with issuance. Report exact Task3 profile/launch interfaces for Task4. API account command takes explicit factory/tenant and typed command; Task4 supplies profile loading, no second configuration format.
User flow can init explicit policy and schema, start API without any default accounts, then run trusted getpass account command against running owned storage. No real user profile is to be populated with test credentials or synthetic policies during acceptance.
Restore must preserve original tenant/user/employee IDs and artifact bucket/key references even though resource owner/volume/container identity changes. Do not rename a restored bucket without migrating every stored reference; separate owned MinIO servers can retain identical internal bucket name. Opaque technical configuration remains private.
Use cold physical-volume archives with both storage containers stopped, matching the spec zero-write boundary and exact image-ID restoration. Docker official cp docs support running or stopped containers and tar streams; SDK get_archive/put_archive or a narrowly owned helper may be used. Verify actual named-volume bytes, destination UID/GID and root-directory structure in real restore tests; do not tar a running PG datadir or assume copied ownership is preserved. Never use pg_dump on a stopped container. Background Docker state and config errors must stay fixed/safe; no container inspect dump exposing Env.
After full stop, verify writes remain and no cleanup hook removes volumes; after Docker port reassignment, rewrite bound runtime configuration under lock. Stable Web/API port must be reused or fail on occupied port. First actual run requires operator policy; test policy values are isolated synthetic only.
