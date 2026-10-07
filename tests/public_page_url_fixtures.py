"""Task 9/13 的公开页面 URL hostile corpus，所有入口共用同一组断言。"""

from __future__ import annotations

HOSTILE_PUBLIC_PAGE_URLS = (
    "http://127.1/private",
    "http://2130706433/private",
    "http://0x7f000001/private",
    "http://0x7f.0.0.1/private",
    "http://127.0x0.0.1/private",
    "http://0x7f.0x0.0x0.0x1/private",
    "http://0177.0.0x0.1/private",
    "http://127.00.0x0.01/private",
    "http://0177.0.0.1/private",
    "http://127.0.0.01/private",
    "http://999.999.999.999/private",
    "http://%31%32%37.0.0.1/private",
    "https://supplier.example@127.0.0.1/private",
    "https://supplier.example/products/hinge#contact",
    "https://supplier.example/products/\x01hinge",
    "https://supplier.example:444/products/hinge",
    "https://127.0.0.1/private",
    "https://10.0.0.1/private",
    "https://169.254.169.254/private",
    "https://224.0.0.1/private",
    "https://239.255.255.250/private",
    "https://255.255.255.255/private",
    "https://0.0.0.0/private",
    "https://127.0.0.1./private",
    "https://[::1]/private",
    "https://[fe80::1]/private",
    "https://[ff02::1]/private",
    "https://[::]/private",
    "https://[2001:db8::1]/private",
)


__all__ = ("HOSTILE_PUBLIC_PAGE_URLS",)
