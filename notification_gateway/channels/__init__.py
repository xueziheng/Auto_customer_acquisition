"""渠道适配器。

Phase 1: in_app.py、email.py
Phase 2: wecom.py（企业微信/OpenClaw 插件，经 connectors/wecom）
Phase 3: macos.py（桌面端系统通知）

每个渠道实现 models.NotificationChannel 并在 worker 启动时注册。
邮件渠道必须使用 TRANSACTIONAL 角色的发件身份——内部通知
不占冷发额度，也不受冷发信誉波及。
"""
