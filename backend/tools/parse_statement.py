"""Run the CIMB parser on your own statement, locally. Prints a summary that is safe to share
(counts and checks only, no amounts or names):

    uv run python -m tools.parse_statement "C:/path/to/statement.pdf"   (from backend/)

--show also prints every parsed transaction, for you to compare with the PDF yourself.
Do not paste the --show output anywhere: it is your real data.
"""

import argparse
import getpass
import sys
from collections import Counter
from pathlib import Path

from duit.parsers.statements.base import StatementError, read_pdf
from duit.parsers.statements.cimb import parse_cimb


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse a CIMB statement PDF locally.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--show", action="store_true", help="print transactions (private)")
    args = parser.parse_args()

    if sys.stdin.isatty():
        password = getpass.getpass("PDF password (empty if none): ")
    else:  # piped (automated tests with fake PDFs); run it in PowerShell for real statements
        password = next(iter(sys.stdin.readline().splitlines()), "")
    try:
        pages = read_pdf(args.pdf.read_bytes(), password)
        statement = parse_cimb(pages)
    except StatementError as exc:
        sys.exit(f"FAILED: {exc}")

    lines = statement.lines
    print(f"pages: {len(pages)}")
    print(f"transactions: {len(lines)}  (in: {sum(t.amount_sen > 0 for t in lines)}, ", end="")
    print(f"out: {sum(t.amount_sen < 0 for t in lines)})")
    print(f"per page: {dict(Counter(t.page for t in lines))}")
    if lines:
        print(f"dates: {lines[0].posted_on} .. {lines[-1].posted_on}")
    print(f"with a ref no: {sum(t.ref is not None for t in lines)}")
    print(f"description lines: {dict(Counter(t.description.count(chr(10)) + 1 for t in lines))}")
    print("reconciled: opening + every transaction = every printed balance and the closing balance")

    if args.show:
        print("\n--- PRIVATE: do not share below this line ---")
        for t in lines:
            first, *rest = t.description.split("\n")
            amount, balance = t.amount_sen / 100, t.balance_sen / 100  # display only
            print(f"{t.posted_on}  {amount:>12,.2f}  {balance:>12,.2f}  {first}")
            for more in rest:
                print(f"{'':>40}{more}")


if __name__ == "__main__":
    main()
