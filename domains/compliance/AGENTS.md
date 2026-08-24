# domains/compliance/ —— 国家政策合规域

## 职责

保存 tenant-scoped 的国家政策候选版本、逐字段 Provenance 与 append-only 激活事实，并对
公开研究、联系人补全和冷 B2B 邮件返回结构化政策判断。它是独立业务域，不属于 Company
Playbook，也不属于 Tool Gateway。

## 核心事实

```text
CountryPolicyVersion（不可变候选）
        ↓ 不同员工独立审批
CountryPolicyActivation（只增不改的生效事实）
        ↓
CountryPolicyDecision（结构化、fail-closed 读取）
```

- 系统没有法律默认值；未激活的精确国家键固定为未配置且不允许。
- 国家键只做 NFKC、trim、空白折叠和 casefold；禁止别名表、ISO 推断或地理库猜测。
- 每个决策字段都有独立、人工确认的 Provenance；请求体不得提交提取/确认身份或时间。
- 候选版本、字段 Provenance 与 activation 只增不改；无 update、delete、bootstrap、force、
  direct activate 或 apply-now 公共路径。

## Phase 1 权限矩阵

```text
boss / TENANT    → COUNTRY_POLICY_READ, COUNTRY_POLICY_PROPOSE
system / SYSTEM  → COUNTRY_POLICY_DECIDE, COUNTRY_POLICY_CHANGE_SNAPSHOT_READ,
                   COUNTRY_POLICY_ACTIVATE
```

actor 绑定的租户、authorizer 配置租户与方法 tenant 必须三者一致。系统 actor 只能由组合根
显式构造，请求代码不提供便利构造器。

## 依赖白名单

```text
允许   shared.events, shared.schemas, shared.errors
禁止   任何其他 domains/*、任何外部 SDK
```

跨边界只暴露 `schemas.py` DTO 与 `service.py` Protocol；`models.py`、`repository.py` 是域内
实现。工作流通过窄 `CountryPolicyApprovalFact` 传入审批事实，合规域不得导入审批域。

## 禁止事项

- 不得预置、推断、搜索或推荐任何真实国家的法律结论。
- 不得把未知国家或读取故障伪装成允许。
- 不得保存模型概率、confidence 数值、网页正文、凭证或 PII。
- 不得让提交人批准自己的候选；首次配置也没有自批或系统默认批准例外。
