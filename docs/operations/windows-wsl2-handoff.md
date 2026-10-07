# Windows / WSL2 代码交接与首次运行

本说明依据 2026-09-28 工作区检查和 WSL2 Ubuntu 24.04 实机验收编写，目标是在同事电脑上独立运行一份 TradeOS。
推荐路线：**固定交付版本 → WSL2 Ubuntu 24.04 → 安装 Linux 依赖 → 受控演示验收 → 持久运行**。
受控入口已在 Docker Desktop WSL Integration 环境完成启动、Windows 浏览器访问、HUP 重启、
TERM 清理和强制 E2E 验收；真实邮箱、模型、搜索及业务数据仍不属于这次验收范围。

## 1. 先选运行入口

| 目的 | 入口 | 停止后的数据 | 额外输入 |
|---|---|---|---|
| 接代码、验证安装、演示 | `scripts/run_web_core_controlled.py` | **删除本次数据** | 无真实密钥；仅合成外部响应 |
| 独立内测、账号登录、保留资料 | `scripts/run_web_pilot.py` | 保留数据库及对象卷 | 负责人确定业务政策，人工创建账号 |
| 内置 Agent、真实模型与公开研究 | `apps.api.standalone` + `apps.scheduler_worker.standalone` | 使用持久 profile | 另需模型配置、额度、资料外发许可；研究另配搜索 |

第三种入口见第 7 节。不要把启动成功等同于真实获客、邮箱、报价全都可用。
这些入口均为本机访问；让同事的电脑独立运行，不等于把一台电脑开放为公司共享服务器。

## 2. 交付方：固定代码版本

原始交接基线为 `aebaa9b6c0ca2c148cc9ffc9d8c926e6f7f8f85f`。WSL2 实机修复在
本机 `codex/wsl2-acceptance-fixes` 分支继续提交，当前只保存在本地、未推送 GitHub；接收方必须
以交付方实际提供的源码和完整 SHA 为准，不能从远端同名分支推断已包含修复。不要复制 `.venv`、
`node_modules`、运行资料或密钥。

推荐交付经审阅的 Git 提交，并把完整 commit SHA 发给同事：

1. 在当前工作区核对 `git status --short`，确定要交付哪些既有修改和新文件。
2. 用 `git add -- 文件路径…` 逐项暂存交付文件。不要对当前工作区直接执行 `git add .`；
   存在大量运行输出，需与源码分开处理。运行 profile、备份、`.env`、OAuth 文件和密钥不进版本库。
3. 在项目虚拟环境运行下列检查，再审阅暂存差异；敏感扫描只是一道检查，不证明整个历史无秘密。

```bash
source .venv/bin/activate
python scripts/check_boundaries.py
python scripts/scan_sensitive.py --staged
git diff --cached --stat
```

4. 完成交付范围的测试、提交并推送到同事有权限访问的仓库分支，记录 `git rev-parse HEAD`。
   只有交付方另行明确发布时，接收方才能下载修复分支；当前 GitHub 交接仍只有
   `codex/gmail-mailbox-sync` 基线，不含本机 WSL2 修复。
5. 若不用远程仓库，在已完成上述提交后生成源码包（不带 Git 历史）：

```bash
git archive --format=tar.gz --prefix=TradeOS/ --output=../tradeos-source.tar.gz HEAD
shasum -a 256 ../tradeos-source.tar.gz
git rev-parse HEAD
```

源码包仅含已提交文件，**不含未提交修改或未跟踪文件**。把 SHA、压缩包校验值和本说明一起交付。
依赖、业务数据和凭证分别处理。开发协作优先 Git clone，源码包适合一次性交付。

## 3. 接收方：Windows 准备

管理员 PowerShell 执行；已有 WSL2 Ubuntu 24.04 则只检查版本：

```powershell
wsl --install -d Ubuntu-24.04
wsl --update
wsl -l -v
```

按提示重启，在 Ubuntu 首次启动时创建 Linux 用户。`wsl -l -v` 中 Ubuntu 的 VERSION 应为 2；
若仍为 1，再执行 `wsl --set-version Ubuntu-24.04 2`。
依据：[Microsoft WSL 安装说明](https://learn.microsoft.com/en-us/windows/wsl/install)。

安装并启动 Windows 版 Docker Desktop，使用 Linux containers，在设置中开启 WSL2 引擎和
Ubuntu-24.04 的 WSL Integration。不要同时在这个发行版里再安装另一套 Docker Engine。
项目固定连接 `/var/run/docker.sock`，仅 Docker CLI 能使用某个远端 context 并不足够。
依据：[Docker WSL2 后端](https://docs.docker.com/desktop/features/wsl/)。

以下命令均在 **Ubuntu Bash 终端**执行，不在 PowerShell、Git Bash 或 Windows Python 中执行。

```bash
sudo apt update
sudo apt install -y git curl ca-certificates build-essential python3.12 python3.12-venv python3.12-dev
docker version
test -S /var/run/docker.sock && echo '本机 Docker socket 存在'
```

Node 必须为 **24.x**（`apps/web/package.json` 的约束）。若尚未安装 Linux 版 nvm，按
[nvm 官方说明](https://github.com/nvm-sh/nvm#installing-and-updating)安装；然后执行：

```bash
nvm install 24
nvm use 24
node --version
command -v node
command -v python3.12
```

路径应是 Linux 路径；不要使用 `/mnt/c/Program Files/…` 下的 Node 或 Windows Python。
每个新终端都应加载 nvm，再激活项目 `.venv`。

## 4. 接收源码并重建依赖

代码放 `~/projects`。运行 profile 放 WSL Linux 用户目录，不放 `/mnt/c`、OneDrive 或 Windows
共享目录：既减少跨文件系统 I/O，也满足项目对 owner、0700/0600 权限和链接的严格检查。
依据：[Microsoft WSL 文件系统互操作说明](https://learn.microsoft.com/en-us/windows/dev-environment/wsl-interop)。

Git 方式：先确保同事具有仓库访问权限，认证通过本机 Git 凭证工具处理，不把 Token 放 URL。

```bash
mkdir -p ~/projects
cd ~/projects
git -c core.autocrlf=false clone --branch codex/gmail-mailbox-sync https://github.com/xueziheng/Auto_customer_acquisition.git TradeOS
cd TradeOS
git config core.autocrlf false
git fetch origin
read -r -p '输入交付方提供的完整 commit SHA: ' TRADEOS_HANDOFF_COMMIT
git switch --detach "$TRADEOS_HANDOFF_COMMIT"
git rev-parse HEAD
```

确认 SHA 与交付方一致再继续。要继续开发，可从该版本创建自己的 `codex/…` 分支。
若使用源码包，先用 `sha256sum` 核对校验值，再解压到 `~/projects`，进入 `TradeOS`；
源码包没有 `.git`，不能运行依赖 Git index 的扫描/协作命令，需要开发时应使用 clone。

安装依赖，**不要从 Mac 复制 `.venv` 或 `node_modules`**：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
python -m pip check
npm --prefix apps/web ci
docker pull pgvector/pgvector:pg16
docker pull bitnamilegacy/minio@sha256:50cec18ac4184af4671a78aedd5554942c8ae105d51a465fa82037949046da01
```

镜像必须预拉取，启动器不会自行 pull。这些入口自己管理 PG/MinIO；**不用先 `make dev` 或
启动 `infra/docker-compose.yml`，也不用复制 `.env`**。那是另一条常规依赖编排路线。
旧的 `minio/minio:RELEASE.2025-04-22T22-12-26Z` 标签已无法从官方仓库取得；受控入口改用与该版本
二进制对应的不可变 digest，并使用镜像为 UID 1001 准备的数据目录，不需要把容器改成 root。

当前前端有 `package-lock.json`，Python 主项目尚无完整锁文件；`pip install` 会解析部分浮动依赖，
不能承诺每次安装都与 Mac 完全相同。首次目标机验收通过后，交接记录应保留 Python/Node/npm/Docker
版本及 Python 包版本清单（只记录名称/版本，不导出环境变量或带认证的安装 URL）。后续可独立补锁定流程。

## 5. 第一次运行：受控演示

在项目根目录、已激活 `.venv` 的 Ubuntu 终端执行：

```bash
source "$HOME/.nvm/nvm.sh"
source .venv/bin/activate
python scripts/check_boundaries.py
python scripts/run_web_core_controlled.py --api-port 18080 --web-port 15173 --scheduler-port 18081
```

等待输出 `ready`，用 Windows Edge/Chrome 打开输出的 `web_url`，本例通常为
`http://127.0.0.1:15173`。Windows 浏览器可访问 WSL 中的本地服务，依据
[Microsoft WSL 网络说明](https://learn.microsoft.com/en-us/windows/wsl/networking)。
保持启动终端运行。按[演示操作说明](web-core-local.md#人工配置与明确的演练输入)演练审批与研究。

**这一入口是一次性合成环境，Ctrl-C 会清理并删除本次数据库和对象，不能用来长期存业务资料。**
关闭 Windows 浏览器不等于停止程序；正常结束请在启动终端 Ctrl-C，核对最终停止结果。

需要自动化验收时，另开同一环境的终端，运行现有测试；测试自行管理独立资源：

```bash
source "$HOME/.nvm/nvm.sh"
source .venv/bin/activate
python -m playwright install --with-deps chromium
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 TRADEOS_REQUIRE_E2E=1 python -m pytest tests/e2e/test_web_core_controlled.py -q --tb=short
```

`TRADEOS_REQUIRE_E2E=1` 要求缺 Docker/镜像/浏览器时失败，不能把 skip 当验收通过。

Docker Desktop 在短时间内反复销毁、重建多套带随机发布端口的完整测试基础设施后，WSL 端口代理
可能明显变慢；日常 HUP 重启只重启四个应用进程，不重建 PG/MinIO，不受这一压力场景影响。
若 Docker Desktop 显示 running，但 Ubuntu 中 `/var/run/docker.sock` 缺失或无法连接，先确认没有
仍需保留的容器和在途任务，再在 **Windows PowerShell** 执行完整重置：

```powershell
docker desktop stop
wsl --shutdown
docker desktop start
```

等待 Docker Desktop 恢复为 running，重新打开 Ubuntu 终端，再核对：

```bash
test -S /var/run/docker.sock
docker version
```

不要在 socket 未恢复时连续重跑整套验收；这会留下无法即时确认的 Docker 请求和误导性的超时。

## 6. 要保留账号和数据：持久内测

先停止第 5 节演示。构建静态前端，Python 虚拟环境必须已激活：

```bash
npm --prefix apps/web run gen:api
npm --prefix apps/web run typecheck
npm --prefix apps/web run build
```

由负责人按[业务政策字段格式](web-internal-pilot.md#显式业务政策)填写真实决定的政策 JSON。
不能替负责人编造 SLA、金额分档或评分映射。文件例如放在
`~/.local/share/tradeos/policy.json`；下面只创建父目录，profile 本身必须尚不存在：

```bash
umask 077
mkdir -p ~/.local/share/tradeos
chmod 700 ~/.local/share/tradeos
export TRADEOS_PROFILE="$HOME/.local/share/tradeos/colleague"
python scripts/run_web_pilot.py init --profile "$TRADEOS_PROFILE" --policy-file "$HOME/.local/share/tradeos/policy.json"
python scripts/run_web_pilot.py start --profile "$TRADEOS_PROFILE"
python scripts/run_web_pilot.py accounts --profile "$TRADEOS_PROFILE" create --username colleague --name 同事 --role boss
python scripts/run_web_pilot.py status --profile "$TRADEOS_PROFILE"
```

政策文件要在 `init` 前人工准备好。账号命令在终端隐蔽询问密码，没有默认密码；按实际职责创建账号。
有双人独立审批的业务，还需要另一位实际审批人的账号，不能以同一人自批绕过规则。
浏览器打开 `status` 返回的 `web_url`，使用刚创建的账号登录。这种模式保留数据，但不启用真实模型和搜索。

日常停止/重启：新终端先进入项目、激活虚拟环境，并重新设置上面的 `TRADEOS_PROFILE` 路径。

```bash
python scripts/run_web_pilot.py stop --profile "$TRADEOS_PROFILE"
python scripts/run_web_pilot.py status --profile "$TRADEOS_PROFILE"
# 确认 storage 和 applications 均为 stopped 后再关闭 Docker、关机或 wsl --shutdown。
python scripts/run_web_pilot.py start --profile "$TRADEOS_PROFILE"
```

更新源码前先停止、按[冷备份流程](web-internal-pilot.md#冷备份与恢复到新目标)保存备份；
更新后重装依赖、重建前端，再显式 `migrate`，最后 `start`。不要对既有 profile 重跑 `init`。
旧文档中的 `0060`、`0067` 是历史 schema 描述，以交付版本的迁移 head 为准，不能照旧号降级。

## 7. 要运行真实 Agent / 搜索 / 邮箱

持久存储、账号确认可用后，沿[内置 DeepSeek Agent 操作说明](builtin-deepseek-agent.md)配置并启动。
从第 6 节切换时先执行 pilot 的 `stop`，再执行 `migrate` 启动存储，最后分别开三个终端启动
standalone API、standalone scheduler 和 notification。不要同时运行 pilot `start` 和 standalone 进程。

旧说明中的 `unsetopt` 和 `read -rs '变量?提示'` 是 zsh 语法，Ubuntu 默认 Bash 应改成：

```bash
set +x
read -r -s -p 'DeepSeek API Key: ' DEEPSEEK_API_KEY
echo
export DEEPSEEK_API_KEY
```

仅在管理员自己操作的 scheduler 终端输入；不要在 Agent 工具调用、聊天、脚本参数或仓库内传密钥。
模型配置中的限额和业务资料外发许可要明确决定；先通过产品内显式连接探测再开始使用。
公开研究还需要搜索配置、有效公司 Playbook 和国家政策；只有模型 key 不等于研究已就绪。

搜索账号不能在两台电脑上同时冒充“独占账号”。如果保留旧机研究运行，应给新机独立账号或先明确切换；
不得直接复制 `exclusive_account_confirmed=true` 而不核对实际使用情况。
个人 Gmail 按[本人邮箱同步](private-gmail-mailbox.md)在新机独立配置授权和绑定，不传原机 Cookie/OAuth Token。
复制代码不等于授权新机发送邮件、启动 Campaign 或承诺价格；这些动作继续遵守审批边界。

## 8. 既有数据迁移单独处理

新机默认是全新数据。既有 profile 不只是源码目录：还引用本机 Docker 容器、卷、随机技术凭证和运行身份。
不要复制运行目录后强行启动，也不要把 Mac 数据卷文件夹直接盖到 Windows 卷上。

现有 backup/restore 是**停止状态的物理归档**，会核对精确镜像 ID；相同镜像 tag 不保证 ID 相同，
Mac ARM 与 Windows x86 的镜像也可能不同。因而本说明不承诺跨机、跨 CPU 架构恢复已经可用。
若必须带走原业务数据，要先确认源/目标架构和镜像、停止所有写入者、迁移 PG 与对象原件、核对关联及哈希，
并在全新目标演练登录、权限、后台任务和恢复。跨架构通常需另行设计逻辑导出/导入及对象复制，现有脚本不是该工具。
备份本身含凭证和业务资料，须通过负责人控制的加密渠道处理，不进 Git 或聊天。

## 9. 常见问题和交接验收

| 现象 | 优先检查 |
|---|---|
| Docker CLI 成功但启动器失败 | Ubuntu 的 WSL Integration；`/var/run/docker.sock` 是否存在且当前用户可访问；不要用远端 context 代替 |
| 报缺镜像 | 第 4 节两条 `docker pull`；启动器不会自动下载 |
| `node` 或 native module 错误 | Node 是否为 Linux 24.x；重新在 WSL 执行 `npm ci`，不复用 Mac/Windows node_modules |
| Python 语法或包找不到 | Python 3.12+，`source .venv/bin/activate`，`python -m pip check` |
| `profile_exists` | `init` 仅限新 profile；既有 profile 用 `start`，不删除资料“重试” |
| `policy_invalid` | 政策 JSON 类型、完整字段、金额字符串和负责人实际决定；不是缺 key |
| `configuration_invalid` | 私有文件 Linux owner、目录 0700、文件 0600；不要放 `/mnt/c` 或使用链接 |
| schema 不匹配 | 停止应用、备份，再显式 `migrate`；不能手改 alembic_version |
| Windows 浏览器打不开 | 输出是否 ready；WSL 内能否访问相同 URL；Docker Desktop/WSL 转发、防火墙/VPN和端口冲突；不要改监听到 0.0.0.0 绕过 |
| 重启后数据没了 | 是否误用了受控演示入口；它的清理语义就是删除合成资源 |

双方按以下内容留一份不含秘密的记录：

- 完整 commit SHA、安装版本、同事电脑的 WSL2 发行版和 CPU 架构。
- 结构检查通过，前端构建通过；受控入口 ready、Windows 浏览器能打开，正常停止无清理错误。
- 如交接持久模式：能创建账号、登录、stop/start 后仍能登录并保留一条人工录入资料。
- 如交接真实 Agent：配置责任人、预算、当前版本 probe 结果；未运行的搜索/邮箱项目明确记为未验收。
- 明确源码交付是否包含数据；保留原机直到目标机验收和必要的数据恢复演练完成。

机器睡眠、Windows 更新、退出 Docker 或关闭 WSL 都可能中断后台任务。本机交接不提供 24 小时服务保证。
需要长期无人值守或多人共享时，应另做服务托管、HTTPS 和备份恢复部署验收。
