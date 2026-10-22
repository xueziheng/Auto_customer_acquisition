# ADR 0086：平台获客来源额度状态

2026-10-11，状态：已采纳。

用户要求在平台来源管理界面看到密钥对应的余额不足或额度用完。沿用平台授权与凭证管理，
增加只读额度快照，不建设订阅、积分钱包、计费或自动付费功能。

## 决策

- `provider.hunter.usage` 是独立 LOW/FREE Gateway 插件，只访问固定 `/account`，不查询联系人。
  遵循 tenant → permission → idempotency → rate_limit 和 EXECUTING 先持久提交的要求。
- 模型、HTTP 入参和返回值均不包含密钥。可信 connector 惰性读取精确凭证版本。
  账户身份、原始 JSON 和异常文本在边界内丢弃，仅提取白名单计数、重置日期、固定错误类型。
- `requests.available` 是总额度，`remaining` 是余额；缺失余额保持未知，不拿总量冒充余额。
  所有计数转换及计算采用 Decimal；统一 credits 池存在时以该池判断总体状态。
- 状态区分正常、低额（≤总量 10%）、部分额度用完、全部用完、需付费、凭证无效、短时限流、
  暂不可用和未知。HTTP 429 不认定为月度额度用完；网络故障不认定为密钥错误。
- 额度快照存在独立私有文件，绑定控制租户、来源、凭证版本和真实 tool_call_id；不改变凭证
  envelope，不改账户/邮箱样本测试状态。轮换、删除和旧响应不得恢复旧额度。
- 页面打开和保持可见时每 5 分钟检查一次已支持的配置，提供单项检查按钮。服务端缓存至少
  60 秒并尊重 Retry-After，相同配置同一分钟由持久幂等合并。没有收费探测或自动重试循环。
- 先接已配置的 Hunter；其余来源明确显示“额度查询待接入”，不能声称其供应商没有接口。
  公开来源显示“不适用”。超过 10 分钟的快照标注已过期。

此检查只证明检查时的账户额度事实，不证明邮箱样本、可达性、生产业务能力已启用；
不修改原未知样本 ledger，不代替获客业务的 quota/compliance/permission 门禁。

接口依据：[Hunter Account Information](https://hunter.io/api-documentation/v2#account)，
2026-10-11 核实该调用免费，requests 返回 used/available/remaining。
