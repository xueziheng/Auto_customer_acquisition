"""只加载已公开的脱敏双头；其余头、正文与 scope 均是测试合成材料。"""

import base64
from pathlib import Path

from tests.unit.test_qq_bounded_trial import Gate
from tests.unit.test_qq_inbound import FROM, TO
from tests.unit.test_qq_trial_wire import alternative

FIXTURE = Path(__file__).parents[1] / "fixtures/qq_trial/q02-address-syntax-redacted.headers"


def address_mail():
    fields = FIXTURE.read_bytes().replace(b"111111111@qq.com", FROM.encode()).replace(
        b"2222222222@qq.com", TO.encode())
    raw = alternative()
    return raw.replace(f"From: {FROM}\r\nTo: {TO}\r\n".encode(), fields[:-2])


def replace_field(raw, name, value):
    prefix, body = raw.split(b"\r\n\r\n", 1)
    lines = prefix.split(b"\r\n")
    lines = [name + b": " + value if line.startswith(name + b":") else line for line in lines]
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


def word(label, *, charset=b"utf-8", encoding=b"B"):
    return b'"=?' + charset + b"?" + encoding + b"?" + base64.b64encode(label) + b'?="'


def bad_value(case, mailbox):
    valid = word(b"Xxxx") + b" <" + mailbox + b">"
    malformed = {
        "invalid_base64": valid.replace(b"WHh4eA==", b"WHh4!A=="),
        "missing_padding": valid.replace(b"WHh4eA==", b"WHh4eA"),
        "extra_padding": valid.replace(b"WHh4eA==", b"WHh4eA==="),
        "nonzero_pad_bits": valid.replace(b"WHh4eA==", b"WHh4eB=="),
        "wrong_charset": word(b"Xxxx", charset=b"ascii") + b" <" + mailbox + b">",
        "unknown_charset": word(b"Xxxx", charset=b"x-unknown") + b" <" + mailbox + b">",
        "q_encoding": b'"=?utf-8?Q?Xxxx?=" <' + mailbox + b">",
        "lowercase_b": valid.replace(b"?B?", b"?b?"),
        "uppercase_charset": valid.replace(b"utf-8", b"UTF-8"),
        "utf8_error": word(b"\xff") + b" <" + mailbox + b">",
        "unicode_display": word("名".encode()) + b" <" + mailbox + b">",
        "display_space": word(b"X xx") + b" <" + mailbox + b">",
        "display_punctuation": word(b"X.xx") + b" <" + mailbox + b">",
        "empty_display": b'"=?utf-8?B??=" <' + mailbox + b">",
        "long_display": word(b"X" * 33) + b" <" + mailbox + b">",
        "display_null": word(b"X\x00x") + b" <" + mailbox + b">",
        "display_control": word(b"X\x1fx") + b" <" + mailbox + b">",
        "encoded_crlf": word(b"X\r\nBcc: 333333333@qq.com") + b" <" + mailbox + b">",
        "literal_crlf": valid.replace(b"WHh4eA==", b"WHh4\r\nBcc: 333333333@qq.com"),
        "literal_lf": valid.replace(b"WHh4eA==", b"WHh4\neA=="),
        "literal_control": valid.replace(b"WHh4eA==", b"WHh4\x00eA=="),
        "two_words": valid.replace(b'" <', b' =?utf-8?B?WA==?=" <'),
        "escaped": valid.replace(b"WHh4eA==", b"\\WHh4eA=="),
        "folded": valid.replace(b'" <', b'"\r\n <'),
        "comment": valid + b" (comment)",
        "internal_comment": valid.replace(b">", b"(comment)>"),
        "two_addresses": valid + b", <333333333@qq.com>",
        "group": b"Friends: " + valid + b";",
        "extra_space": valid.replace(b'" <', b'"  <'),
        "tab_space": valid.replace(b'" <', b'"\t<'),
        "trailing_space": valid + b" ",
        "quote_space": valid.replace(b'?="', b'?= "'),
        "trailing_data": valid + b" junk",
        "wrong_party": valid.replace(mailbox, b"333333333@qq.com"),
    }
    return malformed[case]


BAD_CASES = (
    "invalid_base64", "missing_padding", "extra_padding", "nonzero_pad_bits",
    "wrong_charset", "unknown_charset", "q_encoding", "lowercase_b", "uppercase_charset",
    "utf8_error", "unicode_display", "display_space", "display_punctuation", "empty_display",
    "long_display", "display_null", "display_control", "encoded_crlf", "literal_crlf",
    "literal_lf", "literal_control", "two_words", "escaped", "folded", "comment",
    "internal_comment", "two_addresses", "group", "extra_space", "tab_space",
    "trailing_space", "quote_space", "trailing_data", "wrong_party",
)


class AddressGate(Gate):
    def __init__(self):
        super().__init__()
        self.address_audits = []

    async def record_address_compatibility(self, fields):
        await self.check()
        self.address_audits.append(fields)
