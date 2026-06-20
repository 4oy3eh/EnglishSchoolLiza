"""Regenerate JSON Schema from the Pydantic contracts (source of truth).

Run via `make schema` (or `python -m contracts.export_jsonschema`). Writes one
`<name>.json` per entry in `contracts.REGISTRY` into `contracts/jsonschema/`.
Keep the generated files committed so schema drift shows up in code review.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from app.core.logging import configure_logging, get_logger
from contracts import REGISTRY

log = get_logger(__name__)

OUT_DIR = Path(__file__).resolve().parent / "jsonschema"


def schema_for(model: object) -> dict[str, Any]:
    """JSON Schema for a BaseModel subclass or an annotated discriminated union."""
    if inspect.isclass(model) and issubclass(model, BaseModel):
        return model.model_json_schema()
    # Annotated[Union[...], Field(discriminator=...)] — not a class.
    return TypeAdapter(model).json_schema()


def export(out_dir: Path = OUT_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, model in REGISTRY.items():
        schema = schema_for(model)
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", "utf-8")
        log.info("schema export %-18s -> %s", name, path.name)
        written.append(path)
    return written


def main() -> None:
    configure_logging()
    paths = export()
    log.info("schema export complete: %d file(s) in %s", len(paths), OUT_DIR)


if __name__ == "__main__":
    main()
