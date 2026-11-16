"""CIMB statement parser. Word positions mirror a real (masked) CIMB statement layout; all
names and numbers here are made up."""

import uuid
from datetime import date

import pytest
from fpdf import FPDF
from fpdf.enums import EncryptionMethod

from duit.parsers.statements.base import (
    StatementError,
    StatementPasswordError,
    Word,
    dedupe_keys,
    read_pdf,
)
from duit.parsers.statements.cimb import parse_cimb, to_sen

# Left edges taken from the real layout.
DATE_X, DESC_X, REF_X = 38.3, 80.8, 260.0
# Right edges of the amount columns (numbers are right-aligned under their headers).
WITHDRAWAL_R, DEPOSITS_R, TAX_R, BALANCE_R = 356.0, 412.0, 462.0, 530.0
CHAR_WIDTH = 4.5


def words(top: float, *items: tuple[float, str]) -> list[Word]:
    """Words of one line: (x0, text) pairs; a text may contain spaces (split into words)."""
    out: list[Word] = []
    for x0, text in items:
        x = x0
        for part in text.split():
            out.append(Word(part, x, x + CHAR_WIDTH * len(part), top))
            x += CHAR_WIDTH * (len(part) + 1)
    return out


def right(x1: float, text: str) -> tuple[float, str]:
    """A right-aligned amount ending at x1."""
    return (x1 - CHAR_WIDTH * len(text), text)


def header(top: float) -> list[Word]:
    return (
        words(
            top,
            (49.8, "Date"),
            (137.4, "Description"),
            (258.0, "Ref No"),
            (316.5, "Withdrawal"),
            (381.3, "Deposits"),
            (446.8, "Tax"),
            (502.5, "Balance"),
        )
        + words(top + 8, (47.7, "Tarikh"), (143.4, "Diskripsi"), (315.3, "Pengeluaran"))
        + words(top + 16, (325.1, "(MYR)"), (386.0, "(MYR)"), (505.8, "(MYR)"))
    )


def page_top() -> list[Word]:
    """Lines above the table: bank name, statement date (a date that is not a transaction)."""
    return words(50.4, (42.5, "CIMB Islamic Bank Berhad")) + words(
        102.5, (314.6, "Statement Date / Tarikh"), (480.0, "30/11/2026")
    )


def footer() -> list[Word]:
    return words(710.2, (38.3, "Important Notice / Notis Penting")) + words(
        803.1, (38.3, "17th Floor Menara CIMB, Kuala Lumpur")
    )


def two_page_statement() -> list[list[Word]]:
    page1 = (
        page_top()
        + header(240.4)
        + words(270.1, (DESC_X, "OPENING BALANCE"), right(BALANCE_R, "1,000.00"))
        # Card purchase: the indented 31/10/2026 is the swipe date, not a new transaction.
        + words(
            283.8,
            (DATE_X, "01/11/2026"),
            (DESC_X, "POS DEBIT"),
            (REF_X, "A12345"),
            right(WITHDRAWAL_R, "12.50"),
            right(BALANCE_R, "987.50"),
        )
        + words(293.0, (DESC_X, "KK SUPER MART"))
        + words(302.2, (DESC_X, "31/10/2026 2359"))
        + words(
            324.6,
            (DATE_X, "03/11/2026"),
            (DESC_X, "DUITNOW TO ACCOUNT"),
            right(DEPOSITS_R, "5,000.00"),
            right(BALANCE_R, "5,987.50"),
        )
        + words(333.8, (DESC_X, "Salary"), (183.0, "ACME SDN BHD"))
        + words(
            355.3,
            (DATE_X, "05/11/2026"),
            (DESC_X, "DUITNOW"),
            right(WITHDRAWAL_R, "8.00"),
            right(BALANCE_R, "5,979.50"),
        )
        + footer()
    )
    page2 = (
        page_top()
        + header(189.2)
        # Continues the last transaction of page 1.
        + words(218.9, (DESC_X, "DuitNow QR"))
        + words(
            228.1,
            (DATE_X, "06/11/2026"),
            (DESC_X, "ATM WITHDRAWAL"),
            right(WITHDRAWAL_R, "100.00"),
            right(BALANCE_R, "5,879.50"),
        )
        + words(453.2, (300.0, "CLOSING BALANCE / BAKI PENUTUP"), right(BALANCE_R, "5,879.50"))
        + words(528.0, (180.0, "*** End of Statement / Akhir Penyata ***"))
        + footer()
    )
    return [page1, page2]


def test_parses_a_two_page_statement() -> None:
    statement = parse_cimb(two_page_statement())

    assert statement.opening_balance_sen == 100000
    assert statement.closing_balance_sen == 587950
    assert [(t.posted_on, t.amount_sen, t.balance_sen) for t in statement.lines] == [
        (date(2026, 11, 1), -1250, 98750),
        (date(2026, 11, 3), 500000, 598750),
        (date(2026, 11, 5), -800, 597950),
        (date(2026, 11, 6), -10000, 587950),
    ]
    pos = statement.lines[0]
    assert pos.description == "POS DEBIT\nKK SUPER MART\n31/10/2026 2359"
    assert pos.ref == "A12345"
    assert statement.lines[1].description == "DUITNOW TO ACCOUNT\nSalary ACME SDN BHD"
    # A description that continues on the next page stays with its transaction.
    assert statement.lines[2].description == "DUITNOW\nDuitNow QR"
    assert [t.page for t in statement.lines] == [1, 1, 1, 2]


def test_notices_and_footers_are_not_descriptions() -> None:
    statement = parse_cimb(two_page_statement())
    assert all("Notice" not in t.description for t in statement.lines)
    assert all("CIMB" not in t.description for t in statement.lines)


def test_a_wrong_balance_rejects_the_statement() -> None:
    pages = two_page_statement()
    pages[0] = [
        Word("5,987.51", w.x0, w.x1, w.top) if w.text == "5,987.50" else w for w in pages[0]
    ]
    with pytest.raises(StatementError, match="transaction 2 \\(page 1, 03/11/2026\\)"):
        parse_cimb(pages)


def test_a_missing_last_page_is_rejected() -> None:
    with pytest.raises(StatementError, match="No CLOSING BALANCE"):
        parse_cimb(two_page_statement()[:1])


def test_a_document_without_the_table_is_rejected() -> None:
    with pytest.raises(StatementError, match="No OPENING BALANCE"):
        parse_cimb([page_top() + footer()])


def test_errors_never_contain_amounts_or_descriptions() -> None:
    pages = two_page_statement()
    pages[1] = [w for w in pages[1] if w.text != "5,879.50" or w.top != 228.1]
    with pytest.raises(StatementError) as error:
        parse_cimb(pages)
    message = str(error.value)
    assert "ATM" not in message and "5,879" not in message


@pytest.mark.parametrize(
    ("text", "sen"), [("0.01", 1), ("12.50", 1250), ("1,234.56", 123456), ("1,000,000.00", 10**8)]
)
def test_amounts_are_converted_exactly(text: str, sen: int) -> None:
    assert to_sen(text) == sen


def test_dedupe_keys_are_stable_and_tell_lookalikes_apart() -> None:
    account = uuid.uuid4()
    lines = parse_cimb(two_page_statement()).lines
    keys = dedupe_keys(account, lines)

    assert keys == dedupe_keys(account, lines)  # same statement, same keys
    assert len(set(keys)) == len(keys)
    assert dedupe_keys(uuid.uuid4(), lines) != keys  # scoped to the account
    # Identical lines (even the balance repeats) still get distinct keys.
    twin = (lines[0], lines[0])
    assert len(set(dedupe_keys(account, twin))) == 2


# --- the PDF layer ---------------------------------------------------------------------------


def make_pdf(password: str | None) -> bytes:
    """A real PDF with the statement's words at their positions (top-left origin, points)."""
    pdf = FPDF(unit="pt", format="A4")
    # 6pt: glyphs narrower than CHAR_WIDTH, so neighbouring words never touch and merge.
    pdf.set_font("Helvetica", size=6)
    if password is not None:
        pdf.set_encryption(
            owner_password=password + "-owner",
            user_password=password,
            encryption_method=EncryptionMethod.AES_256,
        )
    for page in two_page_statement():
        pdf.add_page()
        for w in page:
            pdf.text(w.x0, w.top + 6, w.text)  # text() takes the baseline
    return bytes(pdf.output())


def test_reads_a_password_protected_pdf() -> None:
    statement = parse_cimb(read_pdf(make_pdf("J@123456"), "J@123456"))
    assert len(statement.lines) == 4
    assert statement.closing_balance_sen == 587950


@pytest.mark.parametrize("password", [None, "J@654321"])
def test_wrong_or_missing_password(password: str | None) -> None:
    with pytest.raises(StatementPasswordError):
        read_pdf(make_pdf("J@123456"), password)


def test_not_a_pdf() -> None:
    with pytest.raises(StatementError):
        read_pdf(b"hello", None)
