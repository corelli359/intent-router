#!/usr/bin/env python3
"""Sync the router intent catalog from the business CSV into sqlite and split JSON files."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from export_router_intent_catalog_from_db import (
    _load_env_file,
    resolve_database_url,
    sqlite_path_from_url,
)
from router_catalog_files import write_split_catalog


ROOT = Path(__file__).resolve().parents[1]
ROUTER_SRC = ROOT / "backend" / "services" / "router-service" / "src"
if str(ROUTER_SRC) not in sys.path:
    sys.path.insert(0, str(ROUTER_SRC))

from router_service.catalog.csv_catalog_builder import (  # noqa: E402
    DEFAULT_FALLBACK_AGENT_URL,
    build_csv_catalog_payloads,
)


def parse_args() -> argparse.Namespace:
    """Parse CSV sync and export arguments."""
    parser = argparse.ArgumentParser(
        description="Sync router intents from the screenshot CSV into sqlite and split catalog files."
    )
    parser.add_argument(
        "--csv-file",
        default=str(ROOT / "intent_table_from_updated_screenshot.csv"),
        help="Source CSV exported from the updated screenshot table.",
    )
    parser.add_argument(
        "--catalog-dir",
        default=str(ROOT / "k8s" / "intent" / "router-intent-catalog"),
        help="Existing split catalog directory used to preserve the transfer slot contract.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "k8s" / "intent" / "router-intent-catalog"),
        help="Destination split catalog directory.",
    )
    parser.add_argument(
        "--archive-dir",
        default=str(ROOT / "docs" / "archive" / "router-intent-catalog-pre-csv-switch"),
        help="Archive directory where the previous split catalog is copied before overwrite.",
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
        "--fallback-agent-url",
        default=DEFAULT_FALLBACK_AGENT_URL,
        help="Agent URL used by non-transfer intents that are currently recognition-only.",
    )
    return parser.parse_args()


def _ensure_schema(connection: sqlite3.Connection) -> None:
    """Create or upgrade the sqlite registry table required by the router exporter."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS intent_registry (
            intent_code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            domain_code TEXT NOT NULL DEFAULT '',
            domain_name TEXT NOT NULL DEFAULT '',
            domain_description TEXT NOT NULL DEFAULT '',
            examples_json TEXT NOT NULL DEFAULT '[]',
            agent_url TEXT NOT NULL,
            status TEXT NOT NULL,
            is_fallback INTEGER NOT NULL DEFAULT 0,
            is_leaf_intent INTEGER NOT NULL DEFAULT 1,
            dispatch_priority INTEGER NOT NULL DEFAULT 100,
            request_schema_json TEXT NOT NULL DEFAULT '{}',
            field_mapping_json TEXT NOT NULL DEFAULT '{}',
            field_catalog_json TEXT NOT NULL DEFAULT '[]',
            slot_schema_json TEXT NOT NULL DEFAULT '[]',
            graph_build_hints_json TEXT NOT NULL DEFAULT '{}',
            parent_intent_code TEXT NOT NULL DEFAULT '',
            routing_examples_json TEXT NOT NULL DEFAULT '[]',
            resume_policy TEXT NOT NULL DEFAULT 'resume_same_task',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    existing_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(intent_registry)").fetchall()
    }
    additions = {
        "field_catalog_json": ("TEXT", "'[]'"),
        "slot_schema_json": ("TEXT", "'[]'"),
        "graph_build_hints_json": ("TEXT", "'{}'"),
        "domain_code": ("TEXT", "''"),
        "domain_name": ("TEXT", "''"),
        "domain_description": ("TEXT", "''"),
        "is_leaf_intent": ("INTEGER", "1"),
        "parent_intent_code": ("TEXT", "''"),
        "routing_examples_json": ("TEXT", "'[]'"),
    }
    for column_name, (column_type, default_value) in additions.items():
        if column_name in existing_columns:
            continue
        connection.execute(
            f"ALTER TABLE intent_registry "
            f"ADD COLUMN {column_name} {column_type} NOT NULL DEFAULT {default_value}"
        )


def _json_dump(value: object) -> str:
    """Serialize router catalog structures with stable formatting."""
    return json.dumps(value, ensure_ascii=False)


def _utcnow() -> str:
    """Return the current UTC timestamp in ISO-like text form."""
    return datetime.now(timezone.utc).isoformat(sep=" ")


def sync_sqlite(database_path: Path, *, payloads: list[dict[str, object]]) -> dict[str, object]:
    """Replace the sqlite registry contents with the CSV-derived payloads."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        _ensure_schema(connection)
        synced_codes: list[str] = []
        active_codes: set[str] = set()
        for payload in payloads:
            intent_code = str(payload["intent_code"])
            active_codes.add(intent_code)
            existing_row = connection.execute(
                "SELECT created_at FROM intent_registry WHERE intent_code = ?",
                (intent_code,),
            ).fetchone()
            now = _utcnow()
            created_at = str(existing_row[0]) if existing_row else now
            connection.execute(
                """
                INSERT INTO intent_registry (
                    intent_code,
                    name,
                    description,
                    domain_code,
                    domain_name,
                    domain_description,
                    examples_json,
                    agent_url,
                    status,
                    is_fallback,
                    is_leaf_intent,
                    dispatch_priority,
                    request_schema_json,
                    field_mapping_json,
                    field_catalog_json,
                    slot_schema_json,
                    graph_build_hints_json,
                    parent_intent_code,
                    routing_examples_json,
                    resume_policy,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(intent_code) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    domain_code = excluded.domain_code,
                    domain_name = excluded.domain_name,
                    domain_description = excluded.domain_description,
                    examples_json = excluded.examples_json,
                    agent_url = excluded.agent_url,
                    status = excluded.status,
                    is_fallback = excluded.is_fallback,
                    is_leaf_intent = excluded.is_leaf_intent,
                    dispatch_priority = excluded.dispatch_priority,
                    request_schema_json = excluded.request_schema_json,
                    field_mapping_json = excluded.field_mapping_json,
                    field_catalog_json = excluded.field_catalog_json,
                    slot_schema_json = excluded.slot_schema_json,
                    graph_build_hints_json = excluded.graph_build_hints_json,
                    parent_intent_code = excluded.parent_intent_code,
                    routing_examples_json = excluded.routing_examples_json,
                    resume_policy = excluded.resume_policy,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (
                    intent_code,
                    str(payload["name"]),
                    str(payload["description"]),
                    str(payload.get("domain_code", "") or ""),
                    str(payload.get("domain_name", "") or ""),
                    str(payload.get("domain_description", "") or ""),
                    _json_dump(payload.get("examples", [])),
                    str(payload["agent_url"]),
                    str(payload.get("status", "active")),
                    1 if payload.get("is_fallback") else 0,
                    1 if payload.get("is_leaf_intent", True) else 0,
                    int(payload.get("dispatch_priority", 100) or 100),
                    _json_dump(payload.get("request_schema", {})),
                    _json_dump(payload.get("field_mapping", {})),
                    _json_dump(payload.get("field_catalog", [])),
                    _json_dump(payload.get("slot_schema", [])),
                    _json_dump(payload.get("graph_build_hints", {})),
                    str(payload.get("parent_intent_code", "") or ""),
                    _json_dump(payload.get("routing_examples", [])),
                    str(payload.get("resume_policy", "resume_same_task") or "resume_same_task"),
                    created_at,
                    now,
                ),
            )
            synced_codes.append(intent_code)

        deleted_count = 0
        existing_codes = {
            str(row[0])
            for row in connection.execute("SELECT intent_code FROM intent_registry").fetchall()
        }
        stale_codes = sorted(existing_codes - active_codes)
        if stale_codes:
            placeholders = ",".join("?" for _ in stale_codes)
            deleted_count = connection.execute(
                f"DELETE FROM intent_registry WHERE intent_code IN ({placeholders})",
                tuple(stale_codes),
            ).rowcount
        connection.commit()
        return {
            "synced_codes": synced_codes,
            "deleted_codes": stale_codes,
            "deleted_count": deleted_count,
        }
    finally:
        connection.close()


def archive_catalog(*, source_dir: Path, archive_dir: Path) -> bool:
    """Copy the pre-switch split catalog to the archive directory."""
    if not source_dir.is_dir():
        return False
    if archive_dir.exists():
        return False
    archive_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, archive_dir)
    return True


def main() -> int:
    """Sync the screenshot CSV into sqlite and the split file catalog."""
    args = parse_args()
    env_file = Path(args.env_file).expanduser().resolve() if args.env_file else None
    _load_env_file(env_file)

    catalog_dir = Path(args.catalog_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    archive_dir = Path(args.archive_dir).expanduser().resolve()
    csv_path = Path(args.csv_file).expanduser().resolve()

    archived = archive_catalog(source_dir=output_dir, archive_dir=archive_dir)
    payloads = build_csv_catalog_payloads(
        csv_path=csv_path,
        existing_catalog_dir=catalog_dir,
        fallback_agent_url=args.fallback_agent_url,
    )

    database_url = resolve_database_url(args.database_url)
    database_path = sqlite_path_from_url(database_url)
    sync_result = sync_sqlite(database_path, payloads=payloads)
    write_split_catalog(
        output_dir,
        intents=payloads,
        metadata={
            "source_csv": str(csv_path),
            "exported_count": len(payloads),
        },
    )

    print(
        json.dumps(
            {
                "ok": True,
                "csv_path": str(csv_path),
                "database_path": str(database_path),
                "output_dir": str(output_dir),
                "archive_dir": str(archive_dir),
                "archived_previous_catalog": archived,
                "synced_intents": sync_result["synced_codes"],
                "deleted_intents": sync_result["deleted_codes"],
                "synced_count": len(sync_result["synced_codes"]),
                "deleted_count": sync_result["deleted_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
