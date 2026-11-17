"""Statement import end to end: upload a (fake) password-protected CIMB PDF, preview, import."""

from typing import Any

import httpx2
import pytest

from tests.fake_cimb import make_pdf

pytestmark = pytest.mark.anyio

PDF_PASSWORD = "J@123456"


@pytest.fixture
async def account(ana: httpx2.AsyncClient) -> str:
    response = await ana.post("/api/accounts", json={"type": "bank", "name": "CIMB savings"})
    return response.json()["id"]


@pytest.fixture(scope="module")
def pdf() -> bytes:
    return make_pdf(PDF_PASSWORD)


async def upload(
    http: httpx2.AsyncClient,
    account_id: str,
    step: str,
    pdf: bytes,
    password: str | None = PDF_PASSWORD,
) -> httpx2.Response:
    data = {"password": password} if password is not None else {}
    files = {"file": ("statement.pdf", pdf, "application/pdf")}
    return await http.post(f"/api/accounts/{account_id}/statements/{step}", data=data, files=files)


async def transactions(http: httpx2.AsyncClient) -> list[dict[str, Any]]:
    return (await http.get("/api/transactions")).json()["items"]


async def test_preview_shows_lines_and_saves_nothing(
    ana: httpx2.AsyncClient, account: str, pdf: bytes
) -> None:
    response = await upload(ana, account, "preview", pdf)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["new_count"], body["duplicate_count"]) == (4, 0)
    assert body["opening_balance_sen"] == 100000
    assert body["lines"][0]["description"].startswith("POS DEBIT")
    assert await transactions(ana) == []


async def test_import_adds_each_line_once(
    ana: httpx2.AsyncClient, account: str, pdf: bytes
) -> None:
    first = await upload(ana, account, "import", pdf)
    again = await upload(ana, account, "import", pdf)

    assert first.json() == {"imported": 4, "skipped_duplicates": 0, "categorised": 0}
    assert again.json() == {"imported": 0, "skipped_duplicates": 4, "categorised": 0}
    rows = await transactions(ana)
    assert len(rows) == 4
    assert sorted(r["amount_sen"] for r in rows) == [-10000, -1250, -800, 500000]
    assert all(r["currency"] == "MYR" and r["category_id"] is None for r in rows)
    preview = (await upload(ana, account, "preview", pdf)).json()
    assert (preview["new_count"], preview["duplicate_count"]) == (0, 4)


async def test_the_stored_statement_password_is_used(
    ana: httpx2.AsyncClient, account: str, pdf: bytes
) -> None:
    assert (await upload(ana, account, "preview", pdf, password=None)).status_code == 422
    await ana.patch(f"/api/accounts/{account}", json={"statement_password": PDF_PASSWORD})

    assert (await upload(ana, account, "preview", pdf, password=None)).status_code == 200


async def test_wrong_password(ana: httpx2.AsyncClient, account: str, pdf: bytes) -> None:
    response = await upload(ana, account, "preview", pdf, password="J@000000")
    assert response.status_code == 422
    assert "password" in response.json()["detail"]


async def test_not_a_pdf(ana: httpx2.AsyncClient, account: str) -> None:
    response = await upload(ana, account, "preview", b"<html>not a statement</html>")
    assert response.status_code == 422


async def test_too_large(ana: httpx2.AsyncClient, account: str) -> None:
    response = await upload(ana, account, "preview", b"%PDF-" + b"0" * (10 * 1024 * 1024))
    assert response.status_code == 413


async def test_account_currency_must_match(ana: httpx2.AsyncClient, pdf: bytes) -> None:
    usd = await ana.post("/api/accounts", json={"type": "bank", "name": "USD", "currency": "USD"})
    assert (await upload(ana, usd.json()["id"], "preview", pdf)).status_code == 422


async def test_cannot_import_into_another_users_account(
    ana: httpx2.AsyncClient, bob: httpx2.AsyncClient, account: str, pdf: bytes
) -> None:
    assert (await upload(bob, account, "import", pdf)).status_code == 404
    assert await transactions(ana) == []
