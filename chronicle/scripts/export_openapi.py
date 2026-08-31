#!/usr/bin/env python3
"""Write Chronicle's deterministic, checked-in OpenAPI contract."""

import json
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import serve  # noqa: E402


def rendered_schema():
    return json.dumps(serve.app.openapi(), indent=2, sort_keys=True) + "\n"


def main():
    destination = ROOT / "docs" / "openapi.json"
    destination.write_text(rendered_schema(), encoding="utf-8")
    print(destination.relative_to(ROOT))


if __name__ == "__main__":
    main()
