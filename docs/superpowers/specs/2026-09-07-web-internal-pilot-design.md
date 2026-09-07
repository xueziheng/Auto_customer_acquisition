# Web 持久化内测与真实登录设计

## 授权与范围

用户批准下一步按「持久化 Web 内测环境＋真实登录」实施。本轮是架构扩展，交付单租户、本机 loopback 内测：停止、关机、再次启动保留数据；账号登录、退出、失效、停用；备份恢复到新目标。沿用既有 Web 业务服务与桌面扩展契约。真实研究、真实邮件、供应商联系、共享服务器部署和桌面端属于后续阶段。

旧受控入口的资源清理和合成场景保持原语义。新增入口不加载真实外部凭证，不产生合成业务结果冒充真实结果。业务政策必须由操作者显式提供；不能把受控测试的评分、SLA、利润或市场默认值带入内测。没有配置的能力明确显示未配置。

## 选择与代价

采用现有 Postgres 存账号和会话、现有 API 进程提供同源静态 Web 与 `/api`，独立持久化 Docker 卷保存 Postgres 和对象资料。相比第三方身份平台，避免本轮增加外部依赖，但需自行承担密码、会话、撤销与备份维护。相比浏览器保存 bearer token，HttpOnly cookie 减少脚本直接读会话的面，但需明确 CSRF 与来源校验。

本轮只监听 `127.0.0.1`，固定校验精确 Origin 和 Host，不接收转发头推断身份。HTTP cookie 仅在此明确本机模式不设 Secure；不能以此宣称可直接对公网开放。共享部署必须另行完成 HTTPS、Secure cookie、代理信任与运维验收。

## 约束

- 所有新增表与查询强制租户隔离，账号映射到当前 Employee，不从浏览器接受角色、scope、tenant、owner。
- 后端每次身份解析读取当前员工状态及当前权限；停用账号、重置密码、退出使之后请求的旧会话无效。已经被接纳的在途请求不承诺全事务回滚；已有关键业务的执行时事实检查保留。
- 凭证只在运行进程、受限配置文件和浏览器会话内处理，不打印、回传日志、提交、进入 Agent/model/prompt。测试使用合成随机凭证，输出仅计数和安全错误码。
- 密码通过本机交互式终端 `getpass` 设置；没有默认账号密码、注册、邮件找回或命令行密码参数。
- 密码使用标准库 scrypt：N=131072、r=8、p=1、dklen=32、随机16字节salt，单次 maxmem=268435456。密码15–128个Unicode字符且UTF-8不超过512字节；不截断、不去首尾空白。格式严格限定参数，禁止畸形记录触发任意资源消耗。哈希在工作线程运行，最多2个同时执行，避免阻塞事件循环。
- 会话使用32字节随机token；CSRF以解码后的session token作HMAC-SHA256密钥、固定上下文`tradeos:csrf:v1`推导，URL-safe base64无padding编码。数据库仅保存各自SHA-256摘要；刷新读取会话可重建CSRF，不在GET写入或轮换。绝对有效期8小时，无自动续期。登录轮换会话；每账号至多5个活动会话，超出撤销最旧会话。cookie HttpOnly、SameSite=Strict、Path=/api，无 Domain；名称为`tradeos_session_`加已验证Origin端口，避免原profile和恢复profile在同一浏览器互相覆盖（cookie本身不隔离端口）。
- 登录失败对未知账号、错误密码、停用账号使用相同401中文消息。持久化限流：账号5次失败/15分钟；未知用户名共用一个租户桶，避免无限创建；租户30次尝试/分钟，超限429。并发与重启不能绕过；不信 X-Forwarded-For。
- `/auth/login` 只接受小型JSON（4 KiB上限），所有不安全方法要求精确Origin及自定义浏览器请求头；已登录写操作还必须校验会话绑定的CSRF token。登录仅JSON，其他业务写操作保留各原router的请求体/MIME校验与空命令协议，不在认证层复制上传规则。拒绝重复cookie、重复安全头、不匹配来源、跨站请求。GET不产生业务写操作，原匿名unsubscribe保持既有严格独立契约。会话模式的GET `/health/live`与`/health/ready`只提供原固定状态供本机supervisor无密码探测，精确Host仍校验；capabilities与其他health路径不匿名。
- 会话模式禁用 X-Employee-Id/X-Tenant-Id 身份断言，无自动退回开发模式；无认证依赖的非dev入口仍失败关闭。原受控开发模式继续隔离运作。
- Web不在localStorage/sessionStorage/URL保存密码或会话。首次读取会话前不挂载业务页面；401、退出和身份变化清除页面状态并取消/隔离旧请求，通知组件随之卸载。403业务拒绝不一律当退出。浏览器标签页退出同步只传播事件，不传播token。
- Postgres与对象存储使用独立具名卷，profile目录0700、配置0600，拒绝软链接、不正确所有者/权限。停止只停本profile精确拥有的进程和容器，不删除卷或配置。新入口没有自动清库或重置命令。
- 资源操作核对 owner 标签、精确ID、卷绑定和进程出生时间；并发start/stop/backup/restore使用profile锁。Web/API端口首建分配后保存在profile并复用；被其他程序占用时安全拒绝，避免浏览器入口漂移。DB/对象端口重分配后重新生成当前子进程配置，禁止继续使用旧端口。只使用本地已有镜像，不自动pull。
- `init`/`migrate`显式升级schema；`start`只检查当前schema，拒绝落后版本，不自动迁移。单份profile最多一个scheduler与notification worker。通知使用显式LOCAL_IN_APP模式，只承诺真实站内投递并披露email disabled；默认生产入口的邮件必需约束及原controlled模式保留。
- 备份采用两个存储容器均停止后的物理卷归档，全部应用与存储写入已静止，包含DB、对象原件和恢复所需私有配置；完整性manifest含版本、精确镜像ID和SHA256。写入新目录并原子完成，失败不称成功。备份含敏感数据，0700/0600，不进入模型输出或版本库；本轮不实现备份加密或自动保留策略。
- 恢复只到新profile/新卷，不覆盖现有环境；拒绝坏校验和、符号链接、路径穿越、不支持版本/镜像。完成后撤销恢复出的会话，必须重新登录。原始资料字节及tenant业务关联保持一致；恢复失败保留可诊断的安全状态，不清理其他owner资源。
- 外部动作仍经过原Gateway。内测入口不装配真实model、搜索、邮箱客户端，拒绝型适配器不能返回假成功；同时安装明确loopback网络限制作为纵深防线，说明它不是OS沙箱。
- 中文内部UI/文档/日志；Vue3/TS/Vite/Ant Design Vue；API类型由OpenAPI生成。应用进程零互相导入；infra不导入apps、不种业务数据。

## 接口与归属

`shared/authentication.py`只放冻结Principal/IssuedSession DTO与AuthenticationService Protocol，凭证字段repr=False/SecretStr。`infra/authentication/`实现密码、限流、会话仓储；`infra/db/tables.py`及单条Alembic迁移持有schema。账号维护由`apps/api/pilot_accounts.py`可信本机管理入口装配，创建Employee和绑定账号在受控事务中执行，不能绕过租户/经理角色关联校验。员工角色/姓名/经理事实由员工域公开纯校验接口裁决；CLI只映射事实并装配。CLI通过既有公开`create_account(session=...)`取得tenant锁并持有到外部事务结束，随后锁读经理事实并校验、设置关系；拒绝则完整回滚。不新增私有锁访问或平行事务端口。

AuthenticationService对外为 `login(username: str, password: SecretStr) -> IssuedSession`、`authenticate(token: SecretStr, *, csrf_token: SecretStr | None = None) -> AuthPrincipal`、`logout(token: SecretStr) -> None`，以及`get_session(token: SecretStr) -> IssuedSession`用于验证并重建当前会话私有响应（含原绝对expiry）。实例固定tenant；Principal只包含tenant_id、employee_id、user_id，IssuedSession包含principal、token、csrf_token、expires_at。管理接口 `create_account`、`reset_password`、`set_enabled` 与 `revoke_all` 供可信本机装配调用，不暴露给业务Agent。实现可增加内部机械接口，必须在报告记录精确签名。

API以显式AuthenticationService注入真实模式，登录/会话/退出路由均导出OpenAPI。GET `/auth/session`提供当前员工安全资料和会话CSRF材料（禁止日志），不返回密码或session token。cookie只通过Set-Cookie发放。新入口挂载在`/api`，静态路由不得把未知API或路径穿越回退为index.html。注销是POST，清cookie且服务端撤销；浏览器撤销失败要明确显示失败，不能假称服务器会话已撤销。

`infra/pilot/`只负责profile、资源、备份恢复与技术配置；`scripts/run_web_pilot.py`为统一CLI。运行入口在各自apps进程下，复用原canonical工厂与service。同源静态Web使用生产构建，无受控角色选择器。

## 验收

1. 真实Postgres验证密码、会话、限流、租户FK、并发撤销以及upgrade→downgrade→upgrade。
2. API验证cookie、CSRF/Origin/Host、开发头拒绝、当前员工权限、401清理语义及安全错误；原dev和非dev未配置回归。
3. 本机owned资源实际启动→写入→完整停止→重启，业务数据和对象SHA保持；备份→新目标恢复保持，旧会话拒绝；不覆写原profile。
4. 实际浏览器登录、刷新保持、读取业务数据、退出、多标签页退出/失效、不同员工权限、窄屏。验证built Web和同源真实cookie链路，不能只用mock充当端到端。
5. 运行结构自检、受影响Python测试、Web类型/lint/测试/build与独立审查。仅报告实际运行的版本和结果；真实Provider与共享TLS部署标记未运行。

依据：[OWASP密码存储](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)、[会话管理](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)、[CSRF防护](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)。具体数值是本轮技术配置，非业务政策。
