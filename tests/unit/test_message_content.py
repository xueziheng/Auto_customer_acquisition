from apps.api.composition.message_content import parse_message_content


def test_utf8_plaintext_content_and_addresses_are_readable():
    raw = (
        "From: sender@example.test\r\nTo: buyer@example.test\r\nSubject: test Q02\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nhello 你好"
    ).encode()
    result = parse_message_content(raw)
    assert result.subject == "test Q02"
    assert result.sender == "sender@example.test"
    assert result.recipients == "buyer@example.test"
    assert result.body_text == "hello 你好"


def test_html_only_is_not_executed_or_returned_as_markup():
    result = parse_message_content(
        b"Content-Type: text/html\r\n\r\n<script>alert(1)</script>"
    )
    assert result.body_text is None
    assert result.html_only is True


def test_html_only_reply_displays_text_and_quoted_history_without_active_content():
    result = parse_message_content(
        ('Content-Type: text/html; charset=utf-8\r\n\r\n'
         '<div>我想购买太阳能风扇</div><div>请告诉我怎么买</div>'
         '<blockquote>原始邮件正文</blockquote>'
         '<style>secret style</style><script>alert(1)</script>'
         '<img src="https://tracker.invalid/pixel" onerror="bad()">').encode()
    )
    assert "我想购买太阳能风扇" in result.body_text
    assert "原始邮件正文" in result.body_text
    assert "<" not in result.body_text
    assert "alert" not in result.body_text
    assert "tracker" not in result.body_text
    assert "secret style" not in result.body_text


def test_qq_alternative_displays_one_readable_body_without_entity_artifacts():
    from email.message import EmailMessage
    message = EmailMessage()
    message.set_content("hello&nbsp;TradeOS")
    message.add_alternative("<div>hello&nbsp;TradeOS</div><blockquote>original</blockquote>", subtype="html")
    result = parse_message_content(message.as_bytes())
    assert result.body_text.count("hello") == 1
    assert "&nbsp;" not in result.body_text
    assert "original" in result.body_text
