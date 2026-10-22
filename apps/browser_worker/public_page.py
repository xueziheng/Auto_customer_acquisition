"""单次公开页浏览器进程：网络全部由安全HTTP transport代理并逐请求校验。"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from connectors.web_search.transport import (
    PublicPageRejectedError,
    PublicPageTransport,
    SafePublicPageHttpTransport,
)
from shared.errors import TransientError
from shared.schemas.page_failure import PageReadFailure

MAX_BYTES = 2 * 1024 * 1024


def _page_rejection(error: PublicPageRejectedError, stage: str) -> PageReadFailure:
    return PageReadFailure(stage, {
        'page_access_forbidden': 'access_denied', 'login_or_captcha': 'login_required',
        'unsafe_redirect': 'unsafe_redirect',
    }[error.reason.value])


async def render_public_page(url: str, *, transport: PublicPageTransport | None = None) -> dict:
    """只运行公开HTML与内联脚本；固定阶段不携带浏览器或网页异常原文。"""
    stage = 'browser_url'
    timeout_types: tuple[type[Exception], ...] = (TimeoutError,)
    try:
        reader = transport or SafePublicPageHttpTransport(timeout_seconds=10)
        canonical = await reader.validate_url(url)
        stage = 'browser_init'
        from playwright.async_api import TimeoutError as BrowserTimeout
        from playwright.async_api import async_playwright
        timeout_types = (TimeoutError, BrowserTimeout)
        async with asyncio.timeout(40), async_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file():
                raise PageReadFailure(stage, 'failed')
            browser = await playwright.chromium.launch(
                headless=True,
                proxy={'server': 'http://127.0.0.1:9', 'bypass': '<-loopback>'},
                args=['--disable-background-networking', '--disable-quic',
                      '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'],
            )
            primary_failure = False
            try:
                context = await browser.new_context(accept_downloads=False, service_workers='block', java_script_enabled=True)
                count, total = 0, 0
                main_failure: PageReadFailure | None = None
                stage = 'browser_trc'
                await context.tracing.start(screenshots=True, snapshots=True, sources=False)

                async def route(request_route):
                    nonlocal count, total, main_failure
                    request = request_route.request
                    count += 1
                    main_document = request.is_navigation_request() and request.frame == page.main_frame
                    if count > 20 or request.method != 'GET' or request.resource_type not in {'document', 'xhr', 'fetch'}:
                        if main_document and main_failure is None:
                            main_failure = PageReadFailure('browser_nav', 'failed')
                        await request_route.abort()
                        return
                    failure = None
                    try:
                        checked = await reader.validate_url(request.url)
                        if urlsplit(checked).netloc != urlsplit(canonical).netloc:
                            raise PageReadFailure('browser_nav', 'unsafe_redirect')
                        response = await reader.fetch(checked)
                        total += len(response.body)
                        if total > MAX_BYTES:
                            raise PageReadFailure('browser_html', 'failed')
                        await request_route.fulfill(status=200, content_type='text/html', body=response.body)
                        return
                    except PageReadFailure as error:
                        failure = error
                    except PublicPageRejectedError as error:
                        failure = _page_rejection(error, 'browser_nav')
                    except TransientError:
                        failure = PageReadFailure('browser_nav', 'network_unknown')
                    except Exception:  # noqa: BLE001 -- 固定分类，不回显异常原文。
                        failure = PageReadFailure('browser_nav', 'failed')
                    if main_document and main_failure is None:
                        main_failure = failure
                    await request_route.abort()

                await context.route('**/*', route)
                await context.route_web_socket('**/*', lambda websocket: websocket.close())
                page = await context.new_page()
                stage = 'browser_nav'
                try:
                    await page.goto(canonical, wait_until='domcontentloaded', timeout=25000)
                except Exception:
                    if main_failure is not None:
                        raise main_failure from None
                    raise
                if main_failure is not None:
                    raise main_failure
                stage = 'browser_html'
                html = await page.content()
                if len(html.encode()) > MAX_BYTES:
                    raise PageReadFailure(stage, 'failed')
                stage = 'browser_shot'
                screenshot = await page.screenshot(type='png', full_page=False)
                stage = 'browser_trc'
                with tempfile.TemporaryDirectory(prefix='tradeos-browser-') as directory:
                    trace_path = Path(directory) / 'trace.zip'
                    await context.tracing.stop(path=trace_path)
                    if trace_path.stat().st_size > MAX_BYTES or len(screenshot) > MAX_BYTES:
                        raise PageReadFailure(stage, 'failed')
                    trace = trace_path.read_bytes()
                stage = 'browser_end'
                await context.close()
                return {'url': canonical, 'html': html, 'screenshot': base64.b64encode(screenshot).decode(),
                        'trace': base64.b64encode(trace).decode()}
            except BaseException:
                primary_failure = True
                raise
            finally:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001 -- 清理失败不得覆盖首个故障或取消。
                    if not primary_failure:
                        raise PageReadFailure('browser_end', 'failed') from None
    except PageReadFailure:
        raise
    except PublicPageRejectedError as error:
        raise _page_rejection(error, stage) from None
    except Exception as error:  # noqa: BLE001 -- 只按异常类型区分超时，不读取错误文本。
        raise PageReadFailure(stage, 'timeout' if isinstance(error, timeout_types) else 'failed') from None


def main() -> int:
    """stdin只接受公开URL；错误stdout仅允许固定枚举，永不包含原异常。"""
    try:
        raw = sys.stdin.buffer.read(4097)
        value = json.loads(raw)
        if len(raw) > 4096 or not isinstance(value, dict) or set(value) != {'url'} or not isinstance(value['url'], str):
            raise PageReadFailure('browser_ipc', 'invalid_frame')
        result = asyncio.run(render_public_page(value['url']))
        print(json.dumps(result))
        return 0
    except PageReadFailure as failure:
        print(json.dumps({'error': failure.frame()}))
        return 2
    except Exception:  # noqa: BLE001 -- 只返回固定安全类别。
        print('{"error":{"stage":"browser_ipc","reason":"invalid_frame"}}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
