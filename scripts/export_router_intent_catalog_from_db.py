#!/usr/bin/env python3
"""Export router intent catalog JSON from the current sqlite database."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from router_catalog_files import write_split_catalog


ROOT = Path(__file__).resolve().parents[1]


def _load_env_file(env_file: Path | None) -> None:
    """Load one dotenv-style file into the current process without overriding set vars."""
    if env_file is None or not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def parse_args() -> argparse.Namespace:
    """Parse export source and destination arguments."""
    parser = argparse.ArgumentParser(
        description="Export router intent catalog from sqlite into JSON."
    )
    parser.add_argument(
        "--env-file",
        default=str(ROOT / ".env.local"),
        help="Optional dotenv file used to resolve ROUTER_INTENT_CATALOG_DATABASE_URL.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Explicit database URL. Defaults to ROUTER_INTENT_CATALOG_DATABASE_URL.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "k8s" / "intent" / "router-intent-catalog"),
        help="Output directory for split JSON catalog files.",
    )
    parser.add_argument(
        "--status",
        choices=("all", "active", "inactive", "grayscale"),
        default="all",
        help="Optional status filter before export.",
    )
    return parser.parse_args()


def resolve_database_url(explicit_database_url: str | None) -> str:
    """Resolve the database URL from args or environment."""
    if explicit_database_url:
        return explicit_database_url
    database_url = os.getenv("ROUTER_INTENT_CATALOG_DATABASE_URL")
    if not database_url:
        raise RuntimeError("Missing database URL. Set --database-url or ROUTER_INTENT_CATALOG_DATABASE_URL.")
    return database_url


def sqlite_path_from_url(database_url: str) -> Path:
    """Translate a sqlite SQLAlchemy URL into a filesystem path."""
    if not database_url.startswith("sqlite:///"):
        raise RuntimeError(
            f"Only sqlite URLs are supported by this exporter for now, got: {database_url}"
        )
    database_path = database_url.removeprefix("sqlite:///")
    return Path(database_path).expanduser().resolve()


def _loads_json(raw_value: object, default: object) -> object:
    """Load one JSON column value or return the provided default."""
    if raw_value in (None, ""):
        return default
    if isinstance(raw_value, (dict, list)):
        return raw_value
    return json.loads(str(raw_value))


def _coerce_bool(raw_value: object, default: bool = False) -> bool:
    """Normalize sqlite boolean-like values."""
    if raw_value is None:
        return default
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, (int, float)):
        return bool(raw_value)
    return str(raw_value).strip().lower() in {"1", "true", "yes", "on"}


def export_sqlite_catalog(database_path: Path, status_filter: str) -> list[dict[str, object]]:
    """Read the sqlite intent registry and convert rows into file-mode catalog objects."""
    if not database_path.is_file():
        raise RuntimeError(f"SQLite database does not exist: {database_path}")

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        query = "SELECT * FROM intent_registry"
        params: tuple[object, ...] = ()
        if status_filter != "all":
            query += " WHERE status = ?"
            params = (status_filter,)
        query += " ORDER BY created_at ASC, intent_code ASC"
        rows = connection.execute(query, params).fetchall()
    finally:
        connection.close()

    intents: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        intents.append(
            {
                "intent_code": item["intent_code"],
                "name": item["name"],
                "description": item["description"],
                "domain_code": item.get("domain_code", "") or "",
                "domain_name": item.get("domain_name", "") or "",
                "domain_description": item.get("domain_description", "") or "",
                "examples": _loads_json(item.get("examples_json"), []),
                "agent_url": item["agent_url"],
                "is_leaf_intent": _coerce_bool(item.get("is_leaf_intent"), True),
                "parent_intent_code": item.get("parent_intent_code", "") or "",
                "status": item["status"],
                "routing_examples": _loads_json(item.get("routing_examples_json"), []),
                "is_fallback": _coerce_bool(item.get("is_fallback"), False),
                "dispatch_priority": int(item.get("dispatch_priority", 100) or 100),
                "request_schema": _loads_json(item.get("request_schema_json"), {}),
                "field_mapping": _loads_json(item.get("field_mapping_json"), {}),
                "field_catalog": _loads_json(item.get("field_catalog_json"), []),
                "slot_schema": _loads_json(item.get("slot_schema_json"), []),
                "graph_build_hints": _loads_json(item.get("graph_build_hints_json"), {}),
                "resume_policy": item.get("resume_policy", "resume_same_task") or "resume_same_task",
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
        )
    return intents


def main() -> int:
    """Export the current sqlite-backed catalog into the target JSON file."""
    args = parse_args()
    env_file = Path(args.env_file).expanduser().resolve() if args.env_file else None
    _load_env_file(env_file)

    database_url = resolve_database_url(args.database_url)
    database_path = sqlite_path_from_url(database_url)
    intents = export_sqlite_catalog(database_path, args.status)

    output_dir = Path(args.output_dir).expanduser().resolve()
    write_split_catalog(
        output_dir,
        intents=intents,
        metadata={
            "source_database_url": database_url,
            "exported_count": len(intents),
            "status_filter": args.status,
        },
    )
    print(f"[OK] exported {len(intents)} intents from sqlite to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
