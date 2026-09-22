# assistant —— 员工私有 Agent Session / Agent Turn

遵守根九条硬边界。只管理内部会话归属、轮次状态、来源与安全结果引用，不管理客户会话、
Validated Need、研究提案、审批或业务 Run 的规则。依赖仅 shared 与本域。

会话只属于发起员工，老板身份不赋予查看其他员工聊天的权限。读、写、历史投影使用当前身份；
输入凭证检测先于持久化。对象权限由注入的公开授权端口核验，不直读别的域表。
queued/running 占执行槽；已交付澄清/提案释放槽。unknown/failed 可显式新建 attempt，不能覆盖旧轮。
接受输入与执行意图原子保存；外部调用由 scheduler/Gateway 负责。业务确认不能由聊天文本代替。
