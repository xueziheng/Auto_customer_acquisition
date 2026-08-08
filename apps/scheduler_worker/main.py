"""scheduler_worker 入口。职责见 AGENTS.md。

实现要求：
- 单副本约束：启动时获取咨询锁（pg_advisory_lock），拿不到就退出
  ——静默双跑比拒绝启动危险得多
- 每类扫描独立 try/except：一类任务坏了不拖垮整个循环
- 循环间隔可配，默认 10s；每轮记录推进数（监控「停止推进」告警的
  数据源）
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError


if __name__ == "__main__":
    main()
