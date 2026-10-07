"""会话 API 的结构契约来自域 DTO。"""

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from apps.api.routers.assistant import router
from domains.assistant.schemas import TurnInput


def test_browser_cannot_supply_authority():
    with pytest.raises(ValidationError):
        TurnInput.model_validate(
            {"text": "查机会", "idempotency_key": "one", "role": "boss"}
        )


def test_openapi_has_persistent_turn_and_no_secret_configuration():
    app = FastAPI()
    app.include_router(router)
    schema = app.openapi()
    assert schema["paths"]["/agent/sessions/{session_id}/turns"]["post"]["responses"][
        "202"
    ]
    assert "TurnView" in schema["components"]["schemas"]
    assert "secret_ref" not in str(schema)
