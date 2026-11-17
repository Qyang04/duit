"""Categorisation rules: corrections become rules (spec section 4, first stage).

- A *merchant* rule says "merchant key X is category C". Every time the user sets a category by
  hand, a learned merchant rule is written for that transaction's merchant key.
- A *contains* rule says "a merchant key containing X is category C" ("GRAB" -> Transport).
  The user writes these.

The most specific rule wins: an exact merchant rule beats any contains rule, and a longer
contains pattern beats a shorter one. Rules only ever fill in transactions that the user has not
categorised: a category set by hand has category_rule_id NULL and is never touched again.

Applying rules is one SQL statement (Postgres does the work): pick the best rule per transaction,
update only rows whose result changed, and count the hits per rule.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from duit.auth.models import User
from duit.auth.router import CurrentUser
from duit.core.deps import Db
from duit.ledger.categories import find_category
from duit.ledger.common import Input, Patch, flush_or_conflict, not_found, unknown
from duit.ledger.models import CategoryRule, RuleMatch, RuleSource, Transaction

router = APIRouter(prefix="/rules", tags=["rules"])

# Which transactions to (re)consider. Fixed SQL fragments, never built from input.
Scope = Literal["all", "ids", "merchant_key"]
SCOPES: dict[Scope, str] = {
    "all": "",
    "ids": "AND t.id = ANY(:ids)",
    "merchant_key": "AND t.merchant_key = :merchant_key",
}

APPLY = """
WITH best AS (
    SELECT t.id, r.id AS rule_id, r.category_id
    FROM transactions t
    LEFT JOIN LATERAL (
        SELECT r.id, r.category_id
        FROM category_rules r
        WHERE r.user_id = t.user_id
          AND ((r.match_type = 'merchant' AND r.pattern = t.merchant_key)
            OR (r.match_type = 'contains' AND strpos(t.merchant_key, r.pattern) > 0))
        ORDER BY r.match_type = 'merchant' DESC, length(r.pattern) DESC, r.id
        LIMIT 1
    ) r ON true
    WHERE t.user_id = :user_id
      AND (t.category_id IS NULL OR t.category_rule_id IS NOT NULL)  -- never the user's own
      {scope}
),
changed AS (
    UPDATE transactions t
    SET category_id = best.category_id, category_rule_id = best.rule_id, updated_at = now()
    FROM best
    WHERE t.id = best.id
      AND (t.category_id, t.category_rule_id) IS DISTINCT FROM (best.category_id, best.rule_id)
    RETURNING best.rule_id
),
hits AS (
    UPDATE category_rules r
    SET hit_count = r.hit_count + c.n
    FROM (SELECT rule_id, count(*) AS n FROM changed GROUP BY rule_id) c
    WHERE r.id = c.rule_id
)
SELECT count(*) FROM changed
"""


async def apply_rules(
    db: AsyncSession,
    user_id: uuid.UUID,
    scope: Scope = "all",
    *,
    ids: list[uuid.UUID] | None = None,
    merchant_key: str | None = None,
) -> int:
    """(Re)categorise the user's rule-managed and uncategorised transactions in `scope`.
    A rule-set category whose rule no longer matches goes back to uncategorised.
    Returns how many transactions changed."""
    params: dict[str, Any] = {"user_id": user_id, "ids": ids, "merchant_key": merchant_key}
    changed = await db.scalar(text(APPLY.format(scope=SCOPES[scope])), params)
    return changed or 0


async def learn(db: AsyncSession, user_id: uuid.UUID, txn_id: uuid.UUID) -> int:
    """The user just categorised this transaction by hand: remember "this merchant is that
    category" as a learned merchant rule (the latest correction wins), then re-categorise the
    merchant's other rule-managed transactions. Returns how many of those changed."""
    txn = (
        await db.execute(
            select(Transaction.merchant_key, Transaction.category_id).where(
                Transaction.id == txn_id, Transaction.user_id == user_id
            )
        )
    ).one()
    if txn.category_id is None or txn.merchant_key == "":
        return 0  # cleared, or nothing to recognise the merchant by
    stmt = insert(CategoryRule).values(
        user_id=user_id,
        match_type=RuleMatch.MERCHANT,
        pattern=txn.merchant_key,
        category_id=txn.category_id,
        source=RuleSource.LEARNED,
    )
    await db.execute(
        stmt.on_conflict_do_update(
            constraint="uq_category_rules_user_id_match_type_pattern",
            set_={"category_id": stmt.excluded.category_id, "updated_at": text("now()")},
        )
    )
    return await apply_rules(db, user_id, "merchant_key", merchant_key=txn.merchant_key)


# --- API -------------------------------------------------------------------------------------

Pattern = Annotated[str, Field(min_length=1, max_length=200, examples=["GRAB"])]


class RuleIn(Input):
    match_type: RuleMatch = RuleMatch.CONTAINS
    pattern: Pattern
    category_id: uuid.UUID


class RuleUpdate(Patch):
    required = ("match_type", "pattern", "category_id")

    match_type: RuleMatch | None = None
    pattern: Pattern | None = None
    category_id: uuid.UUID | None = None


class RuleOut(BaseModel):
    id: uuid.UUID
    match_type: RuleMatch
    pattern: str  # normalised, as it is matched
    category_id: uuid.UUID
    source: RuleSource
    hit_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, r: CategoryRule) -> "RuleOut":
        return cls(
            id=r.id,
            match_type=r.match_type,
            pattern=r.pattern,
            category_id=r.category_id,
            source=r.source,
            hit_count=r.hit_count,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )


class RuleChange(BaseModel):
    rule: RuleOut | None  # null after a delete
    recategorised: int  # transactions whose category changed because of this


def invalid(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


async def normalised(db: Db, match_type: RuleMatch, pattern: str) -> str:
    """The pattern as a merchant key, computed by the same SQL function as the column."""
    key = str(await db.scalar(select(text("merchant_key(:p)")), {"p": pattern}) or "")
    if key == "":
        raise invalid("The pattern needs letters: digits and punctuation are ignored")
    if match_type is RuleMatch.CONTAINS and len(key) < 3:
        raise invalid("A 'contains' pattern needs at least 3 letters")
    return key


async def get_rule(db: Db, user: User, rule_id: uuid.UUID) -> CategoryRule:
    rule = await db.scalar(
        select(CategoryRule).where(CategoryRule.id == rule_id, CategoryRule.user_id == user.id)
    )
    if rule is None:
        raise not_found("Rule")
    return rule


async def check_category(db: Db, user: User, category_id: uuid.UUID) -> None:
    if await find_category(db, user, category_id) is None:
        raise unknown("category")


DUPLICATE = "A rule with this pattern already exists"
UNIQUE = "uq_category_rules_user_id_match_type_pattern"


@router.get("")
async def list_rules(user: CurrentUser, db: Db) -> list[RuleOut]:
    rules = await db.scalars(
        select(CategoryRule)
        .where(CategoryRule.user_id == user.id)
        .order_by(CategoryRule.hit_count.desc(), CategoryRule.pattern)
    )
    return [RuleOut.of(r) for r in rules]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_rule(body: RuleIn, user: CurrentUser, db: Db) -> RuleChange:
    await check_category(db, user, body.category_id)
    rule = CategoryRule(
        user_id=user.id,
        match_type=body.match_type,
        pattern=await normalised(db, body.match_type, body.pattern),
        category_id=body.category_id,
        source=RuleSource.USER,
    )
    db.add(rule)
    await flush_or_conflict(db, UNIQUE, DUPLICATE)
    changed = await apply_rules(db, user.id)
    await db.commit()
    await db.refresh(rule)  # hit_count was updated in SQL
    return RuleChange(rule=RuleOut.of(rule), recategorised=changed)


@router.patch("/{rule_id}")
async def update_rule(
    rule_id: uuid.UUID, body: RuleUpdate, user: CurrentUser, db: Db
) -> RuleChange:
    rule = await get_rule(db, user, rule_id)
    changes = body.changes()
    if "category_id" in changes:
        await check_category(db, user, changes["category_id"])
    match_type = changes.get("match_type", rule.match_type)
    if "pattern" in changes or "match_type" in changes:
        changes["pattern"] = await normalised(db, match_type, changes.get("pattern", rule.pattern))
    for name, value in changes.items():
        setattr(rule, name, value)
    await flush_or_conflict(db, UNIQUE, DUPLICATE)
    changed = await apply_rules(db, user.id)
    await db.commit()
    await db.refresh(rule)
    return RuleChange(rule=RuleOut.of(rule), recategorised=changed)


@router.delete("/{rule_id}")
async def delete_rule(rule_id: uuid.UUID, user: CurrentUser, db: Db) -> RuleChange:
    rule = await get_rule(db, user, rule_id)
    # What this rule categorised goes back to uncategorised (not to "set by the user", which
    # the foreign key's SET NULL alone would make it); other rules then get a chance at it.
    released = await db.scalars(
        update(Transaction)
        .where(Transaction.user_id == user.id, Transaction.category_rule_id == rule.id)
        .values(category_id=None, category_rule_id=None)
        .returning(Transaction.id)
    )
    ids = list(released)
    await db.delete(rule)
    await db.flush()
    await apply_rules(db, user.id, "ids", ids=ids)
    await db.commit()
    return RuleChange(rule=None, recategorised=len(ids))
