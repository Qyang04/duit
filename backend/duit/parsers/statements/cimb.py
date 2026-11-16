"""CIMB (and CIMB Islamic) savings/current account statements, PDF.

Layout (one row per transaction, then indented description lines):

    Date        Description          Ref No   Withdrawal  Deposits  Tax  Balance
                OPENING BALANCE                                          1,000.00
    01/11/2026  POS DEBIT            A12345   12.50                        987.50
                SHOP NAME
                31/10/2026 2359      <- card swipe date: indented, NOT a new transaction
                ...
                                     CLOSING BALANCE / BAKI PENUTUP      2,345.67

Rules, all derived from the page itself rather than fixed coordinates:
- Columns come from the header row found on each page (Withdrawal, Deposits, Tax, Balance).
- A new transaction starts with a date left of the Date header's right edge; indented lines
  belong to the transaction above (also across a page break).
- Left-margin text that is not a date ends the table on that page (notices, footer).
- Each amount belongs to the column whose header centre is nearest to it.
- Reconciliation: opening + every amount must reproduce every printed balance and the closing
  balance, or the whole statement is rejected.
"""

import re
from dataclasses import dataclass, field
from datetime import date

from duit.parsers.statements.base import (
    Statement,
    StatementError,
    StatementLine,
    Word,
    group_lines,
)

DATE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
AMOUNT = re.compile(r"^\d{1,3}(?:,\d{3})*\.\d{2}$")
AMOUNT_COLUMNS = ("withdrawal", "deposits", "tax", "balance")


def to_sen(text: str) -> int:
    """'1,234.56' -> 123456, exactly: never through a float."""
    return int(text.replace(",", "").replace(".", ""))


@dataclass(frozen=True)
class Columns:
    date_end: float  # right edge of the "Date" header: transaction dates start left of it
    ref_start: float  # left edge of "Ref No": first-line words from here on are not description
    amount_start: float  # left edge of the first amount column
    centers: dict[str, float]

    def amount_column(self, word: Word) -> str:
        return min(self.centers, key=lambda name: abs(self.centers[name] - word.center))


@dataclass
class Row:
    posted_on: date
    page: int
    ref: str | None
    amounts: dict[str, int]
    description: list[str] = field(default_factory=lambda: list[str]())


def find_columns(lines: list[list[Word]]) -> tuple[int, Columns] | None:
    for index, line in enumerate(lines):
        by_text = {w.text.lower(): w for w in line}
        if not {"date", "withdrawal", "deposits", "tax", "balance"} <= by_text.keys():
            continue
        ref = by_text.get("ref")
        withdrawal = by_text["withdrawal"]
        return index, Columns(
            date_end=by_text["date"].x1,
            ref_start=ref.x0 - 10 if ref else withdrawal.x0 - 20,
            amount_start=withdrawal.x0 - 20,
            centers={name: by_text[name].center for name in AMOUNT_COLUMNS},
        )
    return None


def parse_date(text: str, page: int) -> date:
    match = DATE.match(text)
    if match is None:
        raise StatementError(f"Unexpected date format on page {page}")
    day, month, year = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise StatementError(f"Invalid date on page {page}") from exc


def balance_on(line: list[Word], columns: Columns, page: int, what: str) -> int:
    amounts = [w for w in line if AMOUNT.match(w.text)]
    if len(amounts) != 1 or columns.amount_column(amounts[0]) != "balance":
        raise StatementError(f"Cannot read the {what} on page {page}")
    return to_sen(amounts[0].text)


def start_row(line: list[Word], columns: Columns, page: int) -> Row:
    row = Row(posted_on=parse_date(line[0].text, page), page=page, ref=None, amounts={})
    description: list[str] = []
    for word in line[1:]:
        if AMOUNT.match(word.text) and word.x0 >= columns.amount_start:
            name = columns.amount_column(word)
            if name in row.amounts:
                raise StatementError(f"Two amounts in one column on page {page}")
            row.amounts[name] = to_sen(word.text)
        elif word.x0 >= columns.amount_start:
            pass  # a marker in the amount area, e.g. the service-tax flag
        elif word.x0 >= columns.ref_start:
            row.ref = word.text if row.ref is None else f"{row.ref} {word.text}"
        else:
            description.append(word.text)
    row.description.append(" ".join(description))
    return row


def finish(row: Row, number: int) -> tuple[int, int]:
    """(signed amount, printed balance) of a completed row."""
    where = f"transaction {number} (page {row.page}, {row.posted_on:%d/%m/%Y})"
    out, money_in = row.amounts.get("withdrawal"), row.amounts.get("deposits")
    if (out is None) == (money_in is None):
        raise StatementError(f"Expected exactly one of withdrawal or deposit at {where}")
    if "balance" not in row.amounts:
        raise StatementError(f"No balance at {where}")
    amount = (money_in or 0) - (out or 0) - row.amounts.get("tax", 0)
    return amount, row.amounts["balance"]


def parse_cimb(pages: list[list[Word]]) -> Statement:
    opening: int | None = None
    closing: int | None = None
    rows: list[Row] = []

    for page_number, words in enumerate(pages, start=1):
        lines = group_lines(words)
        found = find_columns(lines)
        if found is None:
            continue  # no transaction table on this page (notices, a scanned insert)
        header, columns = found
        for line in lines[header + 1 :]:
            text = " ".join(w.text for w in line).upper()
            first = line[0]
            if text.startswith(("TARIKH", "(MYR)")):
                continue  # second and third header rows
            if "OPENING BALANCE" in text:
                opening = balance_on(line, columns, page_number, "opening balance")
            elif "CLOSING BALANCE" in text:
                closing = balance_on(line, columns, page_number, "closing balance")
                break
            elif first.x0 < columns.date_end:
                if not DATE.match(first.text):
                    break  # left-margin text that is not a transaction: end of the table
                rows.append(start_row(line, columns, page_number))
            elif rows:
                rows[-1].description.append(" ".join(w.text for w in line))
        if closing is not None:
            break

    if opening is None:
        raise StatementError("No OPENING BALANCE found: is this a CIMB account statement?")
    if closing is None:
        raise StatementError("No CLOSING BALANCE found: is the statement complete?")

    lines_out: list[StatementLine] = []
    balance = opening
    for number, row in enumerate(rows, start=1):
        amount, printed = finish(row, number)
        balance += amount
        if balance != printed:
            raise StatementError(
                f"Balance does not add up at transaction {number} "
                f"(page {row.page}, {row.posted_on:%d/%m/%Y})"
            )
        lines_out.append(
            StatementLine(
                posted_on=row.posted_on,
                description="\n".join(d for d in row.description if d),
                amount_sen=amount,
                balance_sen=printed,
                ref=row.ref,
                page=row.page,
            )
        )
    if balance != closing:
        raise StatementError("Transactions do not add up to the closing balance")
    return Statement(opening, closing, tuple(lines_out))
