"""Categorisation rules: corrections become rules, the most specific rule wins, and a category
the user set by hand is never overwritten."""

from typing import Any

import httpx2
import pytest

from tests.fake_cimb import make_pdf
from tests.test_ledger_api import category_id, new_account, new_txn

pytestmark = pytest.mark.anyio


async def txn(http: httpx2.AsyncClient, txn_id: str) -> dict[str, Any]:
    return (await http.get(f"/api/transactions/{txn_id}")).json()


async def stamp(http: httpx2.AsyncClient, txn_id: str, category: str | None) -> dict[str, Any]:
    response = await http.patch(f"/api/transactions/{txn_id}", json={"category_id": category})
    assert response.status_code == 200, response.text
    return response.json()


async def new_rule(http: httpx2.AsyncClient, **body: Any) -> dict[str, Any]:
    response = await http.post("/api/rules", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def rules(http: httpx2.AsyncClient) -> list[dict[str, Any]]:
    return (await http.get("/api/rules")).json()


@pytest.fixture
async def account(ana: httpx2.AsyncClient) -> str:
    return (await new_account(ana))["id"]


# --- the merchant key ------------------------------------------------------------------------


async def test_merchant_key_ignores_dates_times_and_punctuation(
    ana: httpx2.AsyncClient, account: str
) -> None:
    a = await new_txn(ana, account, merchant_raw="POS DEBIT\nKK SUPER MART\n31/10/2026 2359")
    b = await new_txn(ana, account, merchant_raw="POS DEBIT\nKK SUPER-MART\n02/11/2026 0815")
    groceries = await category_id(ana, "Groceries")
    await stamp(ana, a["id"], groceries)
    assert (await txn(ana, b["id"]))["category_id"] == groceries


# --- corrections become rules ----------------------------------------------------------------


async def test_a_correction_teaches_a_rule_and_stamps_the_same_merchant(
    ana: httpx2.AsyncClient, account: str
) -> None:
    first = await new_txn(ana, account, merchant_raw="GRAB* RIDE 1")
    second = await new_txn(ana, account, merchant_raw="GRAB* RIDE 2")
    other = await new_txn(ana, account, merchant_raw="SHELL KAJANG")
    rides = await category_id(ana, "E-hailing")

    stamped = await stamp(ana, first["id"], rides)
    assert stamped["category_rule_id"] is None  # set by hand

    [rule] = await rules(ana)
    assert rule | {"id": None, "created_at": None, "updated_at": None} == {
        "id": None,
        "match_type": "merchant",
        "pattern": "GRAB RIDE",
        "category_id": rides,
        "source": "learned",
        "hit_count": 1,
        "created_at": None,
        "updated_at": None,
    }
    assert (await txn(ana, second["id"]))["category_id"] == rides
    assert (await txn(ana, second["id"]))["category_rule_id"] == rule["id"]
    assert (await txn(ana, other["id"]))["category_id"] is None

    # A later transaction from the same merchant is stamped as it arrives.
    later = await new_txn(ana, account, merchant_raw="GRAB* RIDE 3")
    assert later["category_id"] == rides
    assert later["category_rule_id"] == rule["id"]


async def test_the_latest_correction_wins(ana: httpx2.AsyncClient, account: str) -> None:
    a = await new_txn(ana, account, merchant_raw="TNG EWALLET RELOAD")
    b = await new_txn(ana, account, merchant_raw="TNG EWALLET RELOAD")
    fees = await category_id(ana, "Bank fees")
    tolls = await category_id(ana, "Toll & parking")

    await stamp(ana, a["id"], fees)
    assert (await txn(ana, b["id"]))["category_id"] == fees
    await stamp(ana, a["id"], tolls)  # changed my mind
    assert (await txn(ana, b["id"]))["category_id"] == tolls
    assert [r["category_id"] for r in await rules(ana)] == [tolls]


async def test_rules_never_overwrite_a_category_set_by_hand(
    ana: httpx2.AsyncClient, account: str
) -> None:
    a = await new_txn(ana, account, merchant_raw="SHOPEE PAY")
    b = await new_txn(ana, account, merchant_raw="SHOPEE PAY")
    online = await category_id(ana, "Online shopping")
    gifts = await category_id(ana, "Gifts")

    await stamp(ana, b["id"], gifts)  # this one was a gift
    await stamp(ana, a["id"], online)  # teaches SHOPEE PAY -> Online shopping
    assert (await txn(ana, b["id"]))["category_id"] == gifts


async def test_clearing_a_category_teaches_nothing(ana: httpx2.AsyncClient, account: str) -> None:
    a = await new_txn(ana, account, merchant_raw="MYSTERY", category_id=None)
    await stamp(ana, a["id"], None)
    assert await rules(ana) == []


async def test_a_new_transaction_with_a_category_teaches_too(
    ana: httpx2.AsyncClient, account: str
) -> None:
    fuel = await category_id(ana, "Fuel")
    await new_txn(ana, account, merchant_raw="PETRONAS SS2", category_id=fuel)
    assert (await new_txn(ana, account, merchant_raw="PETRONAS SS2"))["category_id"] == fuel


# --- user-written rules ----------------------------------------------------------------------


async def test_a_contains_rule_categorises_existing_and_new_transactions(
    ana: httpx2.AsyncClient, account: str
) -> None:
    old = await new_txn(ana, account, merchant_raw="POS DEBIT\nGRABFOOD MY\n01/11/2026")
    food = await category_id(ana, "Eating out")

    change = await new_rule(ana, pattern="grabfood*", category_id=food)
    assert change["rule"]["pattern"] == "GRABFOOD"  # normalised like merchant keys
    assert change["rule"]["source"] == "user"
    assert change["recategorised"] == 1
    assert change["rule"]["hit_count"] == 1
    assert (await txn(ana, old["id"]))["category_id"] == food

    new = await new_txn(ana, account, merchant_raw="GRABFOOD ORDER 99")
    assert new["category_id"] == food


async def test_the_most_specific_rule_wins(ana: httpx2.AsyncClient, account: str) -> None:
    transport = await category_id(ana, "Transport")
    food = await category_id(ana, "Eating out")
    rides = await category_id(ana, "E-hailing")
    await new_rule(ana, pattern="GRAB", category_id=transport)
    await new_rule(ana, pattern="GRAB FOOD", category_id=food)  # longer: more specific

    assert (await new_txn(ana, account, merchant_raw="GRAB FOOD 12"))["category_id"] == food
    assert (await new_txn(ana, account, merchant_raw="GRAB CAR"))["category_id"] == transport

    # An exact merchant rule (learned) beats any "contains" rule.
    car = await new_txn(ana, account, merchant_raw="GRAB CAR")
    await stamp(ana, car["id"], rides)
    assert (await new_txn(ana, account, merchant_raw="GRAB CAR"))["category_id"] == rides


async def test_editing_a_rule_moves_what_it_categorised(
    ana: httpx2.AsyncClient, account: str
) -> None:
    transport = await category_id(ana, "Transport")
    tolls = await category_id(ana, "Toll & parking")
    rule = (await new_rule(ana, pattern="PLUS", category_id=transport))["rule"]
    t = await new_txn(ana, account, merchant_raw="PLUS HIGHWAY")

    response = await ana.patch(f"/api/rules/{rule['id']}", json={"category_id": tolls})
    assert response.json()["recategorised"] == 1
    assert (await txn(ana, t["id"]))["category_id"] == tolls

    # A pattern that no longer matches gives the transaction back (uncategorised).
    await ana.patch(f"/api/rules/{rule['id']}", json={"pattern": "TOUCH N GO"})
    assert (await txn(ana, t["id"]))["category_id"] is None


async def test_deleting_a_rule_uncategorises_or_falls_back(
    ana: httpx2.AsyncClient, account: str
) -> None:
    transport = await category_id(ana, "Transport")
    food = await category_id(ana, "Eating out")
    await new_rule(ana, pattern="GRAB", category_id=transport)
    specific = (await new_rule(ana, pattern="GRAB FOOD", category_id=food))["rule"]
    a = await new_txn(ana, account, merchant_raw="GRAB FOOD 1")
    b = await new_txn(ana, account, merchant_raw="GRAB CAR")

    response = await ana.delete(f"/api/rules/{specific['id']}")
    assert response.json() == {"rule": None, "recategorised": 1}
    assert (await txn(ana, a["id"]))["category_id"] == transport  # the broader rule takes over
    assert (await txn(ana, b["id"]))["category_id"] == transport


async def test_deleting_a_category_deletes_its_rules(ana: httpx2.AsyncClient, account: str) -> None:
    created = (await ana.post("/api/categories", json={"name": "Pets"})).json()
    await new_rule(ana, pattern="PETSMORE", category_id=created["id"])
    t = await new_txn(ana, account, merchant_raw="PETSMORE PJ")
    assert t["category_id"] == created["id"]

    assert (await ana.delete(f"/api/categories/{created['id']}")).status_code == 204
    assert await rules(ana) == []
    assert (await txn(ana, t["id"]))["category_id"] is None


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"pattern": "12/34 -"}, 422),  # no letters left
        ({"pattern": "GR"}, 422),  # too short for "contains"
        ({"pattern": "GR", "match_type": "merchant"}, 201),  # an exact key may be short
        ({"pattern": "GRAB", "user_id": "x"}, 422),  # unknown field
    ],
)
async def test_rule_validation(ana: httpx2.AsyncClient, body: dict[str, Any], status: int) -> None:
    food = await category_id(ana, "Eating out")
    response = await ana.post("/api/rules", json={"category_id": food, **body})
    assert response.status_code == status, response.text


async def test_the_same_pattern_twice_is_a_conflict(ana: httpx2.AsyncClient) -> None:
    food = await category_id(ana, "Eating out")
    await new_rule(ana, pattern="MAMAK", category_id=food)
    response = await ana.post("/api/rules", json={"pattern": "mamak!", "category_id": food})
    assert response.status_code == 409


# --- statement import ------------------------------------------------------------------------


async def test_import_applies_rules(ana: httpx2.AsyncClient, account: str) -> None:
    groceries = await category_id(ana, "Groceries")
    await new_rule(ana, pattern="SUPER MART", category_id=groceries)
    files = {"file": ("s.pdf", make_pdf("J@123456"), "application/pdf")}
    response = await ana.post(
        f"/api/accounts/{account}/statements/import", data={"password": "J@123456"}, files=files
    )
    assert response.json()["categorised"] == 1  # the KK SUPER MART card purchase


# --- isolation -------------------------------------------------------------------------------


async def test_rules_are_per_user(
    ana: httpx2.AsyncClient, bob: httpx2.AsyncClient, account: str
) -> None:
    ana_food = await category_id(ana, "Eating out")
    rule = (await new_rule(ana, pattern="MAMAK", category_id=ana_food))["rule"]
    bob_account = (await new_account(bob))["id"]

    assert (await new_txn(bob, bob_account, merchant_raw="MAMAK"))["category_id"] is None
    assert await rules(bob) == []
    for method in ("get", "patch", "delete"):
        kwargs: dict[str, Any] = {"json": {}} if method == "patch" else {}
        response = await getattr(bob, method)(f"/api/rules/{rule['id']}", **kwargs)
        assert response.status_code in (404, 405), (method, response.status_code)
    # Bob cannot point a rule at Ana's category.
    response = await bob.post("/api/rules", json={"pattern": "MAMAK", "category_id": ana_food})
    assert response.status_code == 422
