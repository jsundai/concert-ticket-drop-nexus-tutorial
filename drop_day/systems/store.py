"""Tiny SQLite helpers. Each team owns its own database file under data/."""

import json
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
CHAOS_FILE = DATA_DIR / "chaos.json"


def connect(name: str) -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DATA_DIR / f"{name}.db", timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def read_chaos() -> dict:
    try:
        return json.loads(CHAOS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_chaos(**settings: float) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    CHAOS_FILE.write_text(json.dumps(settings))
