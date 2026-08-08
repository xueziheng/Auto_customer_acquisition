"""老板指令域的事件契约声明。"""

from __future__ import annotations

from shared.events.catalog import DirectiveActivated

PUBLISHES = (DirectiveActivated,)
"""``DirectiveActivated`` —— 指令生效广播。

订阅方各自应用与自己相关的部分：
- ``domains/employees``    市场分配 → Territory Matrix
- ``domains/outreach``     触达边界、暂停市场 → Campaign 校验与暂停
- ``agent_runtime``        探索配比 → 任务生成策略

本域**不直接改其他域的数据**——那会需要跨域 import。事件广播 +
各域自取，是「一条指令改变全系统行为」与「域间零依赖」的兼容方式。
"""

SUBSCRIBES = ()
