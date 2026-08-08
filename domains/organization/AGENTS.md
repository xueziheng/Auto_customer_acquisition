# domains/organization/ —— 组织域（浅）

## 职责

租户、公司档案、**Company Playbook**。

Playbook 是老板必须提供的最小边界集合——设计稿第五节明确：不能指望老板认真填几十项 ICP 画像，所以系统只要这几样，其余由 Agent 自动形成探索章程：

```text
公司是什么类型            公司不能做什么产品
主要能从哪里找货          不希望做哪些国家
大概的交易金额底线        有哪些员工
有哪些业务邮箱            每月预算
哪些行为必须人工批准
```

## Playbook 的消费方

几乎所有域都读它：打分门槛读 `minimum_deal_value` 和 `excluded_categories`，Campaign 校验读 `excluded_countries`，tool_gateway 的 Playbook stage 读全部。**它是配置，不是代码**——改 Playbook 不需要发版。

## 租户

Phase 1 单租户，但租户实体和 `tenant_id` 贯穿全库（ADR 0001）。本域持有租户的生命周期；其他域只消费 `TenantId`。

## 依赖白名单

```text
允许   shared.*        禁止   其他 domains/*、外部 SDK
```

## 事件

发布：无（Playbook 变更走审批，生效后各域下次读取自然拿到新值）
订阅：无

## Phase 1 范围

租户实体、Playbook 模型与读写服务。不做：多租户开通流程、订阅绑定（Phase 3）。
