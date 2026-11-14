# Dependency budget

Every runtime dependency gets one line here: what it does and why it is worth it.
Targets: Python under 25 direct runtime dependencies, web under 20.

## Python runtime (3 / 25)

| Package | Why |
|---|---|
| fastapi | HTTP API with typed request/response models and generated OpenAPI |
| uvicorn | ASGI server that runs the API |
| pydantic-settings | Typed settings from environment variables |

## Python dev (not in images)

| Package | Why |
|---|---|
| pytest | Tests |
| httpx2 | HTTP client behind FastAPI's TestClient |
| ruff | Lint + format |
| pyright | Strict type checking |
| deptry | Flags unused, missing or misplaced dependencies |
