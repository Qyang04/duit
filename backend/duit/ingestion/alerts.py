"""An alert email that a template read becomes a pending transaction.

Pending: an alert is sent the moment money moves, before the bank books it; the statement line
for the same money confirms it later (merging the two is Week 6's deduplication). Everything
happens in the caller's transaction, together with the document's status.
"""

import uuid
from dataclasses import asdict

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from duit.ingestion.documents import link_sources, record_extraction
from duit.ingestion.models import RawDocument
from duit.ledger.models import Account, Transaction, TransactionStatus
from duit.ledger.rules import apply_rules
from duit.parsers.alerts.engine import Alert


async def find_account(db: AsyncSession, user_id: uuid.UUID, alert: Alert) -> Account | None:
    """The user's one account at the template's institution (and with the alert's last 4
    digits, when the alert gives them). None when there is no such account, or several."""
    query = select(Account).where(
        Account.user_id == user_id,
        func.lower(Account.institution) == alert.template.institution.lower(),
    )
    if alert.last4:
        query = query.where(Account.account_last4 == alert.last4)
    accounts = (await db.scalars(query.limit(2))).all()
    return accounts[0] if len(accounts) == 1 else None


async def record_alert(db: AsyncSession, document: RawDocument, alert: Alert) -> str | None:
    """Add the alert's transaction; returns why not, or None when done. The reason is shown to
    the user and logged, so it never contains the amount, the merchant or the digits."""
    template = alert.template
    account = await find_account(db, document.user_id, alert)
    if account is None:
        ending = " with the alert's last 4 digits" if alert.last4 else ""
        return (
            f"no single account with institution '{template.institution}'{ending}: "
            "create it (or fix it), then process the email again"
        )
    document.account_id = account.id

    fields = asdict(alert) | {"template": template.name}
    fields["posted_on"] = alert.posted_on.isoformat()
    fields["occurred_at"] = alert.occurred_at.isoformat() if alert.occurred_at else None
    extraction_id = await record_extraction(
        db,
        document.user_id,
        document.id,
        parser=f"alert:{template.name}",
        parser_version=str(template.version),
        fields=fields,
    )
    dedupe_key = f"alert:{document.id}"  # one transaction per alert, however often it is read
    inserted = await db.scalar(
        insert(Transaction)
        .values(
            user_id=document.user_id,
            account_id=account.id,
            posted_on=alert.posted_on,
            amount_sen=alert.amount_sen,
            currency=account.currency,
            merchant_raw=alert.merchant_raw,
            status=TransactionStatus.PENDING,
            dedupe_key=dedupe_key,
        )
        .on_conflict_do_nothing(constraint="uq_transactions_account_id_dedupe_key")
        .returning(Transaction.id)
    )
    if inserted is None:  # read before (e.g. by an older template version): link it again
        existing = await db.scalar(
            select(Transaction.id).where(
                Transaction.account_id == account.id, Transaction.dedupe_key == dedupe_key
            )
        )
        await link_sources(db, document.user_id, extraction_id, [existing] if existing else [])
        return None
    await link_sources(db, document.user_id, extraction_id, [inserted])
    await apply_rules(db, document.user_id, "ids", ids=[inserted])
    return None
