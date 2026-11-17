"""Alert email templates: data (templates/*.yaml), not code.

A template names the sender, the domain that must have signed the email (DKIM), and regular
expressions for the subject and the body text (duit.parsers.alerts.text) with named groups:

    amount       required, e.g. 1,234.56
    last4        optional, the account's last 4 digits (which account it is)
    counterparty optional, who paid or was paid (becomes the merchant)
    date, time   optional, parsed with date_format; otherwise the email's Date header

Trust: anyone can send an email that says "From: alerts@gxbank.my". The receiving mail server
(Gmail) checks the DKIM signatures and writes the results into the topmost
Authentication-Results header. Only that header is read, and only if the server that wrote it is
ours (authserv_id): a sender can add headers of its own, but never above the ones the receiving
server adds on arrival. A template applies only when its own domain's signature passed.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from email.utils import parseaddr
from functools import cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from duit.parsers.alerts.text import body_lines

TEMPLATES = Path(__file__).parent / "templates"
# Malaysia: UTC+8 all year (no daylight saving). Used for the booking date of alerts without one.
MYT = timezone(timedelta(hours=8), "MYT")


class Template(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str  # unique, e.g. "gx-received"; the extraction's parser is "alert:<name>"
    version: int  # bump on any change, so old and new extractions stay apart
    sender: str  # the From address, exactly
    dkim: str  # this domain (or a subdomain of it) must have a passing signature
    institution: str  # the Duit account it belongs to: accounts.institution
    direction: Literal["in", "out"]  # money in (positive amount) or out (negative)
    subject: str  # regex, searched in the Subject
    body: str  # regex, searched in the body text (multi-line: ^ and $ match per line)
    date_format: str | None = None  # strptime format for "<date> <time>" or "<date>"
    merchant: str | None = None  # the merchant when the body names none (no counterparty)

    @field_validator("sender", "dkim")
    @classmethod
    def lowercase(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def check_patterns(self) -> "Template":
        groups = set(re.compile(self.body, re.MULTILINE).groupindex)
        re.compile(self.subject)
        if "amount" not in groups:
            raise ValueError(f"{self.name}: the body pattern needs an (?P<amount>...) group")
        if "date" in groups and not self.date_format:
            raise ValueError(f"{self.name}: a date group needs date_format")
        if "counterparty" not in groups and not self.merchant:
            raise ValueError(f"{self.name}: needs a counterparty group or a merchant")
        return self


@cache
def templates() -> tuple[Template, ...]:
    loaded = [
        Template.model_validate(item)
        for path in sorted(TEMPLATES.glob("*.yaml"))
        for item in yaml.safe_load(path.read_text(encoding="utf-8"))
    ]
    names = [t.name for t in loaded]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ValueError(f"duplicate alert template names: {sorted(duplicates)}")
    return tuple(loaded)


# --- outcomes --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Alert:
    template: Template
    amount_sen: int  # signed: negative = money out
    posted_on: date
    occurred_at: datetime | None  # when the alert says it happened, if it says
    last4: str | None
    merchant_raw: str


@dataclass(frozen=True)
class NoTemplate:
    """Nobody we have a template for sent this (a newsletter, a Google security email)."""

    sender_domain: str


@dataclass(frozen=True)
class Rejected:
    """Claims to be from a known sender, and is not: no passing signature from its domain.
    Never contains content from the email."""

    reason: str


@dataclass(frozen=True)
class NoMatch:
    """A real email from a known sender that no template reads: a new kind of alert, or the
    bank changed its layout."""

    sender: str


Outcome = Alert | NoTemplate | Rejected | NoMatch


# --- matching --------------------------------------------------------------------------------


def dkim_passes(message: EmailMessage, authserv_id: str) -> set[str]:
    """Domains with a passing DKIM signature, as our receiving server recorded them."""
    results = message.get_all("Authentication-Results") or []
    if not results:
        return set()
    top = " ".join(str(results[0]).split())
    server, _, rest = top.partition(";")
    if server.strip().lower() != authserv_id.lower():
        return set()
    passed: set[str] = set()
    for result in rest.split(";"):
        result = result.strip().lower()
        if not result.startswith("dkim=pass"):
            continue
        found = re.search(r"header\.(?:d=|i=[^@\s]*@)([a-z0-9.-]+)", result)
        if found:
            passed.add(found[1])
    return passed


def signed_by(domain: str, passed: set[str]) -> bool:
    return any(d == domain or d.endswith("." + domain) for d in passed)


def amount_sen(text: str) -> int:
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("unreadable amount") from exc
    if value <= 0 or value != value.quantize(Decimal("0.01")):
        raise ValueError("unreadable amount")
    return int(value * 100)


def match(message: EmailMessage, authserv_id: str, catalogue: tuple[Template, ...]) -> Outcome:
    sender = parseaddr(str(message.get("From", "")))[1].lower()
    candidates = [t for t in catalogue if t.sender == sender]
    if not candidates:
        return NoTemplate(sender.rpartition("@")[2] or "?")
    passed = dkim_passes(message, authserv_id)
    candidates = [t for t in candidates if signed_by(t.dkim, passed)]
    if not candidates:
        return Rejected(f"from {sender} without a passing DKIM signature from its domain")

    subject = " ".join(str(message.get("Subject", "")).split())
    text = "\n".join(body_lines(message))
    for template in candidates:
        if not re.search(template.subject, subject):
            continue
        found = re.search(template.body, text, re.MULTILINE)
        if found is None:
            continue
        groups = found.groupdict()
        sen = amount_sen(groups["amount"])
        occurred_at = None
        if groups.get("date") and template.date_format:
            stamp = " ".join(filter(None, (groups["date"], groups.get("time"))))
            occurred_at = datetime.strptime(stamp, template.date_format).replace(tzinfo=MYT)
            posted_on = occurred_at.date()
        else:
            sent = message["Date"].datetime if message["Date"] else None  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
            moment: datetime = sent if isinstance(sent, datetime) else datetime.now(UTC)
            posted_on = moment.astimezone(MYT).date()
        counterparty = (groups.get("counterparty") or "").strip()
        return Alert(
            template=template,
            amount_sen=sen if template.direction == "in" else -sen,
            posted_on=posted_on,
            occurred_at=occurred_at,
            last4=groups.get("last4"),
            merchant_raw=counterparty or template.merchant or "",
        )
    return NoMatch(sender)
