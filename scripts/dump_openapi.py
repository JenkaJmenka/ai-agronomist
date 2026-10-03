"""
Regenerate web/openapi.json from the live FastAPI app definition.

Importing ``src.api.main`` and calling ``app.openapi()`` builds the schema
in-process from the route/model definitions — it takes no network
connection and opens no database session. ``app.openapi()`` is unaffected
by the ``docs_url``/``redoc_url``/``openapi_url`` gating in
src/api/main.py: that gating only controls whether the interactive
docs/redoc HTTP routes are mounted, not whether the schema can be built.
So this script runs under the default ENVIRONMENT ("production") with no
override needed.

Usage:
    ./venv/bin/python scripts/dump_openapi.py
"""

import json
import os
import sys
from pathlib import Path

# Add the repo root to sys.path so 'src' can be imported when this script
# is invoked directly (matches the convention used by other scripts/ files).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.api.main import app  # noqa: E402

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "web" / "openapi.json"


def main() -> None:
    schema = app.openapi()
    # 2-space indent, no sort_keys (preserve FastAPI's own key order), and no
    # trailing newline — this matches the format of the committed file.
    serialized = json.dumps(schema, indent=2)
    OUTPUT_PATH.write_text(serialized)


if __name__ == "__main__":
    main()
