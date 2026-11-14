"""API entrypoint: `uvicorn duit.main_api:app`."""

from fastapi import FastAPI

from duit.core.settings import get_settings

settings = get_settings()

app = FastAPI(
    title="Duit API",
    # Interactive docs only outside prod; the schema itself stays available for the web client.
    docs_url=None if settings.environment == "prod" else "/docs",
    redoc_url=None,
)


@app.get("/healthz", tags=["ops"])
def healthz() -> dict[str, str]:
    """Liveness: the process is up and serving. Checks no dependencies on purpose."""
    return {"status": "ok"}
