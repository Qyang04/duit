"""API entrypoint: `uvicorn duit.main_api:app`."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from duit.auth.router import router as auth_router
from duit.core.crypto import load_data_key
from duit.core.db import create_engine
from duit.core.settings import get_settings
from duit.ledger import accounts, categories, transactions

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    load_data_key(settings)  # fail fast: a pod without its data key never becomes ready
    # One connection pool per process, opened at startup and closed at shutdown.
    app.state.engine = create_engine(settings)
    # expire_on_commit=False: objects stay readable after commit (responses are built from them).
    app.state.sessionmaker = async_sessionmaker(app.state.engine, expire_on_commit=False)
    yield
    await app.state.engine.dispose()


app = FastAPI(
    title="Duit API",
    lifespan=lifespan,
    # Interactive docs only outside prod; the schema itself stays available for the web client.
    docs_url=None if settings.environment == "prod" else "/docs",
    redoc_url=None,
)
app.include_router(auth_router)
for ledger_router in (accounts.router, categories.router, transactions.router):
    app.include_router(ledger_router)


@app.get("/healthz", tags=["ops"])
def healthz() -> dict[str, str]:
    """Liveness: the process is up. Checks no dependencies, so a database outage never
    makes Kubernetes restart healthy API pods."""
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readyz(request: Request, response: Response) -> dict[str, str]:
    """Readiness: can serve real requests. 503 takes the pod out of the Service until the
    database is back."""
    engine: AsyncEngine = request.app.state.engine
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "down"}
    return {"status": "ok", "database": "up"}
