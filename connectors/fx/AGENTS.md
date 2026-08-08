# connectors/fx/ —— 汇率（Phase 1）

## 能力范围

拉取汇率并产出 FxSnapshot（base、quote、rate、observed_at、source）。

## 铁律

- rate 是 Decimal，任何环节不得转 float（硬边界 2）
- 只产快照不做换算——换算在 shared.schemas.money.convert，绑定快照 ID
- 报价用的快照被 cost_sheet 引用后不可删

## 密钥归属

`FX_API_KEY_REF`（免费源可为空）。
