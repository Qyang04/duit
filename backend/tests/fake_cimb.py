"""A fake CIMB statement: word positions mirror a real (masked) CIMB layout; every name and
number is made up. Shared by the parser and import tests."""

from fpdf import FPDF
from fpdf.enums import EncryptionMethod

from duit.parsers.statements.base import Word

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


def make_pdf(password: str | None, title: str = "Statement") -> bytes:
    """A real PDF with the statement's words at their positions (top-left origin, points)."""
    pdf = FPDF(unit="pt", format="A4")
    pdf.set_title(title)  # a different title = different bytes, same statement
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
