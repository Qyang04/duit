"""API entrypoint: `uvicorn duit.main_api:app`."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from duit.core.db import create_engine
from duit.core.settings import get_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # One connection pool per process, opened at startup and closed at shutdown.
    app.state.engine = create_engine(settings)
    yield
    await app.state.engine.dispose()


app = FastAPI(
    title="Duit API",
    lifespan=lifespan,
    # Interactive docs only outside prod; the schema itself stays available for the web client.
    docs_url=None if settings.environment == "prod" else "/docs",
    redoc_url=None,
)


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
