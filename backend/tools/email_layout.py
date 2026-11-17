"""Print the layout of alert emails with the personal data masked.

For writing alert templates without seeing anyone's real alerts. Run it locally on your own
emails, saved as .eml (Gmail: open the email > three dots > "Download message"):

    uv run python -m tools.email_layout "C:/path/alert1.eml" "C:/path/alert2.eml" > layout.txt

What stays readable: the sender's address and domain (a bank's, not yours), the authentication
results (DKIM, SPF, DMARC: which domains signed it and whether it passed), the MIME structure,
and generic words (KEEP, e.g. "Amount", "Merchant", "successful"). Everything else is masked:
every digit becomes 9 and every letter x/X, so formats survive (RM9,999.99, 99 Xxx 9999) but
values, names and merchants do not. Recipient headers are dropped.
READ layout.txt BEFORE SHARING IT and mask anything that still looks personal.
"""

import argparse
import email.policy
import re
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

from tools.statement_layout import KEEP as STATEMENT_WORDS

# Words alert emails are made of (EN + BM), on top of the statement words.
WORDS = """
A AN THE YOU YOUR YOURS WE OUR US IS ARE WAS HAS HAVE HAD BEEN BE WILL
FOR ON AT IN WITH BY AND OR NOT AS THIS THAT IF PLEASE KINDLY THANK THANKS
DEAR CUSTOMER VALUED SIR MADAM HI HELLO TUAN PUAN PELANGGAN
PAID PAY SPENT SPEND MADE MAKE RECEIVE SEND TRANSFERRED RELOAD RELOADED TOP UP
SUCCESSFUL SUCCESSFULLY SUCCESS FAILED UNSUCCESSFUL PENDING APPROVED DECLINED
BERJAYA GAGAL TELAH ANDA KAMI DI PADA UNTUK DAN ATAU
MERCHANT RECIPIENT SENDER PAYEE BENEFICIARY PENERIMA PENGIRIM PEDAGANG
TIME MASA STATUS TYPE JENIS METHOD DETAIL NOTIFICATION ALERT NOTICE
WALLET EWALLET E-WALLET BALANCE AVAILABLE NEW USED VIA USING DEBITED CREDITED
NUMBER NOMBOR ID EMAIL CONTACT CALL HELP SUPPORT FAQ VISIT APP
IMMEDIATELY RECOGNISE RECOGNIZE AUTHORISE AUTHORIZE SUSPICIOUS
DO REPLY GENERATED AUTOMATICALLY MESSAGE COMPUTER-GENERATED
TNG GX GXBANK DIGITAL MASTERCARD VISA
"""
# Words alert emails are made of (EN + BM), on top of the statement words. No short words that
# are also common names.
EMAIL_WORDS = frozenset(WORDS.split())
KEEP = STATEMENT_WORDS | EMAIL_WORDS
PUNCTUATION = ":.,()[]!?;'\"*-|"

# Headers shown as they are: they describe the sender and the mail system, not the recipient.
SENDER_HEADERS = ("From", "Sender", "Reply-To", "Return-Path")
AUTH_HEADERS = ("Authentication-Results", "ARC-Authentication-Results", "Received-SPF")
BLOCK_TAGS = frozenset(
    "p div br tr li h1 h2 h3 h4 h5 h6 table tbody thead tfoot section header footer hr".split()  # noqa: SIM905
)


def mask(word: str) -> str:
    """Digits become 9; each run of letters stays if it is a KEEP word, else becomes x/X. Runs,
    not whole tokens: "RM12.50" -> "RM99.99", "Amount:RM5" -> "Amount:RM9"."""

    def letters(run: re.Match[str]) -> str:
        if run[0].upper() in KEEP:
            return run[0]
        return re.sub(r"[A-Z]", "X", re.sub(r"[a-z]", "x", run[0]))

    if word.upper().strip(PUNCTUATION) in KEEP:  # e.g. E-WALLET, A/C
        return word
    return re.sub(r"[A-Za-z]+", letters, re.sub(r"[0-9]", "9", word))


def mask_line(line: str) -> str:
    return " ".join(mask(w) for w in line.split())


def mask_digits(text: str) -> str:
    """For mail-system headers: domains and results stay, ids and addresses' numbers do not."""
    return re.sub(r"[0-9]", "9", text)


class HtmlText(HTMLParser):
    """HTML to lines of text: a line per block element, " | " between table cells."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "img":
            alt = dict(attrs).get("alt")
            self.parts.append(f" [img alt={alt!r}] " if alt else " [img] ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)

    def lines(self) -> list[str]:
        text = "".join(self.parts).replace("\xa0", " ")
        out = [" ".join(line.split()).strip(" |") for line in text.splitlines()]
        return [line for line in out if line]


def html_lines(html: str) -> list[str]:
    parser = HtmlText()
    parser.feed(html)
    parser.close()
    return parser.lines()


def describe(message: EmailMessage) -> list[str]:
    out = ["--- sender ---"]
    for name in SENDER_HEADERS:
        for value in message.get_all(name, []):
            out.append(f"{name}: {mask_digits(str(value))}")
    out.append(f"Subject: {mask_line(str(message.get('Subject', '')))}")
    out.append(f"Date: {mask_digits(str(message.get('Date', '')))}")
    forwarded = "X-Forwarded-To" in message
    out.append(f"forwarded by Gmail filter: {'yes' if forwarded else 'no'}")

    out.append("--- authentication (newest first; the receiving server adds these) ---")
    for name in AUTH_HEADERS:
        for value in message.get_all(name, []):
            out.append(f"{name}: {mask_digits(' '.join(str(value).split()))}")
    for value in message.get_all("DKIM-Signature", []):
        tags = dict(
            tag.strip().split("=", 1) for tag in str(value).split(";") if "=" in tag.strip()
        )
        out.append(f"DKIM-Signature: d={tags.get('d', '?')} s={tags.get('s', '?')}")

    out.append("--- structure ---")
    for part in message.walk():
        filename = part.get_filename()
        out.append(
            f"{part.get_content_type()}"
            + (f" charset={part.get_content_charset()}" if part.get_content_charset() else "")
            + (
                f" cte={part['Content-Transfer-Encoding']}"
                if part["Content-Transfer-Encoding"]
                else ""
            )
            + (f" filename={mask_line(filename)}" if filename else "")
        )

    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        text = str(part.get_content())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        lines = html_lines(text) if content_type == "text/html" else text.splitlines()
        out.append(f"--- {content_type} as text, masked ---")
        out.extend(mask_line(line) for line in lines if line.strip())
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Print alert emails' layout, data masked.")
    parser.add_argument("eml", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.eml:
        message = BytesParser(policy=email.policy.default).parsebytes(path.read_bytes())
        print(f"===== {path.name} ({path.stat().st_size} bytes) =====")
        print("\n".join(describe(message)))
        print()


if __name__ == "__main__":
    main()
