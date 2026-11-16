"""Write the API's OpenAPI schema to web/openapi.json, the input of the generated web client.

    uv run python -m tools.export_openapi        (from backend/)

CI runs it and fails if the committed file differs, so the web client can never silently drift
from the API it calls.
"""

import json
import os
from pathlib import Path

# Building the schema needs no database; settings only require the variable to exist.
os.environ.setdefault("DUIT_DATABASE_URL", "postgresql://schema-export@localhost/unused")

from duit.main_api import app  # noqa: E402  (after the environment is prepared)

OUT = Path(__file__).resolve().parents[2] / "web" / "openapi.json"


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
