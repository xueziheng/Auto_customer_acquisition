# 持久化 Web 内测操作说明

本入口用于单租户、本机 loopback 内测。它使用生产 Web 构建、真实账号会话、PostgreSQL、MinIO、
scheduler 与 notification worker；完整停止后保留 profile、数据库卷和对象卷，再次启动读取同一份
数据。它不连接真实模型、搜索、邮箱或供应商，不发送邮件，也未通过共享服务器、TLS 或公网部署验收。

本入口与[本机受控演练](web-core-local.md)是两个环境。受控演练有开发身份、Vite 与四应用，停止时
删除合成资源；持久内测有真实登录、生产静态构建与三个应用，`stop` 保留资源。独立 Linux 报价验收
也不是本入口的能力。三类证据不能拼成一条真实业务闭环。

## 环境与首次准备

需要 Python 3.12+、Node 24.x、npm、本机 Docker socket `/var/run/docker.sock`，以及已存在的本地镜像：

- `pgvector/pgvector:pg16`
- `minio/minio:RELEASE.2025-04-22T22-12-26Z`

启动器只使用本地镜像，不会自动 pull。2026-09-07 验收环境为 Python 3.12.14、Node 24.15.0、
Docker Server 29.5.3、Playwright Chromium 151.0.7922.34。浏览器必须支持 Web Locks；当前只验收了
上述 Chromium。缺少 Web Locks 时页面会明确拒绝登录和退出，不降级到弱并发方案。

在仓库根目录安装依赖并生成生产构建：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
npm --prefix apps/web ci
PATH="$PWD/.venv/bin:/path/to/node-v24/bin:$PATH" npm --prefix apps/web run gen:api
PATH="/path/to/node-v24/bin:$PATH" npm --prefix apps/web run build
```

最后两条命令中的 Node 路径由操作者按实际安装位置替换。`start` 要求
`apps/web/dist/index.html` 已存在；它不启动 Vite 开发服务器。

## 显式业务政策

`init` 必须读取操作者自己决定的政策文件。下面仅说明字段形状，含不可运行占位符；不得直接复制为
公司政策，也不得使用测试中的合成评分、SLA、金额或市场值：

```jsonc
{
  "handoff_policy": {
    "sla_seconds": "<由负责人决定的正整数>",
    "backlog_threshold": "<由负责人决定的正整数>",
    "t1_seconds": "<由负责人决定的正整数>",
    "t2_seconds": "<由负责人决定的正整数>"
  },
  "scoring_policy": {
    "version": "<组织内可追踪的政策版本>",
    "currency": "<ISO 4217 币种>",
    "value_band_boundaries": ["<Decimal 字符串；禁止 float>"],
    "bucket_map": {
      "1": "<等级>", "2": "<等级>", "3": "<等级>",
      "4": "<等级>", "5": "<等级>", "6": "<等级>", "7": "<等级>"
    }
  }
}
```

操作者必须逐项确认 SLA、积压阈值、价值分档、币种和七个证据组合对应等级。未作决定就是信息缺失，
不能用示例补齐。金额边界只接受 Decimal 字符串。

## 初始化、迁移与启动

选择一个尚不存在的私有 profile 目录。不要指向真实用户目录、旧 profile、符号链接或共享目录。

```sh
.venv/bin/python3.12 scripts/run_web_pilot.py init --profile PROFILE --policy-file POLICY_JSON
.venv/bin/python3.12 scripts/run_web_pilot.py status --profile PROFILE
.venv/bin/python3.12 scripts/run_web_pilot.py start --profile PROFILE
```

`init` 创建随机 tenant、owner、数据库/对象凭证及两个持久卷，升级到当前 schema 后停止存储。
它不创建默认员工、默认密码或默认市场政策，也不创建操作者所给评分政策之外的业务资料或真实客户
数据。当前迁移是单一 head `0060`。`start` 只核对当前 schema，不自动迁移；版本落后时先停止，再显式执行：

```sh
.venv/bin/python3.12 scripts/run_web_pilot.py stop --profile PROFILE
.venv/bin/python3.12 scripts/run_web_pilot.py migrate --profile PROFILE
.venv/bin/python3.12 scripts/run_web_pilot.py start --profile PROFILE
```

`start` 启动同源 API/Web、scheduler、notification 三个真实进程，并等待三个 ready；Web 地址由
`status` 的 `web_url` 给出。该地址固定为 `127.0.0.1`，不得改成局域网或公网监听。健康就绪只证明
进程、schema 与存储可用，不证明业务政策、Provider、发送、供应商或审批已准备好。

## 首个账号与日常账号维护

必须先 `start`，再在真实交互终端执行账号命令。创建和重置密码时程序使用 `getpass` 连续询问两次；
密码不能通过 argv、环境变量或管道传入，不会显示在命令结果中。

```sh
.venv/bin/python3.12 scripts/run_web_pilot.py accounts --profile PROFILE create --username USERNAME --name NAME --role boss
.venv/bin/python3.12 scripts/run_web_pilot.py accounts --profile PROFILE create --username USERNAME --name NAME --role manager
.venv/bin/python3.12 scripts/run_web_pilot.py accounts --profile PROFILE create --username USERNAME --name NAME --role sales --manager-id EMPLOYEE_ID
.venv/bin/python3.12 scripts/run_web_pilot.py accounts --profile PROFILE reset-password --username USERNAME
.venv/bin/python3.12 scripts/run_web_pilot.py accounts --profile PROFILE disable --username USERNAME
.venv/bin/python3.12 scripts/run_web_pilot.py accounts --profile PROFILE enable --username USERNAME
```

首次由操作者创建 boss，登录后可在“团队与归属”读取已创建经理的 Employee ID，再为 sales 明确绑定
经理。允许角色为 `boss`、`manager`、`sales`、`sourcing`、`product`、`finance`、`viewer`；服务端在
每次请求重新读取当前员工状态和权限，浏览器显示的标签不是授权证据。

`disable` 只停用登录账号并撤销该账号会话，不删除员工业务记录。`enable` 不恢复旧会话，操作者仍需
重新登录。`reset-password` 原子撤销旧会话，旧密码和旧会话均不能继续使用。退出成功的 HTTP 结果为
204，无 JSON body。多标签页登录/退出依赖同源 Web Locks 和无 payload 的失效广播；Cookie 名带 API
端口只避免两个合作 profile 相互覆盖，不能隔离恶意本机同源服务。

## 停止、关机与再次启动

计划关机、系统更新或移动磁盘前先执行：

```sh
.venv/bin/python3.12 scripts/run_web_pilot.py stop --profile PROFILE
.venv/bin/python3.12 scripts/run_web_pilot.py status --profile PROFILE
```

只有 `storage=stopped` 且 `applications=stopped` 才能关机或备份。`stop` 核验 owner、容器、卷、PID 与
进程出生时间，只停止该 profile 的三个应用及两个存储容器；它不删除 profile、容器或卷。项目没有
生产 `destroy/reset` 命令。不要使用 `docker prune`、宽泛 `pkill`、手工清表或删除卷代替停止。

再次运行 `start` 会复用固定 Web/API 端口和同一份卷；数据库/对象容器重启后的内部公开端口会从
Docker 实际绑定重新读取。固定 Web 端口被占用时安全拒绝，不杀占用者、不漂移浏览器入口。启动前
检查宿主机和 Docker 数据目录剩余空间；持续增长的数据库、对象和备份均由操作者承担容量监控，
当前没有自动保留、压缩或告警策略。

## 冷备份与恢复到新目标

先停止源 profile，再创建一个尚不存在的备份目录：

```sh
.venv/bin/python3.12 scripts/run_web_pilot.py stop --profile PROFILE
.venv/bin/python3.12 scripts/run_web_pilot.py backup --profile PROFILE --destination NEW_BACKUP_DIRECTORY
```

备份在数据库和对象存储都停止后物理归档，包含恢复配置、数据库、对象原件、精确镜像 ID 和 SHA-256
manifest。profile 与备份目录必须为当前用户所有且权限 0700，内部普通文件为 0600；备份含凭证和
业务数据，不得提交版本库、粘贴到对话、放入 Agent prompt 或置于多人可读目录。当前备份不加密、
不自动轮换，也没有异地复制。若备份与原盘位于同一磁盘或同一故障域，磁盘损坏会同时失去两者；
应由操作者另建加密、异地和保留策略并单独验收。

恢复只允许写入一个全新的 profile 路径，绝不覆盖源 profile：

```sh
.venv/bin/python3.12 scripts/run_web_pilot.py restore --profile NEW_PROFILE --backup BACKUP_DIRECTORY
.venv/bin/python3.12 scripts/run_web_pilot.py status --profile NEW_PROFILE
.venv/bin/python3.12 scripts/run_web_pilot.py start --profile NEW_PROFILE
```

恢复会创建新 owner、新卷和新端口，保留 tenant、bucket、对象字节及业务关联，核验归档安全、镜像 ID
和 SHA-256 后撤销恢复副本中的全部会话，并以停止状态交付。浏览器必须在新 `web_url` 重新登录。
源 profile 不被修改，可独立再次启动。恢复失败不自动删除别的 owner 资源，也不能把 pending/failed
目标强行启动。

## 安全界限与尚未验收项

- profile 只允许当前用户私有目录；`config.json` 含敏感引用和本机凭证，不要打印、复制或提交。
- 本入口只监听精确 IPv4 loopback HTTP。真实共享部署需要另行完成 HTTPS、Secure Cookie、反向代理
  信任、Host/Origin、CSRF、告警、备份保留和多人运维验收。
- 当前真实站内通知启用，email 明确 disabled；真实 Tavily、Hunter、Gmail、模型、供应商和客户发送
  均未运行。拒绝型适配器不是 Provider 成功。
- 合成团队、分配规则和对象只证明持久化、权限和恢复，不证明自动需求发现、客户验证、成交或北极星
  指标改善。
- Browser worker 的生产任务来源仍 disabled；本验收中的 Playwright 是测试驱动，不是业务 Browser
  worker。
- 桌面端仍只有[扩展契约](../architecture/12-client-capability-boundaries.md)，未实现 Tauri、IPC、Keychain、
  原生通知或桌面会话接管。

实际浏览器与生命周期证据见[2026-09-07 持久化内测验收](../acceptance/2026-09-07-web-internal-pilot.md)。
