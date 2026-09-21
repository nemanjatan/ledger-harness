"""Connection and schema helpers. The DSN comes from LEDGER_DSN or defaults to the compose db."""

import os
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

DEFAULT_DSN = "postgresql://ledger:ledger@localhost:5435/ledger"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Sandbox roles created by schema.sql. Password equals role name; the data is on tmpfs.
AGENT = "ledger_agent"
REVIEWER = "ledger_reviewer"


def dsn(role: str | None = None) -> str:
    base = os.environ.get("LEDGER_DSN", DEFAULT_DSN)
    if role is None:
        return base
    params = conninfo_to_dict(base)
    params["user"] = role
    params["password"] = role
    return make_conninfo(**params)


def connect(role: str | None = None, **kwargs) -> psycopg.Connection:
    """Connect as the owner (default) or as one of the sandbox roles."""
    return psycopg.connect(dsn(role), **kwargs)


def reset_schema(conn: psycopg.Connection) -> None:
    """Drop everything and apply schema.sql from scratch. Sandbox only; never migrate."""
    with conn.cursor() as cur:
        cur.execute("drop schema public cascade; create schema public;")
        cur.execute(SCHEMA_PATH.read_text())
    conn.commit()


def reset_data(conn: psycopg.Connection) -> None:
    """Empty every table and restart the id sequences, so a rebuilt world gets the same ids."""
    conn.execute("truncate companies, audit_log restart identity cascade")
    conn.commit()
