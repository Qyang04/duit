"""CIMB statement parser. Word positions mirror a real (masked) CIMB statement layout; all
names and numbers here are made up."""

import uuid
from datetime import date

import pytest

from duit.parsers.statements.base import (
    StatementError,
    StatementPasswordError,
    Word,
    dedupe_keys,
    read_pdf,
)
from duit.parsers.statements.cimb import parse_cimb, to_sen
from tests.fake_cimb import footer, make_pdf, page_top, two_page_statement


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
