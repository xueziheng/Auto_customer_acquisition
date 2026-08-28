# domains/approvals/ —— 审批域

## 职责

审批包的生命周期，以及**「哪些变更必须人工审批」的中央注册表**。

## 设计原则：审批人要能不翻页面就做决定

审批被拖延的最大原因不是人懒，是**决定所需的信息散在五个页面里**。所以 `ApprovalPackage` 必须自带：

```text
提议了什么变更          谁（哪个 Run / 哪个员工）提议的
为什么提议              依据的证据
影响范围                批准会发生什么 / 否决会发生什么
```

打开通知 → 看完 → 点批准或否决，一分钟内完成。做不到就是审批包组装的缺陷。

## 状态机

```text
pending ──→ approved ──→ applied
    │            └─────→ apply_failed（应用失败，需人工介入）
    ├──→ rejected
    └──→ expired
```

**过期是必要状态**：一周前批准的变更今天才应用，它作用的对象可能已经变了（客户改了数量、汇率变了、供应商改价了）。过期的审批不能应用，要重新走流程。默认有效期按变更类型定（价格类短、配置类长）。

## MUST_APPROVE 注册表

「什么必须人工过目」分散定义会漂移——某个新工具忘了接审批就成了漏洞。所以集中在本域注册，`tool_gateway` 每次执行前查：

```text
来自 quotations.FORBIDDEN_AUTO_COMMITMENTS 的全部条目
已批准 Campaign 边界之外的任何对外发送
金额超过阈值的任何支出
从抑制名单移除条目
Campaign 边界修改
发件身份配置修改
指令提案确认（by boss）
INDICATIVE 风险接受
国家政策包变更（首次配置与后续修订均需独立审批）
```

条目用字符串引用其他域的枚举值（不跨域 import）。

## 自批禁止

```text
报价审批人 ≠ 报价起草人，且 ≠ 机会负责人
承诺解除人 ≠ 承诺人
国家政策包审批人 ≠ 国家政策包提交人，且 ≠ 该变更 owner
```

不是不信任员工，是消除「赶指标时给自己开绿灯」的结构性诱惑。通用自批规则由本域强制；新版报价的当前归属与用途规则由注入的报价guard判断，本域必须持该租约完成决定事务。

## 幂等应用

**批准两次不能应用两次。** `ApprovalDecided` 事件可能重复投递；变更集应用必须带幂等键。双重应用一个报价变更 = 给客户发两封邮件。

## 依赖白名单

```text
允许   shared.events, shared.schemas, shared.errors
禁止   任何其他 domains/*、任何外部 SDK
```

## 发布 / 订阅

发布：`ApprovalDecided`
订阅：无（审批请求由各域/工作流通过服务接口提交）

## 禁止事项

- 不允许审批记录被删除或修改（只增）
- 不允许过期审批被应用
- 不允许自批
- 不允许「默认批准」的超时策略——超时只能过期，不能变成同意

## Phase 1 范围

审批包、状态机、MUST_APPROVE 注册表、自批禁止、过期机制。

不做：多级审批链（Phase 2，Phase 1 一级审批够用）、按金额自动分级路由。

## Phase 2 报价namespace隔离

新路径必须同时满足精确`quote-approval-v1`载荷及`quote:{quote_id}:{hash}:{type}`引用。
任一疑似标记出现均严格验证，不得因半标记、空白或大小写伪装降级legacy。仅approval_type=quote_send
不是新路径；旧邮件/其他审批保持原默认期限、pending幂等与读取权限。

新请求hash绑定全部原请求及原expires_at_limit，跨状态同键只返回原ID，异请求拒绝；重试时钟
不延长期限。namespace/hash/limit及原请求不可变，首次决定后决定人/时间/备注也不可改写。
新版get/list/decide缺guard必须失败关闭。read/list在当前lease内比较可信reader.role与current_role；
不一致明确拒绝，不能作为不可见候选吞掉。own read不授予decide，内部read_fact不注册HTTP。
decide顺序为员工/机会guard→审批行UPDATE锁→新时钟→决定及outbox提交→退出guard。

报价receipt由报价域持久；APPLIED状态和ApprovalDecided事件不能反向证明报价成功。
恢复仅按真实receipt及不含应用状态的决定hash逐包补记，允许过期后的历史记账但不重新应用商业承诺。
只有实际APPROVED包可标固定apply_failed；超时/未知存储结果保留可恢复，不把异常原文写入审批。

新版报价submit已由ApprovalQuoteSubject严格验证员工身份，原样保留合法持久短编号；只有
legacy继续执行emp_前缀校验。decide在现短读/namespace识别之后，真报价用fact_identity，
legacy保持原前缀规则；非法新版员工固定ValidationError，不增查询、不改变当前guard/锁序/
自批禁令。该短读意味着非法legacy员工可能先遇缺失或损坏审批，不承诺原错误优先级。
