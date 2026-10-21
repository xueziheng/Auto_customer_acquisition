"""普通回复只认确定性报告、精确原始头和报告收件人。"""
from connectors.qq_mail.dialogue_feedback import parse_dialogue_report
from tests.unit.test_gmail_arf import _arf
from tests.unit.test_gmail_feedback_parser import _dsn, _ordinary_mail


def test_dsn_requires_one_exact_original_recipient():
    raw = _dsn().replace(b'private-0@example.com', b'private-recipient@example.com')
    report = parse_dialogue_report(raw)
    assert report and report.kind == 'hard_bounce'
    assert report.original_recipient == 'private-recipient@example.com'
    assert parse_dialogue_report(_dsn()) is None
    assert parse_dialogue_report(_ordinary_mail()) is None
    assert parse_dialogue_report(raw.replace(b'X-TradeOS-Idempotency-V1:', b'Unrelated:')) is None


def test_arf_requires_original_sides_and_rejects_conflicting_report_recipient():
    raw = _arf(message_id='b'*64+'@messages.tradeos.invalid', idempotency_header='c'*64).replace(b'X-TradeOS-Idempotency-V1:', b'From: sender@qq.com\r\nTo: buyer@example.com\r\nX-TradeOS-Idempotency-V1:')
    report = parse_dialogue_report(raw)
    assert report and report.kind == 'complaint'
    contradictory = raw.replace(b'Feedback-Type: abuse', b'Feedback-Type: abuse\r\nOriginal-Rcpt-To: rfc822; another@example.com')
    assert parse_dialogue_report(contradictory) is None
