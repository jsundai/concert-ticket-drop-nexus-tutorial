"""Pretend seat database. Not Temporal code.

Stands in for the venue's real inventory system. Every call is safe to repeat:
a fan who already holds a seat gets the same seat back.
"""

from drop_day.systems.store import connect

SECTIONS = ["Pit", "Floor", "Lower Bowl", "Upper Bowl", "Nosebleeds"]


class SoldOut(Exception):
    pass


class DropNotOpen(Exception):
    pass


def _label(seat_no: int, capacity: int) -> str:
    section = SECTIONS[min(seat_no * len(SECTIONS) // capacity, len(SECTIONS) - 1)]
    return f"{section} · Row {chr(65 + (seat_no // 10) % 26)} · Seat {seat_no % 10 + 1}"


def _db():
    conn = connect("seats")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS drops (
             drop_id TEXT PRIMARY KEY, capacity INTEGER, hold_seconds INTEGER)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS inventory (
             drop_id TEXT, seat_no INTEGER, label TEXT, fan_id TEXT, status TEXT,
             PRIMARY KEY (drop_id, seat_no))"""
    )
    return conn


def open_drop(drop_id: str, capacity: int, hold_seconds: int) -> int:
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO drops VALUES (?, ?, ?)", (drop_id, capacity, hold_seconds))
        conn.executemany(
            "INSERT OR IGNORE INTO inventory VALUES (?, ?, ?, NULL, 'free')",
            [(drop_id, n, _label(n, capacity)) for n in range(capacity)],
        )
        conn.execute("COMMIT")
        return capacity
    finally:
        conn.close()


def reserve(drop_id: str, fan_id: str) -> tuple[str, int]:
    """Hold the next free seat for a fan. Returns (seat label, hold seconds)."""
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        drop = conn.execute("SELECT hold_seconds FROM drops WHERE drop_id=?", (drop_id,)).fetchone()
        if drop is None:
            conn.execute("COMMIT")
            raise DropNotOpen(f"{drop_id} is not open")
        mine = conn.execute(
            "SELECT label FROM inventory WHERE drop_id=? AND fan_id=? AND status IN ('held','sold')",
            (drop_id, fan_id),
        ).fetchone()
        if mine:
            conn.execute("COMMIT")
            return mine[0], drop[0]
        free = conn.execute(
            "SELECT seat_no, label FROM inventory WHERE drop_id=? AND status='free' ORDER BY seat_no LIMIT 1",
            (drop_id,),
        ).fetchone()
        if free is None:
            conn.execute("COMMIT")
            raise SoldOut("Sold out")
        conn.execute(
            "UPDATE inventory SET status='held', fan_id=? WHERE drop_id=? AND seat_no=?", (fan_id, drop_id, free[0])
        )
        conn.execute("COMMIT")
        return free[1], drop[0]
    finally:
        conn.close()


def mark_sold(drop_id: str, fan_id: str) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE inventory SET status='sold' WHERE drop_id=? AND fan_id=? AND status='held'", (drop_id, fan_id)
        )
    finally:
        conn.close()


def free(drop_id: str, fan_id: str) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE inventory SET status='free', fan_id=NULL WHERE drop_id=? AND fan_id=? AND status='held'",
            (drop_id, fan_id),
        )
    finally:
        conn.close()


def seat_map(drop_id: str) -> list[tuple[str, str, str | None]]:
    """(label, status, fan_id) for every seat, in order. Used by the dashboard."""
    conn = _db()
    try:
        return conn.execute(
            "SELECT label, status, fan_id FROM inventory WHERE drop_id=? ORDER BY seat_no", (drop_id,)
        ).fetchall()
    finally:
        conn.close()
