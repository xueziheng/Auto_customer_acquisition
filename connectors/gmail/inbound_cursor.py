"""独立gic1状态；opaque不等于加密，不得持久化到日志。"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field

from shared.schemas.email_inbound import (
    CURSOR_BYTES,
    InboundDTO,
    InboundError,
    InboundRoute,
)


class InboundCursor(InboundDTO):
    route: InboundRoute
    phase: Literal["initial", "bootstrap", "history"]
    bootstrap_started_at: datetime
    after_epoch: int = Field(strict=True, gt=0)
    history_start: str | None = Field(default=None, repr=False, max_length=200)
    page_token: str | None = Field(default=None, repr=False, max_length=200)
    pending: tuple[str, ...] = Field(default=(), repr=False, max_length=100)
    resume_history: str | None = Field(default=None, repr=False, max_length=200)
    loaded: bool = False


def encode_cursor(cursor: InboundCursor) -> str:
    """状态不含bytes/header；长度在编码前后均有界。"""
    raw = cursor.model_dump_json().encode("utf-8")
    value = "gic1." + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if len(value) > CURSOR_BYTES:
        raise InboundError()
    return value


def decode_cursor(value: str, route: InboundRoute) -> InboundCursor:
    """固定失败不回显输入，拒绝旧feedback游标和换route。"""
    try:
        if (
            not isinstance(value, str)
            or not value.startswith("gic1.")
            or len(value) > CURSOR_BYTES
        ):
            raise ValueError()
        encoded = value[5:]
        raw = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        payload = json.loads(raw)
        cursor = InboundCursor.model_validate(payload)
        if cursor.route != route or encode_cursor(cursor) != value:
            raise ValueError()
        started = cursor.bootstrap_started_at
        if (
            started.tzinfo is None
            or not 0 <= int(started.timestamp()) - cursor.after_epoch <= 30 * 86400
        ):
            raise ValueError()
        refs = (
            *cursor.pending,
            cursor.history_start,
            cursor.page_token,
            cursor.resume_history,
        )
        if any(
            v is not None
            and (not 1 <= len(v) <= 200 or any(ord(c) < 33 or ord(c) > 126 for c in v))
            for v in refs
        ):
            raise ValueError()
        if cursor.phase == "initial":
            if (
                cursor.history_start
                or cursor.page_token
                or cursor.pending
                or cursor.loaded
                or cursor.resume_history
            ):
                raise ValueError()
        elif cursor.history_start is None:
            raise ValueError()
        if cursor.pending and not cursor.loaded:
            raise ValueError()
        return cursor
    except Exception:  # noqa: BLE001 不允许Pydantic/base64错误带回cursor
        raise InboundError() from None


def initial_inbound_cursor(
    route: InboundRoute, bootstrap_started_at: datetime, after_epoch: int
) -> str:
    """composition生成一次后由5b耐久保存；重试不得用当前时间重建。"""
    if (
        bootstrap_started_at.tzinfo is None
        or bootstrap_started_at.utcoffset() != timedelta(0)
        or not 0 <= int(bootstrap_started_at.timestamp()) - after_epoch <= 30 * 86400
    ):
        raise InboundError()
    cursor = InboundCursor(
        route=route,
        phase="initial",
        bootstrap_started_at=bootstrap_started_at,
        after_epoch=after_epoch,
    )
    return encode_cursor(cursor)
