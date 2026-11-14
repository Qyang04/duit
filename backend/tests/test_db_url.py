import pytest

from duit.core.db import async_url


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql://duit:p%40ss@db:5432/duit",  # CloudNativePG `uri` form
        "postgres://duit:p%40ss@db:5432/duit",
        "postgresql+asyncpg://duit:p%40ss@db:5432/duit",  # already explicit
    ],
)
def test_async_url_uses_asyncpg_and_keeps_the_password(raw: str) -> None:
    url = async_url(raw)

    assert url.drivername == "postgresql+asyncpg"
    assert url.password == "p@ss"  # URL-encoded characters survive
    assert url.host == "db"
    assert url.database == "duit"
