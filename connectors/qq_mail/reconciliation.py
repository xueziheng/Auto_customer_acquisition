"""持续收信的显式单 UID 补收；复用正常解析，绝不回退持久游标。"""
from connectors.qq_mail.inbound import QQInboundReader, decode_cursor, encode_cursor
from shared.schemas.email_inbound import InboundError, ProviderInboundPage


class QQContinuousReconciliationReader:
    def __init__(self, reader: QQInboundReader, *, uidvalidity: int, uid: int):
        if (type(uidvalidity) is not int or not 1 <= uidvalidity <= 4294967295
            or type(uid) is not int or not 1 <= uid <= 4294967295):
            raise InboundError()
        self.reader, self.uidvalidity, self.uid = reader, uidvalidity, uid
        self.route = reader.route

    async def fetch_inbound_page(self, mailbox_alias: str, cursor: str, page_limit: int) -> ProviderInboundPage:
        state = decode_cursor(cursor, self.reader.route)
        target = await self.reader._target()
        if (self.reader._trial is not None or target.provider_header is None
            or state.uidvalidity != self.uidvalidity or state.last_uid < self.uid
            or state.window_end is not None):
            raise InboundError()
        # 仅构造这一次读取的范围；它从未写入持久 cursor 表。
        scan = state.model_copy(update={"last_uid": self.uid - 1, "window_end": self.uid})
        page = await self.reader.fetch_inbound_page(mailbox_alias, encode_cursor(scan), page_limit)
        if len(page.items) != 1 or decode_cursor(page.next_cursor, self.reader.route).last_uid != self.uid:
            raise InboundError()
        return ProviderInboundPage(route=page.route, starting_cursor=cursor,
            next_cursor=cursor, items=page.items)
