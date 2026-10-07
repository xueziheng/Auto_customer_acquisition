# Task 1 独立评审

## 规格符合性

- ❌ 发现问题：认证基础的功能与接口符合任务范围，但新增测试的失败诊断不满足「合成测试只输出安全计数/错误码、材料不进入模型输出」约束，见 I1。
- ✅ 简报逐项文件均有对应变更；ADR 按允许的编号顺延至 `docs/adr/0067-web-pilot-authentication.md:6`。`shared/authentication.py:51` 提供完整 Protocol，`infra/authentication/service.py:324` 提供外部事务绑定接口；没有新增业务角色或 Employee 种子。
- ⚠️ API/CLI 为 Task 2，运行模式和浏览器为 Task 4/5，本次不判定这些尚未实现的要求；见下方跨任务事项。

## 优点

- `shared/authentication.py:41` 将会话与 CSRF 字段设为 SecretStr、repr=False、exclude=True，冻结 DTO 不携带角色；`infra/authentication/service.py:237` 单条联表读取当前账号、员工状态及 user 映射，同时检查到期、撤销和版本。
- `infra/authentication/passwords.py:20` 固定 scrypt 编码参数与盐/摘要长度；`infra/authentication/passwords.py:62` 用进程级槽和 shield 保持取消后的实际工作并发上限。`tests/unit/test_authentication_passwords.py:56` 验证取消不释放仍在运行的槽。
- `infra/authentication/service.py:139` 在认证失败异常抛出前提交失败计数，并借共享租户锁串行化发行；`infra/authentication/service.py:382`、`:395`、`:406` 沿同一锁顺序完成重置、启停与撤销。代价已在 `docs/adr/0067-web-pilot-authentication.md:30` 明确。
- `infra/db/tables.py:5844`、`:5865`、`:5887` 的三个新增表都有 tenant 复合主键，账号与会话的外键也包括 tenant；未知账号只使用固定桶。迁移与 ORM 定义一致；`tests/integration/test_web_authentication.py:350` 包含迁移往返与聚焦 metadata 比较。
- `tests/integration/test_web_authentication.py:193` 检查并发失败计数；`:218` 检查账号绑定提交/回滚及真实跨租户外键；`:323` 检查并发发行后的五会话上限。没有只断言 mock 调用的替代验证。

## 问题

### Critical

无。

### Important

**I1 — 新增测试的断言重写会在失败时输出认证材料。**

- 位置：`tests/unit/test_authentication_passwords.py:110` 直接比较 `csrf_for(token).get_secret_value()` 与原始字符串 `expected`。一旦派生算法或编码出现回归，pytest 默认 assertion rewriting 会在失败详情显示双方的原始 CSRF 值。
- 同类位置：`tests/integration/test_web_authentication.py:71` 对原始会话做 repr 包含关系断言；`tests/unit/test_authentication_passwords.py:23` 对原始密码做同类断言。其安全性回归发生时，断言诊断本身会输出要保护的字符串。集成测试中的摘要不等于原始材料检查（`:69`、`:70`）也把原始材料交给了断言重写。
- 影响：这是明确的凭证非输出边界，合成材料也必须遵守。已报告的成功运行和 `--tb=no` 只说明那次执行未输出材料，不能使新测试在常规 pytest/CI 失败路径下安全。未实际输出或复现任何材料。
- 建议：在断言之外完成敏感比较，只把布尔值交给带固定安全错误码的断言；失败消息不得附带对象、字符串或局部变量。对安全自检也避免把可能泄漏的 repr 传给 pytest 重写。以受控错误注入验证失败输出，子进程捕获全部输出，仅向控制器报告泄漏检测布尔值/计数；不要打印捕获内容。

### Minor

无。

## 跨任务待核对

- ⚠️ Task 2：`IssuedSession` 默认排除材料，API 必须显式提取 CSRF 到私有 no-store 响应、session 只写 Set-Cookie；校验 cookie/Origin/Host/CSRF、禁开发身份头、从当前 Employee 解析权限，并保持未配置非 dev 失败关闭。此 diff 只有基础接口，不能证明接线正确（`shared/authentication.py:41`、`:51`）。
- ⚠️ Task 2：可信 CLI 必须保持外部事务原子性、异常回滚与租户→账号→员工锁顺序；绑定既有 Employee 的调用方不能先持有员工锁。`infra/authentication/service.py:324` 已明确契约，调用方尚未在本任务实现。
- ⚠️ Task 4/5：请求体限额、数据库日志配置、loopback/owned 资源、真实 Provider 拒绝、恢复撤销、浏览器状态清理属于后续验收。此报告不把接口完成当作这些运行保障已完成。

## 证据与检查范围

- 依据：`.superpowers/sdd/2026-09-07-web-internal-pilot/task-1-brief.md`、同目录 `task-1-report.md`、`review-b31b8d2..6c8a2de.diff`；BASE `b31b8d2a6e5d5dbc4ab3b938d6cf00eab80f25c1`，HEAD `6c8a2de5f443eba8c705e8044757fdb636b50acf`。
- 按切片阅读完整 diff 一次；首个输出被工具截断的 ADR/计划片段补读，随后仅提取定义/断言行号，没有另读变更源码或执行 git 命令。只审 Task 1 的计划/设计修订；后续任务改动不作为本任务完成项。
- 范围外聚焦检查 1（命名风险：认证写入是否由现有引擎默认记录 SQL 参数）：读取 `infra/db/session.py:20`；工厂没有启用 echo，也没有设置 hide_parameters。当前实现捕获 SQLAlchemyError 并固定对外异常；未来装配仍不得启用参数日志，本次不据此推断已有泄漏。
- 范围外聚焦检查 2（命名风险：凭证断言是否由仓库 pytest 配置统一禁止失败展开）：仅查询 `pyproject.toml`、`pytest.ini`、`tests/conftest.py` 的相关选项，未发现保障。查询同时确认 ORM Base 为普通 DeclarativeBase，不是自动输出字段的 dataclass repr。
- 已读根目录及 shared/infra/tests 的 AGENTS.md。未爬取全库，未生成凭证、连接真实 Provider、发送消息、修改源代码/index/HEAD，唯一写入为本报告。
- 实现报告记录最终 25 passed、mypy/ruff/结构自检通过、无测试 warning/skip；新增测试参数化数量与所报命令的 25 项相符。未重跑同版本测试。TDD 历史属于报告证据，不能从最终 diff 独立证明执行时序。

## 质量结论

**Needs fixes。** 核心认证设计、租户查询和事务撤销实现未发现本任务范围内的阻断缺陷；先修复 I1 的失败输出边界，再进入 Task 2 接线。

计数：Critical 0；Important 1；Minor 0。
