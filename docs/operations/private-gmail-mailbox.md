# 本人 Gmail 邮箱同步

入口为「我的邮箱」。显示绑定账号的历史收件、已发送、归档、草稿、垃圾邮件和已删除邮件；
按原 Gmail 线程归组，支持搜索、范围筛选、会话分页和长会话继续加载。只读镜像不会改变 Gmail
已读状态，也没有发送、删除或标签修改权限。正文以纯文本显示，附件名称/大小可见，原件在 Gmail 打开。

“已保存 N 封”是本地实际记录数。历史补录和最新变更追平结束后才显示“历史已补齐”。
“立即同步”只提交请求，必须有后台进程运行；最近同步时间不会因为点击按钮而伪造更新。
所有数据仅绑定员工本人可见，即使同公司的老板或经理也不能直接读取。

## Google 配置

必须使用项目自己的 Google OAuth 授权，不能搬用 Codex 或浏览器的 Token/Cookie。
管理员在 [Google Cloud 控制台](https://console.cloud.google.com/) 创建/选择项目，启用 Gmail API，
配置 OAuth 同意屏幕，并创建 **桌面应用** OAuth 客户端。测试模式下把实际使用的邮箱加入测试用户。
下载客户端 JSON 到自己的私有目录，目录权限 `0700`、文件 `0600`。不要把文件内容粘贴到聊天或提交到 Git。
只申请 `https://www.googleapis.com/auth/gmail.readonly`。

Google 外部应用测试模式、受限 Gmail scope 和正式发布的审核/令牌生命周期需要管理员根据实际部署确认。
这是本机桌面授权入口，不能直接当作多人共享网站的 OAuth 回调。
参考：[桌面 OAuth](https://developers.google.com/identity/protocols/oauth2/native-app)、
[Gmail 同步](https://developers.google.com/workspace/gmail/api/guides/sync)。

## 准备运行环境

使用 Python 3.12+，安装项目依赖并构建前端。可以选择下文的本人邮箱专用环境，也可以复用已初始化的
完整持久 profile 和操作者本人的真实员工账号，参见 [持久内测说明](web-internal-pilot.md)。
不得用验收的合成政策/员工代替真实业务配置。
本功能本身不需要 DeepSeek 密钥，不调用大模型，不消耗模型额度。

## 本人邮箱专用持久环境

这个入口不要求评分、货币或交接政策，不启动其他业务 worker。所有邮箱存入独立持久 PostgreSQL，
只有当前登录员工可以读取。数据库和 Web 仅监听本机回环地址。

```sh
python scripts/run_mailbox.py init --profile PRIVATE_DIRECTORY/config.json
```

`init` 是显式初始化与迁移操作；中断后可对同一配置重试，只接续归属验证通过的资源，不删除旧卷。
`PROFILE_FILE` 在下面均指 `config.json` 文件。先完成后面的 Google 只读授权，再运行：

```sh
python scripts/mailbox_account.py --profile PROFILE_FILE \
  --username LOGIN_EMAIL --email MAILBOX_EMAIL \
  --credentials-file PRIVATE_DIRECTORY/gmail-credentials.json
```

第一次创建只读登录账号，显示姓名使用登录邮箱。密码在真实终端输入两次，至少15字符，不接受密码参数。
登录账号与邮箱绑定分离：同一登录邮箱可以追加多个 `MAILBOX_EMAIL`，追加时须输入现有密码，每个邮箱分别授权、
分别保存凭证文件。不要把绑定邮箱改成另一名员工来规避归属保护。旧短用户名登录继续兼容。

```sh
python scripts/run_mailbox.py install --profile PROFILE_FILE --web-build apps/web/dist
python scripts/run_mailbox.py status --profile PROFILE_FILE
```

`install` 安装 macOS 登录后运行的 LaunchAgent，参数只引用私有文件路径。状态返回的 `origin` 加
`/inbox/mailbox` 即为页面入口；登录后按“邮箱账号”切换，选择“收件箱”“已发送”或“全部邮件”。
新增绑定会由后台发现。`start` 可仅启动当前本机后台，不安装 LaunchAgent。

```sh
python scripts/run_mailbox.py stop --profile PROFILE_FILE
python scripts/run_mailbox.py migrate --profile PROFILE_FILE
```

`stop` 停止进程与数据库但保留所有数据，停止后更新代码再显式 `migrate`，随后 `install` 或 `start`。
应用启动只检查 schema，不自动迁移。持久配置权限0700/0600；数据卷不能手动删除。
电脑关机、休眠、外置工作目录未挂载或 Docker 停止时无法实时同步，恢复条件后后台继续从检查点补齐。
“后台已运行”不代表历史已补齐，以页面同步阶段和最近完成时间为准。

## 复用完整持久环境

先停应用，备份已有 profile，再显式迁移到当前单一 head `0069`：

```sh
python scripts/run_web_pilot.py migrate --profile PROFILE
```

应用启动不会自动迁移。下面 `PROFILE`、`EMPLOYEE_ID`、`EMAIL` 和文件路径是需替换的占位符，
不得照抄。`credentials-file` 是新建授权文件的位置，不是手动输入令牌。

```sh
python -m apps.email_feedback_worker.mailbox authorize \
  --client-file PRIVATE_DIRECTORY/google-desktop-client.json \
  --credentials-file PRIVATE_DIRECTORY/gmail-credentials.json
```

程序打开 Google 页面，由账号本人选择邮箱并同意只读访问。本地回调使用随机端口、随机 state 和 PKCE。
授权材料只由 connector 在本机私有文件保存/刷新；程序不会打印授权码或令牌。

```sh
python -m apps.email_feedback_worker.mailbox sync \
  --profile PROFILE/config.json --employee-id EMPLOYEE_ID --email EMAIL \
  --credentials-file PRIVATE_DIRECTORY/gmail-credentials.json --watch
```

首次自动逐页补齐历史，以后默认每 60 秒检查增量；`--interval` 可设为 15–3600 秒。
不加 `--watch` 只跑到本轮追平。关闭此进程/电脑后停止同步，再运行同一命令从持久检查点恢复。
Web 的“立即同步”会唤醒正常轮询，但不绕过 Gmail 限流等待。

旧受限 pilot 禁止外网；独立邮箱同步在自己的进程运行，不放宽旧 pilot 的网络边界。
API 和 Web 仍按原入口运行。邮箱同步只需数据库，不启动研究、Campaign、发信或报价流程。

## 故障与恢复

- 授权失效或账号不匹配：停在原检查点，重新运行授权向导，再启动同步；不得换 owner 绕过权限。
- Google 限流/网络故障：显示固定原因，保留检查点；`--watch` 按等待时间重试。
- history 过期：重新全量扫描，历史未补齐状态可见；全部扫描成功后才清理 Gmail 中已不存在的镜像。
- 超大或不能解析的邮件：该页不推进，显示读取未完成；不跳过邮件后谎报全部完成。
- 员工停用：读取和同步都拒绝。已有邮箱 owner 不通过重新注册转移。
- 日志与工具账本不保存邮件正文、邮箱令牌和游标。原邮件不会发给 DeepSeek。

迁移 `0067` 在有邮箱数据时拒绝破坏性 downgrade。停止应用、备份与恢复沿用原 profile 工具，
但 OAuth 私有目录独立于 profile，需要按本机凭证管理规则自行保管。
