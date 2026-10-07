# 客户端能力边界：Web 与未来桌面

日期：2026-09-06。依据[Web-first设计第六节](../superpowers/specs/2026-09-05-web-first-completion-design.md)。
本文件描述现有服务契约与未来适配责任，不新增协议实现或 Agent 规则入口；全局约束仍由
[AGENTS.md](../../AGENTS.md)定义。Web 是主应用，桌面只扩展用户本地能力。

## 当前真实接口与未来责任

| 能力 | 拥有者与输入 | 权限与输出 | 失败状态、当前范围 |
| --- | --- | --- | --- |
| 文件选择/目录监听 | 未来桌面拥有用户授权的文件选择、目录范围及监听生命周期；只能产生待处理输入。受信服务拥有上传处理、`RawArtifactStore.put/get/get_meta`、`BoundedRawArtifactStore.get_bounded` 与独立 `GeneratedArtifactStore` | 每次经API重新验证当前员工、tenant、对象用途；`POST /work-uploads`接收文件bytes、artifact_kind/source_kind、occurred_at/customer_timezone与MIME，员工/UserId由当前服务端身份映射，返回WorkUploadView；本人列表及`/{upload_id}/artifact`按当前员工读取，关联Artifact安全metadata/Provenance。Raw与Generated不得混用；读取重算大小与SHA256；路径、对象key、原始内容不进入模型上下文 | 当前手动Work Uploads、报价证据预览/定位、客户文件生成/下载与Inbox/入站原件读取是各自用途接口；work_uploads服务未装配或Mac报价组未配置返回503。无通用任意文件夹同步API。目录监听/Obsidian/导入守护进程未实现；未授权、跨tenant、超限、hash不符拒绝，不自动提升为已验证需求 |
| 本地凭证存储 | 未来Keychain适配器属于受信运行端；复用 `ObjectStoreSecretResolver.resolve(secret_ref)` 形状及已有 `EnvironmentSecretResolver`、`ControlledConfig.resolve` 的最窄引用解析 | 只解析受信配置指定的具名引用；解析值仅在Connector/运行配置内存使用。API/模型/Agent只见允许的安全状态，不得取得密钥、Cookie或枚举凭证。设备声明不能允许解析任意引用 | 引用无效、未配置、owner配置不符固定失败。当前运行环境解析器已存在，Keychain无实现；不把本机配置文件路径当跨客户端API |
| 原生通知 | `NotificationChannel` 实现适配器；`NotificationRouter` 的 `RoutingPolicy` 选择已注册渠道，Notification Worker消费持久job，dedup持久化 | 输入已确定的tenant、收件员工、dedup key和安全通知；设备授权只允许显示系统通知，点开仍调用当前actor授权API。输出投递结果，不授予业务对象权限 | 失败保留可重试/拒绝状态，不能“已排队”冒称投递成功。当前受控worker站内enabled、邮件disabled；无macOS原生适配器。未来OS通知权限被拒须显式不可用 |
| 本地浏览器/人工接管 | 现有 `BrowserJob`、`BrowserJobRepository`、`PublicPageReader`/`BrowserSnapshot` 是Gateway已授权公开页面任务的组件边界；未来桌面另负责明确租户、账号、Run及用户接管会话生命周期 | Gateway签发的job含tenant/run/tool_call_id/url；返回canonical URL、观察时间、hash、Artifact引用。`gateway_authorized` 是受信内部字段，绝非接收客户端true即可授权。未来账号映射/本地登录态只能由受信隔离适配器持有，撤权销毁相应context | 当前无生产BrowserJobRepository/任务来源，Browser Worker disabled；`connectors/playwright/`仅预留模块边界，不存在可用登录态/人工接管进程。公开研究页由现有Gateway端口执行。未来必须独立实现并验收超时/取消/撤权/隔离，不能复用跨租户context或把Cookie交给模型 |
| 客户端能力发现 | 后端持有真实组合状态；`GET /health/capabilities` 返回 `RuntimeCapability(name,status,reason)`，Web读取监督状态与业务端口当前结果 | 当前status精确为 `enabled/disabled/configuration_error`，reason为受限枚举；它不是设备注册或授权端点。未来客户端只能报告设备事实；服务端结合会话、当前员工/tenant、业务策略、adapter/readiness决定可执行入口 | `enabled`不表示审批通过/预算足够/真实Provider已验证。状态过期或请求失败显示unknown/unavailable语义，不推断成功。当前无device capability注册API，不新增available常量或IPC；未实现能力不展示可执行假入口 |

## 可核对源码

- 文件：[Store协议](../../artifact_store/store.py)、[Store实现](../../artifact_store/service_impl.py)、
  [员工手动上传API](../../apps/api/routers/work_uploads.py)、[报价用途API](../../apps/api/routers/quotation_actions.py)、[Inbox证据API](../../apps/api/routers/inbox.py)、
  [入站原件API](../../apps/api/routers/email_inbound.py)。客户可见报价仍要求quoted或人工接受indicative风险并留痕，及独立正式审批。
- 凭证：[对象存储最窄解析Protocol](../../connectors/object_store/s3.py)、
  [环境解析器](../../infra/secrets.py)、[owned配置loader](../../infra/controlled/config.py)。
- 通知：[NotificationChannel](../../notification_gateway/models.py)、[路由与策略](../../notification_gateway/router.py)、
  [站内渠道](../../notification_gateway/channels/in_app.py)、[受控worker](../../apps/notification_worker/controlled.py)。
- 浏览器：[BrowserJob/Repository/Reader](../../apps/browser_worker/main.py)、
  [现有Connector范围](../../connectors/playwright/AGENTS.md)。这些Protocol存在不代表任务源已实现。
- 能力/权限：[RuntimeCapability](../../shared/schemas/runtime_capabilities.py)、[健康API](../../apps/api/routers/health.py)、
  [当前身份解析](../../apps/api/identity.py)、[Inbox actor范围](../../domains/conversations/service_impl.py)。

## 共享部署前的独立门禁

当前loopback的开发角色切换不是真实登录。多人使用前须由后端验证认证主体、建立可撤销会话、
映射当前员工；停用/退出/过期/CSRF/来源约束有真实回归，dev入口在共享部署禁用。
同源TLS、迁移、备份恢复、服务重启、scheduler单副本、告警和Provider readiness均须另行验收。
桌面不能绕过这些门禁，不能自报tenant/role/owner、文件路径或authenticated来取得权限。
本轮没有Tauri工程、桌面占位目录、假IPC、桌面连接状态或服务器部署。
