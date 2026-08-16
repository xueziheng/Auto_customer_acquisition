# 回复评估集（先建评估集，再写 prompt —— HANDBOOK 切片 6）

目录按 `domains/conversations/models.py` 的 14 类 `ReplyCategory` 组织，每类至少 10 个样本，
退订与自动回复各 20 个（误判代价最高）。样本只增不改——改样本等于移动球门。

## 用例格式

```text
<category>/<case_name>/
├── input.json        # 入站消息原文（message_id / subject / body / 可选 from_name）
├── expected.json     # 期望行为
└── note.md           # 样本来源与为什么值得测（可选，tricky 用例必写）
```

## expected.json 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `category` | 是 | `ReplyCategory` 枚举值，必须与所在目录一致 |
| `must_intercept` | 是 | 必须触发的确定性动作（`REPLY_ACTIONS[category]` 的子集） |
| `must_not_intercept` | 否 | 绝不允许触发的动作（与类别动作映射不相交；如自动回复不得停序列） |
| `suppress_scope` | 退订类必填 | `contact`（个人）或 `account`（公司级退订，不得降级为个人） |
| `extract` | 否 | 必须提取的需求字段：`{field, value, quote}`，`quote` 必须逐字出现在输入消息中（provenance 指向原消息） |

## 目录说明

- `high_intent/` 是早期占位目录（非 14 类枚举值），保留为空，不作为样本类别。
- 类别动作映射见 `domains/conversations/models.py::REPLY_ACTIONS`——模型只判断类别，
  做什么由确定性代码决定；本评估集锁定的是「类别 + 期望动作」的行为对。
