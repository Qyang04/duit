"""A read-only IMAP client for the alert mailbox (stdlib imaplib: no extra dependency).

Read-only on purpose: the folder is opened with EXAMINE and bodies are fetched with BODY.PEEK,
so Duit never marks, moves or deletes mail. Where it got to is kept in Postgres instead (UIDs,
see duit.ingestion.inbox), next to the documents it stored.

imaplib is blocking; the poller calls these methods in a thread (asyncio.to_thread), one call
at a time, so a slow mail server never blocks other jobs in the worker.
"""

import imaplib
import re
import ssl
from collections.abc import Sequence
from types import TracebackType
from typing import Any, Protocol, Self, cast

from duit.core.settings import ImapConfig

TIMEOUT = 30  # seconds per socket operation; a hung server fails the run, the next one retries

FETCH_SIZE = re.compile(rb"UID (\d+) RFC822\.SIZE (\d+)|RFC822\.SIZE (\d+) UID (\d+)")
UIDVALIDITY = re.compile(rb"UIDVALIDITY (\d+)")


class ImapError(RuntimeError):
    """The server refused a command. Messages carry the server's words, never mail content."""


class Inbox(Protocol):
    """What the poller needs from a mailbox (a fake in the tests)."""

    def uidvalidity(self) -> int: ...

    def new_messages(self, after_uid: int, limit: int) -> list[tuple[int, int]]:
        """Up to `limit` (uid, size in bytes) with uid > after_uid, oldest first."""
        ...

    def fetch(self, uid: int) -> bytes: ...


def check(result: tuple[str, Sequence[Any]], command: str) -> Sequence[Any]:
    status, data = result
    if status != "OK":
        raise ImapError(f"{command}: {status} {data!r}"[:300])
    return data


def split(item: object) -> tuple[bytes | None, bytes | None]:
    """imaplib's FETCH data: a response line as bytes, or (line, literal) when the response
    carries a literal such as the message body."""
    if isinstance(item, bytes):
        return item, None
    if isinstance(item, tuple):
        pair = cast(tuple[object, ...], item)
        if len(pair) == 2 and isinstance(pair[0], bytes) and isinstance(pair[1], bytes):
            return pair[0], pair[1]
    return None, None


class ImapInbox:
    def __init__(self, config: ImapConfig) -> None:
        self.config = config
        # Certificate and host name are verified (Python's defaults), TLS 1.2 at least.
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.imap = imaplib.IMAP4_SSL(
            config.host, config.port, ssl_context=context, timeout=TIMEOUT
        )
        try:
            check(self.imap.login(config.username, config.password.get_secret_value()), "LOGIN")
            check(self.imap.select(self.quoted(config.folder), readonly=True), "EXAMINE")
        except BaseException:
            self.imap.shutdown()
            raise

    @staticmethod
    def quoted(folder: str) -> str:
        return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self.imap.logout()
        except (imaplib.IMAP4.error, OSError):
            self.imap.shutdown()

    def uidvalidity(self) -> int:
        """Changes when the server renumbers the folder; UIDs from before mean nothing then."""
        _, data = self.imap.response("UIDVALIDITY")
        if data and data[0]:
            return int(data[0])
        status = check(self.imap.status(self.quoted(self.config.folder), "(UIDVALIDITY)"), "STATUS")
        found = UIDVALIDITY.search(status[0] if isinstance(status[0], bytes) else b"")
        if found is None:
            raise ImapError("STATUS: no UIDVALIDITY")
        return int(found[1])

    def new_messages(self, after_uid: int, limit: int) -> list[tuple[int, int]]:
        data = check(self.imap.uid("SEARCH", "UID", f"{after_uid + 1}:*"), "UID SEARCH")
        raw = data[0] if data and isinstance(data[0], bytes) else b""
        # "N:*" always matches the newest message, even when its UID is below N.
        uids = sorted(u for u in map(int, raw.split()) if u > after_uid)[:limit]
        if not uids:
            return []
        data = check(self.imap.uid("FETCH", ",".join(map(str, uids)), "(RFC822.SIZE)"), "UID FETCH")
        sizes: dict[int, int] = {}
        for item in data:
            line, _ = split(item)
            if line is not None and (found := FETCH_SIZE.search(line)):
                uid, size = (found[1], found[2]) if found[1] else (found[4], found[3])
                sizes[int(uid)] = int(size)
        return [(uid, sizes[uid]) for uid in uids if uid in sizes]

    def fetch(self, uid: int) -> bytes:
        """The message exactly as the server stores it (RFC 822 bytes)."""
        data = check(self.imap.uid("FETCH", str(uid), "(BODY.PEEK[])"), "UID FETCH")
        for item in data:
            _, body = split(item)
            if body is not None:
                return body
        raise ImapError(f"UID FETCH {uid}: no message body")
