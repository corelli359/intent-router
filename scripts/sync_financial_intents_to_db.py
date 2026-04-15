#!/usr/bin/env python3
"""Sync the builtin finance intents into the sqlite registry before file export."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from export_router_intent_catalog_from_db import (
    _load_env_file,
    resolve_database_url,
    sqlite_path_from_url,
)
from register_financial_intents import build_payloads


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    """Parse sync source arguments."""
    parser = argparse.ArgumentParser(
        description="Sync builtin finance intents into the sqlite registry."
    )
    parser.add_argument(
        "--env-file",
        default=str(ROOT / ".env.local"),
        help="Optional dotenv file used to resolve ADMIN_DATABASE_URL.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Explicit database URL. Defaults to ROUTER_INTENT_CATALOG_DATABASE_URL or ADMIN_DATABASE_URL.",
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


def sync_sqlite(database_path: Path) -> list[str]:
    """Upsert the builtin finance intents into the target sqlite database."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        _ensure_schema(connection)
        synced_codes: list[str] = []
        for payload in build_payloads():
            intent_code = str(payload["intent_code"])
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
        connection.commit()
        return synced_codes
    finally:
        connection.close()


def main() -> int:
    """Sync builtin finance intents to the sqlite registry used by deployment export."""
    args = parse_args()
    env_file = Path(args.env_file).expanduser().resolve() if args.env_file else None
    _load_env_file(env_file)
    database_url = resolve_database_url(args.database_url)
    database_path = sqlite_path_from_url(database_url)
    synced_codes = sync_sqlite(database_path)
    print(
        json.dumps(
            {
                "ok": True,
                "database_path": str(database_path),
                "synced_intents": synced_codes,
                "synced_count": len(synced_codes),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
