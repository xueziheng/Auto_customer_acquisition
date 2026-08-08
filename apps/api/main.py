"""API 进程入口。

职责（实现时）：
1. FastAPI 实例与生命周期
2. 依赖注入装配：Repository 实现、EventBus、ToolRegistry、
   ConnectorRegistry、各域服务实例
3. 中间件：租户上下文（从认证解析 tenant_id 并贯穿请求）、
   审计、错误转换（域错误 → 结构化 HTTP 响应，
   is_retryable → Retry-After 头）
4. 挂载 routers/ 下全部 router

启动前置校验（失败即拒绝启动，不要带病上线）：
- Playbook 已配置（organization 域）
- ConnectorRegistry 密钥齐备
- 数据库迁移版本匹配
"""

from __future__ import annotations


def create_app():  # -> FastAPI
    """应用工厂。测试用它构造带假实现的实例。"""
    raise NotImplementedError


def main() -> None:
    raise NotImplementedError


if __name__ == "__main__":
    main()
