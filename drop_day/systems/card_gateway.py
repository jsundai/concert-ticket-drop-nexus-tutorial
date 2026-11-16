"""Pretend card gateway. Not Temporal code.

Stands in for a real payment provider's API. Like real gateways, it honors
idempotency keys: charging twice with the same key creates one charge. It also
fails on purpose, at the rate set by the dashboard's chaos slider.
"""

import random

from drop_day.systems.store import connect, read_chaos


class GatewayError(Exception):
    pass


def _db():
    conn = connect("payments")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS charges (
             idempotency_key TEXT PRIMARY KEY, drop_id TEXT, fan_id TEXT, amount_cents INTEGER)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS refunds (
             idempotency_key TEXT PRIMARY KEY, drop_id TEXT, fan_id TEXT, charge_id TEXT, reason TEXT)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS credits (
             idempotency_key TEXT PRIMARY KEY, drop_id TEXT, fan_id TEXT, amount_cents INTEGER, reason TEXT)"""
    )
    conn.execute("CREATE TABLE IF NOT EXISTS hiccups (drop_id TEXT, fan_id TEXT, kind TEXT)")
    return conn


def _id(prefix: str, key: str) -> str:
    return f"{prefix}_{abs(hash(key)) % 10**8:08d}"


def charge(idempotency_key: str, drop_id: str, fan_id: str, amount_cents: int) -> str:
    flakiness = read_chaos().get("gateway_flakiness", 0.3)
    conn = _db()
    try:
        roll = random.random()
        if roll < flakiness / 2:
            conn.execute("INSERT INTO hiccups VALUES (?, ?, '503')", (drop_id, fan_id))
            raise GatewayError("Gateway returned 503 Service Unavailable")
        conn.execute(
            "INSERT OR IGNORE INTO charges VALUES (?, ?, ?, ?)", (idempotency_key, drop_id, fan_id, amount_cents)
        )
        if roll < flakiness:
            # The worst case: the card was charged, but the response never arrived.
            conn.execute("INSERT INTO hiccups VALUES (?, ?, 'lost-response')", (drop_id, fan_id))
            raise GatewayError("Charged, but the gateway response timed out")
        return _id("ch", idempotency_key)
    finally:
        conn.close()


def refund(idempotency_key: str, drop_id: str, fan_id: str, charge_id: str, reason: str) -> str:
    conn = _db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO refunds VALUES (?, ?, ?, ?, ?)",
            (idempotency_key, drop_id, fan_id, charge_id, reason),
        )
        return _id("re", idempotency_key)
    finally:
        conn.close()


def credit(idempotency_key: str, drop_id: str, fan_id: str, amount_cents: int, reason: str) -> str:
    conn = _db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO credits VALUES (?, ?, ?, ?, ?)",
            (idempotency_key, drop_id, fan_id, amount_cents, reason),
        )
        return _id("cr", idempotency_key)
    finally:
        conn.close()


def ledger(drop_id: str) -> dict:
    """What an auditor sees for one drop. A duplicate is a fan with more than one charge."""
    conn = _db()
    try:
        q = lambda sql: conn.execute(sql, (drop_id,)).fetchone()[0]  # noqa: E731
        return {
            "charges": q("SELECT COUNT(*) FROM charges WHERE drop_id=?"),
            "duplicates": q(
                "SELECT COUNT(*) FROM (SELECT fan_id FROM charges WHERE drop_id=? GROUP BY fan_id HAVING COUNT(*) > 1)"
            ),
            "refunds": q("SELECT COUNT(*) FROM refunds WHERE drop_id=?"),
            "credits": q("SELECT COUNT(*) FROM credits WHERE drop_id=?"),
            "hiccups_503": q("SELECT COUNT(*) FROM hiccups WHERE drop_id=? AND kind='503'"),
            "hiccups_lost": q("SELECT COUNT(*) FROM hiccups WHERE drop_id=? AND kind='lost-response'"),
        }
    finally:
        conn.close()
