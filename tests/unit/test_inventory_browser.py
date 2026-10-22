"""浏览器必须逐请求经过安全transport，禁止浏览器自行联网。"""

import importlib

import pytest


def module():
    try:
        return importlib.import_module("apps.browser_worker.public_page")
    except ModuleNotFoundError:
        pytest.fail("RED: 独立受控浏览器尚未实现")


async def test_browser_rejects_private_page_before_starting_chromium():
    with pytest.raises(ValueError):
        await module().render_public_page("http://127.0.0.1/admin")


async def test_browser_executes_inline_javascript_and_blocks_unapproved_requests():
    from connectors.web_search.transport import PublicPageResponse

    m = module()
    calls = []

    class Transport:
        async def validate_url(self, url):
            if not url.startswith("https://company.example/"):
                raise ValueError("blocked")
            return url

        async def fetch(self, url):
            await self.validate_url(url)
            calls.append(url)
            return PublicPageResponse(
                url,
                b'<html><body><p id="result">before</p><script>document.getElementById("result").textContent="after"; fetch("http://127.0.0.1/private")</script></body></html>',
            )

    result = await m.render_public_page(
        "https://company.example/", transport=Transport()
    )
    assert ">after<" in result["html"]
    assert "http://127.0.0.1/private" not in calls
    assert result["screenshot"] and result["trace"]


@pytest.mark.parametrize('kind', ['denied', 'http_error', 'network_unknown'])
async def test_browser_main_document_failure_retains_safe_cause(kind):
    from connectors.web_search.transport import PublicPageRejectedError
    from shared.schemas.page_failure import PageReadFailure
    class Transport:
        async def validate_url(self, url):
            return url
        async def fetch(self, url):
            if kind == 'denied':
                raise PublicPageRejectedError()
            raise PageReadFailure('page_robots', kind)
    with pytest.raises(PageReadFailure) as raised:
        await module().render_public_page('https://company.example/', transport=Transport())
    assert raised.value.reason == ('access_denied' if kind == 'denied' else kind)
    assert raised.value.recoverable == (kind != 'network_unknown')


async def test_browser_missing_binary_records_init_stage(monkeypatch):
    from pathlib import Path

    from shared.schemas.page_failure import PageReadFailure
    class Transport:
        async def validate_url(self, url):
            return url
    monkeypatch.setattr(Path, 'is_file', lambda path: False)
    with pytest.raises(PageReadFailure) as raised:
        await module().render_public_page('https://company.example/', transport=Transport())
    assert raised.value.stage == 'browser_init'
    assert not raised.value.recoverable


@pytest.mark.parametrize('raw', [b'PRIVATE', b'{"error":{"stage":"browser_nav","reason":"PRIVATE"}}',
                                b'{"error":{"stage":"browser_nav","reason":"http_error","url":"PRIVATE"}}'])
def test_browser_error_frame_never_echoes_untrusted_fields(raw):
    from connectors.playwright.public_page import decode_browser_failure
    failure = decode_browser_failure(raw)
    assert failure.stage == 'browser_ipc' and failure.reason == 'invalid_frame'
    assert 'PRIVATE' not in str(failure) and not failure.recoverable


async def test_browser_connector_spawn_error_has_fixed_init_stage(monkeypatch):
    import asyncio

    from connectors.playwright.public_page import BrowserPublicPageReader
    from shared.schemas.page_failure import PageReadFailure
    async def failed_spawn(*args, **kwargs):
        raise OSError('PRIVATE process detail')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', failed_spawn)
    with pytest.raises(PageReadFailure) as raised:
        await BrowserPublicPageReader(None).read_page('tenant', 'https://company.example/', 'user')
    assert raised.value.stage == 'browser_init' and raised.value.reason == 'failed'
    assert 'PRIVATE' not in str(raised.value)


async def test_connector_cleanup_race_preserves_first_fixed_failure(monkeypatch):
    import asyncio
    import os
    from types import SimpleNamespace

    from connectors.playwright.public_page import BrowserPublicPageReader
    from shared.schemas.page_failure import PageReadFailure
    class Input:
        def write(self, value):
            pass
        async def drain(self):
            raise ValueError('PRIVATE first failure')
    async def wait():
        return 2
    async def spawn(*args, **kwargs):
        return SimpleNamespace(stdin=Input(), stdout=object(), returncode=None, pid=123, wait=wait)
    def killed(*args):
        raise ProcessLookupError('PRIVATE cleanup race')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(os, 'killpg', killed)
    with pytest.raises(PageReadFailure) as raised:
        await BrowserPublicPageReader(None).read_page('tenant', 'https://company.example/', 'user')
    assert raised.value.stage == 'browser_ipc'
    assert 'PRIVATE' not in str(raised.value)
