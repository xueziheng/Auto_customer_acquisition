"""持锁主循环调用的组合activation，不能在factory自动probe。"""

from unittest.mock import AsyncMock

import pytest

from apps.scheduler_worker import runtime


@pytest.mark.parametrize("existing", [True, False])
async def test_quote_activation_runs_existing_before_quote_probe(existing):
    order = []
    previous, lifecycle = AsyncMock(), AsyncMock()
    previous.activate.side_effect = lambda: order.append("existing")
    lifecycle.startup.side_effect = lambda: order.append("quote")
    assert hasattr(runtime, "_QuoteRuntimeActivation"), "缺少持锁报价activation"
    await runtime._QuoteRuntimeActivation(
        previous if existing else None, lifecycle
    ).activate()
    assert order == (["existing", "quote"] if existing else ["quote"])


@pytest.mark.parametrize(
    "error", [RuntimeError("controlled"), __import__("asyncio").CancelledError()]
)
async def test_failed_existing_activation_never_starts_quote_parser(error):
    previous, lifecycle = AsyncMock(), AsyncMock()
    previous.activate.side_effect = error
    assert hasattr(runtime, "_QuoteRuntimeActivation"), "缺少持锁报价activation"
    with pytest.raises(type(error)):
        await runtime._QuoteRuntimeActivation(previous, lifecycle).activate()
    lifecycle.startup.assert_not_awaited()
