"""Codex Agent 连接器的静态能力；不代表任何企业已经获准调用。"""

CONNECTOR_ID = "codex_agent"
CAPABILITIES = ("model.generate", "model.connection.test", "task_context.read")
MAX_MODEL_ROUNDS = 4
CREDENTIAL_LOCATION = "trusted_parent_connector"
SUPPORTED_PROTOCOLS = ("responses", "chat_completions")
