"""Print the layout of a bank statement PDF with the personal data masked.

For writing a parser without seeing anyone's real statement. Run it locally on your own file:

    uv run python tools/statement_layout.py "C:/path/to/statement.pdf" > layout.txt

The PDF password is asked for interactively (not echoed, not in shell history, never printed).
Every digit becomes 9 and every letter x/X, so formats survive (99/99/9999, 9,999.99) but values
do not; only the generic words in KEEP (column headers, transaction types) stay readable.
READ layout.txt BEFORE SHARING IT and mask anything that still looks personal.
"""

import argparse
import getpass
import re
import sys
from collections import defaultdict

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfplumber.page import Page
from pdfplumber.utils.exceptions import PdfminerException

# Bank-generated words, not personal data: headers, labels and transaction types (EN + BM).
KEEP = frozenset(
    {
        "DATE",
        "TARIKH",
        "DESCRIPTION",
        "DISKRIPSI",
        "KETERANGAN",
        "BUTIRAN",
        "TRANSACTION",
        "TRANSAKSI",
        "DETAILS",
        "CHEQUE",
        "CEK",
        "REF",
        "REFERENCE",
        "RUJUKAN",
        "NO",
        "WITHDRAWAL",
        "WITHDRAWALS",
        "PENGELUARAN",
        "DEPOSIT",
        "DEPOSITS",
        "SIMPANAN",
        "TAX",
        "CUKAI",
        "BALANCE",
        "BAKI",
        "AMOUNT",
        "AMAUN",
        "JUMLAH",
        "TOTAL",
        "RM",
        "MYR",
        "DR",
        "CR",
        "DEBIT",
        "CREDIT",
        "KREDIT",
        "OPENING",
        "CLOSING",
        "BEGINNING",
        "ENDING",
        "BROUGHT",
        "FORWARD",
        "CARRIED",
        "STATEMENT",
        "PENYATA",
        "ACCOUNT",
        "AKAUN",
        "SAVINGS",
        "CURRENT",
        "SEMASA",
        "PAGE",
        "MUKA",
        "SURAT",
        "OF",
        "DARI",
        "PERIOD",
        "TEMPOH",
        "FROM",
        "TO",
        "KE",
        "CIMB",
        "BANK",
        "BERHAD",
        "ISLAMIC",
        "TRANSFER",
        "DUITNOW",
        "IBG",
        "FPX",
        "ATM",
        "POS",
        "PAYMENT",
        "BAYARAN",
        "INTEREST",
        "FAEDAH",
        "PROFIT",
        "HIBAH",
        "INSTANT",
        "FUND",
        "CARD",
        "KAD",
        "ONLINE",
        "BILL",
        "JOMPAY",
        "QR",
        "REVERSAL",
        "CHARGES",
        "CHARGE",
        "FEE",
        "FEES",
        "SERVICE",
        "SALARY",
        "GAJI",
        "CASH",
        "A/C",
        "IBK",
        "MOBILE",
        "CLICKS",
        "OCTO",
        "RECEIVED",
        "SENT",
        "INTERBANK",
        "DUITNOWQR",
        "PURCHASE",
        "RETAIL",
        "REFUND",
        "STAMP",
        "DUTY",
    }
)
LINE_TOLERANCE = 3.0  # words whose tops differ by less than this (points) share a line
CHARS_PER_POINT = 0.18  # horizontal scale of the text rendering


def mask(word: str) -> str:
    if word.upper().strip(":.,()") in KEEP:
        return word
    word = re.sub(r"[0-9]", "9", word)
    word = re.sub(r"[a-z]", "x", word)
    return re.sub(r"[A-Z]", "X", word)


def render(page: Page) -> list[str]:
    lines: dict[float, list[tuple[float, float, str]]] = defaultdict(list)
    for w in page.extract_words():
        top = next((t for t in lines if abs(t - w["top"]) < LINE_TOLERANCE), w["top"])
        lines[top].append((w["x0"], w["x1"], mask(w["text"])))
    out: list[str] = []
    for top in sorted(lines):
        text = ""
        for x0, _, word in sorted(lines[top]):
            col = int(x0 * CHARS_PER_POINT)
            text = (text + " " if text and len(text) >= col else text.ljust(col)) + word
        out.append(f"{top:6.1f} | {text}")
    return out


def headers(page: Page) -> list[str]:
    """Exact x positions of the readable words: the parser places columns from these."""
    return [
        f"  top={w['top']:6.1f} x0={w['x0']:6.1f} x1={w['x1']:6.1f}  {w['text']}"
        for w in page.extract_words()
        if w["text"].upper().strip(":.,()") in KEEP
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a statement PDF's layout, data masked.")
    parser.add_argument("pdf")
    parser.add_argument("--pages", default="1,2,-1", help="1-based, negative from the end")
    args = parser.parse_args()

    if sys.stdin.isatty():
        password = getpass.getpass("PDF password (empty if none): ")
    else:  # piped (automated tests with fake PDFs); run it in PowerShell for real statements
        password = sys.stdin.readline().rstrip("\r\n")
    try:
        pdf = pdfplumber.open(args.pdf, password=password)
    except PdfminerException as exc:
        if isinstance(exc.args[0] if exc.args else None, PDFPasswordIncorrect):
            sys.exit("Wrong password.")
        raise
    with pdf:
        total = len(pdf.pages)
        wanted: list[int] = []
        for n in (int(p) for p in args.pages.split(",")):
            index = n - 1 if n > 0 else total + n
            if 0 <= index < total and index not in wanted:
                wanted.append(index)
        print(f"pages: {total}, size: {pdf.pages[0].width:.0f} x {pdf.pages[0].height:.0f} pt")
        for index in wanted:
            page = pdf.pages[index]
            print(f"\n===== page {index + 1} of {total}: masked text (top | line) =====")
            print("\n".join(render(page)))
            print(f"\n===== page {index + 1}: readable words with exact positions =====")
            print("\n".join(headers(page)))


if __name__ == "__main__":
    main()
