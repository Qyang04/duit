"""What every bank statement parser produces, and the shared PDF reading.

Parsers are pure functions from positioned words to a Statement, so almost all of their logic is
tested without a PDF. Only read_pdf touches pdfplumber.
"""

import hashlib
import io
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfplumber.utils.exceptions import PdfminerException

MAX_PAGES = 60  # statements are a few pages; refuse anything that looks like a PDF bomb


class StatementError(ValueError):
    """The file is not a statement this parser understands, or its numbers do not add up.
    Messages never include amounts or descriptions: they may end up in logs."""


class StatementPasswordError(StatementError):
    pass


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    x1: float
    top: float

    @property
    def center(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass(frozen=True)
class StatementLine:
    posted_on: date
    description: str  # every line of the bank's description, joined with newlines
    amount_sen: int  # signed: negative = money out
    balance_sen: int  # the running balance the bank printed after this line
    ref: str | None
    page: int


@dataclass(frozen=True)
class Statement:
    opening_balance_sen: int
    closing_balance_sen: int
    lines: tuple[StatementLine, ...]


def statement_fields(statement: Statement) -> dict[str, Any]:
    """The statement as JSON for extractions.fields (dates as ISO strings)."""
    return {
        "opening_balance_sen": statement.opening_balance_sen,
        "closing_balance_sen": statement.closing_balance_sen,
        "lines": [
            {
                "posted_on": line.posted_on.isoformat(),
                "description": line.description,
                "amount_sen": line.amount_sen,
                "balance_sen": line.balance_sen,
                "ref": line.ref,
                "page": line.page,
            }
            for line in statement.lines
        ],
    }


def read_pdf(data: bytes, password: str | None) -> list[list[Word]]:
    """Positioned words, one list per page. Wrong or missing password: StatementPasswordError."""
    try:
        pdf = pdfplumber.open(io.BytesIO(data), password=password or "")
    except PdfminerException as exc:
        if isinstance(exc.args[0] if exc.args else None, PDFPasswordIncorrect):
            raise StatementPasswordError("Wrong or missing PDF password") from exc
        raise StatementError("Not a readable PDF") from exc
    with pdf:
        if len(pdf.pages) > MAX_PAGES:
            raise StatementError(f"More than {MAX_PAGES} pages")
        return [
            [Word(w["text"], w["x0"], w["x1"], w["top"]) for w in page.extract_words()]
            for page in pdf.pages
        ]


def group_lines(words: list[Word], tolerance: float = 2.5) -> list[list[Word]]:
    """Words whose tops are within `tolerance` points form one line, sorted left to right."""
    lines: list[list[Word]] = []
    for word in sorted(words, key=lambda w: (w.top, w.x0)):
        if lines and abs(lines[-1][0].top - word.top) <= tolerance:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w.x0) for line in lines]


def dedupe_keys(account_id: uuid.UUID, lines: tuple[StatementLine, ...]) -> list[str]:
    """A stable fingerprint per line, so importing the same statement twice (or two statements
    that overlap) adds each transaction once.

    The running balance makes lines that look identical (two RM 5 coffees on one day) differ.
    The occurrence count covers the rare case where even the balance repeats (-5, +5, -5).
    """
    seen: dict[tuple[object, ...], int] = {}
    keys: list[str] = []
    for line in lines:
        ident = (line.posted_on, line.amount_sen, line.balance_sen, line.description)
        seen[ident] = seen.get(ident, 0) + 1
        raw = "|".join(
            [str(account_id), line.posted_on.isoformat(), str(line.amount_sen)]
            + [str(line.balance_sen), line.description, str(seen[ident])]
        )
        keys.append(hashlib.sha256(raw.encode()).hexdigest())
    return keys
