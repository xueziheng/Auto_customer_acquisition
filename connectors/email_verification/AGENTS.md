# connectors/email_verification/ —— 邮箱可达性验证（Phase 1）

## 能力范围

单个邮箱地址的可达性验证（硬边界 6 的执行者）。返回四值之一：valid / invalid / risky / unknown。**risky 与 unknown 按不可用处理**（prospecting 域规则）。

## 密钥归属

`EMAIL_VERIFY_API_KEY_REF`。

## 成本

按次计费。每次调用上报成本（tool_call 的成本字段）——「每个已验证联系人花多少」是评估数据源的依据。

## 接口

```text
verify(email) -> {result, provider_raw, cost_note}
```

## 注意

验证服务自己也有误报。同一地址短期内不重复验证（结果缓存 30 天），但发送前的抑制名单检查不可省——验证说可达 ≠ 人家没退订过。
