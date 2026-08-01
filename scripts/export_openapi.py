from __future__ import annotations

import json
from pathlib import Path

from app.main import app


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    destination = root / "packages" / "contracts" / "openapi.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(app.openapi(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    destination.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
