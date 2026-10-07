# sourcing.verify_supplier_candidate — prompt 资产

（骨架阶段占位。实现时在此撰写正式 prompt，要点如下。）

## 必须体现的取向

1. 规格逐项比对，每项给 exact / different / unknown 三值之一。
   **unknown 不许猜成 exact**——未知意味着要问供应商或问客户。
2. 诱导价特征识别：远低于市场且无数量档、模糊区间（$1–$10）、
   计价单位不明、币种不明。给出「建议拒绝 + 理由」，不直接拒绝。
3. match_summary 用销售能直接转述给客户的语言：
   「材质尺寸一致，表面处理是拉丝不是抛光，可替代但外观有差异」。
   禁止输出相似度百分比。
4. 所有价格结论标注 indicative——本技能永远接触不到 quoted 价格。
