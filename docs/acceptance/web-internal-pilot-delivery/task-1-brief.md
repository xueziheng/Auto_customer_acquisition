### Task 1: Postgres账号、密码、限流和可撤销会话

**Files:**
- Create: `shared/authentication.py`
- Create: `infra/authentication/__init__.py`, `infra/authentication/passwords.py`, `infra/authentication/service.py`
- Modify: `infra/db/tables.py`
- Create: `migrations/versions/0060_web_authentication.py`（先核对当前head；若编号已占用，顺延并记录）
- Create: `tests/unit/test_authentication_passwords.py`, `tests/integration/test_web_authentication.py`
- Create: `docs/adr/0028-web-pilot-authentication.md`（若编号占用则顺延）

**Interfaces:**
- Produces `shared.authentication.AuthPrincipal(tenant_id, employee_id, user_id)`、`IssuedSession(principal, token, csrf_token, expires_at)`及AuthenticationService Protocol，签名与Spec一致。
- Produces `infra.authentication.service.PostgresAuthentication(session_factory, tenant_id)`，实现login/authenticate/logout/get_session；管理方法create_account/reset_password/set_enabled/revoke_all精确类型在报告列出，供Task2可信CLI使用。
- Consumes `EmployeeRow`当前tenant/employee/user映射；不负责创造Employee、不新增业务角色逻辑。

- [ ] **Step 1: 写行为失败测试。** 用现有真实PG fixture种两个租户Employee，密码为测试进程随机生成且不打印；验证正确密码可登录、错误/未知/停用同类失败、摘要不等于原token、到期与撤销失败关闭。核心断言形状：
```python
issued = await auth.login(username, password)
assert (await auth.authenticate(issued.token)).employee_id == employee_id
await auth.logout(issued.token)
with pytest.raises(AuthenticationDenied):
    await auth.authenticate(issued.token)
```
补充scrypt严格参数和长度边界、重复会话上限、账号与租户限流、未知用户名桶有界、重启持久化、并发login/reset/disable互斥，跨租户employee绑定拒绝。
- [ ] **Step 2: 运行上述两个新测试文件记录RED。** 失败必须因能力缺失，不得把依赖或Docker未启动视作TDD证据。
- [ ] **Step 3: 实现冻结DTO、安全错误类型、密码与Postgres服务。** 以完整Spec为算法边界；所有密码/会话材料repr=False且错误固定。账户变更与会话发行使用账户行锁/版本重查，哈希工作线程有界；认证每次查当前账号和Employee活跃、user映射。通过单条迁移建tenant复合约束，ORM一致。登录失败计数提交不能随异常rollback；未知用户名用固定桶，不按任意字符串扩表。
```python
# 密码计算在受限线程内；随机session只有摘要持久化。
digest = hashlib.scrypt(password.get_secret_value().encode('utf-8'),
    salt=salt, n=131072, r=8, p=1, dklen=32, maxmem=268435456)
```
- [ ] **Step 4: 运行GREEN与迁移往返。** 新测试文件、现有migration/schema相关测试、结构自检；只保存安全日志。记录实际测试数和版本。
- [ ] **Step 5: 自审并精确提交。** 报告接口、事务/撤销语义、RED/GREEN、变更文件、已知限制。密码或token不得出现在报告。
