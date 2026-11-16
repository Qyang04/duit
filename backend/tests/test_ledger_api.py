"""The ledger endpoints, end to end. Two logged-in users, Ana and Bob: the most important tests
prove that neither can read, change or reference the other's data."""

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from duit.core.crypto import decrypt, load_data_key
from duit.core.settings import get_settings
from duit.ledger.accounts import secret_aad
from duit.ledger.models import Account
from duit.main_api import app

pytestmark = pytest.mark.anyio

PASSWORD = "correct horse battery"


async def sign_in(http: httpx2.AsyncClient, email: str) -> None:
    creds = {"email": email, "password": PASSWORD}
    assert (await http.post("/auth/register", json=creds)).status_code == 201
    assert (await http.post("/auth/login", json=creds)).status_code == 200


@pytest.fixture
async def ana(client: httpx2.AsyncClient) -> httpx2.AsyncClient:
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(
        update={"registration_open": True}
    )
    await sign_in(client, "ana@example.com")
    return client


@pytest.fixture
async def bob(ana: httpx2.AsyncClient) -> AsyncGenerator[httpx2.AsyncClient]:
    """A second browser, with its own cookie jar, on the same app and test transaction."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="https://testserver") as http:
        await sign_in(http, "bob@example.com")
        yield http


async def new_account(http: httpx2.AsyncClient, **extra: Any) -> dict[str, Any]:
    response = await http.post("/accounts", json={"type": "bank", "name": "Savings", **extra})
    assert response.status_code == 201, response.text
    return response.json()


async def new_txn(http: httpx2.AsyncClient, account_id: str, **extra: Any) -> dict[str, Any]:
    body = {
        "account_id": account_id,
        "posted_on": "2026-11-01",
        "amount_sen": -1250,
        "merchant_raw": "GRAB* RIDE",
        **extra,
    }
    response = await http.post("/transactions", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def category_id(http: httpx2.AsyncClient, name: str) -> str:
    categories = (await http.get("/categories")).json()
    return next(c["id"] for c in categories if c["name"] == name)


# --- isolation between users -----------------------------------------------------------------


async def test_ledger_needs_a_login(client: httpx2.AsyncClient) -> None:
    for path in ("/accounts", "/categories", "/transactions"):
        assert (await client.get(path)).status_code == 401


async def test_another_users_rows_look_missing(
    ana: httpx2.AsyncClient, bob: httpx2.AsyncClient
) -> None:
    account = await new_account(ana)
    txn = await new_txn(ana, account["id"])
    category = await category_id(ana, "Fuel")

    for path in (
        f"/accounts/{account['id']}",
        f"/transactions/{txn['id']}",
        f"/categories/{category}",
    ):
        # 404, not 403: Bob cannot even tell that the id exists.
        assert (await bob.patch(path, json={})).status_code == 404
        assert (await bob.delete(path)).status_code == 404
    assert (await bob.get(f"/accounts/{account['id']}")).status_code == 404
    assert (await bob.get(f"/transactions/{txn['id']}")).status_code == 404
    # Ana's data is untouched.
    assert (await ana.get(f"/transactions/{txn['id']}")).status_code == 200


async def test_lists_show_only_your_own_rows(
    ana: httpx2.AsyncClient, bob: httpx2.AsyncClient
) -> None:
    await new_txn(ana, (await new_account(ana))["id"])

    assert (await bob.get("/accounts")).json() == []
    assert (await bob.get("/transactions")).json()["items"] == []
    ana_categories = {c["id"] for c in (await ana.get("/categories")).json()}
    bob_categories = {c["id"] for c in (await bob.get("/categories")).json()}
    assert ana_categories.isdisjoint(bob_categories)
    # Filtering by Ana's account id does not leak her rows either.
    ana_account = (await ana.get("/accounts")).json()[0]["id"]
    assert (await bob.get(f"/transactions?account_id={ana_account}")).json()["items"] == []


async def test_cannot_reference_another_users_account_or_category(
    ana: httpx2.AsyncClient, bob: httpx2.AsyncClient
) -> None:
    anas_account = (await new_account(ana))["id"]
    anas_fuel = await category_id(ana, "Fuel")
    bobs_account = (await new_account(bob))["id"]
    bobs_txn = await new_txn(bob, bobs_account)
    body = {"posted_on": "2026-11-01", "amount_sen": -100, "merchant_raw": "X"}

    create = await bob.post("/transactions", json={**body, "account_id": anas_account})
    assert create.status_code == 422
    move = await bob.patch(f"/transactions/{bobs_txn['id']}", json={"account_id": anas_account})
    assert move.status_code == 422
    tag = await bob.patch(f"/transactions/{bobs_txn['id']}", json={"category_id": anas_fuel})
    assert tag.status_code == 422
    nest = await bob.post("/categories", json={"name": "Mine", "parent_id": anas_fuel})
    assert nest.status_code == 422


async def test_cannot_set_columns_the_api_does_not_offer(ana: httpx2.AsyncClient) -> None:
    account = await new_account(ana)
    body = {
        "account_id": account["id"],
        "posted_on": "2026-11-01",
        "amount_sen": -100,
        "merchant_raw": "X",
        "user_id": str(uuid.uuid4()),  # mass assignment attempt
    }
    assert (await ana.post("/transactions", json=body)).status_code == 422


# --- accounts --------------------------------------------------------------------------------


async def test_account_secrets_are_encrypted_and_never_returned(
    ana: httpx2.AsyncClient, conn: AsyncConnection
) -> None:
    account = await new_account(
        ana, account_number="1234-5678 9012", statement_password="s3cret-pdf"
    )

    assert account["account_last4"] == "9012"
    assert account["has_account_number"] and account["has_statement_password"]
    assert "account_number" not in account and "statement_password" not in account
    columns = (Account.id, Account.account_number_enc, Account.statement_password_enc)
    row = (await conn.execute(select(*columns).where(Account.id == account["id"]))).one()
    assert row.account_number_enc is not None and row.statement_password_enc is not None
    assert b"123456789012" not in row.account_number_enc
    key = load_data_key(get_settings())
    aad = secret_aad("account_number", row.id)
    assert decrypt(key, row.account_number_enc, aad) == b"123456789012"


async def test_account_secrets_can_be_removed(ana: httpx2.AsyncClient) -> None:
    account = await new_account(ana, account_number="12345678", statement_password="pw")

    response = await ana.patch(
        f"/accounts/{account['id']}", json={"account_number": None, "statement_password": None}
    )

    assert response.json()["has_account_number"] is False
    assert response.json()["has_statement_password"] is False
    assert response.json()["account_last4"] is None


async def test_account_names_are_unique_per_user(
    ana: httpx2.AsyncClient, bob: httpx2.AsyncClient
) -> None:
    await new_account(ana)
    duplicate = await ana.post("/accounts", json={"type": "card", "name": "Savings"})
    assert duplicate.status_code == 409
    await new_account(bob)  # the same name is fine for another user


async def test_required_fields_cannot_be_patched_to_null(ana: httpx2.AsyncClient) -> None:
    account = await new_account(ana)
    response = await ana.patch(f"/accounts/{account['id']}", json={"name": None})
    assert response.status_code == 422


async def test_an_account_with_transactions_cannot_be_deleted(ana: httpx2.AsyncClient) -> None:
    account = await new_account(ana)
    txn = await new_txn(ana, account["id"])

    assert (await ana.delete(f"/accounts/{account['id']}")).status_code == 409
    assert (await ana.delete(f"/transactions/{txn['id']}")).status_code == 204
    assert (await ana.delete(f"/accounts/{account['id']}")).status_code == 204


# --- categories ------------------------------------------------------------------------------


async def test_subcategories_inherit_is_income(ana: httpx2.AsyncClient) -> None:
    income = await category_id(ana, "Income")
    response = await ana.post(
        "/categories", json={"name": "Freelance", "parent_id": income, "is_income": False}
    )
    assert response.status_code == 201
    assert response.json()["is_income"] is True


async def test_changing_a_parents_is_income_changes_its_children(
    ana: httpx2.AsyncClient,
) -> None:
    family = await category_id(ana, "Family & giving")
    await ana.patch(f"/categories/{family}", json={"is_income": True})

    categories = (await ana.get("/categories")).json()
    children = [c for c in categories if c["parent_id"] == family]
    assert children and all(c["is_income"] for c in children)


async def test_categories_are_two_levels_deep(ana: httpx2.AsyncClient) -> None:
    fuel = await category_id(ana, "Fuel")  # already a subcategory
    transport = await category_id(ana, "Transport")  # has subcategories
    food = await category_id(ana, "Food & drink")

    under_child = await ana.post("/categories", json={"name": "Petrol", "parent_id": fuel})
    assert under_child.status_code == 422
    move_parent = await ana.patch(f"/categories/{transport}", json={"parent_id": food})
    assert move_parent.status_code == 422
    own_parent = await ana.patch(f"/categories/{food}", json={"parent_id": food})
    assert own_parent.status_code == 422


async def test_rename_and_move_a_category(ana: httpx2.AsyncClient) -> None:
    fuel = await category_id(ana, "Fuel")
    car = await category_id(ana, "Car")

    renamed = await ana.patch(f"/categories/{fuel}", json={"name": "Petrol"})
    assert renamed.json()["name"] == "Petrol"
    # Promote "Car" to a top-level category.
    promoted = await ana.patch(f"/categories/{car}", json={"parent_id": None})
    assert promoted.json()["parent_id"] is None
    # Sibling names are unique.
    clash = await ana.patch(f"/categories/{fuel}", json={"name": "E-hailing"})
    assert clash.status_code == 409


async def test_deleting_a_category_uncategorises_transactions(ana: httpx2.AsyncClient) -> None:
    fuel = await category_id(ana, "Fuel")
    transport = await category_id(ana, "Transport")
    txn = await new_txn(ana, (await new_account(ana))["id"], category_id=fuel)

    assert (await ana.delete(f"/categories/{transport}")).status_code == 409  # has children
    assert (await ana.delete(f"/categories/{fuel}")).status_code == 204
    assert (await ana.get(f"/transactions/{txn['id']}")).json()["category_id"] is None


# --- transactions ----------------------------------------------------------------------------


async def test_currency_defaults_to_the_accounts(ana: httpx2.AsyncClient) -> None:
    txn = await new_txn(ana, (await new_account(ana))["id"])
    assert txn["currency"] == "MYR"
    assert txn["fx_rate"] is None


async def test_foreign_currency_needs_an_fx_rate(ana: httpx2.AsyncClient) -> None:
    account = (await new_account(ana, type="card", name="Visa"))["id"]
    body = {"account_id": account, "posted_on": "2026-11-01", "amount_sen": -999}
    body |= {"merchant_raw": "NETFLIX.COM", "currency": "USD"}

    assert (await ana.post("/transactions", json=body)).status_code == 422
    ok = await ana.post("/transactions", json={**body, "fx_rate": "4.4712"})
    assert ok.status_code == 201
    # Decimal travels as a JSON string, so there is no float rounding on the way.
    assert Decimal(ok.json()["fx_rate"]) == Decimal("4.4712")
    same = await ana.post("/transactions", json={**body, "currency": "MYR", "fx_rate": "1"})
    assert same.status_code == 422


async def test_editing_the_category_bumps_updated_at(ana: httpx2.AsyncClient) -> None:
    txn = await new_txn(ana, (await new_account(ana))["id"])
    fuel = await category_id(ana, "Fuel")

    response = await ana.patch(f"/transactions/{txn['id']}", json={"category_id": fuel})

    assert response.status_code == 200
    assert response.json()["category_id"] == fuel
    assert response.json()["updated_at"] >= txn["updated_at"]


async def test_pages_cover_every_row_once_newest_first(ana: httpx2.AsyncClient) -> None:
    account = (await new_account(ana))["id"]
    created = [
        (await new_txn(ana, account, posted_on=f"2026-11-{day:02d}"))["id"]
        for day in (1, 2, 2, 3, 4)  # a tie on the 2nd: the id breaks it
    ]

    seen: list[dict[str, Any]] = []
    cursor = None
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = (await ana.get("/transactions", params=params)).json()
        seen += page["items"]
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert sorted(t["id"] for t in seen) == sorted(created)  # no gaps, no duplicates
    dates = [t["posted_on"] for t in seen]
    assert dates == sorted(dates, reverse=True)


async def test_bad_cursor_is_rejected(ana: httpx2.AsyncClient) -> None:
    assert (await ana.get("/transactions?cursor=not-a-cursor")).status_code == 422


async def test_filters(ana: httpx2.AsyncClient) -> None:
    account = (await new_account(ana))["id"]
    fuel = await category_id(ana, "Fuel")
    transport = await category_id(ana, "Transport")
    groceries = await category_id(ana, "Groceries")
    await new_txn(
        ana, account, category_id=fuel, merchant_raw="PETRONAS 100%", posted_on="2026-10-30"
    )
    await new_txn(ana, account, category_id=groceries, merchant_raw="JAYA GROCER")
    await new_txn(ana, account, merchant_raw="UNKNOWN SHOP")

    async def merchants(query: str) -> set[str]:
        page = (await ana.get(f"/transactions?{query}")).json()
        return {t["merchant_raw"] for t in page["items"]}

    assert await merchants(f"category_id={transport}") == {"PETRONAS 100%"}  # parent includes child
    assert await merchants("uncategorised=true") == {"UNKNOWN SHOP"}
    assert await merchants("date_from=2026-11-01") == {"JAYA GROCER", "UNKNOWN SHOP"}
    assert await merchants("date_to=2026-10-31") == {"PETRONAS 100%"}
    assert await merchants("q=grocer") == {"JAYA GROCER"}
    # % is searched literally, not as a wildcard that matches everything.
    assert await merchants("q=0%25") == {"PETRONAS 100%"}
