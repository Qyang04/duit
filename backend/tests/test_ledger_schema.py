"""Ledger guarantees the database enforces on its own, even if app code has a bug.

The most important ones: a row can never point at another user's account or category."""

import uuid
from datetime import date
from typing import Any

import pytest
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from duit.auth.models import User
from duit.ledger.defaults import DEFAULT_CATEGORIES, seed_default_categories
from duit.ledger.models import Account, Category, Transaction

pytestmark = pytest.mark.anyio


async def add_user(conn: AsyncConnection, email: str) -> uuid.UUID:
    return (
        await conn.execute(insert(User).values(email=email, password_hash="x").returning(User.id))
    ).scalar_one()


async def add_account(
    conn: AsyncConnection, user_id: uuid.UUID, name: str = "Savings"
) -> uuid.UUID:
    return (
        await conn.execute(
            insert(Account).values(user_id=user_id, type="bank", name=name).returning(Account.id)
        )
    ).scalar_one()


async def add_category(
    conn: AsyncConnection, user_id: uuid.UUID, name: str, parent_id: uuid.UUID | None = None
) -> uuid.UUID:
    return (
        await conn.execute(
            insert(Category)
            .values(user_id=user_id, name=name, parent_id=parent_id)
            .returning(Category.id)
        )
    ).scalar_one()


async def add_transaction(
    conn: AsyncConnection, user_id: uuid.UUID, account_id: uuid.UUID, **extra: Any
) -> uuid.UUID:
    values: dict[str, Any] = {
        "user_id": user_id,
        "account_id": account_id,
        "posted_on": date(2026, 11, 1),
        "amount_sen": -1250,  # RM 12.50 out
        "currency": "MYR",
        "merchant_raw": "GRAB* RIDE",
        **extra,
    }
    return (
        await conn.execute(insert(Transaction).values(values).returning(Transaction.id))
    ).scalar_one()


@pytest.fixture
async def ana(conn: AsyncConnection) -> uuid.UUID:
    return await add_user(conn, "ana@example.com")


@pytest.fixture
async def bob(conn: AsyncConnection) -> uuid.UUID:
    return await add_user(conn, "bob@example.com")


# --- tenant isolation ------------------------------------------------------------------------


async def test_transaction_cannot_use_another_users_account(
    conn: AsyncConnection, ana: uuid.UUID, bob: uuid.UUID
) -> None:
    bobs_account = await add_account(conn, bob)
    with pytest.raises(IntegrityError, match="fk_transactions_user_id_account_id_accounts"):
        await add_transaction(conn, ana, bobs_account)


async def test_transaction_cannot_use_another_users_category(
    conn: AsyncConnection, ana: uuid.UUID, bob: uuid.UUID
) -> None:
    account = await add_account(conn, ana)
    bobs_category = await add_category(conn, bob, "Fuel")
    with pytest.raises(IntegrityError, match="fk_transactions_user_id_category_id_categories"):
        await add_transaction(conn, ana, account, category_id=bobs_category)


async def test_category_parent_must_belong_to_the_same_user(
    conn: AsyncConnection, ana: uuid.UUID, bob: uuid.UUID
) -> None:
    bobs_parent = await add_category(conn, bob, "Transport")
    with pytest.raises(IntegrityError, match="fk_categories_user_id_parent_id_categories"):
        await add_category(conn, ana, "Fuel", parent_id=bobs_parent)


# --- delete rules ----------------------------------------------------------------------------


async def test_deleting_a_category_uncategorises_its_transactions(
    conn: AsyncConnection, ana: uuid.UUID
) -> None:
    account = await add_account(conn, ana)
    category = await add_category(conn, ana, "Transport")
    txn = await add_transaction(conn, ana, account, category_id=category)

    await conn.execute(delete(Category).where(Category.id == category))

    row = (
        await conn.execute(
            select(Transaction.category_id, Transaction.user_id).where(Transaction.id == txn)
        )
    ).one()
    assert row.category_id is None
    assert row.user_id == ana  # SET NULL (category_id) left the other key column alone


async def test_an_account_with_transactions_cannot_be_deleted(
    conn: AsyncConnection, ana: uuid.UUID
) -> None:
    account = await add_account(conn, ana)
    await add_transaction(conn, ana, account)
    with pytest.raises(IntegrityError, match="fk_transactions_user_id_account_id_accounts"):
        await conn.execute(delete(Account).where(Account.id == account))


async def test_a_parent_category_with_children_cannot_be_deleted(
    conn: AsyncConnection, ana: uuid.UUID
) -> None:
    parent = await add_category(conn, ana, "Transport")
    await add_category(conn, ana, "Fuel", parent_id=parent)
    with pytest.raises(IntegrityError, match="fk_categories_user_id_parent_id_categories"):
        await conn.execute(delete(Category).where(Category.id == parent))


async def test_deleting_a_user_deletes_their_whole_ledger(
    conn: AsyncConnection, ana: uuid.UUID
) -> None:
    account = await add_account(conn, ana)
    category = await add_category(conn, ana, "Transport")
    await add_transaction(conn, ana, account, category_id=category)

    await conn.execute(delete(User).where(User.id == ana))

    for model in (Account, Category, Transaction):
        assert await conn.scalar(select(func.count()).select_from(model)) == 0


# --- data integrity --------------------------------------------------------------------------


async def test_the_same_statement_line_cannot_be_imported_twice(
    conn: AsyncConnection, ana: uuid.UUID
) -> None:
    account = await add_account(conn, ana)
    await add_transaction(conn, ana, account, dedupe_key="line-1")
    with pytest.raises(IntegrityError, match="uq_transactions_account_id_dedupe_key"):
        await add_transaction(conn, ana, account, dedupe_key="line-1")


async def test_manual_transactions_need_no_dedupe_key(
    conn: AsyncConnection, ana: uuid.UUID
) -> None:
    account = await add_account(conn, ana)
    await add_transaction(conn, ana, account)
    await add_transaction(conn, ana, account)  # two NULL keys do not collide


async def test_top_level_category_names_are_unique(conn: AsyncConnection, ana: uuid.UUID) -> None:
    await add_category(conn, ana, "Transport")
    # parent_id is NULL for both: only NULLS NOT DISTINCT makes this a duplicate.
    with pytest.raises(IntegrityError, match="uq_categories_user_id_parent_id_name"):
        await add_category(conn, ana, "Transport")


@pytest.mark.parametrize("bad", ["myr", "RM", "MYRR"])
async def test_currency_must_be_an_iso_code(
    conn: AsyncConnection, ana: uuid.UUID, bad: str
) -> None:
    account = await add_account(conn, ana)
    with pytest.raises(IntegrityError, match="ck_transactions_currency_iso"):
        await add_transaction(conn, ana, account, currency=bad)


async def test_account_type_is_checked(conn: AsyncConnection, ana: uuid.UUID) -> None:
    with pytest.raises(IntegrityError, match="ck_accounts_account_type"):
        await conn.execute(insert(Account).values(user_id=ana, type="crypto", name="X"))


# --- default categories ----------------------------------------------------------------------


async def test_default_categories_are_seeded_as_a_two_level_tree(
    conn: AsyncConnection, ana: uuid.UUID
) -> None:
    async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as db:
        await seed_default_categories(db, ana)
        await db.commit()  # releases the savepoint; closing without it would roll back

    parents = await conn.scalar(
        select(func.count()).where(Category.user_id == ana, Category.parent_id.is_(None))
    )
    children = await conn.scalar(
        select(func.count()).where(Category.user_id == ana, Category.parent_id.is_not(None))
    )
    assert parents == len(DEFAULT_CATEGORIES)
    assert children == sum(len(c) for _, _, c in DEFAULT_CATEGORIES)
    salary_is_income = await conn.scalar(
        select(Category.is_income).where(Category.user_id == ana, Category.name == "Salary")
    )
    assert salary_is_income is True
