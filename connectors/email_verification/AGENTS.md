# connectors/email_verification/ —— 邮箱可达性验证（Phase 1）

## 能力范围

单个邮箱地址的可达性验证（硬边界 6 的执行者）。返回四值之一：verified /
invalid / risky / unverified。**risky 与 unverified 按不可用处理**（prospecting 域规则）。

## 密钥归属

Phase 1 只允许 Hunter，实现只解析 `HUNTER_API_KEY_REF`。密钥不得进入参数、DTO、
异常、日志、数据库或模型上下文；generic contract 不注册独立 manifest。

## 成本

按次计费，只上报固定 cost label，不保存当前套餐价格或 Provider 任意文本。

## 接口

`verify(email) -> EmailVerificationResult`。公共结果不含 raw JSON、Provider score 或
`provider_raw`；检查时间必须是 UTC。

## 注意

验证服务自己也有误报。verified / invalid / risky / unverified 四种结果全部缓存 30 天；
risky 与 unverified 在缓存期内仍不可进入发送序列。发送前的抑制名单检查不可省——
验证说可达 ≠ 人家没退订过。
