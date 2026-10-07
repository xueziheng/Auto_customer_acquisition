"""显式真实模型验收；Gmail 受控，不使用日常客户或发件身份。"""

import os
from pathlib import Path

from shared.schemas.identifiers import new_id
from tests.evals.reply_live_settings import load_live_settings
from tests.integration.test_standalone_reply_chain import (
    reply_storage,  # noqa: F401
    standalone_reply_chain,
)


async def test_live_reply_corpus_and_business_chain(request, tmp_path):
    config = load_live_settings(os.environ)
    # 在显式许可检查之后才创建 owned 存储；真实入口没有受控模型回退。
    storage = request.getfixturevalue("reply_storage")
    from tests.evals.reply_live_acceptance import run_acceptance

    report_path = Path("output/acceptance/reply-model") / f"{new_id('eval')}.json"
    async with standalone_reply_chain(
        storage,
        tmp_path,
        model=config.settings,
        model_resolver=config.resolver,
        model_provider=None,
    ) as chain:
        report = await run_acceptance(chain, report_path)
    assert report["ledger"]["calls"] <= config.max_calls
    assert report["complete"], f"真实回复验收未通过；详见 {report_path}"
