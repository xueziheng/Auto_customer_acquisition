# ADR 0008：JSON 金额使用十进制字符串

- 状态：已接受
- 日期：2026-08-09

## 背景

金额必须保持 `Decimal` 精度。JSON number 在客户端、解析器或 Pydantic 边界可能先变为
二进制浮点数，造成不可恢复的精度截断。

理由：金额是确定性计算输入；一旦在共享边界截断，后续业务代码无法恢复原始数值。

## 决策

`Money.amount` 与 `FxRate.rate` 的 Python 输入仅接受有限 `Decimal`；JSON 输入仅接受有限
十进制字符串。对应 OpenAPI schema 声明为 `string`，Pydantic 正常序列化继续使用字符串。

## 后果

调用 API 的客户端必须以字符串发送金额和汇率；精度可端到端保持，float/int/bool/非有限值
会在共享契约边界被拒绝。该规则不包含任何贸易业务判断。

## 备选方案

- 接受 JSON number：会在进入确定性金额代码前失去精度，拒绝。
- 仅在 CRM router 对特定字段拦截：会遗漏其他 API、事件和内部 TypeAdapter 边界，拒绝。

## 重新审视

当外部协议可提供无损十进制数值类型、并能在所有客户端与 Pydantic 边界证明不经二进制
浮点转换时重新审视；在此之前保持字符串 wire contract。
