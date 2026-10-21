# QQ 地址语法离线兼容 v5

本补丁基于 v4 独立副本，仅使用已公开脱敏的 Q02 双地址头。没有读取 private 原始头、
真实正文或附件，没有连接服务器、对象存储、邮箱或模型，也没有生产装配、迁移、开关或上线。
v4 的“精确地址语法未知”缺口由 2026-10-16 单独的只读证据任务补足，本补丁不重复采集证据。

## 固定兼容范围

默认 `reply_headers` 与无 trial 旧流程仍拒绝这些 defects；共享 Gmail MIME 解析器没有放宽。
仅原 QQ reader 当前通过 `QQTrialGate.check` 的 trial 提供精确 scope，标准解析和原字节解析
都必须得出 scope.recipient → scope.sender 的唯一双方。别名、当前身份、租户、attempt、
owner、权限、UID 水位、截止时间和不可退款预算仍走原 Gateway、scope 和账本。

仅对含 defects 的 From/To 允许以下完整形态：

```text
From: "=?utf-8?B?<strict-base64>?=" <one-numeric-qq-mailbox>
```

固定小写 `utf-8`、大写 `B`、单 encoded-word、双引号、冒号后一个空格、结束引号后一个空格，
再接一个尖括号邮箱。Base64 必须严格且规范（包括 padding bits）；解码仅 1–32 个 ASCII
字母/数字。32 是显示字段技术上限。拒绝其他 charset、编码、Unicode/控制字符、空白显示名、
空名、多词、折行、转义、注释、命名组、多地址、重复字段和不匹配双方。

必须由标准库返回恰好两个 `InvalidHeaderDefect`，类型、顺序、数量和消息均一致：

1. `encoded word inside quoted string`
2. `missing trailing whitespace after encoded-word`

新增、缺失或不同 defect 都失败关闭。严格解析的 mailbox 和 decoded display 必须分别与
标准解析器的单一 mailbox 和 display 一致。显示名只供解析一致性核对，不返回给客户/租户
识别逻辑，更不构成发送者认证。无缺陷的原正常地址路径继续使用原规则与精确双方约束。

## 两次检查与原件

头阶段和一次允许的 BODY 后都调用同一函数。trial 的 `ReplyHeaders` 带两个 From/To
原始字段及其 defect 元组，禁止 repr 暴露；后验必须逐字节相同，包括字段名大小写、空白、
显示名和折行。即使 mailbox 一样，等长替换显示名、删除 quotes 修正格式或产生新 defect
也不能通过。原件绝不重写，归档仍是原 MIME bytes，原 archiver 仍复核实际对象与 SHA256。

v4 BODYSTRUCTURE/完整 FETCH envelope、UID/size、Bcc、附件、根 alternative 的 plain→HTML、
子部分计数/编码/charset/octet/line/boundary 门禁保持。精确关联但无法安全解释的候选仍
持久停止为 `reply_headers_unsafe`；明确无关候选保持跳过。标准库在地址读取时抛异常也不能
作为“明确无关”证据。完整 BODY 的超限 literal 可在取得原件前被 wire 限制拒绝，driver
仍持久停止，不能把这类失败假称为已完成原头复核。

## 缺陷与审计

不增加表、字段或迁移；在原 `qq_reply_trial_events.kind` 中新增两个固定技术观察事件：

- `address_from_quoted_utf8_b_v1`
- `address_to_quoted_utf8_b_v1`

每个事件表示该字段已在两个阶段按上述 v1 规则复核，且具有上述完整的两个 defects。
记录前重新验证当前 scope、活动 scan token 和已预留的一次 body，行锁内去重；不记录邮箱、
显示名、异常原文或原始字段。未知字段/组合不能入账。

该观察事件不是成功收信声明：若后续归档或整页提交失败，可留下观察事件但没有收件记录。
正式收件仍必须有原 `page_committed`、trial.message_id、收据 raw_artifact_id/raw_hash/raw_size
及相同租户原件。只有这条已提交链才能把固定 defect 事件关联到不可变原信。原件中两项缺陷
仍可用原标准解析器复现，不能通过改写原件来消除缺陷。失败不提交游标、消息或收据；已用预算
不退还，trial 停止后不自动重复读取。

## 夹具和验收限制

`tests/fixtures/qq_trial/q02-address-syntax-redacted.headers` 是 107 字节脱敏双头，SHA256：
`34525bb52270f13d3060e4203a94fcaba0643b96549578bc8e44d35372151430`。
Git 以 binary 属性保存 CRLF 字节，禁止编辑或自动换行规范化。测试将其中脱敏邮箱替换为
原 trial 测试常量，以合成其他头和正文；这些测试常量及合成内容不代表实际 Q02 邮件。

测试覆盖原解析器、socketpair 假 IMAP、合成 BODYSTRUCTURE、原 Gateway/独立 PostgreSQL UoW，
验证一次归档、原件 hash、固定缺陷审计、无授权阻断、失败不推进游标/不退预算/不重读。
最终命令、原始日志和实际计数见独立交付报告；36 项既有 `dependency_missing` setup errors
仍未验收，本轮不解决这些环境问题。

地址语法离线兼容通过不证明真实 QQ 完整收信：真实协议结构与行数约定、alias 自动采集、
可信启动装配、完整运行时、真实模型分类和上线均未验证。生产、邮箱或旧信 replay 均需后续
明确授权，本补丁不提供启用开关或自动 collector。
